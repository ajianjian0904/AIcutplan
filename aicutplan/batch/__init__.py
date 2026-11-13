# -*- coding: utf-8 -*-
"""批量出片（会话内导演）唯一实现。

所属层：L1 阶段层 ｜ 职责：把「会话内写好的导演稿」变成窗口表 + 逐句绑定，
再跑完整渲染链。作品库 `_batch/` 只保留**数据**（director_EP0X.json）与薄封装，
不再承载逻辑 —— 关键生产逻辑必须在版本控制、测试与 CI 覆盖之内。

入口::

    python -m aicutplan batch --project <pj> --script director_EP02.json
    python -m aicutplan batch --project <pj> --from-step narrate

模块：
    planner   导演稿 → cutplan / cutplan_meta / story_plan / sents（带 src）
    pipeline  链路编排 + 断点续跑前置完整性校验
"""
from __future__ import annotations

__all__ = ["planner", "pipeline"]
