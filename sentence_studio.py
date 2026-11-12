#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [L3 界面层] 职责：句级锚点 + 人物台词联动数据层 ｜ 调用者：server; review ｜ 产物：库
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""句级锚点 + 人物台词联动（纯数据层，不改任何工程文件）

设计依据（SKILL.md 四集实测）：
  * 正确结构 = 「骨架由人工锚点给出，每句镜头块大小 = 该句刀数，换刀只能是句内互换」
  * 人给每句【首镜, 尾镜】是 20% 工作量；被证伪的是「机器按语义自动换图」，不是人工看着改
  * 句级编辑只写本句那几刀的 src_start/src_end，绝不挪其他刀的刀位 → 天然避开级联漂移

时间戳基准：asr_zh.json 的 t0/t1 与 cutplan.src_start 同为源片秒，可直接按时间查台词。
"""
from __future__ import annotations

import bisect
import json
from pathlib import Path

import asr_pipeline as AP

HEAD_IN = 0.10          # 与 server.py 一致：从镜头开头稍后一点进


# --------------------------------------------------------------------------
# 句 ↔ 刀映射
# --------------------------------------------------------------------------
def sent_map(proj) -> dict:
    """用 windows[].sent_ids 反建 {句id: {...}}。

    实测 EP05：164 句里 162 句跨多刀（2 刀 95 / 3 刀 64 / 4 刀 3），
    且相邻句会共享边界刀，因此界面必须显式显示归属与冲突。
    """
    m: dict[int, dict] = {}
    for i, w in enumerate(proj.windows):
        for sid in (w.get("sent_ids") or []):
            sid = int(sid)
            m.setdefault(sid, {"id": sid, "wins": [], "text": "",
                               "t0": 0.0, "dur": 0.0, "beat": None})
            m[sid]["wins"].append(i)
    ns = {int(s["i"]): s for s in proj.sents}
    for sid, v in m.items():
        s = ns.get(sid, {})
        v["text"] = str(s.get("text", ""))
        v["t0"] = round(float(s.get("t0", 0) or 0), 3)
        v["dur"] = round(float(s.get("dur", 0) or 0), 3)
        v["beat"] = proj.sent_beat.get(sid, s.get("beat"))
        v["wins"].sort()
    return m


def sent_view(proj, sid: int, anchors: dict, deltas: dict, smap=None) -> dict:
    """单句详情：刀列表（含当前源时间、缩略图、此刻台词）、共享刀、句级锚点状态。"""
    smap = smap if smap is not None else sent_map(proj)
    if sid not in smap:
        raise ValueError(f"句 {sid} 不存在")
    s = smap[sid]
    b = s["beat"]
    delta = float(deltas.get(b, 0.0)) if b is not None else 0.0
    anchor = anchors.get(sid)
    paras, zh_ready = dialogue_load(proj)
    # 与 merge_sources 完全一致的优先级：句 id 升序，后写的句覆盖共享刀
    owner: dict[int, int] = {}
    for other in sorted(anchors):
        for wi in anchors[other]["covers"]:
            owner[int(wi)] = other

    cuts = []
    for wi in s["wins"]:
        w = proj.windows[wi]
        a0, z0 = float(w["src_start"]), float(w["src_end"])
        by_sent = owner.get(wi)
        if by_sent is not None:
            a, z = anchors[by_sent]["covers"][wi]
            src = "sent" if by_sent == sid else "other_sent"
        else:
            a, z = a0 + delta, z0 + delta
            src = "beat" if abs(delta) > 1e-6 else "orig"
        mid = (a + z) / 2
        line = dialogue_at(paras, mid)["at"]
        cuts.append({
            "win": wi,
            "src": [round(a, 3), round(z, 3)],
            "orig": [round(a0, 3), round(z0, 3)],
            "dur": round(float(w["dur"]), 4),
            "new": [round(float(w["new_start"]), 3), round(float(w["new_end"]), 3)],
            "thumb": f"/api/frame?t={mid:.2f}",
            "by": src, "by_sent": by_sent,
            "line": line["text"] if line else "",
            "line_t": [line["t0"], line["t1"]] if line else None,
            "line_orig": line["orig"] if line else "",
        })
    shared = []
    for other, v in smap.items():
        if other == sid:
            continue
        common = sorted(set(s["wins"]) & set(v["wins"]))
        if common:
            shared.append({"sent": other, "wins": common,
                           "edited": other in anchors})
    first_idx = None
    if cuts:
        t0 = float(cuts[0]["src"][0])
        first_idx = max(0, min(len(proj.shots) - 1,
                              bisect.bisect_right(proj.shot_starts, t0) - 1))
    return {
        "id": sid, "text": s["text"], "beat": b,
        "t0": s["t0"], "dur": s["dur"], "wins": s["wins"],
        "first_shot": proj.shots[first_idx]["idx"] if first_idx is not None else None,
        "suggest_last": (proj.shots[min(first_idx + len(cuts) - 1, len(proj.shots) - 1)]["idx"]
                         if first_idx is not None and cuts else None),
        "cuts": cuts, "shared": shared,
        "anchor": ({"first": anchor["first"], "last": anchor["last"],
                    "picks": anchor["picks"]} if anchor else None),
        "zh_ready": zh_ready,
    }


# --------------------------------------------------------------------------
# 句级锚点：人给【首镜, 尾镜】，句内刀按顺序铺满
# --------------------------------------------------------------------------
def compute_anchor(proj, sid: int, first: int, last: int, smap=None) -> dict:
    """返回该句每一刀的绝对源区间；不写磁盘、不动其他句。"""
    smap = smap if smap is not None else sent_map(proj)
    if sid not in smap:
        raise ValueError(f"句 {sid} 不存在")
    wins = smap[sid]["wins"]
    n = len(wins)
    idxs = [s["idx"] for s in proj.shots]
    if first not in idxs or last not in idxs:
        raise ValueError("镜头编号不存在")
    if last < first:
        raise ValueError("尾镜不能早于首镜")

    cand = [s for s in proj.shots if first <= s["idx"] <= last]
    if len(cand) >= n:
        pick = ([cand[0]] if n == 1 else
                [cand[round(i * (len(cand) - 1) / (n - 1))] for i in range(n)])
    else:
        # 指定块内镜头不够用：从首镜起顺序往后补足，允许越过尾镜
        pos = idxs.index(first)
        pick = proj.shots[pos:pos + n]
        if len(pick) < n:
            raise ValueError(f"首镜之后只剩 {len(pick)} 个镜头，不够本句 {n} 刀")

    covers = {}
    for wi, sh in zip(wins, pick):
        d = float(proj.windows[wi]["dur"])
        a = round(float(sh["start"]) + HEAD_IN, 4)
        z = round(a + d, 4)
        if a < 0 or z > proj.src_dur:
            raise ValueError(f"镜头 {sh['idx']} 取段超出源片（{z:.2f}s > {proj.src_dur:.2f}s）")
        covers[wi] = [a, z]
    return {
        "sent": sid, "first": first, "last": last,
        "wins": wins, "covers": covers,
        "picks": [s["idx"] for s in pick],
        "span": [covers[wins[0]][0], covers[wins[-1]][1]],
    }


def conflict_report(proj, anchors: dict, smap=None) -> list:
    """哪些刀被多句的句级锚点同时指定（后写的句优先，界面需显式提示）。"""
    smap = smap if smap is not None else sent_map(proj)
    owner: dict[int, list] = {}
    for sid in sorted(anchors):
        for wi in anchors[sid]["covers"]:
            owner.setdefault(int(wi), []).append(sid)
    return [{"win": wi, "sents": v} for wi, v in sorted(owner.items()) if len(v) > 1]


def merge_sources(proj, deltas: dict, anchors: dict) -> dict:
    """合并两层改动 → {win_idx: [src_start, src_end]}。句级优先，只返回有变化的刀。"""
    out: dict[int, list] = {}
    for b, d in deltas.items():
        if abs(float(d)) < 1e-6 or b not in proj.beat_win:
            continue
        i0, i1 = proj.beat_win[b]
        for k in range(i0, i1):
            w = proj.windows[k]
            out[k] = [round(float(w["src_start"]) + float(d), 4),
                      round(float(w["src_end"]) + float(d), 4)]
    for sid in sorted(anchors):
        for k, v in anchors[sid]["covers"].items():
            out[int(k)] = [v[0], v[1]]
    changed = {}
    for k, (a, z) in out.items():
        w = proj.windows[k]
        if abs(a - float(w["src_start"])) > 1e-6 or abs(z - float(w["src_end"])) > 1e-6:
            changed[k] = [a, z]
    return changed


# --------------------------------------------------------------------------
# 人物台词（源片秒）
# --------------------------------------------------------------------------
def dialogue_load(proj):
    """优先读中文译文 asr_zh.json；没有则回退原始 ASR 文本。"""
    p = AP.out_path(proj)
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return [_para(x) for x in d.get("paras", [])], True
        except Exception:
            pass
    try:
        src = AP.read_source(proj)
        return [_para(x, fallback=True) for x in src.get("paras", [])], False
    except Exception:
        return [], False


def _para(x: dict, fallback=False) -> dict:
    return {
        "t0": round(float(x.get("t0", 0) or 0), 2),
        "t1": round(float(x.get("t1", 0) or 0), 2),
        "text": str(x.get("text") or x.get("text_orig") or ""),
        "orig": str(x.get("text_orig") or ""),
        "zh": bool(x.get("translated")) and not fallback,
    }


def dialogue_at(paras: list, t: float, before: int = 2, after: int = 4) -> dict:
    starts = [x["t0"] for x in paras]
    i = bisect.bisect_right(starts, t) - 1
    if i < 0:
        i = 0
    lo, hi = max(0, i - before), min(len(paras), i + after + 1)
    at, state = None, "gap"
    if 0 <= i < len(paras):
        x = paras[i]
        if x["t0"] <= t <= x["t1"]:
            at, state = x, "current"
        elif 0 <= t - x["t1"] < 1.5:
            at, state = x, "recent"     # 刚说完：对判断镜头起点仍有参考价值
        elif i + 1 < len(paras) and paras[i + 1]["t0"] - t < 1.0:
            at, state = paras[i + 1], "soon"
    items = []
    for x in paras[lo:hi]:
        y = dict(x)
        y["rel"] = ("current" if y["t0"] <= t <= y["t1"] else
                    "past" if y["t1"] < t else "future")
        items.append(y)
    return {"at": at, "state": state, "items": items, "index": i,
            "zh": any(x["zh"] for x in paras[lo:hi])}


def dialogue_range(paras: list, a: float, z: float, limit: int = 200) -> list:
    out = [x for x in paras if x["t1"] >= a and x["t0"] <= z]
    return out[:limit]
