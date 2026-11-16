# -*- coding: utf-8 -*-
"""L1·数值断言的高清帧：对命中数值断言的句子抽 1280 宽帧，交给视觉模型读画面数字。

所属层：L1 共享层 ｜ 职责：把「画面浮字到底写的几千」变成可读的图，不再靠 320px 猜
调用者：``python -m aicutplan check --number-frames``
副作用：写 ``<工程>/.numframes/``（jpg + `_manifest.json`）

为什么单独抽：段级/质检帧都是 320px，画面浮字（``+5154``、``残り3033``）在那种宽度下
根本不可读；这层的帧只用于「人/模型核对数字」，所以宽 1280，且只抽命中句（EP01 是 7 句）。

取帧点：一句跨多窗时，数字可能出现在句中或句末，所以按时长取 1–3 个点
（≤2s 只取句首窗中点；>2s 追加句子中点与末窗回退 0.4s），实测 7 句 → 21 张。
只取句首窗中点会漏掉「数值浮字在动作高潮处出现」的情况（EP01 句23 就是）。

缓存：清单记 ``sig``（源片/成片指纹 + 宽度 + 时间基准 + 每个取帧点），命中即复用；
文件名带指纹前缀，换片/改窗口后旧图不会被误用。
"""
from __future__ import annotations

from pathlib import Path

from aicutplan import media
from aicutplan.io import dump_json, load_json
from aicutplan.shared import number_claims as NC

#: 核对数字用的帧宽：源片是 1080p，1280 足以看清大号浮字
FRAME_WIDTH = 1280
#: JPEG 质量（1 最好，31 最差）：数字边缘不能糊
FRAME_QUALITY = 3
#: 抽帧口径版本：改取帧点或宽度算法时 +1
FRAMES_VER = "numframes-v2"


def _source_video(d: Path) -> Path:
    pj = load_json(d / "project.json", {}) or {}
    raw = Path(pj.get("source_video") or d / "ref.mp4")
    return raw if raw.is_absolute() else (d / raw).resolve()


def pick_video(proj_dir: str | Path) -> tuple[Path, str]:
    """选抽帧源：成片优先（与质检看的是同一画面），否则退回源片。

    返回 ``(路径, 时间基准)``，基准为 ``"output"`` 或 ``"source"`` —— 两者时间轴不同，
    取帧时刻必须跟着切换，否则会抽到完全无关的画面。
    """
    d = Path(proj_dir)
    for rel in ("out/03-rapid-narration.mp4", "out/02-video-rapid-silent.mp4"):
        p = d / rel
        if p.exists() and p.stat().st_size > 1024:
            return p, "output"
    return _source_video(d), "source"


def sample_points(det: dict, base: str) -> list[float]:
    """一句的取帧时刻（1–3 个）。

    - ≤2s：只取句首窗中点（短句一帧足够）
    - >2s：追加「句子中点」与「末窗回退 0.4s」——数值浮字常出现在动作高潮处
    """
    first = det["t_out"] if base == "output" else det["t_src"]
    start, end = det["out_span"] if base == "output" else det["src_span"]
    if end - start <= 2.0:
        return [round(float(first), 3)]
    pts = [round(float(first), 3),
           round(start + (end - start) / 2, 3),
           round(max(start, end - 0.4), 3)]
    return sorted(set(pts))


def _sig(video: Path, width: int, base: str, plan: list[tuple]) -> str:
    from aicutplan import cache as CL
    return CL.content_sig(FRAMES_VER, CL.file_sig(video), int(width), base, plan)


#: 密集补抽：1 帧/秒；单句上限（超过就只取前 12 秒，避免长句把帧数打爆）
DENSE_FPS = 1.0
DENSE_MAX = 12


def dense_points(det: dict, base: str, *,
                 fps: float = DENSE_FPS, max_frames: int = DENSE_MAX) -> list[float]:
    """一句的密集取帧时刻（1 帧/秒，最多 ``max_frames`` 张）。

    用于「三帧没读到数字」的句子：浮字常常只闪 0.5 秒，稀疏取样必然漏。
    起点在句子窗口内偏移半帧，避免正好落在转场黑帧上。
    """
    start, end = det["out_span"] if base == "output" else det["src_span"]
    step = 1.0 / max(0.1, float(fps))
    pts: list[float] = []
    t = start + min(step, max(0.0, end - start)) / 2
    while t < end and len(pts) < max(0, int(max_frames)):
        pts.append(round(float(t), 3))
        t += step
    if not pts:
        pts = [round(float(det["t_out"] if base == "output" else det["t_src"]), 3)]
    return pts


