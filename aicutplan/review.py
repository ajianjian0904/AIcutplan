# -*- coding: utf-8 -*-
"""L1·跨集阶段汇总：把 N 集同一阶段的产物收成「指标 + 必看 + 抽样」，供人工审批。

所属层：L1 通用工具 ｜ 职责：只读汇总、可复现抽样 ｜ 调用者：``aicutplan review``、插件 aicut_review

为什么不是「全量输出」：EP02–04 每集 146–153 句，逐句审不现实。所以只给三类东西：
1. **指标**：句数 / 字数 / 超槽句 / 覆盖 / 钩子句数；
2. **必看清单**：钩子段全部句、每块首句、超槽句、数字断言句（这几类正是事故高发区）；
3. **随机抽样**：给比例与 seed，结果可复现（seed 会写进批准记录）。

产物形态：Markdown（能直接读、能进 git、不依赖浏览器）。
"""
from __future__ import annotations

import math
import random
from datetime import datetime
from pathlib import Path

from .io import load_json
from .shared.number_claims import check_details

STAGES = ("cutplan", "narration", "publish")


def _root(project: str | Path) -> Path:
    p = Path(project).expanduser().resolve()
    return p if p.is_dir() else p.parent


def _windows(root: Path) -> list[dict]:
    return (load_json(root / "json" / "cutplan.json", {}) or {}).get("windows") or []


def _sentences(root: Path) -> list[dict]:
    items = (load_json(root / "json" / "narration.json", {}) or {}).get("items") or []
    swi = (load_json(root / "json" / "align.json", {}) or {}).get("sent_win_idx") or {}
    out = []
    for it in items:
        try:
            lo, hi = int(swi[str(it["i"])][0]), int(swi[str(it["i"])][1])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        out.append({"i": int(it["i"]), "text": str(it.get("text") or ""),
                    "ws": lo, "we": hi, "t0": it.get("t0"), "dur": it.get("dur")})
    return out


def _beats(root: Path) -> dict[int, int]:
    """窗口号 → 块号（cutplan_meta.meta 与 windows 平行）。"""
    meta = (load_json(root / "json" / "cutplan_meta.json", {}) or {}).get("meta") or []
    return {k: int(m.get("beat", 0)) for k, m in enumerate(meta) if isinstance(m, dict)}


def _over_slot(root: Path) -> dict[int, float]:
    """压缩**之后**仍溢出的句子：speech(适配后) - slot > 0.05。

    注意别用 tts-result.json 的 ``over``——那是压缩前的原始 TTS 时长差，
    11 字/1.32s 配方下几乎句句为正，会把整集都判成超槽。以 qc_report 为准。
    """
    rows = load_json(root / "json" / "qc_report.json", []) or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("rows") or []
    out = {}
    for r in rows:
        try:
            over = float(r.get("speech") or 0) - float(r.get("slot") or 0)
            if over > 0.05:
                out[int(r["i"])] = round(over, 2)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _qc(root: Path) -> dict:
    rows = load_json(root / "json" / "qc_visual.json", {}) or []
    if isinstance(rows, dict):
        rows = rows.get("rows") or []
    judged = sum(1 for x in rows if x.get("judged"))
    bad = [int(x.get("i")) for x in rows
           if isinstance(x.get("i"), int) and int(x.get("match", 1)) == 0]
    return {"total": len(rows), "judged": judged, "mismatch": sorted(bad)}


# ---------------------------------------------------------------- 各阶段汇总

def _hook_ids(root: Path, wins: list[dict]) -> set[int]:
    """钩子窗 = 源时间落在 recap.hook_clip 区间内的窗（tier 只有 line/hold_hl，不标 hook）。"""
    cfg = load_json(root / "project.json", {}) or {}
    clip = (cfg.get("recap") or {}).get("hook_clip")
    if not (isinstance(clip, (list, tuple)) and len(clip) == 2):
        return set()
    lo, hi = float(clip[0]), float(clip[1])
    return {k for k, w in enumerate(wins)
            if float(w.get("src_start") or 0) >= lo - 0.05
            and float(w.get("src_end") or 0) <= hi + 0.05}


