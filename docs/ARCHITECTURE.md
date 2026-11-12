# 架构说明 · AIcutplan

> 本文件是「哪个脚本属于哪一层、被谁调用、产出什么」的唯一权威清单。
> 每个根目录脚本顶部的 `# [L...]` 标识与本文件对应。

---

## 1. 一图看懂

```
                          ┌─────────────────────────────────────────┐
   源片 (MKV/MP4)  ─────▶ │ L1 台词源                                │
   自带 ASS/SRT 字幕轨    │ subs_extract.py / asr_local.py (回退)    │
                          └──────────────────┬──────────────────────┘
                                             │ ja-segments / asr_ja / asr_zh
                                             ▼
┌─────────────────── cut-first 新主链（推荐） ───────────────────────────┐
│                                                                        │
│  L2  cf_pipeline.py  ─── 一键编排 ───────────────────────────────┐   │
│       qc_loop.py     ─── 质检闭环（局部重跑）────────────────┐    │   │
│                                                            │    │   │
│  ① causal_graph      因果图谱（L0 evidence 证据回喂）          │    │   │
│  ② cutfirst_plan     画面先定：连续爽点钩子 + 权重选窗          │    │   │
│  ③ cut.py            粗剪（工程目录脚本，仓库外）                │    │   │
│  ④ narrate_cut       旁白后写 / 写作表导出                       │    │   │
│  ⑤ lint_narration    规则粗筛（L0 contracts）                    │    │   │
│  ⑥ r5_tts + elastic_fit   TTS + 音频弹性适配槽位                 │    │   │
│  ⑦ r6_mix + r7_mux   混音 + 合片验收（工程目录脚本）             │    │   │
│  ⑧ qc_visual         会话内逐句质检 → qc_rewrite.json ──────────┘    │   │
│                                                                        │
│  会话内导演：session_desc / shot_desc / scene_from_shots / 手写句子稿  │
└────────────────────────────────────────────────────────────────────────┘
                                             │
                                             ▼  out/03-rapid-narration.mp4
┌─────────────────── web 校对台（旧 17 步链，仍在维护） ─────────────────┐
│  L3  server.py  +  web/  +  pipeline.py（STEPS / doctor）              │
│      8 个页面：/ /review /shots /asr /story /narration /deliver /flow  │
│      逐句审稿、句级锚点挑画面、临时预览、剪映草稿导出                  │
└────────────────────────────────────────────────────────────────────────┘

        ┌────────────────── L0 基础层 aicutplan/ ──────────────────┐
        │ paths · io · media · cache · contracts                    │
        │ subtitles · planning · evidence · cli                     │
        │ 零第三方依赖 / 无副作用 / 单元测试主体                     │
        └──────────────────────────────────────────────────────────┘
                     ▲ 新旧两链共用 3 个 L1 共享库 ▼
        narration.py（LLM/预算） · story_parse.py（事件表）
        smart_align.py（shots_of / _load，勿按旧检索腿归档）
```

---

## 2. 两条链路的关系（重要）

| | cut-first 新主链 | web 台旧 17 步链 |
|---|---|---|
| 入口 | `cf_pipeline.py` / `python -m aicutplan run` | `server.py`（`start.bat`）→ `/flow` |
| 时序哲学 | **画面先定 → 旁白后写** | 旁白定稿 → TTS → 用语音时长裁画面 |
| 核心脚本 | causal_graph / cutfirst_plan / narrate_cut / elastic_fit | align_chain / smart_align / relayout_smart / make_srt |
| LLM 依赖 | 可完全会话内（零 API） | 剧情解析、译中、写稿默认走 DeepSeek |
| 状态 | 当前主线，v3.5 成片 | 只用于**审稿 / 挑画面 / 导出草稿** |

⚠️ **两条链互不 import，却写同一个工程目录**（`json/cutplan.json`、`align.json`、
`narration.json`）。在 `/flow` 页点第 3/4/6 步的「运行」会重建 `story_plan` /
`cutplan` / `narration`，覆盖已定型的 cut-first 数据。**审稿请用逐句台的句级锚点，
不要点流程页的运行按钮，也不要用块级平移**（钩子块取自片尾、留白块和手工换序段
的源时间都不连续）。

---

## 3. 目录结构

