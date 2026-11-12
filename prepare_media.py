# -*- coding: utf-8 -*-
# [L1 旧链] 职责：洗片去广告/转封装 ｜ 调用者：pipeline:1 ｜ 产物：out/01-video-no-ad.mp4
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""洗片：源片 → out/01-video-no-ad.mp4 + audio/base-no-ad.wav + metrics.json

只做「流复制 + 抽音 + 探针」，不重编码，几秒到几十秒完成。
不改源视频、不删任何文件；已有产物默认跳过（--force 重做）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def sh(argv: list[str], total: float = 0.0, tag: str = "") -> int:
    """跑 ffmpeg 并把 time= 进度折成百分比，每 5% 打一行（日志别刷屏）。"""
    print("$ " + " ".join(argv), flush=True)
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    next_pct, last = 5, ""
    for raw in p.stdout or []:
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        last = line
        m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
        if m and total > 0:
            sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            pct = sec / total * 100
            if pct >= next_pct:
                print(f"[洗片] {tag} {pct:.0f}% · {sec:.0f}/{total:.0f}s", flush=True)
                next_pct += 5
            continue
        if not line.startswith("frame=") and "speed=" not in line:
            print(line, flush=True)
    rc = p.wait()
    if rc != 0 and last:
        print(f"[洗片] ffmpeg 最后输出：{last}", flush=True)
    return rc


def needs_transcode(info: dict) -> tuple[bool, str]:
    """能不能直接流复制进 mp4：10bit / 非 h264 / 带 ass 字幕轨都不行。"""
    pix = (info.get("pix_fmt") or "").lower()
    if "10" in pix or "12" in pix:
        return True, f"10bit（{pix}）浏览器与剪映兼容差"
    if info.get("vcodec") not in ("h264",):
        return True, f"编码 {info.get('vcodec')} 不在 mp4 通用范围"
    return False, ""


def probe(src: Path) -> dict:
    argv = ["ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(src)]
    r = subprocess.run(argv, capture_output=True, timeout=60)
    info = json.loads(r.stdout.decode("utf-8", "ignore") or "{}")
    fmt = info.get("format") or {}
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})

    def fr(s):
        try:
            n, d = s.split("/")
            return round(float(n) / max(1e-9, float(d)), 3)
        except Exception:
            return 0.0

    return {
        "duration": round(float(fmt.get("duration") or 0), 3),
        "size": int(fmt.get("size") or 0),
        "bitrate": int(fmt.get("bit_rate") or 0),
        "w": int(v.get("width") or 0), "h": int(v.get("height") or 0),
        "fps": fr(v.get("r_frame_rate") or "0/1"),
        "vcodec": v.get("codec_name") or "",
        "pix_fmt": v.get("pix_fmt") or "",
        "profile": v.get("profile") or "",
        "nb_frames": int(v.get("nb_frames") or 0) if str(v.get("nb_frames", "")).isdigit() else 0,
        "acodec": a.get("codec_name") or "", "sr": int(a.get("sample_rate") or 0),
        "ch": int(a.get("channels") or 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    pj_f = Path(a.project).expanduser()
    pj = json.loads(pj_f.read_text(encoding="utf-8"))
    d = pj_f.parent
    src = Path(pj.get("source_video") or d / "ref.mp4")
    if not src.is_absolute():
        src = (d / src).resolve()
    if not src.exists():
        print(f"[洗片] 源视频不存在：{src}", flush=True)
        return 2
    for sub in ("out", "audio", "json", "tts", "logs", "verify"):
        (d / sub).mkdir(parents=True, exist_ok=True)

    info = probe(src)
    print(f"[洗片] 源片 {src.name} · {info['duration']:.1f}s · {info['w']}×{info['h']} · "
          f"{info['fps']}fps · {info['size'] / 1048576:.0f}MB", flush=True)

    editlist = ["-ignore_editlist", "1"] if src.suffix.lower() in (".mp4", ".mov", ".m4v") else []
    out_v = d / "out" / "01-video-no-ad.mp4"
    if a.force or not out_v.exists() or out_v.stat().st_size < 1024:
        tc, why = needs_transcode(info)
        if tc:
            print(f"[洗片] 需要转码：{why} → H.264 8bit（流复制进 mp4 会不可播）", flush=True)
        else:
            rc = sh(["ffmpeg", "-y", "-v", "error", *editlist, "-i", str(src),
                     "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-sn",
                     "-movflags", "+faststart", str(out_v)], info["duration"], "复制")
            if rc != 0:
                print("[洗片] 流复制失败，退回重编码", flush=True)
                tc = True
        if tc or not out_v.exists() or out_v.stat().st_size < 1024:
            rc = sh(["ffmpeg", "-y", "-v", "error", *editlist, "-i", str(src),
                     "-map", "0:v:0", "-map", "0:a:0?", "-sn",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                     "-movflags", "+faststart", str(out_v)], info["duration"], "转码")
            if rc != 0:
                return rc
    print(f"[洗片] 画面 → out/01-video-no-ad.mp4 ({out_v.stat().st_size / 1048576:.0f}MB)", flush=True)

    wav = d / "audio" / "base-no-ad.wav"
    if a.force or not wav.exists() or wav.stat().st_size < 1024:
        rc = sh(["ffmpeg", "-y", "-v", "error", *editlist, "-i", str(src),
                 "-vn", "-ac", "2", "-ar", "48000", str(wav)])
        if rc != 0:
            return rc
    print(f"[洗片] 音轨 → audio/base-no-ad.wav ({wav.stat().st_size / 1048576:.0f}MB)", flush=True)

    mf = d / "metrics.json"
    metrics = {}
    if mf.exists():
        try:
            metrics = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            metrics = {}
    metrics["source"] = info
    mf.write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[洗片] 探针 → metrics.json", flush=True)

    pj.setdefault("recap", {}).setdefault("src_dur", info["duration"])
    pj["recap"]["src_dur"] = info["duration"]
    pj["fps"] = pj.get("fps") or (24 if abs(info["fps"] - 23.976) < 0.1 else round(info["fps"]))
    pj_f.write_text(json.dumps(pj, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[洗片] 完成。下一步：镜头切分", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