def summarize_cutplan(project: str | Path) -> dict:
    root = _root(project)
    wins = _windows(root)
    blocks = (load_json(root / "json" / "story_plan.json", {}) or {}).get("blocks") or []
    hook_set = _hook_ids(root, wins)
    hook = sorted(hook_set)
    cfg = load_json(root / "project.json", {}) or {}
    short = [k for k, w in enumerate(wins) if float(w.get("src_end") or 0) - float(w.get("src_start") or 0) < 0.6]
    back = []
    prev = None
    for k, w in enumerate(wins):
        src = float(w.get("src_start") or 0)
        if k not in hook_set and prev is not None and src < prev - 0.01:
            back.append(k)
        prev = src
    return {
        "project": str(root),
        "windows": len(wins),
        "hook_range": [hook[0], hook[-1]] if hook else None,
        "hook_clip": (cfg.get("recap") or {}).get("hook_clip"),
        "blocks": [{"label": b.get("label"), "type": b.get("type"),
                    "t0": b.get("t0"), "t1": b.get("t1"), "target": b.get("target")}
                   for b in blocks],
        "short_windows": short[:12],
        "src_backjumps": back[:12],
    }


def summarize_narration(project: str | Path, *, ratio: float = 0.1, seed: int | None = None) -> dict:
    root = _root(project)
    sents = _sentences(root)
    wins = _windows(root)
    beats = _beats(root)
    over = _over_slot(root)
    numbers = {d["i"]: d["missing"] for d in check_details(root)}
    hook_ids = _hook_ids(root, wins)

    hook_sents = [s["i"] for s in sents
                  if any(w in hook_ids for w in range(s["ws"], s["we"] + 1))]
    block_first = {}
    for s in sents:
        beat = beats.get(s["ws"])
        if beat is not None and beat not in block_first:
            block_first[beat] = s["i"]
    must = sorted(set(hook_sents) | set(block_first.values()) | set(over) | set(numbers))

    seed = int(seed if seed is not None else datetime.now().strftime("%Y%m%d"))
    pool = [s["i"] for s in sents if s["i"] not in must]
    k = min(len(pool), max(3, math.ceil(len(sents) * max(0.0, ratio))))
    sample = sorted(random.Random(seed).sample(pool, k)) if pool else []

    stats = (load_json(root / "json" / "narration.json", {}) or {}).get("stats") or {}
    by_i = {s["i"]: s for s in sents}
    return {
        "project": str(root),
        "sentences": len(sents),
        "chars": sum(len(s["text"]) for s in sents),
        "speech_sec": stats.get("speech_sec"),
        "coverage": stats.get("coverage") or stats.get("speech_pct"),
        "hook_sents": len(hook_sents),
        "over_slot": over,
        "numbers": numbers,
        "must_see": must,
        "sample": sample,
        "seed": seed,
        "texts": {i: by_i[i]["text"] for i in set(must) | set(sample)},
    }


def summarize_publish(project: str | Path) -> dict:
    root = _root(project)
    qc = _qc(root)
    final = root / "out" / "03-rapid-narration.mp4"
    latest = load_json(root / "runs" / "latest.json", {}) or {}
    return {
        "project": str(root),
        "final": str(final) if final.exists() else None,
        "final_mb": round(final.stat().st_size / 1048576, 1) if final.exists() else None,
        "qc": qc,
        "latest_run": latest.get("run_id"),
        "latest_label": latest.get("label"),
    }


_SUM = {"cutplan": summarize_cutplan, "narration": summarize_narration, "publish": summarize_publish}


# ---------------------------------------------------------------- Markdown

def _md_cutplan(d: dict) -> list[str]:
    lines = [f"## {Path(d['project']).name}", "",
             f"- 窗口 {d['windows']} 个；钩子 {'-'.join(map(str, d['hook_range'])) if d['hook_range'] else '无'}",
             f"- hook_clip：{d['hook_clip']}",
             f"- 块 {len(d['blocks'])} 个"]
    for b in d["blocks"]:
        lines.append(f"  - [{b['type']}] {b['label']}  {b['t0']}–{b['t1']}s  目标 {b['target']}")
    if d["short_windows"]:
        lines.append(f"- ⚠ <0.6s 短窗：{d['short_windows']}")
    if d["src_backjumps"]:
        lines.append(f"- ⚠ 源时间回跳窗口：{d['src_backjumps']}")
    return lines


