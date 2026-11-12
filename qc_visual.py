# -*- coding: utf-8 -*-
# [L1 新链] 职责：会话内逐句质检 plan/merge/probe ｜ 调用者：cf_pipeline --qc ｜ 产物：qc_visual/qc_rewrite.json
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""渲染后质检（会话内版）：成片逐句抽帧 → 会话内读图判「旁白说的 vs 画面演的」。

P0-1 闭环第一步。原版把帧 base64 发给 DeepSeek VLM，与「零 DeepSeek」目标冲突
（且没有 key 时整个质检跑不起来），现改为批次清单 + 宫格图，判定在 WorkBuddy 会话内完成：

  1) python qc_visual.py --project <pj> --plan 6
       → .qcframes/session/plan_XX.json（每批 6 句：序号/时间/旁白/窗口/画面证据）
       → .qcframes/session/grid_XX.jpg（2×3 宫格图，按序号左上→右下对应）
  2) 会话内按图逐格判定，写 .qcframes/session/verdict_XX.json：
       [{"i": 0, "match": 1, "why": ""}, {"i": 3, "match": 0, "why": "解说讲巨坑，画面是空镜"}]
  3) python qc_visual.py --project <pj> --merge
       → json/qc_visual.json（全量判定）+ json/qc_rewrite.json（失配句的局部重写清单）
  4) python qc_loop.py --project <pj> --apply <改好的 sentences.json>
       → 只重跑失配相关句子，不整条重来

抽帧时间取「句子首窗中点」而非整句时间中点：v3 一句可跨 6 个窗口，句中点会落到
后半段画面上，那不是旁白开口时观众看到的画面。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from aicutplan.media import ffmpeg_bin   # noqa: E402  L0 统一实现

QC_CRITERIA = """判据（match 0/1）：
1. 解说描述的事件/对象/情绪与画面相同或直接相关 → 1
2. 解说在讲 A，画面演的是无关的 B → 0
3. 解说是对画面的引申评论（钩子、因果解释，且与画面人物/场景相关）→ 1
4. 画面是转场/空景/字幕卡，解说在做段落衔接 → 1
5. 「先说结果再演过程」允许约 2s 领先，不算失配；超出一整句才是 0
why 只在 match=0 时写一句话理由，其余留空字符串。
"""


# ffmpeg_bin 由 aicutplan.media 提供（查找优先级 NARR_FFMPEG → AICUTPLAN_FFMPEG
# → PATH → 本机兜底），避免每个抽帧脚本各写一份、换机器时漏改。


def sent_times(proj_dir: Path, narr: list[dict]) -> list[dict]:
    """按句子首窗中点取帧，附带窗口与源片区间（供失配时定位改词/换窗）。"""
    d = proj_dir / "json"
    wins = json.loads((d / "cutplan.json").read_text(encoding="utf-8"))["windows"]
    swi = {}
    ap = d / "align.json"
    if ap.exists():
        swi = json.loads(ap.read_text(encoding="utf-8")).get("sent_win_idx") or {}
    out = []
    for it in narr:
        i = int(it["i"])
        pair = swi.get(str(i)) or swi.get(i)
        t = float(it["t0"]) + min(float(it["dur"]), 1.2) / 2
        lo = hi = None
        if pair and 0 <= int(pair[0]) < len(wins):
            lo, hi = int(pair[0]), int(pair[1])
            w = wins[lo]
            t = float(w["new_start"]) + min(float(w["dur"]), 1.2) / 2
        row = {"i": i, "t": round(t, 3), "text": it["text"], "wins": [lo, hi]}
        if lo is not None:
            row["src"] = [round(float(wins[lo]["src_start"]), 2),
                          round(float(wins[hi]["src_end"]), 2)]
            row["reason"] = wins[lo].get("reason", "")
        out.append(row)
    return out


def extract_frames(video: Path, rows: list[dict], tmp: Path,
                   force: bool = False) -> dict[int, Path]:
    """抽帧缓存按「成片 mtime」失效：视频一更新就重抽，避免旧版帧冒充当前成片。

    坑：帧名只有句号（qc_0000.jpg），换版本后不失效会直接命中上一版质检的旧帧，
    导致拿旧画面判新旁白，得出完全错误的失配结论。
    """
    tmp.mkdir(parents=True, exist_ok=True)
    vmt = video.stat().st_mtime
    out = {}
    for k, r in enumerate(rows):
        f = tmp / f"qc_{r['i']:04d}.jpg"
        if force or not f.exists() or f.stat().st_size < 500 or f.stat().st_mtime < vmt:
            # 暗帧回退：句首窗中点可能恰好落在黑屏/转场帧上，会误判成「画面与旁白不符」。
            # 平均亮度低于阈值时，在同一句首窗内往后挪 0.35s 再抽，最多 3 次。
            for attempt in range(3):
                t = r["t"] + 0.35 * attempt
                subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{t:.3f}",
                                "-i", str(video), "-frames:v", "1",
                                "-vf", "scale=320:-2", "-q:v", "4", "-y", str(f)], check=False)
                if not (f.exists() and f.stat().st_size > 300):
                    continue
                try:
                    if ImageStat.Stat(Image.open(f).convert("L")).mean[0] >= 16:
                        break
                except Exception:
                    break
        if f.exists() and f.stat().st_size > 300:
            out[r["i"]] = f
    return out


