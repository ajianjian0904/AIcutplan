# -*- coding: utf-8 -*-
# [L1 质检] 职责：两套模型判定的机械比对（求同、列分歧、给 probe 清单）
# 调用者：`python -m aicutplan.chain.qc_compare`、dsh-aicut 的 aicut_qc_compare ｜ 产物：json/qc_cross.json
"""交叉验证的「机械部分」：把两个模型各跑一遍的 verdict 目录做比对。

为什么不在这里调模型：派发给 WorkBuddy（ACP）是会话层的能力，Host 插件没有 MCP 通道。
所以分工是：
  1. 会话层分别用模型 A / B 各跑一遍 `qc --plan → 判定 → 写 verdict_XX.json`，
     分别存到 `.qcframes/session/cross/<模型名>/`；
  2. 本模块比对两个目录，输出一致率、分歧句、以及**需要 probe 的并集**；
  3. 会话层只对并集跑 `qc --probe`，人工/模型仲裁后落最终 verdict。

用法::

    python -m aicutplan.chain.qc_compare \\
        --a .qcframes/session/cross/hy4flash-free \\
        --b .qcframes/session/cross/deepseek-v4.1-flash \\
        --project <pj>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_verdicts(folder: str | Path) -> dict[int, dict]:
    """读一个目录下的 verdict_*.json，返回 {句号: {match, why}}（后写覆盖先写）。"""
    out: dict[int, dict] = {}
    d = Path(folder)
    if not d.exists():
        return out
    for f in sorted(d.glob("verdict_*.json")):
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in rows:
            try:
                out[int(r["i"])] = {"match": int(r.get("match", 1)),
                                    "why": str(r.get("why", ""))[:80]}
            except (KeyError, TypeError, ValueError):
                continue
    return out


def compare(a: dict[int, dict], b: dict[int, dict]) -> dict:
    """纯函数：比对两套判定。缺一边的句子算 only_a / only_b，不计入一致率分母之外的争议。"""
    keys = sorted(set(a) | set(b))
    both = [i for i in keys if i in a and i in b]
    disagree = [{"i": i, "a": a[i]["match"], "b": b[i]["match"],
                 "why_a": a[i]["why"], "why_b": b[i]["why"]}
                for i in both if a[i]["match"] != b[i]["match"]]
    zeros_a = [i for i in both if a[i]["match"] == 0]
    zeros_b = [i for i in both if b[i]["match"] == 0]
    union = sorted(set(zeros_a) | set(zeros_b))
    return {
        "total": len(keys),
        "both": len(both),
        "agreed": len(both) - len(disagree),
        "agreement": round((len(both) - len(disagree)) / len(both), 3) if both else None,
        "only_a": [i for i in keys if i in a and i not in b],
        "only_b": [i for i in keys if i in b and i not in a],
        "zeros_a": zeros_a,
        "zeros_b": zeros_b,
        "union_zeros": union,
        "disagree": disagree,
    }


def format_report(rep: dict) -> str:
    lines = [f"交叉验证：{rep['both']} 句共同判定，一致 {rep['agreed']} "
             f"（{rep['agreement'] * 100:.1f}%）" if rep["agreement"] is not None
             else "交叉验证：没有共同判定的句子"]
    if rep["only_a"] or rep["only_b"]:
        lines.append(f"  只 A 有 {len(rep['only_a'])} 句 {rep['only_a'][:8]}；"
                     f"只 B 有 {len(rep['only_b'])} 句 {rep['only_b'][:8]}")
    lines.append(f"  A 判不匹配：{rep['zeros_a'] or '无'}")
    lines.append(f"  B 判不匹配：{rep['zeros_b'] or '无'}")
    lines.append(f"  → 建议 probe 的并集（{len(rep['union_zeros'])} 句）：{rep['union_zeros'] or '无'}")
    if rep["disagree"]:
        lines.append("  分歧明细：")
        for d in rep["disagree"]:
            lines.append(f"    句{d['i']}: A={d['a']}（{d['why_a']}）｜ B={d['b']}（{d['why_b']}）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qc-compare", description="两套模型判定的交叉验证比对")
    ap.add_argument("--a", required=True, help="模型 A 的 verdict 目录")
    ap.add_argument("--b", required=True, help="模型 B 的 verdict 目录")
    ap.add_argument("--project", default=None, help="给了就把结果写进 <工程>/json/qc_cross.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    a = load_verdicts(args.a)
    b = load_verdicts(args.b)
    if not a or not b:
        print(f"读取失败：A {len(a)} 句 / B {len(b)} 句（目录里要有 verdict_*.json）",
              file=sys.stderr)
        return 2

    rep = compare(a, b)
    rep["dir_a"], rep["dir_b"] = str(Path(args.a).resolve()), str(Path(args.b).resolve())
    if args.project:
        pj = Path(args.project).expanduser().resolve()
        proj = pj if pj.is_dir() else pj.parent
        out = proj / "json" / "qc_cross.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        rep["written"] = str(out)

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(format_report(rep))
        if rep.get("written"):
            print(f"  → {rep['written']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
