# -*- coding: utf-8 -*-
# [L3 界面层] 职责：web 台 17 步定义 / 环境体检 ｜ 调用者：server ｜ 产物：STEPS / doctor
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""制作流水线 · 步骤定义 / 环境体检 / 后台任务执行器

设计要点：
  · 一集从「新建工程」到「交付」共 15 步，每步只声明：产物、依赖、执行命令。
  · 产物存在性决定步骤状态（ok / stale / missing），stale = 上游比本步新。
  · 同一时刻只允许一个后台任务（ffmpeg 与 TTS 抢磁盘，并发只会更慢）。
  · 只做「调用工程既有脚本 + 记录日志」，不改任何脚本的行为；日志落 cache/jobs/。
  · 所有外部命令都用当前解释器（sys.executable）跑，避免 PATH 上的 python 版本错位。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL = Path(r"C:/Users/10451/.workbuddy/skills/anime-recap/scripts")
JOBS = ROOT / "cache" / "jobs"

PY = sys.executable


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024.0
    return ""


def probe_duration(p: Path) -> float:
    if not p.exists():
        return 0.0
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)],
            capture_output=True, timeout=30)
        return float(r.stdout.decode("utf-8", "ignore").strip() or 0)
    except Exception:
        return 0.0


def ensure_script(proj_dir: Path, name: str) -> Path | None:
    """工程内有同名脚本就用工程的（多数脚本按 cwd 读写），否则从 skill 复制一份过去。

    skill 里的脚本都依赖同目录的 common.py，复制时一并带上，否则 import 会失败。
    """
    local = proj_dir / name
    if local.exists():
        return local
    src = SKILL / name
    if src.exists():
        try:
            shutil.copy2(src, local)
            dep = SKILL / "common.py"
            if dep.exists() and not (proj_dir / "common.py").exists():
                shutil.copy2(dep, proj_dir / "common.py")
            return local
        except OSError:
            return src
    return None


# --------------------------------------------------------------------------
# 工程
# --------------------------------------------------------------------------
def proj_cfg(proj_dir: Path) -> dict:
    return _json(proj_dir / "project.json", {}) or {}


def recap_cfg(proj_dir: Path) -> dict:
    cfg = proj_cfg(proj_dir)
    rc = dict(cfg.get("rapid") or {})
    rc.update(cfg.get("recap") or {})
    rc["name"] = cfg.get("name", proj_dir.name)
    rc["fps"] = cfg.get("fps", 24)
    rc["source_video"] = cfg.get("source_video", "")
    return rc


def asr_files(proj_dir: Path) -> list[Path]:
    return [p for p in (proj_dir.glob("asr_*.json")) if p.name != "asr_zh.json"] + \
           [p for p in [proj_dir / "asr.json"] if p.exists()]


def asr_main(proj_dir: Path) -> Path | None:
    files = asr_files(proj_dir)
    return files[0] if files else None


def asr_lang(proj_dir: Path) -> str:
    p = asr_main(proj_dir)
    if not p:
        return recap_cfg(proj_dir).get("lang", "ja")
    m = re.match(r"asr_([a-z]{2,3})", p.stem)
    return m.group(1) if m else "ja"


# --------------------------------------------------------------------------
# 环境体检
# --------------------------------------------------------------------------
_DOCTOR: dict = {"t": 0.0, "v": None}


def doctor(force: bool = False) -> dict:
    now = time.time()
    if not force and _DOCTOR["v"] and now - _DOCTOR["t"] < 300:
        return _DOCTOR["v"]
    import importlib.util as iu

    def has(bin_name):
        return shutil.which(bin_name) is not None

    def mod(name):
        try:
            return iu.find_spec(name) is not None
        except Exception:
            return False

    v = {
        "python": PY,
        "python_ver": "%d.%d.%d" % sys.version_info[:3],
        "ffmpeg": has("ffmpeg"),
        "ffprobe": has("ffprobe"),
        "faster_whisper": mod("faster_whisper"),
        "edge_tts": mod("edge_tts"),
        "pyJianYingDraft": mod("pyJianYingDraft"),
        "deepseek_key": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()),
        "skill_scripts": SKILL.is_dir(),
        "skill_dir": str(SKILL),
    }
    v["ok"] = v["ffmpeg"] and v["ffprobe"]
    _DOCTOR.update(t=now, v=v)
    return v


# --------------------------------------------------------------------------
# 步骤定义
# --------------------------------------------------------------------------
def _cmd_prepare(proj_dir: Path, p: dict) -> list[str]:
    return [PY, str(ROOT / "prepare_media.py"), "--project", str(proj_dir / "project.json")]


def _cmd_shots(proj_dir: Path, p: dict) -> list[str]:
    argv = ["--project", str(proj_dir / "project.json")]
    if p.get("scene") is not None:
        argv += ["--scene", str(p["scene"])]
    return [PY, str(ROOT / "shots_scan.py")] + argv


