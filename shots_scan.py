# -*- coding: utf-8 -*-
# [L1 旧链] 职责：镜头切分 ｜ 调用者：pipeline:2 ｜ 产物：shotlist.csv
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""镜头切分：场景变化检测 → shotlist.csv（idx,start,end,dur,motion,bright,ost）

做法：
  1) ffmpeg 的 select='gt(scene,T)' 逐帧算场景分数，命中帧的 pts_time 就是切点；
  2) 切点之间的间隔小于 --min-dur 的合并（一闪而过的转场不单独成镜）；
  3) 音频按镜算 RMS，高于 --ost-db 记为「有原声」，给混音与校对做参考；
  4) 同时刷新 metrics.json 的镜头统计（cuts_per_min / 时长分位 / 直方图）。

只写 shotlist.csv 与 metrics.json，不改源片、不动 cutplan。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

PTS = re.compile(r"pts_time:\s*([0-9]+\.?[0-9]*)")


def pick_source(d: Path, pj: dict) -> Path:
    a = d / "out" / "01-video-no-ad.mp4"
    if a.exists() and a.stat().st_size > 1024:
        return a
    s = Path(pj.get("source_video") or d / "ref.mp4")
    return s if s.is_absolute() else (d / s).resolve()


def duration(src: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(src)],
                           capture_output=True, timeout=30)
        return float(r.stdout.decode().strip() or 0)
    except Exception:
        return 0.0


def detect_cuts(src: Path, th: float) -> list[float]:
    argv = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-an",
            "-filter:v", f"select='gt(scene,{th})',showinfo", "-f", "null", "-"]
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    cuts: list[float] = []
    last = -1.0
    next_pct = 5
    for raw in p.stderr or []:
        line = raw.decode("utf-8", "replace")
        m = PTS.search(line)
        if m:
            t = float(m.group(1))
            if t - last > 0.04:
                cuts.append(t)
                last = t
                if len(cuts) % 50 == 0:
                    print(f"[切分] 已检测 {len(cuts)} 个切点 · 到 {t:.1f}s", flush=True)
            continue
        mm = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
        if mm:
            sec = int(mm.group(1)) * 3600 + int(mm.group(2)) * 60 + float(mm.group(3))
            pct = sec / max(1e-6, dur_cache[0]) * 100
            if pct >= next_pct:
                print(f"[切分] 分析中 {pct:.0f}%", flush=True)
                next_pct += 5
    p.wait()
    return cuts


dur_cache = [0.0]


