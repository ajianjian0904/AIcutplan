# -*- coding: utf-8 -*-
"""镜头描述层：给每个镜头生成一句话内容描述（业内手法一：可检索的画面侧文本）。

按块批量：抽块内每个镜头的中点帧（缩到 240px），连同镜头号/秒区间给视觉模型，
要求输出每个镜头的一句话描述 + 关键对象标签。结果缓存 json/shots_desc.json，
供 smart_align 做「句子 → 镜头」的内容级匹配（此前只有事件级，镜头内部是盲选）。

用法：python shot_desc.py --project <pj> [--frames 240]
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

DESC_SYS = ("你是动漫镜头标注员。给你一个剧情块内的所有镜头中点截图（每张前标注镜头号与源片秒），"
            "为每个镜头写一句话内容描述，供后续按解说句子匹配画面。")

DESC_Q = """只输出 JSON 数组，不要解释：
[{"shot": 镜头号, "desc": "一句话说清谁在做什么/画面主体（15字内）", "obj": "关键对象，逗号分隔，没有则空"}]
镜头列表：
{shots}

规则：
1. desc 必须具体到动作与对象（"雷恩挥剑砍向壮汉"），不要写"一个场景"。
2. obj 填画面里可辨认的关键词（人名、外套、剑、公会牌、龙、登记表等）。
3. 全部镜头都要有输出，顺序不限。"""


def probe_frames(video: Path, shots: list[dict], tmp: Path) -> dict[int, bytes]:
    tmp.mkdir(parents=True, exist_ok=True)
    out = {}
    for s in shots:
        t = (s["a"] + s["b"]) / 2
        f = tmp / f"shot_{s['i']:04d}.jpg"
        if not f.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "6", "-y", str(f)],
                           check=False)
        if f.exists() and f.stat().st_size > 500:
            out[s["i"]] = f.read_bytes()
    return out


def describe_block(video: Path, shots: list[dict], tmp: Path) -> list[dict]:
    frames = probe_frames(video, shots, tmp)
    shots_line = "\n".join(f"镜头{s['i']} [{s['a']:.1f}-{s['b']:.1f}s]" for s in shots
                           if s["i"] in frames)
    content = [{"type": "text", "text": DESC_Q.replace("{shots}", shots_line)}]
    for s in shots:
        b = frames.get(s["i"])
        if b is None:
            continue
        b64 = base64.b64encode(b).decode()
        content += [{"type": "text", "text": f"镜头{s['i']}："},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
    body = {"model": "deepseek-flash", "stream": False, "max_tokens": 9000,
            "messages": [{"role": "system", "content": DESC_SYS},
                         {"role": "user", "content": content}]}
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read().decode())
    txt = d["choices"][0]["message"].get("content") or ""
    m = txt.find("[")
    rows = json.loads(txt[m:txt.rfind("]") + 1]) if m >= 0 else []
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    pj = Path(a.project).expanduser().resolve()
    proj_dir = pj.parent
    d = proj_dir / "json"
    cache = d / "shots_desc.json"
    if cache.exists() and not a.force:
        old = json.loads(cache.read_text(encoding="utf-8"))
        print(f"[镜头描述] 已有缓存 {len(old.get('rows', []))} 镜，--force 重生成")
        return 0

    import narration as NR
    cfg = NR.llm_config()
    assert cfg, "DeepSeek 未配置"

    video = None
    for name in ("out/01-video-no-ad.mp4", "ref.mp4"):
        p = proj_dir / name
        if p.exists():
            video = p
            break
    if video is None:
        print("[镜头描述] 找不到源片 out/01-video-no-ad.mp4")
        return 2

    shots = SP.shotlist(proj_dir)
    sp = SP.load_plan(proj_dir)
    blocks = [b for b in (sp.get("blocks") or []) if b["type"] != "收录" and b["target"] > 0]

    rows: list[dict] = []
    for bi, bl in enumerate(blocks):
        inb = [s for s in shots if bl["t0"] <= s["a"] < bl["t1"]]
        if not inb:
            continue
        try:
            got = describe_block(video, inb, proj_dir / ".shotdesc")
            print(f"[镜头描述] 块{bi} {bl['label']}: {len(got)}/{len(inb)} 镜", flush=True)
        except Exception as e:
            print(f"[镜头描述] 块{bi} 失败: {type(e).__name__} {str(e)[:120]}", flush=True)
            continue
        byid = {g.get("shot"): g for g in got if isinstance(g, dict)}
        for s in inb:
            g = byid.get(s["i"], {})
            rows.append({"i": s["i"], "a": s["a"], "b": s["b"],
                         "desc": str(g.get("desc", ""))[:40], "obj": str(g.get("obj", ""))[:40]})
    cache.write_text(json.dumps({"rows": rows, "updated": __import__("datetime").datetime.now().isoformat()},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[镜头描述] {len(rows)} 镜 → {cache}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
