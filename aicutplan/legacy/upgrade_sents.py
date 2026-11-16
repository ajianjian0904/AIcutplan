# -*- coding: utf-8 -*-
# [L1 旧链迁移] 职责：历史句子稿升级为 src-v2（补每句源片区间） ｜ 调用者：会话/CLI ｜ 产物：新稿 JSON
"""把没有 ``src`` 的历史句子稿升级成 src-v2 契约稿。

背景：v0.9.5 起 ``sents.json`` 每句必须声明 ``src: [源片起, 源片止]``，落盘前与绑定窗口
逐一比对（防「按窗口序号均匀分配旁白」）。历史工程（EP01 的 my_sentences_v35.json）
是 ``{ws, we, text}``，直接喂 ``narrate`` 会被拒（退出码 5），只能 ``--legacy-sents`` 豁免。

本模块按 ``ws/we`` 从 ``cutplan.windows`` 取 ``src_start → src_end`` 补上 ``src``，
**不改 text、不改 ws/we、不改 cutplan**；升级后先跑一遍契约校验（只读），通过才落盘。

用法::

    python -m aicutplan.legacy.upgrade_sents --project <pj> \\
        --sentences my_sentences_v35.json --out my_sentences_v36.json --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aicutplan.contracts import check_sentences_contract


def _load_items(data) -> list[dict]:
    """接受裸数组或 ``{"items": [...]}`` 两种历史写法。"""
    if isinstance(data, list):
        return [dict(x) for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [dict(x) for x in data["items"] if isinstance(x, dict)]
    raise ValueError("句子稿格式不认识：需要数组或 {items:[...]}")


def upgrade(sents: list[dict], wins: list[dict]) -> tuple[list[dict], list[str]]:
    """按 ws/we 补 src；返回 ``(新稿, 问题清单)``。不修改入参、不写文件。"""
    out: list[dict] = []
    problems: list[str] = []
    for k, s in enumerate(sents):
        item = dict(s)
        try:
            ws, we = int(item["ws"]), int(item["we"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"句{k} 缺少合法 ws/we：{item!r}")
            out.append(item)
            continue
        if not (0 <= ws < len(wins)) or not (0 <= we < len(wins)):
            problems.append(f"句{k} ws/we 越界：{ws}-{we}（共 {len(wins)} 窗）")
            out.append(item)
            continue
        if we < ws:
            problems.append(f"句{k} we < ws：{ws}-{we}")
            out.append(item)
            continue
        if item.get("src"):
            # 已经有 src 就保留（幂等），但仍然校验一致性交给契约层
            out.append(item)
            continue
        item["src"] = [round(float(wins[ws]["src_start"]), 3),
                       round(float(wins[we]["src_end"]), 3)]
        out.append(item)
    return out, problems


def verify(sents: list[dict], wins: list[dict], cfg: dict) -> tuple[list[str], list[str], dict]:
    """只读契约校验（不落盘、不改工程）。"""
    return check_sentences_contract(sents, wins, cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="upgrade-sents", description="历史句子稿升级为 src-v2")
    ap.add_argument("--project", required=True, help="project.json 或其所在目录")
    ap.add_argument("--sentences", required=True, help="旧句子稿 JSON")
    ap.add_argument("--out", required=True, help="输出文件名或绝对路径（相对工程目录）")
    ap.add_argument("--apply", action="store_true", help="校验通过后真正写盘（默认只报告）")
    ap.add_argument("--force", action="store_true", help="输出文件已存在时覆盖")
    a = ap.parse_args(argv)

    pj = Path(a.project).expanduser().resolve()
    proj = pj if pj.is_dir() else pj.parent
    proj_file = proj / "project.json"
    if not proj_file.exists():
        print(f"[upgrade] 找不到 project.json：{proj_file}", file=sys.stderr)
        return 2

    sent_file = Path(a.sentences)
    if not sent_file.is_absolute():
        sent_file = proj / sent_file
    if not sent_file.exists():
        print(f"[upgrade] 找不到句子稿：{sent_file}", file=sys.stderr)
        return 2

    wins = (json.loads((proj / "json" / "cutplan.json").read_text(encoding="utf-8"))
            .get("windows") or [])
    cfg = json.loads(proj_file.read_text(encoding="utf-8"))
    old = _load_items(json.loads(sent_file.read_text(encoding="utf-8")))

    new, problems = upgrade(old, wins)
    if problems:
        print("[upgrade] 稿子本身有问题，未写盘：")
        for p in problems:
            print("  ! " + p)
        return 1

    errors, warnings, stats = verify(new, wins, cfg)
    tagged = sum(1 for s in new if s.get("src"))
    print(f"[upgrade] {len(new)} 句 / 补 src {tagged} 句 / 契约 {stats.get('contract')} "
          f"/ 校验 {stats.get('src_checked')} 句 / 钩子句 {stats.get('hook_sents')}")
    for w in warnings:
        print("  · " + w)
    if errors:
        print("[upgrade] 契约未通过，最终仍以 narrate 落盘校验为准：")
        for e in errors[:10]:
            print("  ! " + e)
        if len(errors) > 10:
            print(f"  … 另有 {len(errors) - 10} 条")

    out = Path(a.out)
    if not out.is_absolute():
        out = proj / out
    if out.exists() and not a.force:
        print(f"[upgrade] 已存在，未覆盖：{out}（确认要换加 --force）")
        return 0 if not errors else 1
    if not a.apply:
        print(f"[upgrade] 预览模式，未写盘；确认后加 --apply 写入 {out}")
        return 0 if not errors else 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[upgrade] 已写入 {out}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
