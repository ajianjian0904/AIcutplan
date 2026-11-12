# -*- coding: utf-8 -*-
# [L1 共享库] 职责：剧情解析 事件表/块表 + 证据回喂 ｜ 调用者：新旧两链共 10 处 ｜ 产物：json/story_plan.json
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""剧情解析 · 台词 + 抽帧 → DeepSeek 视觉 → 事件表 → 块表

这是「人只审核」那一环的机器部分。三步：

  ① 分段：按有效段把整集切成 150–200s 的窗口（保证一段的台词与画面能塞进一次请求）
  ② 逐段解析：每段给「该段中文字幕（源片秒）」+「该段均匀抽的 12 张截图（每张标注源片秒）」，
     要求模型还原成 8–14 个连续事件：谁、做了什么、什么性质、建议保留几秒
  ③ 合成块表：把全部事件 + 目标成片时长 + 六段式模板喂一次，合并成 12–15 个剧情块，
     给出每块的源区间与目标秒数（OP/ED/标题卡标 skip，不进块表）

产物只写两个新文件，不动既有工程文件：
  json/story_plan.json     分段 / 事件 / 块表 / 目标 / 模型 / 时间
  json/story_frames/       抽帧缓存（可删，重跑会重建）

再由调用方决定是否落 script 骨架与 cutplan 初版（见 write_script_skeleton / write_cutplan）。

为什么不用 batched 拼板：实测单次 12 张 320px 独图，prompt 只 3.3k tokens，
reasoning + 输出约 7k；拼板省不了多少 token，却会丢单帧细节。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 被其它脚本 import 时也能找到同目录模块
import narration as NR
import evidence_feed as EF   # P0-2：已有镜头证据包回喂上游（零新增抽帧/读图）

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache" / "story"

SEG_SYS = ("你是动漫速讲的剧情分析师。用户会给你一段原片的台词（源片秒）和按时间顺序排列的画面截图，"
           "每张截图前标注了它的源片秒。你的任务是还原这一段到底发生了什么，供后面压成 5 分钟解说用。")

SEG_Q = """【台词（源片秒 · 中文译文）】
{lines}

【画面】共 {n} 张截图，每张前面标了源片秒，覆盖 {a:.0f}–{b:.0f}s。

只输出 JSON 数组，不要任何解释、不要 markdown 代码块。每个元素是一个剧情事件：
{{"t0": 数字, "t1": 数字, "who": "出场角色", "event": "发生了什么，14 字以内", "type": "钩子|铺垫|冲突|反转|爽点|收尾", "keep": 整数}}

规则：
1. t0/t1 必须落在 {a:.0f}–{b:.0f} 内，按时间升序、首尾相接、互不重叠。
2. 共 8–14 个事件，粒度是「一个镜头组讲完一件事」，不要把同一个动作拆成两条。
3. keep = 你建议成片里保留多少秒（2–12 的整数），要体现「这集最值得留什么」。
4. 纯片头曲 / 标题卡 / 下集预告 / 广告，type 填 "收录" 且 keep 填 0。
5. 只描述画面和台词能支撑的东西，不要脑补后续剧情。"""

BLK_SYS = ("你是抖音动漫速讲的编导。用户给你一整集的剧情事件表（带源片秒），要求你把它压成一条 "
           "5 分钟左右的解说骨架：分块、定每块的源片区间和成片秒数。只做结构，不写解说词。")

BLK_Q = """【本集信息】
原片时长 {src:.0f}s；目标成片时长 {target:.0f}s；每刀固定 1.32s，共约 {cuts} 刀。
解说覆盖约 94% 的成片时间，中文语速按 4.95 字/秒算，全片旁白预算约 {chars} 字。

【剧情事件表（源片秒）】
{events}

【结构模板】
{template}

要求：只输出 JSON 数组，不要解释、不要代码块。每个元素是一块：
{{"label": "块名（2–4 字，如 前情/自首/反转）", "type": "钩子|铺垫|冲突|反转|爽点|收尾|收录",
  "t0": 数字, "t1": 数字, "target": 数字, "summary": "这块讲什么，30 字以内"}}

规则：
1. t0/t1 是源片秒，取自事件表的时间范围；按时间升序、首尾相接、不重叠，且不要越出 {src:.0f}s。
2. 共 12–15 块。第一块必须是钩子（用本集最抓人的画面，可以从片尾/高潮取，允许倒叙）。
3. target 是该块在成片里的秒数，全片 Σtarget 必须落在 {lo:.0f}–{hi:.0f}s 之间。
4. 单块 target 不低于 8s、不高于 30s；信息量小的过场只给 6–8s。
5. 片头曲 / 标题卡 / 下集预告 / 广告：type 填 "收录"、target 填 0，它们会被丢掉。
6. summary 要写出「谁做了什么、结果如何」，不要写「本段讲述…」这种空话。"""


# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------
def pkey(proj_dir) -> str:
    import hashlib
    return hashlib.md5(str(Path(proj_dir).resolve()).encode()).hexdigest()[:12]


def workdir(proj_dir) -> Path:
    d = CACHE / pkey(proj_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def plan_path(proj_dir) -> Path:
    return Path(proj_dir) / "json" / "story_plan.json"


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def cfg(proj_dir) -> dict:
    pj = read_json(Path(proj_dir) / "project.json", {}) or {}
    rc = dict(pj.get("rapid") or {})
    rc.update(pj.get("recap") or {})
    # 语速优先用本机 TTS 实测标定（含句间静音的有效值），工程里的 5.3 是素材估算值
    try:
        cps = float(NR.calibration()["effective_cps"])
    except Exception:
        cps = float(rc.get("chars_per_sec") or 4.9545)
    return {"src": float(rc.get("src_dur") or 0), "target": float(rc.get("target_dur") or 324),
            "fps": float(pj.get("fps") or 24), "valid": rc.get("valid") or "",
            "cps": cps,
            "silence": float(rc.get("silence") or 0.733),
            "name": pj.get("name") or Path(proj_dir).name}


def source_video(proj_dir) -> Path:
    d = Path(proj_dir)
    a = d / "out" / "01-video-no-ad.mp4"
    if a.exists() and a.stat().st_size > 1024:
        return a
    pj = read_json(d / "project.json", {}) or {}
    s = Path(pj.get("source_video") or d / "ref.mp4")
    return s if s.is_absolute() else (d / s).resolve()


def shotlist(proj_dir) -> list:
    import csv
    f = Path(proj_dir) / "shotlist.csv"
    if not f.exists():
        return []
    try:
        with f.open(encoding="utf-8-sig", newline="") as fh:
            return [{"i": int(r["idx"]), "a": float(r["start"]), "b": float(r["end"]),
                     "br": float(r.get("bright") or 0)}
                    for r in csv.DictReader(fh)]
    except Exception:
        return []


def valid_ranges(proj_dir) -> list:
    """有效段解析：'0-275,601-1655' → [(0,275),(601,1655)]；空则整片。"""
    c = cfg(proj_dir)
    raw = (c["valid"] or "").strip()
    total = c["src"] or 0
    if not raw:
        return [(0.0, total)] if total else []
    out = []
    for part in re.split(r"[,，]", raw):
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$", part)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            if b > a:
                out.append((a, min(b, total) if total else b))
    return out or ([(0.0, total)] if total else [])


def segments(proj_dir, seg_sec: float = 180.0) -> list:
    out = []
    for (a, b) in valid_ranges(proj_dir):
        t = a
        while t < b - 1:
            e = min(b, t + seg_sec)
            out.append({"i": len(out), "a": round(t, 2), "b": round(e, 2)})
            if e >= b:
                break
            t = e
    return out


# --------------------------------------------------------------------------
# 字幕
# --------------------------------------------------------------------------
def zh_subtitles(proj_dir) -> list:
    """中文台词（源片秒）。优先工作区译中结果，缺失时退回原文。"""
    import asr_pipeline as AP
    shim = type("P", (), {"dir": Path(proj_dir)})()
    paras = []
    try:
        st = AP.read_source(shim)
        paras = st.get("paras") or []
    except Exception:
        paras = []
    if not paras:
        return []
    zh = read_json(AP.out_path(shim), {}) or {}
    zp = zh.get("paras") or []
    out = []
    for i, p in enumerate(paras):
        z = zp[i] if i < len(zp) else {}
        out.append({"t0": float(p.get("t0") or 0), "t1": float(p.get("t1") or 0),
                    "text": str(z.get("text") or p.get("text") or "").strip()})
    return out


# --------------------------------------------------------------------------
# 抽帧
# --------------------------------------------------------------------------
def grab(proj_dir, t: float, out: Path) -> bool:
    if out.exists() and out.stat().st_size > 800:
        return True
    import subprocess
    src = source_video(proj_dir)
    if not src.exists():
        return False
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(src),
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", "-y", str(out)],
                       capture_output=True, timeout=90)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 800