```
AIcutplan/
├── aicutplan/                 L0 基础层（可打包、零第三方依赖、单测主体）
│   ├── __init__.py            版本 + 分层说明 + 硬规则
│   ├── paths.py               ProjectPaths：工程根 / json / out / tts / .qcframes
│   ├── io.py                  load_json / dump_json（原子写）/ backup_copy / backup_rename
│   ├── media.py               ffmpeg_bin / probe_json / probe_duration / extract_frame
│   ├── cache.py               content_sig / source_sig / is_stale / sig_valid / cache_valid
│   ├── contracts.py           窗口·句子·证据包 schema + lint_narration / check_alignment
│   ├── subtitles.py           probe_subs / clean_text / parse_srt / merge_same_time / sniff_lang
│   ├── planning.py            hook_score / pick_hook_clip / weight / reason / is_continuous
│   ├── evidence.py            load_shots / score_shot / format_shot_line / compact
│   ├── cli.py                 python -m aicutplan <cmd> 门面
│   └── __main__.py
├── docs/ARCHITECTURE.md       本文件
├── tests/                     标准库 unittest，零第三方依赖
├── web/                       8 个页面的静态文件（html / js / css，无构建）
│
├── cf_pipeline.py             L2 一键主链编排
├── qc_loop.py                 L2 质检闭环编排
├── causal_graph.py            L1 新链 ①
├── cutfirst_plan.py           L1 新链 ②
├── narrate_cut.py             L1 新链 ④
├── elastic_fit.py             L1 新链 ⑥
├── qc_visual.py               L1 新链 ⑧
├── subs_extract.py            L1 台词源（内嵌字幕优先）
├── evidence_feed.py           L1 证据回喂（story_parse / causal_graph 的库）
├── story_parse.py             L1 共享库（事件表 / 块表 / 段缓存）
├── narration.py               L1 共享库（LLM 客户端 / 预算门禁 / 模板）
├── smart_align.py             L1 共享库（shots_of / _load / 旧句级对齐）
├── asr_pipeline.py            L1 共享库（译中缓存 / 断点续传）
├── asr_local.py               L1 台词源（faster-whisper 回退）
├── session_desc.py            L1 会话内工具（读图批次 plan/merge）
├── shot_desc.py               L1 会话内工具（镜头证据包生成器）
├── scene_desc.py              L1 会话内工具（2s 窗场景解析，历史实现）
├── scene_from_shots.py        L1 会话内工具（镜头描述推导 2s 窗，零读图）
├── server.py                  L3 web 台入口
├── pipeline.py                L3 17 步定义 / 环境体检 / 后台任务
├── sentence_studio.py         L3 句级锚点数据层
├── review.py                  L3 逐句三轨校对数据层
├── preview_render.py          L3 临时预览渲染
├── jianying_export.py         L3 剪映草稿导出
├── jianying_worker.py         L3 剪映子进程
├── prepare_media.py           L1 旧链：洗片
├── shots_scan.py              L1 旧链：镜头切分
├── align_chain.py             L1 旧链：对齐前置（新链已停用）
├── relayout_smart.py          L1 旧链：句级重排（新链由 elastic_fit 取代）
├── make_srt.py                L1 旧链：字幕 / 烧字幕
│
├── pyproject.toml / requirements.txt / .gitignore / CHANGELOG.md
├── templates.json / tts_calibration.json / start.bat / studio_state.json（本机，忽略）
└── cache/ asr/ drafts/ exports/   运行时数据（.gitignore）
```

### 为什么阶段脚本保持在根目录

这是**硬约束**，不是没整理干净：

1. 跨脚本互相定位依赖 `ROOT = Path(__file__).resolve().parent`；
2. `cf_pipeline.py` 用 `ROOT / "xxx.py"` 以子进程方式启动阶段脚本；
3. 仓库外的工程脚本 `cut.py / r5_tts.py / r6_mix.py / r7_mux.py` 按「`project.json`
   所在目录」约定读写 `json/` `out/` `tts/`；
4. `server.py` 用 `ROOT / "web"` 提供静态页。

把它们搬进 `src/aicutplan/` 会同时打断以上四条。因此**只把无副作用、可单测的共享
能力抽进包**，阶段脚本保持扁平，用顶部 `# [L...]` 注释 + 本文件标识分层。

---

## 4. L0 包模块契约

