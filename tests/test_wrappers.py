# -*- coding: utf-8 -*-
"""守卫测试：根目录阶段脚本里的兼容别名必须指向 L0 包的唯一实现。

背景：本项目反复出现「同一逻辑两份实现」（旧 TTS 音频冒充新句、质检帧取到旧
成片）。抽取到 aicutplan 后，用这组测试锁住「只有一份真相」——谁要是在阶段
脚本里又抄了一份函数实现，这里会立刻失败。

被 import 的脚本都在仓库根，且不需要 ffmpeg / 网络 / 模型。
"""
from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path

from aicutplan import cache, contracts, evidence, media, planning, subtitles
from aicutplan.io import dump_json


class SingleImplementationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.CP = importlib.import_module("aicutplan.chain.cutfirst_plan")
        cls.EF = importlib.import_module("aicutplan.shared.evidence_feed")
        cls.SE = importlib.import_module("aicutplan.chain.subs_extract")
        cls.CF = importlib.import_module("aicutplan.chain.cf_pipeline")

    def test_planning_aliases(self):
        self.assertIs(self.CP._pick_hook_clip, planning.pick_hook_clip)
        self.assertIs(self.CP._hook_score, planning.hook_score)
        self.assertIs(self.CP._weight, planning.weight)
        self.assertIs(self.CP._reason, planning.reason)

    def test_evidence_aliases(self):
        self.assertIs(self.EF._score, evidence.score_shot)
        self.assertIs(self.EF._line, evidence.format_shot_line)
        self.assertIs(self.EF._compact, evidence.compact)
        self.assertIs(self.EF.load_shots, evidence.load_shots)

    def test_subtitle_aliases(self):
        self.assertIs(self.SE.parse_srt, subtitles.parse_srt)
        self.assertIs(self.SE.clean_text, subtitles.clean_text)
        self.assertIs(self.SE.merge_same_time, subtitles.merge_same_time)
        self.assertIs(self.SE.sniff_lang, subtitles.sniff_lang)
        self.assertIs(self.SE.probe_subs, subtitles.probe_subs)
        self.assertIs(self.SE.norm, subtitles.norm)

    def test_cache_aliases(self):
        self.assertIs(self.SE.content_sig, cache.source_sig)
        self.assertIs(self.SE.pkey, cache.project_key)

    def test_io_alias(self):
        from aicutplan import io as IO
        self.assertIs(self.SE.backup, IO.backup_copy)

    def test_media_alias_in_qc_visual(self):
        try:
            qv = importlib.import_module("qc_visual")
        except ImportError:
            self.skipTest("Pillow 未安装，跳过 qc_visual 别名检查")
        self.assertIs(qv.ffmpeg_bin, media.ffmpeg_bin)

    def test_lint_is_a_thin_delegate(self):
        """cf_pipeline.lint_narration 与 contracts 对同一工程应给出相同结论。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "proj"
            (root / "json").mkdir(parents=True)
            (root / "tts").mkdir()
            (root / "project.json").write_text("{}", encoding="utf-8")
            w = {"new_start": 0.0, "new_end": 3.0, "src_start": 100.0,
                 "src_end": 103.0, "dur": 3.0, "tier": "line"}
            dump_json(root / "json" / "cutplan.json", {"windows": [w]})
            dump_json(root / "json" / "narration.json", {
                "items": [{"i": 0, "t0": 0.0, "dur": 2.0,
                           "text": "七个字左右", "beat": 0}], "stats": {}})
            dump_json(root / "json" / "align.json",
                      {"sent_win_idx": {"0": [0, 0]}})
            self.assertEqual(self.CF.lint_narration(root),
                             contracts.lint_narration(root))


if __name__ == "__main__":
    unittest.main()
