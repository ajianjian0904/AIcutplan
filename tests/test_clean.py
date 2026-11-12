# -*- coding: utf-8 -*-
"""L0·aicutplan.clean 单元测试：冗余盘点 / 回收站 / 恢复 / 深清。

全部在 tempfile 内构造最小工程，不碰真实作品库。时间戳用 os.utime 显式控制，
避免「同秒写入」导致过期判据不稳定。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path

from aicutplan import clean as CL
from aicutplan import cli as CLI
from aicutplan.io import dump_json


class CleanFixture(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name) / "EP01"
        for d in ("out", "json", "tts", ".qcframes", "verify", "audio",
                  "runs/r001_20260101-000000_v1"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        dump_json(self.root / "project.json", {"name": "测试集"})
        self.final = self.root / "out" / "03-rapid-narration.mp4"
        self.final.write_bytes(b"FINAL" * 1024)
        now = time.time()
        os.utime(self.final, (now - 1000, now - 1000))
        # 归档指针与当前成片同内容
        dump_json(self.root / "runs" / "latest.json",
                  {"run_id": "r001_20260101-000000_v1",
                   "final_sig": CL.quick_sig(self.final)})
        # 运行产物 / 当前 TTS / runs 内的文件：任何情况下都不该被收走
        (self.root / "tts" / "000.mp3").write_bytes(b"tts")
        (self.root / "runs" / "r001_20260101-000000_v1" / "final.mp4").write_bytes(b"FINAL")
        dump_json(self.root / "json" / "narration.json", {"items": []})

    # -- 构造工具 ----------------------------------------------------
    def add(self, rel: str, data: bytes = b"x" * 128, *, age: float = 0) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def rels(self, items):
        return {x["rel"] for x in items}

    def by_cat(self, items, cat):
        return [x for x in items if x["category"] == cat]


class ScanTests(CleanFixture):
    def test_old_and_dup_final(self):
        self.add("out/03-rapid-narration-v34.mp4", b"OLD" * 64)
        self.add("out/03-rapid-narration-v35.mp4", self.final.read_bytes())
        items = CL.scan(self.root)
        self.assertIn("out/03-rapid-narration-v34.mp4", self.rels(items))
        self.assertEqual([x["rel"] for x in self.by_cat(items, "old_final")],
                         ["out/03-rapid-narration-v34.mp4"])
        self.assertEqual([x["rel"] for x in self.by_cat(items, "dup_final")],
                         ["out/03-rapid-narration-v35.mp4"])
        # 当前成片与 runs/tts 永不入列
        self.assertNotIn("out/03-rapid-narration.mp4", self.rels(items))
        self.assertFalse(any(x["rel"].startswith(("runs/", "tts/")) for x in items))

    def test_backup_files_and_dirs(self):
        self.add("json/narration.json.bak_v35")
        self.add("window_table.md.v31bak")
        self.add("tts_v31_backup/000.mp3", b"a" * 64)
        items = CL.scan(self.root)
        baks = self.by_cat(items, "backup")
        self.assertEqual(self.rels(baks), {"json/narration.json.bak_v35",
                                           "window_table.md.v31bak"})
        self.assertEqual(self.rels(self.by_cat(items, "backup_dir")), {"tts_v31_backup"})
        # 整目录按树体积统计
        self.assertEqual(self.by_cat(items, "backup_dir")[0]["bytes"], 64)
        self.assertNotIn("json/narration.json", self.rels(items))

    def test_qc_frames_and_verify_wav_only_when_stale(self):
        self.add(".qcframes/session/qc_01.jpg", age=3000)      # 早于成片 → 收
        self.add(".qcframes/session/qc_02.jpg", age=10)        # 晚于成片 → 留
        self.add("verify/chk_v31_seg.wav", age=3000)
        self.add("verify/chk_v35_final.wav", age=10)
        items = CL.scan(self.root)
        self.assertEqual(self.rels(self.by_cat(items, "qc_frame")),
                         {".qcframes/session/qc_01.jpg"})
        self.assertEqual(self.rels(self.by_cat(items, "verify_wav")),
                         {"verify/chk_v31_seg.wav"})

    def test_no_final_means_no_stale_judgement(self):
        self.final.unlink()
        self.add(".qcframes/session/qc_01.jpg", age=3000)
        self.add("verify/chk_old.wav", age=3000)
        items = CL.scan(self.root)
        self.assertFalse(self.by_cat(items, "qc_frame"))
        self.assertFalse(self.by_cat(items, "verify_wav"))

    def test_deep_only_adds_regenerable(self):
        self.add("out/01-video-no-ad.mp4", b"1" * 256)
        self.add("out/02-video-rapid.mp4", b"2" * 256)
        self.add("out/mix-rapid.wav", b"3" * 256)
        self.add("audio/base-rapid.wav", b"4" * 256)
        self.add("verify/rapid_seg/p000_a.mp4", b"5" * 256)
        self.add("verify/rapid_seg/list.txt", b"p000\n")
        shallow = CL.scan(self.root)
        self.assertFalse(self.by_cat(shallow, "deep_mid"))
        self.assertFalse(self.by_cat(shallow, "deep_seg"))
        deep = CL.scan(self.root, deep=True)
        self.assertEqual(len(self.by_cat(deep, "deep_mid")), 4)
        self.assertEqual(len(self.by_cat(deep, "deep_seg")), 2)

    def test_summary(self):
        self.add("out/03-rapid-narration-v34.mp4", b"o" * 1000)
        self.add("json/a.json.bak", b"b" * 24)
        sm = CL.summarize(CL.scan(self.root))
        self.assertEqual(sm["count"], 2)
        self.assertEqual(sm["bytes"], 1024)
        self.assertEqual(sm["per"]["old_final"]["count"], 1)


class RecycleTests(CleanFixture):
    def test_dry_run_touches_nothing(self):
        p = self.add("out/03-rapid-narration-v34.mp4")
        rep = CL.clean(self.root)
        self.assertFalse(rep["applied"])
        self.assertTrue(p.exists())
        self.assertFalse((self.root / CL.TRASH_DIR).exists())

    def test_apply_moves_to_trash_with_manifest(self):
        p = self.add("out/03-rapid-narration-v34.mp4", b"o" * 512)
        b = self.add("json/narration.json.bak_v35", b"b" * 64)
        rep = CL.clean(self.root, apply=True)
        self.assertTrue(rep["applied"])
        self.assertEqual(rep["moved"], 2)
        self.assertEqual(rep["bytes"], 576)
        self.assertFalse(p.exists())
        self.assertFalse(b.exists())
        batch = self.root / CL.TRASH_DIR / rep["batch"]
        self.assertTrue((batch / "out" / "03-rapid-narration-v34.mp4").exists())
        mf = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(mf["count"], 2)
        self.assertEqual({x["rel"] for x in mf["items"]},
                         {"out/03-rapid-narration-v34.mp4",
                          "json/narration.json.bak_v35"})

    def test_category_filter(self):
        p1 = self.add("out/03-rapid-narration-v34.mp4")
        p2 = self.add("json/narration.json.bak_v35")
        rep = CL.clean(self.root, apply=True, categories=["backup"])
        self.assertEqual(rep["moved"], 1)
        self.assertTrue(p1.exists())
        self.assertFalse(p2.exists())

    def test_restore_roundtrip(self):
        p = self.add("out/03-rapid-narration-v34.mp4", b"o" * 100)
        content = p.read_bytes()
        rep = CL.clean(self.root, apply=True)
        r = CL.restore(self.root)
        self.assertEqual(r["batch"], rep["batch"])
        self.assertEqual(r["restored"], 1)
        self.assertTrue(p.exists())
        self.assertEqual(p.read_bytes(), content)
        self.assertFalse((self.root / CL.TRASH_DIR).exists())

    def test_restore_does_not_overwrite_current(self):
        p = self.add("out/03-rapid-narration-v34.mp4", b"old")
        CL.clean(self.root, apply=True)
        p.write_bytes(b"new")                       # 期间又生成了同名新文件
        r = CL.restore(self.root)
        self.assertEqual(r["restored"], 0)
        self.assertEqual(r["conflicts"], ["out/03-rapid-narration-v34.mp4"])
        self.assertEqual(p.read_bytes(), b"new")

    def test_empty_frees_and_is_idempotent(self):
        self.add("out/03-rapid-narration-v34.mp4", b"o" * 300)
        CL.clean(self.root, apply=True)
        e = CL.empty(self.root)
        self.assertEqual(e["batches"], 1)
        self.assertGreaterEqual(e["bytes"], 300)   # 含批次 manifest.json
        self.assertFalse((self.root / CL.TRASH_DIR).exists())
        self.assertEqual(CL.empty(self.root), {"batches": 0, "bytes": 0})

    def test_trash_stats(self):
        self.add("json/a.json.bak", b"b" * 10)
        CL.clean(self.root, apply=True)
        st = CL.trash_stats(self.root)
        self.assertEqual(st["batches"], 1)
        self.assertGreaterEqual(st["bytes"], 10)
        self.assertTrue(st["latest"])


class CliTests(CleanFixture):
    def _run(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = CLI.main(["clean", "--project", str(self.root), *argv])
        return rc, buf.getvalue()

    def test_json_preview(self):
        self.add("out/03-rapid-narration-v34.mp4")
        rc, out = self._run("--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertFalse(data["applied"])
        self.assertEqual(data["summary"]["per"]["old_final"]["count"], 1)

    def test_apply_then_empty(self):
        p = self.add("out/03-rapid-narration-v34.mp4", b"o" * 100)
        rc, out = self._run("--apply")
        self.assertEqual(rc, 0)
        self.assertIn(".trash/", out)
        self.assertFalse(p.exists())
        rc, out = self._run("--empty")
        self.assertEqual(rc, 0)
        self.assertIn("释放", out)

    def test_no_redundancy_message(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("没有可回收", out)


if __name__ == "__main__":
    unittest.main()
