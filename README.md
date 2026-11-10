# AIcutplan · 漫剪校对台

> 动漫剪辑，目前针对 AI 自动剪辑无法达到人为要求的情况，加入人机结合的剪辑方法。

本地运行的动漫速讲（漫剪）校对工作台：把「旁白 / 画面 / 人物台词」统一到同一条时间轴上逐句确认，最后写回 `cutplan.json`，可选导出为剪映草稿继续精修。

服务端只用 Python 标准库 `http.server`，前端无构建，三页共用一套静态文件；除 ffmpeg 外没有硬依赖。

## 八个页面

| 页面 | 路径 | 做什么 |
|---|---|---|
| 制作流水线 | `/flow` | **一集从 0 到成片的总控**：17 步按产物自动判定状态，可直接运行洗片 / 切分 / ASR / 译中 / 剧情解析 / 落稿 / TTS / 对齐 / 切片 / 重排 / 混音 / 合流 / 字幕烧入，日志实时回传；也是新建工程的入口 |
| 镜头台 | `/shots` | **镜头表审阅**：逐镜缩略图、时长分布；合并 / 删除 / 切分 / 挪边界 / 标原声；有效段写回 `project.json` |
| 台词台 | `/asr` | **原文与中文译文对照**：逐段可改，只改文本不动时间戳；就地触发分批译中 |
| 剧情台 | `/story` | **AI 解析剧情 + 人工审核**：模型读「字幕 + 抽帧」出事件表，再合成 12–15 个剧情块；可改块名 / 性质 / 边界 / 成片秒数，实时算字数预算；一键落 `script` 骨架与 `cutplan` 初版 |
| 逐句校对台 | `/review` | **逐句三轨对齐**：旁白（成片秒）、画面（每刀缩略图）、人物台词（源片秒投影到成片轴）。播放时三者同步高亮，可给每句定【首镜, 尾镜】、逐句确认 |
| 锚点校对台 | `/` | **按剧情块整块平移**源片取段，A/B 抽帧判有没有跳场，确认后写回 |
| 旁白工作台 | `/narration` | 与 DeepSeek 协作改稿：模板 / 变量注入 / 草稿快照 / 定稿 / 写回 `script.json` / 生成 7 步重跑脚本 |
| 交付台 | `/deliver` | **成片播放与验收**（Range 拖动）、临时预览渲染、导出剪映草稿（整片 / 逐刀） |

## 制作流程（流水线 17 步）

```
素材  洗片 → 镜头切分
台词  语音识别 → 台词译中
文案  剧情解析（AI，机器出骨架 + 人审核）→ 剧情文案（人工，旁白工作台）→ 旁白落稿
旁白  配音 TTS
画面  旁白对齐 → 按刀切片 → 时间线重排
合成  混音 → 合流成片 → 字幕（烧入）
校对  三轨校对（人工，逐句台）→ 写回 cutplan（人工，锚点台）
交付  预览 / 导出剪映
```

每一步只声明三件事：**产物**（决定状态灯）、**依赖**（前置未完成会提示）、**命令**（用当前解释器调工程或 skill 里的脚本）。产物存在即亮绿，`relayout` 这类步骤还会比较产物与上游的 mtime，比上游旧就标「需重排」。

同一时刻只允许跑一个后台任务（ffmpeg 与 TTS 抢磁盘，并发只会更慢）；日志落 `cache/jobs/*.log`，界面按行增量拉取。

## 剧情解析怎么工作（`/story`）

这是「机器出骨架、人只审核」的那一环，三步：

1. **分段**：按 `recap.valid` 把整集切成 200s 的窗口（8 段/集），每段带该段中文字幕（源片秒）与 12 张均匀抽帧。
2. **逐段解析**：一次请求同时给「带时间戳的字幕」+「每张图标注源片秒」，要求输出 8–14 个连续事件：谁、做了什么、性质（钩子/铺垫/冲突/反转/爽点/收尾）、建议保留几秒。段结果落 `cache/story/<hash>/seg_XX.json`，断点续传。
3. **合成块表**：全部事件 + 目标成片时长 + 六段式模板再调一次，输出 12–15 块（源区间 + 成片秒数 + 一句话概要），OP/标题卡/预告标 `收录`、`target=0`，不进块表。

产物：`json/story_plan.json`（分段 / 事件 / 块表 / 预算）。审核在页面上改，可改块名、性质、源区间、成片秒数，底部实时算「Σ成片秒 vs 目标」与字数预算。确认后：

- **写 script 骨架** → `json/script.json`（块名 + 时间锚点 + 空文案），旁白台按块生成文案
- **生成 cutplan 初版** → `json/cutplan.json`（每块按 `target/1.32` 铺刀，块内均匀取镜，`new_start` 累计） + `json/shots-cache.json`

实测（EP05，1420s / 8 段）：每段 10–35s、8–10k tokens，全片约 4 分钟、约 60k tokens。模型会把 OP/标题卡当剧情，所以这一步**必须人审**——这正是它留在页面上的原因。

