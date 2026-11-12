# -*- coding: utf-8 -*-
# [L0 基础层] 职责：工程冗余盘点 + 可恢复回收（旧版成片 / 备份 / 过期帧 / 可再生中间物）
# 调用者：cli（clean）｜ 不参与生成链，只读扫描 + 移入 .trash
"""工程冗余清理。

**为什么需要**：一集动漫反复调 10 版，``out/`` 会堆 7 个 ``-v3x.mp4``（每版 90MB+），
``json/`` 几十个 ``.bak_*``，``.qcframes/`` 留上一版质检帧，``verify/rapid_seg/`` 留
逐句切片。它们不影响正确性，却让「哪份是最新的」变模糊 —— 历史事故就是误用了旧版本。
``workspace.scan_stale`` 只报「可能污染」的告警，本模块负责**真的收走**。

**安全边界**（比功能更重要）::

* 默认只扫描打印（dry-run），``--apply`` 才动文件；
* 收走 = 移到 ``<工程>/.trash/<批次>/`` 并写 ``manifest.json``，``clean --restore`` 可原样放回；
* 真正释放空间要再执行 ``clean --empty``（二次确认，只清 ``.trash``）；
* 只按**白名单规则**匹配（下表），绝不递归删除；``project.json``、``json/`` 当前产物、
  ``tts/`` 当前音频、``runs/`` 归档**永不在**候选集内；
* ``--deep`` 才纳入「可再生成」的大件（片源切片 / 底座音频 / 混音 wav / 快速验证切片）。

**回收类别**::

    old_final    out/03-*.mp4 中不是当前成片、也不是最新归档内容的旧版
    dup_final    out/03-*.mp4 中与最新归档同内容的冗余副本
    backup       *.bak* / *.v3bak / *.v31bak 等备份文件
    backup_dir   tts_v31_backup/ 一类备份目录（整目录）
    qc_frame     .qcframes/qc_*.jpg 中早于当前成片的
    verify_wav   verify/chk_*.wav 中早于当前成片的
    deep_seg     verify/rapid_seg/ 逐句快速验证切片（--deep）
    deep_mid     out/01-*.mp4、out/02-*.mp4、out/mix-*.wav、audio/base-*.wav（--deep）

用法::

    python -m aicutplan clean --project <pj>            # 预览
    python -m aicutplan clean --project <pj> --apply    # 移入 .trash（可恢复）
    python -m aicutplan clean --project <pj> --empty    # 清空 .trash（真正释放）
    python -m aicutplan clean --project <pj> --restore  # 放回最近一批
"""
from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from .io import dump_json, load_json
from .paths import ProjectPaths
from .workspace import LATEST_FILE, RUNS_DIR, quick_sig

#: 回收站目录名（工程根下）
TRASH_DIR = ".trash"

#: 类别 → 中文说明（同时决定打印顺序）
CATEGORIES: dict[str, str] = {
    "old_final": "旧版成片",
    "dup_final": "成片冗余副本",
    "backup": "备份文件",
    "backup_dir": "备份目录",
    "qc_frame": "过期质检帧",
    "verify_wav": "旧校验音频",
    "deep_seg": "快速验证切片（可重抽）",
    "deep_mid": "可再生中间产物",
}

#: 只读、永不进入候选集的目录（相对工程根）
PROTECTED = ("runs", TRASH_DIR, "tts", "json")

#: ``--deep`` 才纳入的类别
DEEP_CATEGORIES = frozenset({"deep_seg", "deep_mid"})

#: 备份文件：``a.json.bak_v35`` / ``a.json.v31bak`` / ``a.json.bak``
_BAK_RE = re.compile(r"\.(?:bak(?:[._-].*)?|v\d+bak)$", re.IGNORECASE)
#: 备份目录：``tts_v31_backup`` / ``out-backup``
_BAK_DIR_RE = re.compile(r"(?:[_-]backup|^backup)$", re.IGNORECASE)
#: 旧版成片：out/03- 开头（当前成片由 base name 单独排除）
_FINAL_RE = re.compile(r"^03-.*\.mp4$", re.IGNORECASE)


# ---------------------------------------------------------------- 扫描

def _size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


def _tree_size(d: Path) -> int:
    return sum(_size(f) for f in d.rglob("*") if f.is_file())


def _item(root: Path, p: Path, category: str, reason: str) -> dict:
    return {
        "rel": p.relative_to(root).as_posix(),
        "category": category,
        "bytes": _tree_size(p) if p.is_dir() else _size(p),
        "reason": reason,
    }


def _trash_batches(root: Path) -> list[Path]:
    d = root / TRASH_DIR
    if not d.exists():
        return []
    return sorted((x for x in d.iterdir() if x.is_dir()), key=lambda x: x.name)