def _cmd_asr(proj_dir: Path, p: dict) -> list[str]:
    lang = p.get("lang") or asr_lang(proj_dir)
    size = p.get("size") or "small"
    argv = ["--project", str(proj_dir / "project.json"), "--lang", lang, "--size", size]
    if p.get("force"):
        argv.append("--force")
    return [PY, str(ROOT / "asr_local.py")] + argv


def _cmd_translate(proj_dir: Path, p: dict) -> list[str]:
    argv = ["--project", str(proj_dir / "project.json")]
    if p.get("batch"):
        argv += ["--batch", str(int(p["batch"]))]
    if p.get("limit"):
        argv += ["--limit", str(int(p["limit"]))]
    if p.get("reset"):
        argv.append("--reset")
    return [PY, str(ROOT / "asr_pipeline.py")] + argv


def _cmd_script_script(proj_dir: Path, p: dict):
    return None  # 由「旁白工作台」页面完成，不在流水线里跑


def _cmd_story(proj_dir: Path, p: dict) -> list[str]:
    argv = ["--project", str(proj_dir / "project.json")]
    if p.get("segment"):
        argv += ["--segment", str(float(p["segment"]))]
    if p.get("frames"):
        argv += ["--frames", str(int(p["frames"]))]
    if p.get("force"):
        argv.append("--force")
    if p.get("compose_only"):
        argv.append("--compose-only")
    return [PY, str(ROOT / "story_parse.py")] + argv


def _cmd_narr(proj_dir: Path, p: dict) -> list[str] | None:
    s = ensure_script(proj_dir, "build_narr.py")
    return [PY, str(s)] if s else None


def _cmd_tts(proj_dir: Path, p: dict) -> list[str] | None:
    s = ensure_script(proj_dir, "r5_tts.py")
    if not s:
        return None
    argv = ["--project", str(proj_dir / "project.json")]
    if p.get("redo"):
        argv.append("--redo")
    return [PY, str(s)] + argv


def _cmd_align(proj_dir: Path, p: dict) -> list[str]:
    """先重建初版 cutplan 再对齐：n3 把 cutplan 当旧轴用，二次对齐前必须回到 script 的轴。"""
    return [PY, str(ROOT / "align_chain.py"), "--project", str(proj_dir / "project.json")]


def _cmd_cut(proj_dir: Path, p: dict) -> list[str] | None:
    s = ensure_script(proj_dir, "cut.py")
    return [PY, str(s), "--project", str(proj_dir / "project.json"), "--force"] if s else None


def _cmd_relayout(proj_dir: Path, p: dict) -> list[str]:
    # 句级精确重排（音频压在该句自己的画面上）；旧 relayout_narr 是块内等比近似
    return [PY, str(ROOT / "relayout_smart.py"), "--project", str(proj_dir / "project.json")]


def _cmd_mix(proj_dir: Path, p: dict) -> list[str] | None:
    s = ensure_script(proj_dir, "r6_mix.py")
    return [PY, str(s), "--project", str(proj_dir / "project.json")] if s else None


def _cmd_subtitle(proj_dir: Path, p: dict) -> list[str]:
    argv = ["--project", str(proj_dir / "project.json")]
    if p.get("burn"):
        argv.append("--burn")
    return [PY, str(ROOT / "make_srt.py")] + argv


def _cmd_mux(proj_dir: Path, p: dict) -> list[str] | None:
    s = ensure_script(proj_dir, "r7_mux.py")
    return [PY, str(s), "--project", str(proj_dir / "project.json")] if s else None


