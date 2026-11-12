# [测试] 职责：L0 配置层（位置解析 / 环境变量覆盖 / 容错 / 原子写 / narration 接入）
"""配置层测试：全部用 tempfile 隔离，不读写真实 config/ 与本机环境变量。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aicutplan import config as CFG


class _TmpRepo(unittest.TestCase):
    """把 repo_root 指向临时目录，并清掉 AICUTPLAN_CONFIG_DIR，保证用例互不污染。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name) / "repo"
        (self.root / "config").mkdir(parents=True)
        self.cfg = self.root / "config"

        p = patch("aicutplan.config.repo_root", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)

        env = {k: v for k, v in os.environ.items() if k != CFG.ENV_CONFIG_DIR}
        e = patch.dict(os.environ, env, clear=True)
        e.start()
        self.addCleanup(e.stop)

    def write(self, path: Path, data) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path


class ConfigDirTests(_TmpRepo):
    def test_default_dir_is_repo_config(self):
        self.assertEqual(CFG.config_dir(), self.cfg)

    def test_env_override(self):
        alt = self.root / "alt-config"
        with patch.dict(os.environ, {CFG.ENV_CONFIG_DIR: str(alt)}):
            self.assertEqual(CFG.config_dir(), alt.resolve())


class ConfigFilePathTests(_TmpRepo):
    def test_prefers_config_dir(self):
        self.write(self.cfg / "templates.json", [{"id": "new"}])
        self.write(self.root / "templates.json", [{"id": "old"}])
        self.assertEqual(CFG.config_file("templates.json"), self.cfg / "templates.json")

    def test_falls_back_to_legacy_root(self):
        legacy = self.write(self.root / "templates.json", [{"id": "old"}])
        self.assertEqual(CFG.config_file("templates.json"), legacy)

    def test_returns_config_path_when_absent(self):
        # 两处都不存在时返回新目录路径，保证后续写入落 config/
        self.assertEqual(CFG.config_file("new.json"), self.cfg / "new.json")

    def test_convenience_paths(self):
        self.assertEqual(CFG.templates_file(), self.cfg / "templates.json")
        self.assertEqual(CFG.calibration_file(), self.cfg / "tts_calibration.json")
        self.assertEqual(CFG.llm_config_file(), self.cfg / "llm_config.json")


class ConfigReadWriteTests(_TmpRepo):
    def test_load_missing_returns_default(self):
        self.assertIsNone(CFG.load_config("none.json"))
        self.assertEqual(CFG.load_config("none.json", {"a": 1}), {"a": 1})

    def test_load_corrupted_returns_default(self):
        (self.cfg / "bad.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(CFG.load_config("bad.json", "fallback"), "fallback")

    def test_load_legacy_file(self):
        self.write(self.root / "templates.json", [{"id": "legacy"}])
        self.assertEqual(CFG.load_config("templates.json"), [{"id": "legacy"}])

    def test_save_writes_config_dir_and_mkdir(self):
        alt = self.root / "nested" / "cfg"
        with patch.dict(os.environ, {CFG.ENV_CONFIG_DIR: str(alt)}):
            p = CFG.save_config("sub/item.json", {"k": "中文"})
            self.assertEqual(p, alt / "sub" / "item.json")
            self.assertTrue(p.exists())
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"k": "中文"})

    def test_save_then_load_roundtrip(self):
        CFG.save_config("x.json", {"n": 2})
        self.assertEqual(CFG.load_config("x.json"), {"n": 2})


class ConfigSummaryTests(_TmpRepo):
    def test_summary_marks_missing_and_legacy(self):
        self.write(self.root / "tts_calibration.json", {"effective_cps": 5})
        rows = {r["name"]: r for r in CFG.summary()}
        self.assertTrue(rows["tts_calibration.json"]["exists"])
        self.assertTrue(rows["tts_calibration.json"]["legacy"])
        self.assertFalse(rows["templates.json"]["exists"])
        self.assertFalse(rows["templates.json"]["legacy"])

    def test_summary_after_migration(self):
        self.write(self.cfg / "templates.json", [{"id": "a"}])
        row = next(r for r in CFG.summary() if r["name"] == "templates.json")
        self.assertTrue(row["exists"])
        self.assertFalse(row["legacy"])


class NarrationIntegrationTests(_TmpRepo):
    """narration 必须走配置层（而不是写死仓库根文件名）。"""

    @classmethod
    def setUpClass(cls):
        from aicutplan.shared import narration as NR
        cls.NR = NR

    def test_templates_read_from_config_dir(self):
        mine = [{"id": "probe", "name": "探针", "system": "s", "user": "u"}]
        self.write(self.cfg / "templates.json", mine)
        self.assertEqual(self.NR.templates(), mine)

    def test_calibration_read_from_config_dir(self):
        self.write(self.cfg / "tts_calibration.json", {"effective_cps": 4.2})
        self.assertEqual(self.NR.calibration()["effective_cps"], 4.2)

    def test_llm_config_read_from_config_dir(self):
        self.write(self.cfg / "llm_config.json", {"model": "probe-model"})
        env = {k: v for k, v in os.environ.items()
               if k not in ("DEEPSEEK_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.NR.llm_config()["model"], "probe-model")

    def test_style_md_env_override(self):
        p = self.root / "style.md"
        p.write_text("文风探针", encoding="utf-8")
        with patch.dict(os.environ, {"AICUTPLAN_STYLE_MD": str(p)}):
            self.assertTrue(self.NR.style_rules().startswith("文风探针"))


if __name__ == "__main__":
    unittest.main()
