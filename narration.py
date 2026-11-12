#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [L1 共享库] 职责：旁白数据层/预算门禁/LLM 客户端 ｜ 调用者：server; smart_align; narrate_cut ｜ 产物：库（无独立产物）
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""旁白工作台 · 数据层、提示词模板、DeepSeek 客户端、只读校验、阶段门禁

设计约束：
  * API Key 只从环境变量 DEEPSEEK_API_KEY 读，绝不写入任何文件、日志或异常。
  * 草稿/快照只写工作区 drafts/。唯一例外是 apply_to_script()：用户显式点
    「写回 script.json」时才改工程文件，且只改 beats[].text 并自动备份。
  * 定稿是独立动作；未定稿时锚点校对台写回被锁，避免白做。
  * 7 步重跑脚本只生成/展示，界面绝不代为执行渲染与配音。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DRAFTS = ROOT / "drafts"
TEMPLATES_FILE = ROOT / "templates.json"
CALIB_FILE = ROOT / "tts_calibration.json"
LLM_CONFIG_FILE = ROOT / "llm_config.json"
STYLE_MD = Path(r"C:/Users/10451/.workbuddy/skills/anime-recap/references/style.md")

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

VAR_NAMES = ["style_rules", "beats_skeleton", "asr_context", "char_budget",
             "current_text", "beat_label", "user_note"]

BUILTIN_TEMPLATES = [
    {
        "id": "outline", "name": "剧情梳理 / 六段式", "stage": "L1", "builtin": True,
        "system": "你是抖音动漫速讲的编导。只做剧情结构梳理，不写完整旁白。输出中文，简洁。",
        "user": "请依据下面这一集的素材，按「钩子 / 回跳 / 危机 / 攻略 / 大鱼 / 撒手」六段式梳理剧情，\n"
                "并指出哪一段适合作为 30–34 秒不打岔的完整原声高潮。\n\n"
                "【文案规则】\n{style_rules}\n\n【剧情块骨架】\n{beats_skeleton}\n\n"
                "【本集人物台词（带时间轴）】\n{asr_context}\n\n【补充要求】\n{user_note}",
    },
    {
        "id": "full", "name": "整稿生成 v1", "stage": "L1", "builtin": True,
        "system": "你是抖音动漫速讲编导，复刻「平时动漫」式速讲。严格按用户给的格式输出，不要加任何解释、标题或总结。",
        "user": "请为这一集写完整的解说旁白，按给定剧情块逐块输出。\n\n"
                "【文案规则（必须遵守）】\n{style_rules}\n\n"
                "【剧情块骨架与字数预算】\n{beats_skeleton}\n\n"
                "【总字数预算】{char_budget}\n\n"
                "【本集人物台词（带时间轴）】\n{asr_context}\n\n"
                "【现有稿（可改写、可保留，别照抄）】\n{current_text}\n\n"
                "【补充要求】\n{user_note}\n\n"
                "输出格式严格如下，每块一行，块内不要换行，不要 markdown、不要编号以外的符号：\n"
                "[00 钩子] 文案\n[01 前情] 文案\n……一直到最后一块。",
    },
    {
        "id": "beat", "name": "单块改写", "stage": "L1", "builtin": True,
        "system": "你是抖音动漫速讲编导。只改写用户指定的那一个剧情块，输出纯正文，不要解释、不要标题、不要引号。",
        "user": "改写剧情块「{beat_label}」。\n\n【文案规则】\n{style_rules}\n\n"
                "【前后块骨架（保持剧情连贯）】\n{beats_skeleton}\n\n"
                "【本块对应的人物台词】\n{asr_context}\n\n"
                "【本块字数预算】{char_budget}\n\n【现有稿】\n{current_text}\n\n"
                "【补充要求】\n{user_note}",
    },
    {
        "id": "polish", "name": "句子润色（≤14 字）", "stage": "L2", "builtin": True,
        "system": "你是短视频字幕编辑。只润色文字，不增加信息，输出纯正文一行。",
        "user": "把这句旁白改得更顺口，保持原意，不超过 14 个汉字，去掉标点。\n"
                "【文案规则】\n{style_rules}\n\n【原句】\n{current_text}\n\n【补充要求】\n{user_note}",
    },
    {
        "id": "lint", "name": "规则自检 / 审稿", "stage": "L1", "builtin": True,
        "system": "你是文案审稿人。只挑毛病，不改写、不重写全文。按条列出，每条指明块号和具体问题。",
        "user": "按文案规则审这份旁白稿，重点看：句长是否超 14 字、转折词密度、反转词间隔、\n"
                "主角是否报了名字、是否有总结陈词、开场是否先给超规格结果。\n\n"
                "【文案规则】\n{style_rules}\n\n【剧情块骨架】\n{beats_skeleton}\n\n"
                "【待审稿】\n{current_text}\n\n【补充要求】\n{user_note}",
    },
]

