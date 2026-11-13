# -*- coding: utf-8 -*-
"""会话内导演编排：导演稿（数据）→ 窗口表 + 逐句绑定（产物）。

从作品库 `_batch/director.py` 迁入仓库并去掉工程库硬编码，逻辑保持一致：

* 按源片剧情顺序挑连续段，段内按时间序切 1.2–3.5s 窗口；
* 每段对着该段画面写 2–3 句，句子自带 ``ws/we`` 与 ``src``（源片区间）；
* 句长按字数比例分配槽长，天然落在窗口时长内；
* 钩子 = 源片连续未裁切爽点段，单独写前置钩子文案，正片原样复用一次；
* 末尾原声留白。

``src`` 是 :mod:`aicutplan.contracts` 语义契约的校验依据 —— 「按窗口序号
均匀贴旁白」会因为声明的源区间与绑定窗口对不上而失败。

导演稿 ``director.json`` schema::

    {
      "title": "集标题",
      "hook": [1144, 1160],            # 源片连续爽点段
      "hook_lines": ["钩子文案…", …],   # >=2 句专属钩子文案
      "tail": [1342, 1352],            # 末尾原声留白
      "span": 14.0,                   # 正片每段取源片秒数（默认 14）
      "chunks": [[2, 18, ["句1","句2","句3"]], …]
    }

本模块只写工程目录内的 4 个 JSON 与 project.json 的 hook_clip，
不调 ffmpeg、不启子进程、不读工程目录以外的状态。
"""
from __future__ import annotations

import json
from pathlib import Path

RATE = 4.95          # TTS 标定 字/秒
WIN_TARGET = 2.2     # 单窗目标时长
MAX_WIN = 3.5
DEFAULT_SPAN = 14.0


def load_spec(path: str | Path) -> dict:
    """读并校验导演稿结构，错误尽早暴露，不要等跑完 cut.py 才发现。"""
    p = Path(path).expanduser().resolve()
    spec = json.loads(p.read_text(encoding="utf-8"))
    for key in ("title", "hook", "hook_lines", "tail", "chunks"):
        if key not in spec:
            raise ValueError(f"导演稿缺字段 {key!r}：{p}")
    if not (isinstance(spec["hook"], (list, tuple)) and len(spec["hook"]) == 2
            and float(spec["hook"][1]) > float(spec["hook"][0])):
        raise ValueError("hook 必须是合法的 [起, 止] 源片区间")
    if not (isinstance(spec["tail"], (list, tuple)) and len(spec["tail"]) == 2
            and float(spec["tail"][1]) > float(spec["tail"][0])):
        raise ValueError("tail 必须是合法的 [起, 止] 源片区间")
    if len(spec["hook_lines"]) < 2:
        raise ValueError("hook_lines 至少要 2 句专属钩子文案")
    if not spec["chunks"]:
        raise ValueError("chunks 不能为空")
    for k, c in enumerate(spec["chunks"]):
        if len(c) != 3 or not c[2]:
            raise ValueError(f"段{k} 格式应为 [a, b, [句子…]] 且句子非空")
        if float(c[1]) <= float(c[0]):
            raise ValueError(f"段{k} 源片区间倒置")
    return spec


