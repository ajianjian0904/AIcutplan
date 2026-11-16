# -*- coding: utf-8 -*-
"""L1·段抽帧：内容指纹缓存 + 镜头感知取帧。

对应两个缺陷：
1. 帧缓存只判「文件存在」，换源片后仍拿旧图理解新片（本项目反复复发的缺陷）；
2. 均匀撒帧会漏掉短促动作，12 张图应覆盖 12 个不同画面而非 12 个时间点。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicutplan.io import dump_json, load_json
from aicutplan.shared import story_parse as SP


class SegMarksTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.proj = self.root / "EP01"
        (self.proj / "json").mkdir(parents=True)
        (self.proj / "shotlist.csv").write_text(
            "idx,start,end,bright\n1,0.0,1.5,0.3\n2,1.5,4.0,0.2\n3,4.0,5.2,0.1\n",
            encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def test_one_frame_per_shot_when_few(self):
        marks = SP.seg_marks(self.proj, {"i": 0, "a": 0.0, "b": 5.2}, 12)
        self.assertEqual(len(marks), 3)
        for got, want in zip(marks, (0.75, 2.75, 4.6)):
            self.assertAlmostEqual(got, want, places=3)

    def test_uniform_fallback_without_shotlist(self):
        (self.proj / "shotlist.csv").unlink()
        marks = SP.seg_marks(self.proj, {"i": 0, "a": 0.0, "b": 12.0}, 4)
        self.assertEqual(len(marks), 4)
        self.assertAlmostEqual(marks[0], 1.5, places=3)
        self.assertAlmostEqual(marks[-1], 10.5, places=3)

    def test_bucketed_when_many_shots(self):
        rows = ["idx,start,end,bright"]
        for i in range(24):
            rows.append(f"{i + 1},{i * 0.5:.2f},{i * 0.5 + 0.5:.2f},0.2")
        (self.proj / "shotlist.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        marks = SP.seg_marks(self.proj, {"i": 0, "a": 0.0, "b": 12.0}, 12)
        self.assertEqual(len(marks), 12)
        self.assertEqual(marks, sorted(marks))
        self.assertTrue(all(0.0 <= m <= 12.0 for m in marks))


class SegFramesCacheTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.proj = self.root / "EP01"
        (self.proj / "json").mkdir(parents=True)
        self.src = self.root / "src.mp4"
        self.src.write_bytes(b"x" * 4096)
        dump_json(self.proj / "project.json", {"source_video": str(self.src)})
        (self.proj / "shotlist.csv").write_text(
            "idx,start,end,bright\n1,0.0,1.5,0.3\n2,1.5,4.0,0.2\n3,4.0,5.2,0.1\n",
            encoding="utf-8")
        self._old_cache = SP.CACHE
        SP.CACHE = self.root / "cache"
        self.calls: list[float] = []
        self._old_grab = SP.grab
        SP.grab = self._fake_grab

    def _fake_grab(self, proj, t, out: Path) -> bool:  # 不调 ffmpeg
        self.calls.append(round(float(t), 2))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"J" * 1000)
        return True

    def tearDown(self):
        SP.grab = self._old_grab
        SP.CACHE = self._old_cache
        self.td.cleanup()

    def test_manifest_written_and_reused(self):
        seg = {"i": 0, "a": 0.0, "b": 5.2}
        first = SP.seg_frames(self.proj, seg, 12)
        self.assertEqual(len(first), 3)
        self.assertEqual(len(self.calls), 3)
        mf = Path(SP.CACHE) / SP.pkey(self.proj) / "frames" / "_seg00.json"
        self.assertTrue(mf.exists())
        manifest = load_json(mf)
        self.assertTrue(manifest.get("frames_sig"))
        # 第二次：指纹一致 → 不重抽
        second = SP.seg_frames(self.proj, seg, 12)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual([p.name for _, p in first], [p.name for _, p in second])

    def test_source_change_invalidates_frames(self):
        seg = {"i": 0, "a": 0.0, "b": 5.2}
        first = SP.seg_frames(self.proj, seg, 12)
        tag_first = first[0][1].name.rsplit("_", 1)[-1]
        self.src.write_bytes(b"y" * 8192)          # 换源片
        second = SP.seg_frames(self.proj, seg, 12)
        self.assertGreater(len(self.calls), 3)     # 重抽了
        tag_second = second[0][1].name.rsplit("_", 1)[-1]
        self.assertNotEqual(tag_first, tag_second)  # 指纹前缀不同，永不混用

    def test_interval_change_invalidates_frames(self):
        SP.seg_frames(self.proj, {"i": 0, "a": 0.0, "b": 2.6}, 12)
        SP.seg_frames(self.proj, {"i": 0, "a": 0.0, "b": 5.2}, 12)
        self.assertGreaterEqual(len(self.calls), 5)


if __name__ == "__main__":
    unittest.main()
