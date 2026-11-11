# -*- coding: utf-8 -*-
"""精细场景解析层：全片每 2 秒一窗，抽帧 + 对齐台词 → 视觉模型写场景描述。

与 shot_desc（镜头级，一镜一描述）不同，这层是**时间网格级**：
  - 每 2s 抽 1 帧（fps=1/2 一次抽完全片），窗口对齐 ja-segments 里的台词原文；
  - 按 20 窗/次批量给视觉模型，要求结合镜头语言（景别/运镜/转场）与人物对话，
    写出该窗口的场景描述；
  - 结果缓存 json/scene_desc.json，批次级断点续传（scenes-cache 批次哈希跳过已完成批）。

用法：python scene_desc.py --project <pj> [--window 2] [--batch 20] [--force]
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

import story_parse as SP  # noqa: E402

DESC_SYS = ("你是动漫分镜分析师。给你按时间顺序排列的连续画面截图（每张前标注源片秒）"
            "和该时刻的人物台词（日语原文）。请结合镜头语言与对话，为每个时间窗写场景描述。")

DESC_Q = """只输出 JSON 数组，不要解释：
[{"t": 起始秒, "desc": "场景描述", "cam": "镜头语言"}]

规则：
1. desc（40字内）：结合台词说清谁在做什么、什么情绪/意图——台词原文是日语，用中文转述其含义并融入描述；
   画面没有人物时（空景/字幕/转场）直接描述画面内容。
2. cam（12字内）：景别与运镜（如"中景固定""特写急推""俯拍摇镜""大远景"），转场帧写"转场"。
3. 相邻窗口若属同一镜头的延续，描述要承接上文但突出画面变化。
4. 全部窗口都要有输出，顺序不限。

