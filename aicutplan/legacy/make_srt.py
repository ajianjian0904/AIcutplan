# -*- coding: utf-8 -*-
# [L1 旧链] 职责：字幕生成 / 烧字幕 ｜ 调用者：pipeline ｜ 产物：out/*.srt
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""字幕：narration.json → out/中文旁白-recap.srt，可选烧进成片

r7_mux 只合画面与音轨，不烧字幕（字幕原本由 r4_script 产出，本工作流跳过了 r4）。
这里直接按 narration.json 的最终时间轴生成 srt —— relayout 之后每句的 t0 就是成片秒。

用法：
  python make_srt.py --project <project.json>              # 只出 srt
  python make_srt.py --project <project.json> --burn       # 烧进 03-rapid-narration.mp4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

STYLE = ("FontName=Microsoft YaHei,FontSize=44,PrimaryColour=&H00FFFFFF,"
         "OutlineColour=&H00000000,Bold=1,Outline=2,Shadow=1,Alignment=2,MarginV=56")


def ts(t: float) -> str:
    if t < 0:
        t = 0.0
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(proj_dir: Path) -> dict:
    narr = json.loads((proj_dir / "json" / "narration.json").read_text(encoding="utf-8"))
    items = narr.get("items") or []
    if not items:
        raise RuntimeError("narration.json 没有句子")
    lines, last_end = [], 0.0
    for it in items:
        t0 = float(it.get("t0") or 0)
        dur = float(it.get("dur") or 0)
        # 句间不留空档：上一句结束到这一句开始之间补到 0.06s，避免字幕闪烁
        a = max(t0, last_end + 0.06) if last_end and t0 < last_end + 0.06 else t0
        z = max(a + 0.5, t0 + dur)
        lines.append(f"{len(lines) + 1}\n{ts(a)} --> {ts(z)}\n{it['text']}\n")
        last_end = z
    srt = "\n".join(lines)
    out = proj_dir / "out" / "中文旁白-recap.srt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(srt, encoding="utf-8")
    return {"srt": str(out), "lines": len(lines), "end": round(last_end, 2)}


def burn(proj_dir: Path, crf: int = 18) -> dict:
    src = proj_dir / "out" / "03-rapid-narration.mp4"
    if not src.is_file():
        raise RuntimeError("没有成片，先跑合流")
    srt = proj_dir / "out" / "中文旁白-recap.srt"
    if not srt.is_file():
        raise RuntimeError("没有字幕文件")
    dst = proj_dir / "out" / "03-rapid-narration-sub.mp4"
    tmp = proj_dir / "out" / "_burn_tmp.mp4"
    # 用相对文件名 + cwd，避开 Windows 路径里冒号与反斜杠在 filter 里的转义问题
    argv = ["ffmpeg", "-y", "-v", "warning", "-stats",
            "-i", src.name, "-vf", f"subtitles={srt.name}:force_style='{STYLE}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", tmp.name]
    print("$ " + " ".join(argv), flush=True)
    t0 = time.time()
    p = subprocess.Popen(argv, cwd=str(src.parent), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    tail = ""
    for raw in p.stdout or []:
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            tail = line
            if "time=" in line and int(time.time() - t0) % 5 == 0:
                print("  " + line.split("time=")[1][:12], flush=True)
    rc = p.wait()
    if rc != 0 or not tmp.exists():
        print(f"[字幕] 烧字幕失败：{tail}", flush=True)
        return {"ok": False, "error": tail[-200:]}
    tmp.replace(dst)
    return {"ok": True, "file": str(dst), "size": dst.stat().st_size,
            "elapsed": round(time.time() - t0, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--burn", action="store_true")
    ap.add_argument("--crf", type=int, default=18)
    a = ap.parse_args(argv)
    proj_dir = Path(a.project).expanduser().resolve().parent
    r = build_srt(proj_dir)
    print(f"[字幕] {r['lines']} 条 → {r['srt']}（末条结束 {r['end']}s）", flush=True)
    if a.burn:
        b = burn(proj_dir, a.crf)
        if not b.get("ok"):
            return 3
        print(f"[字幕] 已烧进成片 {b['file']} · {b['size'] / 1048576:.0f}MB · {b['elapsed']}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
