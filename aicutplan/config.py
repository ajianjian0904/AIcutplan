# [L0 基础层] 职责：仓库级配置文件的位置解析与容错读写 ｜ 调用者：narration / cli / server
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""L0·配置层：配置文件位置解析、容错读取、原子写入。

本项目有三类**仓库级**配置（注意与工程级 ``project.json`` 区分，后者在工程目录）：

============================  ==========================================  ==========
文件                          内容                                       是否入库
============================  ==========================================  ==========
``config/templates.json``     LLM 提示词模板库（web 台可增删改）           是
``config/tts_calibration.json`` TTS 语速/静音校准（calibrate 写入）         是
``config/llm_config.json``    非密钥 LLM 配置 base_url/model/temperature   否（本机）
============================  ==========================================  ==========

密钥**永远只走环境变量**（``DEEPSEEK_API_KEY``），不落配置文件。

三条约定：

1. 配置目录默认 ``<repo>/config``，可用 ``AICUTPLAN_CONFIG_DIR`` 覆盖
   （测试、多环境、CI 都用它，避免污染真实配置）；
2. **旧布局回退**：历史上配置文件直放在仓库根，``config/`` 下不存在时自动读根目录
   同名文件；写回一律写 ``config/``。这样迁移不打断已有本机环境；
3. 读取容错：文件缺失/损坏返回默认值并告警，绝不因配置问题中断整条流水线。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .io import dump_json, load_json
from .paths import repo_root

#: 环境变量：覆盖配置目录（测试/多环境用）
ENV_CONFIG_DIR = "AICUTPLAN_CONFIG_DIR"

#: 已知配置文件 → 用途说明（供 config/README.md 与 CLI 体检输出使用）
KNOWN = {
    "templates.json": "LLM 提示词模板库（web 台「旁白台」可编辑）",
    "tts_calibration.json": "TTS 语速/静音校准（calibrate 产出，非手改）",
    "llm_config.json": "非密钥 LLM 配置（base_url / model / temperature）",
}


def config_dir() -> Path:
    """配置目录：``AICUTPLAN_CONFIG_DIR`` 优先，否则 ``<repo>/config``。"""
    env = os.environ.get(ENV_CONFIG_DIR)
    if env:
        return Path(env).expanduser().resolve()
    return repo_root() / "config"


def legacy_file(name: str) -> Path:
    """旧布局位置：仓库根下的同名文件（迁移前布局，仅用于只读回退）。"""
    return repo_root() / name


def config_file(name: str) -> Path:
    """解析配置文件的真实路径。

    优先级：``config/<name>`` 存在 → 用它；否则旧布局 ``<repo>/<name>`` 存在 → 回退；
    两者都不存在 → 返回 ``config/<name>``（后续写入落新目录）。
    """
    new = config_dir() / name
    if new.exists():
        return new
    old = legacy_file(name)
    if old.exists():
        return old
    return new


# ---- 常用文件的便捷入口（narration 等模块调用，避免各处拼路径）---------------

def templates_file() -> Path:
    return config_file("templates.json")


def calibration_file() -> Path:
    return config_file("tts_calibration.json")


def llm_config_file() -> Path:
    return config_file("llm_config.json")


# ---- 读写 -------------------------------------------------------------------

def load_config(name: str, default: Any = None) -> Any:
    """读配置；缺失或损坏时返回 ``default`` 并在 stderr 告警（不抛异常）。"""
    p = config_file(name)
    if not p.exists():
        return default
    try:
        return load_json(p, default)
    except Exception as e:  # JSON 损坏 / 权限问题：降级而非崩溃
        print(f"[config] 读取 {p} 失败（{type(e).__name__}: {e}），使用默认值",
              file=sys.stderr, flush=True)
        return default


def save_config(name: str, data: Any, *, backup_tag: str | None = None) -> Path:
    """原子写配置到配置目录（自动建目录）；``backup_tag`` 给旧内容留备份。"""
    p = config_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return dump_json(p, data, backup_tag=backup_tag)


def summary() -> list[dict]:
    """体检用：列出已知配置文件的解析路径、是否存在、大小。

    返回 ``[{"name","path","exists","bytes","note","legacy"}]``；
    ``legacy=True`` 表示仍在读仓库根旧文件（建议迁移）。
    """
    out = []
    cdir = config_dir()
    for name, note in KNOWN.items():
        p = config_file(name)
        legacy = p.parent != cdir and p.exists()
        out.append({
            "name": name,
            "path": str(p),
            "exists": p.exists(),
            "bytes": p.stat().st_size if p.exists() else 0,
            "note": note,
            "legacy": legacy,
        })
    return out
