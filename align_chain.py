# -*- coding: utf-8 -*-
# [L1 旧链] 职责：旧对齐前置（cut-first 已停用） ｜ 调用者：pipeline ｜ 产物：cutplan.json
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""对齐前置：先重建初版 cutplan（回到与 script.anchor_new 一致的轴），再跑 n3 对齐。

为什么必须重建：
  n3_align 把 cutplan 当「旧轴」用 —— 它拿 narration 每句的 anchor（成片秒，来自
  script.anchor_new）去 cutplan 的 new_start→src 映射里查该块的源区间。
  但 n3 跑完会把 cutplan 覆写成**新轴**（刀数由语音实测时长决定）。
  于是第二轮 TTS 改稿后再对齐时，cutplan 已经是新轴，anchor 还是老轴 → 映射错位，
  块源区间取错，刀数算少，多出来的旁白句子在 relayout 时被丢掉。

  实测：同一集第二次对齐，成片从 241s 掉到 208s，旁白 127 句只剩 103 句。

所以对齐的正确顺序是：
  script.json（anchor_new 按 target 累加）
    → 重建初版 cutplan（块按 target 铺刀，new_start 累加 = anchor_new）
    → n3_align
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pipeline as PL  # noqa: E402
import story_parse as SP  # noqa: E402


def axis_drift(proj_dir: Path) -> float:
    """script 的块起点 vs cutplan 里每块第一刀的 new_start，最大偏差（秒）。

    两边都按块顺序走一遍（label 可能重名，所以用顺序游标而不是字典）。
    """
    try:
        beats = json.loads((proj_dir / "json" / "script.json").read_text(encoding="utf-8"))["beats"]
        wins = json.loads((proj_dir / "json" / "cutplan.json").read_text(encoding="utf-8"))["windows"]
    except Exception:
        return -1.0
    starts, seen = [], None
    for w in wins:
        if w.get("label") != seen:
            seen = w.get("label")
            starts.append(float(w["new_start"]))
    drift, acc = 0.0, 0.0
    for i, b in enumerate(beats):
        if i < len(starts):
            drift = max(drift, abs(starts[i] - acc))
        acc += float(b.get("target") or 0)
    return round(drift, 2)


def main() -> int:
    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--classic", action="store_true",
                    help="退回 n3_align 的块级均匀铺刀（旧行为）")
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent

    if not a.classic:
        import smart_align as SA
        r = SA.align(proj_dir)
        print(f"[句级对齐] {r['cuts']} 刀 / {r['duration']}s / {r['beats']} 块", flush=True)
        return 0

    plan = SP.load_plan(proj_dir)
    blocks = plan.get("blocks") or []
    d0 = axis_drift(proj_dir)
    print(f"[对齐] 当前轴偏差 {d0}s", flush=True)
    if blocks:
        r = SP.write_cutplan(proj_dir, blocks)
        print(f"[对齐] 重建初版 cutplan：{r['cuts']} 刀 / {r['duration']}s", flush=True)
    else:
        print("[对齐] 没有 story_plan 块表，直接用现有 cutplan", flush=True)

    n3 = PL.ensure_script(proj_dir, "n3_align.py")
    if not n3:
        print("[对齐] 找不到 n3_align.py", flush=True)
        return 2
    argv = [sys.executable, str(n3), "--project", str(pj)]
    print("$ " + " ".join(argv), flush=True)
    return subprocess.run(argv, cwd=str(ROOT)).returncode


if __name__ == "__main__":
    sys.exit(main())
