# -*- coding: utf-8 -*-
# [L1 新链] 职责：画面先定（连续爽点钩子 + 权重选窗） ｜ 调用者：cf_pipeline:2 ｜ 产物：json/cutplan.json
# 分层与依赖全表见 docs/ARCHITECTURE.md
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
HOOK_SHOT_MIN, HOOK_SHOT_MAX = 0.6, 1.2   # 连续段内部按此长度切窗口（播放时无缝）
HOOK_SHOTS_N = 10                         # 钩子块预算的窗口数上限
HOOK_USED_RATIO = 1.0                     # 钩子块预算全部用于前置爽点
# 钩子 = 剪辑规划阶段锁定的「源片连续、未经裁切重排」的一段爽点。
# 不是全球散装检索镜头、不按设定/动作重排、不跳源片时间线；
# 这段画面是「提前复用」，正片里必须原样再演一遍，所以钩子镜头不进 used。
CLIFFHANGER_SEC = 12.0                    # 末块结尾原声留白时长

# ---- 规划领域纯函数统一走 L0 基础层（aicutplan.planning）------------------
# 这里只保留编排逻辑；打分/选段/权重/理由的唯一实现在包内，便于脱离工程单测。
# 硬约束（钩子必须是源片连续未裁切爽点）写在包的模块 docstring 里。
from aicutplan import planning as PL       # noqa: E402

_hook_score = PL.hook_score
_pick_hook_clip = PL.pick_hook_clip
_weight = PL.weight
_reason = PL.reason
is_continuous = PL.is_continuous          # 供 --check / 验收钩子段是否真的连续


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

    def emit(src_a: float, dur: float, gi: int, bi: int,
             ost_mode: str = "narration", tier: str = "line") -> None:
        nonlocal cursor
        e = events[gi] if gi < len(events) else {}
        new_wins.append({"src_start": round(src_a, 3), "src_end": round(src_a + dur, 3),
                         "tier": tier, "label": blocks[bi]["label"] if bi < len(blocks) else "",
                         "lines": [], "dur": round(dur, 3),
                         "new_start": round(cursor, 3), "new_end": round(cursor + dur, 3),
                         "sent_ids": [], "ost_mode": ost_mode,
                         "reason": _reason(e, causal, gi)})
        meta.append({"ev": gi, "beat": bi, "ost_mode": ost_mode})
        cursor += dur

    def _mark_raw(bi: int, w_lo: int, w_hi: int) -> None:
        """把 [w_lo, w_hi] 窗口标为原声留白（r6 只认 tier=hold_hl 做 0dB 门控）。"""
        for wi in range(w_lo, min(w_hi + 1, len(new_wins))):
            if meta[wi]["beat"] != bi:
                break
            new_wins[wi]["tier"] = "hold_hl"
            new_wins[wi]["ost_mode"] = "original"
            meta[wi]["ost_mode"] = "original"

    def mark_climax_raw(bi: int, beat_w0: int) -> None:
        """博主结构：块内第一个 爆发 事件起 → 原声留白到收束前（或块尾），上限 15s。
        反转不触发（开除块的反转需要解说）。"""
        beat_ev: dict[int, int] = {}          # 事件号 -> 该事件首个窗口号
        last_wi = beat_w0
        for wi in range(beat_w0, len(new_wins)):
            if meta[wi]["beat"] != bi:
                break
            beat_ev.setdefault(meta[wi]["ev"], wi)
            last_wi = wi
        if not beat_ev:
            return
        def func(gi):
            return (causal.get(str(gi)) or {}).get("func") or events[gi].get("type", "")
        lo = None
        for gi, wi in sorted(beat_ev.items()):
            if func(gi) == "爆发":
                lo = wi
                break
        if lo is None:
            return
        hi = last_wi                          # 默认留白到块尾
        for gi, wi in sorted(beat_ev.items()):
            if wi > lo and func(gi) == "收束":
                hi = wi - 1
                break
        # 上限 15s：从尾部收
        while hi > lo and sum(new_wins[k]["dur"] for k in range(lo, hi + 1)) > 15.0:
            hi -= 1
        if hi > lo and sum(new_wins[k]["dur"] for k in range(lo, hi + 1)) >= 4.0:
            _mark_raw(bi, lo, hi)

    beat_span = {}
    for bi, bl in enumerate(blocks):
        beat_t0 = cursor
        beat_w0 = len(new_wins)
        ev_in = [gi for gi, e in enumerate(events)
                 if float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
        if not ev_in:
            continue
        target = bl["target"]
        # ---- 钩子块：规划阶段锁定的「连续未裁切爽点段」整段前置 ----
        if bi == 0 and bl.get("type") == "钩子":
            hook_budget = min(target * HOOK_USED_RATIO, HOOK_SHOTS_N * HOOK_SHOT_MAX)
            pj = proj_dir / "project.json"
            cfg = _load(pj) if pj.exists() else {}
            override = (cfg.get("recap") or {}).get("hook_clip")
            a, z, gi = _pick_hook_clip(shots, events, hook_budget, override)
            if z > a:
                cur, left = a, z - a
                while left > 0.05:
                    dur = min(HOOK_SHOT_MAX, max(HOOK_SHOT_MIN, left))
                    if left - dur < HOOK_SHOT_MIN:   # 余量不足一窗就并进最后一窗
                        dur = left
                    emit(cur, dur, gi if gi is not None else 0, bi)
                    new_wins[-1]["reason"] = f"前置爽点·连续未裁切 {a:.2f}-{z:.2f}s"
                    cur += dur
                    left -= dur
                # 不标 used：钩子是「提前复用」，正片里这段必须原样再演一遍
                target = max(4.0, target - (z - a))
        # 预算按权重分配；爆发事件（爽点核心）最少 5s——高潮需要整段演出；
        # 其余单事件最少 1.6s，超出预算池的截断
        weights = {gi: _weight(events[gi], causal, gi) for gi in ev_in}
        wsum = sum(weights.values()) or 1.0
        def _min_alloc(gi):
            c = causal.get(str(gi)) or {}
            return 5.0 if (c.get("func") or events[gi].get("type", "")) == "爆发" else 1.6
        alloc = {gi: max(_min_alloc(gi), target * weights[gi] / wsum) for gi in ev_in}
        # 总量超预算则等比收回
        over = sum(alloc.values()) - target
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
        # ---- 爆发段原声留白（博主：高潮戏旁白闭嘴）----
        if not (bi == 0 and bl.get("type") == "钩子"):
            mark_climax_raw(bi, beat_w0)
        beat_span[bi] = (round(beat_t0, 3), round(cursor, 3))

    # ---- 末块结尾 cliffhanger：尾部 ~12s 原声留白 ----
    if blocks and blocks[-1].get("type") == "收尾" and new_wins:
        budget = 0.0
        wi = len(new_wins) - 1
        last_beat = meta[-1]["beat"]
        while wi >= 0 and meta[wi]["beat"] == last_beat and budget < CLIFFHANGER_SEC:
            budget += new_wins[wi]["dur"]
            wi -= 1
        _mark_raw(last_beat, wi + 1, len(new_wins) - 1)

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