def extract_dense(proj_dir: str | Path, ids, *, width: int = FRAME_WIDTH,
                  quality: int = FRAME_QUALITY, force: bool = False,
                  fps: float = DENSE_FPS, max_frames: int = DENSE_MAX) -> list[dict]:
    """对指定句子做密集补抽（独立清单 ``_dense.json``，不动稀疏帧的缓存）。

    ``ids`` 为句子号集合；只对这些句子抽，避免整集铺开。
    """
    want_ids = {int(i) for i in ids}
    details = [d for d in NC.check_details(proj_dir) if int(d["i"]) in want_ids]
    if not details:
        return []
    video, base = pick_video(proj_dir)
    if not video.exists():
        return []

    plan = [(det["i"], t) for det in details
            for t in dense_points(det, base, fps=fps, max_frames=max_frames)]
    by_i = {det["i"]: det for det in details}

    d = Path(proj_dir) / ".numframes"
    d.mkdir(parents=True, exist_ok=True)
    want = _sig(video, width, base, plan)
    mf = d / "_dense.json"
    cached = load_json(mf, {}) or {}
    if not force and cached.get("sig") == want and cached.get("items"):
        rows, ok = [], True
        for it in cached["items"]:
            p = d / str(it["name"])
            if not (p.exists() and p.stat().st_size > 300):
                ok = False
                break
            rows.append({**it, "path": str(p)})
        if ok:
            return rows

    rows = []
    for i, t in plan:
        det = by_i[i]
        f = d / f"numd_{i:04d}_{int(round(t * 100)):07d}_{want[:6]}.jpg"
        if force or not f.exists() or f.stat().st_size <= 300:
            media.extract_frame(video, t, f, width=width, quality=quality)
        if f.exists() and f.stat().st_size > 300:
            rows.append({"i": i, "text": det["text"], "missing": det["missing"],
                         "t": round(float(t), 3), "name": f.name, "path": str(f),
                         "kind": "dense"})

    dump_json(mf, {"sig": want, "video": str(video), "base": base, "width": int(width),
                   "fps": float(fps), "max_frames": int(max_frames),
                   "items": [{"i": r["i"], "text": r["text"], "missing": r["missing"],
                              "t": r["t"], "name": r["name"]} for r in rows]})
    return rows


def extract(proj_dir: str | Path, *, width: int = FRAME_WIDTH,
            quality: int = FRAME_QUALITY, force: bool = False) -> list[dict]:
    """对命中数值断言的句子抽高清帧。

    返回每个取帧点一行：``{i, text, missing, t, name, path}``（同一句可能 1–3 行）。
    """
    details = NC.check_details(proj_dir)
    if not details:
        return []
    video, base = pick_video(proj_dir)
    if not video.exists():
        return []

    # 先把「哪句、哪个时刻」列成计划，指纹与文件名都基于它
    plan = [(det["i"], t) for det in details for t in sample_points(det, base)]
    by_i = {det["i"]: det for det in details}

    d = Path(proj_dir) / ".numframes"
    d.mkdir(parents=True, exist_ok=True)
    want = _sig(video, width, base, plan)
    mf = d / "_manifest.json"
    cached = load_json(mf, {}) or {}
    if not force and cached.get("sig") == want and cached.get("items"):
        rows, ok = [], True
        for it in cached["items"]:
            p = d / str(it["name"])
            if not (p.exists() and p.stat().st_size > 300):
                ok = False
                break
            rows.append({**it, "path": str(p)})
        if ok:
            return rows

    rows = []
    for i, t in plan:
        det = by_i[i]
        f = d / f"num_{i:04d}_{int(round(t * 100)):07d}_{want[:6]}.jpg"
        if force or not f.exists() or f.stat().st_size <= 300:
            media.extract_frame(video, t, f, width=width, quality=quality)
        if f.exists() and f.stat().st_size > 300:
            rows.append({"i": i, "text": det["text"], "missing": det["missing"],
                         "t": round(float(t), 3), "name": f.name, "path": str(f)})

    dump_json(mf, {"sig": want, "video": str(video), "base": base, "width": int(width),
                   "items": [{"i": r["i"], "text": r["text"], "missing": r["missing"],
                              "t": r["t"], "name": r["name"]} for r in rows]})
    return rows


def format_rows(rows: list[dict]) -> str:
    """按句分组列清单；空清单返回空字符串。"""
    if not rows:
        return ""
    grouped: dict[int, list[dict]] = {}
    for r in rows:
        grouped.setdefault(int(r["i"]), []).append(r)
    head = (f"[数值帧] {len(rows)} 张高清帧（宽 {FRAME_WIDTH}，{len(grouped)} 句）"
            "——用 read_image 看画面数字，再定稿：")
    lines = [head]
    for i in sorted(grouped):
        first = grouped[i][0]
        nums = ", ".join("%g" % v for v in first.get("missing") or [])
        lines.append(f"  句{i} 疑似 {nums}（{len(grouped[i])} 帧）")
        for r in grouped[i]:
            lines.append(f"    {r['path']}  t={r['t']:.2f}s")
    return "\n".join(lines)
