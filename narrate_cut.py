# -*- coding: utf-8 -*-
"""Cut-first 阶段二：旁白后写（narrate-second）。

画面时间轴已由 cutfirst_plan 定型并渲染。本脚本让 LLM **看着已定的输出时间轴**
写旁白：每句在写作时就知道它覆盖输出轴上哪几个窗口、有多长的槽位，字数预算
直接由窗口时长推出（时长条件生成）。旁白写完 TTS 后时长天然落在画面窗口内，
只需 ±15% 弹性微调（elastic_fit.py / r6 内置），不再裁画面。

同步不再靠事后对齐，而是靠生成顺序保证——"对不上"在构造上不可能发生。

用法：python narrate_cut.py --project <pj> [--rate 4.95]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import narration as NR  # noqa: E402
import story_parse as SP  # noqa: E402
from smart_align import _load  # noqa: E402

NARR_SYS = ("你是动漫解说文案写手。画面剪辑已经完成（输出时间轴已定型），你要对着这个时间轴"
            "逐窗口写解说词：每句覆盖指定的连续窗口，字数必须落在该窗口时长的预算内。")

NARR_Q = """【本块画面时间轴】（窗口按播放顺序；"成片秒"是它在最终视频里的位置）
{wins}

【剧情语境】
块名：{label}（叙事功能：{func}）
本块事件因果：{causal}
前文旁白（承接用，不要重复）：
{prev}

【写作要求】
1. 按窗口顺序写解说，输出 JSON 数组（不要解释）：
[{{"ws": 起始窗口号, "we": 结束窗口号, "text": "解说句子"}}]
2. 每个窗口必须被恰好一句覆盖；相邻窗口可合并给同一句（ws..we），但一句最多跨 3 个窗口。
3. **字数硬约束**：每句字数 = 窗口时长合计 × {rate} 字/秒 × 0.95，上下浮动 1 字。
   （窗口时长合计已标在每行末尾，预算也直接给出——贴着预算写，超了会被压语速，少了会留空）
4. 解说内容必须来自这些窗口的画面/事件本身（画面演什么就讲什么，因果解释要落到画面上）；
   可以有语气钩子，但不能空评。
