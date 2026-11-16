# -*- coding: utf-8 -*-
"""L1·数值断言：旁白里的数字必须能在它覆盖的窗口台词里找到依据。

所属层：L1 共享层 ｜ 职责：把「十三万点 / 一万三千点」这类事实错误变成可打印的告警
调用者：``python -m aicutplan check``、``cf_pipeline``（lint 之后）、
        ``aicutplan.shared.number_frames``（按命中句抽高清帧）
副作用：只读

为什么只认台词、不认画面：**剧情事实以人物台词为准**。画面浮字多半是「过程中的数据」
（强化增量 ``+2750``、剩余点数 ``3033``、临时倍率），同一集里可以有好几套口径，
拿它去判旁白对错会大量误报。所以这里只回答「这个数字在台词里有没有依据」；
要读画面字用 :mod:`aicutplan.shared.number_frames`（1280 宽帧）或
:mod:`aicutplan.shared.ocr_numbers`（OCR 参考）。

注意：本模块**只提醒不阻断**。旁白完全可以用画面浮字里的数字（那是观众也看到的），
这里只是让人知道「这个数不是台词给的」，从而判断要不要核对画面。
"""
from __future__ import annotations

from pathlib import Path

from aicutplan.contracts import num_values
from aicutplan.io import load_json

#: 读画面数字时取句首窗的哪一段（与 qc_visual 的取帧口径一致：窗内前 1.2s 的中点）
FRAME_PROBE = 1.2


def _ja_paras(proj_dir: Path) -> list[dict]:
    """台词源（源片秒）：优先内嵌字幕产物，其次 ASR 结果。"""
    j = proj_dir / "json"
    for name in ("ja-segments.json", "asr_ja.json"):
        data = load_json(j / name, {}) or {}
        paras = data.get("paras") or []
        if paras:
            return [{"t0": float(p.get("t0") or 0), "t1": float(p.get("t1") or 0),
                     "text": str(p.get("text") or "")} for p in paras]
    return []


def _zh_paras(proj_dir: Path) -> list[dict]:
    """译中结果（按索引对齐 ja paras），读不到就返回空。"""
    try:
        from aicutplan.shared import asr_pipeline as AP
        shim = type("P", (), {"dir": proj_dir})()
        data = load_json(AP.out_path(shim), {}) or {}
        return [{"t0": float(p.get("t0") or 0), "t1": float(p.get("t1") or 0),
                 "text": str(p.get("text") or "")} for p in (data.get("paras") or [])]
    except Exception:
        return []


def _fmt(vals: list[float]) -> str:
    return " / ".join("%g" % v for v in vals)


def check_details(proj_dir: str | Path) -> list[dict]:
    """返回命中数值断言的句子（结构化），供告警与高清抽帧共用。

    每项：``{i, text, missing, lo, hi, t_out, t_src}``
    - ``t_out``：成片时间轴上的取帧时刻（句首窗内前 1.2s 的中点）
    - ``t_src``：源片时间轴上的同一时刻（成片不存在时用源片抽帧）
    """
    pj = Path(proj_dir)
    j = pj / "json"
    narr = (load_json(j / "narration.json", {}) or {}).get("items") or []
    wins = (load_json(j / "cutplan.json", {}) or {}).get("windows") or []
    swi = (load_json(j / "align.json", {}) or {}).get("sent_win_idx") or {}
    if not (narr and wins and swi):
        return []

    paras = _ja_paras(pj) + _zh_paras(pj)
    if not paras:
        return []

    rows: list[dict] = []
    for it in narr:
        text = str(it.get("text") or "")
        vals = num_values(text)
        if not vals:
            continue
        key = str(it.get("i"))
        if key not in swi:
            continue
        try:
            lo, hi = int(swi[key][0]), int(swi[key][1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (0 <= lo < len(wins) and 0 <= hi < len(wins)):
            continue
        w0 = wins[lo]
        t0 = float(w0.get("src_start") or 0)
        t1 = float(wins[hi].get("src_end") or 0)
        spoken: set[float] = set()
        for p in paras:
            if p["t1"] > t0 - 0.01 and p["t0"] < t1 + 0.01:
                spoken |= num_values(p["text"])
        missing = sorted(v for v in vals if v not in spoken)
        if not missing:
            continue
        # 取帧点：句首窗内前 1.2s 的中点（跨 6 窗的句子也不会落到后半段画面）
        out_start = float(w0.get("new_start") or 0)
        out_end = float(wins[hi].get("new_end") or 0)
        out_mid = out_start + min(out_end - out_start, FRAME_PROBE) / 2
        src_mid = t0 + min(float(w0.get("src_end") or 0) - t0, FRAME_PROBE) / 2
        rows.append({"i": int(it.get("i")), "text": text, "missing": missing,
                     "lo": lo, "hi": hi,
                     "t_out": round(out_mid, 3), "t_src": round(src_mid, 3),
                     "out_span": [round(out_start, 3), round(out_end, 3)],
                     "src_span": [round(t0, 3), round(t1, 3)]})
    return rows


def check(proj_dir: str | Path) -> list[str]:
    """返回告警清单（空 = 没有发现问题或缺少必要产物）。"""
    out = []
    for d in check_details(proj_dir):
        out.append(f"句{d['i']} 数值 {_fmt(d['missing'])} 不在窗口{d['lo']}-{d['hi']} 的台词里"
                   f"（旁白：{d['text'][:16]}）")
    return out


def format_warnings(rows: list[str], *, limit: int = 6) -> str:
    """把告警格式化成几行；空清单返回空字符串。"""
    if not rows:
        return ""
    head = f"[数值断言] {len(rows)} 句的数字在台词里找不到依据（只提醒：可能是画面浮字或过程数据）："
    body = ["  ! " + r for r in rows[:limit]]
    if len(rows) > limit:
        body.append(f"  … 另有 {len(rows) - limit} 句")
    return "\n".join([head, *body])
