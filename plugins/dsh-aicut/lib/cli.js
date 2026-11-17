/**
 * aicutplan 调用层：路径校验、argv 组装、输出归一化。
 *
 * 所有命令都是 `python -X utf8 -m aicutplan <子命令> ...`，参数用数组传递，
 * 不做字符串拼接。解释器、仓库根、TTS 解释器全部来自插件 config。
 *
 * @module @local/dsh-aicut/cli
 */
import { existsSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { TIMEOUTS } from './config.js'

/** 把「工程目录」或「project.json 路径」统一成一个存在的 project.json 绝对路径。 */
export function projectFile(input) {
  if (typeof input !== 'string' || input.trim() === '') {
    throw new Error('project 必填：给工程目录或 project.json 的绝对路径（例如 E:\\workbuddy\\漫剪作品库\\被驱逐的加强师\\EP01）。')
  }
  const target = resolve(input.trim())
  if (!existsSync(target)) {
    throw new Error('工程路径不存在：' + target + '。请确认作品库根目录与集目录名（可用 aicut_projects 列出）。')
  }
  if (statSync(target).isDirectory()) {
    const file = join(target, 'project.json')
    if (!existsSync(file)) {
      throw new Error('该目录下没有 project.json：' + target + '。工程目录 = project.json 所在目录，与仓库根无关。')
    }
    return file
  }
  if (!target.toLowerCase().endsWith('project.json')) {
    throw new Error('project 只接受工程目录或 project.json：' + target)
  }
  return target
}

/** 工程目录（project.json 的父目录）。 */
export function projectDir(projectPath) {
  return resolve(projectPath, '..')
}

/** 组装一次 aicutplan 调用的 argv。``python`` 可覆盖解释器（TTS 步骤要用 ttsPython）。 */
export function buildArgv(cfg, args, python) {
  const argv = [python || cfg.python, '-X', 'utf8', '-m', 'aicutplan']
  for (const arg of args) {
    if (arg === undefined || arg === null || arg === '') continue
    argv.push(String(arg))
  }
  return argv
}

/** 只保留尾部若干行，避免把整段渲染日志塞回模型上下文。 */
export function tailLines(text, limit = 40) {
  const lines = String(text ?? '').replace(/\r\n/g, '\n').trimEnd().split('\n')
  if (lines.length <= limit) return lines.join('\n')
  return '…（前 ' + (lines.length - limit) + ' 行省略）\n' + lines.slice(-limit).join('\n')
}

/** 从输出里抓第一个 JSON 值（aicutplan 的 --json 前后都可能混日志，且可能是数组）。 */
export function firstJson(text) {
  const source = String(text ?? '')
  const objectStart = source.indexOf('{')
  const arrayStart = source.indexOf('[')
  const start = [objectStart, arrayStart].filter((index) => index >= 0).sort((a, b) => a - b)[0]
  if (start === undefined) return undefined
  const closer = source[start] === '[' ? ']' : '}'
  for (let end = source.lastIndexOf(closer); end > start; end = source.lastIndexOf(closer, end - 1)) {
    try {
      return JSON.parse(source.slice(start, end + 1))
    } catch { /* 继续向前收窄 */ }
  }
  return undefined
}

/**
 * 执行一次 aicutplan 命令并归一化结果。
 * @returns {Promise<{ exitCode: number, text: string, stdout: string, stderr: string, durationMs: number, timedOut: boolean }>}
 */
export async function runAicut(cfg, runner, args, options = {}) {
  const argv = buildArgv(cfg, args, options.python)
  const result = await runner.run(argv, {
    timeoutMs: options.timeoutMs,
    signal: options.signal,
    cwd: options.cwd ?? cfg.repoRoot,
  })
  const text = [result.stdout, result.stderr].filter((part) => part && part.trim() !== '').join('\n')
  return { ...result, argv, text, display: argv.join(' ') }
}

/** 失败时的可行动提示：把常见退出码翻译成「下一步做什么」。 */
export function exitHint(result) {
  if (result.timedOut) {
    return '超时：这一步通常要几分钟到几十分钟。可先用 dry_run 看命令、缩小批次，或提高 config.timeoutMs 后重试。'
  }
  if (result.exitCode === 2) return '参数/路径问题（退出码 2）：先跑 aicut_env 与 aicut_status 核对工程与解释器。'
  if (result.exitCode === 4) return '渲染门禁阻断（退出码 4）：产物与当前输入不一致，先补跑上游阶段；确实要出片再加 allowStale。'
  if (result.exitCode === 5) return '句子稿契约不过（退出码 5）：每句 src 必须等于绑定窗口的 src_start/src_end，且句内容要对着画面写。'
  if (result.exitCode === 6) return '前置完整性校验失败（退出码 6）：缺产物或 hook_clip 未定义，先补跑被跳过的阶段。'
  return undefined
}

/** 工程产物概览：只读文件元数据，不解析业务 JSON。 */
export function artifactSummary(projectPath) {
  const dir = projectDir(projectPath)
  const candidates = [
    'out/03-rapid-narration.mp4',
    'out/02-video-rapid-silent.mp4',
    'out/mix-rapid.wav',
    'json/narration.json',
    'json/cutplan.json',
    'json/qc_visual.json',
  ]
  const rows = []
  for (const rel of candidates) {
    const file = join(dir, rel)
    if (!existsSync(file)) { rows.push({ file: rel, exists: false }); continue }
    const stat = statSync(file)
    rows.push({ file: rel, exists: true, bytes: stat.size, mtime: stat.mtime.toISOString() })
  }
  return rows
}

export { TIMEOUTS }
