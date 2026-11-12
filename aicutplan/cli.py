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
import json
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
DIRECT = ("check", "lint", "version", "lib", "runs")


def _library(argv: list[str]) -> int:
    """作品库：路径 / 列表 / 建系列 / 迁移工程。"""
    from . import workspace as WS

    ap = argparse.ArgumentParser(prog="aicutplan lib", description="作品库（动漫文件夹 / 集工程）")
    sub = ap.add_subparsers(dest="sub", required=True)
    p = sub.add_parser("path", help="打印作品库根目录")
    p.add_argument("--library")
    p = sub.add_parser("list", help="列出动漫 / 集 / 最新归档")
    p.add_argument("--library")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("init", help="新建动漫文件夹")
    p.add_argument("name")
    p.add_argument("--library")
    p.add_argument("--alias", action="append", default=[], help="别名，可多次")
    p.add_argument("--note", default="")
    p = sub.add_parser("migrate", help="把已有工程迁入 <动漫>/<集>/")
    p.add_argument("--project", required=True)
    p.add_argument("--series", required=True)
    p.add_argument("--episode", default="EP01")
    p.add_argument("--library")
    p.add_argument("--move", action="store_true", help="移动而非复制（同盘秒级）")
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.sub == "path":
        print(WS.library_root(a.library))
        return 0
    if a.sub == "init":
        d = WS.init_series(a.name, a.library, aliases=a.alias, note=a.note)
        print(f"系列目录: {d}")
        return 0
    if a.sub == "migrate":
        plan = WS.migrate(a.project, a.series, a.episode, library=a.library,
                          move=a.move, dry_run=a.dry_run)
        if a.dry_run:
            print(f"[dry-run] {plan['mode']}: {plan['src']} → {plan['dst']}"
                  f"（{plan['files']} 个文件）")
        else:
            print(f"完成（{plan['mode']}）→ {plan['dst']}")
        return 0

    # list
    data = WS.list_series(a.library)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    print(f"作品库: {WS.library_root(a.library)}")
    if not data:
        print("  （空：先 lib init <动漫名> 或 lib migrate --project <pj> --series <名>）")
        return 0
    print(f"  {'动漫':<20} {'集':<8} {'最新归档':<30} {'归档数':>4} {'成片':>8}  状态")
    for s in data:
        if not s["episodes"]:
            print(f"  {s['series']:<20} {'-':<8} {'（尚无集工程）':<30}")
        for e in s["episodes"]:
            mb = f"{e['final_bytes'] / 1048576:.0f}MB" if e["final_bytes"] else "-"
            st = "干净" if not e["stale"] else f"{len(e['stale'])} 条告警"
            print(f"  {s['series']:<20} {e['episode']:<8} "
                  f"{(e['latest_run'] or '未归档'):<30} {e['runs']:>4} {mb:>8}  {st}")
            for w in e["stale"][:2]:
                print(f"      ! {w[:88]}")
    return 0


def _runs(argv: list[str]) -> int:
    """生成版本：列表 / 归档 / 最新指针 / 校验。"""
    from . import workspace as WS

    ap = argparse.ArgumentParser(prog="aicutplan runs", description="生成版本（runs）管理")
    sub = ap.add_subparsers(dest="sub", required=True)
    for name, h in (("list", "列出版本"), ("latest", "打印最新归档指针"),
                    ("verify", "校验最新归档完整性")):
        p = sub.add_parser(name, help=h)
        p.add_argument("--project", required=True)
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("snapshot", help="把当前成片归档为一次不可变 run")
    p.add_argument("--project", required=True)
    p.add_argument("--label", default="", help="版本标签，如 v36")
    p.add_argument("--sentences", default=None, help="本次使用的句子稿")
    p.add_argument("--note", default="")
    a = ap.parse_args(argv)

    if a.sub == "snapshot":
        m = WS.snapshot(a.project, label=a.label, sentences=a.sentences, note=a.note)
        run_dir = WS.project_dir(a.project) / WS.RUNS_DIR / m["run_id"]
        print(f"[runs] 已归档 {m['run_id']} → {run_dir}")
        print(f"       成片 {m['artifacts'].get('final.mp4', {}).get('bytes', 0) / 1048576:.1f}MB"
              f"  产物 {len(m['artifacts'])} 个")
        return 0
    if a.sub == "latest":
        d = WS.latest(a.project, verify=True)
        if a.json:
            print(json.dumps(d, ensure_ascii=False, indent=1))
        else:
            print(f"最新归档: {d['run_id']}  标签={d.get('label') or '-'}  "
                  f"{d.get('created_at')}")
            print(f"  成片: {d.get('final')}  ({d.get('final_bytes', 0) / 1048576:.1f}MB)")
        return 0
    if a.sub == "verify":
        d = WS.latest(a.project, verify=False)
        run_dir = WS.project_dir(a.project) / WS.RUNS_DIR / d["run_id"]
        issues = WS.verify_run(run_dir)
        if issues:
            print(f"[verify] {d['run_id']} 有问题：")
            for x in issues:
                print("  ! " + x)
            return 1
        print(f"[verify] {d['run_id']} 全部产物指纹一致")
        return 0

    rows = WS.list_runs(a.project)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("（尚无归档；先 runs snapshot --project <pj> --label v1）")
        return 0
    print(f"{'run_id':<32}{'标签':<10}{'时间':<21}{'成片':>9}  最新")
    for r in rows:
        mb = f"{r['final_bytes'] / 1048576:.0f}MB" if r["final_bytes"] else "-"
        print(f"{r['run_id']:<32}{r['label'] or '-':<10}{r['created_at']:<21}{mb:>9}"
              f"  {'*' if r['is_latest'] else ''}")
    return 0


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
        from . import config as CFG
        print(f"配置目录: {CFG.config_dir()}")
        for r in CFG.summary():
            tag = "OK" if r["exists"] else "缺省"
            if r["legacy"]:
                tag += "（旧根目录，建议迁 config/）"
            print(f"  {r['name']:<24} {tag:<10} {r['bytes']:>6}B  {r['note']}")
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
    if a.command == "lib":
        return _library(a.rest)
    if a.command == "runs":
        return _runs(a.rest)
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
