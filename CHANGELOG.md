# 更新日志

版本号遵循 `MAJOR.MINOR.PATCH`。本项目的里程碑以「成片版本 v3.x」和「架构版本 v0.x」两条线记录，
这里按架构版本归并（成片版本见工程目录 `out/03-rapid-narration-v3*.mp4`）。

---

## v0.6.0 · 2026-11-12 — 项目整理与模块化

**结构**
- 新建 `aicutplan/` L0 基础包：`paths`（工程路径约定）、`io`（原子写/两类备份）、
  `media`（ffmpeg 定位）、`cache`（内容指纹与缓存失效）、`contracts`（数据契约与一致性校验）、
  `subtitles` / `planning` / `evidence`（领域纯函数）、`cli`（统一门面）。
- 新增 `python -m aicutplan` 统一入口：`run / plan / subs / qc / qc-apply / narrate / parse / serve / check / lint`。
- `cf_pipeline.lint_narration`、`subs_extract` 字幕解析、`qc_visual.ffmpeg_bin`、
  `evidence_feed` 证据打分、`cutfirst_plan` 钩子选段改为复用 L0 包，消除多份实现。
- 全部 30 个根目录脚本补统一分层标识注释；新增 `docs/ARCHITECTURE.md` 分层与依赖全表。
- 新增 `pyproject.toml` / `requirements.txt` / `CHANGELOG.md` / `tests/`（标准库 unittest，零依赖）。

**清理**
- 删除 5 个零引用且产物无人消费的脚本：`entity_graph.py`、`audit_frames.py`、
  `word_srt.py`、`verify_sync.py`、`auto_script.py`（git 历史可恢复）。
- 修正 `server.py` 等 5 个脚本的 `coding` 声明位置，保证 PEP 263 合规。

**修复**
- `subs_extract.py --check` 从「只打印 sig」升级为真正用 `aicutplan.cache.sig_valid`
  校验源片指纹，源片/字幕轨更换却未重新提取时返回非 0。
- `.gitignore` 补齐打包/测试/编辑器产物。

**文档**
- README 重写为 cut-first 主链优先，旧 web 17 步链标注为历史链路；修正过时指标
  （字/刀 6.21、覆盖 93.8%）与目录清单（原清单缺 15 个新脚本、含 3 个已删脚本）。

---

## v0.5.0 · 2026-11-12 — 内嵌字幕优先

- 新增 `subs_extract.py`：MKV/MP4 自带 ASS/SRT 字幕时直接提取，跳过 faster-whisper。
  支持语言优先级、图像字幕排除、ASS 同时间多行合并、说话人标注、译中缓存按时间+相似度迁移。
- `asr_local.py` 检测到文本字幕自动跳过 ASR（`--force-asr` 可强制）；
  `cf_pipeline.py --subs yes|auto|no` 在链路开始前替换台词源。
- 产物顶层写 `source: embedded-subtitle` 与 `sig` 内容指纹。
- 实测当前工程：台词源 ASR 404 段 → 字幕 352 条，`shots_desc.dlg` 覆盖 78.6% → 88.6%。
- 顺带修复 `cf_pipeline.lint_narration` 两处历史误报（留白窗被算作未覆盖、
  拿上一稿的 `tts_dur` 判新稿超槽）。

## v0.4.0 · 2026-11-12 — 生成后质检闭环（P0-1）

- `qc_visual.py` 去掉 DeepSeek VLM 依赖，改为 `--plan`（2×3 宫格图）→ 会话内判定 →
  `--merge` → `qc_rewrite.json` → `--probe`（失配句 3 帧复核）→ 修正 → 再 merge。
- 新增 `qc_loop.py --list/--apply` 与 `cf_pipeline --qc / --qc-apply / --dry-run / --qc-force`，
  闭环走 `narrate_cut --sentences → r5_tts --only → elastic_fit → r6_mix → r7_mux`。
- 修复两个会让质检失真的坑：抽帧缓存按句号命名导致取到旧版成片帧（改为 `--plan`
  强制重抽 + 成片 mtime 失效）；句首窗中点落在全黑转场帧（加暗帧回退）。
- 修复 `r5_tts.py --only` 见 mp3 存在就跳过合成导致「文本新词 / 成片旧词」：
  改为改名备份后强制重合成，`qc_loop` 在 r5 之后校验 mp3 确实被重写。
- v3.4 → v3.5：54 句质检失配 4 句（7.4%）→ 0 句（0.0%）。

## v0.3.0 · 2026-11-12 — 上游证据回喂（P0-2）

- 新增 `evidence_feed.py`：把 387 镜 `desc/depth/facts/dlg` 按 200s 段 / 事件窗聚合成
  紧凑摘要，零新增抽帧、零新增读图、零模型调用。
- `story_parse.py` / `causal_graph.py` 的 prompt 接入证据段，并新增
  `--export-prompt` / `--import`（会话内模式，不依赖 DeepSeek API）。
- 无 `shots_desc.json` 的工程自动退回旧行为。

## v0.2.0 · 2026-11-12 — 钩子回归剪辑规划

- `cutfirst_plan.py` 删掉「全球散装检索 10 个最爽镜头」，改为 `_pick_hook_clip()`：
  在镜头序列上滑动预算长度窗口，锁定**源片连续、未经裁切重排**的一整段爽点。
- `project.json` 的 `recap.hook_clip=[a,b]` 可在规划阶段锁定区间；钩子镜头不进 `used`
  （提前复用，正片原样再演）。
- `narrate_cut.py --export` 的钩子块只列字数槽位，不再列画面/台词/帧事实/因果。
- `SKILL.md` 新增「钩子选段的硬约束（属于②选段，不属于③写文案）」。

## v0.1.x · 2026-11-09 ~ 11-11 — cut-first 主线与会话内导演

- 切 `cut-first` 分支，翻转时序：画面先定 → 旁白后写，取代「旁白定稿 → TTS → 裁画面」。
- 因果图谱、实体图谱、镜头证据包（`shots_desc.json`）、2s 窗场景描述、词级字幕落地。
- 会话内读图替代全部 DeepSeek 视觉调用（387 镜 / 521 帧），文本写作也移到会话内。
- 反向剖析博主成片并复刻结构：整块快切钩子、爆发段原声留白、先说结果再演过程、
  结尾 cliffhanger。
- 修复两起严重事故：`r6_mix.py` 硬编码 `SR=44100` 而原声轨是 48000Hz 导致全片旁白
  提前 8.125% + 语速快 8.8%；镜头切分阈值漏检。
- 技能点数值由 VLM 读图的「十三万点」纠正为「一万三千点」（原声台词交叉验证）。
- 音色切换为云健（`zh-CN-YunjianNeural`），并发现 `r5_tts` 实际读 `recap` 段而非
  `narration` 段的配置陷阱。

## v0.0.x · 2026-11-07 ~ 11-09 — 检索腿版（main 分支，已冻结）

- web 校对台（8 个页面 / 17 步流水线）、逐句三轨校对、块级平移与句级锚点、
  剪映草稿导出。分支 `main` 停在 `4871650`。
