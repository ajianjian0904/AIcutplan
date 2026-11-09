# -*- coding: utf-8 -*-
"""洗片：源片 → out/01-video-no-ad.mp4 + audio/base-no-ad.wav + metrics.json

只做「流复制 + 抽音 + 探针」，不重编码，几秒到几十秒完成。
不改源视频、不删任何文件；已有产物默认跳过（--force 重做）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def sh(argv: list[str]) -> int:
    print("$ " + " ".join(argv), flush=True)
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for raw in p.stdout or []:
        line = raw.decode("utf-8", "replace").rstrip()
        if line and not line.startswith("frame="):
            print(line, flush=True)
    return p.wait()


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

    out_v = d / "out" / "01-video-no-ad.mp4"
    if a.force or not out_v.exists() or out_v.stat().st_size < 1024:
        rc = sh(["ffmpeg", "-y", "-v", "error", "-ignore_editlist", "1", "-i", str(src),
                 "-c", "copy", "-movflags", "+faststart", str(out_v)])
        if rc != 0:
            print("[洗片] 流复制失败，退回重编码", flush=True)
            rc = sh(["ffmpeg", "-y", "-v", "error", "-ignore_editlist", "1", "-i", str(src),
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                     "-c:a", "aac", "-b:a", "192k", str(out_v)])
        if rc != 0:
            return rc
    print(f"[洗片] 画面 → out/01-video-no-ad.mp4 ({out_v.stat().st_size / 1048576:.0f}MB)", flush=True)

    wav = d / "audio" / "base-no-ad.wav"
    if a.force or not wav.exists() or wav.stat().st_size < 1024:
        rc = sh(["ffmpeg", "-y", "-v", "error", "-ignore_editlist", "1", "-i", str(src),
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
