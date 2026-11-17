# -*- coding: utf-8 -*-
"""跨集阶段汇总：指标、必看清单、可复现抽样。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aicutplan import review as RV


def _w(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _project(base: Path, name: str, *, hook=(10.0, 12.0), texts=None) -> Path:
    root = base / name
    texts = texts or ["钩子句", "块一首句", "块一第二句", "块二首句"]
    _w(root / "project.json", {"name": name, "recap": {"hook_clip": list(hook)}})
    _w(root / "json" / "cutplan.json", {"windows": [
        {"src_start": 10.0, "src_end": 12.0, "tier": "line", "beat": 0},
        {"src_start": 12.0, "src_end": 14.0, "tier": "line", "beat": 1},
        {"src_start": 14.0, "src_end": 16.0, "tier": "line", "beat": 1},
        {"src_start": 16.0, "src_end": 18.0, "tier": "line", "beat": 2},
    ]})
    _w(root / "json" / "cutplan_meta.json",
       {"meta": [{"beat": 0}, {"beat": 1}, {"beat": 1}, {"beat": 2}]})
    _w(root / "json" / "story_plan.json", {"blocks": [
        {"label": "开场", "type": "hook", "t0": 0, "t1": 2, "target": 4},
        {"label": "发展", "type": "body", "t0": 2, "t1": 16, "target": 120}]})
    items = [{"i": i, "t0": i * 2.0, "dur": 2.0, "text": t} for i, t in enumerate(texts)]
    _w(root / "json" / "narration.json", {"items": items, "stats": {"speech_sec": 8.0}})
    _w(root / "json" / "align.json",
       {"sent_win_idx": {str(i): [i, i] for i in range(len(texts))}})
    # qc_report：第三句压缩后仍溢出 0.3s
    _w(root / "json" / "qc_report.json", [
        {"i": i, "slot": 2.0, "speech": 2.0, "ratio": 1.0, "action": "ok"}
        for i in range(len(texts))])
    rows = json.loads((root / "json" / "qc_report.json").read_text(encoding="utf-8"))
    rows[2]["speech"] = 2.3
    rows[2]["ratio"] = 1.15
    _w(root / "json" / "qc_report.json", rows)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / "out" / "03-rapid-narration.mp4").write_bytes(b"X" * 128)
    return root


class NarrationReviewTests(unittest.TestCase):
    def test_must_see_covers_hook_block_first_and_overslot(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(Path(td), "EP01")
            d = RV.summarize_narration(root, ratio=0.0, seed=1)
            self.assertEqual(d["hook_sents"], 1)          # 句0 落钩子窗
            self.assertIn(0, d["must_see"])               # 钩子句
            self.assertIn(1, d["must_see"])               # 块1 首句
            self.assertIn(3, d["must_see"])               # 块2 首句
            self.assertEqual(d["over_slot"].get(2), 0.3)  # 压缩后仍溢出
            self.assertIn(2, d["must_see"])

    def test_sample_is_reproducible_with_seed(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(Path(td), "EP01", texts=[f"句{i}" for i in range(40)])
            a = RV.summarize_narration(root, ratio=0.2, seed=7)
            b = RV.summarize_narration(root, ratio=0.2, seed=7)
            c = RV.summarize_narration(root, ratio=0.2, seed=8)
            self.assertEqual(a["sample"], b["sample"])
            self.assertNotEqual(a["sample"], c["sample"])

    def test_sample_excludes_must_see(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(Path(td), "EP01", texts=[f"句{i}" for i in range(30)])
            d = RV.summarize_narration(root, ratio=0.3, seed=3)
            self.assertFalse(set(d["sample"]) & set(d["must_see"]))


class WriteReviewTests(unittest.TestCase):
    def test_writes_index_and_per_episode(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            p1 = _project(base, "EP01")
            p2 = _project(base, "EP02")
            out = base / "_review"
            res = RV.write_review([p1, p2], "narration", out=out, ratio=0.1, seed=11)
            self.assertTrue((out / "index.md").exists())
            self.assertTrue((out / "EP01.md").exists())
            self.assertTrue((out / "EP02.md").exists())
            text = (out / "index.md").read_text(encoding="utf-8")
            self.assertIn("EP01", text)
            self.assertIn("必看", text)
            self.assertEqual(len(res["summaries"]), 2)

    def test_unknown_stage_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(Path(td), "EP01")
            with self.assertRaises(ValueError):
                RV.write_review([root], "nope")

    def test_publish_stage_reports_qc(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(Path(td), "EP01")
            _w(root / "json" / "qc_visual.json",
               [{"i": 0, "match": 1, "judged": True}, {"i": 1, "match": 0, "judged": True}])
            d = RV.summarize_publish(root)
            self.assertEqual(d["qc"]["judged"], 2)
            self.assertEqual(d["qc"]["mismatch"], [1])


if __name__ == "__main__":
    unittest.main()
