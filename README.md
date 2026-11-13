# AIcutplan · 漫剪解说流水线

> 动漫速讲的一集从片源到成片：**画面先定 → 旁白后写 → 生成后质检闭环**。
> 另带一个本地 web 校对台，用于逐句审稿、挑画面、导出剪映草稿。

项目里存在**两条链路**，先选对入口：

| 你要做什么 | 用哪条 | 入口 |
|---|---|---|
| 出一集成片（当前主线） | **cut-first 新主链** | `python -m aicutplan run` 或 `python -m aicutplan.webui.server` 同级用门面 |
| 逐句审稿 / 挑首镜尾镜 / 看三轨 / 导出草稿 | **web 校对台（旧链）** | `python -m aicutplan serve` → `http://127.0.0.1:8765` |

完整分层、依赖全表与设计约束见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**；
变更历史见 [CHANGELOG.md](CHANGELOG.md)。

---

## 快速开始

```bash
# 核心（L0 基础层 / CLI / 单元测试 / web 台）零第三方依赖，Python >= 3.10 + ffmpeg 即可
python -m aicutplan version
python -m aicutplan check --project <工程目录>/project.json

# 出成片需要 TTS 与（质检用的）Pillow
pip install -e ".[tts,vision]"
# 或按需安装：pip install -r requirements.txt（文件内按用途分组，默认全注释）

# web 校对台
python -m aicutplan serve    # 或双击 start.bat → http://127.0.0.1:8765
```

统一 CLI 门面（参数原样透传给对应阶段脚本）：

```bash
python -m aicutplan subs --project <pj> --apply --refresh-derived   # 内嵌字幕优先
python -m aicutplan run  --project <pj> --sentences sents.json      # 一键主链
python -m aicutplan qc   --project <pj> --plan 6                    # 会话内质检批次
python -m aicutplan check --project <pj>                            # 工程体检
python -m aicutplan clean --project <pj>                            # 冗余清单（--apply 回收）
python -m aicutplan serve                                           # web 台
```

`run / plan / subs / qc / qc-apply / narrate / parse / serve` 都是薄门面，
业务逻辑在 `aicutplan/` 各子包里，不存在两份实现。

---

## 项目结构

```
aicutplan/                    Python 包（全部代码按层入包，根目录零 .py）
  paths io media cache contracts subtitles planning evidence workspace
                              L0 基础层：路径 / 原子写 / ffmpeg / 指纹 / 契约 / 领域纯函数
  cli.py __main__.py          L2 统一 CLI 门面（python -m aicutplan <cmd>）
  shared/                     L1 共享库：narration story_parse smart_align
                              asr_pipeline evidence_feed（新旧两链共用）
  chain/                      L1 cut-first 主链：cf_pipeline cutfirst_plan causal_graph
                              narrate_cut elastic_fit qc_loop qc_visual subs_extract asr_local
  session/                    L1 会话内工具：shot_desc session_desc scene_desc scene_from_shots
  legacy/                     L1 旧检索腿：prepare_media shots_scan align_chain
                              relayout_smart make_srt（web 台旧链运行按钮仍调用）
  webui/                      L3 web 校对台：server pipeline sentence_studio review
                              preview_render jianying_export jianying_worker
config/           仓库级配置（templates.json / tts_calibration.json / llm_config.example.json）
docs/             架构说明（分层图 / 数据契约 / 缓存事故清单）
tests/            标准库 unittest 测试（零 ffmpeg / 零网络 / 零模型）
web/              8 个页面的静态文件（无构建）
pyproject.toml requirements.txt .gitignore CHANGELOG.md
```

分层规则：`chain/webui → shared → L0` 单向依赖；`legacy → shared/webui` 允许；
`shared` 不得反向 import `chain/webui`（会成环）。所有脚本里的 `ROOT` 一律指仓库根
（`parents[2]`），运行时数据（`cache/ asr/ drafts/ exports/ studio_state.json`）都落
在仓库根，与脚本所在层无关。详见 [架构说明 §3](docs/ARCHITECTURE.md)。

---

## 配置

