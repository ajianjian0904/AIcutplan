# -*- coding: utf-8 -*-
"""L0·aicutplan.evidence 单元测试：证据打分、格式化、按预算压缩。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicutplan import evidence as EV
from aicutplan.io import dump_json


class ScoreTests(unittest.TestCase):
    def test_dialogue_is_most_valuable(self):
        plain = EV.score_shot({"desc": "少年走在街上"})
        with_dlg = EV.score_shot({"desc": "少年走在街上", "dlg": "追放？"})
        self.assertAlmostEqual(with_dlg - plain, 2.0)

    def test_emotion_keywords_and_facts(self):
        with_emo = EV.score_shot({"desc": "壮汉怒目震惊", "depth": "对峙"})
        self.assertGreater(with_emo, 1.0)
        with_facts = EV.score_shot({"desc": "少年走在街上", "facts": ["+13033"]})
        self.assertAlmostEqual(with_facts - 0.0, 0.8)

    def test_ordering_matches_priority(self):
        rows = [
            {"a": 1, "b": 2, "desc": "空镜"},
            {"a": 2, "b": 3, "desc": "光球", "facts": ["+13033"]},
            {"a": 3, "b": 4, "desc": "台词", "dlg": "（バリオス）追放？"},
        ]
        scores = [EV.score_shot(r) for r in rows]
        self.assertEqual(scores, sorted(scores))


class LineTests(unittest.TestCase):
    def test_facts_dict_and_string_both_work(self):
        d = EV.format_shot_line({"a": 6.4, "b": 8.1, "desc": "紫光台",
                                 "facts": [{"t": 6.5, "fact": "+13033 注入"}]})
        s = EV.format_shot_line({"a": 6.4, "b": 8.1, "desc": "紫光台",
                                 "facts": ["+13033 注入"]})
        self.assertIn("+13033", d)
        self.assertIn("+13033", s)

    def test_line_truncated(self):
        ln = EV.format_shot_line({"a": 0, "b": 1, "desc": "很长" * 100})
        self.assertLessEqual(len(ln), EV.MAX_LINE)

    def test_includes_time_desc_depth_dialogue(self):
        ln = EV.format_shot_line({"a": 10, "b": 12, "desc": "少年跪坐",
                                  "depth": "沉默", "dlg": "追放"})
        self.assertIn("10-12s", ln)
        self.assertIn("少年跪坐", ln)
        self.assertIn("沉默", ln)
        self.assertIn("台词", ln)


class CompactTests(unittest.TestCase):
    def test_restores_time_order_after_topn(self):
        rows = [
            {"a": 30.0, "b": 31.0, "desc": "结果", "dlg": "台词C"},
            {"a": 10.0, "b": 11.0, "desc": "起因", "dlg": "台词A"},
            {"a": 20.0, "b": 21.0, "desc": "经过", "dlg": "台词B"},
        ]
        txt, n = EV.compact(rows, 3, 1000)
        self.assertEqual(n, 3)
        self.assertLess(txt.index("10-11s"), txt.index("20-21s"))
        self.assertLess(txt.index("20-21s"), txt.index("30-31s"))

    def test_respects_max_rows_and_chars(self):
        rows = [{"a": float(i), "b": float(i) + 1, "desc": f"镜{i}"} for i in range(20)]
        txt, n = EV.compact(rows, 4, 500)
        self.assertLessEqual(n, 4)
        self.assertLessEqual(len(txt), 500)

    def test_char_budget_can_stop_early(self):
        rows = [{"a": float(i), "b": float(i) + 1,
                 "desc": "很长描述" * 8, "dlg": "台词"} for i in range(10)]
        _, n = EV.compact(rows, 10, 80)
        self.assertLess(n, 10)


class LoadShotsTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(EV.load_shots(td), [])

    def test_filters_rows_without_desc(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "json"
            p.mkdir()
            dump_json(p / "shots_desc.json", {"rows": [
                {"i": 0, "a": 0, "b": 1, "desc": "有效"},
                {"i": 1, "a": 1, "b": 2, "desc": ""},
            ]})
            self.assertEqual(len(EV.load_shots(td)), 1)

    def test_corrupt_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "json"
            p.mkdir()
            (p / "shots_desc.json").write_text("{broken", encoding="utf-8")
            self.assertEqual(EV.load_shots(td), [])


class WindowTests(unittest.TestCase):
    def test_in_window_by_overlap(self):
        rows = [{"a": 0, "b": 5, "desc": "A"},
                {"a": 5, "b": 10, "desc": "B"},
                {"a": 10, "b": 15, "desc": "C"},
                {"a": 20, "b": 25, "desc": ""}]
        self.assertEqual([r["desc"] for r in EV.in_window(rows, 4, 6)], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
