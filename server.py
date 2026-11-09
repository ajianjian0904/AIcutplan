#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""锚点校对台 · 本地服务（Python 标准库，零依赖）

只做一件事：让人按「剧情块」整体平移源片取段，然后写回 cutplan.json。
不接管渲染、混音、配音，也不开放逐刀编辑。

启动： python server.py  →  http://127.0.0.1:8765
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import narration as NR
import jianying_export as JY
import asr_pipeline as AP
import sentence_studio as SS
import preview_render as PR
import review as RV

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
FRAME_CACHE = ROOT / "cache" / "frames"
CLIP_CACHE = ROOT / "cache" / "clips"
STATE_FILE = ROOT / "studio_state.json"
PORT = 8765
# 默认工程从环境变量读；未设置时先看 studio_state.json 里上次载入的目录。
# 仓库里不写死任何本机路径。
DEFAULT_PROJECT = os.environ.get("RECAP_PROJECT", "")
HEAD_IN = 0.10          # 规则一：从镜头开头稍后一点进，避开转场黑场
FRAME_LOCK = threading.Lock()

for d in (FRAME_CACHE, CLIP_CACHE):
    d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# 工程
# --------------------------------------------------------------------------
class Project:
    def __init__(self, dirpath: str):
        self.dir = Path(dirpath).resolve()
        if not self.dir.is_dir():
            raise ValueError(f"工程目录不存在：{self.dir}")

        self.cutplan_path = self.dir / "json" / "cutplan.json"
        self.align_path = self.dir / "json" / "align.json"
        self.narr_path = self.dir / "json" / "narration.json"
        self.script_path = self.dir / "json" / "script.json"
        self.shotlist_path = self.dir / "shotlist.csv"
        for p in (self.cutplan_path, self.align_path, self.narr_path, self.shotlist_path):
            if not p.exists():
                raise ValueError(f"缺少 {p.name}，不是完整的漫剪工程")

        cfg = {}
        pj = self.dir / "project.json"
        if pj.exists():
            cfg = json.loads(pj.read_text(encoding="utf-8"))
        self.cfg = cfg
        self.name = cfg.get("name", self.dir.name)
        self.fps = float(cfg.get("fps", 24))

        # cut.py 的真实输入，时间基准必须与 cutplan.src_start 一致
        cut_src = self.dir / "out" / "01-video-no-ad.mp4"
        self.video = cut_src if cut_src.exists() else Path(
            cfg.get("source_video", str(self.dir / "ref.mp4")))
        if not self.video.exists():
            self.video = self.dir / "ref.mp4"

        self._read()

    def _read(self):
        plan = json.loads(self.cutplan_path.read_text(encoding="utf-8"))
        self.windows = plan["windows"]
        self.stats = plan.get("stats", {})
        self.src_dur = float(self.stats.get("src_dur") or 1420.0)
        self.out_dur = float(self.stats.get("out_dur_actual") or self.stats.get("out_dur") or 0)
        self.plan = plan

        al = json.loads(self.align_path.read_text(encoding="utf-8"))
        self.beat_win = {int(k): [int(v[0]), int(v[1])] for k, v in al["beat_win"].items()}
        self.sent_beat = {int(k): int(v) for k, v in al.get("sent_beat", {}).items()}

        narr = json.loads(self.narr_path.read_text(encoding="utf-8"))
        self.sents = narr.get("items", [])

        self.labels = {}
        if self.script_path.exists():
            sc = json.loads(self.script_path.read_text(encoding="utf-8"))
            for i, b in enumerate(sc.get("beats", [])):
                self.labels[i] = str(b.get("beat", f"块 {i}"))

        self.shots = []
        with self.shotlist_path.open("r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                self.shots.append({
                    "idx": int(r["idx"]),
                    "start": float(r["start"]),
                    "end": float(r["end"]),
                    "dur": float(r["dur"]),
                    "motion": float(r.get("motion", 0) or 0),
                    "bright": float(r.get("bright", 0) or 0),
                    "ost": int(float(r.get("ost", 0) or 0)),
                })
        self.shot_starts = [s["start"] for s in self.shots]

    def beat_ids(self):
        return sorted(self.beat_win)

    def beat_info(self, b: int, delta: float = 0.0):
        i0, i1 = self.beat_win[b]
        seg = self.windows[i0:i1]
        sents = [s for s in self.sents if self.sent_beat.get(int(s["i"]), s.get("beat", -1)) == b]
        first, last = seg[0]["src_start"], seg[-1]["src_end"]
        durs = [float(w.get("dur") or (w["new_end"] - w["new_start"])) for w in seg]
        return {
            "id": b,
            "label": self.labels.get(b, f"块 {b}"),
            "win": [i0, i1],
            "cuts": len(seg),
            "sents": len(sents),
            "src": [round(first, 3), round(last, 3)],
            "src_shifted": [round(first + delta, 3), round(last + delta, 3)],
            "new": [round(seg[0]["new_start"], 3), round(seg[-1]["new_end"], 3)],
            "sec_per_cut": round(sum(durs) / max(1, len(durs)), 3),
            "sentences": [{"text": s["text"], "dur": s.get("dur", 0)} for s in sents[:40]],
            "delta": round(delta, 3),
        }

    def snap_delta(self, b: int, raw_delta: float) -> float:
        """把首刀 src_start 吸附到最近的镜头开头 +0.10（规则一）。"""
        i0, _ = self.beat_win[b]
        target = self.windows[i0]["src_start"] + raw_delta
        i = bisect.bisect_left(self.shot_starts, target - HEAD_IN)
        cands = self.shot_starts[max(0, i - 2): i + 3]
        if not cands:
            return raw_delta
        best = min(cands, key=lambda s: abs(s + HEAD_IN - target))
        return round((best + HEAD_IN) - self.windows[i0]["src_start"], 4)

    def bounds_ok(self, b: int, delta: float):
        i0, i1 = self.beat_win[b]
        a = self.windows[i0]["src_start"] + delta
        z = self.windows[i1 - 1]["src_end"] + delta
        if a < 0:
            return False, f"平移后起点 {a:.2f}s 超出源片开头"
        if z > self.src_dur:
            return False, f"平移后终点 {z:.2f}s 超出源片时长 {self.src_dur:.2f}s"
        return True, ""


