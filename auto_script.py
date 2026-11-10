# -*- coding: utf-8 -*-
"""自动写稿：跳过人工审核，直接「整稿生成 → 补缺块 → 定稿 → 写回 script.json」

用在「只为跑通全流程」的场景。人审版仍然是 /narration 页面。

流程：
  1. 读 json/script.json 的块骨架（由 /story 的「写 script 骨架」产出，text 为空）
  2. 用 full 模板跑一次整稿（带全部块骨架 + 字数预算 + 全量台词）
  3. 解析成块；缺的块用 beat 模式逐块补（只给该块区间的台词，避免长片截断）
  4. 写草稿 → 标记定稿 → 写回 script.json（自动备份）

只改 script.json 的 beats[].text，不碰 anchor / anchor_new。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import narration as NR


def shim(proj_dir: Path):
    return type("P", (), {"dir": proj_dir, "name": proj_dir.name})()


def clean(text: str) -> str:
    t = (text or "").strip()
    t = t.replace("\n", " ").replace("\r", " ")
    t = t.replace("**", "").strip()
    if t.startswith("[") and "]" in t:
        t = t.split("]", 1)[1].strip()
    return t


def meta_of(text: str) -> dict:
    return {"chars": len(text), "sents": len(NR.split_sent(text))}


def fit_budget(proj_dir: Path, model=None, tolerance: float = 1.06) -> int:
    """按每块的成片秒数把超标文案压回去。超 tolerance 的块逐块重写，其余不动。

    成片时长由 TTS 实测时长决定，所以字数就是时长：超 30% 字 = 超 30% 时长。
    """
    pj = shim(proj_dir)
    beats = NR.script_beats(pj)
    d = NR.draft(pj)
    c = NR.calibration()
    cps = float(c["effective_cps"])
    cov = 0.938
    changed = 0
    print(f"[收敛] 语速 {cps:.2f} 字/秒 · 覆盖率 {cov:.0%} · 容差 {tolerance:.0%}", flush=True)
    for i, b in enumerate(beats):
        if i >= len(d["beats"]):
            break
        cap = max(40, int(float(b.get("target") or 20) * cov * cps))
        text = d["beats"][i].get("text") or ""
        if len(text) <= cap * tolerance:
            continue
        want = int(cap * 0.95)
        print(f"[收敛] 块 {i:02d} {b.get('beat')}：{len(text)} → ≤{want} 字", flush=True)
        try:
            v = NR.build_vars(pj, "beat", i, NR.VAR_NAMES,
                              f"把这段旁白压缩到 {want} 字以内（现在是 {len(text)} 字）。"
                              f"保留全部关键事件与人名，删掉形容、情绪铺垫和重复信息；"
                              f"只输出压缩后的文案本身，不要编号、不要解释。")
            tpl = NR.get_template("beat")
            p = NR.render(tpl, v)
            r = NR.llm_chat(p["system"], p["user"], model=model, temperature=0.5, timeout=600)
            got = NR.parse_reply(r["text"], beats, i)
            txt = clean(got["items"][0]["text"]) if got.get("items") else ""
            if not txt:
                raise RuntimeError("空返回")
            if len(txt) > cap * 1.15:
                print(f"[收敛]   仍偏长（{len(txt)} 字），保留原稿", flush=True)
                continue
            d["beats"][i]["text"] = txt
            d["beats"][i]["edited"] = True
            changed += 1
            print(f"[收敛]   → {len(txt)} 字：{txt[:50]}", flush=True)
        except Exception as e:
            print(f"[收敛]   失败：{e}", flush=True)
    if not changed:
        print("[收敛] 全部在预算内，无需改动", flush=True)
        return 0
    NR.save_draft(pj, d["beats"], note="auto_script fit_budget")
    total = sum(len(x["text"]) for x in d["beats"])
    secs = total / cps
    print(f"[收敛] 改了 {changed} 块 · 总字数 {total} · 预计语音 {secs:.0f}s", flush=True)
    return changed


def expand_budget(proj_dir: Path, model=None, low: float = 0.82) -> int:
    """对明显短于预算的块扩写（AI 写稿普遍偏保守，成片会因此短一截）。

    只在 len < cap*low 时动，扩到 cap 的 95%，避免来回震荡。
    """
    pj = shim(proj_dir)
    beats = NR.script_beats(pj)
    d = NR.draft(pj)
    cps = float(NR.calibration()["effective_cps"])
    cov = 0.938
    changed = 0
    print(f"[补足] 语速 {cps:.2f} 字/秒 · 低于预算 {low:.0%} 才扩", flush=True)
    for i, b in enumerate(beats):
        if i >= len(d["beats"]):
            break
        cap = max(40, int(float(b.get("target") or 20) * cov * cps))
        text = d["beats"][i].get("text") or ""
        if len(text) >= cap * low:
            continue
        want = int(cap * 0.95)
        print(f"[补足] 块 {i:02d} {b.get('beat')}：{len(text)} → {want} 字", flush=True)
        try:
            v = NR.build_vars(pj, "beat", i, NR.VAR_NAMES,
                              f"这段旁白现在 {len(text)} 字，偏短，扩写到 {want} 字左右。"
                              f"只补具体动作、表情、对话里的关键信息，不要加空泛评价，不要重复已有句子，"
                              f"不要编造骨架里没有的事件。只输出文案本身。")
            tpl = NR.get_template("beat")
            p = NR.render(tpl, v)
            r = NR.llm_chat(p["system"], p["user"], model=model, temperature=0.6, timeout=600)
            got = NR.parse_reply(r["text"], beats, i)
            txt = clean(got["items"][0]["text"]) if got.get("items") else ""
            if not txt or len(txt) > cap * 1.12:
                print(f"[补足]   长度不合（{len(txt)} 字），保留原稿", flush=True)
                continue
            d["beats"][i]["text"] = txt
            d["beats"][i]["edited"] = True
            changed += 1
            print(f"[补足]   → {len(txt)} 字", flush=True)
        except Exception as e:
            print(f"[补足]   失败：{e}", flush=True)
    if not changed:
        print("[补足] 无需要扩写的块", flush=True)
        return 0
    NR.save_draft(pj, d["beats"], note="auto_script expand_budget")
    total = sum(len(x["text"]) for x in d["beats"])
    print(f"[补足] 改了 {changed} 块 · 总字数 {total} · 预计语音 {total / cps:.0f}s", flush=True)
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--template", default="full")
    ap.add_argument("--model", default=None)
    ap.add_argument("--note", default="")
    ap.add_argument("--no-writeback", action="store_true", help="只写草稿，不定稿也不写回")
    ap.add_argument("--fit", action="store_true", help="不重新写稿，只按预算压缩已有的草稿")
    ap.add_argument("--expand", action="store_true", help="对明显偏短的块扩写到预算")
    a = ap.parse_args(argv)

    pj = shim(Path(a.project).expanduser().resolve().parent)
    if a.expand:
        n = expand_budget(pj.dir, model=a.model)
        if n and not a.no_writeback:
            NR.set_finalized(pj, True)
            print("[补足] 写回：", NR.apply_to_script(pj).get("changed"), "块", flush=True)
        return 0
    if a.fit:
        n = fit_budget(pj.dir, model=a.model)
        if n and not a.no_writeback:
            NR.set_finalized(pj, True)
            print("[收敛] 写回：", NR.apply_to_script(pj).get("changed"), "块", flush=True)
        return 0
    beats = NR.script_beats(pj)
    if not beats:
        print("[写稿] 没有 script.json 块骨架，先在剧情台「写 script 骨架」", flush=True)
        return 2
    labels = [b.get("beat") or f"块{i}" for i, b in enumerate(beats)]
    print(f"[写稿] {len(beats)} 块：{' / '.join(labels)}", flush=True)

    tpl = NR.get_template(a.template)
    note = a.note or "严格按块输出，每块一行，块内不要换行；块数与骨架一一对应。"
    t0 = time.time()
    print(f"[写稿] 整稿生成（模板 {tpl['id']} · 模型 {a.model or NR.llm_config()['model']}）", flush=True)
    v = NR.build_vars(pj, tpl["id"], None, NR.VAR_NAMES, note)
    p = NR.render(tpl, v)
    print(f"[写稿] 提示词 {len(p['user'])} 字 · 预算 {v.get('_budget', {}).get('total', '?')} 字", flush=True)
    r = NR.llm_chat(p["system"], p["user"], model=a.model, temperature=0.75, timeout=900)
    parsed = NR.parse_reply(r["text"], beats, None)
    texts = {it["id"]: clean(it["text"]) for it in parsed["items"] if it.get("text")}
    print(f"[写稿] 解析出 {len(texts)}/{len(beats)} 块 · {round(time.time() - t0, 1)}s · "
          f"{r['usage'].get('total_tokens')} tokens", flush=True)

    missing = [i for i in range(len(beats)) if not texts.get(i)]
    if missing:
        print(f"[写稿] 缺 {len(missing)} 块，逐块补：{missing}", flush=True)
        for i in missing:
            try:
                v2 = NR.build_vars(pj, tpl["id"], i, NR.VAR_NAMES,
                                   note + "；只写这一块，不要输出其他块")
                p2 = NR.render(tpl, v2)
                r2 = NR.llm_chat(p2["system"], p2["user"], model=a.model,
                                 temperature=0.75, timeout=600)
                got = NR.parse_reply(r2["text"], beats, i)
                txt = clean(got["items"][0]["text"]) if got.get("items") else ""
                if not txt:
                    raise RuntimeError("空返回")
                texts[i] = txt
                print(f"[写稿]   块 {i} {labels[i]} 补到 {len(txt)} 字", flush=True)
            except Exception as e:
                print(f"[写稿]   块 {i} {labels[i]} 失败：{e}", flush=True)

    still = [i for i in range(len(beats)) if not texts.get(i)]
    if still:
        print(f"[写稿] 仍有 {len(still)} 块为空，已中止（不写空稿）", flush=True)
        return 3

    out = [{"id": i, "label": labels[i], "text": texts[i], "source": "AI", "edited": False}
           for i in range(len(beats))]
    total = sum(len(x["text"]) for x in out)
    sents = sum(len(NR.split_sent(x["text"])) for x in out)
    budget = v.get("_budget", {}).get("total")
    print(f"[写稿] 总字数 {total}（预算 {budget}）· {sents} 句 · "
          f"每块 {' '.join(str(len(x['text'])) for x in out)}", flush=True)
    for i, x in enumerate(out):
        print(f"    [{i:02d} {x['label']}] {x['text'][:60]}", flush=True)

    NR.save_draft(pj, out, note=f"auto_script {time.strftime('%Y-%m-%d %H:%M')}")
    print("[写稿] 草稿已保存", flush=True)
    if a.no_writeback:
        return 0
    NR.set_finalized(pj, True)
    res = NR.apply_to_script(pj)
    print(f"[写稿] 定稿并写回：{res.get('changed')} 块变更 · 备份 {res.get('backup')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