## 全自动跑通（跳过人工审核）

```bash
# 一次跑完：洗片 → 切分 → ASR → 译中 → 剧情解析 → 自动写稿 → TTS → 对齐 → 切片 → 重排 → 混音 → 合流 → 字幕
python prepare_media.py  --project <pj>                      # 10bit/HEVC/带 ASS 自动转 H.264 8bit
python shots_scan.py     --project <pj> --scene 0.30
python asr_local.py      --project <pj> --lang ja --size small
python asr_pipeline.py   --project <pj> --batch 24           # 台词译中
python story_parse.py    --project <pj> --segment 200 --frames 12
python story_parse.py    --project <pj> --write-script --write-cutplan
python auto_script.py    --project <pj>                      # AI 整稿 → 补缺块 → 定稿 → 写回
python auto_script.py    --project <pj> --fit                # 超预算的块压缩
python auto_script.py    --project <pj> --expand             # 偏短的块扩写
python align_chain.py    --project <pj>                      # 重建初版 cutplan + n3 对齐
python cut.py            --project <pj> --force
python relayout_narr.py  (cwd=工程目录)
python r6_mix.py         --project <pj>
python r7_mux.py         --project <pj>
python make_srt.py       --project <pj> --burn               # 出 srt 并烧进成片
```

实测一集（`[NanakoRaws] Tsuihou ... - 01`，23.8 分钟 1080p HEVC 10bit，1429.9s）：

| 阶段 | 耗时 | 产物 |
|---|---|---|
| 洗片（10bit → H.264 8bit 1080p） | 8 分 | 391MB mp4 + 260MB wav |
| 镜头切分（scene 0.30） | 58s | 290 镜，中位 3.25s |
| ASR（faster-whisper small / CUDA float32） | 151s | 404 段 |
| 台词译中（17 批） | 147s | 404/404，35k tokens |
| 剧情解析（8 段 × 12 帧） | 188s | 100 事件 → 14 正片块 + 1 收录，48k tokens |
| 自动写稿 + 压缩 + 补足 | 6 分 | 1325 字 / 127 句 |
| TTS（edge-tts） | 13–161s | 127 句 / 253.9s |
| 对齐 + 切片 + 重排 + 混音 + 合流 | 3 分 | 193 刀 / 254.5s |
| 字幕烧入 | 79s | 127 条，92MB |

成片验收（`r7_mux` 自检）：时长 254.5s ✓、切点密度 45.5/分 ✓、段数 193 ✓、字/刀 6.21 ✓、
旁白覆盖 93.8% ✓、响度 −13.4 LUFS ✓、LRA 2.7 ✓；旁白字数 1198（目标 1350+）、句数 127（目标 130+）双双略低。

## 核心编辑模型

这套工具刻意只做一件事，并且把边界写死：

- **成片时长由旁白 TTS 实测时长决定**，锁死。画面只允许改「从源片哪个时刻开始取」。
- 所有写回**只改 `cutplan.json` 的 `src_start / src_end`**，`dur / new_start / new_end / sent_ids` 一个都不碰，写前自动备份。
- 两级编辑，写回时**句级优先于块级**：
  - 块级：整块平移 Δ，块内所有刀一起移动，长度不变
  - 句级：给一句定【首镜, 尾镜】，句内 2–3 刀按顺序铺满，只改这几刀，**不挪任何其他刀位**（因此天然避开级联漂移）
- 不开放：逐刀拖拽、改刀数/刀长、改块长度、AI 自动重排、匹配度打分。理由是四集实测结论——一句旁白平均跨 1.73 刀、每刀固定 1.32s，逐句一镜会让单镜变 1.96s（比对标慢 48%）；且 48% 是演绎刀、52% 是情境刀，后者本就不该跟旁白语义匹配，机器换图的实测结果是「换对 18%、改坏 64%」。

## 工程目录约定

```
<工程目录>/
  project.json               # 名称、fps、语速等
  shotlist.csv               # 镜头表：idx,start,end,dur,motion,bright,ost
  asr_ja.json                # 语音识别结果（paras: t0/t1/text），语言后缀可为 en/ko/zh
  json/cutplan.json          # 245 个窗口：src_start/src_end/dur/new_start/new_end/sent_ids
  json/align.json            # beat_win（刀→块分组）、sent_span、sent_beat
  json/narration.json        # items：i/text/beat/t0/dur/chars
  json/script.json           # beats[]：剧情块文案
  out/01-video-no-ad.mp4     # cut.py 的真实输入，抽帧与切段的时间基准
  out/mix-rapid.wav          # 最终混音（预览用）
  out/中文旁白-recap.srt      # 成片字幕（预览烧录用）
```

## 快速开始

```bash
# 1) 依赖
python 3.12
ffmpeg / ffprobe 在 PATH 上

# 2) 指定工程（二选一）
set RECAP_PROJECT=<你的工程目录>      # Windows
export RECAP_PROJECT=<你的工程目录>   # bash

# 3) 启动
python server.py        # 或双击 start.bat
# → http://127.0.0.1:8765
```