# --------------------------------------------------------------------------
# 会话状态（独立于工程文件，未写回前不碰磁盘上的 cutplan）
# --------------------------------------------------------------------------
def _anchors_dump(anchors: dict) -> dict:
    """JSON 的对象键只能是字符串，covers 的刀号要显式转回来。"""
    out = {}
    for sid, a in anchors.items():
        b = dict(a)
        b["covers"] = {str(k): v for k, v in a.get("covers", {}).items()}
        out[str(sid)] = b
    return out


def _anchors_load(raw) -> dict:
    out = {}
    for sid, a in (raw or {}).items():
        try:
            b = dict(a)
            b["covers"] = {int(k): v for k, v in a.get("covers", {}).items()}
            b["wins"] = [int(x) for x in a.get("wins", [])]
            out[int(sid)] = b
        except Exception:
            continue
    return out


class State:
    def __init__(self):
        self.project_dir = DEFAULT_PROJECT
        self.proj: Project | None = None
        self.deltas: dict[int, float] = {}
        # 句级锚点：{句id: compute_anchor(...)}。与块级 Δ 分层共存，写回时句级优先。
        self.sent_anchors: dict[int, dict] = {}
        self.selected_sent: int | None = None
        self.confirmed: set[int] = set()          # 块级确认（锚点台）
        self.confirmed_sents: set[int] = set()   # 句级确认（逐句校对台），两套并存互不覆盖
        self.current = 0
        self.undo: list = []
        self.redo: list = []
        self.applied: dict[int, float] = {}
        self.applied_sents = 0
        self.load()

    def load(self):
        raw_deltas, raw_anchors, raw_sel = {}, {}, None
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self.project_dir = d.get("project_dir", DEFAULT_PROJECT)
                self.confirmed = set(d.get("confirmed", []))
                self.confirmed_sents = set(d.get("confirmed_sents", []))
                self.applied = {int(k): float(v) for k, v in d.get("applied", {}).items()}
                self.current = int(d.get("current", 0))
                raw_deltas = d.get("deltas", {}) or {}
                raw_anchors = d.get("sent_anchors", {}) or {}
                raw_sel = d.get("selected_sent")
            except Exception:
                pass
        try:
            self.proj = Project(self.project_dir)
        except Exception as e:
            self.error = str(e)
            # 状态文件可能指向已删除/移动的目录，回退默认工程，保证服务可用
            try:
                if not DEFAULT_PROJECT:
                    raise ValueError("未设置默认工程（可用环境变量 RECAP_PROJECT 指定，"
                                     "或启动后在界面里点「载入工程」）")
                self.proj = Project(DEFAULT_PROJECT)
                self.project_dir = str(self.proj.dir)
                self.applied.clear()      # 旧工程的累计值不应带过来
                self.confirmed.clear()
                self.confirmed_sents.clear()
                self.deltas.clear()
                self.sent_anchors.clear()
                self.selected_sent = None
            except Exception:
                self.proj = None
        if self.proj:
            self._restore_session(raw_deltas, raw_anchors, raw_sel)

    def _restore_session(self, raw_deltas, raw_anchors, raw_sel):
        """未写回的块级平移 / 句级锚点跨服务重启保留，并校验对当前工程仍然有效。"""
        p = self.proj
        try:
            self.deltas = {int(b): float(v) for b, v in raw_deltas.items()
                           if int(b) in p.beat_win and abs(float(v)) > 1e-9}
        except Exception:
            self.deltas = {}
        smap = SS.sent_map(p)
        out = {}
        for sid, a in _anchors_load(raw_anchors).items():
            if sid not in smap:
                continue
            try:
                covers = {int(k): [float(v[0]), float(v[1])]
                          for k, v in a["covers"].items()
                          if 0 <= int(k) < len(p.windows)}
            except Exception:
                continue
            if set(covers) != set(smap[sid]["wins"]):
                continue
            a["covers"] = covers
            a["wins"] = smap[sid]["wins"]
            out[sid] = a
        self.sent_anchors = out
        try:
            self.selected_sent = (int(raw_sel) if raw_sel is not None
                                  and int(raw_sel) in smap else None)
        except Exception:
            self.selected_sent = None

    def persist(self):
        STATE_FILE.write_text(json.dumps({
            "project_dir": self.project_dir,
            "confirmed": sorted(self.confirmed),
            "confirmed_sents": sorted(self.confirmed_sents),
            "applied": self.applied,
            "current": self.current,
            "deltas": {str(k): v for k, v in self.deltas.items()},
            "sent_anchors": _anchors_dump(self.sent_anchors),
            "selected_sent": self.selected_sent,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    def snapshot(self):
        if not self.proj:
            return {"ok": False, "error": getattr(self, "error", "工程未载入")}
        p = self.proj
        beats = [p.beat_info(b, self.deltas.get(b, 0.0)) for b in p.beat_ids()]
        return {
            "ok": True,
            "data": {
                "project": {
                    "dir": str(p.dir), "name": p.name,
                    "src_dur": round(p.src_dur, 2), "out_dur": round(p.out_dur, 2),
                    "windows": len(p.windows), "beats": len(beats),
                    "shots": len(p.shots), "video": str(p.video), "fps": p.fps,
                },
                "beats": beats,
                "current": self.current,
                "confirmed": sorted(self.confirmed),
                "confirmed_sents": sorted(self.confirmed_sents),
                "applied": self.applied,
                "dirty": (any(abs(v) > 1e-6 for v in self.deltas.values())
                          or bool(self.sent_anchors)),
                "deltas": {str(k): v for k, v in self.deltas.items()},
                "sent_anchors": sorted(self.sent_anchors),
                "sent_count": len(self.sent_anchors),
                "selected_sent": self.selected_sent,
                "applied_sents": self.applied_sents,
                # 所有返回 state 的接口都要带门禁标记，否则前端刷新后会短暂解禁写回
                "narr_locked": NR.has_unfinalized_draft(self.project_dir),
            },
        }

    def set_delta(self, b: int, val: float, push=True):
        old = self.deltas.get(b, 0.0)
        if abs(old - val) < 1e-6:
            return
        if push:
            self.undo.append({"kind": "beat", "beat": b, "from": old, "to": val})
            self.redo.clear()
        if abs(val) < 1e-6:
            self.deltas.pop(b, None)      # 复位后不留 0.0 残项
        else:
            self.deltas[b] = round(val, 4)
        self.persist()

    def set_sent_anchor(self, sid: int, anchor):
        old = self.sent_anchors.get(sid)
        if anchor is None:
            if old is None:
                return
            self.sent_anchors.pop(sid, None)
        else:
            self.sent_anchors[sid] = anchor
        self.undo.append({"kind": "sent", "sid": sid, "from": old, "to": anchor})
        self.redo.clear()
        self.persist()

    def apply_undo_op(self, op, key):
        v = op.get(key)
        if op.get("kind", "beat") == "sent":
            if v is None:
                self.sent_anchors.pop(op["sid"], None)
            else:
                self.sent_anchors[op["sid"]] = v
        else:
            self.deltas[op["beat"]] = v if v is not None else 0.0
        self.persist()

    def save_to_disk(self):
        """只写 cutplan.json 的 src_start/src_end，其余字段一个都不碰。

        两层改动合并：先应用块级 Δ，再用句级锚点覆盖（句级优先）。
        句级只动本句那几刀，不挪任何其他刀的刀位 → 天然避开级联漂移。
        """
        if not self.proj:
            raise RuntimeError("工程未载入")
        p = self.proj
        merged = SS.merge_sources(p, self.deltas, self.sent_anchors)
        changed_beats = {b: d for b, d in self.deltas.items() if abs(d) > 1e-6}
        if not merged:
            return {"written": 0, "backup": None, "sent_cuts": 0, "beats": []}
        plan = json.loads(p.cutplan_path.read_text(encoding="utf-8"))
        wins = plan["windows"]
        for k, (a, z) in merged.items():
            wins[k]["src_start"] = a
            wins[k]["src_end"] = z
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = p.cutplan_path.with_suffix(f".json.bak.{ts}")
        shutil.copy2(p.cutplan_path, bak)
        tmp = p.cutplan_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p.cutplan_path)
        for b, d in changed_beats.items():
            self.applied[b] = round(self.applied.get(b, 0.0) + d, 3)
        n_sent = len(self.sent_anchors)
        self.applied_sents += n_sent
        self.deltas.clear()
        self.sent_anchors.clear()
        self.undo.clear()
        self.redo.clear()
        p._read()
        self.persist()
        return {"written": len(changed_beats), "backup": str(bak),
                "beats": sorted(changed_beats), "sent_cuts": len(merged),
                "sent_anchors": n_sent}


ST = State()


# --------------------------------------------------------------------------
# 媒体
# --------------------------------------------------------------------------
def frame_path(t: float) -> Path:
    t = max(0.0, round(float(t), 2))
    key = hashlib.md5(f"{ST.project_dir}|{t:.2f}".encode()).hexdigest()[:16]
    out = FRAME_CACHE / f"{key}.jpg"
    if out.exists() and out.stat().st_size > 100:
        return out
    if not ST.proj or not ST.proj.video.exists():
        raise RuntimeError("源视频不存在")
    tmp = FRAME_CACHE / f"{key}.{os.getpid()}.tmp.jpg"
    with FRAME_LOCK:
        if not out.exists():
            cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}",
                   "-i", str(ST.proj.video), "-frames:v", "1",
                   "-vf", "crop=iw:ih*0.80:0:0,scale=320:-1",
                   "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4",
                   "-y", str(tmp)]
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode != 0 or not tmp.exists():
                raise RuntimeError("抽帧失败：" + r.stderr.decode("utf-8", "ignore")[-300:])
            os.replace(tmp, out)
    return out


