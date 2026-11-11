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
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import narration as NR  # noqa: E402  DeepSeek 客户端（语义配对/跨块覆盖共用）

LEAD, GAP, CUT, SNAP = 0.10, 0.12, 1.32, 0.60
MIN_STEP = 0.30          # 同句两刀至少在源上跳开多少秒，否则等于没切


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def shots_of(proj_dir: Path) -> list[dict]:
    """镜头边界表（优先 shots-cache.json，退回 shotlist.csv）+ 镜头描述（shot_desc 产物）。"""
    f = proj_dir / "json" / "shots-cache.json"
    if f.exists():
        bounds = _load(f)["bounds"]
    else:
        import story_parse as SP
        sl = SP.shotlist(proj_dir)
        bounds = [0.0] + [s["b"] for s in sl]
        f.parent.mkdir(exist_ok=True)
        f.write_text(json.dumps({"bounds": bounds, "fps": 24}, ensure_ascii=False), encoding="utf-8")
    shots = [{"i": i, "a": bounds[i], "b": bounds[i + 1]} for i in range(len(bounds) - 1)]
    df = proj_dir / "json" / "shots_desc.json"
    if df.exists():
        rows = {round(float(r["a"]), 2): r for r in _load(df).get("rows", [])}
        for sh in shots:
            r = rows.get(round(sh["a"], 2))
            if r:
                sh["desc"] = r.get("desc", "")
                sh["obj"] = r.get("obj", "")
    return shots


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


# ---------------------------------------------------------------- 跨块覆盖
SEM2_SYS = ("你是动漫解说的分镜匹配师。有些解说句子描述的具体内容（某件装备挡攻击、某段回忆、"
            "某个大招）在其所属剧情块的画面里根本没有出现——块画面只是剧情发生地。"
            "你的任务：找出这些句子，从全片事件表里挑出真正演了这件事的段落。")

SEM2_Q = """【解说句子】"句子编号 | 文本 | 当前配对: [事件编号] t0-t1s who:event"
{sents}

【全片事件表】"事件编号 [t0-t1s] who: event (类型, keep)"
{events}

判断标准：句子里提到的具体对象或动作（某件衣服挡住攻击、某次回忆闪回、某个招式），在当前配对
事件的画面里是否可能出现——出现了就不动它；出现不了（本块只是剧情发生地，内容在别处演），
才从全片事件表里挑画面真正演了这个内容的事件。
注意：只挑内容错位的句子，不要因为"换个事件也沾边"就乱改；预计只占少数。

只输出 JSON：{{"句子编号": {{"ev": 事件编号, "why": "一句话理由"}}}}，没有需要改的就输出 {{}}"""


def semantic_map(proj_dir: Path, by_beat: dict, blocks: list[dict],
                 events: list[dict]) -> dict[int, int] | None:
    """语义腿：DeepSeek 一次性给全片 127 句配事件。失败/无效则返回 None（兜底两指针）。

    结果缓存在 story_plan.json 的 sent_event_map，重跑对齐不再花 token。
    """
    import story_parse as SP
    plan = SP.load_plan(proj_dir)
    cached = plan.get("sent_event_map")
    n_total = sum(len(v) for v in by_beat.values())
    out: dict[int, int] | None = None
    if isinstance(cached, dict) and len(cached) >= n_total:
        try:
            out = {int(k): int(v) for k, v in cached.items()}
        except (ValueError, TypeError):
            out = None

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
    if out is not None:
        print(f"[语义配对] 命中缓存 {len(out)} 句", flush=True)
        plan = _apply_override(proj_dir, plan, out, by_beat, blocks, events,
                               all_sents, ev_lines, SP)
        return out

    q = SEM_Q.format(sent_lines="\n".join(sent_lines), ev_lines="\n".join(ev_lines),
                     ranges="\n".join(f"  {b} → {r}" for b, r in ranges.items()))
    try:
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
    plan = _apply_override(proj_dir, plan, out, by_beat, blocks, events,
                           all_sents, ev_lines, SP)
    return out


