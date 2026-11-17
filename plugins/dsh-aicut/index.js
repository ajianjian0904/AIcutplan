/**
 * dsh-aicut —— AIcutplan 漫剪解说流水线的 Host 工具插件。
 *
 * 只做「确定性机械动作」：把 aicutplan 已有的 CLI 包成宿主工具，负责路径校验、
 * 解释器选择、超时、输出截断与失败指引。写稿、读图判画这类判断留给会话里的模型，
 * 这也正是 aicutplan 的「会话内导演」设计。
 *
 * 工具注册形状与 dsh-ffmpeg 一致：ctx.tools.register(definition) 返回 disposer。
 *
 * @module @local/dsh-aicut
 */
import { resolveConfig } from './lib/config.js'
import { createRunner } from './lib/run.js'
import { syncSkill } from './lib/skill.js'
import { buildTools } from './lib/tools.js'

export const name = 'aicut'

/**
 * Cordis 服务注入：`tools` 用来注册工具，`subprocess` 用来跑子进程。
 *
 * 注意：Cordis 里**没注入就访问** `ctx.subprocess` 会直接抛
 * 「cannot get property "subprocess" without inject」，所以必须写进 inject。
 * `lib/run.js` 仍保留退化到 node:child_process 的分支，供不启动 DSH 的冒烟测试使用。
 */
export const inject = ['tools', 'subprocess']

/**
 * @param {any} ctx 宿主上下文（Cordis）。
 * @param {any} config 来自 cordis.patch.yml 的行 config。
 */
export function apply(ctx, config) {
  const cfg = resolveConfig(config)
  // 技能随插件自动注册：把 skill/SKILL.md 同步到用户技能根（幂等，失败不影响工具激活）
  const skill = syncSkill(cfg.dshHome)
  if (skill.written) console.log('[aicut] skill 已同步 → ' + skill.target)
  const runner = createRunner(ctx, cfg)
  const disposers = buildTools(cfg, runner).map((definition) => ctx.tools.register(definition))
  const off = typeof ctx.on === 'function'
    ? ctx.on('dispose', () => { for (const dispose of disposers) dispose() })
    : undefined
  return off
}
