# -*- coding: utf-8 -*-
"""L1·画面数字 OCR：把「画面浮字到底写的几千」变成可核对的数字。

所属层：L1 共享层 ｜ 职责：对 :mod:`aicutplan.shared.number_frames` 抽出的 1280 宽帧做 OCR，
                       把旁白里的数字与画面上的数字对齐，输出可核对的判定
调用者：``python -m aicutplan check --ocr``
副作用：写 ``<工程>/json/ocr_numbers.json``（含每帧识别文本与判定）

引擎：``rapidocr_onnxruntime``（可选依赖）。**未安装时只报可行动错误，不影响主链** ——
P2 的定位是「批量/无人值守时的可复现核对」，会话里读图也能达到同样目的。

判定四态（**只作参考，不判对错**）：
- ``screen-match``：画面写的和旁白同一个数，可作辅助佐证
- ``screen-other``：画面有数字但不是旁白那个 —— 多半是口径不同（强化增量 vs 剩余点数），需人看
- ``noisy``      ：美术字被拆字（``13U5``），只作疑似
- ``no-text``    ：这几帧没读到数字（浮字可能出现在更晚的时刻）

**判定原则：台词 > 画面。** 剧情事实以人物台词为准；画面浮字是「过程中的数据」
（+2750 的强化增量、3033 的剩余点数、临时倍率……），同一集里可以有好几套口径，
拿它去否旁白会大量误报。所以这里只回答「画面当时写了什么」，不下「旁白错了」的结论。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from aicutplan import cache as CL
from aicutplan.contracts import num_values
from aicutplan.io import dump_json, load_json
from aicutplan.shared import number_claims as NC
from aicutplan.shared import number_frames as NF

#: 判定口径版本：改归一规则或判定状态时 +1
VER = "ocrnum-v1"
#: 识别结果缓存的顶层键
CACHE_KEY = "frames"

_ENGINE = None


def _engine():
    """懒加载 OCR 引擎；缺依赖时给可行动的报错。"""
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:      # 可选依赖：主链不应因此失败
        raise RuntimeError(
            "OCR 引擎未安装：pip install rapidocr-onnxruntime"
            "（P2 属可选依赖；不想装可继续用 check --number-frames 读图核对）") from exc
    _ENGINE = RapidOCR()
    return _ENGINE


def normalize(text: str) -> str:
    """OCR 文本归一：全角→半角、去千分位、去数字间空格。

    画面浮字常是 ``＋５１５４`` / ``1 000`` / ``30,33`` 这类写法，
    不归一就永远匹配不上旁白。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.replace("，", ",").replace("．", ".")
    s = re.sub(r"(?<=\d)[\s,](?=\d)", "", s)      # 1 000 / 1,000 → 1000
    return s


def numbers_in_texts(texts: list[str]) -> set[float]:
    out: set[float] = set()
    for t in texts:
        out |= num_values(normalize(t))
    return out


#: 数字被字母夹断（``13U5``）或单字符（``5``）都是美术字拆字失败的特征，
#: 这类结果只作「疑似」给人看，不参与自动判定 —— 否则会凭空造出冲突。
_NOISY_RUN = re.compile(r"\d[^\d\s,.]+\d")


def is_noisy_text(text: str) -> bool:
    return bool(_NOISY_RUN.search(normalize(text)))


def read_frame(path: str | Path) -> list[str]:
    """识别一帧，返回文本行（测试里可直接替换本函数，不必真装引擎）。"""
    result, _ = _engine()(str(path))
    rows = []
    for item in result or []:
        if isinstance(item, (list, tuple)) and len(item) > 1 and isinstance(item[1], str):
            rows.append(item[1])
    return rows


def _frame_sig(path: Path) -> str:
    return "%s-%d" % (CL.file_sig(path), int(path.stat().st_size))


def _ocr_rows(frames: list[dict], by_name: dict, force: bool) -> list[dict]:
    """对一批帧做 OCR（带按帧签名缓存），返回帧级结果行。"""
    rows = []
    for row in frames:
        p = Path(row["path"])
        name = str(row.get("name") or p.name)
        sig = _frame_sig(p)
        prev = by_name.get(name)
        if prev and prev.get("sig") == sig and not force:
            texts = list(prev.get("texts") or [])
        else:
            texts = read_frame(p)
        clean = [t for t in texts if not is_noisy_text(t)]
        noisy = [t for t in texts if is_noisy_text(t)]
        rows.append({"name": name, "sig": sig, "texts": texts,
                     "numbers": sorted(numbers_in_texts(clean)),
                     "noisy_numbers": sorted(numbers_in_texts(noisy)),
                     "sentence": row["i"], "t": row["t"],
                     "kind": row.get("kind") or "sparse"})
    return rows


