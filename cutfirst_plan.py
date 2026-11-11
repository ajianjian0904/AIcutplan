# -*- coding: utf-8 -*-
"""Cut-first 阶段一：画面先定（cut-first / narrate-second）。

旧路径是"旁白定稿 → TTS → 用语音时长裁画面"，语义匹配从起点就是被动妥协。
本脚本翻转时序：**只依据剧情理解先定输出时间轴**——

  1. 每个剧情块的成片预算（block.target）按事件叙事权重（因果图谱 func + 类型）
     分配到事件；关键转折多给时间，过渡性内容压缩；
  2. 每个事件从其源区间内按时间序领镜头（整镜 0.8–3.5s，全片一镜一次），
     装满分配秒数；不足向相邻事件借；
  3. 产出 cutplan.json（与 cut.py 完全兼容的 schema，窗口已带输出轴 new_start）
     → cut.py 渲染无声粗剪。

旁白（narrate_cut.py）在此之后对着这个已定型的时间轴写，同步靠生成顺序保证。

用法：python cutfirst_plan.py --project <pj>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from smart_align import shots_of, _load  # noqa: E402

LEAD = 0.10
MIN_SHOT, MAX_SHOT = 0.8, 3.5

# 叙事权重：因果图谱 func 优先，缺省用事件 type
FUNC_W = {"钩子": 3.0, "爆发": 3.0, "反转": 2.6, "升级": 1.8, "收束": 1.3, "铺垫": 1.0}
TYPE_W = {"钩子": 3.0, "反转": 2.6, "爽点": 2.6, "冲突": 1.8, "收尾": 1.3, "铺垫": 1.0}


def _weight(e: dict, causal: dict, gi: int) -> float:
    c = causal.get(str(gi)) or {}
    w = FUNC_W.get(c.get("func", ""), None)
    if w is None:
        w = TYPE_W.get(e.get("type", ""), 1.0)
    # 有因果连接的事件更可能是结构节点
    if c.get("cause") or c.get("result"):
        w *= 1.15
    return w


def _reason(e: dict, causal: dict, gi: int) -> str:
    """窗口保留理由（clip_plan 契约：旁白写作与人工抽查的依据）。"""
    c = causal.get(str(gi)) or {}
    func = c.get("func") or e.get("type", "")
    ev = e.get("event", "")[:30]
    link = ""
    if c.get("cause"):
        link = f"（承接事件{c['cause'][0]}）"
    return f"{func}节点{link}：{ev}"


def align_plan(proj_dir: Path) -> dict:
    d = proj_dir / "json"
    sp = _load(d / "story_plan.json")
    blocks = [b for b in (sp.get("blocks") or []) if b["type"] != "收录" and b["target"] > 0]
    events = sp.get("events") or []
    causal = sp.get("causal") or {}
    shots = shots_of(proj_dir)
    bounds_last = shots[-1]["b"]
    head_guard = shots[0]["b"]
    shot_by_id = {s["i"]: s for s in shots}
    used: set = set()

    new_wins, meta = [], []          # meta: 每窗的事件号 + 块号
    cursor = 0.0

    def emit(src_a: float, dur: float, gi: int, bi: int) -> None:
        nonlocal cursor
        e = events[gi] if gi < len(events) else {}
        c = causal.get(str(gi)) or {}
        # 声音策略：钩子/爆发/反转的高潮段保留原声（mixed），其余旁白覆盖
        func = c.get("func") or e.get("type", "")
        ost_mode = "mixed" if func in ("爆发", "钩子") else "narration"
        new_wins.append({"src_start": round(src_a, 3), "src_end": round(src_a + dur, 3),
                         "tier": "line", "label": blocks[bi]["label"] if bi < len(blocks) else "",
                         "lines": [], "dur": round(dur, 3),
                         "new_start": round(cursor, 3), "new_end": round(cursor + dur, 3),
                         "sent_ids": [], "ost_mode": ost_mode,
                         "reason": _reason(e, causal, gi)})
        meta.append({"ev": gi, "beat": bi, "ost_mode": ost_mode})
        cursor += dur

    beat_span = {}
    for bi, bl in enumerate(blocks):
        beat_t0 = cursor
        ev_in = [gi for gi, e in enumerate(events)
                 if float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
        if not ev_in:
            continue
        # 预算按权重分配；单事件最少 1.6s，超出预算池的截断
        weights = {gi: _weight(events[gi], causal, gi) for gi in ev_in}
        wsum = sum(weights.values()) or 1.0
        alloc = {gi: max(1.6, bl["target"] * weights[gi] / wsum) for gi in ev_in}
        # 总量超预算则等比收回
        over = sum(alloc.values()) - bl["target"]
        if over > 0:
            scal = max(0.4, 1 - over / sum(alloc.values()))
            alloc = {gi: max(1.2, v * scal) for gi, v in alloc.items()}

        for gi in ev_in:
            e = events[gi]
            e0, e1 = float(e["t0"]), float(e["t1"])
            got = 0.0
            pool = [sh for sh in shots if e0 <= sh["a"] < e1
                    and sh["i"] not in used
                    and not ((sh["a"] + sh["b"]) / 2 < head_guard)]
            # 跳过过碎镜头（<0.45s），按时间序整镜领取
            for sh in sorted(pool, key=lambda z: z["a"]):
                if got >= alloc[gi] - 0.2:
                    break
                if float(sh["b"]) - float(sh["a"]) < 0.45:
                    continue
                dur = min(MAX_SHOT, max(MIN_SHOT, float(sh["b"]) - float(sh["a"])),
                          alloc[gi] - got + 0.3, bounds_last - sh["a"] - LEAD - 0.02)
                if dur < 0.45:
                    continue
                used.add(sh["i"])
                emit(sh["a"] + LEAD, dur, gi, bi)
                got += dur
            # 事件内镜头不够（长事件镜头稀）→ 向相邻事件借
            if got < alloc[gi] - 0.8:
                for dj in (-1, 1, -2, 2):
                    gj = gi + dj
                    if not (0 <= gj < len(events)):
                        continue
                    ee = events[gj]
                    pool2 = [sh for sh in shots
                             if float(ee["t0"]) <= sh["a"] < float(ee["t1"])
                             and sh["i"] not in used
                             and not ((sh["a"] + sh["b"]) / 2 < head_guard)
                             and float(sh["b"]) - float(sh["a"]) >= 0.45]
                    for sh in sorted(pool2, key=lambda z: z["a"]):
                        if got >= alloc[gi] - 0.2:
                            break
                        dur = min(MAX_SHOT, max(MIN_SHOT, float(sh["b"]) - float(sh["a"])),
                                  alloc[gi] - got + 0.3)
                        used.add(sh["i"])
                        emit(sh["a"] + LEAD, dur, gi, bi)
                        got += dur
                    if got >= alloc[gi] - 0.2:
                        break
        beat_span[bi] = (round(beat_t0, 3), round(cursor, 3))

    total = round(cursor, 2)
    plan_out = {"windows": new_wins,
                "stats": {"src_dur": bounds_last, "out_dur": total, "out_dur_actual": total,
                          "segments": len(new_wins),
                          "cuts_per_min": round(len(new_wins) / max(total / 60, 0.01), 1),
                          "mode": "cut-first-rough"}}
    (d / "cutplan.json").write_text(json.dumps(plan_out, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    (d / "cutplan_meta.json").write_text(json.dumps(
        {"meta": meta, "beat_span": {str(k): list(v) for k, v in beat_span.items()}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return {"cuts": len(new_wins), "duration": total, "beats": len(blocks)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    a = ap.parse_args()
    proj_dir = Path(a.project).expanduser().resolve().parent
    r = align_plan(proj_dir)
    print(f"[画面先定] {r['cuts']} 刀 / {r['duration']}s / {r['beats']} 块 → cutplan.json + cutplan_meta.json")
    print("下一步：cut.py 渲染无声粗剪 → narrate_cut.py 对轴写旁白")
    return 0


if __name__ == "__main__":
    sys.exit(main())