| 模块 | 关键 API | 谁在用 |
|---|---|---|
| `paths` | `ProjectPaths(pj)` / `repo_root()` / `script()` | 所有阶段脚本、cli、tests |
| `io` | `load_json` / `dump_json` / `write_text_atomic` / `backup_copy` / `backup_rename` | subs_extract、evidence_feed、cache、tests |
| `media` | `ffmpeg_bin` / `probe_json` / `probe_duration` / `extract_frame` | qc_visual、subs_extract、tests |
| `cache` | `content_sig` / `file_sig` / `source_sig` / `project_key` / `is_stale` / `sig_valid` | subs_extract `--check`、cli `check`、tests |
| `contracts` | `validate_windows` / `check_alignment` / `check_tts_consistency` / `lint_narration` / `summarize_quality` | cf_pipeline、cli `check`、tests |
| `subtitles` | `probe_subs` / `clean_text` / `parse_srt` / `merge_same_time` / `sniff_lang` / `norm` | subs_extract、asr_local、cf_pipeline、tests |
| `planning` | `pick_hook_clip` / `hook_score` / `weight` / `reason` / `is_continuous` | cutfirst_plan、tests |
| `evidence` | `load_shots` / `score_shot` / `format_shot_line` / `compact` | evidence_feed、tests |
| `cli` | `main()` | `python -m aicutplan` / `aicutplan` 控制台脚本 |

**约定**：阶段脚本里若仍保留 `parse_srt`、`_score`、`lint_narration` 等名字，那是
**兼容别名**（指向包内实现），不是第二份实现。新增逻辑一律先问「能不能放进 L0」。

---

## 5. 工程数据契约

工程根 = `project.json` 所在目录（**与仓库根无关**）。完整路径表见
`aicutplan/paths.py::ProjectPaths`。

| 产物 | 关键字段 | 写入方 | 主要读取方 |
|---|---|---|---|
| `json/story_plan.json` | `events[]` / `blocks[]` / `causal` / `segment` | story_parse / causal_graph | cutfirst_plan / narrate_cut / evidence_feed |
| `json/cutplan.json` | `windows[]{new_start,new_end,src_start,src_end,dur,tier,ost_mode,reason,sent_ids}` | cutfirst_plan / cut.py 回写 | cut / narrate_cut / r6 / web 台 |
| `json/align.json` | `sent_win_idx{句i:[lo,hi]}` / `beat_win` | narrate_cut | r6 / qc_visual / web 台 |
| `json/narration.json` | `items[]{i,t0,dur,text,beat}` + `stats` | narrate_cut | r5 / r6 / r7 / qc_visual |
| `json/shots_desc.json` | `rows[]{i,a,b,desc,obj,depth,facts,dlg}` | session_desc / shot_desc | smart_align / evidence_feed / qc_visual / subs_extract |
| `json/scene_desc.json` | `rows[]{t,t1,desc,cam,dlg,shot_i,depth}` | scene_from_shots | narrate_cut / smart_align |
| `json/tts-result.json` | `items[]{i,text,tts_dur}` | r5_tts / elastic_fit | lint / r6 / contracts |
| `json/qc_visual.json` / `qc_rewrite.json` | 逐句判定 / 失配清单 | qc_visual | qc_loop |
| `tts/NNN.mp3` | 按句号三位数命名 | r5_tts | r6 / elastic_fit |

**不变量**（`aicutplan.contracts` 自动校验）：

- `narration.items[i].t0 == cutplan.windows[ws].new_start`（cut.py 回写帧网格后必须重跑
  `narrate_cut --sentences`，否则整体偏 ~0.04s）；
- 窗口 `new_start/new_end` 首尾相接、`dur` 与跨度一致；
- `tier=hold_hl` / `ost_mode=original` 是原声留白窗，**不写旁白、不算未覆盖**；
- 钩子块相邻窗口 `src_end == 下一窗 src_start`（`planning.is_continuous`）；
- TTS 文本与旁白逐字一致，且 mp3 不早于 `narration.json`。

---

## 6. 缓存规则：文件存在 ≠ 内容最新

这是本项目**反复复发**的一类事故，所有新脚本必须遵守：

| 事故 | 根因 | 现在的防线 |
|---|---|---|
| 文本新词、成片念旧词 | `r5_tts --only` 见 mp3 存在就跳过合成 | 改名备份后强制重合成；`qc_loop` 校验 mp3 mtime |
| 质检帧取到上一版成片 | 抽帧缓存按句号命名、无版本 | `qc_visual --plan` 强制重抽 + 按成片 mtime 失效 |
| 改了 cutplan 但画面是旧的 | 分段缓存只按组序号命名 | `cut.py` 用窗口指纹 `md5(offset\|span\|expr)` |
| 换工程目录后译中缓存读不到 | 缓存键 = `md5(工程目录)` | `cache.project_key` 明确记录这一语义 |
| 源片换了但台词源没更新 | 产物未记录输入版本 | `ja-segments.json` 顶层 `sig` + `cache.sig_valid` |