| 文件 | 内容 | 说明 |
|---|---|---|
| `config/templates.json` | LLM 提示词模板库（5 套） | web 台「旁白台」可编辑，改动即落盘 |
| `config/tts_calibration.json` | TTS 语速 / 固定静音校准 | `recalibrate()` 产出，非手改 |
| `config/llm_config.example.json` | LLM 非密钥配置样例 | 复制为 `config/llm_config.json` 后生效（已忽略） |
| `config/style.md`（可选） | 文风规则覆盖 | 缺省读 skill 的 `references/style.md` |
| `studio_state.json` | web 台会话状态（本机） | 运行时数据，不入库 |
| `<工程目录>/project.json` | **工程级**配置（源片/音色/语速/字幕） | 跟工程走，不在本仓库 |

解析顺序：`AICUTPLAN_CONFIG_DIR` → `config/<name>` → 旧根目录回退 → `config/<name>`（新写入落这里）。
读取损坏/缺失返回默认值并在 stderr 告警，不会打断流水线；写入走原子写并自动建目录。

环境变量：`AICUTPLAN_CONFIG_DIR`（配置目录）、`AICUTPLAN_STYLE_MD`（文风文件）、
`DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`（覆盖 LLM 地址/模型）、`DEEPSEEK_API_KEY`（**唯一**密钥来源）。
体检会打印每个配置文件的真实解析路径与状态：

```bash
python -m aicutplan check --project <pj>
```

---

## 作品库与版本（多动漫 / 多集）

**目录约定**（库根默认 `<仓库同级>/漫剪作品库`，可用 `AICUTPLAN_LIBRARY` 或
`config/library.json` 覆盖）：

```
漫剪作品库/
└── 被驱逐的加强师/                 # 动漫名（文件夹名，series.json 存别名/备注）
    ├── series.json
    └── EP01/                       # 一集 = 一个工程（project.json 在这里）
        ├── project.json  json/  out/  tts/
        └── runs/
            ├── r001_20261112-2341_v3.5/{manifest.json, final.mp4, narration.json, ...}
            └── latest.json         # 最新归档指针（含成片指纹）
```

**每次生成自动归档**：`aicutplan run` 成功后把成片、关键 JSON、**全部 `tts/*.mp3`、
镜头/场景证据与 `ja-segments.json`** 快照进 `runs/<run_id>/`（当前约 65 个产物，
足以整集回滚重出成片），写 `manifest.json`（产物指纹 + 输入句子稿 + 来源工程）并更新
`latest.json`。同盘用**硬链接**，后续 ffmpeg 覆盖 `out/` 只换 inode，快照不可变；
跨盘自动退化为复制。不想归档加 `--no-snapshot`，自定义标签用 `--label v36`。

```bash
# 作品库
python -m aicutplan lib init 被驱逐的加强师 --alias 追放チート付与魔術師
python -m aicutplan lib list                     # 动漫 / 集 / 最新归档 / 告警
python -m aicutplan lib migrate --project <pj> --series <名> --episode EP01 [--move]

# 版本
python -m aicutplan runs snapshot --project <pj> --label v36 --sentences <稿>
python -m aicutplan runs list --project <pj>
python -m aicutplan runs latest --project <pj>   # 校验成片指纹后打印指针
python -m aicutplan runs verify --project <pj>   # 校验该 run 全部产物
```

**如何避免「引用了旧产物」**：

1. 成片只认 `latest.json` 指向的 run —— `runs latest` 会校验成片指纹，被替换/缺失
   直接报错，**不静默回退**；
2. 渲染前**硬门禁**：`cf_pipeline` / `qc_loop` / 工程侧 r6·r7 会校验「画面/配音/混音
   是否由当前输入生成」，不通过直接阻断（退出码 4），不再只是告警；
3. TTS 音频带内容指纹（`mp3_sig`），r5 合成时写入，扫描按指纹核对而不是 mtime；
4. 多集并行：不同集是独立目录，各自 `cf_pipeline` 互不干扰；同集同时只跑一个渲染。

**渲染门禁**（内容指纹基线，见 `aicutplan/freshness.py`）：

```bash
python -m aicutplan guard --project <pj>            # 四阶段总览 + mix/mux 能否放行
python -m aicutplan guard --project <pj> --stage mix
python -m aicutplan guard --project <pj> --bootstrap # 给已有工程建基线（先确认产物可用）
python -m aicutplan guard --project <pj> --json
# 工程侧脚本也可单独自检：
python <pj>/r6_mix.py --project <pj> --check-only    # 0=可渲染 / 4=应阻断
```

