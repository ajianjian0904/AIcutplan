# [L0 基础层] 职责：作品库布局（动漫/集）+ 生成版本快照与最新指针 ｜ 调用者：cli / cf_pipeline / qc_loop
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""作品库与生成版本管理。

**库结构**::

    <library>/<动漫名>/
        series.json                  # 元信息（名称/别名/备注/创建时间）
        EP01/                        # 一集 = 一个工程（project.json 所在目录）
            project.json
            json/ out/ tts/ ...
            runs/
                r001_20261112-2340_v35/
                    manifest.json    # 输入指纹 + 产物指纹 + 来源工程
                    final.mp4        # 该次成片（默认硬链接，零拷贝）
                    narration.json cutplan.json align.json tts-result.json ...
                latest.json          # 最新一次归档的指针

**为什么要 runs/**：同一集反复调整时，``out/03-rapid-narration.mp4`` 每次被覆盖，
历史版本只能靠手工 ``cp -v3x``，既占空间又容易「引用了旧片子」。这里把每次生成
归档成不可变快照：

* 同盘用 ``os.link`` 硬链接 —— 后续 ffmpeg 覆盖 ``out/`` 会写入新 inode，
  快照天然不受影响；跨盘/不支持时退化为复制；
* ``manifest.json`` 记录每个产物的 sha1 短指纹（首尾 4MB + 大小），
  ``latest.json`` 记录「最新一次」及其成片指纹；
* ``latest()`` 会校验指纹，**被改动或缺失时直接报错**，绝不静默回退到旧产物；
* ``scan_stale()`` 在每次生成前扫描「比最新归档更新的未归档改动」「早于旁白表的旧
  TTS」「早于成片的旧质检帧」，把历史踩过的污染源显式暴露出来。

库根解析：``AICUTPLAN_LIBRARY`` > ``config/library.json`` 的 ``root`` >
``<仓库同级>/漫剪作品库``。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config as CFG
from .io import dump_json, load_json
from .paths import repo_root

#: 环境变量：覆盖作品库根目录
ENV_LIBRARY = "AICUTPLAN_LIBRARY"
SERIES_META = "series.json"
RUNS_DIR = "runs"
LATEST_FILE = "latest.json"
PROJECT_FILE = "project.json"

#: (工程内相对路径, 快照内文件名)
ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("out/03-rapid-narration.mp4", "final.mp4"),
    ("json/narration.json", "narration.json"),
    ("json/cutplan.json", "cutplan.json"),
    ("json/align.json", "align.json"),
    ("json/tts-result.json", "tts-result.json"),
    ("json/qc_visual.json", "qc_visual.json"),
    ("json/qc_report.json", "qc_report.json"),
    ("window_table.md", "window_table.md"),
)

#: 追加归档（整集回滚所需）：按通配符收，保留工程内相对路径。
#: tts/*.mp3 是重混的唯一音频源；证据 JSON 决定重跑剧情/质检时的输入。
ARTIFACT_GLOBS: tuple[str, ...] = (
    "tts/*.mp3",
    "json/shots_desc.json",
    "json/scene_desc.json",
    "json/ja-segments.json",
)

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# ---------------------------------------------------------------- 库与作品

def library_root(override: str | Path | None = None) -> Path:
    """作品库根：参数 > AICUTPLAN_LIBRARY > config/library.json > 仓库同级/漫剪作品库。"""
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get(ENV_LIBRARY)
    if env:
        return Path(env).expanduser().resolve()
    root = (CFG.load_config("library.json", {}) or {}).get("root")
    if root:
        return Path(root).expanduser().resolve()
    return (repo_root().parent / "漫剪作品库").resolve()


def safe_name(name: str) -> str:
    """文件夹名清洗：保留中英文数字，Windows 非法字符换成 '-'，去尾部空格/点。"""
    s = _BAD_CHARS.sub("-", str(name)).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return s or "untitled"


def init_series(name: str, library: str | Path | None = None, *,
                aliases: list[str] | None = None, note: str = "") -> Path:
    """创建（或复用）一个动漫文件夹并写 series.json。返回系列目录。"""
    d = library_root(library) / safe_name(name)
    d.mkdir(parents=True, exist_ok=True)
    meta = d / SERIES_META
    if not meta.exists():
        dump_json(meta, {
            "name": name,
            "aliases": list(aliases or []),
            "note": note,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
    return d


def list_series(library: str | Path | None = None) -> list[dict]:
    """列出库内所有动漫（含集列表与每集最新归档）。"""
    root = library_root(library)
    out: list[dict] = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        meta_f = d / SERIES_META
        if not d.is_dir() or not meta_f.exists():
            continue
        meta = load_json(meta_f, default={}) or {}
        eps = []
        for ep in sorted(d.iterdir()):
            if ep.is_dir() and (ep / PROJECT_FILE).exists():
                st = episode_status(ep / PROJECT_FILE)
                eps.append({"episode": ep.name, **st})
        out.append({"series": meta.get("name") or d.name, "dir": str(d),
                    "aliases": meta.get("aliases") or [], "episodes": eps})
    return out


def locate(project: str | Path) -> dict | None:
    """判断一个工程是否位于作品库内；返回 series/episode 信息，否则 None。"""
    root = project_dir(project)
    parent = root.parent
    if (parent / SERIES_META).exists():
        meta = load_json(parent / SERIES_META, default={}) or {}
        return {"series": meta.get("name") or parent.name, "series_dir": str(parent),
                "episode": root.name, "episode_dir": str(root)}
    return None


def project_dir(project: str | Path) -> Path:
    p = Path(project).expanduser().resolve()
    return p if p.is_dir() else p.parent


# ---------------------------------------------------------------- 指纹与归档

def quick_sig(p: str | Path, chunk: int = 4 << 20) -> str:
    """快速内容指纹：大小 + 首尾各 4MB 的 sha1（大视频秒级，足够发现替换/截断）。"""
    f = Path(p)
    st = f.stat()
    h = hashlib.sha1(str(st.st_size).encode())
    with f.open("rb") as fh:
        h.update(fh.read(chunk))
        if st.st_size > chunk:
            fh.seek(max(0, st.st_size - chunk))
            h.update(fh.read(chunk))
    return h.hexdigest()[:16]


def _link_or_copy(src: Path, dst: Path) -> str:
    """优先硬链接（零拷贝、快照不可变），失败退化为复制。返回 'link' / 'copy'。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def _next_run_id(runs_dir: Path, label: str = "") -> str:
    seq = 0
    if runs_dir.exists():
        for d in runs_dir.iterdir():
            m = re.match(r"r(\d{3,})_", d.name)
            if m:
                seq = max(seq, int(m.group(1)))
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    lab = safe_name(label).replace(" ", "-")[:16] if label else ""
    return f"r{seq + 1:03d}_{ts}" + (f"_{lab}" if lab else "")


def qc_status(root: Path) -> dict:
    """会话内质检状态（只读）：``verified`` / ``partial`` / ``unverified``。

    ``json/qc_visual.json`` 由 ``qc --merge`` 产出，每行带 ``judged`` 标记。
    注意：没有质检判定不等于成片有问题，只是「未被逐格验证」，因此默认
    只记录不阻断；prod 可用 ``require_qc=True`` 强制。
    """
    p = root / "json" / "qc_visual.json"
    if not p.exists():
        return {"state": "unverified", "judged": 0, "total": 0,
                "mismatch": 0, "reason": "缺 json/qc_visual.json（未跑 qc --merge）"}
    rows = load_json(p, default=[]) or []
    judged = sum(1 for r in rows if isinstance(r, dict) and r.get("judged"))
    total = len(rows)
    state = ("verified" if total and judged == total else
             "partial" if judged else "unverified")
    return {"state": state, "judged": judged, "total": total,
            "mismatch": sum(1 for r in rows if isinstance(r, dict) and r.get("match") == 0),
            "reason": "" if state == "verified" else f"{judged}/{total} 句已判定"}


def snapshot(project: str | Path, *, label: str = "", sentences: str | Path | None = None,
             note: str = "", require_qc: bool = False) -> dict:
    """把当前工程的成片与关键 JSON 归档为一次不可变 run，并更新 latest。

    调用前应确保已跑完 r7（成片存在）。返回 manifest 字典。
    ``require_qc=True`` 时，未跑完会话内逐格质检会直接抛错（不产生空 run）。
    """
    root = project_dir(project)
    final_src = root / "out" / "03-rapid-narration.mp4"
    if not final_src.exists():
        raise FileNotFoundError(f"{final_src} 不存在：先跑完渲染（r7_mux）再归档")

    qc = qc_status(root)
    if require_qc and qc["state"] != "verified":
        raise RuntimeError(
            f"质检未完成（{qc['state']}：{qc['reason']}）："
            "先跑 qc --plan → 会话内逐格判定 → qc --merge，或去掉 --require-qc")

    runs_dir = root / RUNS_DIR
    run_id = _next_run_id(runs_dir, label)
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    artifacts: dict[str, dict] = {}
    for rel, dst_name in ARTIFACTS:
        src = root / rel
        if not src.exists():
            continue
        dst = run_dir / dst_name
        mode = _link_or_copy(src, dst)
        st = src.stat()
        artifacts[dst_name] = {
            "src": rel, "path": dst_name, "bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "sig": quick_sig(src), "mode": mode,
        }

    # 追加：配音与证据文件（保留工程内相对路径，便于整集回滚）
    for pattern in ARTIFACT_GLOBS:
        for src in sorted(root.glob(pattern)):
            if not src.is_file():
                continue
            rel = src.relative_to(root).as_posix()
            dst = run_dir / rel
            mode = _link_or_copy(src, dst)
            st = src.stat()
            artifacts[rel] = {
                "src": rel, "path": rel, "bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "sig": quick_sig(src), "mode": mode,
            }

    sent_info = None
    if sentences:
        sp = Path(sentences).expanduser()
        if not sp.is_absolute():
            sp = (Path.cwd() / sp).resolve()
        if sp.exists():
            dst = run_dir / "sentences.json"
            _link_or_copy(sp, dst)
            sent_info = {"src": str(sp), "path": "sentences.json",
                         "bytes": sp.stat().st_size, "sig": quick_sig(sp)}

    loc = locate(root)
    cfg = load_json(root / PROJECT_FILE, default={}) or {}
    manifest = {
        "run_id": run_id,
        "label": label,
        "note": note,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "created_ts": time.time(),   # 高精度：同秒内的改动也能判别先后
        "project": str(root),
        "series": (loc or {}).get("series"),
        "episode": (loc or {}).get("episode"),
        "source_video": cfg.get("source_video"),
        "sentences": sent_info,
        "qc": qc,
        "artifacts": artifacts,
    }
    dump_json(run_dir / "manifest.json", manifest)

    final = artifacts.get("final.mp4") or {}
    latest = {
        "run_id": run_id,
        "label": label,
        "created_at": manifest["created_at"],
        "created_ts": manifest["created_ts"],
        "manifest": f"{RUNS_DIR}/{run_id}/manifest.json",
        "final": f"{RUNS_DIR}/{run_id}/final.mp4",
        "final_sig": final.get("sig"),
        "final_bytes": final.get("bytes"),
        "qc": qc.get("state"),
        "project": str(root),
    }
    dump_json(runs_dir / LATEST_FILE, latest)
    return manifest


def latest(project: str | Path, *, verify: bool = True) -> dict:
    """读最新归档指针；默认校验成片存在且指纹一致（防止被旧/坏文件顶替）。"""
    root = project_dir(project)
    p = root / RUNS_DIR / LATEST_FILE
    if not p.exists():
        raise FileNotFoundError(
            f"{root.name} 尚无归档版本（{p} 不存在）；先执行 runs snapshot")
    d = load_json(p, default=None)
    if not d:
        raise RuntimeError(f"{p} 内容损坏；删除它并重新 snapshot")
    if verify:
        final = root / d.get("final", "")
        if not final.exists():
            raise RuntimeError(f"最新归档成片缺失：{final}；请重新 snapshot 或指定历史 run")
        sig = quick_sig(final)
        if d.get("final_sig") and sig != d["final_sig"]:
            raise RuntimeError(
                f"最新归档成片指纹不符（{sig} != {d['final_sig']}）：文件被替换或损坏；"
                f"请重新 snapshot，勿直接引用 runs/{d.get('run_id')}")
    return d


def list_runs(project: str | Path) -> list[dict]:
    root = project_dir(project)
    runs_dir = root / RUNS_DIR
    out: list[dict] = []
    if not runs_dir.exists():
        return out
    cur = None
    try:
        cur = latest(root, verify=False).get("run_id")
    except Exception:
        pass
    for d in sorted([x for x in runs_dir.iterdir() if x.is_dir()]):
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        m = load_json(mf, default={}) or {}
        final = d / "final.mp4"
        out.append({
            "run_id": m.get("run_id") or d.name,
            "label": m.get("label") or "",
            "created_at": m.get("created_at") or "",
            "final": str(final) if final.exists() else "",
            "final_bytes": final.stat().st_size if final.exists() else 0,
            "is_latest": (m.get("run_id") or d.name) == cur,
            "note": m.get("note") or "",
        })
    return out


def verify_run(run_dir: str | Path) -> list[str]:
    """校验一次归档：manifest 内所有产物存在且指纹一致。返回问题清单。"""
    d = Path(run_dir)
    mf = d / "manifest.json"
    if not mf.exists():
        return [f"{d} 缺 manifest.json"]
    m = load_json(mf, default={}) or {}
    issues: list[str] = []
    for name, a in (m.get("artifacts") or {}).items():
        f = d / a.get("path", name)
        if not f.exists():
            issues.append(f"{name} 缺失")
            continue
        if a.get("sig") and quick_sig(f) != a["sig"]:
            issues.append(f"{name} 指纹不符（被改动）")
    return issues


def episode_status(project: str | Path) -> dict:
    """一集的版本概况：最新 run、是否有未归档改动、陈旧音频/质检帧计数。"""
    root = project_dir(project)
    info: dict = {"latest_run": "", "latest_label": "", "latest_at": "",
                  "final_bytes": 0, "stale": [], "runs": 0}
    try:
        d = latest(root, verify=True)
        info.update(latest_run=d.get("run_id", ""), latest_label=d.get("label", ""),
                    latest_at=d.get("created_at", ""), final_bytes=d.get("final_bytes") or 0)
    except Exception as e:
        info["stale"].append(str(e))
    info["runs"] = len(list_runs(root))
    try:
        info["stale"] += scan_stale(root)
    except Exception:
        pass
    # 去重保序
    seen: set[str] = set()
    info["stale"] = [x for x in info["stale"] if not (x in seen or seen.add(x))]
    return info


# ---------------------------------------------------------------- 污染扫描

def scan_stale(project: str | Path) -> list[str]:
    """生成前体检：列出「旧产物 / 未归档改动」等可能造成污染的情况。

    返回人类可读的告警列表（空 = 干净）。这些是历史真实踩过的坑：
    旧 TTS 冒充新句、旧质检帧判新版、旁白表改了却没重新归档。
    """
    root = project_dir(project)
    issues: list[str] = []
    latest_info = None
    try:
        latest_info = latest(root, verify=True)
    except Exception as e:
        issues.append(str(e))
    if latest_info:
        snap_ts = latest_info.get("created_ts")
        if not snap_ts:
            try:
                snap_ts = datetime.fromisoformat(latest_info["created_at"]).timestamp()
            except Exception:
                snap_ts = None
        if snap_ts:
            watched = ["json/narration.json", "json/cutplan.json", "json/align.json",
                       "json/tts-result.json", "out/03-rapid-narration.mp4"]
            newer = [rel for rel in watched
                     if (root / rel).exists() and (root / rel).stat().st_mtime > snap_ts + 0.002]
            if newer:
                issues.append("最近归档后有未归档改动：" + "、".join(newer)
                              + "（确认无误后 runs snapshot 归档）")

    # 1) TTS 旧音频：判据不是「mp3 比旁白表旧」（重写旁白表但文本未变是正常的），
    #    而是「旁白文本与 tts-result 不一致」或「mp3 早于 tts-result 元数据」——
    #    后者正是 039.mp3 那次的真实事故：元数据更新了、音频没重合成。
    narr = root / "json" / "narration.json"
    tts_res = root / "json" / "tts-result.json"
    tts_dir = root / "tts"
    if narr.exists() and tts_res.exists():
        try:
            n_items = {int(x["i"]): str(x.get("text", ""))
                       for x in (load_json(narr, default={}) or {}).get("items", [])}
            t_items = {int(x["i"]): str(x.get("text", ""))
                       for x in (load_json(tts_res, default={}) or {}).get("items", [])}
            diff = [i for i, txt in n_items.items()
                    if i in t_items and t_items[i] != txt]
            if diff:
                issues.append(f"{len(diff)} 句旁白文本与 tts-result 不一致（旧音频风险，"
                              f"如句 {diff[:3]}）")
            # 指纹核对（最可靠）：r5 合成时把每条音频的内容指纹写进 tts-result，
            # 这里比对实际文件 —— 能抓到「元数据更新但音频没重合成」（039.mp3 事故）。
            # 老数据没有 mp3_sig 时跳过，不做 mtime 猜测（elastic_fit 会重写元数据）。
            if tts_dir.exists():
                bad = []
                for it in (load_json(tts_res, default={}) or {}).get("items", []):
                    sig = it.get("mp3_sig")
                    if not sig:
                        continue
                    f = tts_dir / f"{int(it['i']):03d}.mp3"
                    if not f.exists() or quick_sig(f) != sig:
                        bad.append(int(it["i"]))
                if bad:
                    issues.append(f"{len(bad)} 句音频与 tts-result 指纹不符"
                                  f"（未重合成或被替换，如句 {bad[:3]}）")
        except Exception:
            pass

    # 2) 质检帧：早于当前成片的帧可能被复用（历史事故：用 v2 帧判 v3.4）
    final = root / "out" / "03-rapid-narration.mp4"
    qc = root / ".qcframes"
    if final.exists() and qc.exists():
        f_ts = final.stat().st_mtime
        stale_qc = [p for p in qc.rglob("qc_*.jpg") if p.stat().st_mtime < f_ts - 1]
        if stale_qc:
            issues.append(f".qcframes/ 留有 {len(stale_qc)} 张旧质检帧（早于当前成片；"
                          f"qc --plan 会覆盖重抽，勿直接据此判定）")

    # 3) 备份文件堆积只是提示，不算污染
    baks = list(root.glob("json/*.bak*")) + list(root.glob("*.bak*"))
    if len(baks) > 8:
        issues.append(f"工程内有 {len(baks)} 个 .bak 备份（不参与生成，但建议定期清理）")
    return issues


# ---------------------------------------------------------------- 迁移

def _clone_tree(src: Path, dst: Path) -> int:
    """整树克隆：文件用硬链接（同盘零拷贝），目录逐层重建。返回文件数。"""
    n = 0
    for cur, _dirs, files in os.walk(src):
        rel = Path(cur).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            _link_or_copy(Path(cur) / f, dst / rel / f)
            n += 1
    return n


def migrate(project: str | Path, series: str, episode: str = "EP01", *,
            library: str | Path | None = None, move: bool = False,
            dry_run: bool = False) -> dict:
    """把已有工程迁入作品库 ``<library>/<series>/<episode>/``。

    * 默认**复制**（硬链接零拷贝，原工程保留），``move=True`` 时同盘整目录改名；
    * 迁移后重写 ``project.json`` 的 ``workdir``，避免脚本仍指回旧位置；
    * 目标已存在则报错，绝不覆盖已有集。
    """
    src = project_dir(project)
    if not (src / PROJECT_FILE).exists():
        raise FileNotFoundError(f"{src} 不是工程目录（缺 {PROJECT_FILE}）")
    sdir = init_series(series, library)
    dst = sdir / safe_name(episode)
    if dst.exists():
        raise FileExistsError(f"目标已存在：{dst}（换一个 --episode 名或先手动处理）")
    if dst.resolve() == src.resolve():
        raise ValueError("目标与源相同，无需迁移")

    plan = {"src": str(src), "dst": str(dst), "mode": "move" if move else "copy"}
    if dry_run:
        plan["files"] = sum(len(f) for _, _, f in os.walk(src))
        return plan

    if move:
        try:
            os.rename(src, dst)          # 同盘秒级、原子
        except OSError:
            shutil.move(str(src), str(dst))
        mode = "move"
    else:
        _clone_tree(src, dst)
        mode = "copy"

    # 重写 workdir，保证脚本按新位置工作
    pj = dst / PROJECT_FILE
    cfg = load_json(pj, default={}) or {}
    old = cfg.get("workdir")
    cfg["workdir"] = str(dst)
    dump_json(pj, cfg, backup_tag=".bak_migrate")

    print(f"[migrate] {mode}: {src} → {dst}（workdir {old} → {dst}）",
          file=sys.stderr, flush=True)
    return {"src": str(src), "dst": str(dst), "mode": mode,
            "series_dir": str(sdir), "workdir_updated": True}


def summary(project: str | Path) -> dict[str, Any]:
    """一集的一行式摘要（供 lib list / 诊断使用）。"""
    root = project_dir(project)
    loc = locate(root) or {}
    st = episode_status(root)
    return {"series": loc.get("series"), "episode": loc.get("episode") or root.name,
            "dir": str(root), **st}