STEPS = [
    {"id": "prepare", "group": "素材", "name": "洗片 · 转码/复制 + 抽音",
     "hint": "源片是 h264 8bit 就流复制（快）；10bit / HEVC / 带 ASS 字幕轨则转 H.264 8bit（不然浏览器与剪映都不认）。同时抽一条 48k 双声道 wav 给 ASR 与混音。",
     "cmd": _cmd_prepare, "needs": [], "deps": [], "run_label": "开始洗片"},
    {"id": "shots", "group": "素材", "name": "镜头切分 · 场景变化检测",
     "hint": "ffmpeg 场景检测出切点，再按音频能量标出「有原声」的镜头，写成 shotlist.csv。阈值低一点切得多、高一点会漏切。",
     "cmd": _cmd_shots, "needs": ["scene"], "deps": ["prepare"], "run_label": "重新切分"},
    {"id": "asr", "group": "台词", "name": "语音识别 · faster-whisper",
     "hint": "本地 whisper 转写人物台词，产物 asr_<语言>.json（源片秒 t0/t1 + 原文）。用 small 模型约 3–6 分钟，large 更准但慢一倍。",
     "cmd": _cmd_asr, "needs": ["lang", "size"], "deps": ["prepare"], "run_label": "开始识别"},
    {"id": "translate", "group": "台词", "name": "台词译中 · 分批 DeepSeek",
     "hint": "把原文台词按 24 段一批译成中文，断点续传到 asr/<hash>/asr_zh.json。失败重跑只补缺失批，不重译已完成的。",
     "cmd": _cmd_translate, "needs": [], "deps": ["asr"], "run_label": "开始译中", "page": "/asr"},
    {"id": "story", "group": "文案", "name": "剧情解析 · 台词 + 抽帧",
     "hint": "按有效段切窗，每段把「中文字幕 + 12 张截图」一起给视觉模型，先出事件表，再合成 12–15 个剧情块（源区间 + 成片秒数）。OP/标题卡标「收录」不进块表。第一次约 5–15 分钟。",
     "cmd": _cmd_story, "needs": ["segment", "frames", "force"],
     "deps": ["translate"], "run_label": "开始解析", "page": "/story"},
    {"id": "script", "group": "文案", "name": "剧情文案 · 分块写旁白",
     "hint": "在旁白工作台里和 DeepSeek 一起定每块的解说词，定稿后写回 json/script.json。这一步必须人工参与，流水线不代跑。",
     "cmd": None, "needs": [], "deps": ["story"], "page": "/narration", "run_label": "去写文案"},
    {"id": "narr", "group": "文案", "name": "旁白落稿 · 切听觉句 + 铺时长",
     "hint": "script.json → narration.json：按 14 字切听觉句、用实测语速估时长、顺序铺开。成片时间轴由对齐步骤决定。",
     "cmd": _cmd_narr, "needs": [], "deps": ["script"], "run_label": "生成 narration.json"},
    {"id": "tts", "group": "旁白", "name": "配音 TTS · edge-tts",
     "hint": "逐句合成语音并缓存到 tts/，产物 json/tts-result.json（每句实测时长）。改过的句子才重新合成。",
     "cmd": _cmd_tts, "needs": ["redo"], "deps": ["narr"], "run_label": "开始配音"},
    {"id": "align", "group": "画面", "name": "旁白对齐 · n3 排刀",
     "hint": "按每句实测语音长度算需要几刀（固定 1.32s/刀），生成 cutplan.json + align.json 的句↔刀映射。",
     "cmd": _cmd_align, "needs": [], "deps": ["tts"], "run_label": "开始对齐"},

    {"id": "cut", "group": "画面", "name": "按刀切片 · 重剪无声片",
     "hint": "按 cutplan 逐刀从源片切出并吸附到帧网格，拼出 out/02-video-rapid.mp4。这一步最慢，要读整条源片。",
     "cmd": _cmd_cut, "needs": [], "deps": ["align"], "run_label": "开始切片"},
    {"id": "relayout", "group": "画面", "name": "时间线重排",
     "hint": "cut 之后按最终窗口重建 narration.json，消掉 ±0.3s 的帧吸附累积偏差，保证旁白与画面同源。",
     "cmd": _cmd_relayout, "needs": [], "deps": ["cut"], "run_label": "重排旁白时间轴"},
    {"id": "mix", "group": "合成", "name": "混音 · 旁白铺满 + 原声让位",
     "hint": "旁白几乎不停，只在少数高能块让原声出头，输出 out/mix-rapid.wav。",
     "cmd": _cmd_mix, "needs": [], "deps": ["relayout"], "run_label": "开始混音"},
    {"id": "mux", "group": "合成", "name": "合流成片 · 烧字幕",
     "hint": "画面流直复制（不重编码）+ 混音 + 旁白字幕，输出 out/03-rapid-narration.mp4。画质上限等于片源。",
     "cmd": _cmd_mux, "needs": [], "deps": ["mix"], "run_label": "合成成片"},
    {"id": "subtitle", "group": "合成", "name": "字幕 · 生成 srt + 烧进成片",
     "hint": "按 narration.json 的最终时间轴生成 out/中文旁白-recap.srt；勾「烧入」则重编码成 03-rapid-narration-sub.mp4（3 分钟内，crf 18）。抖音上传只认烧死的字幕，不认软字幕轨。",
     "cmd": _cmd_subtitle, "needs": ["burn"], "deps": ["mux"], "run_label": "生成字幕并烧入"},
    {"id": "review", "group": "校对", "name": "三轨校对 · 逐句定画面",
     "hint": "旁白 / 画面 / 人物台词同轴对齐，逐句确认，给不对的句子改首尾镜。改完要在锚点台写回。",
     "cmd": None, "needs": [], "deps": ["cut"], "page": "/review", "run_label": "去校对"},
    {"id": "anchor", "group": "校对", "name": "写回 cutplan",
     "hint": "把校对时的句级锚点与块级平移写进 cutplan.json，写前自动备份。写回后必须重跑 切片 → 重排 → 混音 → 合流。",
     "cmd": None, "needs": [], "deps": ["review"], "page": "/", "run_label": "去写回"},
    {"id": "deliver", "group": "交付", "name": "预览 / 导出剪映",
     "hint": "临时渲染片段看效果，或导出整片/逐刀剪映草稿继续精修。剪映草稿是单向的，保存后就读不回来了。",
     "cmd": None, "needs": [], "deps": ["mux"], "page": "/export", "run_label": "去交付"},
]


def step_spec(step_id: str) -> dict | None:
    return next((s for s in STEPS if s["id"] == step_id), None)


