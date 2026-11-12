# -*- coding: utf-8 -*-
"""L0·aicutplan.paths 单元测试：工程路径约定（与仓库外脚本共享的契约）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aicutplan.paths import ProjectPaths, project_root, repo_root, script


class ProjectPathsTests(unittest.TestCase):
    def _proj(self, td):
        root = Path(td) / "ep01cf"
        root.mkdir()
        pj = root / "project.json"
        pj.write_text("{}", encoding="utf-8")
        return root, pj

    def test_accepts_project_json_or_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root, pj = self._proj(td)
            a, b = ProjectPaths(pj), ProjectPaths(root)
            self.assertEqual(a.root, b.root)
            self.assertEqual(a.project, b.project)
            self.assertEqual(a.name, "ep01cf")

    def test_standard_locations(self):
        with tempfile.TemporaryDirectory() as td:
            root, pj = self._proj(td)
            p = ProjectPaths(pj)
            self.assertEqual(p.json, root / "json")
            self.assertEqual(p.out, root / "out")
            self.assertEqual(p.tts, root / "tts")
            self.assertEqual(p.qc_session, root / ".qcframes" / "session")
            self.assertEqual(p.cutplan, root / "json" / "cutplan.json")
            self.assertEqual(p.narration, root / "json" / "narration.json")
            self.assertEqual(p.align, root / "json" / "align.json")
            self.assertEqual(p.tts_result, root / "json" / "tts-result.json")
            self.assertEqual(p.ja_segments, root / "json" / "ja-segments.json")
            self.assertEqual(p.qc_visual, root / "json" / "qc_visual.json")
            self.assertEqual(p.qc_rewrite, root / "json" / "qc_rewrite.json")
            self.assertEqual(p.final_video,
                             root / "out" / "03-rapid-narration.mp4")
            self.assertEqual(p.mix_wav, root / "out" / "mix-rapid.wav")

    def test_mp3_zero_padded(self):
        with tempfile.TemporaryDirectory() as td:
            root, pj = self._proj(td)
            p = ProjectPaths(pj)
            self.assertEqual(p.mp3(5).name, "005.mp3")
            self.assertEqual(p.mp3(39), p.tts / "039.mp3")

    def test_require_raises_with_all_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root, pj = self._proj(td)
            p = ProjectPaths(pj)
            with self.assertRaises(FileNotFoundError) as ctx:
                p.require(p.cutplan, p.narration, p.align)
            self.assertIn("cutplan.json", str(ctx.exception))
            self.assertIn("align.json", str(ctx.exception))

    def test_require_returns_paths_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            root, pj = self._proj(td)
            p = ProjectPaths(pj)
            p.ensure_dirs(p.json)
            p.cutplan.write_text("{}", encoding="utf-8")
            self.assertEqual(p.require(p.cutplan), [p.cutplan])

    def test_ensure_dirs_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root, pj = self._proj(td)
            p = ProjectPaths(pj)
            p.ensure_dirs(p.json, p.out)
            p.ensure_dirs(p.json, p.out)
            self.assertTrue(p.json.is_dir() and p.out.is_dir())


class HelpersTests(unittest.TestCase):
    def test_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "ep01cf"
            root.mkdir()
            pj = root / "project.json"
            pj.write_text("{}", encoding="utf-8")
            self.assertEqual(project_root(pj), root.resolve())
            self.assertEqual(project_root(root), root.resolve())

    def test_repo_root_contains_this_package(self):
        r = repo_root()
        self.assertTrue((r / "aicutplan" / "__init__.py").exists())

    def test_script_helper(self):
        self.assertEqual(script("cf_pipeline").name, "cf_pipeline.py")
        self.assertEqual(script("cf_pipeline.py").name, "cf_pipeline.py")


if __name__ == "__main__":
    unittest.main()
