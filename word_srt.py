# -*- coding: utf-8 -*-
"""词级时间戳字幕（Phase D）：edge-tts WordBoundary → 词级精确 srt。

r5_tts 用 save() 拿不到词边界，本脚本用 Communicate.stream() 重放逐句合成，
收集 WordBoundary 的偏移与时长，按 3-5 词/条切字幕块，时间直接落在输出轴
（句 t0 + 词偏移 − 首词前静音），比整句 srt 精确一个数量级。

产物：json/word_times.json + out/narration-word.srt（r7 --burn 可直接烧）。

用法：python word_srt.py --project <pj> [--words-per-cue 4]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


async def synth_words(text: str, voice: str, rate: str, pitch: str) -> list[dict]:
    """单句合成，返回词边界 [{offset_ns, dur_ns, text}]。"""
    import edge_tts
    words = []
    # edge-tts ≥7 默认 SentenceBoundary，词级必须显式指定
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch,
                                       boundary="WordBoundary")
    async for chunk in communicate.stream():
        if chunk["type"] == "WordBoundary":
            words.append({"offset_ns": chunk["offset"], "dur_ns": chunk["duration"],
                          "text": chunk["text"]})
    return words


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--words-per-cue", type=int, default=4)
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent
    sys.path.insert(0, str(proj_dir))
    import common
    ctx = common.load(a.project)

    rc = ctx.cfg.get("recap") or {}
    voice = rc.get("voice", "zh-CN-YunxiNeural")
    rate = rc.get("tts_rate", "+35%")
    pitch = rc.get("pitch", "+0Hz")

    narr = json.loads(ctx.p("json", "narration.json").read_text(encoding="utf-8"))["items"]
    all_rows, cues = [], []

    for it in narr:
        # 词边界来自重新合成（缓存 mp3 无法回放边界）
        try:
            words = asyncio.run(synth_words(it["text"], voice, rate, pitch))
        except Exception as e:
            print(f"  [{it['i']:03d}] 边界获取失败：{type(e).__name__}")
            continue
        # edge-tts 偏移含句首静音：用首个词的偏移做零点，对齐句起点 t0
        if not words:
            continue
        base = words[0]["offset_ns"]
        words = [{**w, "sec": (w["offset_ns"] - base) / 1e9,
                  "dur": w["dur_ns"] / 1e9} for w in words]
        for w in words:
            all_rows.append({"i": it["i"], "w": w["text"], "t": round(it["t0"] + w["sec"], 3),
                             "dur": round(w["dur"], 3)})
        # 按 N 词切块成字幕 cue
        for k in range(0, len(words), a.words_per_cue):
            grp = words[k:k + a.words_per_cue]
            t0 = it["t0"] + grp[0]["sec"]
            t1 = it["t0"] + grp[-1]["sec"] + grp[-1]["dur"]
            cues.append((t0, t1, "".join(w["text"] for w in grp)))

    def ts(sec: float) -> str:
        sec = max(0.0, sec)
        h, m, s = int(sec // 3600), int(sec % 3600 // 60), sec % 60
        ms = int(round((s - int(s)) * 1000))
        if ms == 1000:
            s, ms = int(s) + 1, 0
        else:
            s = int(s)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    srt = []
    for k, (t0, t1, text) in enumerate(cues):
        srt.append(f"{k + 1}\n{ts(t0)} --> {ts(max(t1, t0 + 0.3))}\n{text}\n")
    out_srt = ctx.p("out", "narration-word.srt")
    out_srt.write_text("\n".join(srt), encoding="utf-8")
    ctx.p("json", "word_times.json").write_text(json.dumps(all_rows, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
    print(f"[词级字幕] {len(narr)} 句 / {len(all_rows)} 词 / {len(cues)} 条 cue → {out_srt.name}")
    print("  烧录：ffmpeg -i 成片 -vf ass/srt 滤镜，或把 r7 --burn 的字幕源换成 narration-word.srt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
