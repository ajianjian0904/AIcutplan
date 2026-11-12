# 更新日志

版本号遵循 `MAJOR.MINOR.PATCH`。本项目的里程碑以「成片版本 v3.x」和「架构版本 v0.x」两条线记录，
这里按架构版本归并（成片版本见工程目录 `out/03-rapid-narration-v3*.mp4`）。

---

## v0.9.2 · 2026-11-13 — story_parse 段缓存接内容指纹 + 历史工程脚本统一

**修复**
- `shared/story_parse.py` 的 `seg_NN.json` 旧判据是「文件存在即命中」——改了台词、
  重跑镜头描述或换模型后仍返回旧事件。现改为内容指纹：区间/帧数/模型 + 台词文本 +
  `shots_desc`/`scene_desc` 文件指纹 + `CACHE_VER`（改 prompt 时 +1）。
  旧缓存无 `cache_sig` 时，只在「缓存不比输入旧」时沿用一次，避免无谓重跑大模型；
  新写入的缓存带 `cache_sig`。

**同步**
- 历史工程脚本统一到带门禁版本（旧版备份在各自 `_bak_20261113/`）：
  `2026-11-10…/ep01`、`2026-10-15…/recap-02` 各 5 个（common/cut/r5/r6/r7）；
  `2026-11-05…/ep05` 只有 scripts/common 与 scripts/r6（该集走旧链，不补 cut/r5/r7）。
- 两个工程 `guard --bootstrap` 后四阶段全 OK；ep05 因 cutplan 在成片之后改过，
  门禁如实判 stale（正是它该拦的情况）。

**仓库**
- `main` 快进合并 `cut-first`（此前落后 17 个提交），两分支对齐。

**验证**
- 新增 `tests/test_story_parse.py` 7 项；全套 203 项全绿（跳过 1）。

---

## v0.9.1 · 2026-11-13 — 渲染门禁：保证成片只用「当前输入」生成的产物

**新增**
- `aicutplan/freshness.py`（L0）：四阶段（cut / tts / mix / mux）的**内容指纹基线**。
  每步产出后把「它读到的输入」记进 `json/render_state.json`；判新时重算输入指纹
  逐项比对 —— 一致即证明产物由当前输入生成，不靠 mtime 猜。
  单句重配还额外核对 `mp3_sig` 与旁白文字，抓「元数据新、音频旧」。
- CLI 新命令 `guard`：`--stage mix|mux|all`、`--bootstrap [--force]`、`--json`；
  不通过时退出码 1，并给出「哪个输入变了 / 该补跑哪一步」。

**阻断（重点）**
- `cf_pipeline` 渲染前强制过门禁（`mix` 要求画面+配音最新，`mux` 要求混音最新），
  不通过直接退出 4；新增 `--allow-stale` 显式放行。
- `qc_loop` 同样在 r6/r7 前阻断，避免「改了句 12 却带着句 7 的旧音频混」。
- 工程侧 `r6_mix.py` / `r7_mux.py` 增加 `--check-only` / `--allow-stale` 与自动前检；
  `cut.py` / `elastic_fit.py` 产出后登记基线。手工跑脚本也挡得住。
- 新增 `common.preflight()/stamp()`（工程侧，按需加载仓库实现，找不到时降级为提示）。
- 门禁基线已在 EP01 建立（`guard --bootstrap --force`），四阶段全 OK；
  实测临时改一句旁白后：`r6 --check-only` 与 `r7 --check-only` 均退出 4，
  还原后恢复 OK。

**同步**
- skill 配方 `anime-recap/scripts/{common,cut,r6_mix,r7_mux}.py` 更新为带门禁版本
  （r7 阈值同时由旧标定 1350–1900/130–190/8–13/LRA≤5 换为现行 900–1900/40–190/8–20/LRA≤8）。

**验证**
- 新增 `tests/test_freshness.py` 14 项；全套 196 项全绿（跳过 1）。

---

## v0.9.0 · 2026-11-12 — 冗余清理（clean）：旧版成片 / 备份 / 过期帧 / 可再生中间物

**新增**
- `aicutplan/clean.py`（L0）：按白名单规则盘点并回收工程冗余 ——
  `old_final`（out/03-* 旧版成片）、`dup_final`（与最新归档同内容的副本）、
  `backup`（`*.bak_*` / `*.v3bak`）、`backup_dir`（`tts_v31_backup/`）、
  `qc_frame`（早于成片的 `.qcframes/qc_*.jpg`）、`verify_wav`（旧校验 wav）；
  `--deep` 再加 `deep_seg`（`verify/rapid_seg/`）与 `deep_mid`
  （`out/01-*`、`out/02-*`、`mix-*.wav`、`audio/base-*.wav`）。
- **两段式安全回收**：默认只扫描；`--apply` 移入 `<工程>/.trash/<批次>/` 并写
  `manifest.json`（含原路径与体积），`--restore [--batch]` 原样放回、
  冲突时不覆盖；`--empty` 才真正释放。`project.json` / `json/` / `tts/` / `runs/`
  永不在候选集内。
- CLI 新命令 `clean`：`--project`（必填）、`--deep`、`--apply`、`--category`、
  `--restore`、`--batch`、`--empty`、`--json`。

**清理**
- `aicutplan/__init__.py` 的分层说明是目录分层前的旧版（称「阶段脚本在根目录」），
  已改写为当前 `shared/chain/session/legacy/webui` 结构与依赖方向；版本号
  与 `pyproject.toml` 统一为 0.9.0（此前停留 0.6.0，与 CHANGELOG 脱节）。
