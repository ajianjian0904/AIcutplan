# -*- coding: utf-8 -*-
"""L0·aicutplan.io 单元测试：原子写、JSON 读写容错、两类备份语义。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aicutplan import io as IO


class LoadJsonTests(unittest.TestCase):
    def test_missing_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(IO.load_json(Path(td) / "x.json", default={"a": 1}),
                             {"a": 1})

    def test_missing_required_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                IO.load_json(Path(td) / "x.json", required=True)

    def test_corrupt_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text("{broken", encoding="utf-8")
            self.assertEqual(IO.load_json(p, default=[]), [])
            with self.assertRaises(ValueError):
                IO.load_json(p, required=True)

    def test_reads_utf8_chinese(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            IO.dump_json(p, {"text": "一万三千点"})
            self.assertEqual(IO.load_json(p)["text"], "一万三千点")


class AtomicWriteTests(unittest.TestCase):
    def test_dump_json_keeps_chinese_and_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "x.json"
            IO.dump_json(p, {"t": "布衣注入"})
            self.assertTrue(p.exists())
            self.assertIn("布衣注入", p.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(td).glob("**/*.tmp")), [])

    def test_overwrites_existing_completely(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text("old", encoding="utf-8")
            IO.dump_json(p, {"new": 1})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"new": 1})

    def test_write_text_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.txt"
            IO.write_text_atomic(p, "hello\n")
            self.assertEqual(p.read_text(encoding="utf-8"), "hello\n")

    def test_read_text_handles_bom(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bom.srt"
            p.write_text("\ufeff1\nhello", encoding="utf-8")
            self.assertFalse(IO.read_text(p).startswith("\ufeff"))


class BackupTests(unittest.TestCase):
    def test_backup_copy_keeps_original(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text("A", encoding="utf-8")
            dst = IO.backup_copy(p, ".bak_subs")
            self.assertTrue(p.exists())
            self.assertTrue(dst.exists())
            self.assertEqual(dst.read_text(encoding="utf-8"), "A")

    def test_backup_rename_removes_original(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "039.mp3"
            p.write_bytes(b"old")
            dst = IO.backup_rename(p, ".bak_force")
            self.assertFalse(p.exists())
            self.assertEqual(dst.read_bytes(), b"old")

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope"
            self.assertIsNone(IO.backup_copy(p, ".bak"))
            self.assertIsNone(IO.backup_rename(p, ".bak"))

    def test_existing_backup_gets_unique_name(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text("A", encoding="utf-8")
            (Path(td) / "x.json.bak").write_text("old", encoding="utf-8")
            dst = IO.backup_copy(p, ".bak")
            self.assertNotEqual(dst.name, "x.json.bak")
            self.assertTrue(dst.exists())

    def test_dump_json_with_backup_tag(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            IO.dump_json(p, {"v": 1})
            IO.dump_json(p, {"v": 2}, backup_tag=".bak")
            self.assertEqual(json.loads((Path(td) / "x.json.bak").read_text(
                encoding="utf-8")), {"v": 1})
            self.assertEqual(IO.load_json(p), {"v": 2})


if __name__ == "__main__":
    unittest.main()
