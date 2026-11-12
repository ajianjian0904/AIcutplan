# config/ · 仓库级配置

> 这里放**仓库级**配置。**工程级**配置是各工程目录下的 `project.json`（源片路径、音色、
> 语速、字幕偏好等），跟着工程走，不在这里。

## 文件清单

| 文件 | 内容 | 谁读写 | 入库 |
|---|---|---|---|
| `templates.json` | LLM 提示词模板库（outline / full / beat / polish / lint 5 套） | `narration.templates()` / `save_template()`；web 台「旁白台」可编辑 | ✅ |
| `tts_calibration.json` | TTS 语速与固定静音校准（effective_cps / fixed_silence / instant_cps） | `narration.calibration()` 读、`recalibrate()` 写 | ✅ |
| `llm_config.example.json` | 非密钥 LLM 配置样例（base_url / model / temperature） | 复制为 `llm_config.json` 后由 `narration.llm_config()` 读 | ✅ |
| `llm_config.json` | 本机实际 LLM 配置 | `narration.llm_config()` / `save_llm_config()` | ❌ 已忽略 |
| `style.md`（可选） | 文风规则覆盖（默认读 skill 的 references/style.md） | `narration.style_rules()` | 可选 |

**密钥永远只走环境变量** `DEEPSEEK_API_KEY`，不写任何配置文件、不入日志。

## 路径解析顺序（`aicutplan.config`）

1. 环境变量 `AICUTPLAN_CONFIG_DIR` → 整个配置目录改到该路径（测试 / 多环境 / CI）；
2. `config/<name>` 存在 → 用它；
3. 旧布局 `<仓库根>/<name>` 存在 → 只读回退（迁移期兼容）；
4. 都不存在 → 返回 `config/<name>`，后续写入落到这里（自动建目录、原子写）。

其它相关环境变量：

| 变量 | 作用 |
|---|---|
| `AICUTPLAN_CONFIG_DIR` | 覆盖配置目录 |
| `AICUTPLAN_STYLE_MD` | 覆盖文风参考文件路径 |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | 覆盖 LLM 地址 / 模型（优先于 llm_config.json） |
| `DEEPSEEK_API_KEY` | **唯一**密钥来源 |

## 迁移说明

历史上的 `templates.json` / `tts_calibration.json` 直放在仓库根，现已移入本目录。
旧文件若仍存在会被自动回退读取（不会报错），但建议删除以免误以为是生效配置；
下次写入（保存模板 / 重新标定）会自动落到 `config/`。

体检时可用：

```bash
python -m aicutplan check --project <pj>     # 会打印每个配置文件的实际解析路径与状态
```
