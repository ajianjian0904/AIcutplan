# -*- coding: utf-8 -*-
"""L1·画面数字 OCR：归一规则 + 三态判定 + 识别缓存。

本测试不依赖 OCR 引擎（read_frame 被替换），所以 CI 无模型也能跑；
真引擎的准确率靠 examples/EP01 上的手工核对，不进单测。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aicutplan.io import load_json
from aicutplan.shared import ocr_numbers as ON


def _project(td: str) -> tuple[Path, Path]:
    proj = Path(td) / "EP01"
    (proj / "json").mkdir(parents=True)
    frame = proj / ".numframes" / "num_0000_0000000_abcdef.jpg"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"J" * 900)
    dense = proj / ".numframes" / "numd_0000_000115000_abcdef.jpg"
    dense.write_bytes(b"D" * 900)
    return proj, frame


class NormalizeTests(unittest.TestCase):
    def test_fullwidth_and_separators(self):
        self.assertEqual(ON.normalize("＋５１５４"), "+5154")
        self.assertEqual(ON.normalize("1,000"), "1000")
        self.assertEqual(ON.normalize("1 000"), "1000")
        self.assertEqual(ON.normalize("３０３３！"), "3033!")

    def test_numbers_in_texts(self):
        self.assertEqual(ON.numbers_in_texts(["＋５１５４"]), {5154.0})
        self.assertEqual(ON.numbers_in_texts(["残り ３０３３！", "1,000"]), {3033.0, 1000.0})
        self.assertEqual(ON.numbers_in_texts(["銅の剣（安物）"]), set())


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.proj, self.frame = _project(self.td.name)
        self.dense_frame = self.proj / ".numframes" / "numd_0000_000115000_abcdef.jpg"
        self.texts: list[str] = []
        self.dense_texts: list[str] = []
        self.calls = 0

        def fake_read(path):
            self.calls += 1
            return list(self.dense_texts if "numd_" in str(path) else self.texts)

        self.patches = [
            mock.patch.object(ON, "read_frame", fake_read),
            mock.patch.object(ON.NF, "extract", lambda *_a, **_k: [
                {"i": 0, "text": "先给最便宜的铜剑砸了一万点", "missing": [10000.0],
                 "t": 10.6, "path": str(self.frame)}]),
            mock.patch.object(ON.NC, "check_details", lambda *_a, **_k: [
                {"i": 0, "text": "先给最便宜的铜剑砸了一万点", "missing": [10000.0],
                 "lo": 0, "hi": 0, "t_out": 10.6, "t_src": 100.6,
                 "out_span": [10.0, 13.5], "src_span": [100.0, 103.5]}]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.td.cleanup()

    def test_screen_other_when_frame_shows_another_number(self):
        # 画面 +2750（强化增量）vs 旁白 10000（可能指别的口径）：只作参考，不判对错
        self.texts = ["＋５１５４"]
        result = ON.verify(self.proj)
        row = result["sentences"][0]
        self.assertEqual(row["status"], "screen-other")
        self.assertEqual(row["frame_numbers"], [5154.0])
        self.assertEqual(row["basis"], "dialogue-first")

    def test_screen_match_when_frame_matches(self):
        self.texts = ["＋１００００"]
        result = ON.verify(self.proj)
        self.assertEqual(result["sentences"][0]["status"], "screen-match")

    def test_no_text_when_frame_has_no_number(self):
        self.texts = ["銅の剣（安物）"]
        result = ON.verify(self.proj)
        self.assertEqual(result["sentences"][0]["status"], "no-text")

    def test_stylized_digit_is_noisy_not_conflict(self):
        # 美术字被拆成「13U5」：只作疑似，不自动判冲突
        self.texts = ["13U5"]
        result = ON.verify(self.proj)
        row = result["sentences"][0]
        self.assertEqual(row["status"], "noisy")
        self.assertEqual(row["frame_numbers"], [])
        self.assertEqual(row["noisy_numbers"], [5.0, 13.0])

    def test_artifact_written(self):
        self.texts = ["+5154"]
        ON.verify(self.proj)
        saved = load_json(self.proj / "json" / "ocr_numbers.json", {}) or {}
        self.assertEqual(saved.get("ver"), ON.VER)
        self.assertEqual(saved["sentences"][0]["status"], "screen-other")
        self.assertIn("texts", saved["frames"][0])

    def test_cache_skips_second_read(self):
        self.texts = ["+5154"]
        ON.verify(self.proj)
        self.assertEqual(self.calls, 1)
        ON.verify(self.proj)
        self.assertEqual(self.calls, 1, "同一帧签名不应重复识别")
        ON.verify(self.proj, force=True)
        self.assertEqual(self.calls, 2)

    def test_unknown_engine_is_reported_not_raised(self):
        result = ON.verify(self.proj)
        self.assertIn(result["engine"], ("rapidocr_onnxruntime", "unavailable"))


class DensePassTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.proj, self.frame = _project(self.td.name)
        self.dense_frame = self.proj / ".numframes" / "numd_0000_000115000_abcdef.jpg"
        self.texts: list[str] = []
        self.dense_texts: list[str] = []

        def fake_read(path):
            return list(self.dense_texts if "numd_" in str(path) else self.texts)

        self.patches = [
            mock.patch.object(ON, "read_frame", fake_read),
            mock.patch.object(ON.NF, "extract", lambda *_a, **_k: [
                {"i": 0, "text": "先给最便宜的铜剑砸了一万点", "missing": [10000.0],
                 "t": 10.6, "path": str(self.frame)}]),
            mock.patch.object(ON.NC, "check_details", lambda *_a, **_k: [
                {"i": 0, "text": "先给最便宜的铜剑砸了一万点", "missing": [10000.0],
                 "lo": 0, "hi": 0, "t_out": 10.6, "t_src": 100.6,
                 "out_span": [10.0, 13.5], "src_span": [100.0, 103.5]}]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.td.cleanup()

    def test_dense_second_pass_finds_number(self):
        self.texts = []                 # 稀疏三帧一个数字都没读到
        self.dense_texts = ["+2750"]    # 密集补抽读到了
        with mock.patch.object(ON.NF, "extract_dense", lambda *_a, **_k: [
                {"i": 0, "text": "…", "missing": [10000.0], "t": 11.5,
                 "path": str(self.dense_frame), "kind": "dense"}]):
            result = ON.verify(self.proj, dense=True)
        row = result["sentences"][0]
        self.assertEqual(row["status"], "screen-other")
        self.assertEqual(row["frame_numbers"], [2750.0])
        self.assertTrue(row["dense"])
        self.assertEqual(result["dense_ids"], [0])

    def test_dense_skipped_when_sparse_already_found_number(self):
        self.texts = ["+10000"]
        called: list[int] = []

        def fake_dense(_proj, ids, **_k):
            called.append(1)
            return []

        with mock.patch.object(ON.NF, "extract_dense", fake_dense):
            result = ON.verify(self.proj, dense=True)
        self.assertEqual(called, [], "已有画面数字的句子不该再补抽")
        self.assertEqual(result["sentences"][0]["status"], "screen-match")
        self.assertFalse(result["sentences"][0]["dense"])
        self.assertEqual(result["dense_ids"], [])

    def test_dense_off_by_default(self):
        self.texts = []
        with mock.patch.object(ON.NF, "extract_dense",
                               lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应补抽"))):
            result = ON.verify(self.proj)
        self.assertEqual(result["sentences"][0]["status"], "no-text")
        self.assertEqual(result["dense_ids"], [])


class ReportTests(unittest.TestCase):
    def test_counts_and_reference_lines(self):
        result = {"engine": "fake", "sentences": [
            {"i": 1, "missing": [10000.0], "frame_numbers": [5154.0],
             "status": "screen-other", "frames": ["a.jpg"]},
            {"i": 2, "missing": [3000.0], "frame_numbers": [], "status": "no-text", "frames": []},
            {"i": 3, "missing": [13000.0], "frame_numbers": [13000.0],
             "status": "screen-match", "frames": ["b.jpg"]},
        ]}
        text = ON.format_report(result)
        self.assertIn("同数 1 / 异数 1 / 疑似 0 / 未见 1", text)
        self.assertIn("句1 旁白 10000｜画面 5154", text)
        self.assertIn("台词为准，画面数字多为过程数据", text)
        self.assertNotIn("不一致", text)   # 不再下「错」的结论

    def test_empty_report_is_empty_string(self):
        self.assertEqual(ON.format_report({"sentences": []}), "")


if __name__ == "__main__":
    unittest.main()
