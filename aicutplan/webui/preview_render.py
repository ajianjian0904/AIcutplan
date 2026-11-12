#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [L3 界面层] 职责：会话块级 Δ 预览渲染 ｜ 调用者：server ｜ 产物：cache/preview_clips
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""临时渲染预览：按当前会话状态（块级 Δ + 句级锚点，无需写回）拼出片段。

只读工程素材，产物只写工作区 cache/preview_clips/。
画面 = 按刀的源区间切 out/01-video-no-ad.mp4 再拼接（cut.py 的真实输入）；
声音 = mix-rapid.wav 按成片区间截取（含旁白/原声/BGM 的最终混音）；
字幕 = 中文旁白 SRT 中落在该成片区间的条目，时间轴平移后烧录。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # 仓库根（aicutplan/<子包>/x.py → 上两级）
OUT = ROOT / "cache" / "preview_clips"
OUT.mkdir(parents=True, exist_ok=True)

SRT_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")

# 预览档位。默认 high：与最终成片同为 1280×720，实测只比 640 慢约 1 秒。
# 注意：这只是「临时预览」的转码参数，不影响 out/ 下的成片，也不影响剪映草稿。
QUALITY = {
    "high": {"scale": 1280, "crf": 18, "font": 34, "margin": 40, "name": "原画 1280×720"},
    "mid":  {"scale": 960,  "crf": 20, "font": 26, "margin": 30, "name": "高清 960×540"},
    "low":  {"scale": 640,  "crf": 24, "font": 20, "margin": 22, "name": "流畅 640×360"},
}


def sub_style(q: dict) -> str:
    return ("force_style='FontName=Microsoft YaHei,FontSize=%d,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "Bold=1,Outline=2,Shadow=1,Alignment=2,MarginV=%d'"
            % (q["font"], q["margin"]))


