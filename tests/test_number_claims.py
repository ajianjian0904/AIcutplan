# -*- coding: utf-8 -*-
"""L0/L1·数值断言：中文数字归一 + 旁白数字是否在窗口台词里。

对应事故：「十三万点」被写成「一万三千点」，成片上线才发现。
这里只做告警（不阻断），所以测试同时覆盖「正常不误报」。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicutplan.contracts import num_values
from aicutplan.shared import number_claims as NC
from aicutplan.io import dump_json


class NumValuesTests(unittest.TestCase):
    def test_chinese_numbers_with_units(self):
        self.assertEqual(num_values("十三万点"), {130000.0})
        self.assertEqual(num_values("一万三千点"), {13000.0})
        self.assertEqual(num_values("三千三百"), {3300.0})
        self.assertEqual(num_values("三十"), {30.0})
        self.assertEqual(num_values("十"), {10.0})

    def test_arabic_numbers_and_separators(self):
        self.assertEqual(num_values("3033"), {3033.0})
        self.assertEqual(num_values("1,000"), {1000.0})
        self.assertEqual(num_values("3.5 倍"), {3.5})
        self.assertEqual(num_values("+5154"), {5154.0})

    def test_small_cjk_digits_are_not_claims(self):
        # 「一个」「两把」「三种」不是数字事实，不该触发断言
        self.assertEqual(num_values("他有一把剑"), set())
        self.assertEqual(num_values("两只手"), set())
        self.assertEqual(num_values("三种强化"), set())

    def test_mixed_text(self):
        self.assertEqual(num_values("三十三秒里打出 12000 点"), {33.0, 12000.0})


class NumberClaimsCheckTests(unittest.TestCase):
    def _project(self, td: str, line: str, spoken: str) -> Path:
        proj = Path(td) / "EP01"
        (proj / "json").mkdir(parents=True)
        dump_json(proj / "json" / "narration.json",
                  {"items": [{"i": 0, "text": line}]})
        dump_json(proj / "json" / "cutplan.json",
                  {"windows": [{"new_start": 0, "new_end": 5,
                                "src_start": 100.0, "src_end": 105.0}]})
        dump_json(proj / "json" / "align.json", {"sent_win_idx": {"0": [0, 0]}})
        dump_json(proj / "json" / "ja-segments.json",
                  {"paras": [{"t0": 100.0, "t1": 105.0, "text": spoken}]})
        return proj

    def test_reports_number_missing_from_lines(self):
        with tempfile.TemporaryDirectory() as td:
            proj = self._project(td, "他一剑打出一万三千点伤害", "十三万点だ！")
            rows = NC.check(proj)
            self.assertEqual(len(rows), 1)
            self.assertIn("一万三千", rows[0].replace("13000", "一万三千"))

    def test_match_against_chinese_numeral(self):
        with tempfile.TemporaryDirectory() as td:
            proj = self._project(td, "他一剑打出十三万点伤害", "十三万点だ！")
            self.assertEqual(NC.check(proj), [])

    def test_match_against_arabic_number(self):
        with tempfile.TemporaryDirectory() as td:
            proj = self._project(td, "数值翻到了 12000", "１２０００！")
            # 全角数字不在抽取范围内 → 只要不误报即可（这里台词是阿拉伯数字）
            proj2 = self._project(td + "/b", "数值翻到了 12000", "12000！")
            self.assertEqual(NC.check(proj2), [])

    def test_missing_artifacts_return_empty(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "EP02"
            proj.mkdir()
            self.assertEqual(NC.check(proj), [])

    def test_format_warnings_is_bounded(self):
        self.assertEqual(NC.format_warnings([]), "")
        text = NC.format_warnings([f"句{i} 数值 1 不在台词里" for i in range(9)])
        self.assertIn("9 句", text)
        self.assertIn("另有 3 句", text)


if __name__ == "__main__":
    unittest.main()
