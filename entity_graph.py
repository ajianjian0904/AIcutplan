# -*- coding: utf-8 -*-
"""实体图谱层：人名归一 + 事件表错字修正（Phase B 工具化）。

LLM 从事件表 + 台词转写中识别同一角色的不同写法（雷恩/男主/レイン）、
错别字（SQ→S级）和明显的事实错误，输出修正清单；脚本做确定性应用并缓存
story_plan.json 的 entities 字段。换新集时自动跑一遍，不再人工修。

用法：python entity_graph.py --project <pj> [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import narration as NR  # noqa: E402

ENT_SYS = ("你是动漫剧情数据质检员。给你一集动画的事件表（可能有同一角色的多种写法、"
           "错别字、ASR 误听导致的错词）。任务：输出规范化修正清单。")

ENT_Q = """【事件表】"编号 | who | 事件描述"
{events}

只输出 JSON，不要解释：
{{
  "aliases": {{"标准名": ["别名1", "别名2"]}},
  "fixes": [{{"i": 事件编号, "field": "who或event", "new": "修正后的完整文本", "why": "一句话理由"}}]
}}

规则：
1. aliases：把同一角色的所有写法归到一个标准名下（以出场最多、正式的称呼为准）。
2. fixes：只修明显的错别字/误听/写法不一（如 SQ→S级、混用的人名），不要改写剧情语义。
3. 拿不准的不要修。
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent

    import story_parse as SP
    plan = SP.load_plan(proj_dir)
    if plan.get("entities") and not a.force:
        print(f"[实体图谱] 已有缓存 {len(plan['entities'].get('aliases', {}))} 个角色，--force 重生成")
        return 0
    evs = plan.get("events") or []
    lines = "\n".join(f"[{i}] {e.get('who', '')} | {e.get('event', '')}"
                      for i, e in enumerate(evs) if (e.get("type") or "") != "收录")
    r = NR.llm_chat(ENT_SYS, ENT_Q.replace("{events}", lines), temperature=0, timeout=600)
    txt = (r.get("text") or "").strip()
    m = txt.find("{")
    raw = json.loads(txt[m:txt.rfind("}") + 1]) if m >= 0 else {}

    aliases = {str(k): [str(x) for x in v] for k, v in (raw.get("aliases") or {}).items()
               if isinstance(v, list)}
    fixes = [f for f in (raw.get("fixes") or [])
             if isinstance(f, dict) and isinstance(f.get("i"), int)
             and 0 <= f["i"] < len(evs) and f.get("field") in ("who", "event") and f.get("new")]

    applied = 0
    for f in fixes:
        e = evs[f["i"]]
        old = str(e.get(f["field"], ""))
        new = str(f["new"])
        if new != old:
            e[f["field"]] = new
            applied += 1
    # aliases 反向应用到 who
    canon = {alias: std for std, lst in aliases.items() for alias in lst}
    for e in evs:
        w = e.get("who", "")
        for alias, std in canon.items():
            if alias in w and std not in w:
                e["who"] = w.replace(alias, std)
                applied += 1
                break

    plan["entities"] = {"aliases": aliases,
                        "fixes_applied": applied,
                        "raw_fixes": fixes}
    SP.save_plan(proj_dir, plan)
    print(f"[实体图谱] 角色 {len(aliases)} 个 · 修正 {applied} 处 → story_plan.entities")
    return 0


if __name__ == "__main__":
    sys.exit(main())
