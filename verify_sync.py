# -*- coding: utf-8 -*-
"""验证句-画对应：每句的刀源中点是否落在其配对事件的区间内。

用法：python verify_sync.py <project.json>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import story_parse as SP  # noqa: E402


def main() -> int:
    pj = Path(sys.argv[1]).expanduser().resolve()
    d = pj.parent / "json"
    al = json.load(open(d / "align.json", encoding="utf-8"))
    sp = SP.load_plan(pj.parent)
    events = sp.get("events") or []
    n = json.load(open(d / "narration.json", encoding="utf-8"))["items"]
    sent_beat = {int(k): v for k, v in al["sent_beat"].items()}
    blocks = [b for b in (sp.get("blocks") or []) if b["type"] != "收录"]

    total = hit = 0
    bad = []
    for it in n:
        i = it["i"]
        cuts = [w for w in al["windows"] if i in (w.get("sent_ids") or [])]
        if not cuts:
            continue
        b = sent_beat[i]
        bl = blocks[b] if b < len(blocks) else None
        ev_in = [e for e in events
                 if bl and float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
        for w in cuts:
            mid = (w["src_start"] + w["src_end"]) / 2
            total += 1
            e = next((e for e in ev_in if float(e["t0"]) <= mid < float(e["t1"])), None)
            if e:
                hit += 1
            elif len(bad) < 12:
                bad.append((i, it["text"][:18], round(mid)))
    pct = 100 * hit / max(1, total)
    print(f"刀-事件命中率 {hit}/{total} = {pct:.1f}%   （命中=刀落在该块任一事件内）")
    for x in bad:
        print("  未命中示例:", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
