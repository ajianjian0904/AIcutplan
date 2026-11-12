# -*- coding: utf-8 -*-
"""Cut-first 一键编排：剧情→画面先定→粗剪→旁白→TTS→弹性→混音→合流。

把 cut-first 全链固化成一条命令。LLM 决策环节三种模式：
  --sentences <json>   会话内已写好的句子（WorkBuddy 导演模式，推荐）
  默认                 走 DeepSeek API（narrate_cut 自主写作）
流水线脚本（cut/r5/r6/r7）在工程目录内，用流水线专用解释器执行。

指标按 cut-first 语义重定义：
  句窗比   = 总字数 / 句数（替代"字/刀"）
  语音覆盖 = Σ实测净语音 / Σ槽位（真实"旁白说了多少"，替代含静音的放置总长）
  槽位贴合 = 超槽句数（elastic_fit 后应为 0）

用法：python cf_pipeline.py --project <pj> [--sentences s.json] [--skip-tts] [--skip-render]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def run(cmd: list[str], cwd: Path | None = None) -> bool:
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None)
    return r.returncode == 0


def lint_narration(proj_dir: Path) -> list[str]:
    """TTS 前规则 lint：窗口全覆盖、槽位字数预算、顺序单调。返回违规清单。"""
    d = proj_dir / "json"
    plan = json.loads((d / "cutplan.json").read_text(encoding="utf-8"))
    narr = json.loads((d / "narration.json").read_text(encoding="utf-8"))
    wins = plan["windows"]
    n_w = len(wins)
    covered: set[int] = set()
    bad: list[str] = []
    prev_end = -1.0
    for it in narr["items"]:
        swi = None
        # sent_win_idx 在 align.json
        al = json.loads((d / "align.json").read_text(encoding="utf-8"))
        swi = al["sent_win_idx"].get(str(it["i"]))
        if not swi:
            bad.append(f"句{it['i']} 无窗口绑定")
            continue
        lo, hi = swi
        if lo < prev_end:  # 顺序回跳
            bad.append(f"句{it['i']} 窗口顺序回跳 ({lo} < {prev_end})")
        prev_end = hi
        slot = wins[hi]["new_end"] - wins[lo]["new_start"]
        est = len(it["text"]) / 4.95      # 估算净语音
        if est > slot + 0.35:
            bad.append(f"句{it['i']} 预估语音 {est:.2f}s > 槽位 {slot:.2f}s（{it['text'][:14]}）")
        for x in range(lo, hi + 1):
            if x in covered:
                bad.append(f"窗口{x} 被重复覆盖")
            covered.add(x)
    unc = [x for x in range(n_w) if x not in covered]
    if unc:
        bad.append(f"未覆盖窗口 {unc[:10]}{'...' if len(unc) > 10 else ''}")
    return bad


def report(proj_dir: Path) -> None:
    """cut-first 语义指标（替代旧的字/刀、覆盖口径）。"""
    d = proj_dir / "json"
    plan = json.loads((d / "cutplan.json").read_text(encoding="utf-8"))
    narr = json.loads((d / "narration.json").read_text(encoding="utf-8"))
    qc = json.loads((d / "qc_report.json").read_text(encoding="utf-8")) if (d / "qc_report.json").exists() else []
    wins = plan["windows"]
    total_slot = sum(w["dur"] for w in wins)
    chars = narr["stats"]["chars"]
    speech = sum(r["speech"] for r in qc) or narr["stats"]["speech_sec"]
    over = [r for r in qc if r["ratio"] > 1.0]
    print("=== Cut-first 指标 ===")
    print(f"  时长       {plan['stats']['out_dur']}s / {len(wins)} 窗")
    print(f"  句窗比     {chars / max(1, narr['stats']['sentences']):.2f} 字/句（替代字/刀）")
    print(f"  语音覆盖   {100 * speech / max(1, total_slot):.1f}%（实测净语音 / 槽位）")
    print(f"  槽位贴合   超槽 {len(over)} 句（应为 0）")
    print(f"  响度等     见 r7 验收（时长/密度/响度口径不变）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--sentences", default=None, help="会话内旁白 JSON（导演模式）")
    ap.add_argument("--skip-plan", action="store_true", help="跳过因果+画面先定（已定型）")
    ap.add_argument("--skip-tts", action="store_true")
    ap.add_argument("--skip-render", action="store_true")
    ap.add_argument("--no-lint-block", action="store_true", help="lint 不通过时继续（默认阻断）")
    ap.add_argument("--qc", action="store_true",
                    help="渲染后生成会话内质检批次（qc_visual --plan），判定后 --merge")
    ap.add_argument("--qc-per-batch", type=int, default=6, help="每批几句（宫格图 2×3）")
    ap.add_argument("--qc-apply", default=None,
                    help="质检闭环：按改好的句子稿局部重跑失配句（qc_loop --apply）")
    ap.add_argument("--dry-run", action="store_true", help="只打印闭环命令，不执行")
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent
    PY = sys.executable

    # 闭环局部重跑是独立动作，不能顺带重跑整条链（会覆盖刚改好的窗口/句子）
    if a.qc_apply:
        cmd = [PY, str(ROOT / "qc_loop.py"), "--project", str(pj),
               "--apply", str(Path(a.qc_apply).expanduser().resolve())]
        if a.dry_run:
            cmd.append("--dry-run")
        return 0 if run(cmd) else 2

    # 0) 因果图谱（缓存则跳过）
    if not a.skip_plan:
        if not run([PY, str(ROOT / "causal_graph.py"), "--project", str(pj)]):
            return 2
        # 1) 画面先定
        if not run([PY, str(ROOT / "cutfirst_plan.py"), "--project", str(pj)]):
            return 2
        # 2) 无声粗剪（工程脚本）
        if not run([PY, str(proj_dir / "cut.py"), "--project", str(pj), "--force"], cwd=proj_dir):
            return 2
    # 3) 旁白后写（会话模式 / API 模式）
    cmd = [PY, str(ROOT / "narrate_cut.py"), "--project", str(pj)]
    if a.sentences:
        cmd += ["--sentences", str(Path(a.sentences).resolve())]
    if not run(cmd):
        return 2
    # 4) TTS 前 lint
    bad = lint_narration(proj_dir)
    if bad:
        for b in bad:
            print(f"[lint] {b}")
        if not a.no_lint_block:
            print("[lint] 未通过，阻断（--no-lint-block 跳过）")
            return 3
    # 5) TTS + 弹性对齐
    if not a.skip_tts:
        if not run([PY, str(proj_dir / "r5_tts.py"), "--project", str(pj)], cwd=proj_dir):
            return 2
        if not run([PY, str(ROOT / "elastic_fit.py"), "--project", str(pj)]):
            return 2
    # 6) 混音 + 合流
    if not a.skip_render:
        if not run([PY, str(proj_dir / "r6_mix.py"), "--project", str(pj)], cwd=proj_dir):
            return 2
        if not run([PY, str(proj_dir / "r7_mux.py"), "--project", str(pj)], cwd=proj_dir):
            return 2
    if a.qc:
        if not run([PY, str(ROOT / "qc_visual.py"), "--project", str(pj),
                    "--plan", str(a.qc_per_batch)]):
            return 2
        print("[质检] 会话内按宫格图判定 → verdict_XX.json → qc_visual.py --merge "
              "→ cf_pipeline.py --qc-apply <改好的句子稿>")
    report(proj_dir)
    print(f"[cf_pipeline] 完成 → {proj_dir / 'out' / '03-rapid-narration.mp4'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
