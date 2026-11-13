# -*- coding: utf-8 -*-
"""批量出片唯一链路入口：``python -m aicutplan batch``。

把作品库 `_batch/rebuild.py` 的逻辑收进仓库，并补上断点续跑前置完整性校验
（v1 事故里 EP03/EP04 用 `--from-step plan` 跳过了写 hook_clip 的 story 步骤，
钩子变成 None 却没有任何报错）。

链路::

    plan → cut → narrate --sentences → r5 --redo → elastic_fit → r6 → r7 → snapshot

设计约束：
* 导演稿是**数据**（director.json），逻辑只在本模块；
* 所有步骤用子进程调既有脚本，不在本模块重写渲染逻辑；
* 断点续跑先跑 :func:`preflight_resume`，缺产物/缺钩子/契约不过一律阻断；
* ``--dry-run`` 只打印命令，便于 CI 与本地验证，不碰工程文件。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ..paths import repo_root
from .planner import load_spec, plan

STEPS = ("plan", "cut", "narrate", "tts", "fit", "mix", "mux", "snapshot")


class BatchPreflightError(RuntimeError):
    """断点续跑的前置产物不完整，继续跑会产出废片。"""


def _py_tts() -> str:
    """TTS 解释器：默认当前解释器，可用 AICUTPLAN_TTS_PY 指定装了 edge-tts 的那个。"""
    return os.environ.get("AICUTPLAN_TTS_PY") or sys.executable


def _load(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def preflight_resume(root: Path, start: str) -> list[str]:
    """断点续跑前置校验。返回问题清单，空列表表示可以续跑。

    检查三层：文件存在 → 钩子已定义 → 句子稿仍满足语义契约。
    """
    from .. import contracts as CT

    issues: list[str] = []
    if start == STEPS[0]:
        return issues
    idx = STEPS.index(start)
    need: list[str] = ["sents.json", "json/cutplan.json",
                       "json/cutplan_meta.json", "json/story_plan.json"]
    if idx >= STEPS.index("narrate"):
        need += ["out/02-video-rapid.mp4", "audio/base-rapid.wav"]
    if idx >= STEPS.index("fit"):
        need += ["json/tts-result.json"]
    if idx >= STEPS.index("mux"):
        need += ["out/mix-rapid.wav"]
    for rel in need:
        if not (root / rel).exists():
            issues.append(f"缺前置产物 {rel}（请先从更早的步骤跑起）")

    cfg = _load(root / "project.json", {}) or {}
    clip = (cfg.get("recap") or {}).get("hook_clip")
    if not (isinstance(clip, (list, tuple)) and len(clip) == 2
            and float(clip[1]) > float(clip[0])):
        issues.append("project.json 的 recap.hook_clip 未定义："
                      "说明 plan 步骤被跳过，钩子会退化成规划器随便挑的段")

    wins = (_load(root / "json" / "cutplan.json", {}) or {}).get("windows") or []
    sents = _load(root / "sents.json", None)
    if wins and isinstance(sents, list):
        errs, _, _ = CT.check_sentences_contract(sents, wins, cfg)
        issues.extend(f"旁白契约：{e}" for e in errs[:6])
    elif wins and sents is None:
        issues.append("缺 sents.json：无法证明旁白与窗口的绑定关系")
    return issues


def _run(cmd: list[str], cwd: Path | None, *, dry: bool) -> None:
    print("$ " + " ".join(str(x) for x in cmd), flush=True)
    if dry:
        return
    r = subprocess.run([str(x) for x in cmd], cwd=str(cwd) if cwd else None)
    if r.returncode != 0:
        raise RuntimeError(f"步骤失败 rc={r.returncode}: {cmd[:4]}")


def build(root: str | Path, *, start: str = "plan", script: str | Path | None = None,
          label: str = "v2-director", require_qc: bool = False,
          allow_stale: bool = False, dry_run: bool = False) -> dict:
    """按 STEPS 顺序出片。返回各步骤统计。"""
    root = Path(root).expanduser().resolve()
    # CLI 惯例是 --project <project.json 路径>；也允许直接给工程目录
    if root.is_file():
        root = root.parent
    if not (root / "project.json").exists():
        raise FileNotFoundError(f"不是工程目录（缺 project.json）：{root}")
    tools = repo_root()
    chain = tools / "aicutplan" / "chain"
    PY, TTS = sys.executable, _py_tts()
    pj = root / "project.json"
    i0 = STEPS.index(start)
    need = lambda s: STEPS.index(s) >= i0  # noqa: E731

    if start == STEPS[0] and not script:
        raise ValueError("从 plan 开始必须给 --script <director.json>")
    problems = preflight_resume(root, start)
    if problems:
        raise BatchPreflightError("断点续跑前置校验未通过：\n  - " + "\n  - ".join(problems))

    result: dict = {"root": str(root), "steps": []}
    if need("plan"):
        spec = load_spec(script)
        st = plan(root, spec, report=root / "director_table.md") if not dry_run else {}
        if not dry_run:
            # 自防御：planner 产出的稿必须过契约，否则说明编排有 bug
            from .. import contracts as CT
            wins = json.loads((root / "json" / "cutplan.json").read_text(encoding="utf-8"))["windows"]
            sents = json.loads((root / "sents.json").read_text(encoding="utf-8"))
            cfg = json.loads(pj.read_text(encoding="utf-8"))
            errs, _, _ = CT.check_sentences_contract(sents, wins, cfg)
            if errs:
                raise RuntimeError("planner 生成的稿未通过契约：\n  - " + "\n  - ".join(errs[:6]))
            print(f"[plan] {st['windows']} 窗 / {st['sents']} 句 / {st['chars']} 字 / "
                  f"{st['seconds']}s / 钩子 {st['hook_clip']}", flush=True)
            result["plan"] = st
        # 旧阶段指纹基线对应上一稿，留档后让各阶段重新登记
        state = root / "json" / "render_state.json"
        if not dry_run and state.exists():
            bak = root / "json" / "render_state.prev.json"
            if bak.exists():
                bak.unlink()
            state.rename(bak)
            print(f"[plan] 旧基线已留档 → {bak.name}", flush=True)
        result["steps"].append("plan")

    stale = ["--allow-stale"] if allow_stale else []
    if need("cut"):
        _run([PY, root / "cut.py", "--project", pj, "--force"], root, dry=dry_run)
        result["steps"].append("cut")
    if need("narrate"):
        _run([PY, chain / "narrate_cut.py", "--project", pj,
              "--sentences", root / "sents.json"], tools, dry=dry_run)
        result["steps"].append("narrate")
    if need("tts"):
        _run([TTS, root / "r5_tts.py", "--project", pj, "--redo"], root, dry=dry_run)
        result["steps"].append("tts")
    if need("fit"):
        _run([TTS, chain / "elastic_fit.py", "--project", pj], tools, dry=dry_run)
        result["steps"].append("fit")
    if need("mix"):
        _run([PY, root / "r6_mix.py", "--project", pj, *stale], root, dry=dry_run)
        result["steps"].append("mix")
    if need("mux"):
        _run([PY, root / "r7_mux.py", "--project", pj, *stale], root, dry=dry_run)
        result["steps"].append("mux")
    if need("snapshot"):
        cmd = [PY, "-m", "aicutplan", "runs", "snapshot", "--project", pj,
               "--label", label, "--sentences", root / "sents.json"]
        if require_qc or os.environ.get("AICUTPLAN_REQUIRE_QC") == "1":
            cmd.append("--require-qc")
        _run(cmd, tools, dry=dry_run)
        result["steps"].append("snapshot")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="aicutplan batch",
        description="会话内导演批量出片唯一入口（零 API）")
    ap.add_argument("--project", required=True, help="工程 project.json 路径")
    ap.add_argument("--script", default=None, help="导演稿 director.json（从 plan 开始必填）")
    ap.add_argument("--from-step", default="plan", choices=STEPS)
    ap.add_argument("--label", default="v2-director", help="归档标签")
    ap.add_argument("--require-qc", action="store_true",
                    help="归档前要求已跑完会话内质检（AICUTPLAN_REQUIRE_QC=1 等价）")
    ap.add_argument("--allow-stale", action="store_true", help="放行渲染门禁（不推荐）")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    a = ap.parse_args(argv)
    try:
        r = build(a.project, start=a.from_step, script=a.script, label=a.label,
                  require_qc=a.require_qc, allow_stale=a.allow_stale, dry_run=a.dry_run)
    except (BatchPreflightError, ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"[batch] 已阻断：{e}", file=sys.stderr)
        return 6
    print(f"[batch] 完成：{' → '.join(r['steps'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