def _md_narration(d: dict) -> list[str]:
    name = Path(d["project"]).name
    lines = [f"## {name}", "",
             f"- 句数 {d['sentences']}／字数 {d['chars']}／语音 {d['speech_sec']}s",
             f"- 钩子句 {d['hook_sents']}；超槽 {len(d['over_slot'])} 句；数字断言 {len(d['numbers'])} 句",
             f"- 必看 {len(d['must_see'])} 句；抽样 {len(d['sample'])} 句（seed={d['seed']}）", ""]
    for tag, ids in (("钩子/块首/超槽/数字（必看）", d["must_see"]), ("随机抽样", d["sample"])):
        if not ids:
            continue
        lines.append(f"### {tag}")
        for i in ids:
            extra = []
            if i in d["over_slot"]:
                extra.append(f"超槽 +{d['over_slot'][i]}s")
            if i in d["numbers"]:
                extra.append(f"数字 {d['numbers'][i]}")
            tail = ("（" + "；".join(extra) + "）") if extra else ""
            lines.append(f"- 句{i}：{d['texts'].get(i, '')}{tail}")
        lines.append("")
    return lines


def _md_publish(d: dict) -> list[str]:
    qc = d["qc"]
    ok = qc["judged"] == qc["total"] and not qc["mismatch"]
    return [f"## {Path(d['project']).name}", "",
            f"- 成片：{'%.1f MB' % d['final_mb'] if d['final_mb'] else '缺失'}",
            f"- 质检：判定 {qc['judged']}/{qc['total']}，失配 {qc['mismatch'] or '无'} {'✅' if ok else '⚠'}",
            f"- 最新归档：{d['latest_run']}（{d['latest_label']}）", ""]


_MD = {"cutplan": _md_cutplan, "narration": _md_narration, "publish": _md_publish}


def write_review(projects: list[str | Path], stage: str, *, out: str | Path | None = None,
                 ratio: float = 0.1, seed: int | None = None) -> dict:
    """汇总 N 集某阶段，写 index.md + <EP>.md，返回 {dir, files, summaries}。"""
    if stage not in _SUM:
        raise ValueError(f"未知阶段：{stage}（可选 {'/'.join(STAGES)}）")
    rows = []
    for p in projects:
        if stage == "narration":
            rows.append(_SUM[stage](p, ratio=ratio, seed=seed))
        else:
            rows.append(_SUM[stage](p))

    first = _root(projects[0]).parent
    out_dir = Path(out) if out else first / "_review" / stage / datetime.now().strftime("%Y%m%d-%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = [f"# {stage} 汇总审核（{datetime.now().isoformat(timespec='seconds')}）", ""]
    if stage == "narration":
        lines += ["| 集 | 句数 | 字数 | 语音 | 钩子句 | 超槽 | 数字 | 必看 | 抽样 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for d in rows:
            lines.append(f"| {Path(d['project']).name} | {d['sentences']} | {d['chars']} | "
                         f"{d['speech_sec']}s | {d['hook_sents']} | {len(d['over_slot'])} | "
                         f"{len(d['numbers'])} | {len(d['must_see'])} | {len(d['sample'])} |")
        lines.append("")
    for d in rows:
        part = _MD[stage](d)
        (out_dir / f"{Path(d['project']).name}.md").write_text("\n".join(part), encoding="utf-8")
        lines += part
    index = out_dir / "index.md"
    index.write_text("\n".join(lines), encoding="utf-8")
    return {"dir": str(out_dir), "index": str(index), "summaries": rows}


def format_console(result: dict, stage: str) -> str:
    rows = result["summaries"]
    lines = [f"[review] {stage}：{len(rows)} 集 → {result['index']}"]
    for d in rows:
        name = Path(d["project"]).name
        if stage == "narration":
            lines.append(f"  {name}: {d['sentences']} 句 / {d['chars']} 字 / 超槽 {len(d['over_slot'])} / "
                         f"必看 {len(d['must_see'])} / 抽样 {len(d['sample'])}（seed={d['seed']}）")
        elif stage == "cutplan":
            lines.append(f"  {name}: {d['windows']} 窗 / 块 {len(d['blocks'])} / "
                         f"钩子 {d['hook_range']}")
        else:
            qc = d["qc"]
            lines.append(f"  {name}: 质检 {qc['judged']}/{qc['total']}，失配 {qc['mismatch'] or '无'}")
    return "\n".join(lines)
