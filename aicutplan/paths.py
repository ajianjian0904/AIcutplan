# -*- coding: utf-8 -*-
"""L0·工程路径约定：把散落在各阶段脚本里的路径拼接收敛到一处。

所属层：L0 基础层 ｜ 职责：唯一定义「一个工程长什么样」 ｜ 副作用：无
被谁用：cf_pipeline / qc_visual / qc_loop / subs_extract / evidence_feed /
        story_parse / causal_graph（以及未来所有阶段脚本）

约定（与历史工程完全兼容，未改任何文件名）::

    <工程根>/                     = project.json 所在目录
    ├── project.json              工程配置（recap.voice / hook_clip / src_dur ...）
    ├── json/                     规划与文本产物
    │   ├── story_plan.json       事件表 / 块表 / causal
    │   ├── cutplan.json          窗口表（new_start/new_end/src_start/src_end/tier）
    │   ├── align.json            句子↔窗口绑定 sent_win_idx
    │   ├── narration.json        54 句旁白（i/t0/dur/text/beat）
    │   ├── shots_desc.json       镜头证据包（desc/obj/depth/facts/dlg）
    │   ├── scene_desc.json       2s 窗场景描述
    │   ├── tts-result.json       TTS 实测时长
    │   ├── qc_visual.json        质检判定
    │   └── qc_rewrite.json       失配句重写清单
    ├── tts/                      每句一个 000.mp3（按句号命名，注意缓存失效）
    ├── out/                      01-video-no-ad.mp4 / 02-video-rapid-silent.mp4
    │                             / mix-rapid.wav / 03-rapid-narration.mp4
    ├── verify/                   试听样本、ASR 校验 wav
    └── .qcframes/session/        质检宫格图与会话内判定

注意：**工程根 ≠ 仓库根**。仓库根放脚本；工程根放数据。
仓库外的 cut.py/r5_tts/r6_mix/r7_mux 也按这套约定读写，两边必须一致。
"""
from __future__ import annotations

from pathlib import Path


class ProjectPaths:
    """一个工程的所有标准路径。传入 project.json 或其所在目录均可。"""

    def __init__(self, project: str | Path):
        p = Path(project).expanduser().resolve()
        if p.is_dir():
            p = p / "project.json"
        self.project = p
        self.root = p.parent
        #: 工程名（日志用）
        self.name = self.root.name

    # ---- 一级目录 ----
    @property
    def json(self) -> Path:
        return self.root / "json"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def tts(self) -> Path:
        return self.root / "tts"

    @property
    def verify(self) -> Path:
        return self.root / "verify"

    @property
    def session_prompts(self) -> Path:
        """会话内导出的剧情解析 / 因果图谱 prompt。"""
        return self.json / "session_prompts"

    @property
    def qcframes(self) -> Path:
        return self.root / ".qcframes"

    @property
    def qc_session(self) -> Path:
        """质检宫格图 / plan_XX.json / verdict_XX.json 所在目录。"""
        return self.qcframes / "session"

    # ---- 常用产物 ----
    @property
    def cutplan(self) -> Path:
        return self.json / "cutplan.json"

    @property
    def align(self) -> Path:
        return self.json / "align.json"

    @property
    def narration(self) -> Path:
        return self.json / "narration.json"

    @property
    def story_plan(self) -> Path:
        return self.json / "story_plan.json"

    @property
    def shots_desc(self) -> Path:
        return self.json / "shots_desc.json"

    @property
    def scene_desc(self) -> Path:
        return self.json / "scene_desc.json"

    @property
    def tts_result(self) -> Path:
        return self.json / "tts-result.json"

    @property
    def ja_segments(self) -> Path:
        return self.json / "ja-segments.json"

    @property
    def qc_visual(self) -> Path:
        return self.json / "qc_visual.json"

    @property
    def qc_rewrite(self) -> Path:
        return self.json / "qc_rewrite.json"

    @property
    def final_video(self) -> Path:
        return self.out / "03-rapid-narration.mp4"

    @property
    def silent_video(self) -> Path:
        return self.out / "02-video-rapid-silent.mp4"

    @property
    def mix_wav(self) -> Path:
        return self.out / "mix-rapid.wav"

    def mp3(self, sent_id: int) -> Path:
        """句号 → TTS 音频（三位数补零，与 r5_tts.py 一致）。"""
        return self.tts / f"{int(sent_id):03d}.mp3"

    # ---- 校验 ----
    def require(self, *paths: Path) -> list[Path]:
        """检查必需产物存在，返回路径列表；缺失时抛 FileNotFoundError。

        以前每个脚本各写一遍「四件套缺失就拒绝载入」，这里统一。
        """
        missing = [p for p in paths if not Path(p).exists()]
        if missing:
            raise FileNotFoundError(
                "工程缺少必需产物: " + ", ".join(str(p) for p in missing))
        return list(paths)

    def ensure_dirs(self, *dirs: Path) -> None:
        """幂等创建目录（不删除任何已有内容）。"""
        for d in dirs:
            Path(d).mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return f"<ProjectPaths {self.name} root={self.root}>"


def project_root(project: str | Path) -> Path:
    """快捷方式：只要工程根目录。"""
    return ProjectPaths(project).root


def repo_root() -> Path:
    """仓库根（aicutplan 包的上一级），等价于阶段脚本里的 ``ROOT``。"""
    return Path(__file__).resolve().parent.parent


def script(name: str) -> Path:
    """仓库根下的阶段脚本绝对路径，供 CLI 门面 / 编排器使用。"""
    p = repo_root() / name
    if not p.name.endswith(".py"):
        p = p.with_suffix(".py")
    return p
