# -*- coding: utf-8 -*-
# [L3 界面层] 职责：剪映草稿子进程 ｜ 调用者：jianying_export ｜ 产物：剪映草稿
# 分层与依赖全表见 docs/ARCHITECTURE.md
"""剪映草稿生成器（在 WorkBuddy 托管 venv 中运行，那里才有 pyJianYingDraft）。

输入：一个 JSON 计划文件；输出：一份剪映可读的明文草稿目录。
不读剪映已有草稿、不改 root_meta_info.json、不做任何解密。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pyJianYingDraft as draft
from pyJianYingDraft import TextStyle, trange_seconds


def main() -> int:
    plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_root = Path(plan["out_root"])
    out_root.mkdir(parents=True, exist_ok=True)

    folder = draft.DraftFolder(str(out_root))
    script = folder.create_draft(
        plan["name"], plan["width"], plan["height"],
        fps=plan["fps"], allow_replace=True,
    )

    # 「后来居上」：先建视频轨（下层），再建混音轨；字幕轨由 import_srt 自建并置顶
    script.append_tracks([
        draft.TrackSpec(draft.TrackType.video, "画面"),
        draft.TrackSpec(draft.TrackType.audio, "混音"),
    ])

    for c in plan["clips"]:
        # source / target 的 duration 必须相同，否则 pyJianYingDraft 会自动算变速。
        # src=None 表示整片模式：不裁剪源区间，直接用素材全长。
        kwargs = {}
        if c.get("src") is not None:
            kwargs["source_timerange"] = trange_seconds(c["src"], duration=c["dur"])
        seg = draft.VideoSegment(
            plan["video_material"],
            trange_seconds(c["dst"], duration=c["dur"]),
            **kwargs,
        )
        script.add_segment(seg, "画面")

    script.add_segment(
        draft.AudioSegment(
            plan["audio_material"],
            trange_seconds(0, duration=plan["duration"]),
        ),
        "混音",
    )

    srt = plan.get("srt")
    if srt and Path(srt).exists():
        script.import_srt(
            srt, "字幕",
            text_style=TextStyle(size=5.0, color=(1.0, 1.0, 1.0), align=1,
                                 auto_wrapping=True, max_line_width=0.82),
        )

    script.save()
    print(json.dumps({"ok": True, "draft": str(out_root / plan["name"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