def scan(project: str | Path, *, deep: bool = False) -> list[dict]:
    """盘点可回收的冗余，返回 ``[{rel, category, bytes, reason}]``（不落盘）。

    判据全部是「相对当前成片 / 最新归档」的：没有成片时，与版本相关的类别
    （qc_frame / verify_wav）一律不报，避免把未知状态当冗余删掉。
    """
    root = ProjectPaths(project).root
    items: list[dict] = []

    final = root / "out" / "03-rapid-narration.mp4"
    final_ts = final.stat().st_mtime if final.exists() else 0.0
    latest = load_json(root / RUNS_DIR / LATEST_FILE, default={}) or {}
    latest_sig = latest.get("final_sig") or ""

    # 1) out/ 下的成片版本
    out = root / "out"
    if out.exists():
        for p in sorted(out.glob("*.mp4")):
            if p.name == final.name or not p.is_file():
                continue
            if _FINAL_RE.match(p.name):
                sig = quick_sig(p) if p.stat().st_size else ""
                if latest_sig and sig == latest_sig:
                    items.append(_item(root, p, "dup_final",
                                       "与最新归档同内容（runs 内已有不可变快照）"))
                else:
                    items.append(_item(root, p, "old_final",
                                       "带版本号的旧版成片，已被当前 03-rapid-narration.mp4 取代"))
            elif deep and re.match(r"^0[12]-.*\.mp4$", p.name, re.IGNORECASE):
                items.append(_item(root, p, "deep_mid",
                                   "洗片/粗剪中间片，可由源片重新生成"))
        if deep:
            for p in sorted(out.glob("mix-*.wav")):
                items.append(_item(root, p, "deep_mid", "混音中间 wav，重跑 r6_mix 即可再生"))

    # 2) 备份文件（工程根 + json/，不递归进 runs/.trash）
    for base in (root, root / "json"):
        if not base.exists():
            continue
        for p in sorted(base.iterdir()):
            if p.is_file() and _BAK_RE.search(p.name):
                items.append(_item(root, p, "backup", "历史备份，当前产物已取代"))

    # 3) 备份目录
    for p in sorted(root.iterdir()):
        if p.is_dir() and p.name not in PROTECTED and _BAK_DIR_RE.search(p.name):
            items.append(_item(root, p, "backup_dir", "整目录备份，可由 tts/ 重新合成"))

    # 4) 过期质检帧（早于当前成片 1s 以上）
    qc = root / ".qcframes"
    if final_ts and qc.exists():
        for p in sorted(qc.rglob("qc_*.jpg")):
            try:
                if p.stat().st_mtime < final_ts - 1:
                    items.append(_item(root, p, "qc_frame", "早于当前成片的抽帧，qc --plan 会重抽"))
            except OSError:
                continue

    # 5) 旧校验音频
    ver = root / "verify"
    if final_ts and ver.exists():
        for p in sorted(ver.glob("chk_*.wav")):
            try:
                if p.stat().st_mtime < final_ts - 1:
                    items.append(_item(root, p, "verify_wav", "旧版试听/校验音频"))
            except OSError:
                continue

    # 6) --deep：逐句快速验证切片
    if deep:
        seg = ver / "rapid_seg"
        if seg.exists():
            for p in sorted(seg.iterdir()):
                if p.name.startswith(".") or p.is_dir():
                    continue
                items.append(_item(root, p, "deep_seg", "逐句验证切片，qc --plan 前可随时重抽"))
        audio = root / "audio"
        if audio.exists():
            for p in sorted(audio.glob("base-*.wav")):
                items.append(_item(root, p, "deep_mid", "从 01 中间片抽出的底座音频，可重抽"))

    items.sort(key=lambda x: (list(CATEGORIES).index(x["category"]), -x["bytes"], x["rel"]))
    return items


def summarize(items: list[dict]) -> dict:
    """按类别汇总：``{category: {count, bytes}}`` + 总计。"""
    per: dict[str, dict] = {}
    for it in items:
        row = per.setdefault(it["category"], {"count": 0, "bytes": 0})
        row["count"] += 1
        row["bytes"] += it["bytes"]
    return {
        "per": per,
        "count": len(items),
        "bytes": sum(it["bytes"] for it in items),
    }


def trash_stats(project: str | Path) -> dict:
    """回收站现状：批次数 / 占用字节 / 最近批次。"""
    root = ProjectPaths(project).root
    batches = _trash_batches(root)
    return {
        "batches": len(batches),
        "bytes": sum(_tree_size(b) for b in batches),
        "latest": batches[-1].name if batches else "",
    }


