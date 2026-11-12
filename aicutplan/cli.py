# -*- coding: utf-8 -*-
"""L2·统一 CLI 门面：``python -m aicutplan <子命令>``。

所属层：L2 编排层（门面） ｜ 职责：给分散的阶段脚本一个稳定入口
说明：**不重写任何业务逻辑**，只做参数转发；阶段脚本仍是唯一实现，
      因此这里永远不会有「两份真相」。

常用命令::

    python -m aicutplan subs --project <pj> --apply --refresh-derived
    python -m aicutplan run  --project <pj> --sentences sents.json
    python -m aicutplan qc   --project <pj> --plan 6
    python -m aicutplan check --project <pj>
    python -m aicutplan serve

``run / plan / qc / qc-apply / subs / serve`` 的参数原样透传给对应脚本，
查完整参数用 ``python -m aicutplan <子命令> -h`` 或直接看该脚本的 ``--help``。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import __version__
from .paths import repo_root, script

#: 子命令 → 仓库根下的阶段脚本
SCRIPTS = {
    "run": "aicutplan/chain/cf_pipeline.py",
    "plan": "aicutplan/chain/cutfirst_plan.py",
    "subs": "aicutplan/chain/subs_extract.py",
    "qc": "aicutplan/chain/qc_visual.py",
    "qc-apply": "aicutplan/chain/qc_loop.py",
    "serve": "aicutplan/webui/server.py",
    "narrate": "aicutplan/chain/narrate_cut.py",
    "parse": "aicutplan/shared/story_parse.py",
}

# 这些子命令由本包直接实现，不启子进程
DIRECT = ("check", "lint", "version")


def _delegate(name: str, args: list[str]) -> int:
    target = script(SCRIPTS[name])
    if not target.exists():
        print(f"[aicutplan] 找不到阶段脚本: {target}", file=sys.stderr)
        return 2
    cmd = [sys.executable, str(target)] + args
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(repo_root())).returncode


def _check(project: str | Path, *, lint_only: bool = False) -> int:
    """工程体检：lint + 时间轴 + TTS 一致性 + 台词源指纹。"""
    from .contracts import (check_alignment, check_tts_consistency,
                            format_issues, lint_narration, summarize_quality,
                            validate_windows)
    from .io import load_json
    from .paths import ProjectPaths

    p = ProjectPaths(project)
    issues: list[str] = []
    try:
        p.require(p.cutplan, p.narration, p.align)
    except FileNotFoundError as exc:
        print(f"[check] {exc}", file=sys.stderr)
        return 2

    issues += ["lint: " + x for x in lint_narration(p.root)]
    wins = (load_json(p.cutplan, default={}) or {}).get("windows") or []
    narr = (load_json(p.narration, default={}) or {}).get("items") or []
    issues += ["窗口: " + x for x in validate_windows(wins)]

    al = load_json(p.align, default={}) or {}
    swi = {int(k): int(v[0]) for k, v in (al.get("sent_win_idx") or {}).items()}
    issues += ["对齐: " + x for x in check_alignment(narr, wins, swi)]

    tts = (load_json(p.tts_result, default={}) or {}).get("items") or []
    issues += ["TTS: " + x
               for x in check_tts_consistency(narr, tts, mp3_dir=p.tts)]

    print(format_issues(issues, title="工程体检"))
    if not lint_only:
        q = summarize_quality(p.root)
        print("指标: " + "  ".join(f"{k}={v}" for k, v in q.items()))
    return 0 if not issues else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        argv = argv or ["--help"]

    ap = argparse.ArgumentParser(
        prog="aicutplan",
        description="漫剪解说流水线 · 统一入口（阶段脚本的薄门面）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="子命令: " + ", ".join(list(SCRIPTS) + list(DIRECT)),
    )
    ap.add_argument("--version", action="version", version=f"aicutplan {__version__}")
    ap.add_argument("command", nargs="?", default="--help",
                    choices=[*SCRIPTS, *DIRECT])
    ap.add_argument("rest", nargs=argparse.REMAINDER,
                    help="透传给阶段脚本的参数（如 --project <pj>）")
    a = ap.parse_args(argv)

    if a.command == "version":
        print(f"aicutplan {__version__}")
        return 0
    if a.command in ("check", "lint"):
        # 从透传参数里取 --project
        project = None
        for i, x in enumerate(a.rest):
            if x == "--project" and i + 1 < len(a.rest):
                project = a.rest[i + 1]
                break
            if x.startswith("--project="):
                project = x.split("=", 1)[1]
                break
        if not project:
            print("[aicutplan] check/lint 需要 --project <project.json>", file=sys.stderr)
            return 2
        return _check(project, lint_only=(a.command == "lint"))
    return _delegate(a.command, a.rest)


if __name__ == "__main__":
    raise SystemExit(main())
