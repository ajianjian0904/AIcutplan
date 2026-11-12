# -*- coding: utf-8 -*-
"""L0·镜头证据聚合：把高密度下游证据压缩成上游 prompt 能塞下的文本。

所属层：L0 基础层（领域纯函数） ｜ 副作用：仅读 shots_desc.json
调用者：evidence_feed.py（CLI / 库）、story_parse.parse_segment()、causal_graph.build_prompt()

背景（P0-2）：cut-first 下游已有 387 镜 desc/depth/facts/dlg + 592 个 2s 窗，
但上游 story_parse 仍是「200s 窗口 + 12 张均匀帧」、causal_graph 只看一行
事件概要，形成「高密度证据在下游、低密度证据在上游」的倒挂。本模块零新增
抽帧、零新增读图、零模型调用，只把已有证据按时间窗聚合成紧凑文本。

采样优先级（预算不足时保留谁）：台词 2.0 > 帧事实 0.8 > 情绪/转折 0.6×命中数。
情绪线索最影响「铺垫 vs 反转」判定，所以关键词表是独立的。
"""
from __future__ import annotations

from pathlib import Path

from .io import load_json

EMO_KW = ("惊", "怒", "笑", "哭", "沉默", "紧张", "反转", "转折",
          "爆发", "收束", "伏笔", "震惊", "犹豫", "对峙", "惨叫", "紫光", "数字")
MAX_LINE = 96


def load_shots(proj_dir: str | Path) -> list[dict]:
    """读镜头证据包；不存在或损坏时返回空列表（保持向下兼容，不抛异常）。"""
    p = Path(proj_dir) / "json" / "shots_desc.json"
    rows = load_json(p, default={})
    if not isinstance(rows, dict):
        return []
    return [r for r in (rows.get("rows") or []) if r.get("desc")]


def score_shot(r: dict) -> float:
    """证据价值打分：台词 > 情绪/转折 > 帧级事实。"""
    s = 0.0
    if r.get("dlg"):
        s += 2.0
    t = str(r.get("depth", "")) + str(r.get("desc", ""))
    s += sum(0.6 for k in EMO_KW if k in t)
    if r.get("facts"):
        s += 0.8
    return s


def format_shot_line(r: dict, *, max_chars: int = MAX_LINE) -> str:
    """单镜一行：秒段 + desc + depth + 台词 + 帧事实，超长截断。

    ``facts`` 可能是 ``{"t": 6.5, "fact": "..."}`` 或纯字符串，两种都兼容。
    """
    a, b = float(r.get("a", 0)), float(r.get("b", 0))
    s = f"{a:.0f}-{b:.0f}s {str(r.get('desc', ''))[:34]}"
    if r.get("depth"):
        s += f" ｜{str(r['depth'])[:26]}"
    if r.get("dlg"):
        s += f" ｜台词:{str(r['dlg'])[:26]}"
    if r.get("facts"):
        facts = []
        for x in r["facts"][:2]:
            facts.append(str(x.get("fact", ""))[:14]
                         if isinstance(x, dict) else str(x)[:14])
        s += " ｜帧:" + "/".join(f for f in facts if f)
    return s[:max_chars]


def compact(rows: list[dict], max_rows: int, max_chars: int) -> tuple[str, int]:
    """按价值取 top-N，再按时间排序，拼成不超过 max_chars 的文本。

    返回 ``(文本, 实际保留行数)``。顺序必须先按时间还原，否则 LLM 会把
    「结果」读成「起因」——这正是因果图谱判错的主要来源。
    """
    picked = sorted(rows, key=lambda r: -score_shot(r))[:max_rows]
    picked.sort(key=lambda r: float(r.get("a", 0)))
    out, total = [], 0
    for r in picked:
        ln = format_shot_line(r)
        if total + len(ln) + 1 > max_chars:
            break
        out.append(ln)
        total += len(ln) + 1
    return "\n".join(out), len(out)


def in_window(rows: list[dict], a: float, b: float) -> list[dict]:
    """取与 [a,b] 有交集且带 desc 的镜头（零读图）。"""
    return [r for r in rows if r.get("desc")
            and float(r.get("b", 0)) > a and float(r.get("a", 0)) < b]
