# -*- coding: utf-8 -*-
"""L1·数值高清帧：只对命中数值断言的句子抽 1280 帧，并带指纹缓存。

关注四件事：
1. 取帧时刻用「句首窗内前 1.2s 的中点」起步，不是整句中点；
2. 长句追加句中/句末取帧点（数值浮字常出现在动作高潮处，只取首窗会漏）；
3. 成片/源片两条时间轴必须切换（基准错了会抽到无关画面）；
4. 缓存命中不重抽，换片/改窗口后自动失效。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aicutplan.io import dump_json
from aicutplan.shared import number_frames as NF


def _project(td: str, *, flagged: bool = True, span: float = 3.5) -> Path:
    proj = Path(td) / "EP01"
    (proj / "json").mkdir(parents=True)
    (proj / "out").mkdir(parents=True)
    (proj / "out" / "03-rapid-narration.mp4").write_bytes(b"F" * 2048)
    text = "先给最便宜的铜剑砸了一万点" if flagged else "他随手把剑强化了一下"
    dump_json(proj / "json" / "narration.json", {"items": [{"i": 0, "text": text}]})
    dump_json(proj / "json" / "cutplan.json", {"windows": [
        {"new_start": 10.0, "new_end": 10.0 + span,
         "src_start": 100.0, "src_end": 100.0 + span}]})
    dump_json(proj / "json" / "align.json", {"sent_win_idx": {"0": [0, 0]}})
    dump_json(proj / "json" / "ja-segments.json",
              {"paras": [{"t0": 100.0, "t1": 100.0 + span, "text": "3033！"}]})
    return proj


class SamplePointTests(unittest.TestCase):
    def test_short_sentence_uses_first_window_midpoint_only(self):
        det = {"t_out": 5.0, "t_src": 50.0,
               "out_span": [4.0, 5.5], "src_span": [49.0, 50.5]}
        self.assertEqual(NF.sample_points(det, "output"), [5.0])

    def test_long_sentence_adds_middle_and_tail(self):
        det = {"t_out": 10.6, "t_src": 100.6,
               "out_span": [10.0, 13.5], "src_span": [100.0, 103.5]}
        self.assertEqual(NF.sample_points(det, "output"), [10.6, 11.75, 13.1])
        self.assertEqual(NF.sample_points(det, "source"), [100.6, 101.75, 103.1])


class DensePointTests(unittest.TestCase):
    def test_one_frame_per_second(self):
        det = {"t_out": 10.5, "t_src": 100.5,
               "out_span": [10.0, 20.0], "src_span": [100.0, 110.0]}
        pts = NF.dense_points(det, "output")
        self.assertEqual(len(pts), 10)
        self.assertAlmostEqual(pts[0], 10.5, places=3)
        self.assertAlmostEqual(pts[-1], 19.5, places=3)

    def test_capped_for_long_sentence(self):
        det = {"t_out": 0.5, "t_src": 0.5,
               "out_span": [0.0, 100.0], "src_span": [0.0, 100.0]}
        self.assertEqual(len(NF.dense_points(det, "output")), NF.DENSE_MAX)

    def test_tiny_span_gives_single_frame(self):
        det = {"t_out": 5.15, "t_src": 5.15,
               "out_span": [5.0, 5.3], "src_span": [5.0, 5.3]}
        self.assertEqual(NF.dense_points(det, "output"), [5.15])


class NumberFramesTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.calls: list[tuple] = []

        def fake_extract(video, t, dst, *, width=320, quality=4):
            self.calls.append((str(video), round(float(t), 3), int(width)))
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_bytes(b"J" * 900)
            return True

        self.patches = [mock.patch.object(NF.media, "extract_frame", fake_extract)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.td.cleanup()

    def _pick(self, proj: Path, base: str = "output"):
        if base == "output":
            return mock.patch.object(NF, "pick_video",
                                     return_value=(proj / "out" / "03-rapid-narration.mp4", "output"))
        (proj / "ref.mp4").write_bytes(b"R" * 2048)
        return mock.patch.object(NF, "pick_video", return_value=(proj / "ref.mp4", "source"))

    def test_three_frames_for_long_sentence(self):
        proj = _project(self.td.name)
        with self._pick(proj):
            rows = NF.extract(proj)
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(rows[0]["t"], 10.6, places=3)
        self.assertAlmostEqual(rows[-1]["t"], 13.1, places=3)
        self.assertEqual([c[1] for c in self.calls], [10.6, 11.75, 13.1])
        self.assertTrue(all(c[2] == NF.FRAME_WIDTH for c in self.calls))
        self.assertTrue(all(Path(r["path"]).exists() for r in rows))

    def test_single_frame_for_short_sentence(self):
        proj = _project(self.td.name, span=1.5)
        with self._pick(proj):
            rows = NF.extract(proj)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["t"], 10.6, places=3)

    def test_source_timebase_switches_to_src_seconds(self):
        proj = _project(self.td.name)
        with self._pick(proj, "source"):
            rows = NF.extract(proj)
        self.assertAlmostEqual(rows[0]["t"], 100.6, places=3)
        self.assertAlmostEqual(rows[-1]["t"], 103.1, places=3)

    def test_cache_hit_does_not_reextract(self):
        proj = _project(self.td.name)
        with self._pick(proj):
            first = NF.extract(proj)
            self.assertEqual(len(self.calls), 3)
            second = NF.extract(proj)
        self.assertEqual(len(self.calls), 3, "指纹一致时不应重抽")
        self.assertEqual([r["path"] for r in first], [r["path"] for r in second])

    def test_force_reextracts(self):
        proj = _project(self.td.name)
        with self._pick(proj):
            NF.extract(proj)
            NF.extract(proj, force=True)
        self.assertEqual(len(self.calls), 6)

    def test_no_claim_returns_empty(self):
        proj = _project(self.td.name, flagged=False)
        with self._pick(proj):
            self.assertEqual(NF.extract(proj), [])
        self.assertEqual(self.calls, [])

    def test_dense_extract_only_selected_ids(self):
        proj = _project(self.td.name)
        with self._pick(proj):
            rows = NF.extract_dense(proj, {0})
            self.assertTrue(rows)
            self.assertTrue(all(r["kind"] == "dense" for r in rows))
            self.assertTrue(all(r["name"].startswith("numd_0000_") for r in rows))
            # 3.5s 跨度、1 帧/秒 → 10.5 / 11.5 / 12.5
            self.assertEqual([r["t"] for r in rows], [10.5, 11.5, 12.5])
            before = len(self.calls)
            NF.extract_dense(proj, {0})
            self.assertEqual(len(self.calls), before, "密集帧也应命中缓存")

    def test_dense_extract_ignores_unknown_ids(self):
        proj = _project(self.td.name)
        with self._pick(proj):
            self.assertEqual(NF.extract_dense(proj, {99}), [])

    def test_format_rows_groups_by_sentence(self):
        proj = _project(self.td.name)
        with self._pick(proj):
            rows = NF.extract(proj)
        text = NF.format_rows(rows)
        self.assertIn("[数值帧] 3 张", text)
        self.assertIn("句0 疑似 10000（3 帧）", text)
        self.assertIn(rows[0]["path"], text)


if __name__ == "__main__":
    unittest.main()