def seg_frames(proj_dir, seg: dict, n: int = 12) -> list:
    d = workdir(proj_dir) / "frames"
    d.mkdir(parents=True, exist_ok=True)
    a, b = seg["a"], seg["b"]
    span = max(1.0, b - a)
    n = max(4, min(16, int(n)))
    out = []
    for k in range(n):
        t = a + span * (k + 0.5) / n
        f = d / f"s{seg['i']:02d}_{int(t):05d}.jpg"
        if grab(proj_dir, t, f):
            out.append((round(t, 1), f))
    return out


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------
def _key() -> str:
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not k:
        c = NR.llm_config()
        if not c.get("key_present"):
            raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置")
    return k


def vision_chat(system: str, text: str, images: list, model: str | None = None,
                max_tokens: int = 9000, timeout: int = 900) -> dict:
    """多模态一次调用。images 为 [(标注文本, Path)]，按顺序插在正文之后。"""
    import urllib.request
    import urllib.error
    cfg_ = NR.llm_config()
    model = model or cfg_["model"]
    content = [{"type": "text", "text": text}]
    for label, p in images:
        b64 = base64.b64encode(Path(p).read_bytes()).decode()
        content.append({"type": "text", "text": label})
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
    body = json.dumps({"model": model, "stream": False, "max_tokens": int(max_tokens),
                       "temperature": 0.2,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": content}]}).encode("utf-8")
    req = urllib.request.Request(cfg_["base_url"].rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _key()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"模型 HTTP {e.code}：{NR._safe(e.read().decode('utf-8', 'ignore'))}")
    except Exception as e:
        raise RuntimeError("模型调用失败：" + NR._safe(e))
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    return {"text": msg.get("content") or "", "model": d.get("model", model),
            "usage": d.get("usage") or {}, "reasoning": msg.get("reasoning_content") or ""}


def _json_array(text: str) -> list:
    t = (text or "").strip()
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", t).strip()
    i, j = t.find("["), t.rfind("]")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    arr = json.loads(t)
    if not isinstance(arr, list):
        raise ValueError("模型没返回数组")
    return arr


# --------------------------------------------------------------------------
# ① 分段解析
# --------------------------------------------------------------------------
def parse_segment(proj_dir, seg: dict, model=None, force=False, frames: int = 12) -> dict:
    cache_f = workdir(proj_dir) / f"seg_{seg['i']:02d}.json"
    if cache_f.exists() and not force:
        d = read_json(cache_f, {}) or {}
        if d.get("events"):
            print(f"[剧情] 段 {seg['i'] + 1} 命中缓存（{len(d['events'])} 事件）", flush=True)
            return d

    subs = [s for s in zh_subtitles(proj_dir) if seg["a"] - 0.01 <= s["t0"] < seg["b"]]
    lines = "\n".join(f"[{s['t0']:.1f}-{s['t1']:.1f}] {s['text']}" for s in subs) or "（这一段没有台词）"
    imgs = seg_frames(proj_dir, seg, frames)
    if not imgs:
        raise RuntimeError("抽帧失败：源片不存在或 ffmpeg 不可用")
    q = SEG_Q.format(lines=lines, n=len(imgs), a=seg["a"], b=seg["b"])
    # P0-2 上游证据回喂：12 张均匀截图看不全，补上该段每个镜头的结构化描述
    brief, st = EF.segment_brief(proj_dir, float(seg["a"]), float(seg["b"]))
    if brief:
        q += ("\n\n【镜头证据包（会话内读图产出 · 每镜的客观描述 / 叙事功能 / 台词）】\n"
              + brief + "\n\n注意：上面的截图是均匀抽帧，只覆盖少数时刻；镜头证据包覆盖本段"
              "每一个镜头。判断事件边界、铺垫与反转时以证据包为准，不要把截图没拍到"
              "的内容当成没发生，也不要凭截图脑补后续剧情。")
    labeled = [(f"源片第 {int(t)} 秒：", p) for t, p in imgs]
    print(f"[剧情] 段 {seg['i'] + 1}/{seg['n']} · {seg['a']:.0f}–{seg['b']:.0f}s · "
          f"{len(subs)} 句台词 · {len(imgs)} 帧 · 镜头证据 {st['kept']}/{st['shots']} 镜 "
          f"({st['chars']} 字) → 请求中", flush=True)
    t0 = time.time()
    r = vision_chat(SEG_SYS, q, labeled, model=model, max_tokens=9000)
    if not r["text"].strip():
        raise RuntimeError("模型返回空（推理 token 用尽），把 max_tokens 调大或缩短分段")
    events = _json_array(r["text"])
    data = {"seg": seg, "events": events, "usage": r["usage"], "model": r["model"],
            "elapsed": round(time.time() - t0, 1), "at": datetime.now().isoformat(timespec="seconds")}
    cache_f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[剧情] 段 {seg['i'] + 1} 完成：{len(events)} 事件 · {data['elapsed']}s · "
          f"{r['usage'].get('total_tokens')} tokens", flush=True)
    return data


def evidence_block(proj_dir, a: float, b: float) -> str:
    """P0-2：把该段镜头证据拼到 prompt 末尾（无证据时返回空，保持旧行为）。"""
    brief, _ = EF.segment_brief(proj_dir, a, b)
    if not brief:
        return ""
    return ("\n\n【镜头证据包（会话内读图产出 · 每镜的客观描述 / 叙事功能 / 台词）】\n"
            + brief + "\n\n注意：上面的截图是均匀抽帧，只覆盖少数时刻；镜头证据包覆盖本段"
            "每一个镜头。判断事件边界、铺垫与反转时以证据包为准，不要把截图没拍到"
            "的内容当成没发生，也不要凭截图脑补后续剧情。")


def export_prompts(proj_dir, seg_sec: float = 180.0, frames: int = 12) -> Path:
    """零 API 模式：导出每段完整 prompt（台词 + 截图清单 + 镜头证据）供会话内分析。

    会话内按 SEG_Q 的格式产出事件数组，写成 seg_XX.events.json，再用
    --import-segments 回写段缓存与事件表。
    """
    segs = segments(proj_dir, seg_sec)
    for s in segs:
        s["n"] = len(segs)
    out = proj_dir / "json" / "session_prompts"
    out.mkdir(parents=True, exist_ok=True)
    for s in segs:
        subs = [x for x in zh_subtitles(proj_dir)
                if s["a"] - 0.01 <= x["t0"] < s["b"]]
        lines = "\n".join(f"[{x['t0']:.1f}-{x['t1']:.1f}] {x['text']}" for x in subs) or "（这一段没有台词）"
        imgs = seg_frames(proj_dir, s, frames)
        q = SEG_Q.format(lines=lines, n=len(imgs), a=s["a"], b=s["b"])
        q += evidence_block(proj_dir, float(s["a"]), float(s["b"]))
        (out / f"seg_{s['i']:02d}_prompt.txt").write_text(SEG_SYS + "\n\n" + q,
                                                          encoding="utf-8")
        (out / f"seg_{s['i']:02d}_images.txt").write_text(
            "\n".join(f"{t:.1f}s\t{p}" for t, p in imgs), encoding="utf-8")
    (out / "说明.txt").write_text(
        "逐段读 prompt + 对应图片清单（会话内可直接读图），按 SEG_Q 要求输出事件数组，\n"
        "保存为同目录 seg_XX.events.json（JSON 数组，或 {\"events\": [...]}）。\n"
        "然后：python story_parse.py --project <pj> --import-segments <本目录>\n"
        "注意：导入会重建 story_plan.events，旧 causal / blocks / 句窗映射会失效，\n"
        "务必只在新工程或工程副本上做，不要对已定型的成片工程直接跑。\n", encoding="utf-8")
    print(f"[剧情·会话内] 导出 {len(segs)} 段 prompt → {out}")
    return out


def import_segments(proj_dir, src: Path) -> int:
    """把会话内产出的分段事件写回段缓存 + story_plan.events（会先备份）。"""
    import re as _re
    files = sorted(src.glob("seg_*.events.json"))
    if not files:
        print(f"[剧情·导入] {src} 下没有 seg_*.events.json")
        return 2
    events = []
    for f in files:
        m = _re.search(r"seg_(\d+)", f.name)
        si = int(m.group(1)) if m else 0
        try:
            arr = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[剧情·导入] 跳过坏文件 {f.name}: {e}")
            continue
        rows = arr if isinstance(arr, list) else (arr.get("events") or [])
        for e in rows:
            events.append({**e, "seg": si})
        # 同步写段缓存，下次 parse_segment 会命中
        cf = workdir(proj_dir) / f"seg_{si:02d}.json"
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps({"seg": {"i": si}, "events": rows,
                                  "source": "session-import"}, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    events = _fix_events(events)
    plan = load_plan(proj_dir)
    bak = proj_dir / "json" / "story_plan.json.bak_evidence"
    if not bak.exists():
        bak.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    plan["events"] = events
    save_plan(proj_dir, plan)
    print(f"[剧情·导入] {len(files)} 段 → {len(events)} 事件；原事件表备份 {bak.name}")
    print("  后续：--compose-only 重建块表 → 重跑 causal_graph / cutfirst_plan（旧映射需重建）")
    return 0


def run_parse(proj_dir, seg_sec: float = 180.0, model=None, force=False, frames: int = 12) -> dict:
    segs = segments(proj_dir, seg_sec)
    if not segs:
        raise RuntimeError("拿不到有效段（project.json 的 recap.valid / src_dur 都为空）")
    for s in segs:
        s["n"] = len(segs)
    print(f"[剧情] 共 {len(segs)} 段（每段 {seg_sec:.0f}s）", flush=True)
    events, usages = [], []
    for s in segs:
        d = parse_segment(proj_dir, s, model=model, force=force, frames=frames)
        for e in d.get("events") or []:
            events.append({**e, "seg": s["i"]})
        usages.append(d.get("usage") or {})
    events = _fix_events(events)
    print(f"[剧情] 事件合计 {len(events)} 条 · "
          f"{sum(int(u.get('total_tokens') or 0) for u in usages)} tokens", flush=True)
    return {"segments": segs, "events": events,
            "tokens": sum(int(u.get("total_tokens") or 0) for u in usages)}


def _fix_events(evs: list) -> list:
    out = []
    for e in evs:
        try:
            a, b = float(e.get("t0")), float(e.get("t1"))
        except (TypeError, ValueError):
            continue
        if b <= a:
            continue
        out.append({"t0": round(a, 2), "t1": round(b, 2),
                    "who": str(e.get("who") or "")[:20],
                    "event": str(e.get("event") or "")[:40],
                    "type": str(e.get("type") or "铺垫")[:6],
                    "keep": max(0, min(20, int(float(e.get("keep") or 0)))),
                    "seg": e.get("seg")})
    out.sort(key=lambda x: (x["t0"], x["t1"]))
    # 压掉重叠：后一条的起点不许早于前一条的终点
    for i in range(1, len(out)):
        if out[i]["t0"] < out[i - 1]["t1"] - 0.01:
            out[i]["t0"] = out[i - 1]["t1"]
        if out[i]["t1"] <= out[i]["t0"] + 0.05:
            out[i]["t1"] = round(out[i]["t0"] + 0.5, 2)
    return out


# --------------------------------------------------------------------------
# ② 合成块表
# --------------------------------------------------------------------------
SIX = """1 钩子（前 8–12s）：用本集最抓人的一帧开场，制造疑问
2 回跳（15–25s）：交代前情/身份，把观众留在钩子上
3 危机（40–70s）：主要矛盾坐实，压力上来
4 攻略（60–110s）：主角行动、反转、爽点密度最高的一段
5 大鱼（30–50s）：更大的反派/真相浮出，抬格局
6 撒手（8–12s）：留一个钩子收尾，不总结"""


def compose(proj_dir, events: list, model=None, template: str = "") -> dict:
    c = cfg(proj_dir)
    target = c["target"] or 324
    src = c["src"] or (max((e["t1"] for e in events), default=0))
    cuts = int(round(target / 1.32))
    chars = int(target * 0.938 * c["cps"])
    ev_txt = "\n".join(
        f"[{e['t0']:.0f}-{e['t1']:.0f}] {e['type']}｜{e['who']}｜{e['event']}｜建议留{e['keep']}s"
        for e in events)
    q = BLK_Q.format(src=src, target=target, cuts=cuts, chars=chars,
                     events=ev_txt[:16000], template=template or SIX,
                     lo=target * 0.95, hi=target * 1.04)
    print(f"[剧情] 合成块表：{len(events)} 事件 → 目标 {target:.0f}s / 约 {cuts} 刀 / {chars} 字", flush=True)
    t0 = time.time()
    r = vision_chat(BLK_SYS, q, [], model=model, max_tokens=9000)
    if not r["text"].strip():
        raise RuntimeError("模型返回空（推理 token 用尽），把 max_tokens 调大")
    blocks = _fix_blocks(_json_array(r["text"]), target, src)
    print(f"[剧情] 块表 {len(blocks)} 块 · 合计 {sum(b['target'] for b in blocks):.0f}s · "
          f"{round(time.time() - t0, 1)}s · {r['usage'].get('total_tokens')} tokens", flush=True)
    for b in blocks:
        flag = "  收录" if b["type"] == "收录" else ""
        print(f"    {b['label']:<6} {b['type']:<4} 源 {b['t0']:.0f}–{b['t1']:.0f}s "
              f"→ {b['target']:.0f}s  {b['summary'][:28]}{flag}", flush=True)
    return {"blocks": blocks, "usage": r["usage"], "model": r["model"]}


def _fix_blocks(arr: list, target: float, src: float) -> list:
    out = []
    for b in arr:
        try:
            a, z = float(b.get("t0")), float(b.get("t1"))
            tg = float(b.get("target") or 0)
        except (TypeError, ValueError):
            continue
        if z <= a:
            continue
        ty = str(b.get("type") or "").strip()
        out.append({"label": str(b.get("label") or "块")[:6], "type": ty or "铺垫",
                    "t0": round(max(0.0, a), 2), "t1": round(min(src or z, z), 2),
                    "target": 0.0 if ty == "收录" else max(0.0, round(tg, 1)),
                    "summary": str(b.get("summary") or "")[:80]})
    out.sort(key=lambda x: x["t0"])
    for i in range(1, len(out)):
        if out[i]["t0"] < out[i - 1]["t1"] - 0.01:
            mid = round((out[i - 1]["t1"] + out[i]["t0"]) / 2, 2)
            out[i - 1]["t1"] = max(out[i - 1]["t0"] + 0.5, mid)
            out[i]["t0"] = out[i - 1]["t1"]
    live = [b for b in out if b["type"] != "收录" and b["target"] > 0]
    tot = sum(b["target"] for b in live)
    if live and target > 0 and abs(tot - target) / target > 0.06:
        k = target / tot if tot else 1
        for b in live:
            b["target"] = max(6.0, round(b["target"] * k, 1))
        print(f"[剧情] 总时长 {tot:.0f}s 偏离目标，按 {k:.3f} 归一", flush=True)
    return out


# --------------------------------------------------------------------------
# ③ 落盘 / 骨架
# --------------------------------------------------------------------------
def save_plan(proj_dir, data: dict) -> Path:
    f = plan_path(proj_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    old = read_json(f, {}) or {}
    payload = {**old, **data, "updated": datetime.now().isoformat(timespec="seconds")}
    payload.setdefault("created", payload["updated"])
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return f


def load_plan(proj_dir) -> dict:
    return read_json(plan_path(proj_dir), {}) or {}


def budget(proj_dir, blocks: list) -> dict:
    """按块算字数预算：target × 覆盖率 × 有效语速。"""
    c = cfg(proj_dir)
    live = [b for b in blocks if b["type"] != "收录" and b["target"] > 0]
    total = sum(b["target"] for b in live)
    cov = 0.938
    per = [{"label": b["label"], "sec": b["target"], "chars": int(b["target"] * cov * c["cps"])}
           for b in live]
    return {"total_sec": round(total, 1), "target": c["target"], "chars": int(total * cov * c["cps"]),
            "cps": c["cps"], "coverage": cov, "each": per,
            "diff": round(total - c["target"], 1)}


def write_script_skeleton(proj_dir, blocks: list, overwrite: bool = False) -> dict:
    """写 json/script.json 的块骨架（label + 时间锚点 + 空文案）。

    已有 script.json 时默认不覆盖，只在 overwrite=True 时备份后重写。
    """
    d = Path(proj_dir) / "json"
    f = d / "script.json"
    bak = None
    live = [b for b in blocks if b["type"] != "收录" and b["target"] > 0]
    if f.exists():
        if not overwrite:
            return {"written": False, "reason": "已有 script.json，未覆盖（勾「覆盖文案骨架」再试）",
                    "blocks": len(live)}
        bak = d / f"script.json.bak.{datetime.now():%Y%m%d_%H%M%S}"
        bak.write_bytes(f.read_bytes())
    acc, beats = 0.0, []
    for b in live:
        beats.append({"anchor_new": round(acc, 3), "anchor": round(b["t0"], 2),
                      "beat": b["label"], "text": "",
                      "target": round(b["target"], 1), "type": b["type"],
                      "summary": b["summary"]})
        acc += b["target"]
    f.write_text(json.dumps({"beats": beats}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"written": True, "blocks": len(beats), "duration": round(acc, 1),
            "backup": str(bak) if bak else None}


def write_cutplan(proj_dir, blocks: list, lead: float = 0.10, cut: float = 1.32) -> dict:
    """按块表生成初版 cutplan（成片轴 → 源片轴映射），供 n3 对齐查块源区间。"""
    d = Path(proj_dir) / "json"
    shots = shotlist(proj_dir)
    live = [b for b in blocks if b["type"] != "收录" and b["target"] > 0]
    if not live:
        raise RuntimeError("块表为空，先解析剧情")
    if not shots:
        raise RuntimeError("缺少 shotlist.csv，先做镜头切分")

    plan_f = d / "cutplan.json"
    bak = None
    if plan_f.exists():
        bak = d / f"cutplan.json.bak.{datetime.now():%Y%m%d_%H%M%S}"
        bak.write_bytes(plan_f.read_bytes())

    windows, cur = [], 0.0
    for b in live:
        span = max(0.5, b["t1"] - b["t0"])
        # 块内可铺的源片跨度：块区间比目标长时按目标铺，比目标短时只能压到区间长度
        usable = min(span, b["target"])
        want = max(1, int(round(b["target"] / cut)))
        starts = [s["a"] for s in shots if b["t0"] <= s["a"] < b["t1"] and s["br"] > 18] \
            or [s["a"] for s in shots if b["t0"] <= s["a"] < b["t1"]] \
            or [b["t0"]]
        per = usable / want
        done_in_block = 0.0
        for n in range(want):
            d_want = (b["target"] - done_in_block) if n == want - 1 else per
            # 理想起点：把这一段的源片逛均匀铺进块区间，最后一刀正好收在块尾
            ideal = b["t0"] + (span - d_want) * (n / max(1, want - 1)) if want > 1 else b["t0"]
            a = next((x for x in starts if ideal - 0.02 <= x <= ideal + 1.2), None)
            if a is None:
                a = ideal
            a = round(min(max(a + lead, b["t0"]), max(b["t0"], b["t1"] - 0.3)), 4)
            # 吸附可能把起点推到块尾之外，导致末刀被截：以时长优先，回退到刚好装得下
            if a + d_want > b["t1"] - 0.05 and b["t1"] - d_want >= b["t0"]:
                a = round(b["t1"] - d_want, 4)
            z = round(min(a + max(0.2, d_want), b["t1"]), 4)
            if z - a < 0.2:
                continue
            windows.append({"src_start": a, "src_end": z, "tier": "narr", "label": b["label"],
                            "lines": [], "dur": round(z - a, 4),
                            "new_start": round(cur, 4), "new_end": round(cur + (z - a), 4),
                            "sent_ids": []})
            cur += (z - a)
            done_in_block += (z - a)
    src_dur = round(max((s["b"] for s in shots), default=cur), 3)
    plan_f.write_text(json.dumps({"windows": windows,
                                  "stats": {"cuts": len(windows), "duration": round(cur, 2),
                                            "blocks": len(live), "src_dur": src_dur,
                                            "source": "story_plan"}},
                                 ensure_ascii=False, indent=1), encoding="utf-8")

    # n3 需要 shots-cache.json（镜头边界表）
    bounds = [0.0] + [s["b"] for s in shots]
    (d / "shots-cache.json").write_text(json.dumps({"bounds": bounds, "fps": cfg(proj_dir)["fps"]},
                                                   ensure_ascii=False), encoding="utf-8")
    return {"cuts": len(windows), "duration": round(cur, 2), "blocks": len(live),
            "backup": str(bak) if bak else None}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="剧情解析：台词+抽帧 → 事件表 → 块表")
    ap.add_argument("--project", required=True)
    ap.add_argument("--segment", type=float, default=180.0, help="每段源片秒数")
    ap.add_argument("--frames", type=int, default=12, help="每段抽几张图")
    ap.add_argument("--model", default=None)
    ap.add_argument("--force", action="store_true", help="忽略分段缓存重解析")
    ap.add_argument("--compose-only", action="store_true", help="只重跑块表合成")
    ap.add_argument("--write-script", action="store_true", help="写 script.json 块骨架")
    ap.add_argument("--overwrite", action="store_true", help="允许覆盖已有 script.json（会备份）")
    ap.add_argument("--write-cutplan", action="store_true", help="生成初版 cutplan")
    ap.add_argument("--export-prompt", action="store_true",
                    help="零 API：导出每段完整 prompt（含镜头证据）供会话内分析")
    ap.add_argument("--import-segments", default=None,
                    help="导入会话内产出的 seg_*.events.json 目录，重建事件表（会备份）")
    a = ap.parse_args(argv)

    proj_dir = Path(a.project).expanduser().resolve().parent
    # 会话内模式：不读 llm_config、不调 API，处理完直接返回
    if a.export_prompt or a.import_segments:
        if a.export_prompt:
            export_prompts(proj_dir, seg_sec=a.segment, frames=a.frames)
        if a.import_segments:
            rc = import_segments(proj_dir, Path(a.import_segments).expanduser().resolve())
            if rc:
                return rc
        return 0
    print(f"[剧情] 工程 {proj_dir.name} · 模型 {a.model or NR.llm_config()['model']}", flush=True)

    plan = load_plan(proj_dir)
    events = plan.get("events") or []
    # 只有「首次」或「显式 --force」才重新解析；--write-* / --compose-only 复用已有事件
    need_parse = (not a.compose_only) and (a.force or not events)
    need_compose = a.compose_only or need_parse or not (plan.get("blocks"))
    if need_parse:
        r = run_parse(proj_dir, seg_sec=a.segment, model=a.model, force=a.force, frames=a.frames)
        plan.update({"segments": r["segments"], "events": r["events"],
                     "seg_tokens": r["tokens"], "segment": a.segment, "frames": a.frames})
        save_plan(proj_dir, plan)
        events = r["events"]
    if need_compose:
        if not events:
            print("[剧情] 没有事件可合成，先跑解析", flush=True)
            return 2
        c = compose(proj_dir, events, model=a.model)
        plan.update({"blocks": c["blocks"], "block_model": c["model"],
                     "target": cfg(proj_dir)["target"], "budget": budget(proj_dir, c["blocks"])})
        f = save_plan(proj_dir, plan)
        b = plan["budget"]
        print(f"[剧情] 块表已存 {f}", flush=True)
        print(f"[剧情] 合计 {b['total_sec']:.0f}s / 目标 {b['target']:.0f}s（差 {b['diff']:+.0f}s）· "
              f"旁白预算 {b['chars']} 字", flush=True)

    blocks = plan.get("blocks") or []
    if a.write_script:
        r = write_script_skeleton(proj_dir, blocks, overwrite=a.overwrite)
        print(f"[剧情] script 骨架：{r}", flush=True)
    if a.write_cutplan:
        r = write_cutplan(proj_dir, blocks)
        print(f"[剧情] cutplan 初版：{r}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