判定口径：每步产出后把「它读到的输入」的指纹（大文件首尾 4MB + 大小，小文件全量）
记进 `json/render_state.json`。改了旁白、窗口、音色或任一 mp3，对应上游立即变
`stale`，并给出「该补跑哪一步」；确要强行出片用 `--allow-stale`，日志会标注。

**清理冗余**（旧版成片 / 备份 / 过期质检帧 / 可再生中间物）：

```bash
python -m aicutplan clean --project <pj>            # 只列清单（默认不改文件）
python -m aicutplan clean --project <pj> --apply    # 移入 <工程>/.trash/<批次>/（可恢复）
python -m aicutplan clean --project <pj> --restore  # 放回最近一批（冲突不覆盖）
python -m aicutplan clean --project <pj> --empty    # 清空 .trash，真正释放空间
python -m aicutplan clean --project <pj> --deep     # 连同可再生的中间大件一起看
```

`project.json`、`json/` 当前产物、`tts/` 当前音频、`runs/` 归档永不进入候选集；
`--deep` 才涉及 `out/01-*`、`out/02-*`、`mix-*.wav`、`audio/base-*.wav`、
`verify/rapid_seg/`（都能重跑生成，但重跑要花时间，按需选）。

当前 EP01 已迁入：`E:/workbuddy/漫剪作品库/被驱逐的加强师/EP01`（迁移会重写
`project.json` 的 `workdir`；web 台工程指向已同步）。

---

## cut-first 主链

时序翻转是这套流水线的核心：旧做法是「旁白定稿 → TTS → 用语音时长裁画面」，
语义匹配从起点就是被动妥协；现在**先依据剧情理解定输出时间轴**，旁白对着已定型的
时间轴写，同步靠生成顺序保证。

```bash
PY=D:/Python312/python.exe
PJ=<工程目录>/project.json

$PY -m aicutplan run --project $PJ --sentences sents.json --qc
# 等价：python -m aicutplan run --project $PJ --sentences sents.json --qc
```

| # | 脚本 | 做什么 | 产物 |
|---|---|---|---|
| 0 | `chain/subs_extract.py`（`--subs yes`） | 源片自带字幕时直接提取，跳过 ASR | `ja-segments.json` / `asr_ja.json` / 译中缓存 |
| 1 | `causal_graph.py` | 事件补 `func/cause/result`，证据回喂 | `story_plan.causal` |
| 2 | `cutfirst_plan.py` | 画面先定：连续爽点钩子 + 叙事权重选窗 | `json/cutplan.json` |
| 3 | `cut.py`（工程目录） | 粗剪无声视频 + 原声轨 | `02-video-rapid-silent.mp4` |
| 4 | `chain/narrate_cut.py` | 旁白后写 / 导出写作输入表 | `narration.json` / `align.json` / `window_table.md` |
| 5 | `lint_narration`（L0 contracts） | 窗口全覆盖 / 槽位预算 / 顺序单调 | 违规清单（默认阻断） |
| 6 | `r5_tts.py` + `elastic_fit.py` | TTS + 音频弹性适配已定槽位 | `tts/NNN.mp3` / `qc_report.json` |
| 7 | `r6_mix.py` + `r7_mux.py` | 混音 + 合片 + 12 项验收 | **`out/03-rapid-narration.mp4`** |
| 8 | `qc_visual.py`（`--qc`） | 会话内逐句质检批次 | `.qcframes/session/grid_XX.jpg` |

`--sentences <json>` = 会话内已写好的句子稿（WorkBuddy 导演模式，推荐，零 API）；
不传则 `narrate_cut` 走 DeepSeek 自主写作。`--skip-plan / --skip-tts / --skip-render`
用于只跑部分阶段，配合 `python -m aicutplan check` 做预检。

### 关键数据契约

- `narration.items[i].t0` **必须等于**该句 `ws` 窗口的 `new_start`。`cut.py` 会把帧网格
  吸附后的精确时间回写进 `cutplan.json`（整体可能偏 ~0.04s），所以**重切之后必须重跑
  `narrate_cut --sentences`**，否则全片音画错位。
- `tier=hold_hl` / `ost_mode=original` 是原声留白窗，不写旁白、不算未覆盖；r6 按
  「谁说话谁在场」做 0dB 原声门控，旁白不能塞进留白块。
