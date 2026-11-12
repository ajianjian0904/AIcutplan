# -*- coding: utf-8 -*-
"""测试包：把仓库根加进 sys.path，保证从任意 cwd 都能 import aicutplan。

运行：D:/Python312/python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
