# -*- coding: utf-8 -*-
"""素材解析 · 多语 ASR → 中文台词时间轴（第一版）

输入：工程目录的 asr_ja.json / asr_en.json / asr.json（段级 {t0,t1,text}）
输出：工作区 asr/<工程hash>/asr_zh.json —— 每段保留 t0/t1、原文、译文、是否已译。
      **不写工程目录**，译文可随时清除重来。

设计要点：
  · DeepSeek 分批翻译（默认 24 段/批），每批结果落盘缓存，失败可断点续传。
  · 严守「长度一致」：模型返回条数不对就重试，绝不合并/拆分，保证时间戳不串位。
  · Key 复用 narration.llm_chat()，只读环境变量，不落盘、不进日志。
  · 中文源直接复制，不浪费 token；未译段落回退原文，下游不会拿到空文本。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

import narration as NR

ROOT = Path(__file__).resolve().parent
ASR = ROOT / "asr"

SYS = """你是影视台词翻译。把用户给出的 JSON 数组里每一条台词翻译成中文。
严格要求：
1. 只输出一个 JSON 数组，长度与输入完全一致，每个元素是一条中文字符串。
2. 不合并、不拆分、不增删条目；逐条对应，顺序不变。
3. 口语化、简洁，适合做解说文案的剧情参考；人名与专有名词前后保持一致。
4. 不要输出解释、markdown 代码块、编号、对象或任何额外字段。
5. 若原文本是无意义的语气词或识别噪声，照译或原样保留即可，不要删条目。"""


def pkey(project_dir) -> str:
    return hashlib.md5(str(Path(project_dir).resolve()).encode()).hexdigest()[:12]


def workdir(proj) -> Path:
    d = ASR / pkey(proj.dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def out_path(proj) -> Path:
    return workdir(proj) / "asr_zh.json"


def find_source(proj) -> Path | None:
    for n in ("asr_ja.json", "asr_en.json", "asr_ko.json", "asr.json", "json/asr.json"):
        p = proj.dir / n
        if p.is_file():
            return p
    return None


def detect_lang(paras: list, declared: str = "") -> str:
    text = "".join(str(p.get("text", "")) for p in paras[:80])
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if kana > 20:
        return "ja"
    if hangul > 20:
        return "ko"
    if latin > 60 and latin > han * 2:
        return "en"
    if han > 20:
        return "zh"
    return declared or "unknown"


LANG_NAME = {"ja": "日语", "en": "英语", "ko": "韩语", "zh": "中文", "unknown": "未知"}


def read_source(proj) -> dict:
    f = find_source(proj)
    if f is None:
        return {"path": None, "paras": [], "lang": "unknown", "model": ""}
    d = json.loads(f.read_text(encoding="utf-8"))
    paras = d.get("paras", [])
    # 兼容 asr_to_json.py 的 [t0,t1,kind,text] 行格式
    if paras and isinstance(paras[0], (list, tuple)):
        paras = [{"t0": x[0], "t1": x[1], "kind": x[2], "text": x[3]}
                 for x in paras if len(x) >= 4]
    return {"path": f, "paras": paras,
            "lang": detect_lang(paras, str(d.get("lang", ""))),
            "model": str(d.get("model", ""))}


def state(proj) -> dict:
    src = read_source(proj)
    out = out_path(proj)
    zh = None
    if out.is_file():
        try:
            zh = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            pass
    n = len(src["paras"])
    translated = int(zh.get("translated", 0)) if zh else 0
    sample = []
    if zh:
        for p in zh.get("paras", [])[:3]:
            sample.append({"t0": p.get("t0"), "orig": p.get("text_orig", "")[:60],
                           "zh": p.get("text", "")[:60]})
    return {
        "source": str(src["path"]) if src["path"] else "",
        "source_exists": src["path"] is not None,
        "lang": src["lang"], "lang_name": LANG_NAME.get(src["lang"], src["lang"]),
        "asr_model": src["model"], "segments": n,
        "translated": translated, "ready": bool(zh and translated >= n and n > 0),
        "incomplete": bool(zh and translated < n),
        "output": str(out), "has_output": zh is not None,
        "model": zh.get("model") if zh else None,
        "created": zh.get("created") if zh else None,
        "sample": sample,
        "key_present": NR.llm_config()["key_present"],
        "default_model": NR.llm_config()["model"],
    }


def _parse_array(text: str) -> list:
    t = (text or "").strip()
    t = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", t).strip()
    i, j = t.find("["), t.rfind("]")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    arr = json.loads(t)
    if not isinstance(arr, list):
        raise ValueError("返回不是数组")
    return [str(x.get("t", x)) if isinstance(x, dict) else str(x) for x in arr]


def translate(proj, model: str | None = None, batch: int = 24,
              limit: int | None = None, reset: bool = False) -> dict:
    src = read_source(proj)
    if not src["path"]:
        raise RuntimeError("工程里找不到 asr_*.json；请先完成音转字")
    full_n = len(src["paras"])
    limited = bool(limit) and int(limit) < full_n
    paras = src["paras"][:int(limit)] if limit else src["paras"]
    n = len(paras)
    if not n:
        raise RuntimeError("ASR 段落为空")
    if not NR.llm_config()["key_present"] and src["lang"] != "zh":
        raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置")

    wd = workdir(proj)
    cache = wd / "cache"
    cache.mkdir(exist_ok=True)
    if reset:
        for f in cache.glob("batch_*.json"):
            f.unlink()

    t0 = time.time()
    model = model or NR.llm_config()["model"]
    results: list[str | None] = [None] * n
    errors, calls, tokens = [], 0, 0

    if src["lang"] == "zh":
        results = [str(p.get("text", "")) for p in paras]
    else:
        for bi in range(0, n, batch):
            idx = bi // batch
            chunk = paras[bi:bi + batch]
            cf = cache / f"batch_{idx:04d}.json"
            if cf.is_file():
                try:
                    arr = json.loads(cf.read_text(encoding="utf-8"))
                    if isinstance(arr, list) and len(arr) == len(chunk):
                        results[bi:bi + len(chunk)] = [str(x) for x in arr]
                        continue
                except Exception:
                    pass
            payload = [{"i": bi + k, "t": str(p.get("text", ""))}
                       for k, p in enumerate(chunk)]
            last = ""
            for attempt in range(3):
                try:
                    r = NR.llm_chat(SYS, json.dumps(payload, ensure_ascii=False),
                                    model=model, temperature=0.2)
                    calls += 1
                    tokens += int((r.get("usage") or {}).get("total_tokens") or 0)
                    arr = _parse_array(r["text"])
                    if len(arr) != len(chunk):
                        raise ValueError(f"返回 {len(arr)} 条，期望 {len(chunk)} 条")
                    results[bi:bi + len(chunk)] = arr
                    cf.write_text(json.dumps(arr, ensure_ascii=False), encoding="utf-8")
                    last = ""
                    break
                except Exception as e:
                    last = NR._safe(e)
                    time.sleep(1 + attempt)
            if last:
                errors.append({"batch": idx, "error": last[:180]})

    out_paras = []
    for k, p in enumerate(paras):
        zh = results[k]
        out_paras.append({
            "t0": p.get("t0"), "t1": p.get("t1"),
            "text": zh if zh else str(p.get("text", "")),
            "text_orig": str(p.get("text", "")),
            "translated": bool(zh),
        })
    done = sum(1 for x in out_paras if x["translated"])
    payload = {
        "source": str(src["path"]), "source_lang": src["lang"],
        "model": model, "created": datetime.now().isoformat(timespec="seconds"),
        "total": n, "translated": done, "paras": out_paras,
    }
    # 小批量试译只落缓存，绝不覆盖已生成的完整译文文件
    if limited:
        return {
            "total": n, "translated": done, "ready": False,
            "partial": True, "saved": False,
            "errors": errors, "calls": calls, "tokens": tokens,
            "elapsed": round(time.time() - t0, 1),
            "lang": src["lang"], "model": model, "output": str(out_path(proj)),
            "hint": f"试译 {n}/{full_n} 段：结果已进缓存，未覆盖完整译文。不带 limit 再跑一次即可合并。",
        }
    out_path(proj).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {
        "total": n, "translated": done, "ready": done >= n,
        "errors": errors, "calls": calls, "tokens": tokens,
        "elapsed": round(time.time() - t0, 1),
        "lang": src["lang"], "model": model,
        "output": str(out_path(proj)),
    }


def clear(proj) -> dict:
    wd = workdir(proj)
    n = 0
    for f in [out_path(proj)] + sorted((wd / "cache").glob("batch_*.json")):
        if f.is_file():
            f.unlink()
            n += 1
    return {"ok": True, "removed": n}
