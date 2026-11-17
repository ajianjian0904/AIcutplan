# 漫剪流水线（dsh-aicut）

把 [AICutplan](file:///E:/workbuddy/漫剪软件开发) 的确定性 CLI 接成 DeepSeek Harness 的宿主工具。
插件本身**不做创作判断**：写稿、读宫格图判画、决定改哪句，都留在会话里由模型完成；
插件负责路径校验、选对解释器、超时、日志截断与失败指引。

## 工具

| 工具 | 对应命令 | 作用 |
|---|---|---|
| `aicut_env` | `version` + 文件探测 | 自检解释器 / 仓库根 / 作品库 / ffmpeg |
| `aicut_projects` | `lib list --json` | 列动漫、集、最新归档、告警 |
| `aicut_status` | `guard --json` + `runs latest` | 四阶段门禁、归档指针、产物时间 |
| `aicut_check` | `check` | 契约体检：lint / 时间轴 / TTS 一致性；可选 `--number-frames` / `--ocr` / `--ocr-dense` 核对画面数字 |
| `aicut_refit` | `narrate → r5 --only → elastic_fit → stamp → r6 → r7` | **局部重跑闭环**：改几句旁白只重跑这几句，画面不动 |
| `aicut_upgrade_sents` | `-m aicutplan.legacy.upgrade_sents` | 历史稿补 `src`（src-v2），先只读校验再落盘 |
| `aicut_subs` | `subs` | 内嵌字幕优先，跳过 ASR |
| `aicut_plan` | `plan` | 画面先定，重建 `json/cutplan.json` |
| `aicut_narrate` | `narrate --export` / `--sentences` | 导出写作表 / 落盘句子稿（写盘前校验） |
| `aicut_qc` | `qc --plan/--merge/--probe` | 逐句质检批次、合并判定、多帧复核 |
| `aicut_qc_compare` | `-m aicutplan.chain.qc_compare` | 两套模型判定的交叉验证：一致率、分歧、probe 并集 |
| `aicut_render` | `run` | cut-first 主链出片（可跳过阶段、预演、质检闭环） |
| `aicut_batch` | `batch` | 按导演稿批量出片 / 断点续跑 |
| `aicut_runs` | `runs snapshot/list/latest/verify` | 版本归档与指纹校验 |
| `aicut_clean` | `clean` | 冗余盘点与可恢复回收 |

## 配置

写在 profile 的 `cordis.patch.yml` 里覆盖本插件行的 `config`（整段替换，改动时把要保留的字段一起抄过来）。

| 字段 | 默认 | 说明 |
|---|---|---|
| `repoRoot` | `E:\workbuddy\漫剪软件开发` | aicutplan 包所在仓库根（不是工程目录） |
| `python` | `D:\Python312\python.exe` | 规划 / 渲染 / 体检解释器 |
| `ttsPython` | `~/.workbuddy/.../python.exe` | 装了 edge-tts / faster-whisper 的解释器 |
| `libraryRoot` | `E:\workbuddy\漫剪作品库` | 作品库根，覆盖 `AICUTPLAN_LIBRARY` |
| `ffmpeg` | 空 = 用 PATH | ffmpeg 绝对路径，覆盖 `AICUTPLAN_FFMPEG` |
| `timeoutMs` | `900000` | 单次调用总预算；渲染另有 45 分钟、批量 60 分钟的分档 |
| `graceMs` | `3000` | 终止信号到强杀的宽限 |
| `maxOutputBytes` | `1048576` | 每条流保留的尾部字节数 |

环境变量 `AICUTPLAN_REPO` / `AICUTPLAN_PY` / `AICUTPLAN_TTS_PY` / `AICUTPLAN_LIBRARY` / `AICUTPLAN_FFMPEG` 可覆盖同名配置。

## 使用

1. `aicut_env` 确认环境；`aicut_projects` 找到工程目录。
2. `aicut_status` 看门禁与最新归档，决定从哪一步开始。
3. 需要重规划：`aicut_plan`；已定型就跳过。
4. `aicut_narrate export=true` 拿 `window_table.md`，逐窗对着画面写句子稿。
5. `aicut_narrate sentences=<稿>` 落盘（契约不过会直接拒绝，退出码 5）。
6. `aicut_render` 出片；`aicut_qc plan` 生成宫格图，`read_image` 逐格判定后 `aicut_qc merge`。
7. **只改旁白时不要重跑整链**：改好稿后 `aicut_refit`（自动比对改了哪几句、只重合成它们、盖章、混音、合片），
   再 `aicut_qc plan → probe → merge` 复核。
8. `aicut_runs snapshot` 归档，`aicut_clean` 回收冗余。

完整规则（钩子定义、`src` 绑定、字数上限、单位换算）随包提供在 `skill/SKILL.md`，安装后把该目录放进技能根即可被 `skill` 工具按需加载。

## 已知限制

- **Cordis 服务依赖**：`inject: ['tools', 'subprocess']`。Cordis 里没注入就访问服务会直接抛
  「cannot get property "subprocess" without inject」，所以 `subprocess` 必须声明；
  DSH base 自带该服务。`lib/run.js` 仍保留退化到 `node:child_process` 的分支，只用于不启动
  DSH 的冒烟测试。
- 长任务走同步等待，没有增量进度；用 `dry_run` 先看命令，超大工程建议在终端里跑。
- 不接管本地校对台（`python -m aicutplan serve`，127.0.0.1:8765），它仍作为独立进程使用。
- 句子稿内容对不对（画面与文案是否匹配）插件无法判断，只能由模型读图后写。
- 工程目录脚本 `cut.py / r5_tts.py / r6_mix.py / r7_mux.py` 在工程目录里，不由本插件提供。