def _apply_override(proj_dir: Path, plan: dict, out: dict[int, int], by_beat: dict,
                    blocks: list[dict], events: list[dict], all_sents: list[int],
                    ev_lines: list[str], SP) -> dict:
    """跨块覆盖（第二腿）：钩子/回忆/道具句在本块画面里演不出来，允许全片找。"""
    cached_ov = plan.get("sent_event_override")
    if isinstance(cached_ov, dict):
        ov = {int(k): int(v["ev"]) for k, v in cached_ov.items()}
    else:
        cur_lines = []
        for b in sorted(by_beat):
            bl = blocks[b] if b < len(blocks) else None
            ids = [i for i, e in enumerate(events)
                   if bl and float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
            for s in by_beat[b]:
                g = out.get(s["i"], ids[0] if ids else 0)
                e = events[g] if g < len(events) else {"t0": 0, "t1": 0}
                cur_lines.append(f"{s['i']} | {s['text']} | 当前: [{g}] "
                                 f"{float(e.get('t0', 0)):.0f}-{float(e.get('t1', 0)):.0f}s "
                                 f"{e.get('who', '')}:{e.get('event', '')}")
        q2 = SEM2_Q.format(sents="\n".join(cur_lines), events="\n".join(ev_lines))
        ov = {}
        try:
            r2 = NR.llm_chat(SEM2_SYS, q2, temperature=0.1, timeout=600)
            t2 = (r2.get("text") or "").strip()
            m2 = t2.find("{")
            raw = json.loads(t2[m2:t2.rfind("}") + 1]) if m2 >= 0 else {}
            for k, v in raw.items():
                i = int(k)
                g = int(v.get("ev", -1))
                if i in out and 0 <= g < len(events) and g != out[i]:
                    ov[i] = g
        except Exception as e:
            print(f"[跨块覆盖] 失败（不影响主流程）：{type(e).__name__} {str(e)[:100]}", flush=True)
        if len(ov) > 0.4 * max(1, len(all_sents)):
            print(f"[跨块覆盖] 覆盖 {len(ov)} 句过多（>{40}%），放弃", flush=True)
            ov = {}
        if ov:
            plan["sent_event_override"] = {str(k): {"ev": v} for k, v in ov.items()}
            print(f"[跨块覆盖] {len(ov)} 句改配全片事件", flush=True)
            for i, g in ov.items():
                e = events[g]
                print(f"   句{i} → [{g}] {e['t0']:.0f}-{e['t1']:.0f}s {e['who']}:{e['event']}", flush=True)
    out.update(ov)
    plan["sent_event_map_final"] = {str(k): v for k, v in out.items()}
    SP.save_plan(proj_dir, plan)
    return plan


# -*- coding: utf-8 -*-
SHOT_SYS = ("你是动漫解说的分镜匹配师。给你解说句子和它应配事件的候选镜头（每个镜头带一句内容描述）。"
            "任务：为每个句子挑出画面内容最贴合它的镜头编号。")

SHOT_Q = """【句子与候选镜头】格式 "句子编号 | 句子文本 | 事件概要 | 候选: 镜头号(描述)"
{lines}

只输出 JSON：{{"句子编号": [镜头号, ...]}}（按贴合度排序，最多 3 个；没有贴合的就给空数组 []）
原则：
1. 句子提到具体对象/动作（外套挡攻击、收公会牌、拔剑）→ 挑画面里真的在演这个的镜头。
   当前事件候选里没有时，优先挑 ※全片检索 标记的镜头。
2. 句子是氛围/总结性的 → 挑该事件里情绪最强烈的镜头。
3. 相邻句子尽量挑不同镜头，避免画面重复。"""


def sentence_shot_map(proj_dir: Path, by_beat: dict, blocks: list[dict],
                      events: list[dict], shots: list[dict],
                      scene_ts: dict[int, list[float]] | None = None) -> dict[int, list[int]]:
    """内容级句→镜头匹配（一次文本调用）。失败/描述不足时返回 {}（退回时序取镜）。

    候选池 = 事件区间内镜头 + 全片场景检索（scene_ts）映射回的镜头（标 ※）。
    """
    import story_parse as SP
    plan = SP.load_plan(proj_dir)
    cached = plan.get("sent_shot_map")
    n_total = sum(len(v) for v in by_beat.values())
    # DeepSeek 只给有贴合镜头的句配镜，条数天然 < n_total；有缓存就直接用
    if isinstance(cached, dict) and cached:
        try:
            return {int(k): [int(x) for x in v] for k, v in cached.items()}
        except Exception:
            pass
    final = plan.get("sent_event_map_final") or plan.get("sent_event_map") or {}
    has_desc = sum(1 for s in shots if s.get("desc"))
    if has_desc < len(shots) * 0.6:
        print(f"[句镜匹配] 镜头描述不足（{has_desc}/{len(shots)}），退回按时间序取镜", flush=True)
        return {}
    lines, valid_ids = [], {}
    for b in sorted(by_beat):
        bl = blocks[b] if b < len(blocks) else None
        ids = [i for i, e in enumerate(events)
               if bl and float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
        for s in by_beat[b]:
            g = final.get(str(s["i"]))
            g = int(g) if g is not None else (ids[0] if ids else 0)
            e = events[g] if g < len(events) else None
            e0 = float(e.get("t0", 0)) if e else 0.0
            e1 = float(e.get("t1", bl["t1"] if bl else 1e9)) if e else 1e9
            pool = [sh for sh in shots if e0 <= sh["a"] < e1]
            pool_ids = {sh["i"] for sh in pool}
            extra: list[dict] = []
            for t in (scene_ts or {}).get(s["i"], []):
                sh = _shot_at_t(shots, t)
                if sh and sh["i"] not in pool_ids and all(sh["i"] != x["i"] for x in extra):
                    extra.append(sh)
            all_pool = pool + extra
            valid_ids[s["i"]] = [sh["i"] for sh in all_pool]
            pl = " ; ".join(f"镜头{sh['i']}({sh.get('desc', '')})" for sh in pool) or "（无镜头）"
            if extra:
                pl += " ; ※全片检索: " + " ; ".join(
                    f"镜头{sh['i']}@{sh['a']:.0f}s({sh.get('desc', '')})" for sh in extra)
            ev_s = f"[{g}] {e.get('who', '')}:{e.get('event', '')}" if e else "-"
            lines.append(f"{s['i']} | {s['text']} | {ev_s} | 候选: {pl}")
    if not lines:
        return {}
    q = SHOT_Q.replace("{lines}", "\n".join(lines))
    out: dict[int, list[int]] = {}
    try:
        r = NR.llm_chat(SHOT_SYS, q, temperature=0.1, timeout=600)
        txt = (r.get("text") or "").strip()
        m = txt.find("{")
        raw = json.loads(txt[m:txt.rfind("}") + 1]) if m >= 0 else {}
        for k, v in raw.items():
            i = int(k)
            ids = v if isinstance(v, list) else (v or {}).get("shots", [])
            ok = [int(x) for x in ids if int(x) in valid_ids.get(i, [])]
            if ok:
                out[i] = ok[:4]
        print(f"[句镜匹配] {len(out)} 句已按内容配镜", flush=True)
    except Exception as e:
        print(f"[句镜匹配] 失败（退回按时间序）：{type(e).__name__} {str(e)[:100]}", flush=True)
        return {}
    plan["sent_shot_map"] = {str(k): v for k, v in out.items()}
    SP.save_plan(proj_dir, plan)
    return out


# ---------------------------------------------------------------- 全片场景检索
SEMG_SYS = ("你是动漫解说的画面检索师。给你解说句子（按顺序）和全片每 2 秒的画面场景索引"
            "（秒数|镜头语言|场景描述|台词）。任务：为句子找出全片里**真正演了这句话内容**的时间点。")

SEMG_Q = """【解说句子】"编号 | 文本"
{sent_lines}

【全片场景索引】"秒数 | 镜头语言 | 场景描述 | 台词"
{index}

只输出 JSON，不要解释：{{"句子编号": [秒数, ...]}}
原则：
1. 句子提到具体对象/动作（外套挡攻击、紫光强化、点数球汇聚、挥剑、抱起狂奔）→ 给出演这个内容的
   秒数（动作高潮帧优先），最多 3 个，按贴合度排序。
2. 句子是纯评论/总结/氛围，或全片没有演这个内容的画面 → 给 []，不要硬凑。
3. 场景描述和台词是判断依据；台词是日语原文，含义以描述里的中文转述为准。
4. 秒数必须是索引里出现过的秒数。"""


def scene_global_map(proj_dir: Path, by_beat: dict) -> dict[int, list[float]]:
    """全片场景检索腿：714 窗 scene_desc 做索引，DeepSeek 一次文本调用按句子检索时间点。

    缓存 story_plan.json 的 sent_scene_global（秒数）。scene_desc 重生成后须删除此键。
    失败返回 {}（不影响主流程，只是没有跨块候选）。
    """
    import story_parse as SP
    plan = SP.load_plan(proj_dir)
    cached = plan.get("sent_scene_global")
    n_total = sum(len(v) for v in by_beat.values())
    # 命中本来就是稀疏的（只给有跨块画面的句），有任何缓存就直接用
    if isinstance(cached, dict) and cached:
        try:
            return {int(k): [float(x) for x in v] for k, v in cached.items()}
        except Exception:
            pass

    f = proj_dir / "json" / "scene_desc.json"
    if not f.exists():
        return {}
    rows = [r for r in _load(f).get("rows", []) if r.get("desc")]
    if not rows:
        return {}
    index = "\n".join(f"{float(r['t']):.0f} | {r.get('cam', '')} | {r['desc']} | {r.get('dlg', '')[:20]}"
                      for r in rows)
    sent_lines = []
    for b in sorted(by_beat):
        for s in by_beat[b]:
            sent_lines.append(f"{s['i']} | {s['text']}")
    q = SEMG_Q.format(sent_lines="\n".join(sent_lines), index=index)
    try:
        r = NR.llm_chat(SEMG_SYS, q, temperature=0, timeout=600)
        txt = (r.get("text") or "").strip()
        m = txt.find("{")
        raw = json.loads(txt[m:txt.rfind("}") + 1]) if m >= 0 else {}
        valid_ts = {round(float(r_["t"]), 1) for r_ in rows}
        out = {}
        for k, ts in raw.items():
            i = int(k)
            keep = []
            for t in (ts if isinstance(ts, list) else []):
                try:
                    t = float(t)
                except (TypeError, ValueError):
                    continue
                # 吸附到最近的索引秒
                best = min(valid_ts, key=lambda z: abs(z - t)) if valid_ts else None
                if best is not None and abs(best - t) <= 2.0:
                    keep.append(best)
            if keep:
                out[i] = keep[:3]
        plan["sent_scene_global"] = {str(k): v for k, v in out.items()}
        SP.save_plan(proj_dir, plan)
        n_hit = len(out)
        print(f"[全片检索] {n_hit}/{n_total} 句命中跨块画面", flush=True)
        return out
    except Exception as e:
        print(f"[全片检索] 失败（不影响主流程）：{type(e).__name__} {str(e)[:120]}", flush=True)
        return {}


def _shot_at_t(shots: list[dict], t: float) -> dict | None:
    for sh in shots:
        if sh["a"] <= t < sh["b"]:
            return sh
    return min(shots, key=lambda z: min(abs(z["a"] - t), abs(z["b"] - t)), default=None)


def _clamp_shot(sh: dict, lo: float = 0.8, hi: float = 3.2) -> float:
    """整镜头时长：全长截到 [lo, hi]（从镜头开头播）。"""
    return min(hi, max(lo, float(sh["b"]) - float(sh["a"])))


def align(proj_dir: Path) -> dict:
    """整镜头剪辑引擎：语音时长驱动镜头选择，起止吸附镜头边界，句→镜头按内容匹配。

    与旧版（固定 1.32s 均匀刀）的差异：
      - 每句需要时长 = 语音实测 + 0.2s，用**整镜头**去填（全长截到 0.8-3.2s，博主节奏）
      - 起止都在镜头边界（消灭跳切）；一个镜头全片只用一次（消灭重复）
      - 镜头优先级：内容匹配（句镜匹配）> 事件内时序 > 相邻事件借镜
      - 时间轴账户在句级（Σ镜头时长 ≥ 语音），句子之间自然留出呼吸空档
    """
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
    scene_ts = scene_global_map(proj_dir, by_beat)
    shot_map = sentence_shot_map(proj_dir, by_beat, blocks, events, shots, scene_ts)

    shot_by_id = {s["i"]: s for s in shots}
    head_guard = shots[0]["b"]            # 片头（黑屏标题卡+制作名单）
    used_shots: set = set()               # 全片级：一个镜头只用一次

    new_wins, align_rows = [], []
    cursor = 0.0
    beat_new, beat_win, sent_beat, sent_span, sent_win_idx = {}, {}, {}, [], {}

    def emit(sid: int, src_a: float, dur: float, label: str) -> None:
        nonlocal cursor
        new_wins.append({"src_start": round(src_a, 3), "src_end": round(src_a + dur, 3),
                         "tier": "line", "label": label, "lines": [],
                         "dur": round(dur, 3),
                         "new_start": round(cursor, 3), "new_end": round(cursor + dur, 3),
                         "sent_ids": [sid]})
        rng = sent_win_idx.setdefault(str(sid), [len(new_wins) - 1, len(new_wins) - 1])
        rng[1] = len(new_wins) - 1
        cursor += dur

    for bi, b in enumerate(beats):
        if b in beat_new:
            continue
        sents = by_beat[b]
        bl = blocks[bi] if bi < len(blocks) else None
        if bl is None:
            continue
        beat_t0, beat_i0 = cursor, len(new_wins)

        ev_in = [e for e in events
                 if float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
        if not ev_in:
            ev_in = [{"t0": bl["t0"], "t1": bl["t1"], "keep": bl["target"]}]
        gids = [i for i, e in enumerate(events)
                if float(e.get("t0", 0)) < bl["t1"] and float(e.get("t1", 0)) > bl["t0"]]
        if sem_map:
            fb = assign_sents_to_events(sents, ev_in)
            g_seq = []
            for x, s_ in enumerate(sents):
                g = sem_map.get(s_["i"])
                g_seq.append(g if g is not None and 0 <= g < len(events)
                             else (gids[fb[x]] if gids else 0))
        else:
            fb = assign_sents_to_events(sents, ev_in)
            g_seq = [gids[x] for x in fb] if gids else [0] * len(sents)

        # 分组：连续同事件的句共享镜头池，按句序依次领取（保持叙事时序、不重复）
        i2 = 0
        while i2 < len(sents):
            j2 = i2
            while j2 + 1 < len(sents) and g_seq[j2 + 1] == g_seq[i2]:
                j2 += 1
            gi = g_seq[i2]
            e = events[gi]
            e0, e1 = float(e["t0"]), float(e["t1"])

            for x in range(i2, j2 + 1):
                s = sents[x]
                need = s["dur"] + 0.15
                got = 0.0
                picks: list[int] = []

                def usable(sh) -> bool:
                    return (sh["i"] not in used_shots
                            and not (bi == 0 and (sh["a"] + sh["b"]) / 2 < head_guard))

                def try_take(sh) -> None:
                    nonlocal got
                    if got >= need + 1e-6 or not usable(sh):
                        return
                    used_shots.add(sh["i"])
                    cap = need + 0.4 if got == 0 else need - got + 0.10   # 首刀给呼吸尾，补足刀贴预算
                    d_want = min(_clamp_shot(sh), cap, 6.0, bounds_last - sh["a"] - LEAD - 0.02)
                    if d_want < 0.45:
                        return
                    emit(s["i"], sh["a"] + LEAD, d_want, s["text"][:26])
                    got += d_want
                    picks.append(sh["i"])

                # 1) 内容匹配的镜头优先：全片检索命中（句子的具体内容）> 事件区间内 > 其余
                gids = set()
                for t in scene_ts.get(s["i"], []):
                    sh_t = _shot_at_t(shots, t)
                    if sh_t:
                        gids.add(sh_t["i"])
                pref_g, pref_in, pref_out = [], [], []
                for sid in shot_map.get(s["i"], []):
                    sh = shot_by_id.get(sid)
                    if not sh:
                        continue
                    if sid in gids:
                        pref_g.append(sh)
                    elif e0 - 1.0 <= sh["a"] < e1 + 1.0:
                        pref_in.append(sh)
                    else:
                        pref_out.append(sh)
                if os.environ.get("ALIGN_DEBUG"):
                    print(f"[DBG] 句{s['i']} need={need:.2f} e=[{e0:.0f},{e1:.0f}] "
                          f"g={[z['i'] for z in pref_g]} in={[z['i'] for z in pref_in]} "
                          f"out={[z['i'] for z in pref_out]}", flush=True)
                for sh in pref_g:                       # 检索命中：按贴合度排序，不按时间轴
                    try_take(sh)
                for sh in sorted(pref_in, key=lambda z: z["a"]):
                    try_take(sh)
                for sh in pref_out:
                    try_take(sh)
                # 2) 事件区间内按时间序补足
                if got < need:
                    pool = [sh for sh in shots if e0 <= sh["a"] < e1]
                    for sh in sorted(pool, key=lambda z: z["a"]):
                        try_take(sh)
                        if got >= need:
                            break
                # 3) 相邻事件借镜头（±1..4）
                if got < need:
                    for dj in (-1, 1, -2, 2, -3, 3, -4, 4):
                        gj = gi + dj
                        if not (0 <= gj < len(events)):
                            continue
                        ee = events[gj]
                        pool2 = [sh for sh in shots
                                 if float(ee["t0"]) <= sh["a"] < float(ee["t1"])]
                        for sh in sorted(pool2, key=lambda z: z["a"]):
                            try_take(sh)
                            if got >= need:
                                break
                        if got >= need:
                            break
                # 4) 保底：把本句已选的刀按其镜头全长逐个延长，最后允许越出镜头尾
                if got < need and picks:
                    for sid in reversed(picks):
                        if got >= need:
                            break
                        sh = shot_by_id[sid]
                        full = min(float(sh["b"]) - float(sh["a"]) - LEAD, 6.0,
                                   bounds_last - sh["a"] - LEAD - 0.02)
                        cur_w = next(w for w in new_wins
                                     if abs(w["src_start"] - (sh["a"] + LEAD)) < 0.02)
                        extra = min(need - got, max(0.0, full - cur_w["dur"]))
                        if extra > 0.05:
                            cur_w["src_end"] = round(cur_w["src_end"] + extra, 3)
                            cur_w["dur"] = round(cur_w["dur"] + extra, 3)
                            cur_w["new_end"] = round(cur_w["new_end"] + extra, 3)
                            cursor += extra
                            got += extra
                    if got < need:
                        sh = shot_by_id[picks[-1]]
                        cur_w = next(w for w in new_wins
                                     if abs(w["src_start"] - (sh["a"] + LEAD)) < 0.02)
                        extra = min(need - got + 0.05, 1.2)
                        cur_w["src_end"] = round(cur_w["src_end"] + extra, 3)
                        cur_w["dur"] = round(cur_w["dur"] + extra, 3)
                        cur_w["new_end"] = round(cur_w["new_end"] + extra, 3)
                        cursor += extra
                        got += extra
                # 5) 终极兜底：一把刀都没领到（池耗尽+借镜失败）→ 取全片离事件最近的未用镜头
                if got < need - 0.3:
                    e_mid = (e0 + e1) / 2
                    free = [sh for sh in shots
                            if sh["i"] not in used_shots
                            and not (bi == 0 and (sh["a"] + sh["b"]) / 2 < head_guard)]
                    free.sort(key=lambda z: min(abs(z["a"] - e_mid), abs(z["b"] - e_mid)))
                    for sh in free[:3]:
                        d_want = min(_clamp_shot(sh), need - got + 0.2, 4.0,
                                     bounds_last - sh["a"] - LEAD - 0.02)
                        if d_want < 0.45:
                            continue
                        used_shots.add(sh["i"])
                        emit(s["i"], sh["a"] + LEAD, d_want, s["text"][:26])
                        picks.append(sh["i"])
                        got += d_want
                        if got >= need:
                            break
            i2 = j2 + 1

        beat_new[b] = (beat_t0, cursor)
        beat_win[b] = (beat_i0, len(new_wins))

    # 句子时间轴：按块实际跨度缩放 gap（与 relayout_smart 的句级精确铺法兼容）
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
                          "mode": "shot-aligned-narration-cut"}}
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
    print(f"[整镜头对齐] {r['cuts']} 刀 / {r['duration']}s / {r['beats']} 块", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
