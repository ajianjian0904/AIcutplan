# [L1 会话工具] 职责：镜头描述推导 2s 窗（零读图） ｜ 调用者：subs_extract --refresh-derived ｜ 产物：json/scene_desc.json
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""从镜头描述（会话内视觉产出）推导每 2s 场景描述：窗口继承所在镜头的 desc/depth + 该窗台词。

用法：python scene_from_shots.py --project P [--win 2.0]
产物：json/scene_desc.json（{t,t1,desc,cam,dlg,shot_i,depth}）
"""
import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--win", type=float, default=2.0, help="窗口步长秒")
    a = ap.parse_args()
    proj = Path(a.project).expanduser().resolve().parent
    d = proj / "json"

    sd = json.load(open(d / "shots_desc.json", encoding="utf-8"))["rows"]
    # 台词（日语段）
    dlg_rows = []
    jp = d / "ja-segments.json"
    if jp.exists():
        ja = json.load(open(jp, encoding="utf-8"))
        segs = ja.get("segments") if isinstance(ja, dict) else ja
        for s in segs:
            t0 = float(s.get("start", s.get("t0", 0)))
            t1 = float(s.get("end", s.get("t1", t0 + 2)))
            txt = s.get("text", "")
            if txt:
                dlg_rows.append((t0, t1, txt))
    dlg_rows.sort()

    rows = []
    t = 0.0
    total = max(s["b"] for s in sd) if sd else 0
    while t < total:
        t1 = min(t + a.win, total)
        # 覆盖该窗的镜头（取重叠最长者）
        best, best_ov = None, 0.0
        for s in sd:
            ov = min(s["b"], t1) - max(s["a"], t)
            if ov > best_ov:
                best, best_ov = s, ov
        if best is None:
            t = t1
            continue
        # 台词：与窗口重叠最长的日语段
        dlg, dlg_ov = "", 0.0
        for t0, te, txt in dlg_rows:
            ov = min(te, t1) - max(t0, t)
            if ov > dlg_ov:
                dlg, dlg_ov = txt, ov
        rows.append({
            "t": round(t, 2), "t1": round(t1, 2),
            "desc": best["desc"],
            "cam": f"镜{best['i']}",
            "dlg": dlg,
            "shot_i": best["i"],
            "depth": best.get("depth", ""),
        })
        t = t1
    out = {"rows": rows,
           "updated": __import__("datetime").datetime.now().isoformat(),
           "source": "derived-from-session-shots"}
    (d / "scene_desc.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    print(f"scene_desc: {len(rows)} 窗（步长 {a.win}s）← 镜头描述推导，零读图")
    return 0


if __name__ == "__main__":
    sys.exit(main())
