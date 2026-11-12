# -*- coding: utf-8 -*-
"""L0·aicutplan.media 单元测试：ffmpeg 定位优先级（不实际调用 ffmpeg）。"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from aicutplan import media as ME


class BinaryLookupTests(unittest.TestCase):
    def test_returns_nonempty_string(self):
        self.assertIsInstance(ME.ffmpeg_bin(), str)
        self.assertTrue(ME.ffmpeg_bin())
        self.assertIsInstance(ME.ffprobe_bin(), str)
        self.assertTrue(ME.ffprobe_bin())

    def test_env_override_takes_priority(self):
        with patch.dict(os.environ, {"NARR_FFMPEG": "/custom/ffmpeg"}):
            self.assertEqual(ME.ffmpeg_bin(), "/custom/ffmpeg")

    def test_second_env_name_supported(self):
        with patch.dict(os.environ, {"AICUTPLAN_FFMPEG": "/a/ffmpeg",
                                    "NARR_FFMPEG": ""}):
            self.assertEqual(ME.ffmpeg_bin(), "/a/ffmpeg")

    def test_ffprobe_env_override(self):
        with patch.dict(os.environ, {"AICUTPLAN_FFPROBE": "/a/ffprobe"}):
            self.assertEqual(ME.ffprobe_bin(), "/a/ffprobe")

    def test_image_codecs_are_excluded_from_text_streams(self):
        from aicutplan.subtitles import IMAGE_CODECS, is_text_stream
        for codec in IMAGE_CODECS:
            with self.subTest(codec=codec):
                self.assertFalse(is_text_stream({"codec": codec}))


if __name__ == "__main__":
    unittest.main()
