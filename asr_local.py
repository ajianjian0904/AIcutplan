# -*- coding: utf-8 -*-
"""本地 ASR：faster-whisper 转写人物台词 → asr_<lang>.json

产物格式与历史工程一致：{"model","lang","paras":[{"t0","t1","text"}]}，t0/t1 是源片秒。
设计上刻意绕开 PyAV 解码（本机 av 版本与 vad 组件会打架）：先用 ffmpeg 出 16k 单声道
wav，再读成 numpy 直接喂模型；幻觉与重复段落在后处理里过滤。

语言用 whisper 的语言码：ja / en / ko / zh / es / fr …
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

HALLU = ("ご視聴", "チャンネル登録", "おやすみなさい", "ごちそうさま",
         "Thanks for watching", "Subscribe", "字幕by", "由 提供")


def pick_source(d: Path, pj: dict) -> Path:
    a = d / "out" / "01-video-no-ad.mp4"
    if a.exists() and a.stat().st_size > 1024:
        return a
    s = Path(pj.get("source_video") or d / "ref.mp4")
    return s if s.is_absolute() else (d / s).resolve()


def to_wav(src: Path, wav: Path) -> None:
    if wav.exists() and wav.stat().st_size > 1024:
        print(f"[asr] 复用音频 {wav.name}", flush=True)
        return
    print(f"[asr] 抽取 16k 单声道音频 → {wav.name}", flush=True)
    rc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ignore_editlist", "1",
                         "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav)]).returncode
    if rc != 0:
        raise SystemExit("抽音失败")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--lang", default="ja")
    ap.add_argument("--size", default="small",
                    help="tiny / small / medium / large-v3，或本地模型目录")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    pj_f = Path(a.project).expanduser()
    pj = json.loads(pj_f.read_text(encoding="utf-8"))
    d = pj_f.parent
    out = d / f"asr_{a.lang}.json"
    if out.exists() and not a.force:
        try:
            n = len(json.loads(out.read_text(encoding="utf-8")).get("paras") or [])
        except Exception:
            n = 0
        if n:
            print(f"[asr] 已有 {out.name}（{n} 段），跳过。要重识别加 --force", flush=True)
            return 0

    src = pick_source(d, pj)
    if not src.exists():
        print(f"[asr] 找不到源片：{src}", flush=True)
        return 2
    wav = d / "wav16k.wav"
    to_wav(src, wav)

    # ctranslate2 需要 CUDA12 运行库；torch 自带，把它的 lib 挂进 DLL 搜索路径
    try:
        import torch
        tlib = os.path.join(os.path.dirname(torch.__file__), "lib")
        if os.path.isdir(tlib):
            os.add_dll_directory(tlib)
            os.environ["PATH"] = tlib + os.pathsep + os.environ.get("PATH", "")
            print("[asr] torch CUDA 库目录已挂载", flush=True)
    except Exception as e:
        print(f"[asr] torch DLL 挂载跳过：{e}", flush=True)

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[asr] 缺 faster-whisper：pip install faster-whisper", flush=True)
        return 4

    local_hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    model_path = a.size
    if local_hub.is_dir():
        snaps = sorted(local_hub.glob(f"models--*faster-whisper-{a.size}/snapshots/*"))
        for s in snaps:
            if (s / "model.bin").exists():
                model_path = str(s)
                print(f"[asr] 用本地模型 {s.name[:12]}", flush=True)
                break

    model = None
    for dev, ct in (("cuda", "float16"), ("cuda", "float32"), ("cpu", "int8")):
        try:
            t0 = time.time()
            model = WhisperModel(model_path, device=dev, compute_type=ct, cpu_threads=8)
            print(f"[asr] 后端 {dev}/{ct} · 载入 {time.time() - t0:.1f}s", flush=True)
            break
        except Exception as e:
            print(f"[asr] {dev}/{ct} 不可用：{type(e).__name__}", flush=True)
    if model is None:
        print("[asr] 没有可用推理后端（装了 torch/CUDA 吗）", flush=True)
        return 5

    import numpy as np
    with wave.open(str(wav), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    total = len(audio) / 16000.0
    print(f"[asr] 音频 {total:.0f}s · 语言 {a.lang} · 模型 {a.size}", flush=True)

    segs, info = model.transcribe(audio, language=a.lang, beam_size=5,
                                  vad_filter=False, condition_on_previous_text=False,
                                  no_speech_threshold=0.55, log_prob_threshold=-1.0)
    rows, prev, next_pct = [], "", 5
    for s in segs:
        txt = s.text.strip()
        if not txt or any(h in txt for h in HALLU):
            continue
        if txt == prev:
            continue
        prev = txt
        rows.append({"i": len(rows), "t0": round(s.start, 2), "t1": round(s.end, 2), "text": txt})
        pct = s.end / max(1e-6, total) * 100
        if pct >= next_pct:
            print(f"[asr] 转写 {pct:.0f}% · 已 {len(rows)} 段 / {s.end:.0f}s", flush=True)
            next_pct += 5

    out.write_text(json.dumps({"model": a.size, "lang": a.lang, "paras": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    txt_f = d / f"asr_{a.lang}.txt"
    with txt_f.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"[{int(r['t0'] // 60)}:{r['t0'] % 60:05.2f}] {r['text']}\n")
    print(f"[asr] 完成：{len(rows)} 段 → {out.name}", flush=True)
    print("[asr] 下一步：台词译中", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
