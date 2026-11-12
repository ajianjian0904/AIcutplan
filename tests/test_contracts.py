# -*- coding: utf-8 -*-
"""L0·aicutplan.contracts 单元测试：窗口/句子/TTS 数据契约与渲染前 lint。

这些规则对应本项目真实事故（旁白 t0 漂移、留白窗误判、旧 mp3 冒充新句），
测试用临时目录造最小工程，不碰真实成片。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicutplan import contracts as CT
from aicutplan.io import dump_json


def _win(a: float, z: float, tier: str = "line", **extra) -> dict:
    w = {"new_start": a, "new_end": z, "src_start": a + 100,
         "src_end": z + 100, "dur": z - a, "tier": tier}
    w.update(extra)
    return w


def _project(td: str, wins, items, sent_win_idx, tts=None) -> Path:
    root = Path(td) / "proj"
    (root / "json").mkdir(parents=True)
    (root / "tts").mkdir()
    (root / "project.json").write_text("{}", encoding="utf-8")
    dump_json(root / "json" / "cutplan.json", {"windows": wins})
    dump_json(root / "json" / "narration.json", {"items": items, "stats": {}})
    dump_json(root / "json" / "align.json", {"sent_win_idx": sent_win_idx})
    if tts is not None:
        dump_json(root / "json" / "tts-result.json", {"items": tts})
    return root


class ValidateWindowsTests(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(CT.validate_windows([_win(0, 3), _win(3, 6)]), [])

    def test_missing_field(self):
        w = _win(0, 3)
        del w["tier"]
        self.assertTrue(any("缺字段" in x for x in CT.validate_windows([w])))

    def test_gap_detected(self):
        bad = CT.validate_windows([_win(0, 3), _win(3.5, 6.5)])
        self.assertTrue(any("未接上一窗" in x for x in bad))

    def test_dur_mismatch(self):
        w = _win(0, 3)
        w["dur"] = 2.0
        self.assertTrue(any("不一致" in x for x in CT.validate_windows([w])))

    def test_non_positive_length(self):
        self.assertTrue(any("非正时长" in x
                            for x in CT.validate_windows([_win(3, 3)])))


class UncoveredTests(unittest.TestCase):
    def test_raw_windows_excluded(self):
        wins = [_win(0, 3), _win(3, 6, "hold_hl", ost_mode="original"),
                _win(6, 9)]
        self.assertEqual(CT.uncovered_windows(wins, {"0": [0, 0]}), [2])

    def test_all_covered(self):
        wins = [_win(0, 3), _win(3, 6)]
        self.assertEqual(CT.uncovered_windows(wins, {"0": [0, 1]}), [])


class AlignmentTests(unittest.TestCase):
    def test_aligned(self):
        items = [{"i": 0, "t0": 0.0, "text": "A"}]
        self.assertEqual(CT.check_alignment(items, [_win(0, 3)], {0: 0}), [])

    def test_drift_detected(self):
        items = [{"i": 0, "t0": 0.042, "text": "A"}]
        bad = CT.check_alignment(items, [_win(0, 3)], {0: 0})
        self.assertTrue(any("t0 偏差" in x for x in bad))

    def test_tiny_drift_within_tolerance(self):
        items = [{"i": 0, "t0": 0.0004, "text": "A"}]
        self.assertEqual(CT.check_alignment(items, [_win(0, 3)], {0: 0}), [])

    def test_missing_anchor(self):
        self.assertTrue(any("无有效窗口锚点" in x
                            for x in CT.check_alignment(
                                [{"i": 0, "t0": 0}], [_win(0, 3)], {})))


class TtsConsistencyTests(unittest.TestCase):
    def test_consistent(self):
        items = [{"i": 0, "t0": 0.0, "dur": 2.0, "text": "布衣注入"}]
        tts = [{"i": 0, "text": "布衣注入", "tts_dur": 2.0}]
        self.assertEqual(CT.check_tts_consistency(items, tts), [])

    def test_text_mismatch(self):
        items = [{"i": 0, "t0": 0.0, "dur": 2.0, "text": "新词"}]
        tts = [{"i": 0, "text": "旧词", "tts_dur": 2.0}]
        self.assertTrue(any("不一致" in x
                            for x in CT.check_tts_consistency(items, tts)))

    def test_missing_record_and_count(self):
        items = [{"i": 0, "t0": 0.0, "text": "A"},
                 {"i": 1, "t0": 3.0, "text": "B"}]
        bad = CT.check_tts_consistency(items, [{"i": 0, "text": "A"}])
        self.assertTrue(any("缺 TTS 记录" in x for x in bad))
        self.assertTrue(any("条数" in x for x in bad))

    def test_missing_mp3(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td, [_win(0, 3)],
                            [{"i": 0, "t0": 0.0, "dur": 2.0, "text": "A"}],
                            {"0": [0, 0]})
            bad = CT.check_tts_consistency(
                [{"i": 0, "t0": 0.0, "text": "A"}],
                [{"i": 0, "text": "A"}], mp3_dir=root / "tts")
            self.assertTrue(any("缺 mp3" in x for x in bad))
            (root / "tts" / "000.mp3").write_bytes(b"audio")
            self.assertEqual(CT.check_tts_consistency(
                [{"i": 0, "t0": 0.0, "text": "A"}],
                [{"i": 0, "text": "A"}], mp3_dir=root / "tts"), [])


class LintTests(unittest.TestCase):
    def test_clean_project_including_raw_window(self):
        with tempfile.TemporaryDirectory() as td:
            wins = [_win(0, 3), _win(3, 6, "hold_hl", ost_mode="original")]
            items = [{"i": 0, "t0": 0.0, "dur": 2.0, "text": "七个字左右", "beat": 0}]
            root = _project(td, wins, items, {"0": [0, 0]})
            self.assertEqual(CT.lint_narration(root), [])

    def test_uncovered_line_window_reported(self):
        with tempfile.TemporaryDirectory() as td:
            wins = [_win(0, 3), _win(3, 6)]
            items = [{"i": 0, "t0": 0.0, "dur": 2.0, "text": "七个字左右", "beat": 0}]
            root = _project(td, wins, items, {"0": [0, 0]})
            self.assertTrue(any("未覆盖窗口" in x for x in CT.lint_narration(root)))

    def test_overlong_sentence_reported(self):
        with tempfile.TemporaryDirectory() as td:
            wins = [_win(0, 3)]
            items = [{"i": 0, "t0": 0.0, "dur": 2.0, "text": "很长的旁白" * 6,
                      "beat": 0}]
            root = _project(td, wins, items, {"0": [0, 0]})
            self.assertTrue(any("槽位" in x for x in CT.lint_narration(root)))

    def test_missing_binding_reported(self):
        with tempfile.TemporaryDirectory() as td:
            wins = [_win(0, 3)]
            items = [{"i": 0, "t0": 0.0, "dur": 2.0, "text": "A", "beat": 0}]
            root = _project(td, wins, items, {})
            self.assertTrue(any("无窗口绑定" in x for x in CT.lint_narration(root)))

    def test_order_regression_reported(self):
        with tempfile.TemporaryDirectory() as td:
            wins = [_win(0, 3), _win(3, 6)]
            items = [
                {"i": 0, "t0": 0.0, "dur": 2.0, "text": "A", "beat": 0},
                {"i": 1, "t0": 3.0, "dur": 2.0, "text": "B", "beat": 0},
            ]
            root = _project(td, wins, items, {"0": [1, 1], "1": [0, 0]})
            self.assertTrue(any("顺序回跳" in x for x in CT.lint_narration(root)))

    def test_uses_measured_tts_dur_only_for_same_draft(self):
        # 同稿时实测时长 2.0s < 槽位，不应误报超槽
        with tempfile.TemporaryDirectory() as td:
            wins = [_win(0, 3)]
            text = "很长的旁白" * 6
            items = [{"i": 0, "t0": 0.0, "dur": 2.0, "text": text, "beat": 0}]
            tts = [{"i": 0, "text": text, "tts_dur": 2.0}]
            root = _project(td, wins, items, {"0": [0, 0]}, tts=tts)
            # 显式把 tts-result 设为「不早于 narration」，确保走同稿实测时长分支
            import os
            import time
            nf = root / "json" / "narration.json"
            tf = root / "json" / "tts-result.json"
            now = time.time()
            os.utime(nf, (now - 5, now - 5))
            os.utime(tf, (now, now))
            self.assertEqual(CT.lint_narration(root), [])


class SummaryTests(unittest.TestCase):
    def test_quality_keys(self):
        with tempfile.TemporaryDirectory() as td:
            wins = [_win(0, 3), _win(3, 6, "hold_hl")]
            items = [{"i": 0, "t0": 0.0, "dur": 2.0, "text": "七个字左右",
                      "beat": 0}]
            tts = [{"i": 0, "text": "七个字左右", "tts_dur": 1.5}]
            root = _project(td, wins, items, {"0": [0, 0]}, tts=tts)
            q = CT.summarize_quality(root)
            self.assertEqual(q["句数"], 1)
            self.assertEqual(q["字数"], 5)
            self.assertEqual(q["窗口数"], 2)
            self.assertEqual(q["留白窗数"], 1)
            self.assertAlmostEqual(q["语音覆盖"], 50.0)

    def test_format_issues(self):
        self.assertIn("全部通过", CT.format_issues([]))
        self.assertIn("2 项问题", CT.format_issues(["a", "b"]))


if __name__ == "__main__":
    unittest.main()
