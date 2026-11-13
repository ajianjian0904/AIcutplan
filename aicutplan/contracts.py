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

    # 会话内导演契约：钩子定义 + 专属文案（主链 lint 默认阻断）。
    # 仅对存在钩子窗口的工程生效；窗口源时间逆序只作信息项，不进 bad。
    try:
        cfg = load_json(p.project, default={}) or {}
        hid = hook_window_ids(wins)
        n_hook = None
        if hid:
            lo, hi = hid[0], hid[-1]
            n_hook = sum(1 for v in (al.get("sent_win_idx") or {}).values()
                         if lo <= int(v[0]) <= hi)
        he, _ = check_hook_contract(wins, cfg, hook_sent_count=n_hook)
        bad.extend(he)
    except Exception as exc:      # 契约层异常不能让 lint 静默放行
        bad.append(f"导演契约校验异常：{type(exc).__name__} {exc}")
    return bad


def format_issues(issues: Iterable[str], *, title: str = "校验") -> str:
    """把违规清单格式化成人可读文本。"""
    rows = list(issues)
    if not rows:
        return f"[{title}] 全部通过"
    return f"[{title}] {len(rows)} 项问题：\n" + "\n".join(f"  - {r}" for r in rows)


# =============================================================================
# 会话内导演契约（2026-11-13 事故防线）
# -----------------------------------------------------------------------------
# 事故：批量驱动把「按剧情顺序写好的 N 句」按窗口序号均匀贴到 cutplan 窗口上。
# 所有形式闸门（覆盖度 N/N、超槽 0、验收 10/12、渲染门禁）全绿，成片却整条
# 音画错位 —— 因为旧契约 sents.json = {ws, we, text} 只约束结构，不约束语义。
#
# 修复：每句必须声明 ``src: [源片起, 源片止]``，落盘前与它绑定的窗口逐一比对。
# 这条成立 ⇒ 句子讲的画面 == 它覆盖的窗口画面 ⇒ 画面顺序 == 写稿顺序，
# 「均匀分配」会因为声明的源区间与窗口源区间对不上而当场失败。
#
# 兼容性：``--legacy-sents`` 显式豁免（历史稿无 src），并在 narration.stats
# 打 ``contract: "legacy"`` 标记；逆序仅作信息项 —— EP01 段内逆序 6 次却是
# 0 失配的合格成片（它逐窗对着画面写），故逆序不是质量的必要条件。
# =============================================================================
HOOK_MARK = "连续未裁切"
SRC_TOL_A = 0.6   # 窗口起点容差（cut.py 帧吸附约 ±0.15s）
SRC_TOL_B = 1.0   # 窗口终点容差（末窗可能被合并）


class NarrationContractError(RuntimeError):
    """旁白稿不满足会话内导演契约；必须在写 narration/align 之前阻断。"""


def hook_window_ids(wins: list[dict]) -> list[int]:
    """识别剪辑规划锁定的「源片连续未裁切爽点」钩子窗口。"""
    return [i for i, w in enumerate(wins)
            if HOOK_MARK in str(w.get("reason", ""))]


def check_hook_contract(wins: list[dict], cfg: dict,
                        hook_sent_count: int | None = None) -> tuple[list[str], list[str]]:
    """钩子断言。返回 ``(errors, warnings)``。

    仅在工程存在钩子窗口时生效：旧链工程（ep05 / recap-02 等）本来就没有
    钩子结构，不能因为新增断言把它们判死。
    """
    errors: list[str] = []
    warnings: list[str] = []
    ids = hook_window_ids(wins)
    if not ids:
        warnings.append("未识别到钩子窗口（旧链工程？），跳过钩子契约")
        return errors, warnings

    clip = (cfg.get("recap") or {}).get("hook_clip")
    valid = (isinstance(clip, (list, tuple)) and len(clip) == 2
             and all(isinstance(x, (int, float)) for x in clip)
             and float(clip[1]) > float(clip[0]))
    if not valid:
        errors.append("recap.hook_clip 未定义或不合法：钩子窗口存在，"
                      "但 project.json 没有锁定源片连续爽点段")
    else:
        a, z = float(clip[0]), float(clip[1])
        wa = float(wins[ids[0]]["src_start"])
        wz = float(wins[ids[-1]]["src_end"])
        if abs(wa - a) > SRC_TOL_A or abs(wz - z) > SRC_TOL_B:
            errors.append(f"hook_clip [{a:.2f},{z:.2f}] 与钩子窗口源区间 "
                          f"[{wa:.2f},{wz:.2f}] 不符")
        # 钩子必须是源片连续、未经裁切重排的一整段
        gaps = [k for k in range(len(ids) - 1)
                if abs(float(wins[ids[k + 1]]["src_start"])
                       - float(wins[ids[k]]["src_end"])) > 0.2]
        if gaps:
            errors.append(f"钩子段不连续：{len(gaps)} 处窗口首尾断开（钩子必须是源片连续未裁切段）")

    if hook_sent_count is not None and hook_sent_count < 2:
        errors.append(f"钩子块只有 {hook_sent_count} 句专属文案（要求 ≥2）："
                      "钩子是剪辑锁定的前置爽点，必须有自己的钩子文案，不能贴剧情开场白")
    return errors, warnings


