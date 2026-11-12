# -*- coding: utf-8 -*-
"""aicutplan · 漫剪解说流水线 —— L0 公共基础层。

=============================================================================
分层（完整说明见 docs/ARCHITECTURE.md）
=============================================================================
  L0 基础层   aicutplan.*          路径 / IO / 媒体 / 内容指纹 / 数据契约 / 领域纯函数
  L1 阶段层   仓库根目录 *.py      各阶段 CLI，可独立运行，也供编排器以子进程调用
  L2 编排层   cf_pipeline.py       一键主链；qc_loop.py 质检闭环
  L3 界面层   server.py + web/     本地校对台（旧 17 步链，仍在维护）

=============================================================================
为什么阶段脚本保持在根目录（重要设计约束）
=============================================================================
* 跨脚本互相定位依赖 ``ROOT = Path(__file__).resolve().parent``；
* ``cf_pipeline.py`` 用 ``ROOT / "xxx.py"`` 以子进程方式启动阶段脚本；
* 仓库外的工程脚本 ``cut.py / r5_tts.py / r6_mix.py / r7_mux.py`` 按
  「project.json 所在目录」约定读写 ``json/``、``out/``、``tts/``；
* 把阶段脚本搬进包目录会同时打断以上三条，因此本包**只承载无副作用、
  可单测的共享能力**，不改变既有调用方式。

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

__version__ = "0.6.0"
__all__ = [
    "__version__",
    "paths", "io", "media", "cache", "contracts",
    "subtitles", "planning", "evidence", "cli",
]
