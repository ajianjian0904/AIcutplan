/**
 * 把插件自带的技能同步到用户技能根（真实目录），保证「母版只有一份、注册处永远最新」。
 *
 * 为什么不用目录联接：dsh-skill-filesystem 的 watcher 在联接目标被替换后不再重新解析，
 * 技能会从目录里消失（实测）。改为「插件激活时写一份」：
 *   - 母版：`plugins/dsh-aicut/skill/SKILL.md`（随插件走、进 git）
 *   - 注册处：`~/.dsh/skills/aicut-workflow/SKILL.md`（真实文件，首次/变更时重写）
 *
 * 写入是幂等的：内容一致就不动文件，避免每次激活都触发 catalog 失效。
 *
 * @module @local/dsh-aicut/skill
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
export const SKILL_MASTER = join(HERE, '..', 'skill', 'SKILL.md')
export const SKILL_NAME = 'aicut-workflow'

/** 技能注册路径：遵循 DSH 用户技能根（$DSH_HOME/skills 或 ~/.dsh/skills）。 */
export function skillTarget(dshHome) {
  const root = dshHome || process.env.DSH_HOME || join(homedir(), '.dsh')
  return join(root, 'skills', SKILL_NAME, 'SKILL.md')
}

/**
 * 把母版 skill 同步到注册路径。返回 {target, written, reason}；永不抛错。
 */
export function syncSkill(dshHome) {
  const target = skillTarget(dshHome)
  try {
    if (!existsSync(SKILL_MASTER)) return { target, written: false, reason: 'master missing' }
    const master = readFileSync(SKILL_MASTER, 'utf8')
    if (existsSync(target) && readFileSync(target, 'utf8') === master) {
      return { target, written: false, reason: 'up to date' }
    }
    mkdirSync(dirname(target), { recursive: true })
    writeFileSync(target, master, 'utf8')
    return { target, written: true, reason: 'installed' }
  } catch (error) {
    return { target, written: false, reason: 'failed: ' + String(error?.message ?? error) }
  }
}
