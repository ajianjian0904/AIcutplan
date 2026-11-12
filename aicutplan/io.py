# -*- coding: utf-8 -*-
"""L0·JSON / 文本读写：UTF-8、原子写、可备份。

所属层：L0 基础层 ｜ 职责：统一落盘语义 ｜ 调用者：所有阶段脚本

为什么需要原子写：流水线经常中途被打断（safe-delete 钩子、Ctrl+C、磁盘满），
直接 ``write_text`` 会留下半截 JSON，下次读取直接崩。这里先写同目录临时文件
再 ``os.replace``，保证读者要么看到旧文件、要么看到完整新文件。

为什么同目录：Windows 上临时目录常在 C 盘而工程在 E 盘，跨盘符 ``replace``
会抛 OSError（本项目实际踩过）。临时文件必须建在目标文件所在目录。

备份有两种语义，别用错：
* :func:`backup_copy`  —— 复制一份、保留原文件。适合「接下来原地覆盖」的场景
  （subs_extract 写回 shots_desc.json 时，改名移走会让下游读不到）。
* :func:`backup_rename`—— 改名移走、原路径消失。适合「文件不存在才触发重建」
  的场景（r5_tts 批量重合成，避免旧 mp3 冒充新句）。
两者都是**改名/复制，不调用 unlink**，规避本机批量删除拦截。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def load_json(path: str | Path, default: Any = None, *, required: bool = False) -> Any:
    """读 UTF-8 JSON。文件不存在/损坏时返回 default；required=True 则抛异常。"""
    p = Path(path)
    if not p.exists():
        if required:
            raise FileNotFoundError(p)
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if required:
            raise ValueError(f"JSON 损坏: {p}: {exc}") from exc
        return default


def read_text(path: str | Path, *, encoding: str = "utf-8-sig",
              errors: str = "replace") -> str:
    """读文本。默认 utf-8-sig 兼容带 BOM 的字幕/CSV。"""
    return Path(path).read_text(encoding=encoding, errors=errors)


def _atomic_write(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """同目录临时文件 + os.replace，Windows 跨盘符安全。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        # 失败时清理临时文件；不影响原有文件
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text_atomic(path: str | Path, text: str, *,
                      encoding: str = "utf-8",
                      backup_tag: str | None = None) -> Path:
    """原子写文本。传入 backup_tag 时先 :func:`backup_copy` 一份。"""
    p = Path(path)
    if backup_tag and p.exists():
        backup_copy(p, backup_tag)
    _atomic_write(p, text, encoding=encoding)
    return p


def dump_json(path: str | Path, data: Any, *, indent: int = 1,
              ensure_ascii: bool = False,
              backup_tag: str | None = None) -> Path:
    """原子写 JSON（默认缩进 1、不转义中文，与现有产物格式一致）。"""
    text = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
    if not text.endswith("\n"):
        text += "\n"
    return write_text_atomic(path, text, backup_tag=backup_tag)


def _backup_path(p: Path, tag: str) -> Path:
    dst = p.with_name(p.name + tag)
    if dst.exists():
        dst = p.with_name(p.name + tag + "_" + datetime.now().strftime("%H%M%S"))
    return dst


def backup_copy(p: str | Path, tag: str) -> Path | None:
    """复制备份并**保留**原文件，返回备份路径；文件不存在返回 None。"""
    src = Path(p)
    if not src.exists():
        return None
    dst = _backup_path(src, tag)
    shutil.copy2(src, dst)
    return dst


def backup_rename(p: str | Path, tag: str) -> Path | None:
    """改名备份（原路径消失），返回备份路径；文件不存在返回 None。

    典型用途：r5_tts 的 ``--only`` / ``--redo`` 强制重合成前移走旧 mp3。
    """
    src = Path(p)
    if not src.exists():
        return None
    dst = _backup_path(src, tag)
    src.rename(dst)
    return dst
