# [L1 会话工具] 职责：会话内读图批次 plan/merge ｜ 调用者：会话内导演 ｜ 产物：json/shots_desc.json
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""会话内视觉描述的批次工具：生成批次清单 + 合并会话产出的 JSON 到 shots_desc.json。

用法：
  python session_desc.py --project P --plan 20     # 生成批次清单（每批20帧）到 .shotdesc/session/
  python session_desc.py --project P --merge       # 合并 session/batch_*.json 到 json/shots_desc.json
"""
import argparse
import json
import sys
from pathlib import Path


def _frames_dir(proj: Path) -> Path:
    for name in (".shotdesc", ".scenedesc"):
        d = proj / name
        if d.exists() and any(d.glob("*.jpg")):
            return d
    raise SystemExit("找不到抽帧目录（.shotdesc/.scenedesc），先跑 prepare/shot_desc 抽帧")


def build_plan(proj: Path, per_batch: int) -> None:
    d = _frames_dir(proj)
    sd = json.load(open(proj / "json" / "shots_desc.json", encoding="utf-8"))["rows"]
    files = {p.name for p in d.glob("*.jpg")}
    batches = []
    cur = []
    for r in sd:
        # 长镜(>6s)取 F/M/L 三帧，普通镜取 M 优先、F 兜底
        if r["b"] - r["a"] > 6:
            cands = [f"shot_{r['i']:04d}{s}.jpg" for s in ("F", "M", "L")]
        else:
            cands = [f"shot_{r['i']:04d}{s}.jpg" for s in ("M", "F")]
        got = [c for c in cands if c in files]
        if not got:
            continue
        cur.append({"i": r["i"], "frames": got,
                    "span": [round(r["a"], 1), round(r["b"], 1)]})
        if len(cur) >= per_batch:
            batches.append(cur)
            cur = []
    if cur:
        batches.append(cur)
    out = proj / ".shotdesc" / "session"
    out.mkdir(parents=True, exist_ok=True)
    for bi, b in enumerate(batches):
        (out / f"plan_{bi:02d}.json").write_text(
            json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    total_frames = sum(len(x["frames"]) for b in batches for x in b)
    print(f"批次清单: {len(batches)} 批 / {len(sum(batches,[]))} 镜 / {total_frames} 帧 -> {out}")


def merge(proj: Path) -> None:
    sd_p = proj / "json" / "shots_desc.json"
    sd = json.load(open(sd_p, encoding="utf-8"))
    byid = {r["i"]: r for r in sd["rows"]}
    sess = proj / ".shotdesc" / "session"
    merged = 0
    for f in sorted(sess.glob("batch_*.json")):
        rows = json.load(open(f, encoding="utf-8"))
        for r in rows:
            t = byid.get(r["i"])
            if not t:
                continue
            for k in ("desc", "obj", "depth", "facts"):
                if r.get(k) is not None:
                    t[k] = r[k]
            if not t.get("dlg") and r.get("dlg"):
                t["dlg"] = r["dlg"]
            merged += 1
    sd["updated"] = __import__("datetime").datetime.now().isoformat()
    sd["source"] = "session-vision"
    sd_p.write_text(json.dumps(sd, ensure_ascii=False, indent=1), encoding="utf-8")
    n_depth = sum(1 for r in sd["rows"] if r.get("depth"))
    print(f"合并 {merged} 镜 -> {sd_p}；depth 覆盖 {n_depth}/{len(sd['rows'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--plan", type=int, default=0, help="每批帧数，>0 则生成批次清单")
    ap.add_argument("--merge", action="store_true")
    a = ap.parse_args()
    proj = Path(a.project).expanduser().resolve().parent
    if a.plan:
        build_plan(proj, a.plan)
    if a.merge:
        merge(proj)
    return 0


if __name__ == "__main__":
    sys.exit(main())