def make_grid(frames: list[Path], dst: Path, cols: int = 2) -> bool:
    """把一批帧拼成宫格图，格左上角标序号（1 起），顺序对应 plan 列表。"""
    ims = []
    for p in frames:
        try:
            ims.append(Image.open(p).convert("RGB"))
        except Exception:
            ims.append(Image.new("RGB", (320, 180), (230, 230, 230)))
    if not ims:
        return False
    w, h = ims[0].size
    rows = (len(ims) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * w, rows * h), (24, 24, 24))
    dr = ImageDraw.Draw(canvas)
    for k, im in enumerate(ims):
        x, y = (k % cols) * w, (k // cols) * h
        canvas.paste(im, (x, y))
        dr.rectangle([x + 2, y + 2, x + 30, y + 24], fill=(200, 40, 40))
        dr.text((x + 9, y + 7), str(k + 1), fill=(255, 255, 255))
    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dst, quality=88)
    return True


def build_plan(proj_dir: Path, video: Path, per_batch: int) -> Path:
    d = proj_dir / "json"
    narr = json.loads((d / "narration.json").read_text(encoding="utf-8"))["items"]
    rows = sent_times(proj_dir, narr)
    sess = proj_dir / ".qcframes" / "session"
    sess.mkdir(parents=True, exist_ok=True)
    frames = extract_frames(video, rows, proj_dir / ".qcframes", force=True)
    (sess / "判据.txt").write_text(QC_CRITERIA, encoding="utf-8")

    batches = [rows[k:k + per_batch] for k in range(0, len(rows), per_batch)]
    for bi, chunk in enumerate(batches):
        items = []
        for seq, r in enumerate(chunk, 1):
            items.append({"seq": seq, **r, "frame": str(frames.get(r["i"], "")).replace("/", "\\")})
        (sess / f"plan_{bi:02d}.json").write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        pics = [frames[r["i"]] for r in chunk if r["i"] in frames]
        make_grid(pics, sess / f"grid_{bi:02d}.jpg")
    print(f"[质检·批次] {len(narr)} 句 → {len(batches)} 批 / 每批 {per_batch} 句")
    print(f"  清单与宫格图: {sess}")
    print(f"  判定写回: {sess / 'verdict_XX.json'}，格式见 判据.txt")
    return sess


def shot_hint(proj_dir: Path, r: dict) -> str:
    """失配句的源片画面证据：给改词/换窗时用，不额外读图。"""
    p = proj_dir / "json" / "shots_desc.json"
    if not p.exists() or not r.get("src"):
        return r.get("reason", "")
    shots = json.loads(p.read_text(encoding="utf-8")).get("rows") or []
    a, b = r["src"]
    hits = [s for s in shots if a <= float(s.get("a", 0)) < b
            or (float(s.get("a", 0)) <= a < float(s.get("b", 0)))]
    if not hits:
        mid = (a + b) / 2
        hits = sorted(shots, key=lambda s: abs(float(s.get("a", 0)) - mid))[:2]
    return " / ".join(f"{s.get('desc', '')[:26]}" for s in hits[:3])


