# -*- coding: utf-8 -*-
# [L3 界面层] 职责：剪映草稿导出 ｜ 调用者：server ｜ 产物：exports/jianying
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""剪映草稿导出（单向）：工程时间线 → 剪映可打开的明文草稿。

设计边界（已在本机剪映 11.8 核实）：
  · 剪映能读脚本生成的明文 draft_content.json；被剪映保存后会变成密文，
    脚本再也读不回来。所以这是交付终点，不是回流环节。
  · 默认只写工作区 exports/jianying/，不碰剪映草稿目录；
    install() 需用户二次确认，且不覆盖同名、不改 root_meta_info.json。
  · 剪映 7+ 隐藏了导出控件，最终导出必须人工在剪映里点。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPORTS = ROOT / "exports" / "jianying"
# 生成草稿要用装了 pyJianYingDraft 的解释器：优先 JY_PYTHON，其次当前解释器，
# 最后找 WorkBuddy 托管 venv。找到的结果缓存，避免每次请求都跑子进程探测。
_JY_PICK: dict = {}


def _py_has_jy(exe: Path) -> bool:
    try:
        r = subprocess.run([str(exe), "-c", "import pyJianYingDraft"],
                           capture_output=True, timeout=25)
        return r.returncode == 0
    except Exception:
        return False


def pick_jy_python() -> Path | None:
    if "py" in _JY_PICK:
        return _JY_PICK["py"]
    cands = []
    if os.environ.get("JY_PYTHON"):
        cands.append(Path(os.environ["JY_PYTHON"]))
    cands.append(Path(sys.executable))
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    cands.append(home / ".workbuddy" / "binaries" / "python" / "envs" / "default" / "Scripts" / "python.exe")
    pick = next((c for c in cands if c.is_file() and _py_has_jy(c)), None)
    _JY_PICK["py"] = pick
    return pick
WORKER = ROOT / "jianying_worker.py"
JY_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft"

MODES = {
    "whole": "整片模式：1 个视频片段 + 1 条混音 + 字幕轨，导入后按整片编辑",
    "segments": "逐刀模式：245 个独立片段 + 1 条混音 + 字幕轨，可在剪映里逐刀调整",
}


def _safe_name(name: str) -> str:
    n = str(name or "").strip()
    if not n or not re.fullmatch(r"[\w\u4e00-\u9fff .()-]{1,60}", n):
        raise ValueError("草稿名只能含中文、字母、数字、空格及 .-_()，长度 1–60")
    if n in (".", "..") or "/" in n or "\\" in n:
        raise ValueError("非法草稿名")
    return n


def env_info() -> dict:
    drafts, plain, encrypted = [], 0, 0
    if JY_DIR.is_dir():
        for d in sorted(JY_DIR.iterdir()):
            f = d / "draft_content.json"
            if not f.is_file():
                continue
            try:
                json.loads(f.read_text(encoding="utf-8"))
                plain += 1
                kind = "明文"
            except Exception:
                encrypted += 1
                kind = "密文"
            drafts.append({"name": d.name, "kind": kind})
    return {
        "draft_dir": str(JY_DIR),
        "exists": JY_DIR.is_dir(),
        "drafts": drafts[-12:],
        "plain": plain,
        "encrypted": encrypted,
        "worker_python": str(pick_jy_python() or ""),
        "worker_ok": bool(pick_jy_python()) and WORKER.is_file(),
        "modes": MODES,
        "note": "剪映能读明文草稿，保存后变密文且不可读回；重复导出会覆盖剪映里的人工修改。",
    }


def _probe(path: Path) -> dict:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        w, h = (r.stdout.strip().split(",") + ["0", "0"])[:2]
        return {"width": int(w) or 1280, "height": int(h) or 720}
    except Exception:
        return {"width": 1280, "height": 720}


def _duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return round(float(r.stdout.strip()), 6)
    except Exception:
        return 0.0


def _fill_meta(folder: Path, plan: dict, fold_path: Path | None = None) -> None:
    """pyJianYingDraft 生成的 meta 是空壳，补上名字/路径/时长，剪映列表才能直接显示。"""
    cp = folder / "draft_content.json"
    j = json.loads(cp.read_text(encoding="utf-8"))
    j["name"] = plan["name"]
    cp.write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")

    mp = folder / "draft_meta_info.json"
    m = json.loads(mp.read_text(encoding="utf-8"))
    now = int(time.time() * 1_000_000)
    m["draft_name"] = plan["name"]
    m["draft_fold_path"] = str(fold_path or folder).replace("\\", "/")
    m["draft_root_path"] = str((fold_path or folder).parent).replace("\\", "/")
    m["tm_duration"] = round(plan["duration"] * 1_000_000)
    m["tm_draft_create"] = m.get("tm_draft_create") or now
    m["tm_draft_modified"] = now
    mp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


