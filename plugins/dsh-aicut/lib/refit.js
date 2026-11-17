/**
 * 局部重跑闭环：改几句旁白 → 只重合成这几句 → 弹性适配 → 混音 → 合片（可选归档）。
 *
 * 顺序与 `aicutplan/chain/qc_loop.py` 的 steps 一致（narrate → r5 --only → elastic_fit
 * → r6 → r7），并补上 qc_loop 里那步 `freshness.stamp(proj, "tts")`——没有它，r6 的渲染
 * 门禁会以「配音不是当前输入生成」为由阻断（退出码 4）。
 *
 * 为什么要这个组合工具：改一句话要动 6 个命令、两种解释器、一次盖章；任何一处写错都
 * 会得到「文本新词 / 成片旧音频」。这里把顺序、解释器、`--only` 列表、盖章全固化。
 *
 * @module @local/dsh-aicut/refit
 */
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { projectDir, runAicut } from './cli.js'

const T = {
  narrate: 600000,
  tts: 1200000,
  fit: 600000,
  mix: 1200000,
  mux: 2400000,
  snapshot: 300000,
}

function loadSents(path) {
  const data = JSON.parse(readFileSync(path, 'utf8'))
  const items = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : [])
  return items.map((s, k) => ({ i: Number.isInteger(s.i) ? Number(s.i) : k, text: String(s.text ?? '') }))
}

/** 与 narration.json 比对，返回改了文本的句子号；narration 不存在时返回 undefined。 */
export function changedIds(project, sents) {
  const file = join(projectDir(project), 'json', 'narration.json')
  if (!existsSync(file)) return undefined
  const old = loadSents(file)
  const byI = new Map(old.map((s) => [s.i, s.text]))
  return sents.filter((s) => byI.get(s.i) !== s.text).map((s) => s.i)
}

/**
 * @param {import('./config.js').AicutConfig} cfg
 * @param {ReturnType<import('./run.js').createRunner>} runner
 */
export function createRefit(cfg, runner) {
  async function step(name, argv, options = {}) {
    const started = Date.now()
    const result = await runner.run(argv, {
      timeoutMs: options.timeoutMs,
      cwd: options.cwd,
      signal: options.signal,
    })
    const text = [result.stdout, result.stderr].filter((x) => x && x.trim() !== '').join('\n')
    const row = { name, exitCode: result.exitCode, ms: Date.now() - started, text: text.trim() }
    if (result.exitCode !== 0 || result.timedOut) {
      const err = new Error('步骤失败：' + name + '（退出码 ' + String(result.exitCode) + '）')
      err.row = row
      throw err
    }
    return row
  }

  /**
   * @param {{ project: string, sentences: string, only?: number[], snapshot?: boolean,
   *   label?: string, dryRun?: boolean, signal?: AbortSignal }} request
   */
  return async function refit(request) {
    const project = request.project
    const dir = projectDir(project)
    const sents = loadSents(request.sentences)
    const detected = changedIds(project, sents)
    const changed = Array.isArray(request.only) && request.only.length
      ? request.only.map(Number)
      : detected

    if (changed !== undefined && changed.length === 0) {
      return { ok: true, changed: [], steps: [], text:
        '新稿与当前 narration 文本一致，没有可重跑的句子（要强制重合成请显式给 only）。' }
    }
    if (!changed || changed.length === 0) {
      return { ok: false, changed: [], steps: [], text:
        '找不到 narration.json 无法自动比对，请用 only 指定要重合成的句子号（如 [18,25]）。' }
    }

    const only = changed.join(',')
    const projArg = project
    const plan = [
      ['narrate', [cfg.python, '-X', 'utf8', '-m', 'aicutplan', 'narrate',
        '--project', projArg, '--sentences', request.sentences], { timeoutMs: T.narrate }],
      ['r5_tts', [cfg.ttsPython, join(dir, 'r5_tts.py'), '--project', projArg, '--only', only],
        { timeoutMs: T.tts, cwd: dir }],
      ['elastic_fit', [cfg.ttsPython, join(cfg.repoRoot, 'aicutplan', 'chain', 'elastic_fit.py'),
        '--project', projArg], { timeoutMs: T.fit }],
      ['stamp-tts', [cfg.python, '-X', 'utf8', '-c',
        'import sys; from aicutplan.freshness import stamp; stamp(sys.argv[1], "tts")', projArg],
        { timeoutMs: 60000 }],
      ['r6_mix', [cfg.python, join(dir, 'r6_mix.py'), '--project', projArg],
        { timeoutMs: T.mix, cwd: dir }],
      ['r7_mux', [cfg.python, join(dir, 'r7_mux.py'), '--project', projArg],
        { timeoutMs: T.mux, cwd: dir }],
    ]
    if (request.snapshot === true) {
      plan.push(['snapshot', [cfg.python, '-X', 'utf8', '-m', 'aicutplan', 'runs', 'snapshot',
        '--project', projArg, '--sentences', request.sentences,
        '--label', request.label || 'refit'], { timeoutMs: T.snapshot }])
    }

    if (request.dryRun === true) {
      return { ok: true, changed, steps: [], dryRun: true, text:
        '将重跑 ' + changed.length + ' 句（' + only + '）：\n' +
        plan.map(([name, argv]) => '  ' + name + ': ' + argv.slice(1).join(' ')).join('\n') }
    }

    const steps = []
    for (const [name, argv, options] of plan) {
      try {
        steps.push(await step(name, argv, options))
      } catch (error) {
        steps.push(error.row)
        return { ok: false, changed, steps, text: summarize(changed, steps, error.message) }
      }
    }
    return { ok: true, changed, steps, text: summarize(changed, steps) }
  }
}

function summarize(changed, steps, failure) {
  const lines = [failure ? '局部重跑中断：' + failure : '局部重跑完成']
  lines.push('重合成句子：' + changed.join(', '))
  for (const s of steps) {
    if (!s) continue
    lines.push('  · ' + s.name + '  ' + (s.exitCode === 0 ? 'OK' : '失败') +
      '  (' + (s.ms / 1000).toFixed(1) + 's)')
  }
  const last = steps.filter(Boolean).slice(-1)[0]
  if (last?.text) {
    const tail = last.text.split('\n').slice(-12).join('\n')
    lines.push('最后一步输出：\n' + tail)
  }
  return lines.join('\n')
}