def build_probe(proj_dir: Path, video: Path, per_sent: int = 3) -> Path:
    """对失配句做多帧复核：单帧抽在空镜/转场上很容易假阳性。

    每句在其完整槽位（首窗起点→尾窗终点）内均匀抽 per_sent 帧，拼成
    「一行一句 × N 列」复核图；确认是假阳性就把 verdict 改回 1 再 --merge。
    """
    d = proj_dir / "json"
    rp = d / "qc_rewrite.json"
    if not rp.exists():
        raise SystemExit("无 qc_rewrite.json，先 --merge")
    miss = json.loads(rp.read_text(encoding="utf-8"))
    wins = json.loads((d / "cutplan.json").read_text(encoding="utf-8"))["windows"]
    swi = json.loads((d / "align.json").read_text(encoding="utf-8")).get("sent_win_idx") or {}
    sess = proj_dir / ".qcframes" / "session"
    tmp = proj_dir / ".qcframes" / "probe"
    tmp.mkdir(parents=True, exist_ok=True)
    W, H, LABEL = 320, 180, 110
    rows = []
    for m in miss:
        i = int(m["i"])
        pair = swi.get(str(i)) or [i, i]
        lo, hi = int(pair[0]), int(pair[1])
        a = float(wins[lo]["new_start"])
        b = float(wins[hi]["new_end"])
        pics = []
        for k in range(per_sent):
            t = a + (b - a) * ((k + 0.5) / per_sent)
            p = tmp / f"s{i:03d}_{k}.jpg"
            subprocess.run([ffmpeg_bin(), "-v", "error", "-ss", f"{t:.3f}",
                            "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=320:-2", "-q:v", "4", "-y", str(p)], check=False)
            pics.append(p)
        rows.append({"i": i, "t0": round(a, 2), "t1": round(b, 2),
                     "text": m["text"], "why": m["why"]})
    canvas = Image.new("RGB", (per_sent * W + LABEL, max(1, len(rows)) * H), (24, 24, 24))
    dr = ImageDraw.Draw(canvas)
    for r, row in enumerate(rows):
        y = r * H
        dr.text((4, y + 8), f"S{row['i']}", fill=(255, 220, 80))
        dr.text((4, y + 30), f"{row['t0']:.0f}-{row['t1']:.0f}s", fill=(200, 200, 200))
        for k in range(per_sent):
            p = tmp / f"s{row['i']:03d}_{k}.jpg"
            try:
                im = Image.open(p).convert("RGB")
            except Exception:
                im = Image.new("RGB", (W, H), (60, 60, 60))
            canvas.paste(im, (LABEL + k * W, y))
            dr.rectangle([LABEL + k * W + 2, y + 2, LABEL + k * W + 26, y + 22],
                         fill=(30, 90, 180))
            dr.text((LABEL + k * W + 8, y + 6), str(k + 1), fill=(255, 255, 255))
    out = sess / "probe_miss.jpg"
    canvas.save(out, quality=90)
    (sess / "probe_rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print(f"[质检·复核] {len(rows)} 句 × {per_sent} 帧 → {out}")
    print("  假阳性（窗内其实有对应画面）把 verdict 改回 1 后重跑 --merge")
    return out


def merge(proj_dir: Path) -> int:
    d = proj_dir / "json"
    narr = json.loads((d / "narration.json").read_text(encoding="utf-8"))["items"]
    rows = sent_times(proj_dir, narr)
    by_i = {r["i"]: r for r in rows}
    sess = proj_dir / ".qcframes" / "session"
    verdicts: dict[int, dict] = {}
    for f in sorted(sess.glob("verdict_*.json")):
        try:
            arr = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[质检·合并] 跳过坏文件 {f.name}: {e}")
            continue
        for v in arr:
            try:
                i = int(v["i"])
            except (KeyError, TypeError, ValueError):
                continue
            if i in by_i:
                verdicts[i] = v
    out = []
    for r in rows:
        v = verdicts.get(r["i"])
        out.append({"i": r["i"], "t": r["t"], "text": r["text"],
                    "match": int(v.get("match", 1)) if v else 1,
                    "why": str(v.get("why", ""))[:80] if v else "",
                    "judged": v is not None})
    (d / "qc_visual.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    miss = [x for x in out if x["match"] == 0]
    rewrite = [{"i": x["i"], "text": x["text"], "t": x["t"], "why": x["why"],
                "wins": by_i[x["i"]].get("wins"), "src": by_i[x["i"]].get("src"),
                "画面证据": shot_hint(proj_dir, by_i[x["i"]])}
               for x in miss]
    (d / "qc_rewrite.json").write_text(json.dumps(rewrite, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    judged = sum(1 for x in out if x["judged"])
    print(f"[质检·合并] 判定 {judged}/{len(out)} 句 · 失配 {len(miss)} 句 "
          f"（{100 * len(miss) / max(1, judged):.1f}%）")
    print(f"  → {d/'qc_visual.json'} / {d/'qc_rewrite.json'}")
    for x in miss[:12]:
        print(f"  句{x['i']} t={x['t']:.1f}s [{x['text'][:16]}] {x['why']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--video", default="out/03-rapid-narration.mp4")
    ap.add_argument("--plan", type=int, default=0, help="每批句数，>0 则生成会话内读图批次")
    ap.add_argument("--merge", action="store_true", help="合并会话内判定")
    ap.add_argument("--probe", action="store_true", help="对失配句抽多帧拼复核图，排除单帧假阳性")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    proj_dir = Path(a.project).expanduser().resolve().parent
    video = proj_dir / a.video
    if (a.plan or a.probe or a.report) and not video.exists():
        print(f"[视觉质检] 找不到成片 {video}")
        return 2
    if a.plan:
        build_plan(proj_dir, video, a.plan)
    if a.merge:
        merge(proj_dir)
    if a.probe:
        build_probe(proj_dir, video)
    if a.report:
        p = proj_dir / "json" / "qc_visual.json"
        if not p.exists():
            print("无 qc_visual.json，先 --merge")
            return 2
        rows = json.loads(p.read_text(encoding="utf-8"))
        miss = [x for x in rows if x["match"] == 0]
        print(f"[质检报告] {len(rows)} 句 · 失配 {len(miss)} 句（{100*len(miss)/max(1,len(rows)):.1f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