- 钩子 = **源片连续、未经裁切重排的一整段爽点**（不是全球检索的散装镜头），
  由 `project.json` 的 `recap.hook_clip=[a,b]` 在规划阶段锁定；钩子镜头不进 `used`，
  正片原样再演。`window_table.md` 的钩子块只列字数槽位 —— 这是剪辑规划决策，
  **不属于旁白撰写素材**。
- 全部字段与不变量见 [架构说明 §5](docs/ARCHITECTURE.md)，自动校验用
  `python -m aicutplan check --project <pj>`。

### 验收口径（cut-first 语义）

`r7_mux.py` 的 12 项验收；指标不再是旧链的「字/刀」「旁白覆盖」，而是：

- **句窗比** = 总字数 / 句数
- **语音覆盖** = Σ实测净语音 / Σ槽位（不含静音）
- **槽位贴合** = 超槽句数（`elastic_fit` 后应为 0）

当前 v3.5 实测：338.1s / 1057 字 / 54 句 / 覆盖 69.0% / 原声留白 31.1% /
−13.3 LUFS / LRA 5.2 / 会话内质检 54 句 0 失配。

---

## 会话内工作流（零 DeepSeek API）

视觉描述、旁白写作、质检判定都可在 WorkBuddy 会话内完成：

```bash
# 1) 镜头证据包：分批读图（每轮 ≤8 帧）
$PY aicutplan/session/session_desc.py --project $PJ --plan 8      # 生成批次清单
#    会话内读图后： $PY aicutplan/session/session_desc.py --project $PJ --merge
$PY aicutplan/session/scene_from_shots.py --project $PJ           # 推导 2s 窗场景描述（零读图）

# 2) 导出写作输入表，会话内写句子稿 sents.json
$PY -m aicutplan narrate --project $PJ --export       # → window_table.md
$PY -m aicutplan narrate --project $PJ --sentences sents.json

# 3) 生成后质检闭环
$PY -m aicutplan qc --project $PJ --plan 6         # 2×3 宫格图 + plan_XX.json
#    会话内逐格判定，写 .qcframes/session/verdict_XX.json：{"i":3,"match":0,"why":"..."}
$PY -m aicutplan qc --project $PJ --merge          # → qc_visual.json + qc_rewrite.json
$PY -m aicutplan qc --project $PJ --probe          # 失配句抽 3 帧复核，排除单帧假阳性
$PY -m aicutplan run --project $PJ --qc-apply <改好的句子稿>
```

- 抽帧取**句子首窗中点**（一句可跨 6 窗，整句中点会落到后半段画面）；`--plan`
  强制重抽并按成片 mtime 失效缓存；全黑帧自动 +0.35s 重抽。
- `qc_loop` 校验失配句确实改词，并在 r5 之后检查 mp3 是否真的被重写
  （防「句子稿新词 / 成片旧音频」），不通过即中止后续混音。
- `--dry-run` 只打印命令；`--qc-force` 在文本未变时也强制重合成。
- **换窗口不走闭环**：改 `cutplan.json` 的 `src_start/src_end` 后需重跑
  `cut.py → narrate_cut --sentences → r6_mix → r7_mux`（画面变了音频没变，无需 r5）。

### 批量出片的硬约束（务必遵守）

零 API ≠ 脚本一键无人值守。LLM 决策搬到会话内后，**必须由会话内逐段对着画面写稿**：
`sents.json` 每句都自带 `ws/we` 绑定，句子内容必须就是它覆盖的那几个窗口里正在发生的事。

**严禁**把「按剧情顺序写好的 N 句」按窗口序号均匀分配 —— 窗口顺序不等于源片时间顺序
（钩子前置、块间取镜会跳时间线），这样贴会造成整条音画错位，且钩子段没有专属爽点文案。
2026-11-13 三集首版就是这样废掉的：钩子画面（掏石子炸开）配了「她把自己关了两年」。

正确姿势：

1. 按源片剧情顺序挑**连续段**（每段约 14s），段内按时间序切 1.2–3.5s 窗口，画面不回跳；
2. 每段对着该段画面与台词写 2–3 句，字数按窗口时长分配（`字数 / 槽长 < 4.95` 字/秒）；
3. 钩子 = 源片**连续未裁切爽点段**，单独写 3 句前置钩子文案，正片里原样再演一遍；
4. 末尾约 10s 原声留白；
5. 自检：`narrate --sentences` 覆盖必须是 `N/N`、`qc_report` 超槽 0、非钩子窗口
   `src_start` 不逆序。

