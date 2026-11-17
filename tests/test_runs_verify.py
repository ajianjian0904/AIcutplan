# -*- coding: utf-8 -*-
"""runs 归档的不可变性：manifest 的 final 段 + verify --all 能发现「历史被覆盖」。

对应事故：r7 原地写 out/03（ffmpeg -y），而 snapshot 用硬链接归档，
一次重渲染把所有历史 run 的 final.mp4 一起改写 —— 指纹全变、无人发现。
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from aicutplan import cli
from aicutplan import workspace as WS


def _project(td: str) -> Path:
    root = Path(td) / "EP01"
    (root / "out").mkdir(parents=True)
    (root / "json").mkdir()
    (root / "project.json").write_text(json.dumps({"name": "测试"}, ensure_ascii=False),
                                       encoding="utf-8")
    (root / "out" / "03-rapid-narration.mp4").write_bytes(b"V1" * 1000)
    (root / "json" / "narration.json").write_text("{}", encoding="utf-8")
    return root


def _runs(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli._runs(argv)
    return code, buf.getvalue()


class ManifestFinalTests(unittest.TestCase):
    def test_snapshot_records_final_sig_and_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            m = WS.snapshot(root / "project.json", label="v1")
            self.assertEqual(m["final"]["path"], "final.mp4")
            self.assertEqual(m["final"]["bytes"], 2000)
            self.assertTrue(m["final"]["sig"])
            self.assertEqual(m["final"]["sig"], m["artifacts"]["final.mp4"]["sig"])

    def test_latest_carries_final_sig(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            m = WS.snapshot(root / "project.json", label="v1")
            latest = WS.latest(root / "project.json", verify=False)
            self.assertEqual(latest["final_sig"], m["final"]["sig"])


class VerifyAllTests(unittest.TestCase):
    def test_all_reports_healthy_archives(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            WS.snapshot(root / "project.json", label="v1")
            code, out = _runs(["verify", "--project", str(root / "project.json"), "--all"])
            self.assertEqual(code, 0)
            self.assertIn("指纹一致", out)

    def test_all_detects_in_place_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            first = WS.snapshot(root / "project.json", label="v1")
            # 模拟 r7 的原地写：同一个 inode 被截断重写 → 硬链接归档的 final 跟着变
            (root / "out" / "03-rapid-narration.mp4").write_bytes(b"V2" * 1000)
            code, out = _runs(["verify", "--project", str(root / "project.json"), "--all"])
            self.assertEqual(code, 1)
            self.assertIn(first["run_id"], out)
            self.assertIn("final.mp4", out)

    def test_run_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            m = WS.snapshot(root / "project.json", label="v1")
            code, out = _runs(["verify", "--project", str(root / "project.json"),
                               "--run", m["run_id"]])
            self.assertEqual(code, 0)
            self.assertIn(m["run_id"], out)

    def test_unknown_run_returns_2(self):
        with tempfile.TemporaryDirectory() as td:
            root = _project(td)
            WS.snapshot(root / "project.json", label="v1")
            code, _out = _runs(["verify", "--project", str(root / "project.json"),
                                "--run", "r999_nope"])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