def plan(root: str | Path, spec: dict, *, report: str | Path | None = None) -> dict:
    """把导演稿落成 cutplan / meta / story_plan / sents，返回统计。

    ``root`` 是工程根（project.json 所在目录）。``report`` 给定时额外写一份
    可审阅的导演表 markdown（不写则不产生额外文件，便于单测）。
    """
    root = Path(root).expanduser().resolve()
    d = root / "json"
    d.mkdir(parents=True, exist_ok=True)
    pj = root / "project.json"
    cfg = json.loads(pj.read_text(encoding="utf-8"))
    src_dur = float(cfg["recap"].get("src_dur") or spec["tail"][1])
    span = float(spec.get("span") or DEFAULT_SPAN)

    hook = (float(spec["hook"][0]), float(spec["hook"][1]), list(spec["hook_lines"]))
    tail = (float(spec["tail"][0]), float(spec["tail"][1]), [])
    # 正片段统一收紧到 span 秒；与钩子同起点的那段必须原样复用，不裁剪
    chunks = ([hook] +
              [(float(c[0]), float(c[1]) if float(c[0]) == hook[0]
                else min(float(c[1]), float(c[0]) + span), list(c[2]))
               for c in spec["chunks"]] + [tail])

    wins: list[dict] = []
    meta: list[dict] = []
    sents: list[dict] = []
    beat_span: dict[str, list[float]] = {}
    rows: list[str] = [f"# 会话内导演表 · {spec['title']}", "",
                       "每段 = 源片连续区间；句子按顺序绑定该段窗口；钩子正片原样复用一次。", ""]
    cursor = 0.0
    total_chars = 0
    over: list[tuple] = []

    for ci, (a, b, lines) in enumerate(chunks):
        T = float(b - a)
        is_hook, is_tail = ci == 0, not lines
        beat_t0 = cursor
        w0_beat = len(wins)
        if lines:
            chars = [len(x) for x in lines]
            total_chars += sum(chars)
            ssum = sum(chars) or 1
            slots = [T * c / ssum for c in chars]
        else:
            slots = [T]

        acc = 0.0
        for si, text in enumerate(lines or [None]):
            t = slots[si]
            k = 1 if is_tail else max(1, min(8, round(t / WIN_TARGET)))
            dur = t / k
            if dur > MAX_WIN:
                k = max(1, int(t // MAX_WIN) + 1)
                dur = t / k
            w_first = len(wins)
            for j in range(k):
                sa, se = a + acc + j * dur, a + acc + (j + 1) * dur
                wins.append({
                    "src_start": round(sa, 3), "src_end": round(se, 3),
                    "tier": "hold_hl" if is_tail else "line",
                    "label": "钩子" if is_hook else ("收尾" if is_tail else f"段{ci}"),
                    "lines": [], "dur": round(dur, 3),
                    "new_start": round(cursor, 3), "new_end": round(cursor + dur, 3),
                    "sent_ids": [],
                    "ost_mode": "original" if is_tail else "narration",
                    "reason": ("前置爽点·连续未裁切" if is_hook else
                               "会话内导演·连续剧情段" if not is_tail else "结尾原声留白")
                              + f" {a:.0f}-{b:.0f}s",
                })
                meta.append({"ev": ci, "beat": ci,
                             "ost_mode": "original" if is_tail else "narration"})
                cursor += dur
            if text is not None:
                sents.append({
                    "ws": w_first, "we": len(wins) - 1, "text": text,
                    # 契约字段：该句对应窗口的源片区间
                    "src": [round(wins[w_first]["src_start"], 3),
                            round(wins[len(wins) - 1]["src_end"], 3)],
                })
                if len(text) / RATE > t + 0.05:
                    over.append((ci, text, round(len(text) / RATE, 2), round(t, 2)))
            acc += t

        beat_span[str(ci)] = [round(beat_t0, 3), round(cursor, 3)]
        rows.append(f"## 段{ci} {'钩子' if is_hook else '原声留白' if is_tail else '正片'} "
                    f"源 {a:.0f}-{b:.0f}s → 成片 {beat_t0:.1f}-{cursor:.1f}s "
                    f"({len(wins)-w0_beat} 窗 / {sum(len(x) for x in lines) if lines else 0} 字)")
        if lines:
            for s in sents[-len(lines):]:
                rows.append(f"  w{s['ws']:03d}-{s['we']:03d}  {s['text']}")
        rows.append("")

    plan_out = {"windows": wins,
                "stats": {"src_dur": src_dur, "out_dur": round(cursor, 2),
                          "out_dur_actual": round(cursor, 2), "segments": len(wins),
                          "cuts_per_min": round(len(wins) / max(cursor / 60, 0.01), 1),
                          "mode": "session-director"}}
    meta_out = {"meta": meta, "beat_span": beat_span}

    blocks, events, causal = [], [], {}
    for ci, (a, b, lines) in enumerate(chunks):
        typ = "钩子" if ci == 0 else ("收尾" if ci == len(chunks) - 1 else
                                     "爆发" if ci in (7, len(chunks) - 6) else "铺垫")
        blocks.append({"label": "钩子" if ci == 0 else f"段{ci}", "type": typ,
                       "t0": beat_span[str(ci)][0], "t1": beat_span[str(ci)][1],
                       "target": float(b - a),
                       "summary": " | ".join(lines)[:80] if lines else "原声留白"})
        events.append({"t0": float(a), "t1": float(b), "type": typ, "who": "",
                       "event": " | ".join(lines)[:60], "keep": 6, "seg": 0})
        causal[str(ci)] = {"cause": [ci - 1] if ci else [],
                           "result": [ci + 1] if ci + 1 < len(chunks) else [],
                           "func": "爆发" if typ == "爆发" else "铺垫"}
    story = {"segments": [{"i": 0, "a": 0.0, "b": src_dur}],
             "events": events, "causal": causal, "blocks": blocks,
             "seg_tokens": 0, "segment": 180, "frames": 12,
             "target": round(cursor, 1),
             "budget": {"total_sec": round(cursor, 1), "target": round(cursor, 1),
                        "diff": 0.0, "chars": total_chars},
             "block_model": "workbuddy-session-director",
             "source": "embedded-subtitle+frame-grid+session",
             "sent_event_map": {}, "sent_event_map_final": {},
             "sent_event_override": {}, "sent_scene_global": {}, "sent_shot_map": {}}

    (d / "cutplan.json").write_text(json.dumps(plan_out, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "cutplan_meta.json").write_text(json.dumps(meta_out, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "story_plan.json").write_text(json.dumps(story, ensure_ascii=False, indent=1), encoding="utf-8")
    # sents.json 必须在工程根：narrate --sentences 会把句子稿所在目录当工程根
    (root / "sents.json").write_text(json.dumps(sents, ensure_ascii=False, indent=1), encoding="utf-8")

    cfg["recap"]["hook_clip"] = [hook[0], hook[1]]
    pj.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")

    if report:
        rows.insert(3, f"共 {len(wins)} 窗 / {len(sents)} 句 / {total_chars} 字 / "
                       f"{cursor:.1f}s / 密度 {total_chars/max(cursor,0.01):.2f} 字每秒")
        if over:
            rows.append(f"!! 超槽风险 {len(over)} 句：{over[:5]}")
        Path(report).write_text("\n".join(rows), encoding="utf-8")

    return {"windows": len(wins), "sents": len(sents), "chars": total_chars,
            "seconds": round(cursor, 1), "density": round(total_chars / max(cursor, 0.01), 2),
            "over": len(over), "hook_clip": [hook[0], hook[1]]}
