# -*- coding: utf-8 -*-
"""内嵌字幕优先的台词源：MKV/MP4 自带字幕轨时直接提取，跳过 ASR 音转字。

为什么：ASR 是「猜」出来的（本项目 small 模型出现「ついほ?」「エンバーターチュー」
这类幻听，且 faster-whisper 的段时间戳会整体漂移 3–16s）；内嵌字幕是确定性文本，
还常带「（バリオス）」这类说话人标注，专有名词、数字、角色名都不会错。

产物（与现有链路完全兼容，不改下游任何读法）：
  json/ja-segments.json   {segments:[{start,end,text,speaker}], source, sig, ...}
                          ← cut.py 对白映射 / shot_desc.dlg / scene_from_shots / scene_desc
  asr_ja.json             {model:"embedded-subtitle", lang, paras:[{i,t0,t1,text}]}
                          ← asr_pipeline / story_parse.zh_subtitles / narration.py
  asr/<pkey>/asr_zh.json  译中缓存按「时间邻近 + 原文相似度」迁移，条数与新台词源一致
                          （否则 zh_subtitles 按索引取译文会整体错行）

用法：
  python subs_extract.py --project <pj>                    # 只探测对比，不写任何文件
  python subs_extract.py --project <pj> --apply           # 写回（自动改名备份旧产物）
  python subs_extract.py --project <pj> --apply --refresh-derived
  python subs_extract.py --project <pj> --prefer en       # 优先英文字幕
  python subs_extract.py --project <pj> --check           # 校验现有台词源指纹是否过期
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "subviewer"}
IMAGE_CODECS = {"hdmv_pgs_subtitle", "dvb_subtitle", "dvd_subtitle", "xsub", "dvb_teletext"}
LANG_TAG = {"jpn": "ja", "jp": "ja", "ja": "ja", "eng": "en", "en": "en",
            "zho": "zh", "chi": "zh", "zh": "zh", "zh-hans": "zh", "zh-hant": "zh",
            "chs": "zh", "cht": "zh", "kor": "ko", "ko": "ko"}
KANA = re.compile(r"[\u3040-\u30ff]")
HAN = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z]")
TAG = re.compile(r"\{[^}]*\}|<[^>]+>|&\w+;")
SPEAKER = re.compile(r"^[（(]([^）)]{1,12})[）)]\s*")
SRT_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def ffprobe_json(video: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                        "-of", "json", str(video)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {r.stderr[:200]}")
    return json.loads(r.stdout or "{}")


def probe_subs(video: Path) -> list[dict]:
    d = ffprobe_json(video)
    out = []
    for s in d.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        out.append({"index": int(s["index"]), "codec": s.get("codec_name", ""),
                    "language": str(LANG_TAG.get(str(tags.get("language", "")).lower(),
                                                 str(tags.get("language", "")).lower())),
                    "title": str(tags.get("title", "")),
                    "forced": bool(disp.get("forced")),
                    "default": bool(disp.get("default"))})
    return out


def clean_text(raw: str) -> tuple[str, str]:
    """去样式标签、合并换行；把「（角色名）」拆成 speaker 字段。"""
    t = TAG.sub("", raw).replace("\\N", " ").replace("\\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    speaker = ""
    m = SPEAKER.match(t)
    if m:
        speaker = m.group(1).strip()
    return t, speaker


def parse_srt(path: Path) -> list[dict]:
    txt = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    rows = []
    for block in re.split(r"\n\s*\n", txt):
        ls = [x for x in block.splitlines() if x.strip()]
        if len(ls) < 2:
            continue
        ti = next((i for i, x in enumerate(ls) if SRT_TIME.search(x)), None)
        if ti is None:
            continue
        m = SRT_TIME.search(ls[ti])
        g = list(map(int, m.groups()))
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        z = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = " ".join(ls[ti + 1:])
        text, speaker = clean_text(body)
        if not text or z <= a:
            continue
        rows.append({"start": round(a, 3), "end": round(z, 3),
                     "text": text, "speaker": speaker})
    rows.sort(key=lambda x: x["start"])
    return rows


def merge_same_time(rows: list[dict]) -> list[dict]:
    """ASS 常把一句台词按屏幕位置拆成多行（同一 start/end），需合并成一条，
    否则 dlg 会出现「ああ うちのギルドで」「いらねえやつをな。」两条半句。"""
    out: list[dict] = []
    for r in rows:
        if out and abs(out[-1]["start"] - r["start"]) < 0.06 \
                and abs(out[-1]["end"] - r["end"]) < 0.06:
            out[-1]["text"] = (out[-1]["text"] + " " + r["text"]).strip()
            out[-1]["end"] = max(out[-1]["end"], r["end"])
            if r["speaker"] and not out[-1]["speaker"]:
                out[-1]["speaker"] = r["speaker"]
        else:
            out.append(dict(r))
    return out


def extract(video: Path, stream: dict, tmp: Path) -> list[dict]:
    tmp.mkdir(parents=True, exist_ok=True)
    dst = tmp / f"stream{stream['index']}.srt"
    if not dst.exists() or dst.stat().st_size < 100:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
                        "-map", f"0:{stream['index']}", "-c:s", "srt", str(dst)],
                       check=False)
    rows = parse_srt(dst)
    return merge_same_time(rows) if stream["codec"] in ("ass", "ssa") else rows


def sniff_lang(rows: list[dict]) -> str:
    s = "".join(r["text"] for r in rows[:80])
    if KANA.search(s):
        return "ja"
    if LATIN.search(s) and not HAN.search(s):
        return "en"
    if HAN.search(s):
        return "zh"
    return "unknown"


def pick_stream(video: Path, streams: list[dict], tmp: Path,
                prefer: list[str]) -> tuple[dict | None, list[dict]]:
    """逐条提取候选字幕，打分选出最合适的一条。图像字幕直接排除（需 OCR）。"""
    cands = []
    for s in streams:
        if s["codec"] in IMAGE_CODECS:
            print(f"  stream {s['index']} {s['codec']}：图像字幕，需 OCR，跳过")
            continue
        if s["codec"] not in TEXT_CODECS:
            print(f"  stream {s['index']} {s['codec']}：不支持的文本格式，跳过")
            continue
        try:
            rows = extract(video, s, tmp)
        except Exception as e:
            print(f"  stream {s['index']} 提取失败：{type(e).__name__} {str(e)[:80]}")
            continue
        if not rows:
            continue
        lang = s["language"] if s["language"] in prefer else sniff_lang(rows)
        score = 0.0
        if lang in prefer:
            score += 30 - 10 * prefer.index(lang)
        if s["language"] in prefer:
            score += 3
        # SRT 一行一句、最干净；ASS 即使合并了同时间行也常带样式残留，优先降级。
        # 注意不要按「条数多」加分，否则 ASS 的多行拆分会反而胜出。
        score += {"subrip": 8.0, "srt": 8.0, "webvtt": 5.0,
                  "mov_text": 4.0, "ass": 0.0, "ssa": 0.0}.get(s["codec"], 1.0)
        if not s["forced"]:
            score += 0.5
        cands.append({**s, "rows": rows, "lang": lang, "score": score,
                      "speakers": sum(1 for r in rows if r["speaker"])})
        print(f"  stream {s['index']} {s['codec']:<8} lang={lang:<7} "
              f"{len(rows):>4} 条 · 说话人 {sum(1 for r in rows if r['speaker'])} · {s['title']}")
    if not cands:
        return None, []
    cands.sort(key=lambda x: -x["score"])
    return cands[0], cands


def content_sig(video: Path, stream: dict) -> str:
    st = video.stat()
    raw = f"{video.resolve()}|{st.st_size}|{int(st.st_mtime)}|{stream['index']}|{stream['codec']}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def pkey(proj_dir: Path) -> str:
    return hashlib.md5(str(proj_dir.resolve()).encode()).hexdigest()[:12]


def backup(p: Path, tag: str) -> None:
    """复制备份并保留原文件。

    这里所有写回都是 write_text 强制覆盖，不存在「文件存在就跳过」的问题；
    而 shots_desc.json 是 refresh_derived 要**原地修改**的输入，若用改名把它
    移走，下游 scene_from_shots.py 就会读不到（曾实际踩到）。
    r5_tts 那类靠「文件不存在」触发重合成的环节才该用改名。
    """
    if not p.exists():
        return
    dst = p.with_name(p.name + tag)
    if dst.exists():
        dst = p.with_name(p.name + tag + "_" + datetime.now().strftime("%H%M%S"))
    shutil.copy2(p, dst)
    print(f"  备份 {p.name} → {dst.name}")


def norm(s: str) -> str:
    return re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]", "", str(s)).lower()


def migrate_zh(proj_dir: Path, rows: list[dict], lang: str) -> None:
    """译中缓存必须跟新台词源「条数一致」，否则 zh_subtitles 按索引取译文会整体错行。

    用「±20s 时间窗 + 原文相似度」把旧译文迁移过来；匹配不上的保留原文并标
    translated=False，后续可在会话内补译，绝不静默错配。
    """
    d = ROOT / "asr" / pkey(proj_dir)
    d.mkdir(parents=True, exist_ok=True)
    zh_f = d / "asr_zh.json"
    old: list[dict] = []
    if zh_f.exists():
        try:
            old = json.loads(zh_f.read_text(encoding="utf-8")).get("paras") or []
        except Exception:
            old = []
    out, hit = [], 0
    for r in rows:
        window = [z for z in old if abs(float(z.get("t0") or 0) - r["start"]) <= 20]
        best, sc = None, 0.0
        for z in window:
            v = difflib.SequenceMatcher(None, norm(r["text"]),
                                        norm(z.get("text_orig") or z.get("text") or "")).ratio()
            if v > sc:
                best, sc = z, v
        if best and sc >= 0.45 and best.get("translated"):
            text, ok = str(best.get("text") or ""), True
            hit += 1
        else:
            text, ok = r["text"], False
        out.append({"t0": r["start"], "t1": r["end"], "text": text,
                    "text_orig": r["text"], "translated": ok})
    if lang == "zh":  # 中文字幕本身就是译文，无需翻译
        out = [{"t0": r["start"], "t1": r["end"], "text": r["text"],
                "text_orig": r["text"], "translated": True} for r in rows]
        hit = len(rows)
    backup(zh_f, ".bak_subs")
    zh_f.write_text(json.dumps({"source": "embedded-subtitle", "source_lang": lang,
                                "model": "embedded-subtitle",
                                "created": datetime.now().isoformat(timespec="seconds"),
                                "total": len(out), "translated": hit, "paras": out},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  译中缓存 → {len(out)} 条，复用译文 {hit} 条（未匹配保留原文）")


def refresh_derived(proj_dir: Path, rows: list[dict]) -> None:
    """只重算确定性派生数据，零读图、零 API。"""
    p = proj_dir / "json" / "shots_desc.json"
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        for r in d.get("rows", []):
            hits = [s["text"] for s in rows
                    if s["start"] < r["b"] and s["end"] > r["a"] and s["text"]]
            r["dlg"] = " / ".join(t[:24] for t in hits[:2])
        d["subtitle_source"] = "embedded-subtitle"
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  shots_desc.dlg 重算 {len(d.get('rows', []))} 镜")
    sc = ROOT / "scene_from_shots.py"
    pj = proj_dir / "project.json"
    if sc.exists() and pj.exists():
        r = subprocess.run([sys.executable, str(sc), "--project", str(pj)],
                           capture_output=True, text=True)
        print("  scene_from_shots:", (r.stdout or r.stderr).strip().splitlines()[-1][:100])


def dry_run(proj_dir: Path, rows: list[dict], stream: dict, cur: list[dict]) -> None:
    print(f"\n=== 对比（只读，未写任何文件）===")
    print(f"  内嵌字幕  {len(rows):>4} 条  {rows[0]['start']:.2f}–{rows[-1]['end']:.2f}s  "
          f"lang={stream['lang']} stream={stream['index']}/{stream['codec']}")
    if cur:
        print(f"  现有 ASR  {len(cur):>4} 条  {cur[0]['start']:.2f}–{cur[-1]['end']:.2f}s")
    print("\n  前 6 条字幕：")
    for r in rows[:6]:
        who = f"[{r['speaker']}] " if r["speaker"] else ""
        print(f"    {r['start']:>8.2f}-{r['end']:>8.2f}  {who}{r['text'][:40]}")
    if cur:
        print("\n  现有 ASR 前 4 条（对照）：")
        for r in cur[:4]:
            print(f"    {float(r['start']):>8.2f}-{float(r['end']):>8.2f}  {r['text'][:40]}")
    print("\n  执行 --apply 后：ja-segments / asr_ja / 译中缓存 会改名备份再写回；"
          "\n  --refresh-derived 会重算 shots_desc.dlg 与 scene_desc（零读图、零 API）。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--prefer", default="ja,zh,en", help="语言优先级，逗号分隔")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--refresh-derived", action="store_true")
    ap.add_argument("--check", action="store_true", help="只校验现有台词源指纹")
    ap.add_argument("--video", default=None, help="覆盖 project.json 的 source_video")
    a = ap.parse_args()

    pj_f = Path(a.project).expanduser().resolve()
    cfg = json.loads(pj_f.read_text(encoding="utf-8"))
    proj_dir = pj_f.parent
    video = Path(a.video or cfg.get("source_video") or "").expanduser()
    if not video.is_absolute():
        video = proj_dir / video
    seg_f = proj_dir / "json" / "ja-segments.json"
    cur: list[dict] = []
    if seg_f.exists():
        try:
            cur = json.loads(seg_f.read_text(encoding="utf-8")).get("segments") or []
        except Exception:
            cur = []

    if a.check:
        old = json.loads(seg_f.read_text(encoding="utf-8")) if seg_f.exists() else {}
        print("台词源:", old.get("source", "asr/未知"), "| sig:", old.get("sig", "无"))
        print("源片:", video, "| 存在:", video.exists())
        return 0

    if not video.exists():
        print(f"找不到源片：{video}")
        return 2
    streams = probe_subs(video)
    print(f"源片 {video.name}\n检测到 {len(streams)} 条字幕轨：")
    if not streams:
        print("  无内嵌字幕 → 应走 ASR（asr_local.py）")
        return 1
    tmp = proj_dir / "verify" / "subs"
    best, cands = pick_stream(video, streams, tmp, [x.strip() for x in a.prefer.split(",") if x.strip()])
    if not best:
        print("没有可用的文本字幕轨 → 回退 ASR")
        return 1
    rows = best["rows"]

    if not a.apply:
        dry_run(proj_dir, rows, best, cur)
        return 0

    tag = ".bak_subs"
    print(f"\n=== 应用（stream {best['index']} / {best['codec']} / {best['lang']}）===")
    for f in (seg_f, proj_dir / "asr_ja.json",
              proj_dir / "json" / "shots_desc.json", proj_dir / "json" / "scene_desc.json"):
        backup(f, tag)
    sig = content_sig(video, best)
    seg_f.parent.mkdir(parents=True, exist_ok=True)
    seg_f.write_text(json.dumps({
        "segments": rows, "source": "embedded-subtitle", "lang": best["lang"],
        "stream": best["index"], "codec": best["codec"], "sig": sig,
        "video": str(video), "count": len(rows),
        "updated": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (proj_dir / "asr_ja.json").write_text(json.dumps({
        "model": "embedded-subtitle", "lang": best["lang"],
        "paras": [{"i": i, "t0": r["start"], "t1": r["end"], "text": r["text"]}
                  for i, r in enumerate(rows)],
        "sig": sig}, ensure_ascii=False, indent=1), encoding="utf-8")
    (proj_dir / "json" / "subs_meta.json").write_text(json.dumps({
        "sig": sig, "video": str(video), "stream": best["index"],
        "codec": best["codec"], "lang": best["lang"], "count": len(rows),
        "speakers": best["speakers"], "candidates": [
            {k: v for k, v in c.items() if k != "rows"} for c in cands]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  ja-segments.json / asr_ja.json → {len(rows)} 条（sig={sig}）")
    migrate_zh(proj_dir, rows, best["lang"])
    if a.refresh_derived:
        refresh_derived(proj_dir, rows)
    rapid = proj_dir / "json" / "ja-segments-rapid.json"
    if rapid.exists():
        print(f"  注意：{rapid.name} 仍是旧台词源的对白映射（由 cut.py 生成）。"
              "\n        要出字幕或剪映草稿时重跑 cut.py 刷新；成片渲染不读它。")
    print("完成。成片未重渲染。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
