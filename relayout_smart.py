# -*- coding: utf-8 -*-
"""cut.py 之后重建 narration.json（r6 混音读这份）—— 句级精确版。

旧 relayout_narr 按 beat 跨度等比铺句，句子音频只是"大致"落在自己的画面上。
这里用 align.json 的 sent_win_idx（smart_align 记录的每句→窗口序号区间），
把每句音频的 t0 精确压在该句**第一刀**的成片起点上 —— 音画由构造保证逐句同源。

必须在 cut.py --force 之后跑（cut 会重新吸附帧网格并累加 new_start）。
没有 sent_win_idx 时退回 beat 等比铺法（兼容 n3 旧对齐）。

用法：python relayout_smart.py --project <pj>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    a = ap.parse_args()
    d = Path(a.project).expanduser().resolve().parent / "json"

    al = json.load(open(d / "align.json", encoding="utf-8"))
    wins = json.load(open(d / "cutplan.json", encoding="utf-8"))["windows"]
    tts = {x["i"]: x for x in json.load(open(d / "tts-result.json", encoding="utf-8"))["items"]}
    narr = json.load(open(d / "narration.json", encoding="utf-8"))
    meta = {it["i"]: it for it in narr["items"]}

    sent_idx = al.get("sent_win_idx") or {}
    sent_beat = {int(k): v for k, v in al["sent_beat"].items()}
    by_beat = {}
    for i, b in sorted(sent_beat.items()):
        by_beat.setdefault(b, []).append(i)

    items, exact = [], 0
    for b in sorted(by_beat):
        sents = sorted(by_beat[b])
        for i in sents:
            m = meta.get(i, {"text": "", "beat": b, "chars": 0})
            real = float((tts.get(i) or {}).get("trim_dur")
                         or (tts.get(i) or {}).get("tts_dur") or 0)
            rng = sent_idx.get(str(i))
            if rng:                          # 句级：第一刀起点
                t0 = float(wins[rng[0]]["new_start"])
                exact += 1
            else:                            # 回退：块内等比
                bw = al["beat_win"].get(str(b))
                if not bw:
                    continue
                seg = [w for w in wins[bw[0]:bw[1]]]
                if not seg:
                    continue
                bs, be = seg[0]["new_start"], seg[-1]["new_end"]
                reals = [float((tts.get(x) or {}).get("trim_dur")
                               or (tts.get(x) or {}).get("tts_dur") or 0) for x in sents]
                before = sum(reals[:sents.index(i)])
                span = be - bs
                gap_k = max(0.0, (span - sum(reals)) / max(1, len(sents)))
                t0 = bs + before + gap_k * sents.index(i)
            items.append({"i": i, "text": m["text"], "beat": b,
                          "t0": round(t0, 3), "dur": round(real, 3),
                          "chars": m.get("chars", len(m["text"]))})

    items.sort(key=lambda x: x["t0"])
    # 音频互相重叠时（刀容量 < 语音），后一句顺延，最多吃 0.6s 的下一句刀头
    for x in range(1, len(items)):
        prev_end = items[x - 1]["t0"] + items[x - 1]["dur"]
        if items[x]["t0"] < prev_end:
            items[x]["t0"] = round(prev_end, 3)
    narr["items"] = items
    json.dump(narr, open(d / "narration.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    tot = sum(i["dur"] for i in items)
    out = wins[-1]["new_end"]
    print(f"narration 重建 {len(items)} 句 / 语音 {tot:.1f}s / 成片 {out:.1f}s / "
          f"覆盖 {100 * tot / max(1e-6, out):.1f}%")
    print(f"  句级精确 {exact}/{len(items)} · 首句 t0 {items[0]['t0']:.2f}s · "
          f"末句尾 {items[-1]['t0'] + items[-1]['dur']:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
