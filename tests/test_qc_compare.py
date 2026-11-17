# -*- coding: utf-8 -*-
"""L1·质检交叉验证比对：两套 verdict 的一致率、分歧、probe 并集。

纯函数 + 目录读取两个层面都测，零模型、零 ffmpeg。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aicutplan.chain import qc_compare as QC


def _write(folder: Path, batches: dict[str, list[dict]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, rows in batches.items():
        (folder / name).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


class LoadTests(unittest.TestCase):
    def test_reads_all_batches_and_later_wins(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write(d, {"verdict_00.json": [{"i": 0, "match": 0, "why": "old"}],
                       "verdict_01.json": [{"i": 1, "match": 1, "why": "ok"}]})
            got = QC.load_verdicts(d)
            self.assertEqual(got[0]["match"], 0)
            self.assertEqual(got[1]["match"], 1)

    def test_missing_dir_is_empty(self):
        self.assertEqual(QC.load_verdicts("Z:/nope"), {})

    def test_bad_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "verdict_00.json").write_text("{not json", encoding="utf-8")
            _write(d, {"verdict_01.json": [{"i": 2, "match": 0, "why": "x"}]})
            self.assertEqual(list(QC.load_verdicts(d)), [2])


class CompareTests(unittest.TestCase):
    def test_agreement_and_union(self):
        a = {0: {"match": 1, "why": ""}, 1: {"match": 0, "why": "a"}, 2: {"match": 1, "why": ""}}
        b = {0: {"match": 1, "why": ""}, 1: {"match": 1, "why": "b"}, 2: {"match": 0, "why": "b"}}
        rep = QC.compare(a, b)
        self.assertEqual(rep["both"], 3)
        self.assertEqual(rep["agreed"], 1)
        self.assertEqual(rep["zeros_a"], [1])
        self.assertEqual(rep["zeros_b"], [2])
        self.assertEqual(rep["union_zeros"], [1, 2])
        self.assertEqual([d["i"] for d in rep["disagree"]], [1, 2])

    def test_only_one_side(self):
        rep = QC.compare({0: {"match": 0, "why": ""}}, {1: {"match": 1, "why": ""}})
        self.assertEqual(rep["only_a"], [0])
        self.assertEqual(rep["only_b"], [1])
        self.assertEqual(rep["both"], 0)
        self.assertIsNone(rep["agreement"])

    def test_report_mentions_probe_set(self):
        a = {0: {"match": 1, "why": ""}, 3: {"match": 0, "why": "x"}}
        b = {0: {"match": 1, "why": ""}, 3: {"match": 1, "why": "y"}}
        text = QC.format_report(QC.compare(a, b))
        self.assertIn("建议 probe", text)
        self.assertIn("[3]", text)


class CliTests(unittest.TestCase):
    def test_writes_qc_cross_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            (root / "json").mkdir(parents=True)
            _write(root / "cross" / "a", {"verdict_00.json": [{"i": 0, "match": 0, "why": "a"}]})
            _write(root / "cross" / "b", {"verdict_00.json": [{"i": 0, "match": 1, "why": "b"}]})
            code = QC.main(["--a", str(root / "cross" / "a"), "--b", str(root / "cross" / "b"),
                            "--project", str(root / "project.json")])
            self.assertEqual(code, 0)
            saved = json.loads((root / "json" / "qc_cross.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["union_zeros"], [0])
            self.assertEqual(saved["disagree"][0]["i"], 0)

    def test_missing_side_returns_2(self):
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "empty"
            empty.mkdir()
            self.assertEqual(QC.main(["--a", str(empty), "--b", str(empty)]), 2)


if __name__ == "__main__":
    unittest.main()
