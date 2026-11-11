# -*- coding: utf-8 -*-
"""Cut-first 阶段三：弹性对齐 + 质检报告（elastic alignment）。

画面槽位已定型，TTS 之后做的是"音频适配槽位"而非"裁画面适配音频"：
  - 每句槽位 = 它绑定的窗口跨度（sent_win_idx → cutplan windows）
  - 语音净长（去 edge-tts 首尾静音）超出槽位：
      ≤15%  → ffmpeg atempo 压回（听感无碍）
      ≤35%  → edge-tts 加速重合成一次（rate +20%），再压余量
      >35%  → 记入质检清单（文字过长，需人工改写或接受溢出）
  - 语音短于槽位：留白呼吸，正常

同时产出质检报告 json/qc_report.json：每句的槽位/语音/超槽率/处理方式，
超 35% 的句子列出 —— 全自动路径的回环入口（改词后只重跑该句 TTS）。

用法：python elastic_fit.py --project <pj>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

SILENCE = 0.60        # edge-tts 每句首尾静音估计（r5 实测 ~0.7s，取保守 0.6）
MAX_TEMPO = 1.15      # 直接 atempo 的上限
MAX_RESYN = 1.35      # 允许加速重合成的上限


def ffprobe_dur(ffprobe: str, p: Path) -> float:
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(p)], capture_output=True)
    try:
        return float(r.stdout.decode().strip() or 0)
    except ValueError:
        return 0.0


def atempo_file(ffmpeg: str, src: Path, tempo: float, dst: Path) -> None:
    subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(src),
                    "-af", f"atempo={tempo:.4f}", "-c:a", "libmp3lame", "-q:a", "4", str(dst)],
                   check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None)
    a = ap.parse_args()
    # common.py 在工程目录里，先把它加进 path
    _pj = Path(a.project or ".").expanduser().resolve()
    sys.path.insert(0, str(_pj.parent if _pj.is_file() else _pj))
    import common
    ctx = common.load(a.project)
    ff = str(ctx.ffmpeg)

    d = ctx.p("json")
    narr = json.loads((d / "narration.json").read_text(encoding="utf-8"))["items"]
    tts = {x["i"]: x for x in json.loads((d / "tts-result.json").read_text(encoding="utf-8"))["items"]}
    plan = json.loads((d / "cutplan.json").read_text(encoding="utf-8"))
    wins = plan["windows"]
    align = json.loads((d / "align.json").read_text(encoding="utf-8"))
    swi = align["sent_win_idx"]

    rc = ctx.cfg.get("recap") or {}
    voice = rc.get("voice", "zh-CN-YunxiNeural")
    base_rate = rc.get("tts_rate", "+35%")

    report = []
    fixed_atempo, fixed_resyn, over_list = [], [], []
    for it in narr:
        i = it["i"]
        r = tts.get(i)
        if not r:
            continue
        lo, hi = swi.get(str(i), [None, None])
        if lo is None:
            continue
        slot = wins[hi]["new_end"] - wins[lo]["new_start"] + 0.15   # 少量呼吸宽限
        speech = r["tts_dur"] - SILENCE
        ratio = speech / slot if slot > 0.1 else 1.0
        row = {"i": i, "slot": round(slot, 2), "speech": round(speech, 2),
               "ratio": round(ratio, 2), "action": "ok"}
        mp3 = ctx.p("tts", f"{i:03d}.mp3")

        def _tmp() -> Path:
            fd, name = tempfile.mkstemp(suffix=".mp3", dir=str(mp3.parent))
            os.close(fd)                      # 立刻关句柄 + 同盘符，Windows 下才能 replace
            return Path(name)

        if ratio > 1.0 and mp3.exists():
            if ratio <= MAX_TEMPO:
                tmp = _tmp()
                atempo_file(ff, mp3, ratio, tmp)
                tmp.replace(mp3)
                row["action"] = f"atempo×{ratio:.2f}"
                fixed_atempo.append(i)
                tts[i]["tts_dur"] = round(ffprobe_dur(ctx.ffprobe, mp3), 3)
            elif ratio <= MAX_RESYN:
                faster = "+55%" if base_rate in ("+35%", "+50%") else "+50%"
                tmp = _tmp()
                try:
                    asyncio.run(edge_tts.Communicate(it["text"], voice, rate=faster,
                                                     pitch=rc.get("pitch", "+0Hz")).save(str(tmp)))
                    d2 = ffprobe_dur(ctx.ffprobe, tmp)
                    ratio2 = (d2 - SILENCE) / slot
                    if 1.0 < ratio2 <= MAX_TEMPO:
                        atempo_file(ff, tmp, ratio2, mp3)
                    else:
                        tmp.replace(mp3)
                        if ratio2 > 1.0:
                            atempo_file(ff, mp3, min(ratio2, MAX_TEMPO), mp3)
                    row["action"] = f"resyn{faster}"
                    fixed_resyn.append(i)
                    tts[i]["tts_dur"] = round(ffprobe_dur(ctx.ffprobe, mp3), 3)
                except Exception as e:
                    row["action"] = f"resyn失败:{type(e).__name__}"
                finally:
                    if tmp.exists():
                        tmp.unlink()
            else:
                row["action"] = "OVER"
                over_list.append(i)
        report.append(row)

    (d / "tts-result.json").write_text(json.dumps(
        {"voice": voice, "rate": base_rate, "items": [tts[k] for k in sorted(tts)]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "qc_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                      encoding="utf-8")

    over_ratio = [r for r in report if r["ratio"] > 1.0]
    print("=== 弹性对齐 ===")
    print(f"{len(report)} 句 · 超槽 {len(over_ratio)} · atempo 压回 {len(fixed_atempo)} · "
          f"加速重合成 {len(fixed_resyn)} · 仍超 35% {len(over_list)}")
    if over_list:
        print(f"质检清单（需改词）：{over_list[:10]}")
    print(f"→ qc_report.json / tts-result.json 已更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