**唯一入口（v0.9.6）**：编排与渲染逻辑已收进仓库 `aicutplan/batch/`
（`planner.py` 导演稿→窗口表，`pipeline.py` 链路），受版本控制、单测与 CI 覆盖。
导演稿是**数据**：`_batch/director_EP0X.json`；作品库侧只留 `run.py` 薄封装。

```bash
# 在仓库根运行（或先 pip install -e .）
python -m aicutplan batch --project <pj>/project.json --script _batch/director_EP02.json
python -m aicutplan batch --project <pj>/project.json --from-step narrate  # 断点续跑
python -m aicutplan batch --project <pj>/project.json --dry-run            # 只打印命令
# 作品库侧等价入口（自动处理运行目录）
python _batch/run.py EP02 EP03
```

断点续跑会先做**前置完整性校验**：缺产物、`hook_clip` 未定义、句子稿契约不过
一律退出 6——v1 事故里 EP03 用 `--from-step plan` 跳过写 `hook_clip` 的步骤，
钩子退化成空镜却无人报错，这个洞已堵死。TTS 解释器可用
`AICUTPLAN_TTS_PY` 指定（装了 edge-tts 的那个）。

归档会记录质检状态 `qc.state`（`verified` / `partial` / `unverified`）；
`--require-qc` 或 `AICUTPLAN_REQUIRE_QC=1` 可强制「未逐格质检不许归档」。
默认只记录不阻断，避免把未跑 qc 的历史归档卡死（dev 宽松 / prod 强制）。

逐集可核对的导演表 `_batch/director_EP0X.md` 与音画对照表
`_batch/alignment_EP0X.md` 保留；旧的 `_batch/{build,director,rebuild}.py`
已标 DEPRECATED，仅作事故追溯，不再维护。

### 配方标定模式（v0.10.0，默认）

`run.py` 现在默认用 `director_v3_EP0X.json` —— 严格按「平时动漫」四集实测配方：
11 字一句、1.32s 一刀、六段式叙事、主角不报名、6 段短插 + 唯一 30s 大鱼原声、
YunxiNeural `+26%`、烧单行中文 + 原声日文双轨字幕、响度 −13.2 / LRA ≤4.5。

```bash
python _batch/make_v3.py                 # 从 v3_data.py 重生成三集稿
python _batch/run.py EP02 EP03 EP04      # 按配方出片（默认 v3）
python _batch/run.py EP02 --v2           # 回退到旧版 v2 稿
```

`r7_mux` 验收分成两段：**配方不变量**逐条打 ✓/✗；**契约限制项**
（切点密度 / 单镜中位 / 镜:句 / 字/刀 / 段数）只打印实际值与配方目标。
原因是「一句跨多刀」会违反 `t0 == wins[ws].new_start` 的音画契约并留下句间空隙，
本实现选择保留契约，不用放宽阈值伪装达标。


**已固化为强制契约（v0.9.5）**：`sents.json` 每句必须带
`src: [源片起, 源片止]`，`narrate --sentences` 会在**落盘前**拿它与绑定窗口的
`src_start/src_end` 逐一比对，不符直接退出码 5、不写任何产物；同时校验
`recap.hook_clip`、钩子段连续性、钩子块 ≥2 句专属文案、正片稿内源区间不回跳。
`narration.stats.contract` 会写 `src-v2`。

```bash
# 历史稿（无 src，如 EP01/my_sentences_v35.json）重出时显式豁免并打 legacy 标记
$PY -m aicutplan narrate --project $PJ --sentences sents.json --legacy-sents
# 或：AICUTPLAN_LEGACY_SENTS=1
```

注意：窗口源时间回跳只作**信息项**，不阻断——EP01 段内回跳 6 次仍是 0 失配的
合格成片（它逐窗对着画面写）；真正拦的是「句子声明的画面 ≠ 它绑定的窗口」。
回归测试见 `tests/test_narration_contract.py`（19 项，含 v1 均匀分配稿负向用例）。

---

## 内嵌字幕优先（跳过音转字）

MKV 常自带 ASS/SRT 字幕轨（有的同时有日文、英文）。**字幕是确定性文本**，比 ASR
转写准：本项目 small 模型出现过「ついほ?」「エンバーターチュー」这类幻听，且
faster-whisper 的段时间戳会漂移 3–16s；字幕还常带「（バリオス）」说话人标注。
检测到文本字幕就直接提取，不再跑 ASR。