TURN_WORDS = list("而 却 但 可 所以 于是 结果 因为 毕竟 只是 反而 原来 没想到 殊不知 至于 要是 刚好".split())
TWIST_WORDS = ["殊不知", "没想到", "原来", "竟", "偏偏", "反而", "岂知"]


# ---------------------------------------------------------------- 基础
def pkey(project_dir: str) -> str:
    return hashlib.md5(str(Path(project_dir).resolve()).encode()).hexdigest()[:12]


def ddir(project_dir: str) -> Path:
    d = DRAFTS / pkey(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "snapshots").mkdir(exist_ok=True)
    return d


def _read(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(p: Path, obj):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- 模板
def templates() -> list:
    saved = _read(TEMPLATES_FILE, None)
    if saved is None:
        saved = [dict(t) for t in BUILTIN_TEMPLATES]
        _write(TEMPLATES_FILE, saved)
    return saved


def save_template(tpl: dict) -> list:
    items = templates()
    tpl = dict(tpl)
    tpl.setdefault("builtin", False)
    tpl.setdefault("stage", "L1")
    if not tpl.get("id"):
        tpl["id"] = "tpl_" + hashlib.md5(
            (tpl.get("name", "") + datetime.now().isoformat()).encode()).hexdigest()[:8]
    for i, t in enumerate(items):
        if t["id"] == tpl["id"]:
            if t.get("builtin"):
                tpl["id"] = tpl["id"] + "_copy"
                tpl["builtin"] = False
                items.append(tpl)
            else:
                items[i] = tpl
            break
    else:
        items.append(tpl)
    _write(TEMPLATES_FILE, items)
    return items


def get_template(tid: str) -> dict:
    for t in templates():
        if t["id"] == tid:
            return t
    raise ValueError(f"模板不存在：{tid}")


# ---------------------------------------------------------------- 标定
def calibration() -> dict:
    c = _read(CALIB_FILE, None)
    if c:
        return c
    return {"effective_cps": 4.955, "fixed_silence": 0.733,
            "instant_cps": 8.20, "source": "EP05 默认实测值（未重新标定）"}


def recalibrate(proj) -> dict:
    f = proj.dir / "json" / "tts-result.json"
    if not f.exists():
        raise ValueError("缺少 json/tts-result.json，无法标定")
    d = json.loads(f.read_text(encoding="utf-8"))
    it = [x for x in d["items"] if x.get("tts_dur")]
    C = sum(x["chars"] for x in it)
    T = sum(x["tts_dur"] for x in it)
    n = len(it)
    sx, sy = C, T
    sxx = sum(x["chars"] ** 2 for x in it)
    sxy = sum(x["chars"] * x["tts_dur"] for x in it)
    b = (n * sxy - sx * sy) / max(1e-9, n * sxx - sx * sx)
    a = (sy - b * sx) / n
    out = {
        "effective_cps": round(C / T, 4),
        "fixed_silence": round(a, 4),
        "instant_cps": round(1 / b, 3),
        "voice": d.get("voice"), "rate": d.get("rate"),
        "sentences": n, "chars": C, "speech_sec": round(T, 2),
        "source": str(f), "calibrated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write(CALIB_FILE, out)
    return out


# ---------------------------------------------------------------- 草稿 / 快照 / 门禁
def script_beats(proj) -> list:
    f = proj.dir / "json" / "script.json"
    if not f.exists():
        return []
    return json.loads(f.read_text(encoding="utf-8")).get("beats", [])


def draft(proj, create=True) -> dict:
    f = ddir(proj.dir) / "draft.json"
    d = _read(f, None)
    if d is None:
        beats = []
        for i, b in enumerate(script_beats(proj)):
            beats.append({"id": i, "label": b.get("beat", f"块{i}"),
                          "text": b.get("text", ""), "source": "原稿", "edited": False})
        d = {"beats": beats, "created": datetime.now().isoformat(timespec="seconds"),
             "updated": "", "note": ""}
        if create:
            _write(f, d)
    return d


def save_draft(proj, beats: list, note: str = "") -> dict:
    d = draft(proj)
    # 定稿后继续改稿 → 自动回到草稿态，重新锁住锚点页写回。
    # 否则用户改了文案，锚点页却以为文案没变，会带着旧 cutplan 继续写回。
    if state(proj).get("finalized"):
        set_finalized(proj, False)
    d["beats"] = beats
    d["updated"] = datetime.now().isoformat(timespec="seconds")
    if note:
        d["note"] = note
    _write(ddir(proj.dir) / "draft.json", d)
    return d


def state(proj) -> dict:
    f = ddir(proj.dir) / "state.json"
    return _read(f, {"finalized": False, "finalized_at": None, "revisions": []})


def set_finalized(proj, value: bool) -> dict:
    s = state(proj)
    s["finalized"] = bool(value)
    s["finalized_at"] = datetime.now().isoformat(timespec="seconds") if value else None
    if value:
        s["revisions"] = (s.get("revisions") or []) + [s["finalized_at"]]
    _write(ddir(proj.dir) / "state.json", s)
    return s


def has_unfinalized_draft(project_dir: str) -> bool:
    """只有用户真的建过草稿才锁锚点页，避免影响既有工作流。"""
    d = DRAFTS / pkey(project_dir)
    if not (d / "draft.json").exists():
        return False
    return not bool(_read(d / "state.json", {}).get("finalized"))


def discard(proj) -> dict:
    d = ddir(proj.dir)
    for n in ("draft.json", "state.json"):
        p = d / n
        if p.exists():
            p.unlink()
    return {"ok": True, "kept_snapshots": len(list_snapshots(proj))}


def make_snapshot(proj, label: str) -> dict:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", label)[:24] or "snap"
    f = ddir(proj.dir) / "snapshots" / f"{ts}_{safe}.json"
    _write(f, {"label": label, "at": ts, "draft": draft(proj), "state": state(proj)})
    return {"file": str(f), "name": f.name, "label": label, "at": ts}


def list_snapshots(proj) -> list:
    d = ddir(proj.dir) / "snapshots"
    return [{"name": p.name, "label": _read(p, {}).get("label", p.stem),
             "at": _read(p, {}).get("at", "")}
            for p in sorted(d.glob("*.json"), reverse=True)]


def restore_snapshot(proj, name: str) -> dict:
    f = ddir(proj.dir) / "snapshots" / name
    if not f.exists():
        raise ValueError("快照不存在")
    snap = _read(f, {})
    d = snap.get("draft")
    if not d:
        raise ValueError("快照内容为空")
    _write(ddir(proj.dir) / "draft.json", d)
    _write(ddir(proj.dir) / "state.json", {"finalized": False, "finalized_at": None,
                                           "revisions": state(proj).get("revisions", [])})
    return d


# ---------------------------------------------------------------- 上下文
def style_rules() -> str:
    if STYLE_MD.exists():
        return STYLE_MD.read_text(encoding="utf-8")[:9000]
    return "11 字一句，一句一个信息点；每 4–5 秒一个转折词；每 19–29 秒一个反转词；主角不报名字；开场先给超规格结果。"


def beat_src_ranges(proj) -> list:
    """从 align.beat_win + cutplan.windows 取每块真实的源片区间（不能用 script.anchor）。"""
    al = proj.dir / "json" / "align.json"
    plan = proj.dir / "json" / "cutplan.json"
    if not (al.exists() and plan.exists()):
        return []
    bw = json.loads(al.read_text(encoding="utf-8"))["beat_win"]
    wins = json.loads(plan.read_text(encoding="utf-8"))["windows"]
    out = []
    for k in sorted(bw, key=int):
        i0, i1 = bw[k]
        seg = wins[i0:i1]
        if seg:
            out.append({"id": int(k), "src": [min(w["src_start"] for w in seg),
                                              max(w["src_end"] for w in seg)],
                        "new": [seg[0]["new_start"], seg[-1]["new_end"]]})
    return out


def asr_segments(proj) -> list:
    """优先读中文台词时间轴（工作区 asr/<hash>/asr_zh.json），未译段落已回退原文。

    下游 asr_for_range() 只用 t0/t1/text，因此这里做结构归一化即可，
    模板变量 {asr_context} 会自动从「日语原文」切换成「中文台词」。
    """
    try:
        import asr_pipeline as AP
        out = AP.out_path(proj)
        if out.is_file():
            paras = json.loads(out.read_text(encoding="utf-8")).get("paras", [])
            if paras:
                return [{"t0": p.get("t0"), "t1": p.get("t1"),
                         "text": p.get("text") or p.get("text_orig", "")}
                        for p in paras]
    except Exception:
        pass
    # 回退：工程目录里的原始 ASR
    for name in ("asr_ja.json", "asr_en.json", "asr.json"):
        f = proj.dir / name
        if f.is_file():
            paras = json.loads(f.read_text(encoding="utf-8")).get("paras", [])
            if paras and isinstance(paras[0], (list, tuple)):
                paras = [{"t0": x[0], "t1": x[1], "text": x[3]}
                         for x in paras if len(x) >= 4]
            return paras
    return []


def asr_for_range(paras, a, z, limit=4200) -> str:
    rows = [p for p in paras if p["t1"] >= a and p["t0"] <= z]
    buf, total = [], 0
    for p in rows:
        line = f"[{p['t0']:.0f}-{p['t1']:.0f}s] {p['text']}"
        if total + len(line) > limit:
            buf.append("…（已截断）")
            break
        buf.append(line)
        total += len(line)
    return "\n".join(buf) if buf else "（该区间无可用台词）"


def skeleton_text(beats, ranges, budget_each=None) -> str:
    lines = []
    for i, b in enumerate(beats):
        r = ranges[i] if i < len(ranges) else None
        seg = f"成片 {r['new'][0]:.1f}–{r['new'][1]:.1f}s" if r else ""
        extra = f" / 预算 {budget_each[i]} 字" if budget_each and i < len(budget_each) else ""
        lines.append(f"[{i:02d} {b['label']}] {seg} / 现稿 {len(b['text'])} 字{extra}")
    return "\n".join(lines)


def char_budget(proj, beats, calib) -> dict:
    try:
        out = float(proj.stats.get("out_dur_actual") or proj.stats.get("out_dur") or 323.5)
    except Exception:
        out = 323.5
    cov = 0.938
    nf = proj.dir / "json" / "narration.json"
    if nf.exists():
        cov = (json.loads(nf.read_text(encoding="utf-8")).get("stats", {}).get("coverage_pct", 93.8)) / 100
    total = round(out * cov * calib["effective_cps"])
    each = []
    lens = [len(b["text"]) for b in beats]
    s = sum(lens) or 1
    for L in lens:
        each.append(round(total * L / s))
    return {"total": total, "each": each, "out_dur": round(out, 2),
            "coverage": round(cov * 100, 1), "cps": calib["effective_cps"]}


def build_vars(proj, tid, beat, vars_on, user_note, mode="draft") -> dict:
    d = draft(proj, create=False)   # 预览提示词不算改动，不触发阶段门禁
    beats = d["beats"]
    ranges = beat_src_ranges(proj)
    paras = asr_segments(proj)
    calib = calibration()
    budget = char_budget(proj, beats, calib)

    if beat is None:
        asr_parts = []
        for i, r in enumerate(ranges):
            seg = asr_for_range(paras, r["src"][0], r["src"][1], limit=700)
            label = beats[i]["label"] if i < len(beats) else f"块{i}"
            asr_parts.append(f"### [{i:02d} {label}] 源片 {r['src'][0]:.0f}–{r['src'][1]:.0f}s\n{seg}")
        asr_ctx = "\n".join(asr_parts)[:14000]
        cur = "\n".join(f"[{i:02d} {b['label']}] {b['text']}" for i, b in enumerate(beats))
        bud = f"{budget['total']} 字（成片 {budget['out_dur']}s × 覆盖 {budget['coverage']}% × 有效语速 {budget['cps']} 字/秒）"
        skel = skeleton_text(beats, ranges, budget["each"])
        blabel = "整集"
    else:
        i = int(beat)
        r = ranges[i] if i < len(ranges) else None
        asr_ctx = asr_for_range(paras, r["src"][0], r["src"][1], limit=6000) if r else "（无区间信息）"
        cur = beats[i]["text"] if i < len(beats) else ""
        bud = f"{budget['each'][i] if i < len(budget['each']) else '—'} 字"
        lo, hi = max(0, i - 2), min(len(beats), i + 3)
        skel = "\n".join(f"[{j:02d} {beats[j]['label']}] {beats[j]['text'][:60]}"
                         for j in range(lo, hi))
        blabel = beats[i]["label"] if i < len(beats) else f"块{i}"

    allv = {
        "style_rules": style_rules(),
        "beats_skeleton": skel,
        "asr_context": asr_ctx,
        "char_budget": bud,
        "current_text": cur,
        "beat_label": blabel,
        "user_note": user_note or "（无）",
    }
    return {k: (allv[k] if k in (vars_on or VAR_NAMES) else "") for k in VAR_NAMES} | {
        "_budget": budget, "_mode": mode, "_beat": beat, "_blabel": blabel}


def render(tpl: dict, v: dict) -> dict:
    def sub(s):
        for k in VAR_NAMES:
            s = s.replace("{" + k + "}", str(v.get(k, "")))
        return s
    return {"system": sub(tpl.get("system", "")), "user": sub(tpl.get("user", ""))}


# ---------------------------------------------------------------- DeepSeek
def llm_config() -> dict:
    c = _read(LLM_CONFIG_FILE, {})
    return {
        "base_url": os.environ.get("DEEPSEEK_BASE_URL") or c.get("base_url") or DEFAULT_BASE_URL,
        "model": os.environ.get("DEEPSEEK_MODEL") or c.get("model") or DEFAULT_MODEL,
        "temperature": float(c.get("temperature", 0.7)),
        "key_var": "DEEPSEEK_API_KEY",
        "key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
    }


def save_llm_config(d: dict) -> dict:
    c = _read(LLM_CONFIG_FILE, {})
    for k in ("base_url", "model", "temperature"):
        if k in d:
            c[k] = d[k]
    _write(LLM_CONFIG_FILE, c)
    return llm_config()


def _key() -> str:
    k = os.environ.get("DEEPSEEK_API_KEY", "")
    if not k:
        raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置；请在本机环境变量里配置后重启服务")
    return k


def _safe(e) -> str:
    msg = str(e)
    k = os.environ.get("DEEPSEEK_API_KEY", "")
    if k:
        msg = msg.replace(k, "***")
    return msg[:500]


def llm_models() -> list:
    cfg, key = llm_config(), _key()
    req = urllib.request.Request(cfg["base_url"].rstrip("/") + "/models",
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        return [m.get("id") for m in d.get("data", []) if m.get("id")]
    except Exception as e:
        raise RuntimeError("拉取模型列表失败：" + _safe(e))


def llm_chat(system: str, user: str, model: str | None = None,
             temperature: float | None = None, timeout: int = 180) -> dict:
    cfg, key = llm_config(), _key()
    model = model or cfg["model"]
    temp = cfg["temperature"] if temperature is None else float(temperature)
    body = json.dumps({
        "model": model, "temperature": temp, "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return {"text": d["choices"][0]["message"]["content"],
                "model": d.get("model", model), "usage": d.get("usage", {})}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"DeepSeek HTTP {e.code}：{_safe(e.read().decode('utf-8', 'ignore'))}")
    except Exception as e:
        raise RuntimeError("DeepSeek 调用失败：" + _safe(e))


# ---------------------------------------------------------------- 解析 / 校验
HEAD_RE = re.compile(r"^\s*[\[【]\s*(\d{1,2})\s+([^\]】]+)[\]】]\s*(.*)$")


def parse_reply(text: str, beats: list, beat: int | None) -> dict:
    if beat is not None:
        return {"mode": "single", "beat": int(beat), "items": [{"id": int(beat), "text": text.strip()}]}
    items, cur = [], None
    for line in text.splitlines():
        m = HEAD_RE.match(line)
        if m:
            if cur:
                items.append(cur)
            cur = {"id": int(m.group(1)), "label": m.group(2).strip(), "text": m.group(3).strip()}
        elif cur and line.strip():
            cur["text"] += line.strip()
    if cur:
        items.append(cur)
    ok = [x for x in items if 0 <= x["id"] < len(beats) and x["text"]]
    return {"mode": "multi", "items": ok, "parsed": len(ok),
            "expected": len(beats), "complete": len(ok) == len(beats)}


def split_sent(text: str) -> list:
    out = []
    for para in re.split(r"[。！？!?]", text):
        buf = ""
        for c in [x for x in re.split(r"[，、；：]", para) if x]:
            if buf and len(buf) + len(c) + 1 > 14:
                out.append(buf)
                buf = c
            else:
                buf = f"{buf}，{c}" if buf else c
        if buf:
            out.append(buf)
    return out


def check(beats: list, proj, calib=None) -> dict:
    calib = calib or calibration()
    budget = char_budget(proj, beats, calib)
    texts = [b["text"] for b in beats]
    alltext = "".join(texts)
    sents = [s for t in texts for s in split_sent(t)]
    lens = sorted(len(s) for s in sents)
    out_dur = budget["out_dur"]
    turns = sum(alltext.count(w) for w in TURN_WORDS)
    twists = sum(alltext.count(w) for w in TWIST_WORDS)
    return {
        "chars": sum(len(s) for s in sents),
        "budget": budget["total"],
        "beats": len(beats),
        "sentences": len(sents),
        "len_median": lens[len(lens) // 2] if lens else 0,
        "over18": sum(1 for L in lens if L > 18),
        "turn_words": turns,
        "turn_interval": round(out_dur / turns, 2) if turns else None,
        "twist_words": twists,
        "twist_interval": round(out_dur / twists, 2) if twists else None,
        "cps": calib["effective_cps"],
        "note": "只读参考，不作为验收或自动改写依据",
    }


# ---------------------------------------------------------------- 定稿落盘
SKILL_SCRIPTS = Path(r"C:/Users/10451/.workbuddy/skills/anime-recap/scripts")


def script_info(proj) -> dict:
    """写回前的只读体检：块数是否对齐、当前是否已定稿、备份是否已存在。"""
    f = proj.dir / "json" / "script.json"
    d = _read(f, {})
    sb = d.get("beats", []) if isinstance(d, dict) else []
    dr = draft(proj, create=False)["beats"]
    return {
        "path": str(f), "exists": f.exists(),
        "script_beats": len(sb), "draft_beats": len(dr),
        "aligned": bool(sb) and len(sb) == len(dr),
        "finalized": bool(state(proj).get("finalized")),
        "backups": len(list(f.parent.glob("script.json.bak.*"))),
        "draft_chars": sum(len(b.get("text", "")) for b in dr),
        "script_chars": sum(len(str(b.get("text", ""))) for b in sb),
    }


def apply_to_script(proj) -> dict:
    """把定稿草稿的 14 块文案写回 json/script.json，只改 beats[].text。

    · 必须已定稿；块数必须与 script.json 一致；无变化则不写盘。
    · 写前自动备份 script.json.bak.<时间戳>，不触碰 anchor/anchor_new 等字段。
    · 不执行任何渲染/配音步骤——那是 7 步脚本的事。
    """
    info = script_info(proj)
    if not info["finalized"]:
        raise RuntimeError("旁白尚未定稿，不能写回 script.json")
    if not info["exists"]:
        raise RuntimeError("工程缺少 json/script.json")
    if not info["aligned"]:
        raise RuntimeError(f"块数不一致：script.json {info['script_beats']} 块 / 草稿 {info['draft_beats']} 块")

    f = proj.dir / "json" / "script.json"
    d = json.loads(f.read_text(encoding="utf-8"))
    src_beats = d["beats"]
    new_beats = draft(proj, create=False)["beats"]
    changes = []
    for i, (a, b) in enumerate(zip(src_beats, new_beats)):
        old, new = str(a.get("text", "")), str(b.get("text", "")).strip()
        if not new:
            raise RuntimeError(f"第 {i} 块草稿为空，已中止（未写盘）")
        if old != new:
            changes.append({"id": i, "label": b.get("label", f"块{i}"),
                            "old_len": len(old), "new_len": len(new)})
        a["text"] = new
    if not changes:
        return {"changed": 0, "backup": None, "path": str(f),
                "hint": "文案与 script.json 一致，无需写回。"}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f.with_name(f"script.json.bak.{ts}")
    shutil.copy2(f, backup)
    f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return {
        "changed": len(changes), "changes": changes,
        "backup": str(backup), "path": str(f),
        "hint": "写回后必须重跑 7 步链路；新 cutplan 会覆盖锚点校对台已做的整块平移。",
    }


def rebuild_commands(proj) -> list:
    """定稿后的 7 步链路（只生成，不执行）。顺序不能颠倒：先配音拿实测时长再切画面。"""
    d = proj.dir
    pj = d / "project.json"
    build = d / "build_narr.py"
    if not build.exists():
        build = SKILL_SCRIPTS / "build_narr.py"
    r6 = d / "scripts" / "r6_mix.py"
    if not r6.exists():
        r6 = SKILL_SCRIPTS / "r6_mix.py"
    relayout = d / "relayout_narr.py"
    if not relayout.exists():
        relayout = SKILL_SCRIPTS / "relayout_narr.py"
    return [
        f'cd /d "{d}"',
        "rem ① 切句（读 script.json → narration.json）",
        f'python "{build}"',
        "rem ② 配音（edge-tts，逐句合成）",
        f'python "{SKILL_SCRIPTS / "r5_tts.py"}" --project "{pj}"',
        "rem ③ 刀长对齐（刀数 = 旁白总时长 / 1.32）",
        f'python "{SKILL_SCRIPTS / "n3_align.py"}" --cut 1.32 --project "{pj}"',
        "rem ④ 重剪（按新 cutplan 出画面）",
        f'python "{SKILL_SCRIPTS / "cut.py"}" --force --project "{pj}"',
        "rem ⑤ 重铺旁白（校正 n3 的 ±0.3s 累积偏差）",
        f'python "{relayout}"',
        "rem ⑥ 混音（旁白区原声 −50dB，勿加回二次 trim）",
        f'python "{r6}" --project "{pj}"',
        "rem ⑦ 合片与验收（−13.2 LUFS 两遍 loudnorm）",
        f'python "{SKILL_SCRIPTS / "r7_mux.py"}" --project "{pj}"',
    ]
