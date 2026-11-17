# -*- coding: utf-8 -*-
"""L0·阶段审批：把「人看过并放行」变成可校验、可失效的记录。

所属层：L0 基础层 ｜ 职责：三阶段审批的签名、记录、失效判定与口令确认 ｜ 副作用：读写工程 json/
调用者：``aicutplan approve`` / ``approvals``、cf_pipeline / refit 的闸门、runs snapshot

= = = 设计要点 = = =
1. **签名绑定产物内容**，不是时间戳：产物一改，之前的批准自动失效（避免「批了旧版」）。
2. 三个阶段的签名只覆盖**该阶段决策本身**，排除后续合法改写的字段：
   - cutplan 排除 ``new_start/new_end``（cut.py 会把帧网格时间回写进去）；
   - narration 排除 mp3 / tts-result（重新配音不算文案变动）。
3. **口令确认**：``approve <stage>`` 先发一个 6 位码（绑定 stage + 当前 sig，10 分钟过期、一次性），
   再带 ``--code`` 落记录。模型无法自批——它拿不到「你回给它的那个码」。
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime
from pathlib import Path

from .cache import content_sig
from .io import dump_json, load_json
from .workspace import quick_sig

#: 可审批的阶段（顺序即展示顺序）
STAGES = ("cutplan", "narration", "publish")
#: 口令有效期（秒）
CODE_TTL = 600

APPROVALS = ("json", "approvals.json")
CODE_FILE = ("json", "approval_code.json")


def _root(project: str | Path) -> Path:
    p = Path(project).expanduser().resolve()
    return p if p.is_dir() else p.parent


def _cutplan_sig(root: Path) -> str | None:
    plan = load_json(root / "json" / "cutplan.json", {}) or {}
    if not plan.get("windows"):
        return None
    cfg = load_json(root / "project.json", {}) or {}
    hook = (cfg.get("recap") or {}).get("hook_clip")
    rows = [{"s": round(float(w.get("src_start") or 0), 3),
             "e": round(float(w.get("src_end") or 0), 3),
             "t": w.get("tier"), "b": w.get("beat")}
            for w in plan["windows"]]
    return content_sig("cutplan-v1", hook, rows)


def _narration_sig(root: Path) -> str | None:
    narr = load_json(root / "json" / "narration.json", {}) or {}
    items = narr.get("items") or []
    if not items:
        return None
    rows = [{"i": it.get("i"), "t0": it.get("t0"), "dur": it.get("dur"), "text": it.get("text")}
            for it in items]
    align = load_json(root / "json" / "align.json", {}) or {}
    return content_sig("narration-v1", rows, align.get("sent_win_idx"))


def _publish_sig(root: Path) -> str | None:
    final = root / "out" / "03-rapid-narration.mp4"
    if not final.exists():
        return None
    qc = load_json(root / "json" / "qc_visual.json", {}) or []
    if isinstance(qc, dict):
        qc = qc.get("rows") or []
    judged = sum(1 for x in qc if x.get("judged"))
    mismatch = sorted(int(x.get("i")) for x in qc if int(x.get("match", 1)) == 0
                      and isinstance(x.get("i"), int))
    return content_sig("publish-v1", quick_sig(final), judged, mismatch)


_SIGS = {"cutplan": _cutplan_sig, "narration": _narration_sig, "publish": _publish_sig}


def stage_sig(project: str | Path, stage: str) -> str | None:
    """当前产物的签名；缺产物返回 None（未就绪，不能审批）。"""
    if stage not in _SIGS:
        raise ValueError(f"未知阶段：{stage}（可选 {'/'.join(STAGES)}）")
    return _SIGS[stage](_root(project))


def load(project: str | Path) -> dict:
    return load_json(_root(project).joinpath(*APPROVALS), {}) or {}


def save(project: str | Path, data: dict) -> None:
    dump_json(_root(project).joinpath(*APPROVALS), data)


def status(project: str | Path) -> dict:
    """每阶段一条：{sig, at, by, note, valid, approved}。valid=False 表示产物已变、批准作废。"""
    root = _root(project)
    data = load(root)
    out = {}
    for stage in STAGES:
        cur = stage_sig(root, stage)
        rec = (data.get("stages") or {}).get(stage) or {}
        approved = bool(rec.get("sig"))
        out[stage] = {
            "sig": cur,
            "approved": approved,
            "valid": bool(approved and cur and rec.get("sig") == cur),
            "at": rec.get("at"),
            "by": rec.get("by"),
            "note": rec.get("note"),
            "sample_seed": rec.get("sample_seed"),
            "checked": rec.get("checked") or [],
        }
    return out


def enforced(project: str | Path) -> bool:
    """项目是否开启审批闸门：``project.json`` 的 ``approvals.enforce``，默认 True。"""
    cfg = load_json(_root(project) / "project.json", {}) or {}
    flag = (cfg.get("approvals") or {}).get("enforce")
    return True if flag is None else bool(flag)


def require(project: str | Path, stage: str) -> str | None:
    """闸门：已批准且签名仍匹配返回 None，否则返回给人看的原因。"""
    st = status(project)[stage]
    if st["sig"] is None:
        return f"{stage} 还没有产物，无法审批（先跑到该阶段）"
    if not st["approved"]:
        return f"{stage} 未审批：先跑 `aicutplan approve {stage} --project <pj>` 拿口令码"
    if not st["valid"]:
        return f"{stage} 的审批已失效（产物在批准后被改过）：重新审批"
    return None


# ---------------------------------------------------------------- 口令确认

def _code_path(root: Path) -> Path:
    return root.joinpath(*CODE_FILE)


def issue_code(project: str | Path, stage: str) -> tuple[str, int]:
    """发一个绑定 (stage, 当前签名) 的一次性口令码，返回 (code, 过期时间戳)。"""
    root = _root(project)
    sig = stage_sig(root, stage)
    if sig is None:
        raise RuntimeError(f"{stage} 还没有产物，无法审批（先跑到该阶段）")
    code = f"{secrets.randbelow(1000000):06d}"
    dump_json(_code_path(root), {"stage": stage, "sig": sig, "code": code,
                                 "expires_at": time.time() + CODE_TTL})
    return code, int(time.time() + CODE_TTL)


def confirm(project: str | Path, stage: str, code: str) -> str:
    """用口令码换一次批准；返回新的签名。码错/过期/产物已变都抛错。"""
    root = _root(project)
    pending = load_json(_code_path(root), {}) or {}
    if not pending:
        raise RuntimeError("没有待确认的口令，先跑 `approve <stage>` 生成一个")
    if pending.get("stage") != stage:
        raise RuntimeError(f"口令是为 {pending.get('stage')} 发的，不能用于 {stage}")
    if time.time() > float(pending.get("expires_at") or 0):
        raise RuntimeError("口令已过期，重新生成")
    if str(pending.get("code")) != str(code).strip():
        raise RuntimeError("口令不对")
    sig = stage_sig(root, stage)
    if sig != pending.get("sig"):
        raise RuntimeError("产物在发码之后变过，批准会绑到旧版本；重新生成口令")
    _code_path(root).unlink(missing_ok=True)   # 一次性
    return sig


def record(project: str | Path, stage: str, *, sig: str, by: str = "user",
           note: str = "", sample_seed: int | None = None,
           checked: list[str] | None = None, channel: str = "chat") -> dict:
    """写入一条批准记录（调用前应先 confirm）。"""
    root = _root(project)
    data = load(root)
    stages = data.setdefault("stages", {})
    stages[stage] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "by": by,
        "channel": channel,
        "sig": sig,
        "note": note,
        "sample_seed": sample_seed,
        "checked": list(checked or []),
        "version": 1,
    }
    data["version"] = 1
    save(root, data)
    return stages[stage]
