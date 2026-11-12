# -*- coding: utf-8 -*-
# [L2 编排层] 职责：质检闭环（失配句局部重跑） ｜ 调用者：cf_pipeline --qc-apply ｜ 产物：局部 TTS/混音/成片
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""质检闭环编排（P0-1 第二步）：失配句 → 会话内改词 → 局部重跑，不整条重来。

链路天然支持局部重跑，无需重新抽帧/匹配/规划：
  narrate_cut --sentences（整表 JSON 只改失配句，其余句子 t0 不变）
    → r5_tts --only <ids>（只重合成改过的句子）
    → elastic_fit → r6_mix → r7_mux
  → 再跑 qc_visual --plan/--merge 复核

用法：
  python qc_loop.py --project <pj> --list
  python qc_loop.py --project <pj> --apply <改好的 sentences.json> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 与既有流水线一致：渲染/规划用系统 Python，TTS 用带 edge_tts 的 WorkBuddy 环境
PY = sys.executable
TTS_PY = r"C:\Users\10451\.workbuddy\binaries\python\envs\default\Scripts\python.exe"


def _load_sents(p: Path) -> dict[int, str]:
    d = json.loads(p.read_text(encoding="utf-8"))
    arr = d if isinstance(d, list) else (d.get("sents") or d.get("sentences") or [])
    return {int(x["i"]) if "i" in x else k: x["text"] for k, x in enumerate(arr)}


def run(cmd: list[str], cwd: Path | None = None) -> bool:
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--list", action="store_true", help="打印失配句重写清单")
    ap.add_argument("--apply", default=None, help="会话内改好的句子 JSON")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="文本未变也重跑（用于「文本已改但音频未重合成」的补跑）")
    ap.add_argument("--python", default=PY, help="渲染/规划解释器")
    ap.add_argument("--tts-python", default=TTS_PY, help="TTS 解释器（需 edge_tts）")
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent
    d = proj_dir / "json"

    rp = d / "qc_rewrite.json"
    if not rp.exists():
        print("无 qc_rewrite.json：先跑 qc_visual.py --plan / --merge")
        return 2
    rewrite = json.loads(rp.read_text(encoding="utf-8"))
    if a.list or not a.apply:
        print(f"失配 {len(rewrite)} 句（改词或换窗口后交给 --apply）：")
        for r in rewrite:
            print(f"  句{r['i']:>3} t={r['t']:.1f}s win={r.get('wins')} src={r.get('src')}")
            print(f"       现文本: {r['text']}")
            print(f"       失配因: {r['why']}")
            print(f"       画面: {r.get('画面证据', '')[:70]}")
        return 0

    sp = Path(a.apply).expanduser().resolve()
    if not sp.exists():
        print(f"找不到句子稿 {sp}")
        return 2
    new = _load_sents(sp)
    old = {int(x["i"]): x["text"]
           for x in json.loads((d / "narration.json").read_text(encoding="utf-8"))["items"]}
    ids = [r["i"] for r in rewrite]
    changed = [i for i in ids if i in new and new.get(i) != old.get(i)]
    if not changed and not a.force:
        print("失配句在新句子稿里一个字都没改 —— 空跑没意义，先改词再 --apply（或 --force 强制重合成）")
        return 3
    if a.force and not changed:
        changed = ids
        print("[闭环] --force：文本未变，强制重合成并重跑")
    print(f"[闭环] 待重跑 {len(changed)} 句: {changed}")
    for i in changed:
        print(f"  句{i}: {old.get(i)} → {new.get(i)}")

    only = ",".join(str(i) for i in changed)
    steps = [
        ([a.python, str(ROOT / "narrate_cut.py"), "--project", str(pj),
          "--sentences", str(sp)], None),
        ([a.tts_python, str(proj_dir / "r5_tts.py"), "--project", str(pj),
          "--only", only], proj_dir),
        # elastic_fit 内部会调 edge_tts 重合成超槽句，必须跟 r5 用同一个解释器
        ([a.tts_python, str(ROOT / "elastic_fit.py"), "--project", str(pj)], None),
        ([a.python, str(proj_dir / "r6_mix.py"), "--project", str(pj)], proj_dir),
        ([a.python, str(proj_dir / "r7_mux.py"), "--project", str(pj)], proj_dir),
    ]
    if a.dry_run:
        for cmd, cwd in steps:
            print("  $ " + " ".join(str(c) for c in cmd) + (f"   (cwd={cwd})" if cwd else ""))
        return 0
    t_start = time.time() - 2
    for k, (cmd, cwd) in enumerate(steps):
        if not run(cmd, cwd):
            print(f"[闭环] 中断于: {cmd[1]}")
            return 2
        if k == 1:  # r5 之后立刻校验音频真的换了，防止「文本新词 / 成片旧音频」
            stale = []
            for i in changed:
                f = proj_dir / "tts" / f"{i:03d}.mp3"
                if not f.exists() or f.stat().st_mtime < t_start:
                    stale.append(i)
            if stale:
                print(f"[闭环] 音频未更新（句子 {stale}），中止后续混音 —— 检查 r5_tts 缓存/删除钩子")
                return 4
            print(f"[闭环] 音频校验通过：{len(changed)} 句 mp3 均为本次重合成")
    print("[闭环] 局部重跑完成 → 再跑 qc_visual.py --plan / --merge 复核失配是否消除")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