- EP01 首次清理：149 项 / 632.4MB 移入 `.trash`（6 个旧版成片、1 个 v35 副本、
  41 个备份文件、`tts_v31_backup/`、96 张过期质检帧、4 个旧校验 wav）。
  清理后 `check` 全过、`runs verify` 指纹一致；`--deep` 另有 1.24GB 可再生中间物待定。

**验证**
- 新增 `tests/test_clean.py` 16 项；全套 182 项全绿（跳过 1）。

---

## v0.8.0 · 2026-11-12 — 作品库布局 + 生成版本（runs）隔离

**新增**
- `aicutplan/workspace.py`（L0）：作品库 `<动漫>/<EPnn>/` 布局、`series.json`、
  `runs/` 不可变快照（同盘硬链接、跨盘复制）、`manifest.json` 产物指纹、
  `latest.json` 指针与校验、`scan_stale` 污染扫描、`migrate` 迁移（重写 workdir）。
- CLI 新命令：`lib path|list|init|migrate`、`runs list|snapshot|latest|verify`。
- `cf_pipeline` 渲染成功后自动归档（`--no-snapshot` 关闭、`--label` 指定标签），
  生成前打印污染检查；`qc_loop` 闭环成功后同样归档。
- r5_tts 为每条 TTS 写入 `mp3_sig` / `synth_at`；`scan_stale` 按音频指纹核对
  （能抓到「元数据更新但音频没重合成」），skill 配方副本同步。

**修复**
- `contracts.lint_narration` 优先采信弹性对齐后的实测（`qc_report.speech`），
  且按**内容**（句子集合 + 槽位容差 0.6s）判断报告是否适用 —— 修掉
  `narrate --export` 刷新 mtime 后把已对齐句子误报超槽（句23/51）。
- 迁移后译中缓存（按工程目录 hash 分桶）需同步复制，否则 web 台 `zh_ready=false`；
  EP01 已按新路径 `519d1e975b37` 迁移。

**迁移**
- 当前工程已迁入 `E:/workbuddy/漫剪作品库/被驱逐的加强师/EP01`，成片 md5 不变
  （`bf4e6445…`），基线归档 `r001`（v3.5）、`r002`（v3.5.1 回填音频指纹）。
- web 台 `studio_state.json` 指向同步，`/review` 54 句、中文台词正常。

**验证**
- 新增 `tests/test_workspace.py` 25 项 + 契约 2 项；全套 166 项全绿。

---

## v0.7.1 · 2026-11-12 — 配置收口：config/ 目录 + L0 配置层

**结构**
- 新增 `config/` 仓库级配置目录：`templates.json`、`tts_calibration.json` 从仓库根迁入，
  新增 `llm_config.example.json` 与 `config/README.md`（文件清单 / 解析顺序 / 环境变量 / 迁移说明）。
- 新增 `aicutplan/config.py`（L0）：`config_dir`（`AICUTPLAN_CONFIG_DIR` 覆盖）/
  `config_file`（config/ 优先、旧根目录只读回退）/ `load_config`（缺失或损坏降级 + 告警）/
  `save_config`（原子写 + 自动建目录）/ `summary`（体检用）。
- `narration.py` 的模板、TTS 校准、LLM 配置读写全部改走配置层；文风文件从硬编码本机路径
  改为 `AICUTPLAN_STYLE_MD` > `config/style.md` > 历史 skill 路径。
- `python -m aicutplan check` 新增配置体检：打印每个配置文件的真实解析路径、大小、
  是否仍在读旧根目录文件。
- `.gitignore` 只忽略 `config/llm_config.json`（其余配置入库）；密钥仍只认环境变量。

**验证**
- 新增 `tests/test_config.py` 17 项：默认目录 / 环境变量覆盖 / config 优先 / 旧路径回退 /
  缺失与损坏降级 / 原子写与建目录 / summary 标记 / narration 四项集成。
- 全套测试 139 项全绿；ep01cf 体检全过；web 台模板与校准接口改为读新目录后正常。

---

## v0.7.0 · 2026-11-12 — 目录分层：全部脚本按层入包

**结构**
- 30 个根目录脚本全部 `git mv` 进 `aicutplan/` 五个子包：`shared/`（共享库 5 个）、
  `chain/`（cut-first 主链 9 个）、`session/`（会话内工具 4 个）、`legacy/`（旧检索腿
  5 个）、`webui/`（web 校对台 7 个）。**根目录零 `.py`**。
- 跨脚本裸导入全部改为包内绝对导入（`import narration as NR` →
  `from aicutplan.shared import narration as NR`）；`ROOT` 统一改为 `parents[2]`
  （仓库根），运行时数据目录（asr/cache/drafts/exports/web/studio_state.json）
  路径与迁移前逐字节一致。
- `cf_pipeline / qc_loop / pipeline / subs_extract / jianying_export` 的子进程路径、
  `cli.py` 门面映射、`start.bat`（→ `py -3.12 -m aicutplan.webui.server`）、
  `tests/test_wrappers` 导入全部同步新位置。
- 依赖方向约束成文：`chain/webui → shared → L0` 单向，`shared` 不得反向依赖
  `chain/webui`（防导入环）。

**验证**
- 122 项 unittest 全绿；27 个模块导入冒烟通过；ep01cf 真实工程回归：
  `check` 全过、`run --subs auto --skip-*` 退出 0、`narrate --export` /
  `causal_graph --export-prompt` / `story_parse --export-prompt` 正常、
  web 台从新路径启动后 5 页面 200 且工程指向保留（studio_state 未迁移）。

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
