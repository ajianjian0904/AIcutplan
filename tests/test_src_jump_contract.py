# -*- coding: utf-8 -*-
"""L0·src 契约：跨源片跳转的句子不能按「区间倒置」判错。

背景：剪辑会把后段镜头提前用（反转插叙、钩子复用），一句可能跨两段源片，
它的 ``src`` 首端晚于末端是合法的。历史稿升级（EP01 句9：窗 18-21，
源 143.25 → 118.25）就撞在这里。
"""
from __future__ import annotations

import unittest

from aicutplan.contracts import _windows_jump, check_sentences_contract


def _win(i: int, src_a: float, src_b: float) -> dict:
    return {"new_start": i * 2.0, "new_end": i * 2.0 + 2.0,
            "src_start": src_a, "src_end": src_b,
            "tier": "line", "ost_mode": "narration"}


class WindowsJumpTests(unittest.TestCase):
    def test_detects_backward_jump(self):
        wins = [_win(0, 110, 112), _win(1, 112, 114), _win(2, 140, 142), _win(3, 114, 116)]
        self.assertTrue(_windows_jump(wins, 2, 3))
        self.assertFalse(_windows_jump(wins, 0, 1))

    def test_forward_only_is_not_a_jump(self):
        wins = [_win(0, 110, 112), _win(1, 130, 132)]
        self.assertFalse(_windows_jump(wins, 0, 1))


class JumpSpanningSentenceTests(unittest.TestCase):
    def setUp(self):
        self.wins = [_win(0, 110, 112), _win(1, 112, 114),
                     _win(2, 140, 142), _win(3, 114, 116)]

    def test_inverted_src_over_jump_is_warning(self):
        sents = [{"ws": 2, "we": 3, "text": "他当场被宣布开除，理由很粗暴", "src": [140.0, 116.0]}]
        errors, warnings, stats = check_sentences_contract(sents, self.wins, {})
        self.assertEqual(errors, [])
        self.assertEqual(stats["src_checked"], 1)
        self.assertTrue(any("跨源片跳转" in w for w in warnings), warnings)

    def test_inverted_src_over_monotonic_windows_still_error(self):
        sents = [{"ws": 0, "we": 1, "text": "测试", "src": [114.0, 110.0]}]
        errors, _warnings, _stats = check_sentences_contract(sents, self.wins, {})
        self.assertTrue(any("src 区间倒置" in e for e in errors), errors)

    def test_jump_sentence_with_wrong_endpoints_is_error(self):
        # 端点对不上窗口（这不是均匀分配，而是抄错了窗口范围）
        sents = [{"ws": 2, "we": 3, "text": "测试", "src": [139.0, 116.0]}]
        errors, _warnings, _stats = check_sentences_contract(sents, self.wins, {})
        self.assertTrue(any("跨源片跳转" in e and "不符" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