```bash
$PY -m aicutplan subs --project $PJ                          # 只探测对比，不写文件
$PY -m aicutplan subs --project $PJ --apply --refresh-derived
$PY -m aicutplan subs --project $PJ --prefer en             # 改成优先英文字幕
$PY -m aicutplan subs --project $PJ --check                 # 校验内容指纹是否过期
```

- 探测**原始源片**（`project.json` 的 `source_video`），不能用 `out/01-video-no-ad.mp4`
  —— 洗片转封装时字幕轨已被丢弃。
- 排除图像字幕（PGS/DVB，需 OCR）→ 按 `--prefer` 语言优先级 → SRT/WEBVTT 优先于
  ASS；ASS 会合并同 `start/end` 的多行（实测 516 行 → 352 条，与 SRT 一致）。
- 产物与现有链路兼容：`json/ja-segments.json`、`asr_ja.json`、`asr/<pkey>/asr_zh.json`。
  **译中缓存按「±20s 时间窗 + 原文相似度」迁移并补齐条数**，否则 `zh_subtitles`
  按索引取译文会整体错行；匹配不上的保留原文标 `translated=false`。
- 顶层写 `source: embedded-subtitle` 与 `sig`，`--check` 用 `aicutplan.cache.sig_valid`
  比对，源片换了但台词源没更新时返回非 0。
- `asr_local.py` 检测到文本字幕会自动跳过 faster-whisper（`--force-asr` 强制）；
  `python -m aicutplan run --subs yes` 在链路开始前替换，默认 `auto` 只提示、不动已定型工程。
- 时间轴前提：字幕时间是源片原始秒。若洗片裁过广告，需先给字幕加偏移，脚本不自动估计。

实测当前工程：台词源 ASR 404 段 → 内嵌字幕 352 条，`shots_desc.dlg` 覆盖率
78.6% → 88.6%（镜 1 从「ついほ?」变成「（バリオス）追放？」）。

---

## web 校对台（旧 17 步链）

服务端只用 Python 标准库 `http.server`，前端无构建，除 ffmpeg 外没有硬依赖。
**它仍可用于审稿与挑画面，但不要用它重跑生成步骤**（会覆盖 cut-first 数据，见下方风险）。

| 页面 | 路径 | 做什么 |
|---|---|---|
| 制作流水线 | `/flow` | 17 步状态灯与后台任务（**只看看，别点运行**）；新建工程入口 |
| 镜头台 | `/shots` | 逐镜缩略图、时长分布；合并 / 删除 / 切分 / 挪边界 / 标原声 |
| 台词台 | `/asr` | 原文与中文译文对照，逐段可改；就地触发分批译中 |
| 剧情台 | `/story` | 事件表 + 块表人工审核；写 `script.json` / 初版 `cutplan.json` |
| 逐句校对台 | `/review` | **旁白 / 画面 / 人物台词三轨对齐**，逐句定【首镜, 尾镜】、确认 |
| 锚点校对台 | `/` | 按剧情块整块平移源片取段（cut-first 工程**不适用**） |
| 旁白工作台 | `/narration` | 模板 / 草稿快照 / 定稿 / 写回（旧 DeepSeek 写稿路径） |
| 交付台 | `/deliver` | 成片播放（Range 拖动）、临时预览、导出剪映草稿 |

⚠️ **两条链写同一个工程目录**：`/flow` 页第 3/4/6 步的「运行」会调旧 ASR /
剧情解析 / 旁白台脚本，重建 `story_plan` / `cutplan` / `narration`，覆盖已定型的
cut-first 数据。**审稿只用逐句台的句级锚点 + 确认（会话状态，不写盘）**；块级平移
在钩子块（取自片尾）、原声留白块、手工换序段上会打乱画面。

点「写回」时 `/api/save` 只改 `cutplan.json` 的 `src_start/src_end`
（其余 `dur/new_start/new_end/tier/ost_mode/reason/sent_ids` 全保留），写前自动
生成 `cutplan.json.bak.<时间戳>`。写回后需重跑 `cut → narrate_cut → r6 → r7`。

流程页部分步骤显示「缺失」属正常：cut-first 换了产物名与工程目录，旧步骤按旧路径
检查。详见 [架构说明 §2](docs/ARCHITECTURE.md)。

---

