#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""逐句三轨校对（纯数据层，不写任何工程文件）

目标：把「旁白 / 画面 / 人物台词」统一投影到同一条【成片秒】横轴上，
让人一眼看出这句话到底配了哪些画面、那些时刻人物在说什么。

设计约束（沿用 SKILL.md 四集实测结论）：
  * 成片时间轴由旁白 TTS 实测时长决定，锁死；这里只做「看得见的对齐 + 只读提示」。
  * 不做匹配度打分、不做自动推荐镜头、不做语义一一对应（52% 是情境刀）。
  * 换镜头复用 sentence_studio.compute_anchor / server 的 /api/sent/anchor，
    本模块只算「候选长什么样」，不产生任何会话状态或磁盘写入。

时间基准：
  * 旁白 / 画面刀的 new_start、new_end = 成片秒（锁死）
  * 画面刀的 src_start、src_end 与人物台词 t0/t1 = 源片秒
  * 一刀内 src 段长 === new 段长（实测最大差 0.0001s），故源片→成片是线性平移：
    成片秒 = new_start + (源片秒 - src_start)
"""
from __future__ import annotations

import bisect
import re

import sentence_studio as SS

# 信息刀候选：旁白里点名了数字、引文或次序，值得人工看一眼画面里在不在。
# 只读提示，绝不据此自动换图（v6 的教训）。
INFO_RE = re.compile(
    r"[0-9０-９]"
    r"|[一二三四五六七八九十百千万两]{1,4}\s*(?:个|人|次|招|集|层|年|岁|米|秒|分钟|天|只|条|张|颗|刀|剑|拳|步|成|倍|块|名|位|枚)"
    r"|第[一二三四五六七八九十0-9]{1,3}"
    r"|[「」『』“”]"
)
GAP_LIMIT = 10.0    # 相邻刀在源片上跳跃超过 10s：可能是跳场，值得人工扫一眼

# 「只看需复核」的筛选集合。ost=1 在本机 404/608 镜、shared 在 164/164 句都命中，
# 属于常态信息，只作展示不进筛选，否则筛选等于没筛。
WATCH = ("info", "blank", "gap", "conflict", "edited")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _win_owner(proj, smap) -> dict:
    """{刀号: [句id, ...]}，用于标出跨句共享的边界刀。"""
    owner: dict[int, list] = {}
    for sid, s in smap.items():
        for wi in s["wins"]:
            owner.setdefault(wi, []).append(sid)
    return owner


def _shot_at(proj, t: float):
    if not proj.shots:
        return None
    i = max(0, min(len(proj.shots) - 1, bisect.bisect_right(proj.shot_starts, t) - 1))
    return proj.shots[i]


def _ost_hits(proj, a: float, z: float) -> list:
    """与源片区间 [a,z] 重叠的原声镜头（只读保护提示）。"""
    if not proj.shots:
        return []
    i = max(0, bisect.bisect_right(proj.shot_starts, a) - 2)
    out = []
    for s in proj.shots[i:i + 6]:
        if s["end"] >= a and s["start"] <= z and s.get("ost"):
            out.append(s["idx"])
    return out


def _flags(text: str, sid: int, wins: list, owner: dict,
           anchors: dict, ost: list, *, gap: float = 0.0,
           blank: bool = False, conflict: bool = False) -> list:
    f = []
    if INFO_RE.search(text or ""):
        f.append({"k": "info", "watch": True,
                  "text": "信息刀候选 · 旁白点名了数字/引文，人工看一眼画面"})
    if blank:
        f.append({"k": "blank", "watch": True,
                  "text": "这句覆盖的画面没有任何人物台词（纯动作/空镜）"})
    if gap > GAP_LIMIT:
        f.append({"k": "gap", "watch": True,
                  "text": f"句内相邻刀在源片上跳了 {gap:.1f}s，留意有没有跳场"})
    if conflict:
        f.append({"k": "conflict", "watch": True,
                  "text": "共享刀被相邻句的句级锚点覆盖，写回以后写的那句为准"})
    if ost:
        f.append({"k": "ost", "watch": False,
                  "text": f"镜头 {','.join(map(str, ost[:4]))} 标为有原声（本片 404/608 镜均为 1，属常态）"})
    shared = [wi for wi in wins if len(owner.get(wi, [])) > 1]
    if shared:
        others = sorted({x for wi in shared for x in owner.get(wi, []) if x != sid})
        f.append({"k": "shared", "watch": False,
                  "text": f"与句 {','.join(map(str, others[:3]))} 共享边界刀 {','.join(map(str, shared))}"})
    if sid in anchors:
        f.append({"k": "edited", "watch": True,
                  "text": "本句已设句级锚点（写回时优先于块级平移）"})
    return f


# --------------------------------------------------------------------------
# 台词投影：源片秒 → 成片秒
# --------------------------------------------------------------------------
def _dialogue_track(paras: list, cuts: list) -> list:
    """把落在每一刀源区间内的人物台词，投影到该刀的成片区间上。

    同一条台词可能横跨多刀（刀在源片上是跳着挑的），会被拆成几段；
    相邻刀里的同一条台词且时间连续时合并，避免视觉上重复。
    """
    segs = []
    for c in cuts:
        a, z = float(c["src"][0]), float(c["src"][1])
        na, nz = float(c["new"][0]), float(c["new"][1])
        dur = z - a
        if dur <= 0:
            continue
        hits = [x for x in paras if x["t1"] > a and x["t0"] < z]
        for x in hits:
            s0 = na + max(0.0, x["t0"] - a)
            s1 = na + min(dur, x["t1"] - a)
            if s1 - s0 < 0.06:
                continue
            segs.append({
                "new": [round(s0, 3), round(s1, 3)],
                "text": x["text"], "orig": x["orig"],
                "t0": x["t0"], "t1": x["t1"],
                "wins": [c["win"]], "state": "current", "virtual": False,
            })
        if not hits:
            # 这一刀没有任何台词覆盖：给出「刚说完 / 马上要说」的近邻作参考
            d = SS.dialogue_at(paras, (a + z) / 2)
            if d["at"]:
                x = d["at"]
                segs.append({
                    "new": [na, nz], "text": x["text"], "orig": x["orig"],
                    "t0": x["t0"], "t1": x["t1"],
                    "wins": [c["win"]], "state": d["state"], "virtual": True,
                })
    segs.sort(key=lambda x: (x["new"][0], x["new"][1]))
    merged: list = []
    for s in segs:
        if (merged and not s["virtual"] and not merged[-1]["virtual"]
                and merged[-1]["text"] == s["text"]
                and s["new"][0] <= merged[-1]["new"][1] + 0.06):
            merged[-1]["new"][1] = max(merged[-1]["new"][1], s["new"][1])
            merged[-1]["wins"] += s["wins"]
        else:
            merged.append(s)
    return merged


# --------------------------------------------------------------------------
# 列表（轻量，164 句一次给前端做导航）
# --------------------------------------------------------------------------
def rows(proj, deltas: dict, anchors: dict, confirmed: set,
         *, beat=None, only: str = "all", q: str = "",
         limit: int = 0, offset: int = 0) -> dict:
    smap = SS.sent_map(proj)
    owner = _win_owner(proj, smap)
    paras, zh_ready = SS.dialogue_load(proj)
    merged = SS.merge_sources(proj, deltas, anchors)
    beats: dict[int, int] = {}

    all_rows = []
    for sid in sorted(smap):
        s = smap[sid]
        b = s["beat"]
        beats[b] = beats.get(b, 0) + 1
        w0, w1 = proj.windows[s["wins"][0]], proj.windows[s["wins"][-1]]
        A, Z = float(w0["new_start"]), float(w1["new_end"])
        wi = s["wins"][0]
        a, z = merged.get(wi, [float(w0["src_start"]), float(w0["src_end"])])
        l = SS.dialogue_at(paras, (a + z) / 2)["at"]
        segs, ost, has_line = [], [], False
        for k in s["wins"]:
            w = proj.windows[k]
            aa, zz = merged.get(k, [float(w["src_start"]), float(w["src_end"])])
            segs.append((aa, zz))
            ost += _ost_hits(proj, aa, zz)
            if SS.dialogue_at(paras, (aa + zz) / 2)["at"]:
                has_line = True
        gap = max([segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1)] or [0.0])
        flags = _flags(s["text"], sid, s["wins"], owner, anchors, sorted(set(ost)),
                       gap=gap, blank=not has_line)
        all_rows.append({
            "id": sid, "beat": b, "text": s["text"],
            # 列表统一显示「成片实际时长」（帧吸附后），与中栏 header 同一口径；
            # narration.dur 是 TTS 估算，和按 1.32s/刀 排出的成片时长会差几十毫秒。
            "t0": s["t0"], "dur": round(Z - A, 3), "new": [round(A, 3), round(Z, 3)],
            "wins": s["wins"], "cuts": len(s["wins"]),
            "confirmed": sid in confirmed, "edited": sid in anchors,
            "delta": round(float(deltas.get(b, 0.0) or 0), 3),
            "line": l["text"] if l else "",
            "flags": [x["k"] for x in flags],
        })

    total = len(all_rows)
    view = all_rows
    if beat is not None and str(beat) != "all":
        view = [x for x in view if x["beat"] == int(beat)]
    if only == "unconfirmed":
        view = [x for x in view if not x["confirmed"]]
    elif only == "edited":
        view = [x for x in view if x["edited"]]
    elif only == "flagged":
        view = [x for x in view if any(k in WATCH for k in x["flags"])]
    if q:
        view = [x for x in view if q in x["text"] or q in x["line"]]
    matched = len(view)
    if limit:
        view = view[offset:offset + limit]

    labels = getattr(proj, "labels", {}) or {}
    return {
        "ok": True, "rows": view, "total": total, "matched": matched,
        "offset": offset, "zh_ready": zh_ready,
        "progress": {
            "total": total,
            "confirmed": sum(1 for x in all_rows if x["confirmed"]),
            "edited": len(anchors),
            "flagged": sum(1 for x in all_rows
                           if any(k in WATCH for k in x["flags"])),
        },
        "beats": [{"id": b, "label": labels.get(b, f"块 {b}"), "count": n}
                  for b, n in sorted(beats.items())],
    }


# --------------------------------------------------------------------------
# 单句三轨详情
# --------------------------------------------------------------------------
def row(proj, sid: int, deltas: dict, anchors: dict, confirmed: set) -> dict:
    smap = SS.sent_map(proj)
    if sid not in smap:
        raise ValueError(f"句 {sid} 不存在")
    v = SS.sent_view(proj, sid, anchors, deltas, smap)
    paras, zh_ready = SS.dialogue_load(proj)
    cuts = v["cuts"]
    A, Z = float(cuts[0]["new"][0]), float(cuts[-1]["new"][1])
    owner = _win_owner(proj, smap)
    ost = []
    for c in cuts:
        ost += _ost_hits(proj, c["src"][0], c["src"][1])
        sh = _shot_at(proj, c["src"][0] + 0.01)
        c["shot"] = sh["idx"] if sh else None
        c["ost"] = bool(sh and sh.get("ost"))
    track = _dialogue_track(paras, cuts)
    conflicts = SS.conflict_report(proj, anchors, smap)
    mine = [x for x in conflicts if x["win"] in smap[sid]["wins"]]
    segs = [(float(c["src"][0]), float(c["src"][1])) for c in cuts]
    gap = max([segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1)] or [0.0])
    blank = not any(not s["virtual"] for s in track)
    b = v["beat"]
    return {
        "ok": True,
        "id": sid, "beat": b,
        "label": (getattr(proj, "labels", {}) or {}).get(b, f"块 {b}"),
        "text": v["text"], "t0": v["t0"], "dur": v["dur"],
        "new": [round(A, 3), round(Z, 3)], "duration": round(Z - A, 3),
        "wins": v["wins"], "cuts": cuts, "track": track,
        "shared": v["shared"], "anchor": v["anchor"],
        "first_shot": v["first_shot"], "suggest_last": v["suggest_last"],
        "confirmed": sid in confirmed, "edited": sid in anchors,
        "delta": round(float(deltas.get(b, 0.0) or 0), 3),
        "flags": _flags(v["text"], sid, v["wins"], owner, anchors, sorted(set(ost)),
                        gap=gap, blank=blank, conflict=bool(mine)),
        "conflicts": mine, "zh_ready": zh_ready,
        "play": {"new_a": round(A, 3), "new_z": round(Z, 3)},
    }


# --------------------------------------------------------------------------
# 候选首镜卡：改前 / 改后并排（只算不写）
# --------------------------------------------------------------------------
def candidates(proj, sid: int, deltas: dict, anchors: dict,
               base_first: int | None = None,
               offsets=(-4, -2, 0, 2, 4)) -> dict:
    """给出若干候选首镜，每个候选把句内每刀的画面 + 此刻人物台词一次列全。

    改前 = 当前会话生效画面（块级 Δ / 既有句级锚点合并后的结果）
    改后 = 若把首镜定在该候选，compute_anchor 会得到的画面（不落盘）
    """
    smap = SS.sent_map(proj)
    if sid not in smap:
        raise ValueError(f"句 {sid} 不存在")
    wins = smap[sid]["wins"]
    n = len(wins)
    v = SS.sent_view(proj, sid, anchors, deltas, smap)
    paras, zh_ready = SS.dialogue_load(proj)
    idxs = [s["idx"] for s in proj.shots]
    if not idxs:
        raise ValueError("工程没有镜头表")

    before = []
    for c in v["cuts"]:
        a, z = c["src"]
        sh = _shot_at(proj, a + 0.01)
        before.append({
            "win": c["win"], "src": c["src"], "new": c["new"],
            "shot": sh["idx"] if sh else None,
            "thumb": f"/api/frame?t={(a + z) / 2:.2f}",
            "line": c["line"], "line_orig": c["line_orig"], "by": c["by"],
        })

    base = int(base_first if base_first is not None else (v["first_shot"] or idxs[0]))
    out = []
    for off in offsets:
        f = base + off
        l = f + n - 1
        if f < idxs[0] or l > idxs[-1]:
            continue
        try:
            anc = SS.compute_anchor(proj, sid, f, l, smap)
        except Exception:
            continue
        items = []
        for wi, sh_idx in zip(wins, anc["picks"]):
            a, z = anc["covers"][wi]
            d = SS.dialogue_at(paras, (a + z) / 2)
            x = d["at"]
            items.append({
                "win": wi, "shot": sh_idx,
                "src": [round(a, 3), round(z, 3)],
                "thumb": f"/api/frame?t={(a + z) / 2:.2f}",
                "line": x["text"] if x else "",
                "line_orig": x["orig"] if x else "",
                "line_state": d["state"],
            })
        out.append({
            "first": f, "last": l, "picks": anc["picks"], "cuts": items,
            "is_current": bool(v["anchor"] and v["anchor"]["first"] == f),
            "is_baseline": off == 0,
        })
    return {
        "ok": True, "sid": sid, "wins": wins,
        "first_shot": v["first_shot"], "suggest_last": v["suggest_last"],
        "before": before, "candidates": out,
        "anchor": v["anchor"], "zh_ready": zh_ready,
    }
