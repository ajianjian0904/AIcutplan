# -*- coding: utf-8 -*-
"""因果图谱层：给事件表补上「起因 / 结果 / 叙事功能」，替代扁平事件列表。

输入 story_plan.events（DeepSeek 剧情解析产物），一次文本调用输出：
  {"事件号": {"cause": [事件号...], "result": [事件号...], "func": "钩子|铺垫|升级|爆发|反转|收束"}}
缓存 story_plan.json 的 causal 字段。供 cutfirst_plan 按叙事权重分配时长、
narrate_cut 在写作时知道"这一句该解释哪个因果连接"。

用法：python causal_graph.py --project <pj> [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import narration as NR  # noqa: E402

CAUSAL_SYS = ("你是动漫剧情结构分析师。给你一集动画的事件表（按时间顺序，含秒区间与概要）。"
              "任务：梳理事件之间的因果关系与叙事功能，输出结构化图谱。")

CAUSAL_Q = """【事件表】"编号 [t0-t1s] who: 概要 (当前类型)"
{events}

只输出 JSON，不要解释：
{{"编号": {{"cause": [起因事件编号...], "result": [结果事件编号...], "func": "功能标签"}}}}

功能标签只能取：钩子 / 铺垫 / 升级 / 爆发 / 反转 / 收束
判断依据：
- cause/result 必须是真实的剧情因果（谁导致谁、什么引出什么），不是时间相邻；没有就给 []。
- func 按该事件在叙事里的作用：开场悬念=钩子，背景/交代=铺垫，矛盾加剧=升级，
  高潮动作/爽点=爆发，意外转折=反转，段落结束/情绪落地=收束。"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent

    import story_parse as SP
    plan = SP.load_plan(proj_dir)
    if plan.get("causal") and not a.force:
        print(f"[因果图谱] 已有缓存 {len(plan['causal'])} 条，--force 重生成")
        return 0
    evs = plan.get("events") or []
    live = [e for e in evs if (e.get("type") or "") != "收录"]
    if not live:
        print("[因果图谱] 事件表为空，先跑剧情解析")
        return 2

    lines = "\n".join(f"[{i}] {float(e.get('t0', 0)):.0f}-{float(e.get('t1', 0)):.0f}s "
                      f"{e.get('who', '')}: {e.get('event', '')} ({e.get('type', '')})"
                      for i, e in enumerate(evs) if (e.get("type") or "") != "收录")
    q = CAUSAL_Q.replace("{events}", lines)
    r = NR.llm_chat(CAUSAL_SYS, q, temperature=0, timeout=600)
    txt = (r.get("text") or "").strip()
    m = txt.find("{")
    raw = json.loads(txt[m:txt.rfind("}") + 1]) if m >= 0 else {}

    n = len(evs)
    causal: dict[str, dict] = {}
    for k, v in raw.items():
        try:
            i = int(k)
        except (ValueError, TypeError):
            continue
        if not (0 <= i < n) or not isinstance(v, dict):
            continue
        cause = [int(x) for x in (v.get("cause") or []) if isinstance(x, (int, float))
                 and 0 <= int(x) < n and int(x) != i]
        result = [int(x) for x in (v.get("result") or []) if isinstance(x, (int, float))
                  and 0 <= int(x) < n and int(x) != i]
        func = str(v.get("func", "")).strip()
        causal[str(i)] = {"cause": cause, "result": result, "func": func}

    plan["causal"] = causal
    SP.save_plan(proj_dir, plan)
    n_func = sum(1 for v in causal.values() if v.get("func"))
    n_link = sum(1 for v in causal.values() if v.get("cause") or v.get("result"))
    print(f"[因果图谱] {len(causal)} 事件 · 功能标签 {n_func} · 因果连接 {n_link} 条 → story_plan.causal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
