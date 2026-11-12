# -*- coding: utf-8 -*-
"""L0·aicutplan.planning 单元测试：钩子连续爽点选段、权重、连续性验收。"""
from __future__ import annotations

import unittest

from aicutplan import planning as PL


def _shot(a, b, desc="", depth="", obj=""):
    return {"a": a, "b": b, "desc": desc, "depth": depth, "obj": obj}


class HookScoreTests(unittest.TestCase):
    def test_action_keyword_scores_higher_than_setting(self):
        setting = PL.hook_score(_shot(0, 2, depth="紫光点数"), {})
        action = PL.hook_score(_shot(0, 2, depth="挥剑轰出巨坑"), {})
        self.assertAlmostEqual(setting, 2.0)
        self.assertAlmostEqual(action, 3.0)

    def test_bonus_for_climax_event_and_penalty_for_long_shot(self):
        base = PL.hook_score(_shot(0, 2, desc="挥剑"), {})
        with_event = PL.hook_score(_shot(0, 2, desc="挥剑"), {"type": "爽点"})
        self.assertAlmostEqual(with_event - base, 1.5)
        long_shot = PL.hook_score(_shot(0, 9, desc="挥剑"), {})
        self.assertAlmostEqual(long_shot - base, -2.0)


class PickHookClipTests(unittest.TestCase):
    def test_override_wins_and_reports_event(self):
        events = [{"t0": 800.0, "t1": 830.0, "event": "围攻"}]
        a, z, gi = PL.pick_hook_clip([], events, 10.8, [809.68, 820.47])
        self.assertEqual((a, z, gi), (809.68, 820.47, 0))

    def test_override_invalid_falls_back_to_scan(self):
        shots = [_shot(0, 20, desc="挥剑轰坑")]
        a, z, _ = PL.pick_hook_clip(shots, [], 4.0, [10.0, 5.0])
        self.assertGreater(z, a)
        self.assertGreater(a, 0)

    def test_picks_densest_region(self):
        # 0-10s 平淡，20-30s 全是爽点镜头；budget=6 时应落在 20s 之后
        shots = [_shot(float(i), float(i) + 1, desc="空镜") for i in range(10)]
        shots += [_shot(20.0 + i, 21.0 + i, desc="挥剑轰出巨坑", depth="惨叫")
                  for i in range(10)]
        a, z, _ = PL.pick_hook_clip(shots, [], 6.0)
        self.assertGreaterEqual(a, 20.0)
        self.assertAlmostEqual(z - a, 6.0)

    def test_empty_shots_returns_zero(self):
        self.assertEqual(PL.pick_hook_clip([], [], 6.0), (0.0, 0.0, None))

    def test_budget_longer_than_source_returns_zero(self):
        shots = [_shot(0, 2, desc="挥剑")]
        self.assertEqual(PL.pick_hook_clip(shots, [], 99.0), (0.0, 0.0, None))


class WeightTests(unittest.TestCase):
    def test_func_overrides_type(self):
        e = {"type": "铺垫"}
        self.assertAlmostEqual(PL.weight(e, {"0": {"func": "爆发"}}, 0), 3.0)
        self.assertAlmostEqual(PL.weight(e, {}, 0), 1.0)

    def test_causal_link_bonus(self):
        base = PL.weight({"type": "冲突"}, {}, 3)
        linked = PL.weight({"type": "冲突"}, {"3": {"cause": [1], "result": [5]}}, 3)
        self.assertAlmostEqual(linked / base, 1.15)


class ReasonTests(unittest.TestCase):
    def test_plain_and_linked(self):
        e = {"type": "铺垫", "event": "少年被公会开除"}
        self.assertEqual(PL.reason(e, {}, 0), "铺垫节点：少年被公会开除")
        linked = PL.reason(e, {"0": {"func": "反转", "cause": [7]}}, 0)
        self.assertIn("反转节点", linked)
        self.assertIn("承接事件7", linked)


class ContinuousTests(unittest.TestCase):
    def test_all_gaps_zero(self):
        wins = [{"src_start": 809.68, "src_end": 810.76},
                {"src_start": 810.76, "src_end": 811.55},
                {"src_start": 811.55, "src_end": 812.34}]
        ok, gaps = PL.is_continuous(wins)
        self.assertTrue(ok)
        self.assertEqual(gaps, [0.0, 0.0])

    def test_jump_detected(self):
        wins = [{"src_start": 772.15, "src_end": 773.35},
                {"src_start": 809.68, "src_end": 810.88}]
        ok, gaps = PL.is_continuous(wins)
        self.assertFalse(ok)
        self.assertGreater(gaps[0], 30)

    def test_single_window_is_continuous(self):
        self.assertTrue(PL.is_continuous([{"src_start": 0, "src_end": 1}])[0])


if __name__ == "__main__":
    unittest.main()
