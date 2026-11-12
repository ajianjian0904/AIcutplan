# -*- coding: utf-8 -*-
"""L0·外部媒体工具定位：ffmpeg / ffprobe 的查找与常用探测。

所属层：L0 基础层 ｜ 职责：消除各脚本里重复的 ``shutil.which("ffmpeg")``
调用者：qc_visual / subs_extract / elastic_fit / prepare_media / 未来所有抽帧脚本

查找优先级（与旧 ``qc_visual.ffmpeg_bin()`` 完全一致，保证行为不变）::

    环境变量 NARR_FFMPEG  →  AICUTPLAN_FFMPEG  →  PATH  →  本机已知路径

注意：本机 ffmpeg 不在标准 PATH 时依赖最后的兜底路径；换机器请设置
``AICUTPLAN_FFMPEG`` / ``AICUTPLAN_FFPROBE`` 环境变量，不要改代码。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# 本机 WorkBuddy 托管运行时的 ffmpeg（换机器请用环境变量覆盖）
_FFMPEG_FALLBACK = r"C:\Users\10451\.workbuddy\binaries\ffmpeg\bin\ffmpeg.exe"
_FFPROBE_FALLBACK = r"C:\Users\10451\.workbuddy\binaries\ffmpeg\bin\ffprobe.exe"


def _bin(env_names: tuple[str, ...], exe: str, fallback: str) -> str:
    for name in env_names:
        v = os.environ.get(name)
        if v:
            return v
    found = shutil.which(exe)
    if found:
        return found
    return fallback if Path(fallback).exists() else exe


def ffmpeg_bin() -> str:
    """返回 ffmpeg 可执行文件路径。"""
    return _bin(("NARR_FFMPEG", "AICUTPLAN_FFMPEG"), "ffmpeg", _FFMPEG_FALLBACK)


def ffprobe_bin() -> str:
    """返回 ffprobe 可执行文件路径。"""
    return _bin(("NARR_FFPROBE", "AICUTPLAN_FFPROBE"), "ffprobe", _FFPROBE_FALLBACK)


def probe_json(video: str | Path, *, streams: bool = True) -> dict:
    """ffprobe 取流/格式信息，返回解析后的 dict。"""
    args = [ffprobe_bin(), "-v", "error"]
    if streams:
        args.append("-show_streams")
    args += ["-show_format", "-of", "json", str(video)]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {r.stderr[:200]}")
    import json
    return json.loads(r.stdout or "{}")


def probe_duration(video: str | Path) -> float:
    """容器时长（秒）。失败返回 0.0，不抛异常，方便调用方做兜底。"""
    try:
        info = probe_json(video, streams=False)
        return float(info.get("format", {}).get("duration") or 0.0)
    except (RuntimeError, ValueError, TypeError):
        return 0.0


def probe_streams(video: str | Path) -> list[dict]:
    """所有流信息（字幕/音轨/视频轨）。"""
    return probe_json(video).get("streams", [])


def extract_frame(video: str | Path, t: float, dst: str | Path, *,
                  width: int = 320, quality: int = 4) -> bool:
    """抽单帧为 JPEG。成功返回 True。

    调用方负责决定抽帧时间；暗帧回退、缓存失效等策略在 qc_visual 里。
    """
    p = Path(dst)
    p.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-ss", f"{float(t):.3f}",
         "-i", str(video), "-frames:v", "1",
         "-vf", f"scale={int(width)}:-2", "-q:v", str(int(quality)),
         "-y", str(p)],
        capture_output=True,
    )
    return r.returncode == 0 and p.exists() and p.stat().st_size > 300