def check_sentences_contract(sents: list[dict], wins: list[dict], cfg: dict | None = None,
                             *, legacy: bool = False) -> tuple[list[str], list[str], dict]:
    """**落盘前**校验会话内导演稿。返回 ``(errors, warnings, stats)``。

    校验项：
      1. 每句声明 ``src``（或整稿显式 legacy 豁免）；
      2. ``src`` 与绑定窗口的源片区间一致 —— 拦住「均匀分配」；
      3. 稿内源片区间不回跳 —— 导演稿声称按剧情顺序；
      4. 钩子定义 + 钩子专属文案（复用 :func:`check_hook_contract`）；
      5. 窗口源时间逆序仅作信息项，不阻断。

    本函数无副作用、不写文件，便于单测直接用构造数据回归。
    """
    errors: list[str] = []
    warnings: list[str] = []
    sents = list(sents or [])
    cfg = cfg or {}
    stats = {"contract": "src-v2", "sentences": len(sents), "src_checked": 0,
             "hook_sents": 0, "inversions": 0}
    if not sents:
        errors.append("旁白稿为空")
        return errors, warnings, stats

    with_src = [s for s in sents if s.get("src")]
    if not with_src:
        if legacy:
            stats["contract"] = "legacy"
            warnings.append("legacy 稿：全部句子缺 src，已显式豁免（历史工程兼容期）；"
                            "本次不校验语义绑定，narration.stats 会打 legacy 标记")
        else:
            errors.append("旁白稿缺 src 字段：会话内导演稿每句必须声明它对应的源片区间 "
                          "[a,b]；历史稿请加 --legacy-sents 显式豁免")
    elif len(with_src) != len(sents):
        errors.append(f"src 字段混用：{len(with_src)}/{len(sents)} 句带 src，禁止部分声明")
    else:
        mismatches = 0
        for k, s in enumerate(sents):
            try:
                ws, we = int(s["ws"]), int(s["we"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"句{k} 缺 ws/we 或不是整数")
                continue
            if not (0 <= ws <= we < len(wins)):
                errors.append(f"句{k} 窗口越界 ws={ws} we={we}（共 {len(wins)} 窗）")
                continue
            try:
                a, z = float(s["src"][0]), float(s["src"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                errors.append(f"句{k} src 不是合法的 [a,b] 数值对")
                continue
            if z <= a:
                errors.append(f"句{k} src 区间倒置 [{a:.1f},{z:.1f}]")
                continue
            wa, wz = float(wins[ws]["src_start"]), float(wins[we]["src_end"])
            if abs(a - wa) > SRC_TOL_A or abs(z - wz) > SRC_TOL_B:
                mismatches += 1
                if mismatches <= 6:      # 均匀分配会全表报错，只列前 6 条
                    errors.append(f"句{k} 声明源片 [{a:.1f},{z:.1f}]，绑定窗口却来自 "
                                  f"[{wa:.1f},{wz:.1f}] —— 旁白与画面不是同一段")
            else:
                stats["src_checked"] += 1
        if mismatches > 6:
            errors.append(f"… 另有 {mismatches - 6} 句源片区间与窗口不符（典型症状：按序号均匀分配旁白）")

    hid = hook_window_ids(wins)
    if hid:
        lo, hi = hid[0], hid[-1]
        stats["hook_sents"] = sum(1 for s in sents
                                  if lo <= int(s.get("ws", -1)) <= hi)
    he, hw = check_hook_contract(wins, cfg,
                                 hook_sent_count=stats["hook_sents"] if hid else None)
    errors.extend(he)
    warnings.extend(hw)

    hid_set = set(hid)
    body = [float(w["src_start"]) for i, w in enumerate(wins) if i not in hid_set]
    stats["inversions"] = sum(1 for i in range(1, len(body))
                              if body[i] < body[i - 1] - 0.01)
    if stats["inversions"]:
        warnings.append(f"非钩子窗口源时间回跳 {stats['inversions']} 次（信息项：EP01 亦有 5 次"
                        "且 0 失配；src 契约已保证逐句绑定正确，故不阻断）")

    # src 契约成立时追加自洽断言：导演稿声称按剧情顺序，稿内源区间不得倒退。
    # 钩子是「提前复用」的后置爽点，源片时间天然晚于正片，必须跳过钩子块，
    # 否则每集都会在句 3（正片开头）误报一次回跳。
    if stats["contract"] == "src-v2" and stats["src_checked"] == len(sents):
        prev = -1.0
        hook_end = hid[-1] if hid else -1
        for k, s in enumerate(sents):
            if int(s.get("ws", -1)) <= hook_end:
                continue
            try:
                a = float(s["src"][0])
            except (KeyError, IndexError, TypeError, ValueError):
                break
            if a < prev - SRC_TOL_A:
                errors.append(f"句{k} 声明的源片区间回跳（{a:.1f} < {prev:.1f}）："
                              "导演稿声称按剧情顺序，正片稿内却倒退")
                break
            prev = a
    return errors, warnings, stats