def rms_db(audio: Path, bounds: list[float]) -> list[float]:
    """每个镜头区间的平均能量（dBFS）。流式读 s16le，避免整段驻留。"""
    argv = ["ffmpeg", "-v", "error", "-i", str(audio), "-vn", "-ac", "1",
            "-ar", "8000", "-f", "s16le", "-"]
    try:
        raw = subprocess.run(argv, capture_output=True, timeout=1800).stdout
    except Exception:
        return [0.0] * (len(bounds) - 1)
    import array
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    sr = 8000
    out = []
    for i in range(len(bounds) - 1):
        i0 = max(0, int(bounds[i] * sr))
        i1 = min(len(a), int(bounds[i + 1] * sr))
        if i1 <= i0:
            out.append(-99.0)
            continue
        step = max(1, (i1 - i0) // 4000)  # 每镜最多采 4000 点，长镜头不至于卡
        seg = a[i0:i1:step]
        s = 0
        n = 0
        for v in seg:
            s += v * v
            n += 1
        ms = s / max(1, n)
        out.append(round(10 * math.log10(ms / (32768.0 ** 2) + 1e-12), 2))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--scene", type=float, default=0.30, help="场景阈值，低=切得碎，高=会漏")
    ap.add_argument("--min-dur", type=float, default=0.12, help="短于此的镜头合并")
    ap.add_argument("--ost-db", type=float, default=-38.0, help="高于此能量记为有原声")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    pj_f = Path(a.project).expanduser()
    pj = json.loads(pj_f.read_text(encoding="utf-8"))
    d = pj_f.parent
    src = pick_source(d, pj)
    if not src.exists():
        print(f"[切分] 找不到源片：{src}", flush=True)
        return 2
    if not a.force and (d / "shotlist.csv").exists() and "--redo" not in sys.argv:
        pass  # 仍然重算：切分很便宜，重算比缓存不一致更安全

    dur_cache[0] = duration(src)
    print(f"[切分] 源片 {src.name} · {dur_cache[0]:.1f}s · 场景阈值 {a.scene}", flush=True)
    cuts = detect_cuts(src, a.scene)
    print(f"[切分] 检测到 {len(cuts)} 个切点", flush=True)

    bounds = [0.0]
    for t in cuts:
        if t - bounds[-1] >= a.min_dur and t < dur_cache[0] - 0.05:
            bounds.append(round(t, 3))
    bounds.append(round(dur_cache[0], 3))
    if len(bounds) < 3:
        print("[切分] 切点太少，阈值可能过高（试 --scene 0.15）", flush=True)
        return 3

    audio = d / "audio" / "base-no-ad.wav"
    if not audio.exists():
        audio = src
    print(f"[切分] 按镜算能量（{len(bounds) - 1} 镜）…", flush=True)
    dbs = rms_db(audio, bounds)

    rows = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        rows.append({"idx": i + 1, "start": f"{s:.2f}", "end": f"{e:.2f}",
                     "dur": f"{e - s:.2f}", "motion": "0.0", "bright": "0.0",
                     "ost": "1" if dbs[i] > a.ost_db else "0"})

    f = d / "shotlist.csv"
    if f.exists():
        bak = f.with_suffix(f".csv.bak.{__import__('time').strftime('%Y%m%d_%H%M%S')}")
        bak.write_bytes(f.read_bytes())
        print(f"[切分] 已备份旧表 → {bak.name}", flush=True)
    with f.open("w", encoding="utf-8-sig", newline="") as fh:
        fh.write("idx,start,end,dur,motion,bright,ost\n")
        for r in rows:
            fh.write(",".join(str(r[k]) for k in
                              ("idx", "start", "end", "dur", "motion", "bright", "ost")) + "\n")

    durs = sorted(float(r["dur"]) for r in rows)
    n = len(durs)
    p90 = durs[min(n - 1, int(n * 0.9))]
    hist = {"0-0.5": 0, "0.5-1": 0, "1-2": 0, "2-3": 0, "3-4": 0, "4-99": 0}
    for v in durs:
        k = ("0-0.5" if v < 0.5 else "0.5-1" if v < 1 else "1-2" if v < 2
             else "2-3" if v < 3 else "3-4" if v < 4 else "4-99")
        hist[k] += 1
    mf = d / "metrics.json"
    metrics = {}
    if mf.exists():
        try:
            metrics = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            metrics = {}
    metrics.update({
        "shots": n,
        "cuts_per_min": round(n / max(1e-6, dur_cache[0] / 60), 1),
        "shot_median": round(durs[n // 2], 2),
        "shot_p90": round(p90, 2),
        "shot_max": round(durs[-1], 2),
        "dur_hist": {k: round(v * 100 / n, 1) for k, v in hist.items()},
    })
    mf.write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")

    # 有效段：默认全片，界面上可再裁
    rc = pj.setdefault("recap", {})
    if not rc.get("valid"):
        rc["valid"] = f"0-{dur_cache[0]:.0f}"
        pj_f.write_text(json.dumps(pj, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[切分] 写出 {n} 镜 · 中位 {metrics['shot_median']}s · p90 {metrics['shot_p90']}s · "
          f"{metrics['cuts_per_min']} 切/分 · 有原声 {sum(1 for r in rows if r['ost'] == '1')} 镜", flush=True)
    print("[切分] 完成。下一步：语音识别", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
