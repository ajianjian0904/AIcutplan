# -*- coding: utf-8 -*-
"""会话内导演编排：导演稿（数据）→ 窗口表 + 逐句绑定（产物）。

v0.10 起按「平时动漫」四集实测配方标定（见 anime-recap/SKILL.md）：

* 旁白 11 字一句、一句一个信息点，配音瞬时语速 7.17 字/秒；
* 旁白段按句落刀（一句一组窗口），原声段按 1.32s 常量等距落刀；
* 导演稿里的空句段 = 原声段（``tier=hold_hl`` / ``ost_mode=original``），
  用于中段短插与唯一一段 30–34s 的「大鱼」；
* 钩子 = 源片连续未裁切爽点段，单独写前置钩子文案，正片原样复用一次；
* 末尾原声留白；块类型按六段式（钩子/回跳/危机/攻略/大鱼/撒手）标注。

``src`` 是 :mod:`aicutplan.contracts` 语义契约的校验依据 —— 「按窗口序号
均匀贴旁白」会因为声明的源区间与绑定窗口对不上而失败。

.. note:: 配方里「一句可跨多刀、切点咬旁白字数」在本链做不到：
   ``narrate_cut`` 令 ``t0 == wins[ws].new_start``、``dur`` 等于窗口组总长，
   一句跨两刀会留下约 1s 句间空隙，反而违反配方「句间空隙≈0」。
   这是上一轮事故后加固的契约约束，本实现选择保留契约，用「原声段多落刀」
   把镜:句与切点密度拉回配方区间，并在验收里如实报告残余偏差。

导演稿 ``director.json`` schema::

    {
      "title": "集标题",
      "hook": [1144, 1160],            # 源片连续爽点段
      "hook_lines": ["钩子文案…", …],   # >=2 句专属钩子文案（11 字短句）
      "tail": [1342, 1352],            # 末尾原声留白
      "span": 11.5,                   # 正片每段取源片秒数
      "chunks": [[2, 13.5, ["句1", …], "回跳"],
                 [700, 732, [], "大鱼"], …]   # 第三项为空 = 原声段
    }

本模块只写工程目录内的 4 个 JSON 与 project.json 的 hook_clip，
不调 ffmpeg、不启子进程、不读工程目录以外的状态。
"""
from __future__ import annotations

import json
from pathlib import Path

RATE = 7.17          # 配方标定：edge-tts YunxiNeural +26% 裁静音后 字/秒
WIN_TARGET = 1.32    # 配方常量：每刀 1.32 秒
MAX_WIN = 2.4        # 配方单镜 p90 <= 2.4s
DEFAULT_SPAN = 11.5

SIX_BEATS = ("钩子", "回跳", "危机", "攻略", "大鱼", "撒手")


def load_spec(path: str | Path) -> dict:
    """读并校验导演稿文件；校验逻辑见 :func:`load_spec_from_dict`。"""
    p = Path(path).expanduser().resolve()
    return load_spec_from_dict(json.loads(p.read_text(encoding="utf-8")))


def load_spec_from_dict(spec: dict) -> dict:
    """校验内存中的导演稿（供单测与程序化编排使用），错误尽早暴露。"""
    for key in ("title", "hook", "hook_lines", "tail", "chunks"):
        if key not in spec:
            raise ValueError(f"导演稿缺字段 {key!r}")
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
        if not (3 <= len(c) <= 4) or not isinstance(c[2], list):
            raise ValueError(f"段{k} 格式应为 [a, b, [句子…](, 类型)]")
        if float(c[1]) <= float(c[0]):
            raise ValueError(f"段{k} 源片区间倒置")
        if c[2] and not all(isinstance(x, str) and x.strip() for x in c[2]):
            raise ValueError(f"段{k} 含空句子")
    return spec