窗口列表：
{wins}
"""


def probe_video(video: Path, dur: float, win: float, tmp: Path) -> dict[int, Path]:
    """一次抽完全部窗口帧：fps=1/win 输出等间隔帧，第 k 帧时间 ≈ k*win。"""
    tmp.mkdir(parents=True, exist_ok=True)
    fps = 1.0 / win
    n_expect = int(dur / win) + 1
    done = {int(p.stem.split("_")[1]) for p in tmp.glob("frame_*.jpg")}
    if len(done) >= n_expect:
        return {k: tmp / f"frame_{k:05d}.jpg" for k in range(n_expect)}
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-an",
                    "-vf", f"fps={fps},scale=240:-2", "-q:v", "6",
                    "-y", str(tmp / "frame_%05d.jpg")], check=False)
    done = sorted(int(p.stem.split("_")[1]) for p in tmp.glob("frame_*.jpg"))
    # ffmpeg 序号从 1 开始且首帧在 0s 附近：帧 k（0 基）→ 文件 k+1
    out = {}
    for k in range(n_expect):
        p = tmp / f"frame_{k + 1:05d}.jpg"
        if p.exists() and p.stat().st_size > 300:
            out[k] = p
    return out


def load_dialogue(proj_dir: Path) -> list[dict]:
    f = proj_dir / "json" / "ja-segments.json"
    if not f.exists():
        return []
    segs = json.loads(f.read_text(encoding="utf-8")).get("segments", [])
    return [{"t0": float(s.get("start", 0)), "t1": float(s.get("end", 0)),
             "text": str(s.get("text", ""))} for s in segs if s.get("text")]


def dlg_of(win_t0: float, win_t1: float, segs: list[dict]) -> str:
    """窗口内重叠最大的台词（最多 2 条，防止超长）。"""
    hits = [s for s in segs if s["t0"] < win_t1 and s["t1"] > win_t0]
    hits.sort(key=lambda s: min(s["t1"], win_t1) - max(s["t0"], win_t0), reverse=True)
    return " / ".join(s["text"][:40] for s in hits[:2])


def call_vlm(wins: list[dict], frames: dict[int, Path], win: float) -> list[dict]:
    content = [{"type": "text", "text": DESC_Q.replace("{wins}",
                "\n".join(f"{w['t']:.0f}s (台词: {w['dlg'] or '无'})" for w in wins))}]
    for w in wins:
        p = frames.get(w["k"])
        if p is None:
            continue
        b64 = base64.b64encode(p.read_bytes()).decode()
        content.append({"type": "text", "text": f"{w['t']:.0f}s："})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    body = {"model": "deepseek-flash", "stream": False, "max_tokens": 8000,
            "messages": [{"role": "system", "content": DESC_SYS},
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
    ap.add_argument("--window", type=float, default=2.0)
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent
    d = proj_dir / "json"
    out_f = d / "scene_desc.json"
    if out_f.exists() and not a.force:
        print(f"[场景解析] 已有缓存 {out_f}，--force 重生成")
        return 0

    video = None
    for name in ("out/01-video-no-ad.mp4", "ref.mp4"):
        p = proj_dir / name
        if p.exists():
            video = p
            break
    if video is None:
        print("[场景解析] 找不到源片 out/01-video-no-ad.mp4")
        return 2

    dur = SP.duration(video) if hasattr(SP, "duration") else 0
    if dur <= 0:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(video)],
                           capture_output=True, timeout=30)
        dur = float(r.stdout.decode().strip() or 0)
    if dur <= 0:
        print("[场景解析] 取不到源片时长")
        return 2

    n_win = int(dur / a.window)
    segs = load_dialogue(proj_dir)
    frames = probe_video(video, dur, a.window, proj_dir / ".scenedesc")
    print(f"[场景解析] 源片 {dur:.0f}s · {n_win} 窗 · {len(frames)} 帧 · 台词 {len(segs)} 段", flush=True)

    wins = []
    for k in range(n_win):
        t0 = k * a.window
        wins.append({"k": k, "t": t0, "t1": t0 + a.window,
                     "dlg": dlg_of(t0, t0 + a.window, segs)})

    # 批次断点续传：已完成的批次窗口起点集合
    done_map: dict[str, dict] = {}
    if out_f.exists():
        try:
            for r in json.loads(out_f.read_text(encoding="utf-8")).get("rows", []):
                done_map[f"{float(r['t']):.0f}"] = r
        except Exception:
            done_map = {}

    rows: list[dict] = []
    for bi in range(0, n_win, a.batch):
        chunk = wins[bi:bi + a.batch]
        if all(f"{w['t']:.0f}" in done_map for w in chunk):
            print(f"[场景解析] 批{bi // a.batch}: 全部命中缓存，跳过", flush=True)
            continue
        try:
            got = call_vlm(chunk, frames, a.window)
        except Exception as e:
            print(f"[场景解析] 批{bi // a.batch} 失败: {type(e).__name__} {str(e)[:120]}", flush=True)
            continue
        byt = {}
        for g in got:
            if isinstance(g, dict) and "t" in g:
                byt[f"{float(g['t']):.0f}"] = g
        filled = 0
        for w in chunk:
            g = byt.get(f"{w['t']:.0f}", {})
            desc = str(g.get("desc", ""))[:80]
            cam = str(g.get("cam", ""))[:20]
            if not desc and f"{w['t']:.0f}" in done_map:
                g, desc = done_map[f"{w['t']:.0f}"], done_map[f"{w['t']:.0f}"]["desc"]
                cam = done_map[f"{w['t']:.0f}"].get("cam", "")
            if desc:
                filled += 1
            rows.append({"t": round(w["t"], 1), "t1": round(w["t1"], 1),
                         "desc": desc, "cam": cam, "dlg": w["dlg"][:60]})
        print(f"[场景解析] 批{bi // a.batch}: {filled}/{len(chunk)} 窗 ({bi + len(chunk)}/{n_win})", flush=True)
        rows.sort(key=lambda r: float(r["t"]))
        out_f.parent.mkdir(exist_ok=True)
        out_f.write_text(json.dumps({"rows": rows, "window": a.window,
                                     "updated": __import__("datetime").datetime.now().isoformat()},
                                    ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[场景解析] 完成 {len(rows)} 窗 → {out_f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
