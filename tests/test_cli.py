# -*- coding: utf-8 -*-
"""L2·aicutplan.cli 单元测试：统一门面的命令分发与工程体检。

只测不启子进程的分支（version / check / lint / 参数校验）；
run/serve 等会转发到阶段脚本，属于集成链路，不在单测范围。
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from aicutplan import __version__
from aicutplan import cli as CLI
from aicutplan.io import dump_json
from aicutplan.paths import repo_root


def _win(a, z):
    return {"new_start": a, "new_end": z, "src_start": a + 100,
            "src_end": z + 100, "dur": z - a, "tier": "line"}


def _project(td, *, broken=False):
    root = Path(td) / "proj"
    (root / "json").mkdir(parents=True)
    (root / "tts").mkdir()
    (root / "project.json").write_text("{}", encoding="utf-8")
    text = "很长的旁白" * 6 if broken else "七个字左右"
    dump_json(root / "json" / "cutplan.json", {"windows": [_win(0, 3)]})
    dump_json(root / "json" / "narration.json", {
        "items": [{"i": 0, "t0": 0.0, "dur": 2.0, "text": text, "beat": 0}],
        "stats": {}})
    dump_json(root / "json" / "align.json", {"sent_win_idx": {"0": [0, 0]}})
    dump_json(root / "json" / "tts-result.json", {
        "items": [{"i": 0, "text": text, "tts_dur": 9.0 if broken else 2.0}]})
    (root / "tts" / "000.mp3").write_bytes(b"audio")
    return root / "project.json"


class VersionTests(unittest.TestCase):
    def test_version_subcommand(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = CLI.main(["version"])
        self.assertEqual(rc, 0)
        self.assertIn(__version__, buf.getvalue())

    def test_version_flag(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                CLI.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                CLI.main([])
        self.assertEqual(ctx.exception.code, 0)


class CheckTests(unittest.TestCase):
    def test_check_clean_project(self):
        with tempfile.TemporaryDirectory() as td:
            pj = _project(td)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = CLI.main(["check", "--project", str(pj)])
            self.assertEqual(rc, 0)
            self.assertIn("全部通过", buf.getvalue())
            self.assertIn("指标", buf.getvalue())

    def test_lint_does_not_print_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            pj = _project(td)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = CLI.main(["lint", "--project", str(pj)])
            self.assertEqual(rc, 0)
            self.assertNotIn("指标", buf.getvalue())

    def test_check_reports_broken_project(self):
        with tempfile.TemporaryDirectory() as td:
            pj = _project(td, broken=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = CLI.main(["check", "--project", str(pj)])
            self.assertEqual(rc, 1)
            self.assertIn("槽位", buf.getvalue())

    def test_check_requires_project(self):
        self.assertEqual(CLI.main(["check"]), 2)

    def test_check_missing_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            pj = Path(td) / "proj" / "project.json"
            pj.parent.mkdir(parents=True)
            pj.write_text("{}", encoding="utf-8")
            self.assertEqual(CLI.main(["check", "--project", str(pj)]), 2)

    def test_unknown_command_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                CLI.main(["not-a-command"])
        self.assertEqual(ctx.exception.code, 2)


class DelegateMappingTests(unittest.TestCase):
    def test_all_delegated_scripts_exist(self):
        for name, target in CLI.SCRIPTS.items():
            with self.subTest(name=name):
                self.assertTrue(CLI.script(target).exists(),
                                f"{name} → {target} 不存在")

    def test_direct_commands_have_no_script(self):
        for name in CLI.DIRECT:
            self.assertNotIn(name, CLI.SCRIPTS)


if __name__ == "__main__":
    unittest.main()