def collect(proj) -> dict:
    d = Path(proj.dir)
    c = {
        "source_video": d / "out" / "01-video-no-ad.mp4",   # 逐刀模式的素材（只引用，不切碎）
        "whole_video": d / "out" / "02-video-rapid.mp4",     # 整片模式的无声画面
        "mix": d / "out" / "mix-rapid.wav",
        "srt": d / "out" / "中文旁白-recap.srt",
        "final": d / "out" / "03-rapid-narration.mp4",
    }
    items = []
    for key, p in c.items():
        items.append({"key": key, "path": str(p), "exists": p.is_file(),
                      "size": p.stat().st_size if p.is_file() else 0})
    w = proj.windows
    total = round(float(w[-1]["new_end"]), 4) if w else 0.0
    return {
        "project": {"name": proj.name, "dir": str(d)},
        "files": items,
        "ready": all(x["exists"] for x in items if x["key"] != "final"),
        "cuts": len(w),
        "duration": total,
        "whole_duration": round(_duration(c["whole_video"]), 3) or total,
        "fps": 24,
        "canvas": _probe(c["whole_video"] if c["whole_video"].is_file() else c["source_video"]),
    }


def state(proj) -> dict:
    if not proj:
        return {"ok": False, "error": "工程未载入"}
    ex = []
    if EXPORTS.is_dir():
        for d in sorted(EXPORTS.iterdir()):
            if d.is_dir() and (d / "draft_content.json").is_file():
                ex.append({"name": d.name,
                           "at": datetime.fromtimestamp(d.stat().st_mtime).strftime("%m-%d %H:%M"),
                           "installed": (JY_DIR / d.name).is_dir()})
    return {"ok": True, "env": env_info(), "collect": collect(proj), "exports": ex}


def build_plan(proj, mode: str, name: str) -> dict:
    if mode not in MODES:
        raise ValueError("未知导出模式")
    info = collect(proj)
    if not info["ready"]:
        missing = [x["key"] for x in info["files"] if not x["exists"] and x["key"] != "final"]
        raise RuntimeError("缺少成片产物：" + "、".join(missing) + "。请先跑完混音合片链路。")
    p = {x["key"]: Path(x["path"]) for x in info["files"]}
    if mode == "whole":
        # 无声画面比 cutplan 尾部短约 0.04s，source_timerange 不能超出素材真实时长
        dur = info["whole_duration"]
        clips = [{"src": None, "dst": 0.0, "dur": dur}]
        video = str(p["whole_video"])
    else:
        dur = info["duration"]
        clips = [{"src": round(float(w["src_start"]), 4),
                  "dst": round(float(w["new_start"]), 4),
                  "dur": round(float(w["dur"]), 4)} for w in proj.windows]
        video = str(p["source_video"])
    return {
        "name": _safe_name(name),
        "mode": mode,
        "out_root": str(EXPORTS),
        "width": info["canvas"]["width"],
        "height": info["canvas"]["height"],
        "fps": info["fps"],
        "duration": dur,
        "video_material": video,
        "audio_material": str(p["mix"]),
        "srt": str(p["srt"]) if p["srt"].is_file() else "",
        "clips": clips,
    }