# ---------------------------------------------------------------- 回收 / 恢复

def _new_batch(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = root / TRASH_DIR / stamp
    n = 2
    while d.exists():
        d = root / TRASH_DIR / f"{stamp}-{n}"
        n += 1
    d.mkdir(parents=True)
    return d


def _move(src: Path, dst: Path) -> None:
    """同盘 rename（秒级）；跨盘退化为复制后删除源。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        if src.is_dir():
            shutil.move(str(src), str(dst))
        else:
            shutil.copy2(src, dst)
            src.unlink()


def clean(project: str | Path, *, apply: bool = False, deep: bool = False,
          categories: list[str] | None = None) -> dict:
    """清理冗余。``apply=False`` 只扫描。

    返回 ``{items, summary, batch, moved, bytes, applied}``。
    移动失败的单条不会中断整批，会记在 ``failed`` 里。
    """
    root = ProjectPaths(project).root
    items = scan(root, deep=deep)
    if categories:
        wanted = {c for c in categories if c}
        items = [it for it in items if it["category"] in wanted]
    report = {"items": items, "summary": summarize(items), "applied": False,
              "batch": "", "moved": 0, "bytes": 0, "failed": []}
    if not apply or not items:
        return report

    batch = _new_batch(root)
    manifest = {
        "batch": batch.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "created_ts": time.time(),
        "project": str(root),
        "count": 0,
        "bytes": 0,
        "items": [],
    }
    for it in items:
        src = root / it["rel"]
        dst = batch / it["rel"]
        try:
            _move(src, dst)
        except OSError as exc:
            report["failed"].append(f"{it['rel']}: {exc}")
            continue
        manifest["items"].append({**it, "restore_to": it["rel"]})
        manifest["count"] += 1
        manifest["bytes"] += it["bytes"]
        report["moved"] += 1
        report["bytes"] += it["bytes"]
    dump_json(batch / "manifest.json", manifest)
    report["applied"] = True
    report["batch"] = batch.name
    return report


def restore(project: str | Path, *, batch: str = "") -> dict:
    """把某一批（默认最近一批）从 ``.trash`` 放回原路径。

    目标已存在时不覆盖（记 ``conflicts``），避免把当前产物顶掉。
    """
    root = ProjectPaths(project).root
    batches = _trash_batches(root)
    if not batches:
        return {"batch": "", "restored": 0, "conflicts": [], "missing": []}
    target = batches[-1] if not batch else root / TRASH_DIR / batch
    mf = load_json(target / "manifest.json", default={}) or {}
    out = {"batch": target.name, "restored": 0, "conflicts": [], "missing": []}
    remaining: list[dict] = []
    for it in mf.get("items") or []:
        rel = it.get("restore_to") or it.get("rel")
        src, dst = target / rel, root / rel
        if not src.exists():
            out["missing"].append(rel)
            remaining.append(it)
            continue
        if dst.exists():
            out["conflicts"].append(rel)
            remaining.append(it)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        _move(src, dst)
        out["restored"] += 1
    # 清单同步收缩：全部放回则删批次，部分冲突则只留未放回的
    if remaining:
        mf["items"] = remaining
        mf["count"] = len(remaining)
        mf["bytes"] = sum(_size(target / (x.get("restore_to") or x.get("rel")))
                          for x in remaining)
        dump_json(target / "manifest.json", mf)
    else:
        try:
            (target / "manifest.json").unlink()
        except OSError:
            pass
    # 批次清空后顺手删掉空目录
    try:
        for d in sorted((x for x in target.rglob("*") if x.is_dir()),
                        key=lambda x: len(x.parts), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
        if target.exists() and not any(target.iterdir()):
            target.rmdir()
        td = root / TRASH_DIR
        if td.exists() and not any(td.iterdir()):
            td.rmdir()
    except OSError:
        pass
    return out


def _rm_tree(d: Path) -> int:
    """逐文件删除目录树（不用 rmtree，便于统计与中断可读）。返回释放字节。"""
    freed = 0
    for p in sorted(d.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if p.is_file() or p.is_symlink():
                freed += _size(p)
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        except OSError:
            continue
    try:
        d.rmdir()
    except OSError:
        pass
    return freed


def empty(project: str | Path) -> dict:
    """清空回收站（真正释放磁盘）。只动 ``<工程>/.trash``。"""
    root = ProjectPaths(project).root
    batches = _trash_batches(root)
    freed = 0
    for b in batches:
        freed += _rm_tree(b)
    td = root / TRASH_DIR
    try:
        if td.exists() and not any(td.iterdir()):
            td.rmdir()
    except OSError:
        pass
    return {"batches": len(batches), "bytes": freed}