## 工程目录约定

```
<工程目录>/                       = project.json 所在目录（≠ 仓库根）
├── project.json                   fps / recap.voice / recap.tts_rate / recap.hook_clip
├── shotlist.csv                   镜头表
├── asr_ja.json                    台词源（paras: t0/t1/text）
├── json/                          story_plan / cutplan / align / narration /
│                                  shots_desc / scene_desc / tts-result / qc_*.json
├── tts/                           每句 000.mp3（按句号命名，注意缓存失效）
├── out/                           01-video-no-ad.mp4 / 02-video-rapid-silent.mp4 /
│                                  mix-rapid.wav / 03-rapid-narration.mp4
├── verify/                        试听样本、ASR 校验 wav
└── .qcframes/session/             质检宫格图与会话内判定
```

完整字段表见 [架构说明 §5](docs/ARCHITECTURE.md)，路径常量见 `aicutplan/paths.py`。

---

## 测试与开发

```bash
PY=D:/Python312/python.exe
$PY -m unittest discover -s tests -v      # 标准库，零第三方依赖
$PY -m pytest                            # 可选运行器
$PY -m aicutplan check --project <pj>     # 真实工程体检
$PY -m py_compile *.py aicutplan/*.py     # 全量语法
```

测试全部用 `tempfile` 造数据，**不碰真实工程、不调 ffmpeg / 网络 / 模型**。
覆盖：字幕解析与 ASS 合并、钩子连续段选择与权重、证据打分压缩、窗口/对齐/TTS
一致性契约、内容指纹失效、原子写与两类备份、工程路径、CLI 门面。

新增脚本请遵守：

1. 无副作用、可复用的纯逻辑先进 `aicutplan/`，根目录脚本只留 CLI 编排；
2. 文件顶部补 `# [层] 职责 / 调用者 / 产物`，并更新 `docs/ARCHITECTURE.md`；
3. **不要用「文件存在就跳过」当缓存**，走 `aicutplan.cache` 的内容指纹
   （历史事故清单见 [架构说明 §6](docs/ARCHITECTURE.md)）；
4. 落盘用 `aicutplan.io.dump_json`（同目录原子写），备份区分 `backup_copy` /
   `backup_rename`；
5. 跑完自动校验产物与输入一致，别把发现问题的责任留给人看片。

---

## 解释器口径与安全边界

| 解释器 | 有 | 用途 |
|---|---|---|
| `D:/Python312/python.exe` | torch / numpy / PIL | 规划、渲染、web 台、单元测试 |
| `~/.workbuddy/binaries/python/envs/default/Scripts/python.exe` | edge_tts / faster_whisper | r5_tts、elastic_fit、asr_local |

`cut.py / r5_tts.py / r6_mix.py / r7_mux.py` 是**工程目录脚本**（配方副本在
`~/.workbuddy/skills/anime-recap/scripts/`），不在本仓库；关键修复（如 r6 采样率）
需同步两处。ffmpeg 不在 PATH 时设 `AICUTPLAN_FFMPEG` 或 `NARR_FFMPEG`。

- 除显式动作外不修改工程目录：写回 `cutplan` / `script.json`、镜头台改
  `shotlist.csv`、台词台改 `asr_*.json`、新建工程写 `project.json`；
- 所有写回前自动备份，可回滚；
- `DEEPSEEK_API_KEY` 只从环境变量读，不落盘、不入日志；会话内模式完全不需要它；
- 剪映草稿默认只写到 `exports/jianying/`，写入剪映目录需单独确认且不覆盖同名。

## 已知限制

- 说话人分离、原声能量/语速特征、数字事实 OCR 交叉校验尚未实现
  （「十三万点 → 一万三千点」靠人工发现）。
- `story_parse.py` 段缓存 `seg_XX.json` 仍按文件存在判定；重跑剧情解析时若缓存命中，
  P0-2 的证据回喂不会进模型，需 `--force`。见 [架构说明 §9](docs/ARCHITECTURE.md)。
- 剪映草稿是单向的：剪映保存后草稿加密，脚本读不回来。
- 预览渲染是独立转码，画质上限等于片源；最终成片由 `r7_mux.py` 以 `-c:v copy` 输出。
- `main` 分支停在检索腿版（旧链），`cut-first` 是当前主线，尚未合回。
- 图像字幕（蓝光 PGS）暂不支持，自动回退 ASR。
