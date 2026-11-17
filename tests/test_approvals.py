# -*- coding: utf-8 -*-
"""三阶段审批：签名只覆盖该阶段决策、产物一改即失效、口令把人锁在回路里。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aicutplan import approvals as AP


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _project(td: str, *, text0: str = "第一句", tiers=("hook", "line", "line")) -> Path:
    root = Path(td) / "EP01"
    _write(root / "project.json", {"name": "测试", "recap": {"hook_clip": [10.0, 20.0]}})
    _write(root / "json" / "cutplan.json", {"windows": [
        {"src_start": 10.0, "src_end": 12.0, "new_start": 0.0, "new_end": 2.0,
         "tier": tiers[0], "beat": 0},
        {"src_start": 12.0, "src_end": 14.0, "new_start": 2.0, "new_end": 4.0,
         "tier": tiers[1], "beat": 1},
        {"src_start": 14.0, "src_end": 16.0, "new_start": 4.0, "new_end": 6.0,
         "tier": tiers[2], "beat": 1},
    ]})
    _write(root / "json" / "cutplan_meta.json", {"meta": [{"beat": 0}, {"beat": 1}, {"beat": 1}]})
    _write(root / "json" / "narration.json", {"items": [
        {"i": 0, "t0": 0.0, "dur": 2.0, "text": text0},
        {"i": 1, "t0": 2.0, "dur": 2.0, "text": "第二句"},
    ], "stats": {"speech_sec": 4.0}})
    _write(root / "json" / "align.json", {"sent_win_idx": {"0": [0, 0], "1": [1, 2]}})
    _write(root / "json" / "tts-result.json", {"items": [{"i": 1, "over": 0.4}]})
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / "out" / "03-rapid-narration.mp4").write_bytes(b"FINAL" * 100)
    _write(root / "json" / "qc_visual.json", [
        {"i": 0, "match": 1, "judged": True}, {"i": 1, "match": 0, "judged": True}])
    return root


class SigTests(unittest.TestCase):
    def test_narration_sig_changes_with_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            before = AP.stage_sig(root, "narration")
            _write(root / "json" / "narration.json", {"items": [
                {"i": 0, "t0": 0.0, "dur": 2.0, "text": "改过的第一句"},
                {"i": 1, "t0": 2.0, "dur": 2.0, "text": "第二句"}], "stats": {}})
            self.assertNotEqual(before, AP.stage_sig(root, "narration"))

    def test_cutplan_sig_ignores_framesnap_times(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            before = AP.stage_sig(root, "cutplan")
            plan = json.loads((root / "json" / "cutplan.json").read_text(encoding="utf-8"))
            plan["windows"][0]["new_start"] = 0.37   # cut.py 回写帧网格时间
            _write(root / "json" / "cutplan.json", plan)
            self.assertEqual(before, AP.stage_sig(root, "cutplan"))

    def test_cutplan_sig_changes_with_src_range(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            before = AP.stage_sig(root, "cutplan")
            plan = json.loads((root / "json" / "cutplan.json").read_text(encoding="utf-8"))
            plan["windows"][1]["src_end"] = 15.5
            _write(root / "json" / "cutplan.json", plan)
            self.assertNotEqual(before, AP.stage_sig(root, "cutplan"))

    def test_publish_sig_changes_with_qc(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            before = AP.stage_sig(root, "publish")
            _write(root / "json" / "qc_visual.json", [
                {"i": 0, "match": 1, "judged": True}, {"i": 1, "match": 1, "judged": True}])
            self.assertNotEqual(before, AP.stage_sig(root, "publish"))

    def test_missing_artifacts_give_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            (root / "json" / "narration.json").unlink()
            self.assertIsNone(AP.stage_sig(root, "narration"))


class CodeFlowTests(unittest.TestCase):
    def test_full_approve_flow(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            self.assertIn("未审批", AP.require(root, "narration"))
            code, _exp = AP.issue_code(root, "narration")
            with self.assertRaises(RuntimeError):
                AP.confirm(root, "narration", "000000" if code != "000000" else "111111")
            sig = AP.confirm(root, "narration", code)
            AP.record(root, "narration", sig=sig, note="抽样 10%", sample_seed=20261118)
            self.assertIsNone(AP.require(root, "narration"))
            self.assertTrue(AP.load(root)["stages"]["narration"]["sample_seed"])

    def test_code_is_single_use(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            code, _ = AP.issue_code(root, "narration")
            AP.confirm(root, "narration", code)
            with self.assertRaises(RuntimeError):
                AP.confirm(root, "narration", code)

    def test_stale_code_rejected_after_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            code, _ = AP.issue_code(root, "narration")
            _write(root / "json" / "narration.json", {"items": [
                {"i": 0, "t0": 0.0, "dur": 2.0, "text": "发码后改了"},
                {"i": 1, "t0": 2.0, "dur": 2.0, "text": "第二句"}], "stats": {}})
            with self.assertRaises(RuntimeError) as ctx:
                AP.confirm(root, "narration", code)
            self.assertIn("变过", str(ctx.exception))

    def test_expired_code_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            code, _ = AP.issue_code(root, "narration")
            path = root / "json" / "approval_code.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["expires_at"] = 1
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                AP.confirm(root, "narration", code)
            self.assertIn("过期", str(ctx.exception))


class StatusTests(unittest.TestCase):
    def test_invalidation_after_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            code, _ = AP.issue_code(root, "narration")
            AP.record(root, "narration", sig=AP.confirm(root, "narration", code))
            self.assertTrue(AP.status(root)["narration"]["valid"])
            _write(root / "json" / "narration.json", {"items": [
                {"i": 0, "t0": 0.0, "dur": 2.0, "text": "又改了"},
                {"i": 1, "t0": 2.0, "dur": 2.0, "text": "第二句"}], "stats": {}})
            st = AP.status(root)["narration"]
            self.assertTrue(st["approved"])
            self.assertFalse(st["valid"])
            self.assertIn("失效", AP.require(root, "narration"))

    def test_unknown_stage_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            with self.assertRaises(ValueError):
                AP.stage_sig(root, "nope")


if __name__ == "__main__":
    unittest.main()