def plan(root: str | Path, spec: dict, *, report: str | Path | None = None) -> dict:
    """把导演稿落成 cutplan / meta / story_plan / sents，返回统计。"""
    root = Path(root).expanduser().resolve()
    d = root / "json"
    d.mkdir(parents=True, exist_ok=True)
    pj = root / "project.json"
    cfg = json.loads(pj.read_text(encoding="utf-8"))
    src_dur = float(cfg["recap"].get("src_dur") or spec["tail"][1])
    span = float(spec.get("span") or DEFAULT_SPAN)

    hook = (float(spec["hook"][0]), float(spec["hook"][1]), list(spec["hook_lines"]), "钩子")
    tail = (float(spec["tail"][0]), float(spec["tail"][1]), [], "撒手")
    # 正片段统一收紧到 span 秒；原声段与钩子同起点段不裁剪，原样复用
    chunks = ([hook] +
              [(float(c[0]),
                float(c[1]) if not c[2] or float(c[0]) == hook[0]
                else min(float(c[1]), float(c[0]) + span),
                list(c[2]), str(c[3]) if len(c) > 3 else "铺垫")
               for c in spec["chunks"]] + [tail])

    wins: list[dict] = []
    meta: list[dict] = []
    sents: list[dict] = []
    beat_span: dict[str, list[float]] = {}
    rows: list[str] = [f"# 会话内导演表 · {spec['title']}", "",
                       "每段 = 源片连续区间；空句段为原声（短插/大鱼）；钩子正片原样复用一次。", ""]
    cursor = 0.0
    total_chars = 0
    over: list[tuple] = []

    for ci, (a, b, lines, typ) in enumerate(chunks):
        T = float(b - a)
        is_hook = ci == 0
        beat_t0 = cursor
        w0_beat = len(wins)

        # 原声段：整段按 1.32s 等距落刀，不写旁白
        if not lines:
            k = max(1, round(T / WIN_TARGET))
            dur = T / k
            for j in range(k):
                sa, se = a + j * dur, a + (j + 1) * dur
                wins.append({
                    "src_start": round(sa, 3), "src_end": round(se, 3),
                    "tier": "hold_hl", "label": typ,
                    "lines": [], "dur": round(dur, 3),
                    "new_start": round(cursor, 3), "new_end": round(cursor + dur, 3),
                    "sent_ids": [], "ost_mode": "original",
                    "reason": f"原声·{typ}（配方：短插 / 30–34s 大鱼） {a:.0f}-{b:.0f}s",
                })
                meta.append({"ev": ci, "beat": ci, "ost_mode": "original"})
                cursor += dur
            beat_span[str(ci)] = [round(beat_t0, 3), round(cursor, 3)]
            rows.append(f"## 段{ci} {typ}·原声 源 {a:.0f}-{b:.0f}s → 成片 "
                        f"{beat_t0:.1f}-{cursor:.1f}s ({k} 窗 / 无旁白)")
            rows.append("")
            continue

        chars = [len(x) for x in lines]
        total_chars += sum(chars)
        ssum = sum(chars) or 1
        slots = [T * c / ssum for c in chars]

        acc = 0.0
        for si, text in enumerate(lines):
            t = slots[si]
            k = max(1, min(8, round(t / WIN_TARGET)))
            dur = t / k
            if dur > MAX_WIN:
                k = max(1, int(t // MAX_WIN) + 1)
                dur = t / k
            w_first = len(wins)
            for j in range(k):
                sa, se = a + acc + j * dur, a + acc + (j + 1) * dur
                wins.append({
                    "src_start": round(sa, 3), "src_end": round(se, 3),
                    "tier": "line", "label": typ,
                    "lines": [], "dur": round(dur, 3),
                    "new_start": round(cursor, 3), "new_end": round(cursor + dur, 3),
                    "sent_ids": [], "ost_mode": "narration",
                    "reason": ("前置爽点·连续未裁切" if is_hook else
                               f"会话内导演·{typ}·连续剧情段") + f" {a:.0f}-{b:.0f}s",
                })
                meta.append({"ev": ci, "beat": ci, "ost_mode": "narration"})
                cursor += dur
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
        rows.append(f"## 段{ci} {typ} 源 {a:.0f}-{b:.0f}s → 成片 {beat_t0:.1f}-{cursor:.1f}s "
                    f"({len(wins)-w0_beat} 窗 / {sum(chars)} 字 / {len(lines)} 句)")
        for s in sents[-len(lines):]:
            rows.append(f"  w{s['ws']:03d}-{s['we']:03d}  {s['text']}")
        rows.append("")

    plan_out = {"windows": wins,
                "stats": {"src_dur": src_dur, "out_dur": round(cursor, 2),
                          "out_dur_actual": round(cursor, 2), "segments": len(wins),
                          "cuts_per_min": round(len(wins) / max(cursor / 60, 0.01), 1),
                          "mode": "session-director-v3"}}
    meta_out = {"meta": meta, "beat_span": beat_span}

    blocks, events, causal = [], [], {}
    for ci, (a, b, lines, typ) in enumerate(chunks):
        t0, t1 = beat_span[str(ci)]
        blocks.append({"label": typ, "type": typ,
                       "t0": t0, "t1": t1, "target": float(b - a),
                       "summary": " | ".join(lines)[:80] if lines else "原声·不插嘴"})
        events.append({"t0": float(a), "t1": float(b), "type": typ, "who": "",
                       "event": " | ".join(lines)[:60], "keep": 6, "seg": 0})
        causal[str(ci)] = {"cause": [ci - 1] if ci else [],
                           "result": [ci + 1] if ci + 1 < len(chunks) else [],
                           "func": typ}
    story = {"segments": [{"i": 0, "a": 0.0, "b": src_dur}],
             "events": events, "causal": causal, "blocks": blocks,
             "seg_tokens": 0, "segment": 180, "frames": 12,
             "target": round(cursor, 1),
             "budget": {"total_sec": round(cursor, 1), "target": round(cursor, 1),
                        "diff": 0.0, "chars": total_chars},
             "block_model": "workbuddy-session-director-v3",
             "source": "embedded-subtitle+frame-grid+session+six-beat",
             "sent_event_map": {}, "sent_event_map_final": {},
             "sent_event_override": {}, "sent_scene_global": {},
             "sent_shot_map": {}}

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