界面右上角也可以直接填工程目录点「载入工程」，选择会记进 `studio_state.json`，下次启动自动载入。

### 可选依赖

| 能力 | 需要 | 说明 |
|---|---|---|
| 旁白 AI 协作、台词译中 | 环境变量 `DEEPSEEK_API_KEY` | Key 只在内存读取，不写文件、不进日志；缺失时可用「手工粘贴」模式 |
| 导出剪映草稿 | `pip install pyJianYingDraft==0.3.1` | 需装在某个解释器里，用 `JY_PYTHON` 指定；未指定时自动探测当前解释器与托管 venv |

## 目录结构

```
server.py            HTTP 服务：工程载入、会话状态、写回、抽帧、预览、路由
pipeline.py          流水线：15 步定义、产物扫描、后台任务执行器、新建工程、镜头表/台词表读写
prepare_media.py     洗片：流复制 + 抽音 + ffprobe（流水线第 1 步）
shots_scan.py        镜头切分：场景检测 + 按镜音频能量（第 2 步）
asr_local.py         本地 faster-whisper 转写（第 3 步）
review.py            逐句三轨数据（台词投影到成片轴）、候选首镜、确认进度
sentence_studio.py   句↔刀映射、句级锚点计算、两层合并、台词查询
preview_render.py    临时渲染：按刀切源片 + 拼接 + 混音 + 烧字幕 + 首帧海报
narration.py         旁白模板/草稿/快照/定稿、DeepSeek 客户端、字数预算与只读校验
asr_pipeline.py      多语 ASR 检测、按批翻译为中文、断点续传（含 CLI，供流水线调用）
auto_script.py       自动写稿：整稿生成 → 补缺块 → 按预算压缩/扩写 → 定稿写回
align_chain.py       对齐前置：重建初版 cutplan（回到 script 轴）再跑 n3
make_srt.py          narration.json → srt，可选烧进成片
jianying_export.py   剪映草稿计划/自检/安装
jianying_worker.py   在装了 pyJianYingDraft 的解释器里生成明文草稿
web/                 七页前端 + 共用底座 base.css
templates.json       旁白提示词模板
tts_calibration.json 本机 TTS 实测标定（有效语速、固定静音）
```

## 阶段门禁

- 旁白有**未定稿草稿**时，锚点台与逐句台的写回按钮被锁（后端 `/api/save` 直接返回 409），避免白做。
- 定稿后继续改稿会自动撤销定稿并重新上锁。
- 写回之后必须重跑 7 步链路（`build_narr → r5_tts → n3_align → cut → relayout_narr → r6_mix → r7_mux`），否则新的 `cutplan.json` 会覆盖刚做的平移。界面只展示/复制命令，不代为执行。

## 安全边界

- 除以下**显式动作**外，不修改工程目录任何文件：写回 `cutplan`、写回 `script.json`、镜头台改 `shotlist.csv`、台词台改 `asr_*.json`、新建工程写 `project.json`。草稿、快照、预览、翻译缓存、任务日志全部落在本仓库目录下。
- 所有写回前自动备份（`*.bak.<时间戳>`），可回滚。
- 流水线只**调用**工程与 skill 里的既有脚本，不改脚本行为；人工步骤（写文案、逐句校对、写回）不会被代跑。
- `DEEPSEEK_API_KEY` 只从环境变量读，不落盘、不入日志、不进异常信息。
- 剪映草稿默认只写到 `exports/jianying/`；写入剪映草稿目录需单独确认，且不覆盖同名、不改剪映索引。

## 已知限制

- 逐句确认、块级平移、句级锚点属于**会话状态**（持久化到 `studio_state.json`），重跑 7 步链路会失效。
- 预览渲染是独立转码，画质上限等于片源；最终成片由 `r7_mux.py` 以 `-c:v copy` 输出，不重编码。
- 剪映草稿是**单向**的：剪映保存后草稿加密，脚本读不回来；重复导出会覆盖剪映里的人工修改。
- 镜头表的 `motion / bright` 两列在自动切分时不计算（填 0），只有历史工程自带；校对只用 `ost`。
- 流水线的「剧情文案 / 三轨校对 / 写回」是人工步骤，页面只给跳转，不代跑；要全自动用 `auto_script.py`（质量会低于人工审）。
- **对齐前必须重建初版 cutplan**（`align_chain.py` 已内置）。n3 把 cutplan 当旧轴查块源区间，跑完又会把 cutplan 覆写成新轴；二次对齐若不重建，anchor 与新轴错位（实测偏差 47.9s），刀数少算、多出的旁白在 relayout 被丢掉（127 句掉到 103 句，成片 241s 变 208s）。
- AI 自动写稿普遍偏保守：整稿 2196 字（预算 1503）→ 压缩到 1268 → 补足到 1325，仍低于 1350 的下限。要达标需要人工加料或调高 `--expand` 阈值。
- 说话人分离、剧情解析尚未实现。
