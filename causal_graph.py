# -*- coding: utf-8 -*-
"""因果图谱层：给事件表补上「起因 / 结果 / 叙事功能」，替代扁平事件列表。

输入 story_plan.events + 会话内读图产出的镜头证据（P0-2 回喂），输出：
  {"事件号": {"cause": [事件号...], "result": [事件号...], "func": "钩子|铺垫|升级|爆发|反转|收束"}}
缓存 story_plan.json 的 causal 字段。供 cutfirst_plan 按叙事权重分配时长、
narrate_cut 在写作时知道"这一句该解释哪个因果连接"。

三种模式：
  python causal_graph.py --project <pj>                 # 默认：有缓存即用；否则调 API
  python causal_graph.py --project <pj> --export-prompt # 零 API：导出完整 prompt 供会话内分析
  python causal_graph.py --project <pj> --import <json> # 导入会话内产出的图谱（会备份）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import narration as NR  # noqa: E402
import evidence_feed as EF  # noqa: E402

CAUSAL_SYS = ("你是动漫剧情结构分析师。给你一集动画的事件表（按时间顺序，含秒区间与概要）"
              "和每个事件区间里的镜头证据（画面描述与台词）。任务：梳理事件之间的因果关系与叙事功能，"
              "输出结构化图谱。")

CAUSAL_Q = """【事件表】"编号 [t0-t1s] who: 概要 (当前类型)"
{events}

【镜头证据（会话内读图产出 · 按事件区间聚合）】
{evidence}

只输出 JSON，不要解释：
{{"编号": {{"cause": [起因事件编号...], "result": [结果事件编号...], "func": "功能标签"}}}}

功能标签只能取：钩子 / 铺垫 / 升级 / 爆发 / 反转 / 收束
判断依据：
- cause/result 必须是真实的剧情因果（谁导致谁、什么引出什么），不是时间相邻；没有就给 []。
- func 按该事件在叙事里的作用：开场悬念=钩子，背景/交代=铺垫，矛盾加剧=升级，
  高潮动作/爽点=爆发，意外转折=反转，段落结束/情绪落地=收束。
- 判定 func 时以镜头证据里的动作强度、情绪与台词为准；事件概要一行可能漏掉伏笔镜头
  或情绪转折点，不要只凭概要文字和 type 字段。"""
FUNC_SET = {"钩子", "铺垫", "升级", "爆发", "反转", "收束"}


def build_prompt(proj_dir: Path, evs: list[dict]) -> tuple[str, dict]:
    lines = "\n".join(f"[{i}] {float(e.get('t0', 0)):.0f}-{float(e.get('t1', 0)):.0f}s "
                      f"{e.get('who', '')}: {e.get('event', '')} ({e.get('type', '')})"
                      for i, e in enumerate(evs) if (e.get("type") or "") != "收录")
    ev_txt, st = EF.event_brief(proj_dir, evs)
    if not ev_txt:
        ev_txt = "（本工程没有镜头证据包，按事件表判断）"
    return CAUSAL_Q.replace("{events}", lines).replace("{evidence}", ev_txt), st


def parse_causal(raw: dict, n: int) -> dict[str, dict]:
    """把模型/会话内产出的原始 JSON 规范化，供 API 与 --import 共用。"""
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
    return causal


def commit(proj_dir: Path, plan: dict, causal: dict) -> None:
    bak = proj_dir / "json" / "story_plan.json.bak_causal"
    if not bak.exists():
        bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    plan["causal"] = causal
    import story_parse as SP
    SP.save_plan(proj_dir, plan)
    n_func = sum(1 for v in causal.values() if v.get("func") in FUNC_SET)
    n_link = sum(1 for v in causal.values() if v.get("cause") or v.get("result"))
    print(f"[因果图谱] {len(causal)} 事件 · 功能标签 {n_func} · 因果连接 {n_link} 条"
          f" → story_plan.causal（备份 {bak.name}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--export-prompt", action="store_true", help="零 API：导出完整 prompt")
    ap.add_argument("--import", dest="import_file", default=None, help="导入会话内产出的图谱 JSON")
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent

    import story_parse as SP
    plan = SP.load_plan(proj_dir)
    evs = plan.get("events") or []
    live = [e for e in evs if (e.get("type") or "") != "收录"]
    if not live:
        print("[因果图谱] 事件表为空，先跑剧情解析")
        return 2

    q, st = build_prompt(proj_dir, evs)
    print(f"[因果图谱] 镜头证据 {st['lines']} 行 / {st['chars']} 字", flush=True)

    if a.export_prompt:
        out = proj_dir / "json" / "session_prompts"
        out.mkdir(parents=True, exist_ok=True)
        f = out / "causal_prompt.txt"
        f.write_text(CAUSAL_SYS + "\n\n" + q, encoding="utf-8")
        print(f"[因果图谱·会话内] → {f}；产出 JSON 后用 --import 回写")
        return 0

    if a.import_file:
        p = Path(a.import_file).expanduser().resolve()
        txt = p.read_text(encoding="utf-8").strip()
        m = txt.find("{")
        raw = json.loads(txt[m:txt.rfind("}") + 1]) if m >= 0 else {}
        commit(proj_dir, plan, parse_causal(raw, len(evs)))
        return 0

    if plan.get("causal") and not a.force:
        print(f"[因果图谱] 已有缓存 {len(plan['causal'])} 条，--force 重生成")
        return 0
    r = NR.llm_chat(CAUSAL_SYS, q, temperature=0, timeout=600)
    txt = (r.get("text") or "").strip()
    m = txt.find("{")
    raw = json.loads(txt[m:txt.rfind("}") + 1]) if m >= 0 else {}
    commit(proj_dir, plan, parse_causal(raw, len(evs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
