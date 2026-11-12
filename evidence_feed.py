# -*- coding: utf-8 -*-
# [L1 新链] 职责：镜头证据回喂上游（零读图） ｜ 调用者：story_parse; causal_graph ｜ 产物：解析/因果 prompt 文本
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""上游证据回喂（P0-2）：把会话内读图产出的镜头证据包聚合给剧情解析 / 因果图谱。

背景：cut-first 下游已有高密度证据（387 镜 desc/depth/facts/dlg + 592 个 2s 窗），
但上游 story_parse 仍是「200s 窗口 + 12 张均匀帧」，causal_graph 只看一行事件概要，
形成「高密度证据在下游、低密度证据在上游」的倒挂。本模块把已有证据按时间窗聚合成
紧凑文本，零新增抽帧、零新增读图、零模型调用。

被 story_parse.parse_segment() 与 causal_graph 共同调用；也可单独导出供会话内阅读：
  python evidence_feed.py --project <pj> --check
  python evidence_feed.py --project <pj> --export
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# L0 基础层：证据打分 / 格式化 / 压缩的唯一实现，本模块只保留时间窗编排。
from aicutplan import evidence as EV          # noqa: E402
from aicutplan.io import load_json            # noqa: E402

load_shots = EV.load_shots                    # 兼容别名
_score = EV.score_shot
_line = EV.format_shot_line
_compact = EV.compact


def segment_brief(proj_dir, a: float, b: float,
                  max_chars: int = 2600, max_rows: int = 42) -> tuple[str, dict]:
    """一个剧情解析段（约 200s）的镜头证据摘要。"""
    rows = [r for r in load_shots(proj_dir) if a <= float(r.get("a", 0)) < b]
    if not rows:
        return "", {"shots": 0, "kept": 0, "chars": 0}
    txt, kept = _compact(rows, max_rows, max_chars)
    return txt, {"shots": len(rows), "kept": kept, "chars": len(txt)}


def event_brief(proj_dir, events: list[dict],
                max_chars: int = 6200, per_event: int = 2) -> tuple[str, dict]:
    """按事件时间窗聚合证据，给因果图谱判断「铺垫/升级/爆发/反转」用。

    每个事件最多取 per_event 个代表镜头（优先有台词/情绪的），控制总长度避免 prompt 膨胀。
    """
    shots = load_shots(proj_dir)
    if not shots:
        return "", {"events": len(events), "lines": 0, "chars": 0}
    lines, total = [], 0
    for i, e in enumerate(events):
        if (e.get("type") or "") == "收录":
            continue
        try:
            a, b = float(e.get("t0", 0)), float(e.get("t1", 0))
        except (TypeError, ValueError):
            continue
        pool = [r for r in shots if a <= float(r.get("a", 0)) < b]
        if not pool:
            continue
        pick = sorted(pool, key=lambda r: -_score(r))[:per_event]
        pick.sort(key=lambda r: float(r.get("a", 0)))
        parts = []
        for r in pick:
            t = str(r.get("desc", ""))[:22]
            dl = str(r.get("dlg", ""))[:18]
            parts.append(t + (f"(台词:{dl})" if dl else ""))
        ln = f"[{i}] {a:.0f}-{b:.0f}s {' / '.join(parts)}"
        if total + len(ln) + 1 > max_chars:
            break
        lines.append(ln)
        total += len(ln) + 1
    return "\n".join(lines), {"events": len(events), "lines": len(lines), "chars": total}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--check", action="store_true", help="打印各段证据统计")
    ap.add_argument("--export", action="store_true", help="导出 json/evidence_brief.md 供会话内阅读")
    a = ap.parse_args()
    proj_dir = Path(a.project).expanduser().resolve().parent
    plan_p = proj_dir / "json" / "story_plan.json"
    plan = load_json(plan_p, default={}) or {}
    seg_sec = float(plan.get("segment") or 200.0)
    src = 0.0
    for key in ("src_dur", "source_dur"):
        if plan.get(key):
            src = float(plan[key])
            break
    if src <= 0:
        pj = proj_dir / "project.json"
        if pj.exists():
            cfg = load_json(pj, default={}) or {}
            src = float(cfg.get("recap", {}).get("src_dur") or cfg.get("src_dur") or 0)
    shots = load_shots(proj_dir)
    last_shot = max((float(r["b"]) for r in shots), default=0.0)
    if src <= 0:
        src = last_shot
    elif last_shot > 0:
        # 配置时长常含片尾收录段（无有效镜头），按证据实际覆盖范围截断
        src = min(src, last_shot)

    out = ["# 上游证据回喂摘要（由 shots_desc 聚合，零新增读图）", ""]
    n = max(1, int(src // seg_sec) + (1 if src % seg_sec else 0))
    for i in range(n):
        a0, b0 = i * seg_sec, min((i + 1) * seg_sec, src)
        txt, st = segment_brief(proj_dir, a0, b0)
        print(f"段{i:>2} {a0:>6.0f}-{b0:>6.0f}s  镜头 {st['shots']:>3} → 保留 {st['kept']:>2} / {st['chars']:>4} 字")
        out += [f"## 段{i} {a0:.0f}-{b0:.0f}s（{st['shots']} 镜，取 {st['kept']}）", "", txt or "（无镜头证据）", ""]
    ev = plan.get("events") or []
    etxt, est = event_brief(proj_dir, ev)
    print(f"事件证据: {est['lines']}/{est['events']} 条 · {est['chars']} 字")
    out += ["## 事件级证据（因果图谱用）", "", etxt or "（无镜头证据）", ""]
    if a.export:
        p = proj_dir / "json" / "evidence_brief.md"
        p.write_text("\n".join(out), encoding="utf-8")
        print(f"→ {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