5. 句子短、有节奏，适配动漫解说风格；专有名词（人名/招式）以画面与台词为准。
"""


def _shot_at(shots: list[dict], t: float) -> dict | None:
    for sh in shots:
        if sh["a"] <= t < sh["b"]:
            return sh
    return None


def build_wins_lines(wins: list[dict], meta: list[dict], lo: int, hi: int,
                     shots: list[dict], scene: list[dict], events: list[dict]) -> list[str]:
    lines = []
    for wi in range(lo, hi + 1):
        w = wins[wi]
        gi = meta[wi]["ev"]
        e = events[gi] if gi < len(events) else {}
        mid = (w["src_start"] + w["src_end"]) / 2
        sh = _shot_at(shots, mid)
        desc = (sh.get("desc", "") if sh else "") or e.get("event", "")
        dlg = ""
        for r in scene:
            if float(r["t"]) <= mid < float(r["t1"]) and r.get("dlg"):
                dlg = r["dlg"][:36]
                break
        span = w["new_end"] - w["new_start"]
        lines.append(f"窗口{wi} | 成片 {w['new_start']:.1f}-{w['new_end']:.1f}s "
                     f"({span:.2f}s, 预算≈{int(span * 4.7)}字) | 画面: {desc}"
                     + (f" | 台词: {dlg}" if dlg else ""))
    return lines


def narrate(proj_dir: Path, rate: float, sentences_path: str | None = None) -> dict:
    d = proj_dir / "json"
    plan = _load(d / "cutplan.json")
    meta = _load(d / "cutplan_meta.json")
    wins, wmeta = plan["windows"], meta["meta"]
    beat_span = {int(k): v for k, v in meta["beat_span"].items()}
    sp = _load(d / "story_plan.json")
    events = sp.get("events") or []
    causal = sp.get("causal") or {}
    blocks = [b for b in (sp.get("blocks") or []) if b["type"] != "收录" and b["target"] > 0]
    shots = _load_shots(proj_dir)
    scene = (_load(d / "scene_desc.json").get("rows", []) if (d / "scene_desc.json").exists() else [])
    beat_wins: dict[int, list[int]] = {}
    for wi, m in enumerate(wmeta):
        beat_wins.setdefault(m["beat"], []).append(wi)

    # 会话内写作模式：句子已备好，只做校验与落盘
    if sentences_path:
        raw = _load(Path(sentences_path))
        sents_all = raw if isinstance(raw, list) else raw.get("sents", [])
        items, binding = [], {}
        covered: set[int] = set()
        for s in sents_all:
            ws, we = int(s["ws"]), int(s["we"])
            text = str(s.get("text", "")).strip()
            if not text or we < ws:
                continue
            for x in range(ws, we + 1):
                covered.add(x)
            slot = wins[we]["new_end"] - wins[ws]["new_start"]
            sid = len(items)
            items.append({"i": sid, "text": text, "beat": wmeta[ws]["beat"],
                          "t0": round(wins[ws]["new_start"], 3), "dur": round(slot, 3),
                          "chars": len(text)})
            binding[sid] = [ws, we]
        n_all = len(wins)
        n_cov = len(covered)
        print(f"[旁白后写·会话版] {len(items)} 句 · 窗口覆盖 {n_cov}/{n_all}", flush=True)
        unc = [wi for wi in range(n_all) if wi not in covered]
        if unc:
            print(f"[旁白后写·会话版] 未覆盖窗口 {unc}", flush=True)
        _write_outputs(d, items, binding, beat_span, beat_wins, n_cov, n_all)
        return {"sents": len(items), "covered": f"{n_cov}/{n_all}"}

    items: list[dict] = []
    binding: dict[int, list[int]] = {}
    prev_text = ""
    for bi in sorted(beat_wins):
        wids = beat_wins[bi]
        bl = blocks[bi] if bi < len(blocks) else {}
        lines = build_wins_lines(wins, wmeta, wids[0], wids[-1], shots, scene, events)
        # 因果语境：本块事件里 func 为反转/爆发的因果连接
        cs = []
        for wi in wids:
            c = causal.get(str(wmeta[wi]["ev"])) or {}
            if c.get("func") or c.get("cause"):
                cs.append(f"事件{wmeta[wi]['ev']}({c.get('func', '')})"
                          f" 起因{c.get('cause', [])} 结果{c.get('result', [])}")
        q = NARR_Q.replace("{wins}", "\n".join(lines)) \
                 .replace("{label}", bl.get("label", "")) \
                 .replace("{func}", bl.get("type", "")) \
                 .replace("{causal}", "；".join(cs[:6]) or "无") \
                 .replace("{prev}", prev_text[-300:] or "（开头）") \
                 .replace("{rate}", f"{rate:.2f}")
        sents: list[dict] = []
        try:
            r = NR.llm_chat(NARR_SYS, q, temperature=0.3, timeout=600)
            txt = (r.get("text") or "").strip()
            m = txt.find("[")
            sents = json.loads(txt[m:txt.rfind("]") + 1]) if m >= 0 else []
        except Exception as e:
            print(f"[旁白后写] 块{bi} 失败：{type(e).__name__} {str(e)[:120]}", flush=True)
            continue

        # 校验/修补：窗口全覆盖、ws<=we、不越界
        covered: set[int] = set()
        for s in sents:
            try:
                ws, we = int(s["ws"]), int(s["we"])
            except (KeyError, TypeError, ValueError):
                continue
            ws = max(wids[0], min(ws, wids[-1]))
            we = max(ws, min(we, wids[-1]))
            text = str(s.get("text", "")).strip()
            if not text:
                continue
            sents_ok = True
            for x in range(ws, we + 1):
                if x in covered:
                    sents_ok = False   # 重叠 → 后面这句放弃该窗口（保持顺序语义）
            if not sents_ok:
                continue
            for x in range(ws, we + 1):
                covered.add(x)
            slot = wins[we]["new_end"] - wins[ws]["new_start"]
            sid = len(items)
            items.append({"i": sid, "text": text, "beat": bi,
                          "t0": round(wins[ws]["new_start"], 3),
                          "dur": round(slot, 3),
                          "chars": len(text)})
            binding[sid] = [ws, we]
        # 漏掉的窗口 → 并给前一句（拉长槽位），没有前句则并给后句写完后补
        missing = [x for x in wids if x not in covered]
        for x in missing:
            cand = None
            for sid in range(len(items) - 1, -1, -1):
                if items[sid]["beat"] == bi and binding[sid][1] < x:
                    cand = sid
                    break
            if cand is not None and binding[cand][1] == x - 1:
                binding[cand][1] = x
                items[cand]["dur"] = round(wins[x]["new_end"] - wins[binding[cand][0]]["new_start"], 3)
                covered.add(x)
        prev_text += " / ".join(s["text"] for s in items if s["beat"] == bi)

    # 未覆盖窗口兜底（开头块没有前句时）：报告
    n_all = sum(len(v) for v in beat_wins.values())
    n_cov = sum(1 for wi in range(n_all) if any(wi in rng for rng in binding.values()))
    print(f"[旁白后写] {len(items)} 句 · 窗口覆盖 {n_cov}/{n_all}", flush=True)
    if n_cov < n_all:
        unc = [wi for wi in range(n_all) if not any(wi in rng for rng in binding.values())]
        print(f"[旁白后写] 未覆盖窗口 {unc[:10]}（留白，原声 −16dB 环境）", flush=True)
    _write_outputs(d, items, binding, beat_span, beat_wins, n_cov, n_all)
    return {"sents": len(items), "covered": f"{n_cov}/{n_all}"}


def _write_outputs(d: Path, items: list[dict], binding: dict[int, list[int]],
                   beat_span: dict, beat_wins: dict, n_cov: int, n_all: int) -> None:
    """写 narration.json（含 r7 需要的 stats）+ align.json（含 sent_win_idx）。"""
    import statistics as _st
    lens = [len(it["text"]) for it in items]
    narr_out = {"items": items,
                "stats": {"speech_sec": round(sum(it["dur"] for it in items), 2),
                          "chars": sum(lens),
                          "sentences": len(items),
                          "len_median": int(_st.median(lens)) if lens else 0}}
    (d / "narration.json").write_text(json.dumps(narr_out, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    sent_span = [[it["i"], it["t0"], round(it["t0"] + it["dur"], 3), it["dur"]] for it in items]
    align_out = {"windows": _load(d / "cutplan.json")["windows"],
                 "align": [{"kind": "line", "sent": it["i"], "text": it["text"],
                            "src": None, "new_start": it["t0"], "dur": it["dur"],
                            "sents": [it["i"]]} for it in items],
                 "beat_new": {str(b): list(v) for b, v in beat_span.items()},
                 "beat_win": {str(b): [min(v), max(v) + 1] for b, v in beat_wins.items()},
                 "sent_beat": {str(it["i"]): it["beat"] for it in items},
                 "sent_win_idx": {str(k): v for k, v in binding.items()},
                 "sent_span": sent_span,
                 "mode": "cut-first"}
    (d / "align.json").write_text(json.dumps(align_out, ensure_ascii=False, indent=1),
                                  encoding="utf-8")


def _load_shots(proj_dir: Path) -> list[dict]:
    from smart_align import shots_of
    return shots_of(proj_dir)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--rate", type=float, default=4.95, help="TTS 语速标定 字/秒")
    ap.add_argument("--sentences", default=None,
                    help="会话内已写好的句子 JSON 路径（[{'ws','we','text'}...]），跳过 LLM 写作")
    a = ap.parse_args()
    proj_dir = Path(a.project).expanduser().resolve().parent
    if a.sentences:
        proj_dir = Path(a.sentences).resolve().parent
    r = narrate(proj_dir, a.rate, sentences_path=a.sentences)
    print(f"[旁白后写] 完成 {r['sents']} 句，覆盖 {r['covered']} → narration.json + align.json")
    print("下一步：r5_tts.py → elastic_fit.py → r6_mix.py → r7_mux.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
