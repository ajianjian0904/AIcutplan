# -*- coding: utf-8 -*-
"""L0·数据契约与一致性校验：把「靠人看片才发现的问题」变成可自动跑的规则。

所属层：L0 基础层 ｜ 职责：定义窗口/句子/证据包 schema，并校验产物一致性
调用者：cf_pipeline.lint_narration（直接复用）、qc_loop（闭环后校验）、
        tests/（单元测试主体）、未来任何写 cutplan/narration 的脚本

=============================================================================
契约字段（与现有工程数据一致，改字段名必须同步改这里）
=============================================================================
窗口 ``cutplan.json.windows[]``
    new_start / new_end  输出时间轴（秒，cut.py 会回写帧网格后的精确值）
    src_start / src_end  源片区间（钩子块允许与前后不连续）
    dur                  窗口时长
    tier                 line（旁白）/ hold_hl（原声留白）/ hook（钩子快切）
    ost_mode / reason / sent_ids（web 台读 sent_ids）
句子 ``narration.json.items[]``
    i / t0 / dur / text / beat
    **t0 必须等于其 ws 窗口的 new_start**，否则音画整体错位
绑定 ``align.json.sent_win_idx``
    {"0": [lo, hi], ...}  句子覆盖的窗口区间（闭区间）
镜头证据 ``shots_desc.json.rows[]``
    i / a / b / desc / obj / depth / facts / dlg

=============================================================================
已固化的三条事故防线
=============================================================================
1. :func:`check_alignment` —— 旁白 t0 与窗口 new_start 逐句比对（cut.py 回写
   帧网格后会整体平移 ~0.04s，必须重跑 narrate_cut 才对齐）。
2. :func:`check_tts_consistency` —— 文本一致 + mp3 存在 + mp3 不早于
   narration.json（防「文本新词、音频旧词」）。
3. :func:`lint_narration` —— 渲染前粗筛：窗口全覆盖、槽位预算、顺序单调；
   **原声留白窗不计入未覆盖**，粗估在 +35% 语速下系统性高估故放宽容差。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .io import load_json

# ---- schema ---------------------------------------------------------------
WINDOW_REQUIRED = ("new_start", "new_end", "src_start", "src_end", "dur", "tier")
SENTENCE_REQUIRED = ("i", "t0", "dur", "text")
SHOT_KEYS = ("i", "a", "b", "desc", "obj", "depth", "facts", "dlg")

#: 不写旁白的窗口类型，不能算「未覆盖」
RAW_WINDOW_TIERS = frozenset({"hold_hl"})
RAW_WINDOW_OST = frozenset({"original"})

#: +35% 语速下的字数→秒粗估系数（真超槽由 elastic_fit 兜底）
CHARS_PER_SEC = 4.95


def is_raw_window(w: dict) -> bool:
    """是否原声留白窗（旁白区与原声块互斥）。"""
    return (w.get("tier") in RAW_WINDOW_TIERS
            or w.get("ost_mode") in RAW_WINDOW_OST)


# ---- 结构校验 -------------------------------------------------------------
def validate_windows(wins: list[dict], *, tol: float = 0.05) -> list[str]:
    """校验窗口表：字段齐全、输出时间首尾相接、dur 与实际跨度一致。"""
    bad: list[str] = []
    prev_end = None
    for i, w in enumerate(wins):
        missing = [k for k in WINDOW_REQUIRED if k not in w]
        if missing:
            bad.append(f"窗口{i} 缺字段 {missing}")
            continue
        a, z, dur = float(w["new_start"]), float(w["new_end"]), float(w["dur"])
        if z <= a:
            bad.append(f"窗口{i} 非正时长 {a:.3f}-{z:.3f}")
        if abs((z - a) - dur) > tol:
            bad.append(f"窗口{i} dur={dur:.3f} 与跨度 {z-a:.3f} 不一致")
        if prev_end is not None and abs(a - prev_end) > tol:
            bad.append(f"窗口{i} 起点 {a:.3f} 未接上一窗末尾 {prev_end:.3f}")
        prev_end = z
    return bad


def uncovered_windows(wins: list[dict], sent_win_idx: dict) -> list[int]:
    """返回既未被任何句子覆盖、又不是留白窗的窗口索引。"""
    covered: set[int] = set()
    for pair in sent_win_idx.values():
        lo, hi = int(pair[0]), int(pair[1])
        covered.update(range(lo, hi + 1))
    return [i for i, w in enumerate(wins)
            if i not in covered and not is_raw_window(w)]


def check_alignment(items: list[dict], wins: list[dict],
                    sent_ws: dict[int, int] | list[int],
                    *, tol: float = 0.001) -> list[str]:
    """逐句校验 ``t0 == wins[ws].new_start``。

    ``sent_ws`` 可以是 ``{句i: ws}`` 或按句号索引的序列（句子稿的 ws 字段）。
    """
    bad: list[str] = []
    for it in items:
        i = int(it["i"])
        ws = (sent_ws.get(i) if isinstance(sent_ws, dict)
              else sent_ws[i] if i < len(sent_ws) else None)
        if ws is None or not (0 <= int(ws) < len(wins)):
            bad.append(f"句{i} 无有效窗口锚点")
            continue
        delta = float(it["t0"]) - float(wins[int(ws)]["new_start"])
        if abs(delta) > tol:
            bad.append(f"句{i} t0 偏差 {delta:+.4f}s（应重跑 narrate_cut）")
    return bad


def check_tts_consistency(items: list[dict], tts_items: list[dict], *,
                          mp3_dir: Path | None = None,
                          require_mp3: bool = True) -> list[str]:
    """校验 TTS 与当前旁白是否同一稿。

    三层：条数 / 文本逐字一致 / mp3 存在且不早于 narration.json。
    第三层专门拦截 `r5_tts --only` 曾出现的「元数据更新了但音频没重合成」。
    """
    bad: list[str] = []
    by_id = {int(x["i"]): x for x in tts_items}
    if len(by_id) != len(items):
        bad.append(f"TTS 条数 {len(by_id)} ≠ 旁白句数 {len(items)}")
    narr_mtime = None  # 由调用方传入时用 mp3_dir 的父级 narration.json
    if mp3_dir is not None:
        cand = mp3_dir.parent / "json" / "narration.json"
        if cand.exists():
            narr_mtime = cand.stat().st_mtime
    for it in items:
        i = int(it["i"])
        t = by_id.get(i)
        if t is None:
            bad.append(f"句{i} 缺 TTS 记录")
            continue
        if str(t.get("text", "")) != str(it.get("text", "")):
            bad.append(f"句{i} TTS 文本与旁白不一致")
        if require_mp3 and mp3_dir is not None:
            p = mp3_dir / f"{i:03d}.mp3"
            if not p.exists() or p.stat().st_size == 0:
                bad.append(f"句{i} 缺 mp3")
            elif (narr_mtime and p.stat().st_mtime < narr_mtime
                  and str(t.get("text", "")) != str(it.get("text", ""))):
                # 文本已变而音频没跟上 → 典型的「旧 mp3 冒充新句」
                bad.append(f"句{i} mp3 早于 narration.json 且文本已改（旧音频）")
    return bad


def summarize_quality(proj_dir: str | Path) -> dict:
    """汇总一个工程的四项核心指标，供 report / CI 使用。"""
    from .paths import ProjectPaths
    p = ProjectPaths(proj_dir)
    plan = load_json(p.cutplan, default={})
    narr = load_json(p.narration, default={})
    tts = load_json(p.tts_result, default={})
    wins = plan.get("windows") or []
    items = narr.get("items") or []
    tts_items = tts.get("items") or []
    durs = {int(x["i"]): float(x.get("tts_dur") or 0) for x in tts_items}
    slots = 0.0
    speech = 0.0
    al = load_json(p.align, default={})
    for it in items:
        pair = (al.get("sent_win_idx") or {}).get(str(it["i"]))
        if pair and wins:
            lo, hi = int(pair[0]), int(pair[1])
            slots += float(wins[hi]["new_end"]) - float(wins[lo]["new_start"])
        speech += durs.get(int(it["i"]), 0.0)
    chars = sum(len(str(it.get("text", ""))) for it in items)
    return {
        "句数": len(items),
        "字数": chars,
        "句窗比": round(chars / len(items), 2) if items else 0.0,
        "语音覆盖": round(speech / slots * 100, 1) if slots else 0.0,
        "窗口数": len(wins),
        "留白窗数": sum(1 for w in wins if is_raw_window(w)),
    }


# ---- 渲染前 lint（原 cf_pipeline.lint_narration，逐字等价搬入） ------------
def lint_narration(proj_dir: str | Path) -> list[str]:
    """TTS 前规则粗筛：窗口全覆盖、槽位字数预算、顺序单调。返回违规清单。

    两个历史误报已修正：
    * 留白窗（hold_hl / ost_mode=original）本来就不写旁白，不算未覆盖；
    * lint 跑在 r5_tts 之前，``tts-result.json`` 常是上一稿数据；只有它不早于
      ``narration.json`` 才算「同稿」，否则退回宽限的字数粗估。
    """
    from .paths import ProjectPaths
    p = ProjectPaths(proj_dir)
    plan = load_json(p.cutplan, default={})
    narr = load_json(p.narration, default={})
    al = load_json(p.align, default={})
    wins = plan.get("windows") or []
    items = narr.get("items") or []
    n_w = len(wins)
    covered: set[int] = set()
    bad: list[str] = []
    prev_end = -1.0

    fresh = (p.tts_result.exists() and p.narration.exists()
             and p.tts_result.stat().st_mtime >= p.narration.stat().st_mtime)

    # 当前每句槽位（用于内容级判断 qc_report 是否仍适用于这一版 cutplan）
    swi_all = al.get("sent_win_idx") or {}
    slots: dict[int, float] = {}
    for it in items:
        swi = swi_all.get(str(it["i"]))
        if swi and wins:
            lo_i, hi_i = int(swi[0]), int(swi[1])
            if 0 <= lo_i < len(wins) and 0 <= hi_i < len(wins):
                slots[int(it["i"])] = (float(wins[hi_i]["new_end"])
                                       - float(wins[lo_i]["new_start"]))

    # 优先用弹性对齐后的实测（qc_report.speech）：tts-result 记的是对齐**前**的
    # 原始时长，拿它判槽位会把已 atempo 压好的句子误报成超槽（句23/51 实际吻合）。
    # qc_report 是否适用按**内容**判定（句子集合 + 槽位一致），不看 mtime ——
    # narrate --export 等动作会刷新 narration.json，用时间戳会把有效报告判成过期。
    qc_f = p.root / "json" / "qc_report.json"
    qc_dur: dict[int, float] = {}
    if qc_f.exists() and slots:
        try:
            rows = load_json(qc_f, default=[]) or []
            by_i = {int(x["i"]): x for x in rows}
            if set(by_i) == set(slots):
                # cut.py 帧吸附会重写窗口时间（±0.15s 量级），故容差 0.6s
                same = sum(1 for i, x in by_i.items()
                           if abs(float(x.get("slot", -1e9)) - slots[i]) <= 0.6)
                if same >= 0.95 * len(by_i):
                    qc_dur = {i: float(x["speech"]) for i, x in by_i.items()
                              if x.get("speech") is not None}
        except Exception:
            qc_dur = {}
    tts_dur = ({int(x["i"]): float(x.get("tts_dur") or 0)
                for x in (load_json(p.tts_result, default={}).get("items") or [])}
               if fresh else {})
    measured = qc_dur or tts_dur
    tol = 0.35 if measured else 0.8

    for it in items:
        swi = (al.get("sent_win_idx") or {}).get(str(it["i"]))
        if not swi:
            bad.append(f"句{it['i']} 无窗口绑定")
            continue
        lo, hi = int(swi[0]), int(swi[1])
        if lo < prev_end:
            bad.append(f"句{it['i']} 窗口顺序回跳 ({lo} < {prev_end})")
        prev_end = hi
        slot = float(wins[hi]["new_end"]) - float(wins[lo]["new_start"])
        est = measured.get(int(it["i"])) or len(str(it.get("text", ""))) / CHARS_PER_SEC
        if est > slot + tol:
            bad.append(f"句{it['i']} 语音 {est:.2f}s > 槽位 {slot:.2f}s"
                       f"（{str(it.get('text',''))[:14]}）")
        for x in range(lo, hi + 1):
            if x in covered:
                bad.append(f"窗口{x} 被重复覆盖")
            covered.add(x)
    unc = uncovered_windows(wins, al.get("sent_win_idx") or {})
    if unc:
        bad.append(f"未覆盖窗口 {unc[:10]}{'...' if len(unc) > 10 else ''}")
    return bad


def format_issues(issues: Iterable[str], *, title: str = "校验") -> str:
    """把违规清单格式化成人可读文本。"""
    rows = list(issues)
    if not rows:
        return f"[{title}] 全部通过"
    return f"[{title}] {len(rows)} 项问题：\n" + "\n".join(f"  - {r}" for r in rows)
