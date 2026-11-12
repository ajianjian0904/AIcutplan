# [测试] 职责：作品库布局 + runs 版本快照/最新指针/污染扫描/迁移
"""workspace 测试：全部在 tempfile 内构造最小工程，不碰真实作品库与本机配置。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aicutplan import workspace as WS
from aicutplan.io import dump_json


class _Fixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.lib = self.tmp / "library"
        env = patch.dict(os.environ, {WS.ENV_LIBRARY: str(self.lib)})
        env.start()
        self.addCleanup(env.stop)

    def make_episode(self, series="测试动漫", ep="EP01", *, final=True):
        WS.init_series(series)          # 作品库要求 series.json 才被识别
        root = self.lib / series / ep
        (root / "json").mkdir(parents=True)
        (root / "out").mkdir()
        (root / "tts").mkdir()
        dump_json(root / "project.json",
                  {"source_video": "F:/src/ep01.mkv", "workdir": str(root)})
        dump_json(root / "json" / "narration.json", {"items": [{"i": 0, "text": "一句"}]})
        dump_json(root / "json" / "cutplan.json", {"windows": [{"dur": 1.0}]})
        dump_json(root / "json" / "align.json", {"sent_win_idx": {"0": [0, 0]}})
        if final:
            (root / "out" / "03-rapid-narration.mp4").write_bytes(b"video" * 4096)
        return root


class NameAndLibraryTests(_Fixture):
    def test_safe_name_strips_windows_illegal(self):
        self.assertEqual(WS.safe_name('Re:Zero / 从零开始?'), "Re-Zero - 从零开始-")

    def test_library_root_env(self):
        self.assertEqual(WS.library_root(), self.lib.resolve())

    def test_init_series_idempotent(self):
        d1 = WS.init_series("测试动漫", aliases=["别名A"])
        d2 = WS.init_series("测试动漫")
        self.assertEqual(d1, d2)
        meta = (d1 / WS.SERIES_META).read_text(encoding="utf-8")
        self.assertIn("别名A", meta)

    def test_locate_inside_library(self):
        root = self.make_episode()
        loc = WS.locate(root / "project.json")
        self.assertEqual(loc["series"], "测试动漫")
        self.assertEqual(loc["episode"], "EP01")

    def test_locate_outside_library(self):
        other = self.tmp / "plain"
        other.mkdir()
        dump_json(other / "project.json", {})
        self.assertIsNone(WS.locate(other / "project.json"))


class SnapshotTests(_Fixture):
    def test_snapshot_creates_run_and_latest(self):
        root = self.make_episode()
        m = WS.snapshot(root / "project.json", label="v1")
        run_dir = root / "runs" / m["run_id"]
        self.assertTrue((run_dir / "manifest.json").exists())
        self.assertTrue((run_dir / "final.mp4").exists())
        self.assertEqual(m["label"], "v1")
        self.assertIn("final.mp4", m["artifacts"])
        latest = WS.latest(root)
        self.assertEqual(latest["run_id"], m["run_id"])
        self.assertEqual(latest["final_sig"], m["artifacts"]["final.mp4"]["sig"])

    def test_snapshot_requires_final(self):
        root = self.make_episode(final=False)
        with self.assertRaises(FileNotFoundError):
            WS.snapshot(root)

    def test_hardlink_isolated_from_later_overwrite(self):
        root = self.make_episode()
        m = WS.snapshot(root)
        run_final = root / "runs" / m["run_id"] / "final.mp4"
        sig_before = WS.quick_sig(run_final)
        # 模拟下一次渲染覆盖 out/（写新文件再替换，与 ffmpeg 行为一致）
        new = root / "out" / "tmp.mp4"
        new.write_bytes(b"NEW" * 8192)
        os.replace(new, root / "out" / "03-rapid-narration.mp4")
        self.assertEqual(WS.quick_sig(run_final), sig_before)
        self.assertEqual(WS.quick_sig(run_final), m["artifacts"]["final.mp4"]["sig"])

    def test_snapshot_falls_back_to_copy(self):
        root = self.make_episode()
        with patch("aicutplan.workspace.os.link", side_effect=OSError("cross-device")):
            m = WS.snapshot(root)
        self.assertEqual(m["artifacts"]["final.mp4"]["mode"], "copy")

    def test_two_snapshots_get_sequential_ids(self):
        root = self.make_episode()
        m1 = WS.snapshot(root, label="v1")
        m2 = WS.snapshot(root, label="v2")
        self.assertLess(m1["run_id"], m2["run_id"])
        rows = WS.list_runs(root)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[-1]["is_latest"])
        self.assertEqual(WS.latest(root)["run_id"], m2["run_id"])

    def test_latest_detects_tampered_final(self):
        root = self.make_episode()
        m = WS.snapshot(root)
        (root / "runs" / m["run_id"] / "final.mp4").write_bytes(b"tampered")
        with self.assertRaises(RuntimeError) as ctx:
            WS.latest(root)
        self.assertIn("指纹不符", str(ctx.exception))

    def test_latest_missing_raises(self):
        root = self.make_episode()
        with self.assertRaises(FileNotFoundError):
            WS.latest(root)

    def test_verify_run_reports_missing_artifact(self):
        root = self.make_episode()
        m = WS.snapshot(root)
        run_dir = root / "runs" / m["run_id"]
        (run_dir / "narration.json").unlink()
        issues = WS.verify_run(run_dir)
        self.assertTrue(any("narration.json" in x for x in issues))


class StaleScanTests(_Fixture):
    def _age(self, p: Path, sec: float):
        t = time.time() - sec
        os.utime(p, (t, t))

    def test_scan_reports_no_archive(self):
        root = self.make_episode()
        self.assertTrue(any("尚无归档版本" in x for x in WS.scan_stale(root)))

    def test_scan_reports_unarchived_change(self):
        root = self.make_episode()
        WS.snapshot(root)
        time.sleep(0.05)
        narr = root / "json" / "narration.json"
        dump_json(narr, {"items": [{"i": 0, "text": "改过了"}]})
        self.assertTrue(any("未归档改动" in x for x in WS.scan_stale(root)))

    def test_scan_reports_text_mismatch(self):
        root = self.make_episode()
        dump_json(root / "json" / "tts-result.json",
                  {"items": [{"i": 0, "text": "旧词", "tts_dur": 1.0}]})
        WS.snapshot(root)
        dump_json(root / "json" / "narration.json", {"items": [{"i": 0, "text": "新词"}]})
        self.assertTrue(any("旧音频风险" in x for x in WS.scan_stale(root)))

    def test_scan_reports_mp3_sig_mismatch(self):
        root = self.make_episode()
        mp3 = root / "tts" / "000.mp3"
        mp3.write_bytes(b"old-audio")
        dump_json(root / "json" / "tts-result.json",
                  {"items": [{"i": 0, "text": "一句", "mp3_sig": "deadbeefdeadbeef"}]})
        WS.snapshot(root)
        self.assertTrue(any("指纹不符" in x for x in WS.scan_stale(root)))

    def test_scan_ignores_legacy_without_sig(self):
        root = self.make_episode()
        (root / "tts" / "000.mp3").write_bytes(b"audio")
        dump_json(root / "json" / "tts-result.json", {"items": [{"i": 0, "text": "一句"}]})
        WS.snapshot(root)
        self.assertFalse(any("指纹不符" in x for x in WS.scan_stale(root)))

    def test_scan_reports_stale_qc_frames(self):
        root = self.make_episode()
        qc = root / ".qcframes"
        qc.mkdir()
        f = qc / "qc_0000.jpg"
        f.write_bytes(b"jpg")
        self._age(f, 3600)
        WS.snapshot(root)
        self.assertTrue(any("旧质检帧" in x for x in WS.scan_stale(root)))

    def test_scan_clean_after_fresh_snapshot(self):
        root = self.make_episode()
        WS.snapshot(root)
        self.assertEqual(WS.scan_stale(root), [])


class MigrateTests(_Fixture):
    def _plain_project(self):
        p = self.tmp / "old" / "ep01cf"
        (p / "json").mkdir(parents=True)
        (p / "out").mkdir()
        dump_json(p / "project.json", {"workdir": str(p), "source_video": "F:/a.mkv"})
        (p / "json" / "narration.json").write_text("{}", encoding="utf-8")
        (p / "cut.py").write_text("# local script", encoding="utf-8")
        return p

    def test_migrate_copy_keeps_source_and_rewrites_workdir(self):
        src = self._plain_project()
        r = WS.migrate(src / "project.json", "测试动漫", "EP01")
        self.assertEqual(r["mode"], "copy")
        dst = Path(r["dst"])
        self.assertTrue((src / "project.json").exists())
        self.assertTrue((dst / "cut.py").exists())
        import json
        cfg = json.loads((dst / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(cfg["workdir"]), dst)

    def test_migrate_move_removes_source(self):
        src = self._plain_project()
        r = WS.migrate(src / "project.json", "测试动漫", "EP01", move=True)
        self.assertFalse(src.exists())
        self.assertTrue((Path(r["dst"]) / "project.json").exists())

    def test_migrate_refuses_existing_target(self):
        src = self._plain_project()
        WS.migrate(src / "project.json", "测试动漫", "EP01")
        with self.assertRaises(FileExistsError):
            WS.migrate(src / "project.json", "测试动漫", "EP01")

    def test_migrate_dry_run_changes_nothing(self):
        src = self._plain_project()
        plan = WS.migrate(src / "project.json", "测试动漫", "EP01", dry_run=True)
        self.assertFalse(Path(plan["dst"]).exists())
        self.assertGreater(plan["files"], 0)

    def test_list_series_shows_episode_status(self):
        root = self.make_episode()
        WS.snapshot(root, label="v1")
        data = WS.list_series()
        self.assertEqual(len(data), 1)
        ep = data[0]["episodes"][0]
        self.assertEqual(ep["episode"], "EP01")
        self.assertTrue(ep["latest_run"].startswith("r00"))
        self.assertEqual(ep["runs"], 1)


if __name__ == "__main__":
    unittest.main()
