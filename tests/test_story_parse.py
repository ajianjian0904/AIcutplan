# -*- coding: utf-8 -*-
"""L1·story_parse 段缓存测试：缓存键必须含内容版本。

历史缺陷：`seg_NN.json` 只要存在就命中，改了台词/重跑镜头描述仍返回旧事件。
这里验证：输入不变才命中；台词、证据、prompt 版本变了必须重解析；旧缓存按 mtime 兼容。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aicutplan.io import dump_json
from aicutplan.shared import story_parse as SP


class SegmentCacheTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.proj = self.tmp / "proj"
        (self.proj / "json").mkdir(parents=True)
        for n in ("ja-segments.json", "shots_desc.json", "scene_desc.json"):
            dump_json(self.proj / "json" / n, {"x": 1})
        self.seg = {"i": 0, "n": 1, "a": 0.0, "b": 30.0}
        p = patch.object(SP, "CACHE", self.tmp / "cache")
        p.start()
        self.addCleanup(p.stop)
        self.calls: list[str] = []

    # -- 桩 ----------------------------------------------------------
    def _vision(self, system, q, labeled, model=None, max_tokens=0):
        self.calls.append(q)
        return {"text": json.dumps([{"who": "某人", "what": "做了事"}], ensure_ascii=False),
                "usage": {"total_tokens": 1}, "model": model or "m"}

    def parse(self, *, text="台词一", force=False):
        with patch.object(SP, "zh_subtitles",
                          return_value=[{"t0": 1.0, "t1": 2.0, "text": text}]), \
             patch.object(SP, "seg_frames", return_value=[(1.0, self.tmp / "f.jpg")]), \
             patch.object(SP, "vision_chat", side_effect=self._vision), \
             patch.object(SP.EF, "segment_brief",
                          return_value=("", {"kept": 0, "shots": 0, "chars": 0})):
            return SP.parse_segment(self.proj, self.seg, force=force)

    def cache_file(self) -> Path:
        return SP.workdir(self.proj) / "seg_00.json"

    # -- 用例 --------------------------------------------------------
    def test_hit_when_inputs_unchanged(self):
        d1 = self.parse()
        d2 = self.parse()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(d1["events"], d2["events"])
        self.assertEqual(json.loads(self.cache_file().read_text(encoding="utf-8"))["cache_sig"],
                         d1["cache_sig"])

    def test_miss_when_dialogue_changes(self):
        self.parse(text="台词一")
        self.parse(text="台词改了")
        self.assertEqual(len(self.calls), 2)

    def test_miss_when_evidence_changes(self):
        self.parse()
        dump_json(self.proj / "json" / "shots_desc.json", {"x": 2})
        self.parse()
        self.assertEqual(len(self.calls), 2)

    def test_miss_when_prompt_version_changes(self):
        self.parse()
        with patch.object(SP, "CACHE_VER", SP.CACHE_VER + 1):
            self.parse()
        self.assertEqual(len(self.calls), 2)

    def test_force_bypasses_cache(self):
        self.parse()
        self.parse(force=True)
        self.assertEqual(len(self.calls), 2)

    def test_legacy_cache_without_sig(self):
        # 旧缓存（无 cache_sig）比输入旧 → 不命中
        self.parse()
        d = json.loads(self.cache_file().read_text(encoding="utf-8"))
        d.pop("cache_sig", None)
        self.cache_file().write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        old = time.time() - 100
        os.utime(self.cache_file(), (old, old))
        for n in ("ja-segments.json", "shots_desc.json", "scene_desc.json"):
            os.utime(self.proj / "json" / n, (time.time(), time.time()))
        self.parse()
        self.assertEqual(len(self.calls), 2)

    def test_legacy_cache_newer_than_inputs_is_reused(self):
        self.parse()
        d = json.loads(self.cache_file().read_text(encoding="utf-8"))
        d.pop("cache_sig", None)
        self.cache_file().write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        self.parse()
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
