# -*- coding: utf-8 -*-
"""v0.10 配方标定回归：planner 的 11 字短句 / 1.32s 刀 / 原声块 / 六段式。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aicutplan import contracts as CT
from aicutplan.batch import planner as PL


def _spec() -> dict:
    return {
        "title": "配方夹具",
        "hook": [900, 916],
        "hook_lines": ["男人有门逆天手艺", "随手一碰强化万物", "一把小石子扔出去"],
        "tail": [1300, 1310],
        "span": 10.0,
        "chunks": [
            [2, 12, ["女孩关了整两年", "而今天终于出门", "结果连光都怕"], "回跳"],
            [50, 60, ["龙炎扑面而来", "他却没退半步", "原来衣服扛住了"], "危机"],
            [200, 202.5, [], "原声"],
            [300, 332, [], "大鱼"],
            [900, 916, ["踩上去比铁蒺藜狠", "投出去当场炸开", "而六人不敢上前"], "撒手"],
        ],
    }


class Fixture(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "EP99"
        (self.root / "json").mkdir(parents=True)
        cfg = {"workdir": str(self.root),
               "recap": {"src_dur": 1400.0, "valid": "0-1400", "hook_clip": None}}
        (self.root / "project.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(self.td.cleanup)

    def _plan(self, spec=None):
        return PL.plan(self.root, PL.load_spec_from_dict(spec or _spec()))


class TestBenchmarkPlanner(Fixture):
    def test_constants_match_benchmark(self):
        self.assertAlmostEqual(PL.RATE, 7.17)
        self.assertAlmostEqual(PL.WIN_TARGET, 1.32)
        self.assertLessEqual(PL.MAX_WIN, 2.4)

    def test_load_spec_accepts_raw_and_label(self):
        s = PL.load_spec_from_dict(_spec())
        self.assertEqual(len(s["chunks"]), 5)
        self.assertEqual(s["chunks"][2][2], [])
        self.assertEqual(s["chunks"][2][3], "原声")

    def test_raw_chunks_are_original_and_cut_at_132(self):
        self._plan()
        wins = json.loads((self.root / "json/cutplan.json").read_text(encoding="utf-8"))["windows"]
        raw = [w for w in wins if w.get("ost_mode") == "original"]
        self.assertTrue(raw, "原声段必须产出原声窗口")
        self.assertTrue(all(w["tier"] == "hold_hl" for w in raw))
        big = [w for w in raw if w["label"] == "大鱼"]
        self.assertTrue(big)
        self.assertAlmostEqual(sum(w["dur"] for w in big), 32.0, delta=0.01)
        self.assertTrue(all(abs(w["dur"] - 32 / len(big)) < 0.01 for w in big))
        self.assertLessEqual(max(w["dur"] for w in big), 1.5)

    def test_hook_replay_chunk_not_trimmed(self):
        self._plan()
        wins = json.loads((self.root / "json/cutplan.json").read_text(encoding="utf-8"))["windows"]
        replay = [w for w in wins if w["label"] == "撒手" and w["src_start"] >= 900
                  and w.get("ost_mode") != "original"]
        self.assertTrue(replay)
        self.assertAlmostEqual(sum(w["dur"] for w in replay), 16.0, delta=0.01)

    def test_six_beat_labels_written(self):
        self._plan()
        story = json.loads((self.root / "json/story_plan.json").read_text(encoding="utf-8"))
        labels = {b["type"] for b in story["blocks"]}
        self.assertTrue({"钩子", "回跳", "危机", "撒手", "大鱼"} <= labels)
        wins = json.loads((self.root / "json/cutplan.json").read_text(encoding="utf-8"))["windows"]
        self.assertTrue(any("连续未裁切" in w["reason"] for w in wins))

    def test_contract_still_holds(self):
        self._plan()
        wins = json.loads((self.root / "json/cutplan.json").read_text(encoding="utf-8"))["windows"]
        sents = json.loads((self.root / "sents.json").read_text(encoding="utf-8"))
        cfg = json.loads((self.root / "project.json").read_text(encoding="utf-8"))
        errors, _, stats = CT.check_sentences_contract(sents, wins, cfg)
        self.assertEqual(errors, [])
        self.assertEqual(stats["src_checked"], len(sents))
        self.assertGreaterEqual(stats["hook_sents"], 2)
        self.assertEqual(cfg["recap"]["hook_clip"], [900, 916])

    def test_over_budget_sentence_is_reported(self):
        s = _spec()
        long = "这是一个明显超过十六秒槽位容量的超长句子" * 3
        s["hook_lines"] = [long, long]
        st = self._plan(s)
        self.assertGreater(st["over"], 0)


if __name__ == "__main__":
    unittest.main()
