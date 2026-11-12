# -*- coding: utf-8 -*-
"""L0·剪辑规划领域纯函数：钩子连续爽点选段 + 叙事权重。

所属层：L0 基础层（领域纯函数） ｜ 副作用：无
调用者：cutfirst_plan.py（阶段①「画面先定」）、tests/

=============================================================================
钩子选段的硬约束（用户 2026-11-12 明确要求，属于剪辑规划、不属于旁白撰写）
=============================================================================
钩子必须是**源片里连续、未经裁切重排的一整段爽点**：

* 不全球散装检索「最爽的 10 个镜头」再按设定/动作重排（旧实现，已废弃）；
* 不跳源片时间线（相邻窗口 src 首尾相接，缝隙必须为 0）；
* 这段画面是「提前复用」，正片里要原样再演一遍，所以钩子镜头不进 ``used``；
* ``window_table.md`` 的钩子块只列字数槽位，不列画面/台词/因果 —— 那些是
  剪辑规划的决策，不是旁白撰写素材。

实现：在镜头序列上滑动一个 budget 长度的窗口，取**单位时间爽点密度**最高的
一段；``override=[a,b]`` 时直接采用规划阶段锁定的区间（``project.json`` 的
``recap.hook_clip``）。
"""
from __future__ import annotations

LEAD = 0.10
HOOK_SHOT_MIN, HOOK_SHOT_MAX = 0.6, 1.2
HOOK_SHOTS_N = 10
HOOK_USED_RATIO = 1.0
CLIFFHANGER_SEC = 12.0

# 叙事权重：因果图谱 func 优先，缺省用事件 type
FUNC_W = {"钩子": 3.0, "爆发": 3.0, "反转": 2.6, "升级": 1.8,
          "收束": 1.3, "铺垫": 1.0}
TYPE_W = {"钩子": 3.0, "反转": 2.6, "爽点": 2.6, "冲突": 1.8,
          "收尾": 1.3, "铺垫": 1.0}

HOOK_KW_A = ("强化", "紫光", "点数", "数字", "球", "一万", "一万三千")
HOOK_KW_B = ("弹飞", "轰", "挥剑", "拳", "砸", "坑", "惊", "惨叫")


def hook_score(sh: dict, e: dict) -> float:
    """单个镜头的爽点密度打分（depth/desc/obj 关键词 + 事件类型）。"""
    txt = f"{sh.get('depth', '')}{sh.get('desc', '')}{sh.get('obj', '')}"
    s = 0.0
    if any(k in txt for k in HOOK_KW_A):
        s += 2.0
    if any(k in txt for k in HOOK_KW_B):
        s += 3.0
    if e.get("type") == "爽点":
        s += 1.5
    if sh.get("b", 0) - sh.get("a", 0) > 6:   # 超长镜不适合快切钩子
        s -= 2.0
    return s


def pick_hook_clip(shots: list[dict], events: list[dict], budget: float,
                   override: list | None = None,
                   *, lead: float = LEAD) -> tuple[float, float, int | None]:
    """锁定开场钩子 = 源片连续的一整段爽点。返回 ``(起点, 终点, 主事件)``。"""
    if override and len(override) == 2 and float(override[1]) > float(override[0]):
        a, z = float(override[0]), float(override[1])
        gi = next((i for i, e in enumerate(events)
                   if float(e.get("t0", 0)) <= a < float(e.get("t1", 0))), None)
        return a, z, gi

    best = None
    last_b = float(shots[-1]["b"]) if shots else 0.0
    for k, sh in enumerate(shots):
        a = float(sh["a"]) + lead
        z = a + budget
        if z > last_b:
            break
        score, votes = 0.0, {}
        for s2 in shots[k:]:
            if float(s2["a"]) >= z:
                break
            lo, hi = max(a, float(s2["a"])), min(z, float(s2["b"]))
            if hi <= lo:
                continue
            gi = next((i for i, e in enumerate(events)
                       if float(e.get("t0", 0)) <= s2["a"] < float(e.get("t1", 0))),
                      None)
            e = events[gi] if gi is not None else {}
            w = hook_score(s2, e)
            score += w * (hi - lo)
            if w >= 3.0 and gi is not None:
                votes[gi] = votes.get(gi, 0.0) + (hi - lo)
        cand = (score / max(budget, 0.1), a, z,
                max(votes, key=votes.get) if votes else None)
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        return 0.0, 0.0, None
    return best[1], best[2], best[3]


def weight(e: dict, causal: dict, gi: int) -> float:
    """事件叙事权重：func > type，有因果连接 ×1.15。"""
    c = causal.get(str(gi)) or {}
    w = FUNC_W.get(c.get("func", ""))
    if w is None:
        w = TYPE_W.get(e.get("type", ""), 1.0)
    if c.get("cause") or c.get("result"):
        w *= 1.15
    return w


def reason(e: dict, causal: dict, gi: int) -> str:
    """窗口保留理由（clip_plan 契约：旁白写作与人工抽查的依据）。"""
    c = causal.get(str(gi)) or {}
    func = c.get("func") or e.get("type", "")
    ev = str(e.get("event", ""))[:30]
    link = ""
    if c.get("cause"):
        link = f"（承接事件{c['cause'][0]}）"
    return f"{func}节点{link}：{ev}"


def is_continuous(wins: list[dict], *, tol: float = 0.05) -> tuple[bool, list[float]]:
    """校验一组窗口的 src 是否首尾相接（钩子连续段的验收规则）。

    返回 ``(是否连续, 相邻缝隙列表)``。缝隙全 0 才叫「源片未裁切」。
    """
    gaps = [round(float(wins[i + 1]["src_start"]) - float(wins[i]["src_end"]), 4)
            for i in range(len(wins) - 1)]
    return all(abs(g) <= tol for g in gaps), gaps