# --------------------------------------------------------------------------
# 产物扫描
# --------------------------------------------------------------------------
def _shot_rows(proj_dir: Path) -> list[dict]:
    f = proj_dir / "shotlist.csv"
    if not f.exists():
        return []
    import csv
    try:
        with f.open(encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    except Exception:
        return []


def scan(proj_dir: Path) -> list[dict]:
    """返回每一步的产物与状态。状态：ok / stale / missing / blocked。"""
    out: list[dict] = []
    cutter = _mtime(proj_dir / "out" / "02-video-rapid.mp4")

    def arts(*items):
        lst = []
        for rel, label, extra in items:
            p = proj_dir / rel if not Path(rel).is_absolute() else Path(rel)
            if "*" in rel:
                files = sorted(proj_dir.glob(rel))
                lst.append({"path": rel, "label": label, "ok": len(files) > 0,
                            "size": sum(_size(f) for f in files),
                            "extra": extra if extra is not None else f"{len(files)} 个文件"})
                continue
            ok = p.exists() and _size(p) > 1024
            lst.append({"path": rel, "label": label, "ok": ok, "size": _size(p),
                        "mtime": _mtime(p),
                        "extra": extra if extra is not None else (fmt_size(_size(p)) if ok else "缺失")})
        return lst

    cfg = recap_cfg(proj_dir)
    src = Path(cfg.get("source_video") or proj_dir / "ref.mp4")
    ctx = {
        "src_ok": src.exists(),
        "src_dur": probe_duration(src) if src.exists() else 0.0,
        "valid": cfg.get("valid", ""),
        "target": cfg.get("target_dur", 0),
    }

    shots = _shot_rows(proj_dir)
    script = _json(proj_dir / "json" / "script.json", {}) or {}
    beats = script.get("beats") or []
    chars = sum(len(str(b.get("text", ""))) for b in beats)
    narr = _json(proj_dir / "json" / "narration.json", {}) or {}
    items = narr.get("items") or []
    tts = _json(proj_dir / "json" / "tts-result.json", {}) or {}
    titems = tts.get("items") or []
    tts_total = 0.0
    for it in titems:
        try:
            tts_total += float(it.get("dur") or 0)
        except (TypeError, ValueError):
            pass
    plan = _json(proj_dir / "json" / "cutplan.json", {}) or {}
    wins = plan.get("windows") or []
    al = _json(proj_dir / "json" / "align.json", {}) or {}
    sent_beat = al.get("sent_beat") or {}
    n_sent = len(sent_beat) or len(items)

    for s in STEPS:
        sid = s["id"]
        a: list[dict] = []
        if sid == "prepare":
            a = arts(("out/01-video-no-ad.mp4", "无声源片", f"{ctx['src_dur']:.1f}s" if ctx["src_dur"] else None),
                     ("audio/base-no-ad.wav", "基础音轨", None))
            if not ctx["src_ok"]:
                a.insert(0, {"path": "project.json", "label": "源视频", "ok": False,
                             "size": 0, "extra": "project.json 里没找到源视频"})
        elif sid == "shots":
            a = arts(("shotlist.csv", "镜头表",
                      f"{len(shots)} 镜" + (f" · 中位 {sorted(float(r['dur']) for r in shots)[len(shots)//2]:.2f}s"
                                           if shots else "")))
        elif sid == "asr":
            f = asr_main(proj_dir)
            n = len((_json(f, {}) or {}).get("paras") or []) if f else 0
            a = arts((str(f.relative_to(proj_dir)) if f else "asr_ja.json", "识别结果",
                      f"{n} 段" if n else None))
        elif sid == "translate":
            import asr_pipeline as AP
            shim = type("P", (), {"dir": Path(proj_dir)})()
            n = done = 0
            try:
                st = AP.state(shim)
                n, done = int(st["segments"]), int(st["translated"])
            except Exception:
                f = asr_main(proj_dir)
                n = len((_json(f, {}) or {}).get("paras") or []) if f else 0
            a = [{"path": "（工作区缓存）asr/<hash>/asr_zh.json", "label": "中文台词",
                  "ok": bool(n and done >= n), "size": 0, "mtime": 0,
                  "extra": f"{done}/{n} 段已译" if n else "缺 asr 结果"}]
        elif sid == "story":
            plan = _json(proj_dir / "json" / "story_plan.json", {}) or {}
            blk = [b for b in (plan.get("blocks") or []) if b.get("type") != "收录"]
            ev = len(plan.get("events") or [])
            tot = sum(float(b.get("target") or 0) for b in blk)
            a = arts(("json/story_plan.json", "剧情骨架",
                      f"{len(blk)} 块 · {ev} 事件 · {tot:.0f}s" if blk else None))
        elif sid == "script":
            a = arts(("json/script.json", "剧情块文案", f"{len(beats)} 块 · {chars} 字" if beats else None))
        elif sid == "narr":
            a = arts(("json/narration.json", "旁白句表",
                      f"{len(items)} 句 · {sum(len(str(i.get('text',''))) for i in items)} 字" if items else None))
        elif sid == "tts":
            a = arts(("json/tts-result.json", "配音结果",
                      f"{len(titems)} 句 · 共 {tts_total:.1f}s" if titems else None),
                     ("tts/*", "音频缓存", None))
        elif sid == "align":
            a = arts(("json/cutplan.json", "刀表",
                      f"{len(wins)} 刀" if wins else None),
                     ("json/align.json", "句刀映射", f"{n_sent} 句" if n_sent else None))
        elif sid == "cut":
            a = arts(("out/02-video-rapid.mp4", "无声成片", None))
        elif sid == "relayout":
            f = proj_dir / "json" / "narration.json"
            fresh = _mtime(f) >= cutter > 0
            a = arts(("json/narration.json", "旁白时间轴", "已按最终窗口重排" if fresh else "比成片旧，需重排"))
            a[0]["ok"] = bool(fresh)
        elif sid == "mix":
            a = arts(("out/mix-rapid.wav", "混音音轨", None))
        elif sid == "mux":
            a = arts(("out/03-rapid-narration.mp4", "成片", None),
                     ("out/中文旁白-recap.srt", "字幕", None))
        elif sid == "review":
            a = arts(("json/cutplan.json", "待校对刀表", "在 /review 逐句确认" if wins else None))
        elif sid == "anchor":
            a = arts(("json/cutplan.json", "已写回刀表", "改完要在锚点台写回"))
        elif sid == "subtitle":
            sub = proj_dir / "out" / "03-rapid-narration-sub.mp4"
            a = arts(("out/中文旁白-recap.srt", "旁白字幕", None),
                     ("out/03-rapid-narration-sub.mp4", "带字幕成片",
                      "已烧入" if sub.exists() else None))
            for x in a:
                if x["path"].endswith("-sub.mp4"):
                    x["opt"] = True
                    if not x["ok"]:
                        x["extra"] = "未烧入（可选）"
        elif sid == "deliver":
            sub = proj_dir / "out" / "03-rapid-narration-sub.mp4"
            a = arts(("out/03-rapid-narration-sub.mp4", "成片（带字幕）", None)
                     if sub.exists() else ("out/03-rapid-narration.mp4", "成片", None))
            jy_dir = ROOT / "exports" / "jianying"
            n_draft = len([p for p in jy_dir.glob("*") if p.is_dir()]) if jy_dir.is_dir() else 0
            a.append({"path": "exports/jianying/", "label": "剪映草稿", "ok": n_draft > 0,
                      "size": 0, "opt": True,
                      "extra": f"{n_draft} 份（工作区）" if n_draft else "未导出（可选）"})

        done = bool(a) and all(x["ok"] for x in a if x["label"] != "源视频" and not x.get("opt")) \
            and any(x["ok"] for x in a)
        state = "ok" if done else "missing"
        if sid in ("prepare", "shots", "asr") and not ctx["src_ok"]:
            state = "blocked"
        out.append({
            "id": sid, "group": s["group"], "name": s["name"], "hint": s["hint"],
            "deps": s["deps"], "page": s.get("page"), "needs": s["needs"],
            "run_label": s["run_label"], "state": state, "artifacts": a,
            "manual": s["cmd"] is None,
        })
    return out


# --------------------------------------------------------------------------
# 后台任务
# --------------------------------------------------------------------------
class Job:
    def __init__(self, step: str, argv: list[str], cwd: Path):
        self.id = uuid.uuid4().hex[:10]
        self.step = step
        self.argv = argv
        self.cwd = cwd
        self.status = "running"
        self.exit: int | None = None
        self.t0 = time.time()
        self.t1: float | None = None
        self.lines: list[str] = []
        self.proc: subprocess.Popen | None = None
        JOBS.mkdir(parents=True, exist_ok=True)
        self.log = JOBS / f"{datetime.now():%Y%m%d_%H%M%S}_{step}_{self.id}.log"

    def snapshot(self, after: int = 0) -> dict:
        return {
            "id": self.id, "step": self.step, "status": self.status, "exit": self.exit,
            "elapsed": round((self.t1 or time.time()) - self.t0, 1),
            "total": len(self.lines), "lines": self.lines[after:],
            "cmd": " ".join(self.argv), "log": str(self.log),
            "percent": _guess_percent(self.lines),
        }

    def start(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        self._fh = self.log.open("w", encoding="utf-8", errors="replace")
        self._fh.write("$ %s\n$ cd %s\n\n" % (" ".join(self.argv), self.cwd))
        self._fh.flush()
        try:
            self.proc = subprocess.Popen(
                self.argv, cwd=str(self.cwd), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0)
        except Exception as e:
            self.status = "error"
            self.exit = -1
            self.lines.append(f"[启动失败] {e}")
            self._fh.write(f"[启动失败] {e}\n")
            self._fh.close()
            self.t1 = time.time()
            return
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        buf = b""
        try:
            while True:
                chunk = self.proc.stdout.read(4096) if self.proc.stdout else b""
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    self._line(raw)
            if buf:
                self._line(buf)
        finally:
            rc = self.proc.wait() if self.proc else -1
            self.exit = rc
            self.status = "done" if rc == 0 else "error"
            self.t1 = time.time()
            tail = f"\n[{'完成' if rc == 0 else '失败'} · 退出码 {rc} · {self.t1 - self.t0:.1f}s]\n"
            self.lines.append(tail.strip())
            try:
                self._fh.write(tail)
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            _write_index(self)

    def _line(self, raw: bytes):
        s = raw.decode("utf-8", "replace").rstrip("\r")
        if not s.strip():
            return
        # ffmpeg 的 \r 进度条行不刷屏
        self.lines.append(s)
        if len(self.lines) > 4000:
            del self.lines[:1000]
        try:
            self._fh.write(s + "\n")
            self._fh.flush()
        except Exception:
            pass

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.status = "stopped"


def _guess_percent(lines: list[str]) -> int | None:
    for s in reversed(lines[-40:]):
        m = re.search(r"(\d{1,3})%", s)
        if m and int(m.group(1)) <= 100:
            return int(m.group(1))
        m = re.search(r"(\d+)\s*/\s*(\d+)", s)
        if m and int(m.group(2)) > 0 and int(m.group(1)) <= int(m.group(2)):
            return int(int(m.group(1)) * 100 / int(m.group(2)))
    return None


def _write_index(job: Job):
    try:
        idx_f = JOBS / "index.json"
        idx = _json(idx_f, []) or []
        idx.append({"id": job.id, "step": job.step, "status": job.status,
                    "exit": job.exit, "t0": job.t0, "t1": job.t1,
                    "log": job.log.name, "cmd": " ".join(job.argv)})
        idx_f.write_text(json.dumps(idx[-80:], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


class Runner:
    def __init__(self):
        self.job: Job | None = None
        self.lock = threading.Lock()

    def busy(self) -> bool:
        return bool(self.job and self.job.status == "running")

    def start(self, proj_dir: Path, step_id: str, params: dict) -> dict:
        spec = step_spec(step_id)
        if not spec:
            raise RuntimeError("未知步骤")
        if spec["cmd"] is None:
            raise RuntimeError("这一步在页面上完成，不由流水线代跑")
        if self.busy():
            raise RuntimeError(f"已有任务在跑（{self.job.step}），等它结束或先停止")
        argv = spec["cmd"](proj_dir, params or {})
        if not argv:
            raise RuntimeError("找不到这一步需要的脚本（工程目录与 skill 里都没有）")
        with self.lock:
            job = Job(step_id, argv, proj_dir)
            self.job = job
            job.start()
        return job.snapshot(0)

    def stop(self) -> dict | None:
        if not self.job:
            return None
        self.job.stop()
        return self.job.snapshot(0)

    def snapshot(self, after: int = 0) -> dict | None:
        return self.job.snapshot(after) if self.job else None


RUNNER = Runner()


# --------------------------------------------------------------------------
# 新建工程
# --------------------------------------------------------------------------
SKELETON_DIRS = ("audio", "json", "out", "tts", "logs", "verify", "scripts")


def new_project(cfg: dict) -> dict:
    """在指定目录建工程骨架。只写 project.json + 空目录，不动源视频。"""
    raw = (cfg.get("dir") or "").strip()
    if not raw:
        raise RuntimeError("请填工程目录")
    d = Path(raw).expanduser()
    if d.exists() and any(d.iterdir()):
        if not cfg.get("force"):
            raise RuntimeError(f"目录非空：{d}（勾「允许写入已有目录」再试）")
    for sub in SKELETON_DIRS:
        (d / sub).mkdir(parents=True, exist_ok=True)

    video = (cfg.get("video") or "").strip().strip('"')
    if not video:
        raise RuntimeError("请填视频完整路径")
    vp = Path(video).expanduser()
    if not vp.exists():
        raise RuntimeError(f"视频不存在：{vp}")

    name = (cfg.get("name") or d.name).strip()
    target = float(cfg.get("target_dur") or 324)
    lang = (cfg.get("lang") or "ja").strip()
    fps = float(cfg.get("fps") or 24)

    pj = {
        "name": name,
        "workdir": str(d).replace("\\", "/"),
        "source_video": str(vp).replace("\\", "/"),
        "fps": fps,
        "mode": "rapid",
        "rapid": {"target_ratio": round(target / max(1.0, probe_duration(vp)), 4),
                  "narration_pct": 0.938, "chars_per_sec": 5.3,
                  "shot_window": [1.2, 2.4], "shot_window_pref": 1.32},
        "narration": {"voice": "zh-CN-YunxiNeural", "rate": "+22%", "pitch": "+0Hz",
                      "chars_per_sec": 5.3},
        "mix": {"gate_level_db": -50.0, "orig_level_db": 0.0},
        "encode": {"vbitrate": "6M", "abitrate": "192k"},
        "recap": {"lang": lang, "target_dur": target, "valid": "",
                  "chars_per_sec": 4.9545, "silence": 0.733, "true_peak": -1.0},
    }
    f = d / "project.json"
    if f.exists() and not cfg.get("force"):
        raise RuntimeError("已有 project.json，勾「允许写入已有目录」再试")
    f.write_text(json.dumps(pj, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"dir": str(d), "project": pj, "src_dur": probe_duration(vp),
            "files": ["project.json"] + [f"{s}/" for s in SKELETON_DIRS]}


def job_history(limit: int = 12) -> list[dict]:
    idx = _json(JOBS / "index.json", []) or []
    out = []
    for it in reversed(idx[-limit:]):
        lp = JOBS / it.get("log", "")
        out.append({**it, "size": _size(lp), "exists": lp.exists()})
    return out


def job_log_text(name: str, tail: int = 200) -> str:
    p = (JOBS / name).resolve()
    if JOBS.resolve() not in p.parents or not p.exists():
        return ""
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])


# --------------------------------------------------------------------------
# 镜头表读写（镜头台页面）
# --------------------------------------------------------------------------
def shots_table(proj_dir: Path) -> dict:
    rows = []
    for r in _shot_rows(proj_dir):
        try:
            rows.append({
                "idx": int(r["idx"]),
                "start": round(float(r["start"]), 3),
                "end": round(float(r["end"]), 3),
                "dur": round(float(r["dur"]), 3),
                "ost": int(float(r.get("ost") or 0)),
                "motion": float(r.get("motion") or 0),
                "bright": float(r.get("bright") or 0),
            })
        except (KeyError, ValueError):
            continue
    cfg = recap_cfg(proj_dir)
    metrics = _json(proj_dir / "metrics.json", {}) or {}
    src = Path(cfg.get("source_video") or proj_dir / "ref.mp4")
    total = float((metrics.get("source") or {}).get("duration") or 0) or \
        (rows[-1]["end"] if rows else 0)
    durs = sorted(r["dur"] for r in rows)
    return {
        "rows": rows,
        "count": len(rows),
        "total": round(total, 3),
        "ost_count": sum(1 for r in rows if r["ost"]),
        "median": round(durs[len(durs) // 2], 2) if durs else 0,
        "p90": round(durs[min(len(durs) - 1, int(len(durs) * 0.9))], 2) if durs else 0,
        "longest": round(durs[-1], 2) if durs else 0,
        "shortest": round(durs[0], 2) if durs else 0,
        "cuts_per_min": metrics.get("cuts_per_min"),
        "hist": metrics.get("dur_hist"),
        "valid": cfg.get("valid", ""),
        "src": str(src) if src.exists() else "",
        "source_name": src.name,
        "has_file": (proj_dir / "shotlist.csv").exists(),
    }


def _write_shots(proj_dir: Path, rows: list[dict]) -> Path:
    f = proj_dir / "shotlist.csv"
    if f.exists():
        bak = f.with_suffix(f".csv.bak.{datetime.now():%Y%m%d_%H%M%S}")
        bak.write_bytes(f.read_bytes())
    with f.open("w", encoding="utf-8-sig", newline="") as fh:
        fh.write("idx,start,end,dur,motion,bright,ost\n")
        for i, r in enumerate(rows):
            r["idx"] = i + 1
            fh.write(f'{r["idx"]},{r["start"]:.3f},{r["end"]:.3f},{r["dur"]:.3f},'
                     f'{r.get("motion", 0):.1f},{r.get("bright", 0):.1f},{r.get("ost", 0)}\n')
    return f


def edit_shots(proj_dir: Path, action: str, p: dict) -> dict:
    """在镜头表上做最小编辑：合并 / 删除 / 切分 / 挪边界 / 标记原声 / 存有效段。"""
    if action == "valid":
        pj_f = proj_dir / "project.json"
        pj = json.loads(pj_f.read_text(encoding="utf-8"))
        pj.setdefault("recap", {})["valid"] = str(p.get("valid") or "").strip()
        pj_f.write_text(json.dumps(pj, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": True, "table": shots_table(proj_dir)}

    t = shots_table(proj_dir)
    rows = t["rows"]
    if not rows:
        raise RuntimeError("还没有 shotlist.csv，先做镜头切分")
    idx = int(p.get("idx") or 0)
    pos = next((i for i, r in enumerate(rows) if r["idx"] == idx), None)
    if pos is None:
        raise RuntimeError(f"镜头 {idx} 不存在")

    if action == "merge":
        if pos + 1 >= len(rows):
            raise RuntimeError("最后一镜没有下一镜可合并")
        rows[pos]["end"] = rows[pos + 1]["end"]
        rows[pos]["dur"] = round(rows[pos]["end"] - rows[pos]["start"], 3)
        rows[pos]["ost"] = max(rows[pos]["ost"], rows[pos + 1]["ost"])
        rows.pop(pos + 1)
    elif action == "delete":
        if len(rows) <= 1:
            raise RuntimeError("只剩一镜，不能删")
        if pos + 1 < len(rows):
            rows[pos + 1]["start"] = rows[pos]["start"]
            rows[pos + 1]["dur"] = round(rows[pos + 1]["end"] - rows[pos + 1]["start"], 3)
        elif pos > 0:
            rows[pos - 1]["end"] = rows[pos]["end"]
            rows[pos - 1]["dur"] = round(rows[pos - 1]["end"] - rows[pos - 1]["start"], 3)
        rows.pop(pos)
    elif action == "split":
        at = float(p.get("at") or 0)
        r = rows[pos]
        if not (r["start"] + 0.05 < at < r["end"] - 0.05):
            raise RuntimeError("切点必须落在这一镜内部")
        a = dict(r)
        b = dict(r)
        a["end"], b["start"] = round(at, 3), round(at, 3)
        a["dur"] = round(a["end"] - a["start"], 3)
        b["dur"] = round(b["end"] - b["start"], 3)
        rows[pos:pos + 1] = [a, b]
    elif action == "nudge":
        at = float(p.get("at") or 0)
        if pos + 1 >= len(rows):
            raise RuntimeError("最后一镜没有下边界")
        if not (rows[pos]["start"] < at < rows[pos + 1]["end"]):
            raise RuntimeError("边界超出相邻两镜的范围")
        rows[pos]["end"] = rows[pos + 1]["start"] = round(at, 3)
        rows[pos]["dur"] = round(rows[pos]["end"] - rows[pos]["start"], 3)
        rows[pos + 1]["dur"] = round(rows[pos + 1]["end"] - rows[pos + 1]["start"], 3)
    elif action == "ost":
        rows[pos]["ost"] = 0 if rows[pos]["ost"] else 1
    else:
        raise RuntimeError("未知操作")

    _write_shots(proj_dir, rows)
    return {"ok": True, "table": shots_table(proj_dir)}


# --------------------------------------------------------------------------
# 台词表（台词台页面）
# --------------------------------------------------------------------------
def asr_merged(proj_dir: Path) -> dict:
    """ASR 原文 + 中文译文合并成一条可编辑的表。"""
    import asr_pipeline as AP
    src = asr_main(proj_dir)
    paras = ((_json(src, {}) or {}).get("paras") or []) if src else []
    shim = type("P", (), {"dir": Path(proj_dir)})()
    zh_path = AP.out_path(shim)
    zh = _json(zh_path, {}) or {}
    zh_paras = zh.get("paras") or []
    rows = []
    for i, p in enumerate(paras):
        z = zh_paras[i] if i < len(zh_paras) else {}
        rows.append({
            "i": i, "t0": p.get("t0"), "t1": p.get("t1"),
            "orig": str(p.get("text", "")),
            "zh": str(z.get("text", "") or ""),
            "translated": bool(z.get("translated")),
        })
    return {
        "rows": rows, "count": len(rows),
        "translated": sum(1 for r in rows if r["translated"]),
        "lang": asr_lang(proj_dir),
        "source": str(src) if src else "",
        "source_name": src.name if src else "",
        "zh_path": str(zh_path), "has_zh": zh_path.exists(),
        "model": zh.get("model"),
    }


def save_asr_rows(proj_dir: Path, changes: list[dict]) -> dict:
    """只改文本，不动时间戳。原文写回 asr_*.json，译文写回 asr_zh.json。"""
    import asr_pipeline as AP
    src = asr_main(proj_dir)
    if not src:
        raise RuntimeError("工程里没有 asr_*.json")
    data = _json(src, {}) or {}
    paras = data.get("paras") or []
    idx = {int(c["i"]): c for c in changes}
    orig_changed = 0
    for i, p in enumerate(paras):
        if i in idx and "orig" in idx[i]:
            new = str(idx[i]["orig"])
            if new != str(p.get("text", "")):
                p["text"] = new
                orig_changed += 1
    if orig_changed:
        bak = src.with_suffix(src.suffix + f".bak.{datetime.now():%Y%m%d_%H%M%S}")
        bak.write_bytes(src.read_bytes())
        src.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    shim = type("P", (), {"dir": Path(proj_dir)})()
    zh_path = AP.out_path(shim)
    zh = _json(zh_path, {}) or {}
    zp = zh.get("paras") or []
    zh_changed = 0
    for i, row in enumerate(zp):
        if i in idx and "zh" in idx[i]:
            new = str(idx[i]["zh"])
            if new != str(row.get("text", "")):
                row["text"] = new
                row["translated"] = bool(new.strip())
                zh_changed += 1
    if zh_changed:
        if zh_path.exists():
            bak = zh_path.with_suffix(".json.bak." + datetime.now().strftime("%Y%m%d_%H%M%S"))
            bak.write_bytes(zh_path.read_bytes())
        zh["paras"] = zp
        zh["translated"] = sum(1 for r in zp if r.get("translated"))
        zh_path.write_text(json.dumps(zh, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "orig_changed": orig_changed, "zh_changed": zh_changed,
            "table": asr_merged(proj_dir)}

