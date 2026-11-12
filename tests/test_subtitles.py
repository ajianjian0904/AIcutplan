# -*- coding: utf-8 -*-
"""L0·aicutplan.subtitles 单元测试：SRT/ASS 清洗、解析、合并、语言判定。

零 ffmpeg / 零网络 / 零模型，全部用 tempfile 造字幕文本。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicutplan import subtitles as SUB


class CleanTextTests(unittest.TestCase):
    def test_removes_ass_and_html_tags(self):
        t, sp = SUB.clean_text(r"{\pos(10,20)}强<b>化</b>成功")
        self.assertEqual(t, "强化成功")
        self.assertEqual(sp, "")

    def test_merges_newlines(self):
        t, _ = SUB.clean_text("第一行\\N第二行\\n第三行")
        self.assertEqual(t, "第一行 第二行 第三行")

    def test_extracts_speaker_but_keeps_text(self):
        t, sp = SUB.clean_text("（バリオス）追放？")
        self.assertEqual(sp, "バリオス")
        self.assertEqual(t, "（バリオス）追放？")

    def test_ascii_parenthesis_speaker(self):
        t, sp = SUB.clean_text("(妮娜) 小心！")
        self.assertEqual(sp, "妮娜")
        self.assertIn("小心", t)

    def test_empty_input(self):
        self.assertEqual(SUB.clean_text("  "), ("", ""))


class ParseSrtTests(unittest.TestCase):
    def _parse(self, text: str) -> list[dict]:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.srt"
            p.write_text(text, encoding="utf-8-sig")
            return SUB.parse_srt(p)

    def test_standard_two_blocks_sorted(self):
        srt = ("1\n00:00:03,170 --> 00:00:05,470\n第一条台词\n\n"
               "2\n00:00:01,000 --> 00:00:02,500\n第二条台词\n")
        rows = self._parse(srt)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["start"], 1.0)
        self.assertEqual(rows[1]["end"], 5.47)
        self.assertEqual(rows[0]["text"], "第二条台词")

    def test_dot_millisecond_and_missing_index(self):
        rows = self._parse("00:01:02.250 --> 00:01:03.500\n无编号也行\n")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["start"], 62.25)
        self.assertAlmostEqual(rows[0]["end"], 63.5)

    def test_multiline_body_and_speaker(self):
        rows = self._parse("1\n00:00:00,000 --> 00:00:02,000\n（バリオス）\n追放？\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["speaker"], "バリオス")
        self.assertIn("追放", rows[0]["text"])

    def test_skips_invalid_blocks(self):
        srt = ("1\n00:00:05,000 --> 00:00:04,000\n结束早于开始\n\n"
               "2\n00:00:06,000 --> 00:00:07,000\n有效\n\n"
               "3\n没有时间行\n缺时间\n")
        rows = self._parse(srt)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "有效")

    def test_empty_file(self):
        self.assertEqual(self._parse(""), [])


class MergeSameTimeTests(unittest.TestCase):
    def test_merges_ass_split_lines(self):
        rows = [
            {"start": 1.0, "end": 2.0, "text": "ああ うちのギルドで", "speaker": ""},
            {"start": 1.0, "end": 2.0, "text": "いらねえやつをな。", "speaker": "バリオス"},
        ]
        out = SUB.merge_same_time(rows)
        self.assertEqual(len(out), 1)
        self.assertIn("ああ", out[0]["text"])
        self.assertIn("いらねえ", out[0]["text"])
        self.assertEqual(out[0]["speaker"], "バリオス")

    def test_keeps_different_times(self):
        rows = [
            {"start": 1.0, "end": 2.0, "text": "A", "speaker": ""},
            {"start": 2.5, "end": 3.0, "text": "B", "speaker": ""},
        ]
        self.assertEqual(len(SUB.merge_same_time(rows)), 2)

    def test_does_not_mutate_input(self):
        rows = [{"start": 1.0, "end": 2.0, "text": "A", "speaker": ""},
                {"start": 1.0, "end": 2.0, "text": "B", "speaker": ""}]
        SUB.merge_same_time(rows)
        self.assertEqual(rows[0]["text"], "A")


class LangTests(unittest.TestCase):
    def test_sniff(self):
        self.assertEqual(SUB.sniff_lang([{"text": "追放された"}]), "ja")
        self.assertEqual(SUB.sniff_lang([{"text": "Hello there"}]), "en")
        self.assertEqual(SUB.sniff_lang([{"text": "被驱逐的强化师"}]), "zh")
        self.assertEqual(SUB.sniff_lang([{"text": "12345"}]), "unknown")

    def test_norm(self):
        self.assertEqual(SUB.norm(" 追放！？ "), "追放")
        self.assertEqual(SUB.norm("Hello, World!"), "helloworld")

    def test_text_vs_image_stream(self):
        self.assertTrue(SUB.is_text_stream({"codec": "subrip"}))
        self.assertTrue(SUB.is_text_stream({"codec": "ass"}))
        self.assertFalse(SUB.is_text_stream({"codec": "hdmv_pgs_subtitle"}))


if __name__ == "__main__":
    unittest.main()