def verify(proj_dir: str | Path, *, force: bool = False, dense: bool = False) -> dict:
    """对命中数值断言的句子做 OCR，返回结果字典（同时写 json/ocr_numbers.json）。

    ``dense=True`` 时对「稀疏三帧里一个数字都没读到」的句子做第二遍：按 1 帧/秒补抽
    （单句上限 :data:`aicutplan.shared.number_frames.DENSE_MAX`）后再识别。
    浮字常只闪半秒，稀疏取样必然漏；补抽只增加参考信息，不改变「台词为准」的判定。
    """
    proj = Path(proj_dir)
    frames = NF.extract(proj)
    details = {d["i"]: d for d in NC.check_details(proj)}
    if not frames:
        return {"engine": _engine_name(), "generated_at": _now(), "frames": [], "sentences": []}

    cache_file = proj / "json" / "ocr_numbers.json"
    cached = (load_json(cache_file, {}) or {}) if not force else {}
    by_name = {f.get("name"): f for f in (cached.get(CACHE_KEY) or []) if f.get("name")}

    frame_rows = _ocr_rows(frames, by_name, force)

    dense_ids: list[int] = []
    if dense:
        by_sent: dict[int, list[dict]] = {}
        for r in frame_rows:
            by_sent.setdefault(int(r["sentence"]), []).append(r)
        dense_ids = sorted(i for i, rs in by_sent.items()
                           if not any(r["numbers"] for r in rs))
        if dense_ids:
            extra = NF.extract_dense(proj, dense_ids, force=force)
            frame_rows.extend(_ocr_rows(extra, by_name, force))

    sentences = []
    for i in sorted({r["sentence"] for r in frame_rows}):
        det = details.get(i) or {}
        mine = [r for r in frame_rows if r["sentence"] == i]
        seen: set[float] = set()
        noisy_seen: set[float] = set()
        for r in mine:
            seen |= set(r["numbers"])
            noisy_seen |= set(r["noisy_numbers"])
        want = set(det.get("missing") or [])
        # 语义：台词是剧情的第一权威；画面浮字多半是「过程数据」
        # （强化增量 +2750、剩余点数 3033、临时倍率…），口径常与旁白不同，
        # 所以画面数字**只作参考**，不判旁白对错。
        if want and want & seen:
            status = "screen-match"      # 画面写的和旁白同一个数，可作辅助佐证
        elif seen:
            status = "screen-other"      # 画面有数字但不是旁白那个：口径可能不同，需人看
        elif noisy_seen:
            status = "noisy"             # 美术字拆字失败，只有疑似值
        else:
            status = "no-text"           # 这几帧没读到数字
        sentences.append({
            "i": i, "text": det.get("text", ""),
            "missing": sorted(want), "frame_numbers": sorted(seen),
            "noisy_numbers": sorted(noisy_seen - seen),
            "status": status,
            "basis": "dialogue-first",   # 判定依据：台词 > 画面；画面仅参考
            "dense": i in dense_ids,
            "frames": [r["name"] for r in mine],
        })

    out = {"engine": _engine_name(), "generated_at": _now(), "ver": VER,
           "width": NF.FRAME_WIDTH, "dense_ids": dense_ids,
           CACHE_KEY: frame_rows, "sentences": sentences}
    (proj / "json").mkdir(parents=True, exist_ok=True)
    dump_json(cache_file, out)
    return out


def _engine_name() -> str:
    """只探测依赖是否装（不实例化引擎，避免每次 check 都加载模型）。"""
    try:
        import importlib.util as u
        return "rapidocr_onnxruntime" if u.find_spec("rapidocr_onnxruntime") else "unavailable"
    except Exception:
        return "unavailable"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_report(result: dict, *, limit: int = 8) -> str:
    """把「画面当时写了什么」整理成几行，供人对照旁白；不含「对/错」判定。"""
    rows = result.get("sentences") or []
    if not rows:
        return ""
    order = {"screen-other": 0, "noisy": 1, "no-text": 2, "screen-match": 3}
    rows = sorted(rows, key=lambda r: (order.get(r.get("status"), 9), r.get("i", 0)))
    counts = {k: sum(1 for r in rows if r.get("status") == k)
              for k in ("screen-match", "screen-other", "noisy", "no-text")}
    eng = result.get("engine") or "?"
    head = (f"[OCR 参考] {len(rows)} 句｜台词为准，画面数字多为过程数据｜"
            f"同数 {counts['screen-match']} / 异数 {counts['screen-other']} / "
            f"疑似 {counts['noisy']} / 未见 {counts['no-text']}（{eng}）")
    dense_n = len(result.get("dense_ids") or [])
    lines = [head]
    if dense_n:
        lines.append(f"  （其中 {dense_n} 句做过 1 帧/秒 补抽）")
    shown = 0
    for r in rows:
        if r["status"] == "screen-match" or shown >= limit:
            continue
        shown += 1
        if r["status"] == "screen-other":
            lines.append(f"  · 句{r['i']} 旁白 {_fmt(r['missing'])}｜画面 {_fmt(r['frame_numbers'])}"
                         f"（口径可能不同：增量/剩余/倍率）")
        elif r["status"] == "noisy":
            lines.append(f"  · 句{r['i']} 旁白 {_fmt(r['missing'])}｜OCR 疑似 "
                         f"{_fmt(r.get('noisy_numbers'))}（美术字拆字，需人看）")
        else:
            lines.append(f"  · 句{r['i']} 旁白 {_fmt(r['missing'])}｜这几帧没读到数字")
    if counts["screen-match"]:
        lines.append(f"  ✓ {counts['screen-match']} 句画面与旁白同数（辅助佐证，不展开）")
    return "\n".join(lines)


def _fmt(vals) -> str:
    return " / ".join("%g" % v for v in vals or []) or "-"
