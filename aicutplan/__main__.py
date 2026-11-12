# -*- coding: utf-8 -*-
"""允许 ``python -m aicutplan <子命令>``。"""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
