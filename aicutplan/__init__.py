# -*- coding: utf-8 -*-
"""aicutplan · 漫剪解说流水线 —— 分层单一真相。

=============================================================================
分层（完整说明见 docs/ARCHITECTURE.md）
=============================================================================
  L0 基础层   aicutplan/           路径 / IO / 媒体 / 指纹 / 契约 / 配置 / 作品库 / 清理
  L1 阶段层   aicutplan/shared/    新旧两链共用能力（旁白 / 台词 / 对齐 / 证据）
              aicutplan/chain/     cut-first 主链阶段（编排 + 各步）
              aicutplan/session/   会话内读图工具（产出被主链消费）
              aicutplan/legacy/    旧检索腿（web 台 17 步链仍调用）
  L2 门面     aicutplan/cli.py    ``python -m aicutplan <cmd>``，只转发不重写逻辑
  L3 界面     aicutplan/webui/ + web/   本地校对台（127.0.0.1:8765）

=============================================================================
依赖方向（硬约束）
=============================================================================
``chain / webui → shared → L0`` 单向；``shared`` 禁止反向依赖。
``legacy → shared / webui`` 允许。新增逻辑先问「能不能放进 L0」，
同一能力只允许一份实现，兼容别名必须指向包内实现。

工程侧的 ``cut.py / r5_tts.py / r6_mix.py / r7_mux.py`` 按「project.json 所在目录」
约定读写 ``json/``、``out/``、``tts/``，与包内路径约定（:mod:`aicutplan.paths`）一致。

=============================================================================
本包的硬规则
=============================================================================
1. 零第三方依赖：只用标准库（Pillow 仅在调用宫格图时由调用方自行 import）。
2. 不读写工程目录以外的状态；所有需要落盘的函数显式接收路径。
3. 「文件存在 ≠ 内容最新」：缓存一律走 :mod:`aicutplan.cache` 的内容指纹，
   不复制「if path.exists(): 跳过」这种会静默复用旧产物的判断。
   历史上因此产生过：旧 TTS 音频冒充新句、质检帧取到上一版成片等事故。

用法::

    from aicutplan.paths import ProjectPaths
    from aicutplan.io import load_json
    from aicutplan.contracts import lint_narration

    paths = ProjectPaths(project_json)
    plan = load_json(paths.json / "cutplan.json")
    issues = lint_narration(paths.root)
"""
from __future__ import annotations

__version__ = "0.9.1"
__all__ = [
    "__version__",
    "paths", "io", "media", "cache", "contracts",
    "subtitles", "planning", "evidence", "config", "workspace", "clean", "freshness", "cli",
]
