# -*- coding: utf-8 -*-
"""端到端审计：对成片逐句抽帧，让视觉模型判「这句旁白与画面是否匹配」。

用法：python audit_frames.py <project_dir> [--batch 10]
输出：<project>/.audit/audit_result.json + 控制台汇总
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

AUDIT_SYS = ("你是动漫解说的质检员。给你一批「旁白句子 + 该句响起时成片的截图」，"
             "逐条判断画面是否在演句子所说的内容。")

AUDIT_Q = """对下面每一条，判断画面与旁白的关系，只输出 JSON 数组，不要解释：
[{"i": 句子编号, "v": 2, "why": "不超过12字"}]
v 的取值：
2 = 画面在演这句话说的内容（人物/动作/对象对得上）
1 = 画面与句子同场景、有语境关联，但不演这句话的具体内容
0 = 画面与句子内容对不上（讲了A画面演B）
判断要严：句子提到具体对象（外套、剑、公会牌）时，画面里看不到该对象或没在演相关动作就是 1 或 0。

{items}"""


def call_vision(chunk, d):
    items = "\n".join(f"{m['i']} | {m['text']}（第 {m['t']:.0f} 秒的截图）" for m in chunk)
    content = [{"type": "text", "text": AUDIT_Q.replace("{items}", items)}]
    for m in chunk:
        f = d / f"s{m['i']:03d}.jpg"
        if not f.exists():
            continue
        b64 = base64.b64encode(f.read_bytes()).decode()
        content += [{"type": "text", "text": f"句子 {m['i']} 的截图："},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
    body = {"model": "deepseek-flash", "stream": False, "max_tokens": 9000,
            "messages": [{"role": "system", "content": AUDIT_SYS},
                         {"role": "user", "content": content}]}
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]})
    with urllib.request.urlopen(req, timeout=300) as r:
        d2 = json.loads(r.read().decode())
    txt = d2["choices"][0]["message"].get("content") or ""
    m0 = txt.find("[")
    if m0 < 0:
        return []
    try:
        return json.loads(txt[m0:txt.rfind("]") + 1])
    except json.JSONDecodeError:
        return []


def main() -> int:
    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("proj")
    ap.add_argument("--batch", type=int, default=10)
    a = ap.parse_args()
    proj = Path(a.proj)
    d = proj / ".audit"
    manifest = json.load(open(d / "manifest.json", encoding="utf-8"))
    results = []
    for k in range(0, len(manifest), a.batch):
        chunk = manifest[k:k + a.batch]
        try:
            rows = call_vision(chunk, d)
        except Exception as e:
            print(f"[audit] batch@{k} 失败: {type(e).__name__} {str(e)[:120]}", flush=True)
            continue
        got = {int(r_["i"]): r_ for r_ in rows if isinstance(r_, dict) and "i" in r_}
        for m in chunk:
            r_ = got.get(m["i"])
            if r_:
                results.append({"i": m["i"], "text": m["text"], "t": m["t"],
                                "v": int(r_.get("v", -1)), "why": str(r_.get("why", ""))[:40]})
        done = len(results)
        print(f"[audit] {done}/{len(manifest)}", flush=True)
    (d / "audit_result.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    n = {0: 0, 1: 0, 2: 0}
    for r_ in results:
        n[r_["v"]] = n.get(r_["v"], 0) + 1
    tot = max(1, len(results))
    print(f"\n匹配度：完全匹配 {n[2]}（{100*n[2]/tot:.0f}%）· 同场景 {n[1]}（{100*n[1]/tot:.0f}%）· 不匹配 {n[0]}（{100*n[0]/tot:.0f}%）")
    print("不匹配清单：")
    for r_ in results:
        if r_["v"] == 0:
            print(f"  句{r_['i']} [{r_['t']:.0f}s] {r_['text'][:24]} → {r_['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