**规范**：

1. 产物写盘时把 `aicutplan.cache.content_sig(...)` 存进顶层 `sig`；
2. 读缓存时用 `cache.cache_valid(产物, 输入, expected_sig=...)` 判定，不要写
   `if p.exists(): 跳过`；
3. 备份用 `io.backup_copy`（保留原文件）或 `io.backup_rename`（移走以触发重建），
   两者都不调用 `unlink`，规避本机批量删除拦截；
4. 跑完自动校验产物与输入是否一致（ASR / 三帧 probe / 波形互相关），不要靠人看片。

---

## 7. 解释器口径

本机有两个解释器，装错会静默产出错误结果：

| 解释器 | 有 | 用途 |
|---|---|---|
| `D:/Python312/python.exe` | torch / numpy / PIL | 规划、渲染、web 台、单元测试 |
| `~/.workbuddy/binaries/python/envs/default/Scripts/python.exe` | edge_tts / faster_whisper | r5_tts、elastic_fit、asr_local |

`qc_loop.py` 用 `--python` / `--tts-python` 双解释器；`cf_pipeline.py` 用
`sys.executable` 跑规划/渲染，工程目录的 r5 由闭环另行指定。
`ffmpeg` 不在 PATH 时设 `AICUTPLAN_FFMPEG` / `NARR_FFMPEG`。

**仓库外依赖**：`cut.py` / `r5_tts.py` / `r6_mix.py` / `r7_mux.py` 位于每个工程目录
（以及 `~/.workbuddy/skills/anime-recap/scripts/` 配方副本），不在本仓库。缺一即整链
失败；采样率修复等关键改动需同步两处。

---

## 8. 测试策略

`tests/` 全部用标准库 `unittest` + `tempfile`，**零 ffmpeg / 零网络 / 零 API / 零模型**：

```bash
D:/Python312/python.exe -m unittest discover -s tests -v
# 或（装了 pytest 时）
pytest
```

覆盖范围：`subtitles`（SRT 解析 / 清洗 / ASS 合并 / 语言判定）、`planning`（连续钩子
选段 / 权重 / 连续性校验）、`evidence`（打分 / 压缩 / 时间顺序）、`contracts`（窗口 /
对齐 / TTS 一致性 / lint）、`cache`（指纹 / 失效 / sig_valid）、`io`（原子写 / 两类备份）、
`paths`（工程解析）、`cli`（版本 / 帮助）。

凡是需要真实成片/模型的验证放在流水线的 `qc_visual --probe`、`ASR 复检`、
`r7_mux 12 项验收`里，不进单元测试。

---

## 9. 已知风险与待办

1. **两条链写同一工程目录**（见 §2）：web 台块级平移在 cut-first 数据上语义不成立，
   只应用句级锚点审稿。
2. **`story_parse.py` 段缓存 `seg_XX.json` 仍按文件存在判定**：P0-2 的证据回喂拼进
   prompt 后，若缓存命中则新证据不会进模型，需 `--force` 或接入 `cache.sig_valid`。
   这是当前剩下的最大「旧片段复用」风险点。
3. `shot_desc.py` / `scene_desc.py` / `session_desc.py` 脚本本身零代码引用，但产物
   `shots_desc.json` / `scene_desc.json` 是新链核心输入 —— **保留为数据契约生成器，
   不要当死代码删除**。
4. skill 配方里的 `r7_mux.py` 质检阈值仍是旧值（1350–1900 字 / 130–190 句 / LRA≤5），
   拿它跑 v3 结构会误判 12 项失败；`ep01` / `ep05` / `recap-02` 的 `r6_mix.py`
   仍带已修复的采样率 bug。
5. `main` 分支停在检索腿版 `4871650`，`cut-first` 是主线，尚未合回。
6. 说话人分离、原声能量/语速特征、数字事实 OCR 交叉校验尚未实现
   （「十三万点 → 一万三千点」靠人工发现）。
7. `.workbuddy/run_chain.py` / `run_chain2.py` 是会话临时脚本（被 `.gitignore`
   排除、不入库），其中 `run_chain2.py` 仍引用已删除的 `auto_script.py`。
   它们不属于项目代码，**不要执行**；`.workbuddy/memory/` 是工作区记忆，勿删。
