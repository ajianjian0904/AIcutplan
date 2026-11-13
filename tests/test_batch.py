# -*- coding: utf-8 -*-
"""批量出片唯一入口 / 断点续跑前置校验 / 质检归档状态的回归测试。

标准库 unittest + tempfile，零 ffmpeg / 零网络 / 零 API / 零模型。
重点验证 P1 要堵的洞：EP03 用 --from-step 跳过 plan 导致 hook_clip=None
却无人报错；以及「编排 → 语义契约」确实串成一条链。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aicutplan import contracts as CT
from aicutplan import workspace as WS
from aicutplan.batch import pipeline as BP
from aicutplan.batch import planner as PL

REPO = Path(__file__).resolve().parents[1]

SPEC = {
    "title": "测试集",
    "hook": [100.0, 106.0],
    "hook_lines": ["钩子第一句讲的正是这段爽点", "钩子第二句把悬念抛出来"],
    "tail": [280.0, 290.0],
    "span": 8.0,
    "chunks": [
        [10, 26, ["第一段的句子一", "第一段的句子二"]],
        [30, 46, ["第二段的句子一", "第二段的句子二"]],
    ],
}


def _mini(root: Path, *, with_clip: bool = True) -> None:
    for sub in ("json", "out", "tts", "runs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    recap = {"src_dur": 300.0, "valid": "0-300"}
    if with_clip:
        recap["hook_clip"] = [100.0, 106.0]
    (root / "project.json").write_text(
        json.dumps({"name": "测试", "workdir": str(root), "recap": recap},
                   ensure_ascii=False), encoding="utf-8")
    # 作品库定位所需（locate 在非作品库目录下会返回 None，但 fixture 给全更稳）
    (root.parent / "series.json").write_text(
        json.dumps({"name": "测试动漫"}, ensure_ascii=False), encoding="utf-8")


def _planned(root: Path) -> dict:
    _mini(root)
    (root / "director.json").write_text(json.dumps(SPEC, ensure_ascii=False), encoding="utf-8")
    return PL.plan(root, PL.load_spec(root / "director.json"))


class TestPlanner(unittest.TestCase):
    def test_plan_generates_contract_valid_script(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            st = _planned(root)
            for rel in ("sents.json", "json/cutplan.json", "json/cutplan_meta.json",
                        "json/story_plan.json"):
                self.assertTrue((root / rel).exists(), rel)
            sents = json.loads((root / "sents.json").read_text(encoding="utf-8"))
            wins = json.loads((root / "json/cutplan.json").read_text(encoding="utf-8"))["windows"]
            cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
            self.assertTrue(all(s.get("src") for s in sents), "planner 必须写 src")
            errs, _, stats = CT.check_sentences_contract(sents, wins, cfg)
            self.assertEqual(errs, [], errs)
            self.assertEqual(stats["src_checked"], len(sents))
            self.assertGreaterEqual(stats["hook_sents"], 2)
            self.assertEqual(cfg["recap"]["hook_clip"], [100.0, 106.0])
            self.assertGreater(st["windows"], 0)
            self.assertEqual(st["over"], 0)

    def test_plan_rewrites_hook_clip(self):
        """plan 是写入 hook_clip 的唯一位置，必须覆盖旧值。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            _mini(root, with_clip=False)
            PL.plan(root, dict(SPEC))
            cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["recap"]["hook_clip"], [100.0, 106.0])

    def test_load_spec_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "d.json"
            for bad in ({k: v for k, v in SPEC.items() if k != "hook"},
                        dict(SPEC, hook=[10, 5]),
                        dict(SPEC, hook_lines=["只有一句"]),
                        dict(SPEC, chunks=[]),
                        dict(SPEC, chunks=[[10, 5, ["x"]]])):
                p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ValueError):
                    PL.load_spec(p)

    def test_load_spec_reads_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "d.json"
            p.write_text(json.dumps(SPEC, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(PL.load_spec(p)["title"], "测试集")


class TestPreflightResume(unittest.TestCase):
    def test_plan_step_skips_checks(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(BP.preflight_resume(Path(td), "plan"), [])

    def test_normal_resume_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            _planned(root)
            # 从 narrate 续跑 = cut 已完成，上游画面/底座必须存在
            (root / "out" / "02-video-rapid.mp4").write_bytes(b"FAKE")
            (root / "audio").mkdir(exist_ok=True)
            (root / "audio" / "base-rapid.wav").write_bytes(b"FAKE")
            self.assertEqual(BP.preflight_resume(root, "narrate"), [])

    def test_missing_hook_clip_is_blocked(self):
        """EP03 事故回归：--from-step 跳过 plan，钩子退化却无人报错。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            _planned(root)
            cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
            cfg["recap"].pop("hook_clip")
            (root / "project.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            issues = BP.preflight_resume(root, "narrate")
            self.assertTrue(any("hook_clip" in x for x in issues), issues)

    def test_missing_artifacts_are_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            _planned(root)
            issues = BP.preflight_resume(root, "mux")
            self.assertTrue(any("mix-rapid.wav" in x for x in issues), issues)
            self.assertTrue(any("02-video-rapid.mp4" in x for x in issues), issues)

    def test_uniform_script_is_blocked_on_resume(self):
        """续跑时若句子稿被换成均匀分配稿，前置校验必须拦下。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            _planned(root)
            sents = json.loads((root / "sents.json").read_text(encoding="utf-8"))
            for k, s in enumerate(sents):
                s["src"] = [float(2 + k * 20), float(12 + k * 20)]
            (root / "sents.json").write_text(json.dumps(sents, ensure_ascii=False), encoding="utf-8")
            issues = BP.preflight_resume(root, "narrate")
            self.assertTrue(any("不是同一段" in x or "契约" in x for x in issues), issues)

    def test_missing_sents_is_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            _planned(root)
            (root / "sents.json").unlink()
            issues = BP.preflight_resume(root, "narrate")
            self.assertTrue(any("sents.json" in x for x in issues), issues)


class TestBatchCli(unittest.TestCase):
    def test_dry_run_prints_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            _mini(root)
            spec = Path(td) / "director.json"
            spec.write_text(json.dumps(SPEC, ensure_ascii=False), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "aicutplan", "batch", "--project",
                 str(root / "project.json"), "--script", str(spec), "--dry-run"],
                cwd=str(REPO), capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("cut.py", r.stdout)
            self.assertFalse((root / "sents.json").exists(), "dry-run 不应写产物")

    def test_resume_without_hook_clip_exits_6(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "EP01"
            _planned(root)
            cfg = json.loads((root / "project.json").read_text(encoding="utf-8"))
            cfg["recap"].pop("hook_clip")
            (root / "project.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "aicutplan", "batch", "--project",
                 str(root / "project.json"), "--from-step", "narrate", "--dry-run"],
                cwd=str(REPO), capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 6, r.stderr)
            self.assertIn("hook_clip", r.stderr)


class TestQcAudit(unittest.TestCase):
    def _root(self, td: str) -> Path:
        root = Path(td) / "EP01"
        _mini(root)
        (root / "out" / "03-rapid-narration.mp4").write_bytes(b"FAKE-MP4")
        return root

    def test_qc_status_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(WS.qc_status(self._root(td))["state"], "unverified")

    def test_qc_status_partial_and_verified(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            p = root / "json" / "qc_visual.json"
            p.write_text(json.dumps([{"i": 0, "judged": True}, {"i": 1, "judged": False}]),
                         encoding="utf-8")
            self.assertEqual(WS.qc_status(root)["state"], "partial")
            p.write_text(json.dumps([{"i": 0, "judged": True, "match": 1},
                                     {"i": 1, "judged": True, "match": 0}]), encoding="utf-8")
            s = WS.qc_status(root)
            self.assertEqual(s["state"], "verified")
            self.assertEqual(s["mismatch"], 1)

    def test_require_qc_blocks_archive(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            with self.assertRaises(RuntimeError):
                WS.snapshot(root, require_qc=True)
            self.assertFalse((root / "runs" / "latest.json").exists(),
                             "被拒绝的归档不能留下 latest 指针")
            m = WS.snapshot(root, require_qc=False)
            self.assertEqual(m["qc"]["state"], "unverified")

    def test_require_qc_passes_after_merge(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            (root / "json" / "qc_visual.json").write_text(
                json.dumps([{"i": 0, "judged": True, "match": 1}]), encoding="utf-8")
            m = WS.snapshot(root, require_qc=True)
            self.assertEqual(m["qc"]["state"], "verified")
            self.assertEqual(WS.latest(root, verify=False).get("qc"), "verified")


if __name__ == "__main__":
    unittest.main()