def selfcheck(folder: Path, plan: dict) -> dict:
    f = folder / "draft_content.json"
    res = {"readable": False, "issues": [], "checks": []}
    try:
        j = json.loads(f.read_text(encoding="utf-8"))
        res["readable"] = True
    except Exception as e:
        res["issues"].append("draft_content.json 不是可读明文：" + str(e)[:100])
        return res

    dur_us = int(j.get("duration") or 0)
    want_us = round(plan["duration"] * 1_000_000)
    res["duration_us"] = dur_us
    if abs(dur_us - want_us) > 200_000:
        res["issues"].append(f"时长偏差 {abs(dur_us-want_us)/1e6:.2f}s（草稿 {dur_us/1e6:.2f}s / 期望 {plan['duration']:.2f}s）")
    res["checks"].append(f"总时长 {dur_us/1e6:.2f}s")

    tracks = j.get("tracks", [])
    by_type = {}
    for t in tracks:
        by_type.setdefault(t.get("type"), []).extend(t.get("segments", []))
    vids = by_type.get("video", [])
    texts = by_type.get("text", [])
    auds = by_type.get("audio", [])
    res["tracks"] = {k: len(v) for k, v in by_type.items()}
    if len(vids) != len(plan["clips"]):
        res["issues"].append(f"视频段数 {len(vids)} ≠ 计划 {len(plan['clips'])}")
    res["checks"].append(f"轨道 视频 {len(vids)} 段 / 音频 {len(auds)} 段 / 字幕 {len(texts)} 段")
    if plan.get("srt") and not texts:
        res["issues"].append("SRT 存在但字幕轨为空")

    mats = j.get("materials", {})
    paths = [m.get("path") for m in (mats.get("videos", []) + mats.get("audios", [])) if m.get("path")]
    missing = [x for x in set(paths) if not Path(x).is_file()]
    if missing:
        res["issues"].append("素材路径不存在：" + "、".join(missing[:2]))
    res["checks"].append(f"素材引用 {len(set(paths))} 个文件，全部存在" if not missing else "素材路径缺失")

    spans = sorted((int(s["target_timerange"]["start"]), int(s["target_timerange"]["duration"]))
                   for s in vids if s.get("target_timerange"))
    gaps = [spans[i][0] - (spans[i-1][0] + spans[i-1][1]) for i in range(1, len(spans))]
    worst = max((abs(g) for g in gaps), default=0)
    if worst > 3000:
        res["issues"].append(f"时间轴不连续，最大间隙 {worst/1e6:.3f}s")
    res["checks"].append(f"时间轴连续（最大间隙 {worst/1e6:.4f}s）")

    speeds = [float(s.get("speed", 1.0) or 1.0) for s in vids]
    bad_speed = sum(1 for x in speeds if abs(x - 1.0) > 0.002)
    if bad_speed:
        res["issues"].append(f"{bad_speed} 段被变速（source/target 时长不一致）")
    res["checks"].append("全部片段 speed = 1.0，无变速" if not bad_speed else f"{bad_speed} 段变速")

    meta = folder / "draft_meta_info.json"
    if not meta.is_file():
        res["issues"].append("缺少 draft_meta_info.json")
    else:
        try:
            mj = json.loads(meta.read_text(encoding="utf-8"))
            if mj.get("draft_name") != plan["name"]:
                res["issues"].append("draft_meta_info 的草稿名不一致")
        except Exception as e:
            res["issues"].append("draft_meta_info 不可读：" + str(e)[:80])
    res["ok"] = not res["issues"]
    return res


def build(proj, mode: str, name: str) -> dict:
    plan = build_plan(proj, mode, name)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    plan_file = EXPORTS / f"_plan_{plan['name']}.json"
    plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    py = pick_jy_python()
    if not py:
        raise RuntimeError("未找到装了 pyJianYingDraft 的 Python："
                           "请 pip install pyJianYingDraft==0.3.1，或用环境变量 JY_PYTHON 指定解释器")
    r = subprocess.run([str(py), str(WORKER), str(plan_file)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="ignore", timeout=300)
    if r.returncode != 0:
        raise RuntimeError("草稿生成失败：" + (r.stderr or r.stdout or "")[-500:])
    folder = EXPORTS / plan["name"]
    if not (folder / "draft_content.json").is_file():
        raise RuntimeError("草稿文件未落地：" + str(folder))
    _fill_meta(folder, plan)
    health = selfcheck(folder, plan)
    return {
        "ok": health["ok"],
        "name": plan["name"], "mode": mode,
        "folder": str(folder), "clips": len(plan["clips"]),
        "duration": plan["duration"], "health": health,
        "installed": (JY_DIR / plan["name"]).is_dir(),
    }


def install(name: str, replace: bool = False) -> dict:
    n = _safe_name(name)
    src, dst = EXPORTS / n, JY_DIR / n
    if not (src / "draft_content.json").is_file():
        raise RuntimeError("草稿尚未生成：" + n)
    if not JY_DIR.is_dir():
        raise RuntimeError("剪映草稿目录不存在：" + str(JY_DIR))
    if dst.exists():
        if not replace:
            raise RuntimeError(f"剪映里已有同名草稿「{n}」，请换个名字（不会覆盖）")
        backup = EXPORTS / f"_backup_{n}_{datetime.now():%Y%m%d_%H%M%S}"
        shutil.copytree(dst, backup)
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    plan_file = EXPORTS / f"_plan_{n}.json"
    if plan_file.is_file():
        _fill_meta(dst, json.loads(plan_file.read_text(encoding="utf-8")), fold_path=dst)
    return {
        "ok": True, "name": n, "path": str(dst),
        "hint": "重启剪映，或进出一次其他草稿刷新列表即可看到；剪映保存后此草稿会变密文，请勿重复导出覆盖人工修改。",
    }
