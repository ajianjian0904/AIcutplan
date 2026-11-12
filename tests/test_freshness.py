# -*- coding: utf-8 -*-
"""L0·aicutplan.freshness 单元测试：渲染门禁的基线判定与内容兜底。

构造一个"四阶段齐全"的最小工程，验证：
* 盖章后判 fresh；改输入 → 明确指出是哪个输入；
* 产物被替换 → 判 stale；
* 无基线 → unknown，渲染门禁失败（提示 bootstrap）；
* 内容兜底能抓到「旁白改了但 TTS 没重跑」「mp3 与指纹不符」。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from aicutplan import cli as CLI
from aicutplan import freshness as FR
from aicutplan.io import dump_json


class Fixture(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name) / "EP01"
        for d in ("json", "out", "tts", "audio", "verify"):
            (self.root / d).mkdir(parents=True)
        dump_json(self.root / "project.json", {
            "recap": {"voice": "zh-CN-YunjianNeural", "tts_rate": "+35%"}})
        dump_json(self.root / "json" / "cutplan.json",
                  {"windows": [{"new_start": 0, "new_end": 1.0}]})
        dump_json(self.root / "json" / "ja-segments.json", {"segments": []})
        dump_json(self.root / "json" / "narration.json",
                  {"items": [{"i": 0, "text": "第一句"}, {"i": 1, "text": "第二句"}]})
        (self.root / "out" / "01-video-no-ad.mp4").write_bytes(b"SRC" * 512)
        (self.root / "out" / "02-video-rapid.mp4").write_bytes(b"CUT" * 512)
        (self.root / "audio" / "base-rapid.wav").write_bytes(b"BASE" * 512)
        for i, txt in enumerate(("第一句", "第二句")):
            (self.root / "tts" / f"{i:03d}.mp3").write_bytes(f"AUDIO{i}".encode() * 64)
            self.age(f"tts/{i:03d}.mp3", -100)
        dump_json(self.root / "json" / "tts-result.json", {
            "voice": "zh-CN-YunjianNeural", "rate": "+35%",
            "items": [{"i": i, "text": txt,
                       "mp3_sig": self.sig(f"tts/{i:03d}.mp3")}
                      for i, txt in enumerate(("第一句", "第二句"))]})
        (self.root / "out" / "mix-rapid.wav").write_bytes(b"MIX" * 512)
        (self.root / "out" / "03-rapid-narration.mp4").write_bytes(b"FINAL" * 512)
        self.age("out/03-rapid-narration.mp4", -50)   # 最晚写 final
        self.age("out/mix-rapid.wav", -60)
        self.age("json/tts-result.json", -70)
        self.age("out/02-video-rapid.mp4", -80)
        self.age("audio/base-rapid.wav", -80)
        self.age("json/narration.json", -90)
        self.age("json/cutplan.json", -95)
        self.age("out/01-video-no-ad.mp4", -120)
        self.age("json/ja-segments.json", -120)

    def age(self, rel: str, offset: float) -> None:
        t = time.time() + offset
        os.utime(self.root / rel, (t, t))

    def sig(self, rel: str) -> str:
        import hashlib
        p = self.root / rel
        h = hashlib.sha1(str(p.stat().st_size).encode())
        h.update(p.read_bytes())
        return h.hexdigest()[:16]

    def bootstrap(self):
        return FR.bootstrap(self.root, force=True)

    def status(self):
        return {r["stage"]: r for r in FR.check(self.root)}


class StampTests(Fixture):
    def test_bootstrap_then_all_fresh(self):
        r = self.bootstrap()
        self.assertEqual(r["stamped"], ["cut", "tts", "mix", "mux"])
        st = self.status()
        for s in FR.STAGES:
            self.assertEqual(st[s]["status"], "fresh", st[s])
            self.assertEqual(st[s]["source"], "stamp")
        self.assertTrue(FR.render_ready(self.root, "mix")[0])
        self.assertTrue(FR.render_ready(self.root, "mux")[0])

    def test_no_baseline_blocks(self):
        st = self.status()
        self.assertTrue(all(r["status"] == "unknown" for r in st.values()))
        ok, issues = FR.render_ready(self.root, "mix")
        self.assertFalse(ok)
        self.assertIn("无基线", issues[0])

    def test_narration_edit_makes_tts_and_render_stale(self):
        self.bootstrap()
        dump_json(self.root / "json" / "narration.json",
                  {"items": [{"i": 0, "text": "改过的第一句"},
                             {"i": 1, "text": "第二句"}]})
        st = self.status()
        self.assertEqual(st["tts"]["status"], "stale")
        # 内容兜底会先报「文字与 TTS 不一致」，基线判据会报「输入已变更」——两者都成立
        self.assertTrue("不一致" in st["tts"]["detail"]
                        or "json/narration.json" in st["tts"]["detail"])
        ok, issues = FR.render_ready(self.root, "mix")
        self.assertFalse(ok)
        self.assertTrue(any("配音" in x for x in issues))

    def test_mp3_replaced_detected(self):
        self.bootstrap()
        (self.root / "tts" / "000.mp3").write_bytes(b"NEWAUDIO" * 64)
        st = self.status()
        self.assertEqual(st["tts"]["status"], "stale")
        # 内容兜底（指纹不符）或基线（产物被替换）先命中都算检出
        self.assertTrue("指纹不符" in st["tts"]["detail"]
                        or "产物被替换" in st["tts"]["detail"])

    def test_cutplan_edit_blocks_render(self):
        self.bootstrap()
        dump_json(self.root / "json" / "cutplan.json",
                  {"windows": [{"new_start": 0, "new_end": 2.0}]})
        st = self.status()
        self.assertEqual(st["cut"]["status"], "stale")
        ok, issues = FR.render_ready(self.root, "mux")
        self.assertFalse(ok)
        self.assertTrue(any("画面粗剪" in x for x in issues))

    def test_partial_stamp(self):
        FR.stamp(self.root, "cut")
        st = self.status()
        self.assertEqual(st["cut"]["status"], "fresh")
        self.assertEqual(st["mix"]["status"], "unknown")

    def test_stamp_after_rerun(self):
        self.bootstrap()
        (self.root / "tts" / "000.mp3").write_bytes(b"NEW" * 64)
        self.assertEqual(self.status()["tts"]["status"], "stale")
        # 模拟 r5 重跑：重写 tts-result 并重新盖章
        dump_json(self.root / "json" / "tts-result.json", {
            "voice": "zh-CN-YunjianNeural", "rate": "+35%",
            "items": [{"i": 0, "text": "第一句", "mp3_sig": self.sig("tts/000.mp3")},
                      {"i": 1, "text": "第二句", "mp3_sig": self.sig("tts/001.mp3")}]})
        FR.stamp(self.root, "tts")
        self.assertEqual(self.status()["tts"]["status"], "fresh")


class ContentFallbackTests(Fixture):
    def test_text_mismatch_without_stamp(self):
        dump_json(self.root / "json" / "tts-result.json", {
            "voice": "zh-CN-YunjianNeural", "rate": "+35%",
            "items": [{"i": 0, "text": "旧词", "mp3_sig": self.sig("tts/000.mp3")},
                      {"i": 1, "text": "第二句", "mp3_sig": self.sig("tts/001.mp3")}]})
        st = self.status()
        self.assertEqual(st["tts"]["status"], "stale")
        self.assertIn("文字与 TTS 不一致", st["tts"]["detail"])

    def test_voice_change_detected(self):
        dump_json(self.root / "project.json",
                  {"recap": {"voice": "zh-CN-YunxiNeural", "tts_rate": "+35%"}})
        st = self.status()
        self.assertEqual(st["tts"]["status"], "stale")
        self.assertIn("音色改过", st["tts"]["detail"])

    def test_mtime_only_flags_old_mix(self):
        # 无基线 + mix 比 tts-result 旧 → 判过期（mtime 兜底）
        self.age("out/mix-rapid.wav", -10)
        self.age("json/tts-result.json", -5)
        st = self.status()
        self.assertEqual(st["mix"]["status"], "stale")
        self.assertIn("比混音新", st["mix"]["detail"])

    def test_bootstrap_respects_mtime(self):
        self.age("out/mix-rapid.wav", -10)
        self.age("json/tts-result.json", -5)
        r = FR.bootstrap(self.root)          # 不加 force
        self.assertIn("mix", r["skipped"])
        r = FR.bootstrap(self.root, force=True)
        self.assertIn("mix", r["stamped"])


class CliTests(Fixture):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = CLI.main(["guard", "--project", str(self.root), *argv])
        return rc, buf.getvalue()

    def test_cli_blocks_unknown(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("未通过", out)

    def test_cli_bootstrap_then_pass(self):
        rc, _ = self.run_cli("--bootstrap", "--force")
        self.assertEqual(rc, 0)
        rc, out = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("可以渲染", out)

    def test_cli_json(self):
        self.bootstrap()
        rc, out = self.run_cli("--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(len(data["rows"]), 4)
        self.assertEqual(data["gates"], {"mix": [], "mux": []})


if __name__ == "__main__":
    unittest.main()
