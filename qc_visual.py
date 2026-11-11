# -*- coding: utf-8 -*-
"""渲染后质检：成片逐句抽帧 → VLM 判「旁白说的 vs 画面演的」是否一致。

Phase E 的回环闭环件。渲染完成后运行：
  1. 从最终成片按每句中点抽帧（240px）；
  2. 10 句一批给 VLM：判断画面与旁白文本的语义一致性（match 0/1 + 一句话理由）；
  3. 产出 json/qc_visual.json，失配句清单供局部重写（改词或换窗口），不整条重跑。

用法：python qc_visual.py --project <pj> [--video out/03-rapid-narration.mp4] [--batch 10]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

QC_SYS = ("你是动漫解说质检员。给你成片中按时间顺序抽取的画面帧（每张标注时间）"
          "和该时间点的解说词。判断解说内容与画面是否语义一致。")

QC_Q = """只输出 JSON 数组，不要解释：
[{"t": 时间秒, "match": 0或1, "why": "一句话理由"}]

判断标准：
1. 解说描述的事件/对象/情绪与画面相同或直接相关 → match 1；
2. 解说在讲 A，画面演的是无关的 B → match 0；
3. 解说是对画面的引申评论（语气钩子、因果解释且与画面人物/场景相关）→ match 1；
4. 画面是转场/空景/字幕卡，解说在做段落衔接 → match 1。

样本列表：
{samples}
"""


def extract_frames(ffmpeg: str, video: Path, times: list[float], tmp: Path) -> dict[int, bytes]:
    tmp.mkdir(parents=True, exist_ok=True)
    out = {}
    for k, t in enumerate(times):
        f = tmp / f"qc_{k:04d}.jpg"
        if not f.exists() or f.stat().st_size < 500:
            subprocess.run([ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "6",
                            "-y", str(f)], check=False)
        if f.exists() and f.stat().st_size > 300:
            out[k] = f.read_bytes()
    return out


def call_vlm(samples: list[dict], frames: dict[int, bytes]) -> list[dict]:
    content = [{"type": "text", "text": QC_Q.replace("{samples}",
                "\n".join(f"t={s['t']:.1f}s | 解说: {s['text'][:40]}" for s in samples))}]
    for k, s in enumerate(samples):
        b = frames.get(k)
        if b is None:
            continue
        content.append({"type": "text", "text": f"t={s['t']:.1f}s："})
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode()}})
    body = {"model": "deepseek-flash", "stream": False, "max_tokens": 4000,
            "messages": [{"role": "system", "content": QC_SYS},
                         {"role": "user", "content": content}]}
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]})
    with urllib.request.urlopen(req, timeout=420) as r:
        d = json.loads(r.read().decode())
    txt = d["choices"][0]["message"].get("content") or ""
    m = txt.find("[")
    return json.loads(txt[m:txt.rfind("]") + 1]) if m >= 0 else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--video", default="out/03-rapid-narration.mp4")
    ap.add_argument("--batch", type=int, default=10)
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent
    d = proj_dir / "json"
    narr = json.loads((d / "narration.json").read_text(encoding="utf-8"))["items"]
    video = proj_dir / a.video
    if not video.exists():
        print(f"[视觉质检] 找不到成片 {video}")
        return 2

    ffmpeg = os.environ.get("NARR_FFMPEG", r"C:\Users\10451\.workbuddy\binaries\ffmpeg\bin\ffmpeg.exe")
    times = [it["t0"] + min(it["dur"], 1.2) / 2 for it in narr]   # 句中点（短句取前半，避开下一句）
    frames = extract_frames(ffmpeg, video, times, proj_dir / ".qcframes")

    rows: list[dict] = []
    for bi in range(0, len(narr), a.batch):
        chunk = [{"t": times[bi + k], "text": narr[bi + k]["text"], "i": narr[bi + k]["i"]}
                 for k in range(min(a.batch, len(narr) - bi))]
        try:
            got = call_vlm(chunk, frames)
        except Exception as e:
            print(f"[视觉质检] 批{bi // a.batch} 失败: {type(e).__name__} {str(e)[:100]}", flush=True)
            continue
        byt = {}
        for g in got:
            if isinstance(g, dict) and "t" in g:
                byt[f"{float(g['t']):.1f}"] = g
        for k, s in enumerate(chunk):
            g = byt.get(f"{s['t']:.1f}", {})
            rows.append({"i": s["i"], "t": round(s["t"], 2), "text": s["text"],
                         "match": int(g.get("match", 1)), "why": str(g.get("why", ""))[:60]})
        done = min(bi + a.batch, len(narr))
        print(f"[视觉质检] {done}/{len(narr)} 句", flush=True)
        (d / "qc_visual.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                          encoding="utf-8")

    miss = [r for r in rows if r["match"] == 0]
    print(f"[视觉质检] 完成 {len(rows)} 句 · 失配 {len(miss)} 句（{100 * len(miss) / max(1, len(rows)):.0f}%）")
    for r in miss[:12]:
        print(f"  句{r['i']} t={r['t']:.1f}s [{r['text'][:16]}] {r['why']}")
    print(f"→ json/qc_visual.json（失配句供局部重写）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
