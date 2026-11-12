# -*- coding: utf-8 -*-
"""L0·aicutplan.cache 单元测试：内容指纹与缓存失效（本项目历史事故防线）。"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path

from aicutplan import cache as CA
from aicutplan.io import dump_json


class SigTests(unittest.TestCase):
    def test_content_sig_is_stable_and_order_sensitive(self):
        self.assertEqual(CA.content_sig("a", 1, True), CA.content_sig("a", 1, True))
        self.assertNotEqual(CA.content_sig("a", 1), CA.content_sig(1, "a"))
        self.assertEqual(len(CA.content_sig("x")), 12)

    def test_file_sig_changes_with_content(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.bin"
            p.write_bytes(b"A" * 100)
            s1 = CA.file_sig(p)
            self.assertNotEqual(s1, CA.file_sig(Path(td) / "missing.bin"))
            p.write_bytes(b"B" * 100)
            # 同秒覆盖且大小相同仍应能发现（附内容摘要）；mtime 强制拉开以便稳定断言
            os.utime(p, (p.stat().st_mtime + 2, p.stat().st_mtime + 2))
            self.assertNotEqual(s1, CA.file_sig(p))

    def test_source_sig_matches_legacy_formula(self):
        # subs_extract 旧实现逐字等价；改动会让已写盘的 ja-segments.json 全部失配
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "src.mkv"
            v.write_bytes(b"x")
            st = v.stat()
            stream = {"index": 3, "codec": "subrip"}
            raw = (f"{v.resolve()}|{st.st_size}|{int(st.st_mtime)}"
                   f"|{stream['index']}|{stream['codec']}")
            expected = hashlib.md5(raw.encode()).hexdigest()[:12]
            self.assertEqual(CA.source_sig(v, stream), expected)

    def test_project_key_depends_on_path_only(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "ep01"
            b = Path(td) / "ep01cf"
            a.mkdir(); b.mkdir()
            self.assertEqual(CA.project_key(a), CA.project_key(a))
            self.assertNotEqual(CA.project_key(a), CA.project_key(b))
            self.assertEqual(len(CA.project_key(a)), 12)


class StaleTests(unittest.TestCase):
    def _files(self, td):
        src = Path(td) / "src.json"
        art = Path(td) / "art.json"
        src.write_text("{}", encoding="utf-8")
        art.write_text("{}", encoding="utf-8")
        return src, art

    def test_missing_artifact_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(CA.is_stale(Path(td) / "nope.json", Path(td) / "src.json"))

    def test_fresh_artifact_not_stale(self):
        with tempfile.TemporaryDirectory() as td:
            src, art = self._files(td)
            os.utime(src, (time.time() - 10, time.time() - 10))
            os.utime(art, (time.time(), time.time()))
            self.assertFalse(CA.is_stale(art, src))
            self.assertEqual(CA.stale_reason(art, src), "")

    def test_newer_source_makes_stale(self):
        with tempfile.TemporaryDirectory() as td:
            src, art = self._files(td)
            now = time.time()
            os.utime(art, (now - 10, now - 10))
            os.utime(src, (now, now))
            self.assertTrue(CA.is_stale(art, src))
            self.assertIn("输入比产物新", CA.stale_reason(art, src))

    def test_multiple_sources(self):
        with tempfile.TemporaryDirectory() as td:
            src, art = self._files(td)
            other = Path(td) / "video.mkv"
            other.write_bytes(b"x")
            now = time.time()
            os.utime(art, (now - 10, now - 10))
            os.utime(src, (now - 20, now - 20))
            os.utime(other, (now, now))
            self.assertTrue(CA.is_stale(art, [src, other]))
            self.assertFalse(CA.is_stale(art, [src]))


class SigValidTests(unittest.TestCase):
    def test_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "manifest.json"
            CA.write_manifest(p, {"segments": [1, 2]}, "abc123")
            self.assertEqual(CA.sig_valid(p, "abc123"), (True, "ok"))

    def test_mismatch_and_missing_sig(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.json"
            CA.write_manifest(p, {"a": 1}, "sig-a")
            ok, why = CA.sig_valid(p, "sig-b")
            self.assertFalse(ok)
            self.assertIn("指纹不匹配", why)
            dump_json(p, {"a": 1})
            self.assertFalse(CA.sig_valid(p, "sig-b")[0])

    def test_missing_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(CA.sig_valid(Path(td) / "none.json", "x")[0])

    def test_cache_valid_combines_both(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.mkv"
            art = Path(td) / "art.json"
            src.write_bytes(b"x")
            CA.write_manifest(art, {"v": 1}, "sig-a")
            now = time.time()
            os.utime(src, (now - 10, now - 10))
            os.utime(art, (now, now))
            self.assertEqual(CA.cache_valid(art, src, expected_sig="sig-a"),
                             (True, "ok"))
            self.assertFalse(CA.cache_valid(art, src, expected_sig="sig-b")[0])
            os.utime(src, (now + 5, now + 5))
            self.assertFalse(CA.cache_valid(art, src)[0])


if __name__ == "__main__":
    unittest.main()
