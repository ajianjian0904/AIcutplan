# -*- coding: utf-8 -*-
"""句级事件对齐：旁白句子 → 剧情 → 事件 → 画面，替代 n3 的块级均匀铺刀。

n3_align 的问题：它在**块级别**把 k = T/1.32 刀均匀铺满块源区间（step 均分），
句子落在哪段画面只由时间占比决定。块内一有多个事件（通常 5-8 个），
"句讲 A、画演 B"就是常态。实测 EP01：开除块 9 句跨 6 个事件，
"他上个月才挡下团灭"这句响起时画面在演"装备齐了强化师没用了"。

这里改成三层映射：
  句（按实测语音时长）→ 事件（按 keep 建议保留秒加权，两指针顺序配对）
                        → 刀（只落在所配事件的源区间内，吸附镜头开头）

块级经济性不变：每块总刀数 k_b = round(T_b/1.32)、每刀 1.32s、
块成片跨度 = k_b×1.32、句子按 n3 同款规则缩放 gap 铺进块跨度，
cut.py / relayout / r6 / r7 / make_srt 的下游契约（cutplan.json / align.json schema）不变。

事件区间装不下该句的刀时，向同块相邻事件借区间；没有事件表的块退回均匀铺。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LEAD, GAP, CUT, SNAP = 0.10, 0.12, 1.32, 0.60
MIN_STEP = 0.30          # 同句两刀至少在源上跳开多少秒，否则等于没切


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def shots_of(proj_dir: Path) -> list[dict]:
    """镜头边界表：优先 shots-cache.json，退回 shotlist.csv。"""
    f = proj_dir / "json" / "shots-cache.json"
    if f.exists():
        bounds = _load(f)["bounds"]
    else:
        import story_parse as SP
        sl = SP.shotlist(proj_dir)
        bounds = [0.0] + [s["b"] for s in sl]
        f.parent.mkdir(exist_ok=True)
        f.write_text(json.dumps({"bounds": bounds, "fps": 24}, ensure_ascii=False), encoding="utf-8")
    return [{"i": i, "a": bounds[i], "b": bounds[i + 1]} for i in range(len(bounds) - 1)]


def assign_sents_to_events(sents: list[dict], events: list[dict]) -> list[int]:
    """两指针：句按 (real+gap) 累计占比，事件按 keep 累计占比，顺序配对。

    返回每句对应的事件下标（长度 == len(sents)）。
    """
    n, m = len(sents), len(events)
    out = [0] * n
    if m <= 1:
        return [0] * n
    total_s = sum(s["dur"] + GAP for s in sents) or 1.0
    total_k = sum(max(1.0, float(e.get("keep") or 3)) for e in events) or 1.0
    j, acc_k = 0, 0.0
    for i, s in enumerate(sents):
        ratio_s = (sum(x["dur"] + GAP for x in sents[:i + 1])) / total_s
        # 事件游标推进到：keep 累计占比 ≥ 句累计占比
        while j < m - 1 and acc_k / total_k < ratio_s - 1e-9:
            acc_k += max(1.0, float(events[j].get("keep") or 3))
            j += 1
        out[i] = j
    return out


# ---------------------------------------------------------------- 语义配对腿
SEM_SYS = ("你是动漫解说的分镜匹配师。给你解说稿的句子（按剧情块分组、块内按时间顺序）"
           "和 AI 从原片识别出的事件表（含源片秒区间）。任务：给每个句子挑出它所描述的事件。")

SEM_Q = """【解说句子】按块分组，格式 "块号:句子全局编号 文本"。
{sent_lines}

【事件表】格式 "事件全局编号 [t0-t1s] who: event (类型, 建议保留keep秒)"。
{ev_lines}

【块的有效事件范围】块号 → 事件全局编号区间（句子只能配该范围内的，且块内配对结果的事件编号必须不递减，允许重复）。
{ranges}

