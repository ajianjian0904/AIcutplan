# -*- coding: utf-8 -*-
"""L0·字幕领域纯函数：探测 / 提取 / 清洗 / 语言判定。

所属层：L0 基础层（领域纯函数） ｜ 副作用：无（除 read_text / ffprobe）
调用者：subs_extract.py（CLI 与写回）、asr_local.py、cf_pipeline.py 的台词源预检

为什么优先用内嵌字幕而不是 ASR：ASR 是「猜」出来的 —— small 模型出现过
「ついほ?」「エンバーターチュー」这类幻听，且段时间戳整体漂移 3–16s；
内嵌字幕是确定性文本，常带「（バリオス）」说话人标注，专有名词/数字/角色名
都不会错（本项目「十三万点 → 一万三千点」的读图事故就是反面例子）。

实现从 ``subs_extract.py`` 原样抽出，行为逐字等价，便于脱离 ffmpeg 单测。
"""
from __future__ import annotations

import re
from pathlib import Path

from .media import probe_streams

TEXT_CODECS = frozenset({
    "subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "subviewer"})
IMAGE_CODECS = frozenset({
    "hdmv_pgs_subtitle", "dvb_subtitle", "dvd_subtitle", "xsub", "dvb_teletext"})
LANG_TAG = {
    "jpn": "ja", "jp": "ja", "ja": "ja",
    "eng": "en", "en": "en",
    "zho": "zh", "chi": "zh", "zh": "zh",
    "zh-hans": "zh", "zh-hant": "zh", "chs": "zh", "cht": "zh",
    "kor": "ko", "ko": "ko",
}
KANA = re.compile(r"[\u3040-\u30ff]")
HAN = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")
TAG = re.compile(r"\{[^}]*\}|<[^>]+>|&\w+;")
SPEAKER = re.compile(r"^[（(]([^）)]{1,12})[）)]\s*")
SRT_TIME = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def probe_subs(video: str | Path) -> list[dict]:
    """列出源片里的字幕流（含图像字幕，由调用方决定是否过滤）。"""
    out = []
    for s in probe_streams(video):
        if s.get("codec_type") != "subtitle":
            continue
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        lang = str(tags.get("language", "")).lower()
        out.append({
            "index": int(s["index"]),
            "codec": s.get("codec_name", ""),
            "language": LANG_TAG.get(lang, lang),
            "title": str(tags.get("title", "")),
            "forced": bool(disp.get("forced")),
            "default": bool(disp.get("default")),
        })
    return out


def clean_text(raw: str) -> tuple[str, str]:
    """去 ASS/HTML 样式标签、合并换行；把「（角色名）」拆成 speaker。

    返回 ``(台词正文, 说话人)``。注意说话人标注**保留在正文里**（历史产物
    就是这样，下游 zh_subtitles 与 dlg 都依赖它），只是额外给一个字段。
    """
    t = TAG.sub("", raw).replace("\\N", " ").replace("\\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    speaker = ""
    m = SPEAKER.match(t)
    if m:
        speaker = m.group(1).strip()
    return t, speaker


def parse_srt(path: str | Path) -> list[dict]:
    """解析 SRT（支持 BOM / 编号缺失 / 时间行不在第 2 行 / 逗号或点做毫秒分隔）。"""
    from .io import read_text
    txt = read_text(path).strip()
    rows = []
    for block in re.split(r"\n\s*\n", txt):
        ls = [x for x in block.splitlines() if x.strip()]
        if len(ls) < 2:
            continue
        ti = next((i for i, x in enumerate(ls) if SRT_TIME.search(x)), None)
        if ti is None:
            continue
        m = SRT_TIME.search(ls[ti])
        g = list(map(int, m.groups()))
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        z = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text, speaker = clean_text(" ".join(ls[ti + 1:]))
        if not text or z <= a:
            continue
        rows.append({"start": round(a, 3), "end": round(z, 3),
                     "text": text, "speaker": speaker})
    rows.sort(key=lambda x: x["start"])
    return rows


def merge_same_time(rows: list[dict]) -> list[dict]:
    """合并同一 start/end 的多行（ASS 常按屏幕位置把一句拆成两行）。

    不合并会让 ``dlg`` 出现「ああ うちのギルドで」「いらねえやつをな。」
    两条半句。实测 ASS 516 行合并后 = SRT 352 条，可互相印证。
    """
    out: list[dict] = []
    for r in rows:
        if out and abs(out[-1]["start"] - r["start"]) < 0.06 \
                and abs(out[-1]["end"] - r["end"]) < 0.06:
            out[-1]["text"] = (out[-1]["text"] + " " + r["text"]).strip()
            out[-1]["end"] = max(out[-1]["end"], r["end"])
            if r["speaker"] and not out[-1]["speaker"]:
                out[-1]["speaker"] = r["speaker"]
        else:
            out.append(dict(r))
    return out


def sniff_lang(rows: list[dict]) -> str:
    """按前 80 条台词猜语言：假名→ja / 纯拉丁→en / 汉字→zh / 否则 unknown。"""
    s = "".join(str(r.get("text", "")) for r in rows[:80])
    if KANA.search(s):
        return "ja"
    if LATIN.search(s) and not HAN.search(s):
        return "en"
    if HAN.search(s):
        return "zh"
    return "unknown"


def norm(s: str) -> str:
    """归一化用于译文迁移时的原文相似度比较（去标点、转小写）。"""
    return re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]", "", str(s)).lower()


def is_text_stream(s: dict) -> bool:
    """是否可提取为文本的字幕流（排除蓝光 PGS 等图像字幕）。"""
    return (s.get("codec") in TEXT_CODECS
            and s.get("codec") not in IMAGE_CODECS)