def clip_path(t: float, tag: str) -> Path:
    t = max(0.0, round(float(t), 2))
    pkey = hashlib.md5(ST.project_dir.encode()).hexdigest()[:8]
    out = CLIP_CACHE / f"{pkey}_{tag}_{t:.2f}.mp4"
    if out.exists() and out.stat().st_size > 1000:
        return out
    if not ST.proj or not ST.proj.video.exists():
        raise RuntimeError("源视频不存在")
    tmp = CLIP_CACHE / f"{pkey}_{tag}_{t:.2f}.tmp.mp4"
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(ST.proj.video),
           "-t", "3", "-vf", "crop=iw:ih*0.80:0:0,scale=480:-1", "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
           "-pix_fmt", "yuv420p", "-y", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, timeout=180)
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError("片段生成失败：" + r.stderr.decode("utf-8", "ignore")[-300:])
    os.replace(tmp, out)
    return out


def rebuild_commands():
    if not ST.proj:
        return []
    d = ST.proj.dir
    skill = Path(r"C:/Users/10451/.workbuddy/skills/anime-recap/scripts")
    pj = d / "project.json"
    cut = skill / "cut.py"
    r6 = d / "scripts" / "r6_mix.py"
    if not r6.exists():
        r6 = skill / "r6_mix.py"
    r7 = skill / "r7_mux.py"
    relayout = d / "relayout_narr.py"
    if not relayout.exists():
        relayout = skill / "relayout_narr.py"
    return [
        f'cd /d "{d}"',
        f'python "{cut}" --force --project "{pj}"',
        f'python "{relayout}"',
        f'python "{r6}" --project "{pj}"',
        f'python "{r7}" --project "{pj}"',
    ]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "AnchorStudio/1.0"

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def err(self, msg, code=400):
        self.json({"ok": False, "error": str(msg)}, code)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._file(WEB / "index.html", "text/html; charset=utf-8")
            if p == "/app.js":
                return self._file(WEB / "app.js", "application/javascript; charset=utf-8")
            if p == "/style.css":
                return self._file(WEB / "style.css", "text/css; charset=utf-8")
            if p in ("/narration", "/narration.html"):
                return self._file(WEB / "narration.html", "text/html; charset=utf-8")
            if p == "/narration.js":
                return self._file(WEB / "narration.js", "application/javascript; charset=utf-8")
            if p == "/narration.css":
                return self._file(WEB / "narration.css", "text/css; charset=utf-8")
            if p in ("/review", "/review.html"):
                return self._file(WEB / "review.html", "text/html; charset=utf-8")
            if p == "/review.css":
                return self._file(WEB / "review.css", "text/css; charset=utf-8")
            if p == "/review.js":
                return self._file(WEB / "review.js", "application/javascript; charset=utf-8")

            if p == "/api/health":
                return self.json({"ok": True, "project": ST.project_dir,
                                  "loaded": ST.proj is not None})
            if p == "/api/state":
                return self.json(ST.snapshot())
            if p == "/api/frame":
                fp = frame_path(float(q.get("t", 0)))
                return self._send(200, fp.read_bytes(), "image/jpeg", cache=True)
            if p == "/api/shots":
                return self.json(self.api_shots(q))
            if p == "/api/ab":
                return self.json(self.api_ab(q))
            if p == "/api/abclip":
                cp = clip_path(float(q.get("t", 0)), q.get("tag", "x"))
                return self._send(200, cp.read_bytes(), "video/mp4", cache=True)
            if p == "/api/sentences":
                return self.json(self.api_sentences(q))
            if p == "/api/sentence":
                return self.json(self.api_sentence(q))
            if p == "/api/review/rows":
                return self.json(RV.rows(ST.proj, ST.deltas, ST.sent_anchors,
                                         ST.confirmed_sents,
                                         beat=q.get("beat"), only=q.get("only", "all"),
                                         q=q.get("q", ""),
                                         limit=int(q.get("limit", 0) or 0),
                                         offset=int(q.get("offset", 0) or 0)))
            if p == "/api/review/row":
                sid = int(q.get("sid", ST.selected_sent if ST.selected_sent is not None else 0))
                return self.json({"ok": True, "data": RV.row(
                    ST.proj, sid, ST.deltas, ST.sent_anchors, ST.confirmed_sents)})
            if p == "/api/review/candidates":
                return self.json(RV.candidates(
                    ST.proj, int(q.get("sid", 0)), ST.deltas, ST.sent_anchors,
                    base_first=int(q["base"]) if q.get("base") else None))
            if p == "/api/dialogue":
                return self.json(self.api_dialogue(q))
            if p == "/api/dialogue/range":
                return self.json(self.api_dialogue_range(q))
            if p == "/api/preview/file":
                f = PR.file_for(q.get("k", ""))
                return self._send(200, f.read_bytes(), "video/mp4", cache=True)
            if p == "/api/preview/poster":
                f = PR.poster_for(q.get("k", ""))
                return self._send(200, f.read_bytes(), "image/jpeg", cache=True)
            if p == "/api/preview/list":
                return self.json({"ok": True, "items": PR.list_previews()})
            if p == "/api/rebuild":
                return self.json({"ok": True, "commands": rebuild_commands()})
            if p == "/api/narr/state":
                return self.json({"ok": True, "data": self.narr_state()})
            if p == "/api/narr/templates":
                return self.json({"ok": True, "templates": NR.templates()})
            if p == "/api/narr/models":
                return self.json({"ok": True, "models": NR.llm_models()})
            if p == "/api/narr/calibration":
                return self.json({"ok": True, "calibration": NR.calibration()})
            if p == "/api/narr/snapshots":
                return self.json({"ok": True, "snapshots": NR.list_snapshots(ST.proj)})
            if p == "/api/narr/commands":
                return self.json({"ok": True, "commands": NR.rebuild_commands(ST.proj)})
            if p == "/api/narr/script_info":
                return self.json({"ok": True, "info": NR.script_info(ST.proj)})
            if p == "/api/asr/state":
                return self.json({"ok": True, "data": AP.state(ST.proj)})
            if p == "/api/export/state":
                return self.json(JY.state(ST.proj))
            return self.err("未知接口", 404)
        except Exception as e:
            traceback.print_exc()
            return self.err(e, 500)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            d = self.body()
            if p == "/api/load":
                proj = Project(d.get("dir", ""))
                ST.project_dir = str(proj.dir)
                ST.proj = proj
                ST.deltas.clear()
                ST.sent_anchors.clear()
                ST.selected_sent = None
                ST.confirmed_sents.clear()
                ST.undo.clear()
                ST.redo.clear()
                ST.current = 0
                ST.persist()
                FRAME_CACHE.mkdir(parents=True, exist_ok=True)
                return self.json(ST.snapshot())
            if not ST.proj:
                return self.err("工程未载入")
            if p.startswith("/api/narr/"):
                return self.json(self.narr_post(p, d))
            if p == "/api/asr/translate":
                return self.json({"ok": True, "data": AP.translate(
                    ST.proj, model=d.get("model"), batch=int(d.get("batch", 24)),
                    limit=d.get("limit"), reset=bool(d.get("reset", False)))})
            if p == "/api/asr/clear":
                return self.json({"ok": True, "data": AP.clear(ST.proj)})
            if p == "/api/export/build":
                return self.json({"ok": True, "data": JY.build(ST.proj, d.get("mode", "segments"),
                                                                d.get("name", ""))})
            if p == "/api/export/install":
                return self.json({"ok": True, "data": JY.install(d.get("name", ""),
                                                                  bool(d.get("replace", False)))})
            if p == "/api/sent/anchor":
                sid = int(d["sent"])
                first, last = int(d["first"]), int(d["last"])
                anchor = SS.compute_anchor(ST.proj, sid, first, last)
                ST.set_sent_anchor(sid, anchor)
                ST.selected_sent = sid
                return self.json(ST.snapshot())
            if p == "/api/sent/clear":
                ST.set_sent_anchor(int(d["sent"]), None)
                return self.json(ST.snapshot())
            if p == "/api/sent/clear_all":
                for sid in list(ST.sent_anchors):
                    ST.set_sent_anchor(sid, None)
                return self.json(ST.snapshot())
            if p == "/api/sent/select":
                v = d.get("sent")
                ST.selected_sent = None if v in (None, "", -1) else int(v)
                ST.persist()
                return self.json(ST.snapshot())
            if p == "/api/review/confirm":
                sids = d.get("sents")
                if sids is None:
                    sids = [int(d["sent"])]
                value = bool(d.get("value", True))
                for sid in (int(x) for x in sids):
                    if value:
                        ST.confirmed_sents.add(sid)
                    else:
                        ST.confirmed_sents.discard(sid)
                ST.persist()
                return self.json({"ok": True, "data": {
                    "confirmed_sents": sorted(ST.confirmed_sents),
                    "progress": RV.rows(ST.proj, ST.deltas, ST.sent_anchors,
                                        ST.confirmed_sents, limit=0)["progress"],
                }})
            if p == "/api/preview/render":
                return self.json({"ok": True, "data": self.api_preview(d)})

            b = int(d.get("beat", ST.current))
            if p == "/api/shift":
                raw = float(d.get("delta", 0))
                if d.get("snap", True):
                    raw = ST.proj.snap_delta(b, raw)
                ok, msg = ST.proj.bounds_ok(b, raw)
                if not ok:
                    return self.err(msg)
                ST.set_delta(b, raw)
                ST.current = b
                return self.json(ST.snapshot())
            if p == "/api/jump":
                idx = int(d.get("idx"))
                shot = next((s for s in ST.proj.shots if s["idx"] == idx), None)
                if not shot:
                    return self.err("镜头编号不存在")
                i0, _ = ST.proj.beat_win[b]
                raw = (shot["start"] + HEAD_IN) - ST.proj.windows[i0]["src_start"]
                ok, msg = ST.proj.bounds_ok(b, raw)
                if not ok:
                    return self.err(msg)
                ST.set_delta(b, raw)
                ST.current = b
                return self.json(ST.snapshot())
            if p == "/api/reset":
                ST.set_delta(b, 0.0)
                return self.json(ST.snapshot())
            if p == "/api/current":
                ST.current = b
                ST.persist()
                return self.json(ST.snapshot())
            if p == "/api/confirm":
                if d.get("value", True):
                    ST.confirmed.add(b)
                else:
                    ST.confirmed.discard(b)
                ST.persist()
                return self.json(ST.snapshot())
            if p == "/api/undo":
                if not ST.undo:
                    return self.err("没有可撤销的操作")
                op = ST.undo.pop()
                ST.redo.append(op)
                ST.apply_undo_op(op, "from")
                return self.json(ST.snapshot())
            if p == "/api/redo":
                if not ST.redo:
                    return self.err("没有可重做的操作")
                op = ST.redo.pop()
                ST.undo.append(op)
                ST.apply_undo_op(op, "to")
                return self.json(ST.snapshot())
            if p == "/api/save":
                if NR.has_unfinalized_draft(ST.project_dir):
                    return self.err("旁白尚未定稿：请先在「旁白工作台」定稿（或放弃草稿），"
                                    "否则后续重跑会覆盖已做的锚点平移", 409)
                res = ST.save_to_disk()
                return self.json({"ok": True, **res, "state": ST.snapshot()["data"]})
            return self.err("未知接口", 404)
        except Exception as e:
            traceback.print_exc()
            return self.err(e, 500)

    def _file(self, path: Path, ctype: str):
        if not path.exists():
            return self.err(f"缺少 {path.name}", 404)
        self._send(200, path.read_bytes(), ctype)

    # ------------------------------------------------------------ 旁白工作台
    def narr_state(self):
        if not ST.proj:
            raise RuntimeError("工程未载入")
        p = ST.proj
        d = NR.draft(p, create=False)   # 只浏览不落盘，避免无谓锁住锚点页
        calib = NR.calibration()
        return {
            "project": {"name": p.name, "dir": str(p.dir),
                        "out_dur": round(p.out_dur, 2), "beats": len(d["beats"])},
            "draft": d,
            "templates": NR.templates(),
            "llm": NR.llm_config(),
            "calibration": calib,
            "budget": NR.char_budget(p, d["beats"], calib),
            "check": NR.check(d["beats"], p, calib),
            "state": NR.state(p),
            "snapshots": NR.list_snapshots(p),
            "locked": NR.has_unfinalized_draft(ST.project_dir),
            "asr": AP.state(p),
            "script_info": NR.script_info(p),
        }

    def narr_post(self, p, d):
        if not ST.proj:
            raise RuntimeError("工程未载入")
        pj = ST.proj
        raw_beat = d.get("beat")
        beat = None if raw_beat in (None, "", "all") else int(raw_beat)

        if p == "/api/narr/render":
            tpl = NR.get_template(d["template_id"])
            v = NR.build_vars(pj, tpl["id"], beat, d.get("vars_on"), d.get("user_note", ""))
            r = NR.render(tpl, v)
            return {"ok": True, "rendered": r,
                    "meta": {"budget": v["_budget"], "beat": beat,
                             "label": v["_blabel"], "template": tpl["name"],
                             "user_chars": len(r["user"]), "system_chars": len(r["system"])}}

        if p == "/api/narr/llm":
            tpl = NR.get_template(d["template_id"])
            v = NR.build_vars(pj, tpl["id"], beat, d.get("vars_on"), d.get("user_note", ""))
            r = NR.render(tpl, v)
            out = NR.llm_chat(r["system"], r["user"], d.get("model"), d.get("temperature"))
            return {"ok": True, **out, "rendered": r, "prompt_chars": len(r["user"])}

        if p == "/api/narr/parse":
            return {"ok": True, **NR.parse_reply(d.get("text", ""),
                                                 NR.draft(pj, create=False)["beats"], beat)}

        if p == "/api/narr/apply":
            cur = NR.draft(pj)["beats"]
            for x in d.get("items", []):
                i = int(x["id"])
                if 0 <= i < len(cur) and str(x.get("text", "")).strip():
                    cur[i]["text"] = str(x["text"]).strip()
                    cur[i]["source"] = d.get("source", "AI")
                    cur[i]["edited"] = True
            NR.save_draft(pj, cur, d.get("note", ""))
            return {"ok": True, "data": self.narr_state()}

        if p == "/api/narr/draft":
            NR.save_draft(pj, d.get("beats", []), d.get("note", ""))
            return {"ok": True, "data": self.narr_state()}

        if p == "/api/narr/snapshot":
            return {"ok": True, "snapshot": NR.make_snapshot(pj, d.get("label", "手动快照")),
                    "data": self.narr_state()}

        if p == "/api/narr/restore":
            NR.restore_snapshot(pj, d["name"])
            return {"ok": True, "data": self.narr_state()}

        if p == "/api/narr/finalize":
            NR.make_snapshot(pj, "定稿前")
            NR.set_finalized(pj, bool(d.get("value", True)))
            return {"ok": True, "data": self.narr_state()}

        if p == "/api/narr/discard":
            res = NR.discard(pj)
            return {"ok": True, **res}

        if p == "/api/narr/apply_script":
            return {"ok": True, "data": NR.apply_to_script(pj)}
        if p == "/api/narr/script_info":
            return {"ok": True, "info": NR.script_info(pj)}

        if p == "/api/narr/template":
            return {"ok": True, "templates": NR.save_template(d.get("template", {}))}

        if p == "/api/narr/config":
            return {"ok": True, "llm": NR.save_llm_config(d)}

        if p == "/api/narr/recalibrate":
            return {"ok": True, "calibration": NR.recalibrate(pj),
                    "data": self.narr_state()}

        raise KeyError("未知旁白接口")

    def api_shots(self, q):
        if not ST.proj:
            raise RuntimeError("工程未载入")
        b = int(q.get("beat", ST.current))
        delta = ST.deltas.get(b, 0.0)
        info = ST.proj.beat_info(b, delta)
        # 句级编辑弹窗可指定中心镜头，做前后翻页
        center = float(q["center"]) if "center" in q else info["src_shifted"][0]
        starts = ST.proj.shot_starts
        i = bisect.bisect_left(starts, center)
        lo, hi = max(0, i - 4), min(len(starts), i + 12)
        paras, zh_ready = SS.dialogue_load(ST.proj)
        items = []
        for s in ST.proj.shots[lo:hi]:
            mid = (s["start"] + s["end"]) / 2
            l = SS.dialogue_at(paras, mid)["at"]
            items.append({
                "idx": s["idx"], "start": round(s["start"], 2),
                "end": round(s["end"], 2), "dur": round(s["dur"], 2),
                "ost": s["ost"], "mid": round(mid, 2),
                "thumb": f"/api/frame?t={mid:.2f}",
                "active": abs(s["start"] + HEAD_IN - center) < 0.35,
                "line": l["text"] if l else "",
                "line_orig": l["orig"] if l else "",
                "line_t": [l["t0"], l["t1"]] if l else None,
            })
        return {"ok": True, "shots": items, "beat": b,
                "center": round(center, 2), "zh_ready": zh_ready}

    def api_ab(self, q):
        if not ST.proj:
            raise RuntimeError("工程未载入")
        b = int(q.get("beat", ST.current))
        delta = ST.deltas.get(b, 0.0)
        merged = SS.merge_sources(ST.proj, ST.deltas, ST.sent_anchors)
        sid = q.get("sid")
        if sid not in (None, ""):
            indices = SS.sent_map(ST.proj)[int(sid)]["wins"]
            seg = [ST.proj.windows[k] for k in indices]
        else:
            i0, i1 = ST.proj.beat_win[b]
            indices = list(range(i0, i1))
            seg = ST.proj.windows[i0:i1]
        picks = ([0, len(seg) // 2, len(seg) - 1] if len(seg) >= 3
                 else list(range(len(seg))))
        paras, zh_ready = SS.dialogue_load(ST.proj)
        pairs = []
        for k in picks:
            w = seg[k]
            t = (w["src_start"] + w["src_end"]) / 2
            wa = merged.get(indices[k], [w["src_start"], w["src_end"]])
            ta = (wa[0] + wa[1]) / 2
            lb = SS.dialogue_at(paras, t)["at"]
            la = SS.dialogue_at(paras, ta)["at"]
            pairs.append({
                "cut": indices[k],
                "before": round(t, 2),
                "after": round(ta, 2),
                "before_url": f"/api/frame?t={t:.2f}",
                "after_url": f"/api/frame?t={ta:.2f}",
                "before_line": lb["text"] if lb else "",
                "after_line": la["text"] if la else "",
            })
        first = merged.get(indices[0], [seg[0]["src_start"], seg[0]["src_end"]])
        return {"ok": True, "pairs": pairs, "delta": round(delta, 3),
                "zh_ready": zh_ready, "sent_count": len(ST.sent_anchors),
                "sid": int(sid) if sid not in (None, "") else None,
                "clip_before": round(seg[0]["src_start"], 2),
                "clip_after": round(first[0], 2)}

    # -------------------------------------------------- 句画联动 / 句级锚点
    def api_sentences(self, q):
        if not ST.proj:
            raise RuntimeError("工程未载入")
        b = int(q.get("beat", ST.current))
        smap = SS.sent_map(ST.proj)
        paras, zh_ready = SS.dialogue_load(ST.proj)
        merged = SS.merge_sources(ST.proj, ST.deltas, ST.sent_anchors)
        items = []
        for sid in sorted(smap):
            s = smap[sid]
            if s["beat"] != b:
                continue
            wi = s["wins"][0]
            a, z = merged.get(wi, [ST.proj.windows[wi]["src_start"],
                                   ST.proj.windows[wi]["src_end"]])
            l = SS.dialogue_at(paras, (a + z) / 2)["at"]
            items.append({
                "id": sid, "text": s["text"], "t0": s["t0"], "dur": s["dur"],
                "wins": s["wins"], "cuts": len(s["wins"]),
                "src": [round(a, 2), round(z, 2)],
                "edited": sid in ST.sent_anchors,
                "line": l["text"] if l else "",
            })
        return {"ok": True, "beat": b, "sentences": items,
                "zh_ready": zh_ready, "selected": ST.selected_sent,
                "sent_count": len(ST.sent_anchors)}

    def api_sentence(self, q):
        if not ST.proj:
            raise RuntimeError("工程未载入")
        sid = int(q.get("sid", ST.selected_sent if ST.selected_sent is not None else 0))
        v = SS.sent_view(ST.proj, sid, ST.sent_anchors, ST.deltas)
        v["conflicts"] = SS.conflict_report(ST.proj, ST.sent_anchors)
        v["selected"] = ST.selected_sent
        return {"ok": True, "data": v}

    def api_dialogue(self, q):
        if not ST.proj:
            raise RuntimeError("工程未载入")
        t = float(q.get("t", 0))
        paras, zh_ready = SS.dialogue_load(ST.proj)
        r = SS.dialogue_at(paras, t, before=int(q.get("before", 3)),
                           after=int(q.get("after", 5)))
        return {"ok": True, "t": t, "zh_ready": zh_ready, **r}

    def api_dialogue_range(self, q):
        if not ST.proj:
            raise RuntimeError("工程未载入")
        a, z = float(q.get("a", 0)), float(q.get("z", 0))
        paras, zh_ready = SS.dialogue_load(ST.proj)
        items = SS.dialogue_range(paras, a, z, int(q.get("limit", 200)))
        return {"ok": True, "a": a, "z": z, "items": items,
                "count": len(items), "zh_ready": zh_ready}

    def api_preview(self, d):
        """按当前会话状态（未写回也生效）临时渲染这一块 / 这一句。"""
        p = ST.proj
        scope = d.get("scope", "beat")
        b = int(d.get("beat", ST.current))
        merged = SS.merge_sources(p, ST.deltas, ST.sent_anchors)
        if scope == "sentence":
            sid = int(d.get("sent", ST.selected_sent if ST.selected_sent is not None else 0))
            wins = SS.sent_map(p)[sid]["wins"]
            label = f"句 {sid}"
        else:
            i0, i1 = p.beat_win[b]
            wins = list(range(i0, i1))
            label = f"块 {b}"
        specs = []
        for k in wins:
            w = p.windows[k]
            a, z = merged.get(k, [float(w["src_start"]), float(w["src_end"])])
            specs.append({"a": a, "z": z, "dur": float(w["dur"])})
        new_a = float(p.windows[wins[0]]["new_start"])
        new_z = float(p.windows[wins[-1]]["new_end"])
        r = PR.render(p, specs, new_a, new_z,
                      subs=bool(d.get("subs", True)),
                      audio=bool(d.get("audio", True)),
                      scope=label, force=bool(d.get("force", False)),
                      quality=str(d.get("quality", "high")))
        r["label"] = label
        r["beat"] = b
        r["dirty_cuts"] = len(merged)
        r["final"] = PR.final_info(p)
        return r


def main():
    if ST.proj is None:
        print(f"[警告] 默认工程载入失败：{getattr(ST, 'error', '')}")
        print("       启动后在界面里点「载入工程」指定目录即可。")
    else:
        p = ST.proj
        print(f"工程  {p.name}")
        print(f"      {p.dir}")
        print(f"      {len(p.windows)} 刀 / {len(p.beat_ids())} 块 / {len(p.shots)} 镜头 / 源片 {p.src_dur:.1f}s")
        print(f"抽帧  {p.video}")
    print(f"\n锚点校对台  →  http://127.0.0.1:{PORT}\n")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
