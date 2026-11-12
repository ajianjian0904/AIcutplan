# -*- coding: utf-8 -*-
"""L0·内容指纹与缓存失效：根治「文件存在 = 内容最新」这个反复复发的设计缺陷。

所属层：L0 基础层 ｜ 职责：判定产物是否仍匹配其输入 ｜ 副作用：只读

=============================================================================
背景（本项目真实事故清单，值得每个新脚本作者读一遍）
=============================================================================
* ``r5_tts.py --only`` 见 mp3 存在就跳过合成 → 文本是新词、成片念旧词；
* ``qc_visual.py`` 抽帧缓存按句号命名 → 拿 v2 的旧帧判 v3.4 成片；
* 批量删除被 safe-delete 钩子拦截 → 114 个旧 mp3 残留冒充新句；
* 译中缓存按 ``md5(工程目录)`` 分目录 → 换 ep01cf 后读不到（反向失效）；
* ``cut.py`` 分段缓存只按组序号命名 → 改 cutplan 后画面旧、音轨新。

``cut.py`` 后来用「窗口指纹」修好了自己，但经验没有推广。本模块把它变成
全流水线共用的基础设施：**缓存键必须包含内容版本**，读缓存时比对。

=============================================================================
三种判定
=============================================================================
* :func:`is_stale`      —— 按 mtime：产物早于任一输入即过期（粗粒度，零成本）。
* :func:`sig_valid`     —— 按显式指纹：产物 JSON 里记录的 sig 与当前输入算出的
                           sig 比对（精确，推荐）。
* :func:`cache_valid`   —— 两者叠加，并支持自定义 expected_sig。

产物写法约定：写产物时把 :func:`content_sig` 的结果存进顶层 ``sig`` 字段，
读的时候用 :func:`sig_valid` 校验。``json/ja-segments.json`` 已按此实现。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable

from .io import load_json


def content_sig(*parts: Any) -> str:
    """任意来源组合 → 12 位指纹。内容相同的输入必得同一指纹。"""
    raw = "|".join(str(p) for p in parts)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def file_sig(path: str | Path) -> str:
    """单文件指纹：路径 + 大小 + mtime（秒）+ 内容前 64KB 的 hash。

    只比 mtime 不足以发现「同秒覆盖且大小相同」的情况，故附带内容摘要。
    """
    p = Path(path)
    if not p.exists():
        return content_sig(str(p.resolve()), "missing")
    st = p.stat()
    with p.open("rb") as f:
        head = hashlib.md5(f.read(65536)).hexdigest()[:8]
    return content_sig(str(p.resolve()), st.st_size, int(st.st_mtime), head)


def source_sig(video: str | Path, stream: dict) -> str:
    """源片 + 字幕流的指纹。**必须与 subs_extract 旧实现逐字一致**，
    否则已写盘的 ``ja-segments.json`` / ``subs_meta.json`` 的 sig 会全部失配。
    """
    v = Path(video)
    st = v.stat()
    raw = (f"{v.resolve()}|{st.st_size}|{int(st.st_mtime)}"
           f"|{stream['index']}|{stream['codec']}")
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def project_key(proj_dir: str | Path) -> str:
    """工程目录 → 12 位缓存键（与 asr_pipeline 的 pkey 口径一致）。

    注意这是**按路径**分缓存，不是按内容：换工程目录会自然失效，
    但同一目录内换源片不会失效 —— 那种情况必须额外用 :func:`source_sig`。
    """
    return hashlib.md5(str(Path(proj_dir).resolve()).encode()).hexdigest()[:12]


def is_stale(artifact: str | Path,
             sources: Iterable[str | Path] | str | Path) -> bool:
    """产物是否存在、且**不早于**所有输入。任一输入更新即返回 True。"""
    a = Path(artifact)
    if not a.exists():
        return True
    if isinstance(sources, (str, Path)):
        sources = [sources]
    a_mtime = a.stat().st_mtime
    for s in sources:
        p = Path(s)
        if p.exists() and p.stat().st_mtime > a_mtime:
            return True
    return False


def stale_reason(artifact: str | Path,
                 sources: Iterable[str | Path] | str | Path) -> str:
    """给人类看的失效原因（空字符串表示新鲜），用于日志与 --check 输出。"""
    a = Path(artifact)
    if not a.exists():
        return f"产物不存在: {a}"
    if isinstance(sources, (str, Path)):
        sources = [sources]
    a_mtime = a.stat().st_mtime
    for s in sources:
        p = Path(s)
        if p.exists() and p.stat().st_mtime > a_mtime:
            return f"输入比产物新: {p.name}"
    return ""


def sig_valid(manifest: str | Path, expected: str,
              *, key: str = "sig") -> tuple[bool, str]:
    """校验产物 JSON 顶层记录的指纹是否与预期一致。

    返回 ``(是否新鲜, 说明)``。产物不存在/无 sig 字段 → 判为不新鲜，
    迫使调用方重新生成，而不是「先跑着再说」。
    """
    p = Path(manifest)
    data = load_json(p, default={})
    if not isinstance(data, dict):
        return False, f"产物格式异常: {p.name}"
    got = str(data.get(key) or "")
    if not got:
        return False, f"产物缺少 {key} 字段: {p.name}"
    if got != expected:
        return False, f"指纹不匹配: 产物 {got} / 当前输入 {expected}"
    return True, "ok"


def cache_valid(artifact: str | Path,
                sources: Iterable[str | Path] | str | Path,
                *, expected_sig: str | None = None,
                sig_key: str = "sig") -> tuple[bool, str]:
    """完整判定：mtime 新鲜 +（可选）指纹一致。返回 ``(bool, 原因)``。"""
    reason = stale_reason(artifact, sources)
    if reason:
        return False, reason
    if expected_sig is not None:
        return sig_valid(artifact, expected_sig, key=sig_key)
    return True, "ok"


def write_manifest(path: str | Path, data: dict, sig: str,
                   *, key: str = "sig") -> None:
    """写产物并把指纹写进顶层字段，配套 :func:`sig_valid` 使用。"""
    from .io import dump_json
    payload = dict(data)
    payload[key] = sig
    dump_json(path, payload)