只输出 JSON 对象，不要解释：{{"句子全局编号": 事件全局编号, ...}}
原则：优先按"句子说的内容 vs 事件内容"匹配；回忆/总结/评价性质的句子配它所指的事件，而非字面相邻的事件。"""


def semantic_map(proj_dir: Path, by_beat: dict, blocks: list[dict],
                 events: list[dict]) -> dict[int, int] | None:
    """语义腿：DeepSeek 一次性给全片 127 句配事件。失败/无效则返回 None（兜底两指针）。

    结果缓存在 story_plan.json 的 sent_event_map，重跑对齐不再花 token。
    """
    import story_parse as SP
    plan = SP.load_plan(proj_dir)
    cached = plan.get("sent_event_map")
    n_total = sum(len(v) for v in by_beat.values())
    if isinstance(cached, dict) and len(cached) >= n_total:
        try:
            return {int(k): int(v) for k, v in cached.items()}
        except (ValueError, TypeError):
            pass

    sent_lines, all_sents = [], []
    for b in sorted(by_beat):
        for s in by_beat[b]:
            sent_lines.append(f"{b}:{s['i']} {s['text']}")
            all_sents.append(s["i"])
    ev_lines = [f"[{i}] {float(e.get('t0', 0)):.0f}-{float(e.get('t1', 0)):.0f}s "
                f"{e.get('who', '')}: {e.get('event', '')} ({e.get('type', '')}, keep{e.get('keep')})"
                for i, e in enumerate(events)]
    ranges = {}
    for bi, b in enumerate(sorted(by_beat)):
        bl = blocks[bi] if bi < len(blocks) else None
        if bl is None:
            continue
        ids = [i for i, e in enumerate(events)
               if float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
        if ids:
            ranges[b] = f"{ids[0]}-{ids[-1]}"
    if not all_sents or not ev_lines:
        return None

    q = SEM_Q.format(sent_lines="\n".join(sent_lines), ev_lines="\n".join(ev_lines),
                     ranges="\n".join(f"  {b} → {r}" for b, r in ranges.items()))
    try:
        import narration as NR
        r = NR.llm_chat(SEM_SYS, q, temperature=0.1, timeout=600)
        txt = (r.get("text") or "").strip()
        m = txt.find("{")
        pairs = json.loads(txt[m:txt.rfind("}") + 1]) if m >= 0 else {}
        out = {int(k): int(v) for k, v in pairs.items()}
    except Exception as e:
        print(f"[语义配对] 失败，退回比例法：{type(e).__name__} {str(e)[:120]}", flush=True)
        return None

    # 校验：覆盖全句、编号合法、块内不递减
    valid = len(out) >= len(all_sents)
    for bi, b in enumerate(sorted(by_beat)):
        bl = blocks[bi] if bi < len(blocks) else None
        ids = [i for i, e in enumerate(events)
               if bl and float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
        lo, hi = (ids[0], ids[-1]) if ids else (0, len(events) - 1)
        prev = lo - 1
        for s in by_beat[b]:
            v = out.get(s["i"])
            if v is None or not (lo <= v <= hi) or v < prev:
                valid = False
                break
            prev = v
        if not valid:
            break
    if not valid:
        print("[语义配对] 返回不合法，退回比例法", flush=True)
        return None
    print(f"[语义配对] DeepSeek 配对 {len(out)} 句 ✓", flush=True)
    plan["sent_event_map"] = {str(k): v for k, v in out.items()}
    SP.save_plan(proj_dir, plan)
    return out


def cut_positions(window: tuple[float, float], k: int, shots: list[dict],
                  guard: tuple[float, float], used: set | None = None) -> list[float]:
    """在源区间 window 内给 k 刀找起点：均匀铺 + 吸附镜头开头 + 不越 guard。

    used：已占用刀起点（0.1s 精度），吸附时避开，防止相邻句共用同一镜头开头。
    """
    a0, a1 = max(window[0], guard[0]), min(window[1], guard[1])
    if a1 - a0 < 0.5:
        a0, a1 = window
    starts = [s["a"] for s in shots if a0 - 0.02 <= s["a"] <= a1]
    if used is not None:
        free = [x for x in starts if round(x, 1) not in used]
        starts = free or starts
    usable = max(0.6, a1 - a0 - CUT)
    if k <= 1:
        raw = [a0]
    else:
        step = usable / (k - 1) if k > 1 else 0.0
        raw = [a0 + i * step for i in range(k)]
    out, prev_end = [], -1e9
    for i, t in enumerate(raw):
        t = next((x for x in starts if abs(x - t) <= SNAP and x >= prev_end + MIN_STEP - 0.02),
                 t if t >= prev_end + MIN_STEP else prev_end + MIN_STEP)
        if used is not None and round(t, 1) in used:
            t = t + 0.7                      # 微移避开已用镜头开头
        t = min(t, a1 - 0.3) if a1 - 0.3 > t else t
        out.append(round(max(a0, t), 3))
        prev_end = t + CUT
    if used is not None:
        used.update(round(x, 1) for x in out)
    return out


def align(proj_dir: Path) -> dict:
    proj_dir = Path(proj_dir)
    d = proj_dir / "json"
    narr = _load(d / "narration.json")["items"]
    tts = {x["i"]: x for x in _load(d / "tts-result.json")["items"]}
    plan = _load(d / "cutplan.json")
    shots = shots_of(proj_dir)
    bounds_last = shots[-1]["b"]

    import story_parse as SP
    sp = SP.load_plan(proj_dir)
    blocks = [b for b in (sp.get("blocks") or []) if b["type"] != "收录" and b["target"] > 0]
    events = sp.get("events") or []

    by_beat: dict[int, list] = {}
    for it in narr:
        t = tts.get(it["i"], {})
        dur = float(t.get("trim_dur") or t.get("tts_dur") or it["dur"])
        by_beat.setdefault(it["beat"], []).append({**it, "dur": dur})
    beats = sorted(by_beat)
    sem_map = semantic_map(proj_dir, by_beat, blocks, events)

    # 保留旧 plan 里的 hook / hold_hl 窗（story 流程通常没有，EP05 式工程可能有）
    kept = [w for w in plan.get("windows", []) if w.get("tier") in ("hook", "hold_hl")]

    new_wins, align_rows = [], []
    cursor = 0.0
    emitted: set[int] = set()
    beat_new, beat_win, sent_beat, sent_span, sent_win_idx = {}, {}, {}, [], {}

    def emit(src_a: float, dur: float, label: str) -> None:
        nonlocal cursor
        new_wins.append({"src_start": round(src_a, 3), "src_end": round(src_a + dur, 3),
                         "tier": "line", "label": label, "lines": [],
                         "dur": round(dur, 3),
                         "new_start": round(cursor, 3), "new_end": round(cursor + dur, 3),
                         "sent_ids": []})
        cursor += dur

    for bi, b in enumerate(beats):
        if b in emitted:
            continue
        emitted.add(b)
        sents = by_beat[b]
        # 钩子窗（若有）排在最前，先照搬
        hook_w = [w for w in kept if w["tier"] == "hook"]
        if hook_w and bi == 0 and float(sents[0].get("anchor", sents[0]["t0"])) < 0.5 \
                and plan.get("stats", {}).get("mode") == "hook-first":
            for w in hook_w:
                emit(w["src_start"], w["dur"], w.get("label", ""))
            beat_new[b] = (0.0, cursor)
            beat_win[b] = (0, len(new_wins))
            continue

        bl = blocks[bi] if bi < len(blocks) else None
        if bl is None:
            continue
        beat_t0, beat_i0 = cursor, len(new_wins)
        T = sum(s["dur"] + GAP for s in sents)
        k_b = max(1, int(round(T / CUT)))
        span_b = k_b * CUT

        # 句 → 刀数分配（按时间占比）；只有 ks×CUT 真装不下语音时才加刀（ceil(real/CUT)），
        # 否则按占比取整即可 —— 过度加刀会把成片撑长、稀释旁白覆盖
        shares = [(s["dur"] + GAP) / max(T, 1e-6) for s in sents]
        ks = [max(1, int(round(k_b * x))) for x in shares]
        for i, s in enumerate(sents):
            need = int(-(-s["dur"] // CUT))            # ceil(real / CUT)
            if ks[i] < need:
                ks[i] = need
        k_b = sum(ks)
        span_b = k_b * CUT

        # 句 → 事件配对：语义腿优先（DeepSeek 配的，全局编号），比例法兜底
        ev_in = [e for e in events
                 if float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
        if not ev_in:                      # 没事件表：整块当一个事件
            ev_in = [{"t0": bl["t0"], "t1": bl["t1"], "keep": bl["target"]}]
        if sem_map:
            gids = [i for i, e in enumerate(events)
                    if float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
            ev_idx = []
            for s in sents:
                g = sem_map.get(s["i"])
                ev_idx.append(gids.index(g) if g is not None and g in gids else 0)
        else:
            ev_idx = assign_sents_to_events(sents, ev_in)

        # 每句的画面窗口：连续同事件的句共享一个事件区间 → 按刀数切成不重叠子区间，
        # 否则同样的铺刀公式会产出完全相同的刀位，画面原样重复。
        src_windows: list[tuple[float, float] | None] = [None] * len(sents)
        head_guard = shots[0]["b"] if bi == 0 else 0.0   # 第一块跳过片头（黑屏标题卡+制作名单）
        i2 = 0
        while i2 < len(sents):
            j2 = i2
            while j2 + 1 < len(sents) and ev_idx[j2 + 1] == ev_idx[i2]:
                j2 += 1
            e = ev_in[ev_idx[i2]]
            e0, e1 = float(e["t0"]), float(e["t1"])
            K = sum(ks[x] for x in range(i2, j2 + 1))
            need = K * CUT
            guard_l = float(ev_in[ev_idx[i2] - 1]["t1"]) if ev_idx[i2] > 0 else bl["t0"]
            guard_r = float(ev_in[ev_idx[i2] + 1]["t0"]) if ev_idx[i2] + 1 < len(ev_in) else bl["t1"]
            while e1 - e0 < need and (e0 > guard_l or e1 < guard_r):
                grow_l = (e0 - guard_l) if e0 > guard_l else 0.0
                grow_r = (guard_r - e1) if e1 < guard_r else 0.0
                if grow_l >= grow_r and grow_l > 0:
                    e0 = max(guard_l, e0 - need + (e1 - e0))
                elif grow_r > 0:
                    e1 = min(guard_r, e1 + need - (e1 - e0))
                else:
                    break
            if bi == 0 and e1 - head_guard >= need:
                e0 = max(e0, head_guard)
            cur = e0
            for x in range(i2, j2 + 1):
                seg = (e1 - e0) * ks[x] / max(1, K)
                src_windows[x] = (cur, cur + seg)
                cur += seg
            i2 = j2 + 1

        # 铺刀：句按顺序；块的最后一刀吃掉取整余量，保证 Σdur == span_b
        # 同时记录每句的窗口序号区间（relayout_smart 用它把音频精确压在该句自己的画面上）
        beat_done = 0.0
        used_starts: set = set()
        for i, s in enumerate(sents):
            k_i = ks[i]
            i0w = len(new_wins)
            positions = cut_positions(src_windows[i], k_i, shots, (bl["t0"], bl["t1"]),
                                      used_starts)
            for jx, p in enumerate(positions):
                is_last = (i == len(sents) - 1 and jx == k_i - 1)
                dur = max(0.6, span_b - beat_done) if is_last else CUT
                dur = min(dur, bounds_last - p - 0.02)
                if dur < 0.18:
                    continue
                emit(p, dur, s["text"][:26])
                beat_done += dur
            if len(new_wins) > i0w:
                sent_win_idx[str(s["i"])] = [i0w, len(new_wins) - 1]
        beat_new[b] = (beat_t0, cursor)
        beat_win[b] = (beat_i0, len(new_wins))

    # 句子时间轴：与 n3 相同，按块实际跨度缩放 gap
    for b in beats:
        sents = by_beat[b]
        bs, be = beat_new.get(b, (0.0, 0.0))
        span = max(0.0, be - bs)
        gap_k = max(0.0, (span - sum(s["dur"] for s in sents)) / max(1, len(sents)))
        t = bs
        for s in sents:
            sent_span.append([s["i"], round(t, 3), round(t + s["dur"] + gap_k, 3), round(s["dur"], 3)])
            sent_beat[str(s["i"])] = b
            t += s["dur"] + gap_k
    for nw in new_wins:
        lo, hi = nw["new_start"], nw["new_end"]
        nw["sent_ids"] = [i for i, a, b2, _ in sent_span if b2 > lo + 1e-6 and a < hi - 1e-6]
    for nw in new_wins:
        align_rows.append({"kind": nw["tier"], "sent": (nw["sent_ids"] or [None])[0],
                           "text": nw["label"], "src": nw["src_start"],
                           "new_start": nw["new_start"], "dur": nw["dur"],
                           "sents": nw["sent_ids"]})

    total = round(cursor, 2)
    plan_out = {"windows": new_wins,
                "stats": {"src_dur": (plan.get("stats") or {}).get("src_dur", bounds_last),
                          "out_dur": total, "out_dur_actual": total,
                          "segments": len(new_wins),
                          "cuts_per_min": round(len(new_wins) / max(total / 60, 0.01), 1),
                          "mode": "event-aware-narration-cut"}}
    align_out = {"windows": new_wins, "align": align_rows,
                 "beat_new": {str(b): [round(v[0], 3), round(v[1], 3)] for b, v in beat_new.items()},
                 "beat_win": {str(b): list(v) for b, v in beat_win.items()},
                 "sent_beat": sent_beat,
                 "sent_win_idx": sent_win_idx,
                 "sent_span": sent_span}
    (d / "cutplan.json").write_text(json.dumps(plan_out, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "align.json").write_text(json.dumps(align_out, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"cuts": len(new_wins), "duration": total, "beats": len(beats)}


def main() -> int:
    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("--project", required=True)
    a = ap.parse_args()
    proj_dir = Path(a.project).expanduser().resolve().parent
    r = align(proj_dir)
    print(f"[句级对齐] {r['cuts']} 刀 / {r['duration']}s / {r['beats']} 块", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
