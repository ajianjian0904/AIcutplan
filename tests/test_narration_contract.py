# -*- coding: utf-8 -*-
"""会话内导演契约的负向回归测试（2026-11-13 三集音画错位事故）。

核心目标：让 v1 那类「按剧情顺序写稿、按窗口序号均匀贴」的稿子**必须失败**，
并证明 EP01 式历史稿（窗口源时间有回跳、句子无 src）走 legacy 豁免不被误伤。

全部使用标准库 unittest + tempfile，零 ffmpeg / 零网络 / 零 API / 零模型。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aicutplan import contracts as CT

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


def _wins(*, body_inversions: bool = False, hook_gap: bool = False) -> list[dict]:
    """12 窗：前 3 窗是钩子（源 100–103.6 连续），后 9 窗是正片（源 10–28）。"""
    wins: list[dict] = []
    for k in range(3):
        a = 100.0 + k * 1.2
        if hook_gap and k == 1:
            a += 0.5                      # 人为制造钩子段断点
        wins.append({"src_start": round(a, 3), "src_end": round(a + 1.2, 3),
                     "new_start": round(k * 1.2, 3), "new_end": round((k + 1) * 1.2, 3),
                     "dur": 1.2, "tier": "line", "ost_mode": "narration",
                     "reason": "前置爽点·连续未裁切 100.0-103.6s"})
    srcs = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0, 26.0]
    if body_inversions:
        srcs = [10.0, 20.0, 14.0, 16.0, 18.0, 22.0, 24.0, 26.0, 28.0]
    for k, a in enumerate(srcs):
        wins.append({"src_start": a, "src_end": a + 2.0,
                     "new_start": 3.6 + k * 2.0, "new_end": 3.6 + (k + 1) * 2.0,
                     "dur": 2.0, "tier": "line", "ost_mode": "narration",
                     "reason": "会话内导演·连续剧情段"})
    return wins


def _cfg(*, with_clip: bool = True) -> dict:
    return {"recap": {"hook_clip": [100.0, 103.6]} if with_clip else {}}


def _good(wins: list[dict]) -> list[dict]:
    """正确的导演稿：3 句钩子专属文案 + 5 句正片，每句 src 与窗口一致。"""
    out = []
    for i in range(3):
        out.append({"ws": i, "we": i, "text": f"钩子文案第{i + 1}句",
                    "src": [wins[i]["src_start"], wins[i]["src_end"]]})
    for ws, we in ((3, 4), (5, 6), (7, 8), (9, 9), (10, 11)):
        out.append({"ws": ws, "we": we, "text": "这一句写的正是这几个窗口的画面",
                    "src": [wins[ws]["src_start"], wins[we]["src_end"]]})
    return out


def _uniform(wins: list[dict]) -> list[dict]:
    """v1 事故稿：句子按剧情顺序写（src 从 10s 递增），却按窗口序号均匀贴。

    第一句会贴到钩子窗（src 100），声明的却是 10–12s —— 契约必须拦住。
    """
    groups = [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9), (10, 11)]
    out = []
    for k, (ws, we) in enumerate(groups):
        a = 10.0 + k * 3.0
        out.append({"ws": ws, "we": we, "text": "按剧情顺序写的第%d句" % (k + 1),
                    "src": [a, a + 3.0]})
    return out


class TestSentencesContract(unittest.TestCase):
    def setUp(self):
        self.wins = _wins()
        self.cfg = _cfg()

    def test_v2_director_script_passes(self):
        e, w, s = CT.check_sentences_contract(_good(self.wins), self.wins, self.cfg)
        self.assertEqual(e, [], e)
        self.assertEqual(s["contract"], "src-v2")
        self.assertEqual(s["src_checked"], 8)
        self.assertGreaterEqual(s["hook_sents"], 2)
        self.assertEqual(s["inversions"], 0)

    def test_uniform_assignment_is_blocked(self):
        """核心负向回归：v1 均匀分配稿必须失败。"""
        e, _, s = CT.check_sentences_contract(_uniform(self.wins), self.wins, self.cfg)
        self.assertTrue(e, "均匀分配稿竟然通过了契约")
        self.assertTrue(any("不是同一段" in x or "均匀分布" in x or "不符" in x for x in e), e)
        self.assertLess(s["src_checked"], 6)

    def test_missing_src_is_blocked(self):
        sents = [{k: v for k, v in s.items() if k != "src"} for s in _good(self.wins)]
        e, _, _ = CT.check_sentences_contract(sents, self.wins, self.cfg)
        self.assertTrue(any("缺 src" in x for x in e), e)

    def test_legacy_exemption_passes_and_marks(self):
        sents = [{k: v for k, v in s.items() if k != "src"} for s in _good(self.wins)]
        e, w, s = CT.check_sentences_contract(sents, self.wins, self.cfg, legacy=True)
        self.assertEqual(e, [], e)
        self.assertEqual(s["contract"], "legacy")
        self.assertTrue(any("legacy" in x for x in w), w)

    def test_partial_src_is_blocked(self):
        sents = _good(self.wins)
        sents[3].pop("src")
        e, _, _ = CT.check_sentences_contract(sents, self.wins, self.cfg)
        self.assertTrue(any("混用" in x for x in e), e)

    def test_hook_clip_missing_is_blocked(self):
        e, _, _ = CT.check_sentences_contract(_good(self.wins), self.wins, _cfg(with_clip=False))
        self.assertTrue(any("hook_clip" in x for x in e), e)

    def test_hook_clip_mismatch_is_blocked(self):
        cfg = {"recap": {"hook_clip": [500.0, 503.6]}}
        e, _, _ = CT.check_sentences_contract(_good(self.wins), self.wins, cfg)
        self.assertTrue(any("不符" in x for x in e), e)

    def test_hook_without_dedicated_copy_is_blocked(self):
        sents = [s for s in _good(self.wins) if s["ws"] > 2]   # 钩子窗无专属文案
        # 补上窗口 0-2 的覆盖，但只给 1 句
        sents.insert(0, {"ws": 0, "we": 2, "text": "唯一一句钩子",
                         "src": [self.wins[0]["src_start"], self.wins[2]["src_end"]]})
        e, _, _ = CT.check_sentences_contract(sents, self.wins, self.cfg)
        self.assertTrue(any("专属文案" in x for x in e), e)

    def test_hook_gap_is_blocked(self):
        wins = _wins(hook_gap=True)
        sents = _good(self.wins)
        for s in sents:
            s["src"] = [wins[s["ws"]]["src_start"], wins[s["we"]]["src_end"]]
        e, _, _ = CT.check_sentences_contract(sents, wins, self.cfg)
        self.assertTrue(any("不连续" in x for x in e), e)

    def test_no_hook_structure_skips_hook_rules(self):
        """旧链工程没有钩子窗，不能被新断言判死。"""
        wins = [dict(w, reason="普通镜头") for w in self.wins]
        cfg = {"recap": {}}                     # 没有 hook_clip
        sents = _good(wins)
        for s in sents:
            s["src"] = [wins[s["ws"]]["src_start"], wins[s["we"]]["src_end"]]
        e, w, _ = CT.check_sentences_contract(sents, wins, cfg)
        self.assertFalse(any("hook_clip" in x or "专属文案" in x for x in e), e)
        self.assertTrue(any("跳过钩子契约" in x for x in w), w)

    def test_window_inversion_is_warning_only_for_legacy(self):
        """EP01 段内逆序 6 次但 0 失配：历史稿不能被逆序判死。"""
        wins = _wins(body_inversions=True)
        sents = [{k: v for k, v in s.items() if k != "src"} for s in _good(wins)]
        e, w, s = CT.check_sentences_contract(sents, wins, self.cfg, legacy=True)
        self.assertEqual(e, [], e)
        self.assertGreater(s["inversions"], 0)
        self.assertTrue(any("回跳" in x for x in w), w)

    def test_src_script_order_inversion_is_blocked(self):
        """带 src 的导演稿声称按剧情顺序，稿内源区间倒退必须失败。"""
        wins = _wins(body_inversions=True)
        # 一句一窗，让稿内 src 序列原样复现窗口的回跳（分组跨窗会吃掉回跳点）
        sents = [{"ws": i, "we": i, "text": "一句对应一窗",
                  "src": [w["src_start"], w["src_end"]]}
                 for i, w in enumerate(wins)]
        e, _, _ = CT.check_sentences_contract(sents, wins, self.cfg)
        self.assertTrue(any("倒退" in x for x in e), e)

    def test_empty_script_is_blocked(self):
        e, _, _ = CT.check_sentences_contract([], self.wins, self.cfg)
        self.assertTrue(e)

    def test_hook_window_ids(self):
        self.assertEqual(CT.hook_window_ids(self.wins), [0, 1, 2])


def _mini_project(root: Path, *, with_clip: bool = True,
                  sents: list[dict] | None = None) -> None:
    """构造 narrate_cut CLI 能加载的最小工程（无 ffmpeg / 无网络）。"""
    wins = _wins()
    (root / "json").mkdir(parents=True, exist_ok=True)
    (root / "project.json").write_text(json.dumps(_cfg(with_clip=with_clip)), encoding="utf-8")
    (root / "json" / "cutplan.json").write_text(
        json.dumps({"windows": wins, "stats": {}}, ensure_ascii=False), encoding="utf-8")
    meta = [{"ev": 0 if i < 3 else 1, "beat": 0 if i < 3 else 1,
             "ost_mode": "narration"} for i in range(len(wins))]
    (root / "json" / "cutplan_meta.json").write_text(
        json.dumps({"meta": meta, "beat_span": {"0": [0, 3.6], "1": [3.6, 21.6]}},
                   ensure_ascii=False), encoding="utf-8")
    (root / "json" / "story_plan.json").write_text(
        json.dumps({"events": [], "causal": {},
                    "blocks": [{"label": "钩子", "type": "钩子", "t0": 0, "t1": 3.6,
                                "target": 3.6, "summary": ""},
                               {"label": "正片", "type": "铺垫", "t0": 3.6, "t1": 21.6,
                                "target": 18.0, "summary": ""}]}, ensure_ascii=False),
        encoding="utf-8")
    (root / "json" / "shots-cache.json").write_text(
        json.dumps({"bounds": [float(i) for i in range(15)], "fps": 24}, ensure_ascii=False),
        encoding="utf-8")
    (root / "json" / "narration.json").write_text('{"sentinel": true}', encoding="utf-8")
    (root / "sents.json").write_text(
        json.dumps(sents if sents is not None else _good(wins), ensure_ascii=False),
        encoding="utf-8")


def _cli(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "aicutplan.chain.narrate_cut", "--project", str(root / "project.json"),
         "--sentences", str(root / "sents.json"), *extra],
        cwd=str(REPO), capture_output=True, text=True, timeout=60)


class TestCliGate(unittest.TestCase):
    def test_cli_blocks_before_write(self):
        """缺 src → 退出 5，且 narration.json 未被覆盖（落盘前阻断）。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = [{k: v for k, v in s.items() if k != "src"} for s in _good(_wins())]
            _mini_project(root, sents=bad)
            r = _cli(root)
            self.assertEqual(r.returncode, 5, r.stderr)
            self.assertIn("缺 src", r.stderr)
            self.assertEqual(json.loads((root / "json" / "narration.json").read_text(
                encoding="utf-8")), {"sentinel": True})

    def test_cli_legacy_writes_mark(self):
        """--legacy-sents 放行，并在 narration.stats 打 legacy 标记。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = [{k: v for k, v in s.items() if k != "src"} for s in _good(_wins())]
            _mini_project(root, sents=bad)
            r = _cli(root, "--legacy-sents")
            self.assertEqual(r.returncode, 0, r.stderr)
            narr = json.loads((root / "json" / "narration.json").read_text(encoding="utf-8"))
            self.assertEqual(narr["stats"]["contract"], "legacy")

    def test_cli_good_script_writes_src_v2_mark(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mini_project(root)
            r = _cli(root)
            self.assertEqual(r.returncode, 0, r.stderr)
            narr = json.loads((root / "json" / "narration.json").read_text(encoding="utf-8"))
            self.assertEqual(narr["stats"]["contract"], "src-v2")

    def test_cli_hook_clip_missing_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mini_project(root, with_clip=False)
            r = _cli(root)
            self.assertEqual(r.returncode, 5, r.stderr)
            self.assertIn("hook_clip", r.stderr)


class TestLintIntegration(unittest.TestCase):
    def test_lint_reports_hook_contract(self):
        """主链 lint 已接入钩子断言：hook_clip 缺失要报出来。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _mini_project(root, with_clip=False)
            # 补齐 lint 需要的 narration/align（用简单结构）
            wins = _wins()
            (root / "json" / "narration.json").write_text(json.dumps(
                {"items": [{"i": i, "text": "x", "t0": w["new_start"], "dur": w["dur"]}
                           for i, w in enumerate(wins)], "stats": {}}, ensure_ascii=False),
                encoding="utf-8")
            (root / "json" / "align.json").write_text(json.dumps(
                {"sent_win_idx": {str(i): [i, i] for i in range(len(wins))}},
                ensure_ascii=False), encoding="utf-8")
            bad = CT.lint_narration(root)
            self.assertTrue(any("hook_clip" in x for x in bad), bad)


if __name__ == "__main__":
    unittest.main()