def _t(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _fmt(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = round((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def collect_materials(proj) -> dict:
    d = proj.dir / "out"
    audio = next((p for p in [d / "mix-rapid.wav", d / "03-rapid-narration.mp4"]
                  if p.is_file()), None)
    srt = next((p for p in [d / "中文旁白-recap.srt", d / "recap.srt"]
                if p.is_file()), None)
    return {"video": proj.video, "audio": audio, "srt": srt}


_FINAL_CACHE: dict[str, dict] = {}


def final_info(proj) -> dict:
    """最终成片规格（用于界面澄清：预览画质 ≠ 导出画质）。"""
    d = proj.dir / "out"
    p = next((x for x in [d / "03-rapid-narration-sub.mp4", d / "03-rapid-narration.mp4",
                          d / "02-video-rapid.mp4"] if x.is_file()), None)
    if p is None:
        return {"exists": False}
    ck = f"{p}|{int(p.stat().st_mtime)}"
    if ck in _FINAL_CACHE:
        return _FINAL_CACHE[ck]
    info = {"exists": True, "file": str(p), "name": p.name,
            "width": 0, "height": 0, "codec": "", "bitrate": 0}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,bit_rate",
             "-of", "json", str(p)], capture_output=True, text=True, timeout=20)
        s = (json.loads(r.stdout).get("streams") or [{}])[0]
        info.update({"codec": s.get("codec_name", ""),
                     "width": int(s.get("width", 0)),
                     "height": int(s.get("height", 0)),
                     "bitrate": int(s.get("bit_rate", 0) or 0)})
    except Exception:
        pass
    _FINAL_CACHE.clear()
    _FINAL_CACHE[ck] = info
    return info


def _srt_slice(srt: Path, a: float, z: float, dst: Path) -> int:
    """取成片区间 [a,z) 内的字幕，时间平移到 0 起，写到 dst；返回条数。"""
    try:
        text = srt.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = srt.read_text(encoding="gbk", errors="ignore")
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    kept = []
    for blk in blocks:
        mm = SRT_TS.search(blk)
        if not mm:
            continue
        g = mm.groups()
        t0, t1 = _t(*g[:4]), _t(*g[4:])
        if t1 < a - 0.01 or t0 > z + 0.01:
            continue
        lines = blk.splitlines()
        body = "\n".join(x for x in lines if not SRT_TS.search(x) and x.strip() and not x.strip().isdigit())
        if not body.strip():
            continue
        kept.append(f"{len(kept) + 1}\n{_fmt(max(0, t0 - a))} --> {_fmt(min(z - a, t1 - a))}\n{body}\n")
    dst.write_text("\n".join(kept), encoding="utf-8")
    return len(kept)


def cache_key(proj, specs, mat, new_a, new_z, subs, audio, scope, quality: str) -> str:
    raw = json.dumps({
        "dir": str(proj.dir), "specs": [[round(x["a"], 3), round(x["z"], 3)] for x in specs],
        "new": [round(new_a, 3), round(new_z, 3)],
        "audio": str(mat["audio"]) if audio else "", "srt": str(mat["srt"]) if subs else "",
        "scope": scope, "quality": quality,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()[:20]


def render(proj, specs: list, new_a: float, new_z: float,
           subs: bool = True, audio: bool = True, scale: int | None = None,
           scope: str = "beat", force: bool = False, quality: str = "high") -> dict:
    """specs = [{'a':源片起, 'z':源片止, 'dur':成片刀长}, ...]（按成片顺序）

    quality 只影响「临时预览」的转码，不写工程、不参与最终成片。
    """
    if not specs:
        raise ValueError("没有可渲染的刀")
    if not proj.video.is_file():
        raise RuntimeError("源视频不存在：" + str(proj.video))
    q = QUALITY.get(quality, QUALITY["high"])
    scale = int(scale or q["scale"])
    crf = int(q["crf"])
    font = max(16, round(q["font"] * scale / q["scale"]))
    style = sub_style({**q, "font": font})
    mat = collect_materials(proj)
    want_audio = bool(audio and mat["audio"] and mat["audio"].is_file())
    want_subs = bool(subs and mat["srt"] and mat["srt"].is_file())
    key = cache_key(proj, specs, mat, new_a, new_z, want_subs, want_audio, scope, quality)
    out = OUT / f"{key}.mp4"
    dur = round(sum(float(x["dur"]) for x in specs), 3)
    meta = {"ok": True, "key": key, "url": f"/api/preview/file?k={key}",
            "file": str(out), "cuts": len(specs), "duration": dur,
            "scope": scope, "subs": want_subs, "audio": want_audio,
            "quality": quality, "quality_name": q["name"],
            "width": scale, "height": round(scale * 9 / 16), "crf": crf,
            "new": [round(new_a, 3), round(new_z, 3)],
            "material": {"video": str(mat["video"]),
                         "audio": str(mat["audio"]) if want_audio else None,
                         "srt": str(mat["srt"]) if want_subs else None}}
    if out.is_file() and out.stat().st_size > 20000 and not force:
        poster = OUT / f"{key}.jpg"
        meta["poster"] = f"/api/preview/poster?k={key}" if poster.is_file() else ""
        meta.update({"cached": True, "elapsed": 0.0, "subs_fallback": False,
                     "size": out.stat().st_size})
        return meta

    t0 = time.time()
    tmpdir = OUT
    tmp = OUT / f"{key}.tmp.mp4"
    sub_file = OUT / f"{key}.srt"
    n_sub = _srt_slice(mat["srt"], new_a, new_z, sub_file) if want_subs else 0
    if want_subs and n_sub == 0:
        want_subs = False

    cmd = ["ffmpeg", "-v", "error", "-y"]
    for x in specs:
        cmd += ["-ss", f"{float(x['a']):.3f}", "-t", f"{float(x['z']) - float(x['a']):.3f}",
                "-i", str(mat["video"])]
    n = len(specs)
    if want_audio:
        cmd += ["-ss", f"{new_a:.3f}", "-t", f"{dur:.3f}", "-i", str(mat["audio"])]

    parts = []
    for i, x in enumerate(specs):
        parts.append(f"[{i}:v]fps=24,scale={scale}:-2,setsar=1,"
                     f"trim=duration={float(x['dur']):.4f},setpts=PTS-STARTPTS[v{i}]")
    chain = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat]"
    parts.append(chain)
    last = "cat"
    if want_subs:
        parts.append(f"[cat]subtitles={sub_file.name}:{style}[vsub]")
        last = "vsub"
    cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]"]
    if want_audio:
        cmd += ["-map", f"{n}:a", "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", str(tmp)]

    def run(c):
        return subprocess.run(c, capture_output=True, timeout=600, cwd=str(tmpdir))

    r = run(cmd)
    subs_fallback = False
    if r.returncode != 0 and want_subs:
        # 字幕滤镜（字体/路径）失败时降级为无字幕，不丢整段预览
        subs_fallback = True
        cmd2 = list(cmd)
        fi = cmd2.index("-filter_complex")
        parts2 = parts[:-1]          # 去掉 subtitles 那段
        cmd2[fi + 1] = ";".join(parts2)
        mi = cmd2.index("-map")
        cmd2[mi + 1] = "[cat]"
        r = run(cmd2)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 10000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("渲染失败：" + r.stderr.decode("utf-8", "ignore")[-400:])
    os.replace(tmp, out)
    # 从成片里抽一张真实首帧当视频海报：播放器未点播放时也有画面可看
    poster = OUT / f"{key}.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.4", "-i", str(out),
                    "-frames:v", "1", "-vf", f"scale={scale}:-2", "-q:v", "3",
                    "-y", str(poster)], capture_output=True, timeout=60)
    meta["poster"] = f"/api/preview/poster?k={key}" if poster.is_file() else ""
    meta.update({"cached": False, "elapsed": round(time.time() - t0, 1),
                 "subs": want_subs and not subs_fallback, "subs_fallback": subs_fallback,
                 "sub_count": n_sub, "size": out.stat().st_size})
    return meta


def list_previews() -> list:
    out = []
    for p in sorted(OUT.glob("*.mp4"), key=lambda x: -x.stat().st_mtime):
        out.append({"file": str(p), "size": p.stat().st_size,
                    "key": p.stem, "url": f"/api/preview/file?k={p.stem}"})
    return out[:20]


def _check_key(key: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{6,32}", key or ""):
        raise ValueError("非法的预览文件名")
    return key


def file_for(key: str) -> Path:
    p = OUT / f"{_check_key(key)}.mp4"
    if not p.is_file():
        raise FileNotFoundError(key)
    return p


def poster_for(key: str) -> Path:
    p = OUT / f"{_check_key(key)}.jpg"
    if not p.is_file():
        raise FileNotFoundError(key)
    return p
