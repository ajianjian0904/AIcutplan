# -*- coding: utf-8 -*-
# [L0 基础层] 职责：渲染前「上游是否最新」判定（内容指纹基线 + mtime 兜底）
# 调用者：cli guard / cf_pipeline / qc_loop / 工程侧 cut·r5·r6·r7
"""渲染门禁：保证成片只用最新一轮产物。

**要解决的问题**：改了一句旁白却直接跑 r6/r7，成片会混入旧音频；改了窗口表却没
重跑 cut.py，画面还是上一版。历史上这两类事故都真实发生过（039.mp3、v2 帧判 v3）。
``workspace.scan_stale`` 只负责“告警”，本模块负责给出**可阻断的判定**。

**为什么不用 mtime 当主判据**：同一份内容会被后续动作重写（归档补指纹、elastic_fit
重写 tts-result），mtime 变新但内容等价 —— EP01 实测过，纯 mtime 会误报成“成片过期”。
所以主判据是**内容指纹基线**：每个阶段产出后，把「它读到的输入」的指纹记进
``json/render_state.json``。判新时重算输入指纹，与基线逐项比对：

* 全部一致 → 该阶段产物就是这批输入生成的（哪怕文件很旧）；
* 有差异   → 明确指出「哪个输入变了、要补跑哪一步」；
* 无基线   → 无法证明最新，渲染门禁判失败，提示 ``guard --bootstrap``。

**阶段与输入**::

    cut  ← cutplan / ja-segments / 01 中间片            → 02 画面 / base-rapid.wav
    tts  ← narration / 音色语速                         → tts/*.mp3 + tts-result.json
    mix  ← narration / tts-result / cutplan / base-rapid / 全部 mp3 → mix-rapid.wav
    mux  ← narration / cutplan / mix / 02               → 03 成片

**不做什么**：不写产物、不调 ffmpeg。只写 ``json/render_state.json`` 这一份基线。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from .io import dump_json, load_json
from .paths import ProjectPaths

#: 阶段顺序（上游在前）
STAGES: tuple[str, ...] = ("cut", "tts", "mix", "mux")

#: 阶段中文名
STAGE_CN: dict[str, str] = {
    "cut": "画面粗剪（cut.py）",
    "tts": "配音（r5_tts + elastic_fit）",
    "mix": "混音（r6_mix）",
    "mux": "封装（r7_mux）",
}

#: 渲染 S 之前必须先保证新鲜的上游阶段
UPSTREAM: dict[str, tuple[str, ...]] = {
    "cut": (),
    "tts": (),
    "mix": ("cut", "tts"),
    "mux": ("cut", "mix"),
}

#: 各阶段真正读取的文件
INPUT_FILES: dict[str, tuple[str, ...]] = {
    "cut": ("json/cutplan.json", "json/ja-segments.json", "out/01-video-no-ad.mp4"),
    "tts": ("json/narration.json",),
    "mix": ("json/narration.json", "json/tts-result.json",
            "json/cutplan.json", "audio/base-rapid.wav"),
    "mux": ("json/narration.json", "json/cutplan.json",
            "out/mix-rapid.wav", "out/02-video-rapid.mp4"),
}

#: 各阶段生成的产物（用于发现「产物被替换」）
OUTPUT_FILES: dict[str, tuple[str, ...]] = {
    "cut": ("out/02-video-rapid.mp4", "audio/base-rapid.wav"),
    "tts": ("json/tts-result.json",),
    "mix": ("out/mix-rapid.wav",),
    "mux": ("out/03-rapid-narration.mp4",),
}

#: 只对 mix 生效：全部 mp3 的指纹也算输入（单句重配必须重混）
MIX_MP3 = True

#: mtime 兜底容差（同一次运行多文件可能同秒）
TOL = 1.0

#: 基线文件
STATE_FILE = "json/render_state.json"


class _Ctx:
    """一次判定的路径与指纹缓存。"""

    def __init__(self, project: str | Path):
        self.p = ProjectPaths(project)
        self.root = self.p.root
        self._sig: dict[str, str] = {}
        data = load_json(self.root / STATE_FILE, default={}) or {}
        self.state: dict[str, dict] = data.get("stages") or {}

    def path(self, rel: str) -> Path:
        return self.root / rel

    def mtime(self, rel: str) -> float:
        try:
            return self.path(rel).stat().st_mtime
        except OSError:
            return -1.0

    def sig(self, rel: str) -> str:
        """内容指纹：小文件全量 sha1、大文件首尾 4MB（与 workspace.quick_sig 同口径）。"""
        if rel in self._sig:
            return self._sig[rel]
        p = self.path(rel)
        try:
            st = p.stat()
        except OSError:
            self._sig[rel] = ""
            return ""
        h = hashlib.sha1(str(st.st_size).encode())
        with p.open("rb") as f:
            if st.st_size <= (8 << 20):
                h.update(f.read())
            else:
                h.update(f.read(4 << 20))
                f.seek(max(0, st.st_size - (4 << 20)))
                h.update(f.read(4 << 20))
        self._sig[rel] = h.hexdigest()[:16]
        return self._sig[rel]

    def cfg(self) -> dict:
        return load_json(self.p.project, default={}) or {}


def _newer(a: float, b: float) -> bool:
    return a >= b - TOL


def _row(stage: str, status: str, detail: str = "", fix: str = "",
         source: str = "") -> dict:
    return {"stage": stage, "status": status, "detail": detail,
            "fix": fix, "source": source}


def _inputs(c: _Ctx, stage: str) -> dict[str, str]:
    """该阶段当前输入的内容指纹表（键含 mp3 与音色/语速）。"""
    out: dict[str, str] = {}
    for rel in INPUT_FILES[stage]:
        out[rel] = c.sig(rel)
    if stage in ("tts", "mix"):
        rc = c.cfg().get("recap") or {}
        out["cfg.voice"] = str(rc.get("voice") or "")
        out["cfg.rate"] = str(rc.get("tts_rate") or "")
    if stage == "mix":
        for f in sorted(c.p.tts.glob("*.mp3")):
            out[f"tts/{f.name}"] = c.sig(f"tts/{f.name}")
    return out


def _inputs_digest(inputs: dict[str, str]) -> str:
    text = "\n".join(f"{k}\t{v}" for k, v in sorted(inputs.items()))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _outputs(c: _Ctx, stage: str) -> dict[str, dict]:
    outs: dict[str, dict] = {}
    for rel in OUTPUT_FILES[stage]:
        p = c.path(rel)
        outs[rel] = {"sig": c.sig(rel) if p.exists() else "",
                     "bytes": p.stat().st_size if p.exists() else 0}
    if stage == "tts":
        for f in sorted(c.p.tts.glob("*.mp3")):
            rel = f"tts/{f.name}"
            outs[rel] = {"sig": c.sig(rel), "bytes": f.stat().st_size}
    return outs


# ---------------------------------------------------------------- 各阶段判定

def _mtime_problem(c: _Ctx, stage: str) -> tuple[str, str]:
    """无基线时的粗判：产物是否早于它的输入。返回 (问题, 修复建议)。"""
    if stage == "cut":
        pic, plan = "out/02-video-rapid.mp4", "json/cutplan.json"
        # 不拿 base-rapid 比 cutplan：cut.py 先写 base、最后回写吸附后的 cutplan，
        # 天然会让 cutplan 比 base 新几秒（不是过期）。
        if not _newer(c.mtime(pic), c.mtime(plan)):
            return "窗口表比粗剪画面新（cutplan 改过但没重剪）", "跑 cut.py --force"
    if stage == "mix":
        mix = c.mtime("out/mix-rapid.wav")
        for rel, what in (("json/tts-result.json", "TTS 对齐结果"),
                          ("json/narration.json", "旁白表"),
                          ("json/cutplan.json", "窗口表"),
                          ("audio/base-rapid.wav", "粗剪底座")):
            if not _newer(mix, c.mtime(rel)):
                return f"{what}比混音新（改了却没重混）", "重跑 r6_mix.py"
        newest = max((f.stat().st_mtime for f in c.p.tts.glob("*.mp3")), default=-1.0)
        if newest > 0 and not _newer(mix, newest):
            return "有句子重配了音但没重混", "重跑 r6_mix.py"
    if stage == "mux":
        t = c.mtime("out/03-rapid-narration.mp4")
        for rel, what in (("out/mix-rapid.wav", "混音"),
                          ("out/02-video-rapid.mp4", "粗剪画面"),
                          ("json/cutplan.json", "窗口表"),
                          ("json/narration.json", "旁白表")):
            if not _newer(t, c.mtime(rel)):
                return f"{what}比成片新（成片还是上一版）", "重跑 r7_mux.py（必要时先 r6）"
    return "", ""


def _check_presence(c: _Ctx, stage: str) -> dict | None:
    for rel in INPUT_FILES[stage]:
        if not c.path(rel).exists():
            return _row(stage, "stale", f"缺少上游输入 {rel}", "先补齐该文件")
    for rel in OUTPUT_FILES[stage]:
        if not c.path(rel).exists():
            return _row(stage, "stale", f"缺少产物 {rel}",
                        {"cut": "跑 cut.py --force", "tts": "跑 r5_tts.py",
                         "mix": "跑 r6_mix.py", "mux": "跑 r7_mux.py"}[stage])
    return None


def _check_tts_content(c: _Ctx) -> str:
    """内容级兜底：旁白文字 ≡ TTS 文字、mp3 指纹 ≡ 记录（防旧音频冒充新句）。"""
    narr = {int(x.get("i", -1)): x
            for x in (load_json(c.path("json/narration.json"), default={}) or {}).get("items", [])}
    data = load_json(c.path("json/tts-result.json"), default={}) or {}
    tts = {int(x.get("i", -1)): x for x in data.get("items", [])}
    if not narr:
        return "旁白表为空"
    missing = sorted(set(narr) - set(tts))
    if missing:
        return f"{len(missing)} 句旁白没有对应 TTS（如句 {missing[:3]}）"
    diff = [i for i in sorted(narr)
            if str(narr[i].get("text", "")) != str(tts[i].get("text", ""))]
    if diff:
        return f"{len(diff)} 句旁白文字与 TTS 不一致（如句 {diff[:3]}）"
    rc = c.cfg().get("recap") or {}
    if rc.get("voice") and data.get("voice") and data["voice"] != rc["voice"]:
        return f"音色改过（{data['voice']} → {rc['voice']}）但 TTS 未重合成"
    if rc.get("tts_rate") and data.get("rate") and data["rate"] != rc["tts_rate"]:
        return f"语速改过（{data['rate']} → {rc['tts_rate']}）但 TTS 未重合成"
    bad = []
    for i, it in tts.items():
        f = c.p.tts / f"{int(i):03d}.mp3"
        if not f.exists():
            bad.append(int(i))
        elif it.get("mp3_sig") and c.sig(f"tts/{int(i):03d}.mp3") != it["mp3_sig"]:
            bad.append(int(i))
    if bad:
        return f"{len(bad)} 句音频与 tts-result 指纹不符（如句 {bad[:3]}）"
    return ""


def check_stage(project: str | Path, stage: str) -> dict:
    """单阶段判定：基线优先，内容兜底，mtime 最后。"""
    c = _Ctx(project)
    miss = _check_presence(c, stage)
    if miss:
        return miss

    if stage == "tts":
        bad = _check_tts_content(c)
        if bad:
            return _row(stage, "stale", bad, "跑 r5_tts.py（--only 指定句号）", "content")

    st = c.state.get(stage)
    if st:
        cur = _inputs(c, stage)
        if st.get("digest") != _inputs_digest(cur):
            changed = [k for k in cur if st.get("inputs", {}).get(k) != cur[k]]
            return _row(stage, "stale",
                        "输入已变更：" + "、".join(changed[:4])
                        + (f" 等 {len(changed)} 项" if len(changed) > 4 else ""),
                        {"cut": "跑 cut.py --force",
                         "tts": "跑 r5_tts.py --only <句号>，再跑 elastic_fit",
                         "mix": "重跑 r6_mix.py", "mux": "重跑 r7_mux.py"}[stage],
                        "stamp")
        for rel, rec in (st.get("outputs") or {}).items():
            if not c.path(rel).exists() or c.sig(rel) != rec.get("sig"):
                return _row(stage, "stale", f"产物被替换或改动：{rel}",
                            "确认后重跑该阶段并重新盖章", "stamp")
        return _row(stage, "fresh", f"基线 {st.get('at', '?')}", "", "stamp")

    # 无基线：mtime 兜底；能过则“未验证”，过不了直接 stale
    problem, fix = _mtime_problem(c, stage)
    if problem:
        return _row(stage, "stale", problem, fix, "mtime")
    return _row(stage, "unknown", "没有指纹基线，无法证明是最新", "guard --bootstrap", "mtime")


def check(project: str | Path) -> list[dict]:
    """逐阶段判定（不抛异常）。"""
    out = []
    for st in STAGES:
        try:
            out.append(check_stage(project, st))
        except Exception as exc:
            out.append(_row(st, "stale", f"判定异常：{type(exc).__name__} {exc}",
                            "检查工程数据是否损坏"))
    return out


def blocking(project: str | Path, stage: str) -> list[str]:
    """渲染 ``stage`` 之前必须清零的问题（只看上游阶段）。"""
    rows = {r["stage"]: r for r in check(project)}
    need = list(UPSTREAM.get(stage, ())) or [stage]
    issues: list[str] = []
    for st in need:
        r = rows[st]
        if r["status"] != "fresh":
            tag = "无基线" if r["status"] == "unknown" else "不是最新"
            issues.append(f"{STAGE_CN[st]}{tag}：{r['detail']} → {r['fix']}")
    return issues


def render_ready(project: str | Path, stage: str) -> tuple[bool, list[str]]:
    issues = blocking(project, stage)
    return (not issues), issues


def stamp(project: str | Path, stage: str, *, note: str = "") -> dict:
    """记录「本阶段当前输入 → 产物」的基线。渲染成功后调用。"""
    c = _Ctx(project)
    inputs = _inputs(c, stage)
    entry = {
        "digest": _inputs_digest(inputs),
        "inputs": inputs,
        "outputs": _outputs(c, stage),
        "at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
    }
    data = load_json(c.root / STATE_FILE, default={}) or {}
    stages = data.get("stages") or {}
    stages[stage] = entry
    data.update({"stages": stages, "project": str(c.root),
                 "updated_at": entry["at"]})
    dump_json(c.root / STATE_FILE, data)
    return entry


def bootstrap(project: str | Path, *, force: bool = False) -> dict:
    """为已有工程建立基线。

    ``force=False`` 时只盖「mtime 粗判没问题」的阶段；``force=True`` 无条件盖章
    （用于已用其它手段证明产物可用，例如重跑 md5 一致）。
    """
    c = _Ctx(project)
    done, skipped = [], []
    for st in STAGES:
        if not force:
            if _mtime_problem(c, st)[0] or _check_presence(c, st):
                skipped.append(st)
                continue
            if st == "tts" and _check_tts_content(c):
                skipped.append(st)
                continue
        try:
            stamp(c.root, st, note="bootstrap" + ("-force" if force else ""))
            done.append(st)
        except Exception:
            skipped.append(st)
    return {"stamped": done, "skipped": skipped}


def summary(project: str | Path) -> dict:
    rows = check(project)
    return {"rows": rows,
            "mix_ready": not blocking(project, "mix"),
            "mux_ready": not blocking(project, "mux")}
