/**
 * 面向模型的工具定义：每个工具对应 aicutplan 的一个确定性子命令。
 *
 * 注册形状参考 dsh-ffmpeg：`{ name, description, parameters(JSON Schema), output, execute, timeoutMs }`。
 * 描述只写「做什么/返回什么」，流程规则放在随包的 skill 里，避免每个 schema 重复一遍。
 *
 * @module @local/dsh-aicut/tools
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { TIMEOUTS } from './config.js'
import { artifactSummary, exitHint, firstJson, projectDir, projectFile, runAicut, tailLines } from './cli.js'
import { createRefit } from './refit.js'

const projectParam = {
  type: 'string',
  description: '工程路径：工程目录（project.json 所在目录）或 project.json 本身，绝对路径。',
}

/** 统一输出：模型看到的是 text。 */
const TEXT_OUTPUT = {
  schema: { type: 'object', additionalProperties: true },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

function compose(result, extraLines = []) {
  const head = [
    result.display,
    '退出码 ' + String(result.exitCode) + '（' + (result.durationMs / 1000).toFixed(1) + 's）',
    ...extraLines,
  ]
  const body = result.text.trim() === '' ? '（无输出）' : tailLines(result.text, 40)
  const hint = result.exitCode === 0 ? '' : '\n' + (exitHint(result) ?? '看上面日志定位；先用 aicut_status 确认上游产物是否 fresh。')
  return head.join('\n') + '\n' + body + hint
}

async function callTool(cfg, runner, args, options, extraLines = []) {
  const result = await runAicut(cfg, runner, args, options)
  return {
    exitCode: result.exitCode,
    command: result.argv,
    durationMs: result.durationMs,
    text: compose(result, extraLines),
  }
}

function readProjectJson(projectPath) {
  try {
    return JSON.parse(readFileSync(projectPath, 'utf8'))
  } catch {
    return undefined
  }
}

/** 列出质检宫格图（模型之后用 read_image 逐格判定）。 */
function qcGrids(projectPath) {
  const dir = join(projectDir(projectPath), '.qcframes', 'session')
  if (!existsSync(dir)) return []
  return readdirSync(dir)
    .filter((name) => /^grid_.*\.(jpg|jpeg|png)$/i.test(name))
    .sort()
    .map((name) => join(dir, name))
}

/**
 * @param {import('./config.js').AicutConfig} cfg
 * @param {ReturnType<import('./run.js').createRunner>} runner
 */
export function buildTools(cfg, runner) {
  const refit = createRefit(cfg, runner)
  return [
    // ---------- 只读：环境与工程 ----------
    {
      name: 'aicut_env',
      description: 'AICutplan 自检：解释器、仓库根、作品库、ffmpeg、aicutplan 版本是否可用。排查任何失败前先跑它。',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      output: TEXT_OUTPUT,
      async execute(_args, exec) {
        const lines = []
        lines.push('仓库根: ' + cfg.repoRoot + (existsSync(cfg.repoRoot) ? '' : '（不存在！）'))
        lines.push('规划/渲染解释器: ' + cfg.python + (existsSync(cfg.python) ? '' : '（不存在！）'))
        lines.push('TTS/ASR 解释器: ' + cfg.ttsPython + (existsSync(cfg.ttsPython) ? '' : '（不存在！）'))
        lines.push('作品库: ' + cfg.libraryRoot + (existsSync(cfg.libraryRoot) ? '' : '（不存在，可用 config.libraryRoot 覆盖）'))
        const version = await runAicut(cfg, runner, ['version'], { timeoutMs: TIMEOUTS.env, signal: exec?.signal })
        lines.push('版本: ' + version.text.trim().split('\n').slice(-1)[0])
        try {
          const ff = await runner.run([cfg.ffmpeg || 'ffmpeg', '-version'], { timeoutMs: 20000, signal: exec?.signal })
          lines.push('ffmpeg: ' + (ff.stdout.trim().split('\n')[0] || '已找到'))
        } catch (error) {
          lines.push('ffmpeg: 无法启动（' + String(error?.message ?? error) + '）→ 配置 config.ffmpeg 为绝对路径')
        }
        return { exitCode: version.exitCode, text: lines.join('\n') }
      },
      timeoutMs: TIMEOUTS.env,
    },
    {
      name: 'aicut_projects',
      description: '列出作品库的动漫 / 集 / 最新归档 / 告警数，用于确定 project 路径。',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      output: TEXT_OUTPUT,
      async execute(_args, exec) {
        const result = await runAicut(cfg, runner, ['lib', 'list', '--json'], { timeoutMs: TIMEOUTS.projects, signal: exec?.signal })
        const rows = firstJson(result.text)
        if (!Array.isArray(rows)) {
          return { exitCode: result.exitCode, text: compose(result) }
        }
        const lines = ['作品库: ' + cfg.libraryRoot]
        for (const series of rows) {
          lines.push('· ' + series.series + (series.episodes?.length ? '' : '（尚无集工程）'))
          for (const episode of series.episodes ?? []) {
            const stale = episode.stale?.length ? ' 告警 ' + episode.stale.length + ' 条' : ' 干净'
            const final = episode.final_bytes ? (episode.final_bytes / 1048576).toFixed(0) + 'MB' : '-'
            lines.push('    ' + episode.episode + '  最新归档 ' + (episode.latest_run || '未归档') + '  成片 ' + final + stale)
            if (series.dir) lines.push('      ' + join(series.dir, episode.episode))
          }
        }
        return { exitCode: 0, text: lines.join('\n') }
      },
      timeoutMs: TIMEOUTS.projects,
    },
    {
      name: 'aicut_status',
      description: '只读查看工程状态：渲染门禁四阶段（fresh/stale）、最新归档、关键产物时间与大小、工程配置要点。',
      parameters: { type: 'object', properties: { project: projectParam }, required: ['project'], additionalProperties: false },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const lines = ['工程: ' + project]
        const projectJson = readProjectJson(project)
        if (projectJson) {
          const recap = projectJson.recap ?? {}
          lines.push('配置: fps=' + (projectJson.fps ?? '?') + '  音色=' + (recap.voice ?? '?')
            + '  语速=' + (recap.tts_rate ?? '?') + '  钩子=' + JSON.stringify(recap.hook_clip ?? null))
          if (projectJson.source_video) lines.push('源片: ' + projectJson.source_video)
        }
        const guard = await runAicut(cfg, runner, ['guard', '--project', project, '--json'], { timeoutMs: TIMEOUTS.status, signal: exec?.signal })
        const guardJson = firstJson(guard.text)
        if (guardJson?.rows) {
          const mark = { fresh: 'OK   ', stale: 'STALE', unknown: '未知 ' }
          for (const row of guardJson.rows) {
            lines.push('  ' + (mark[row.status] ?? row.status) + ' ' + row.stage + ' · ' + row.detail
              + (row.status === 'fresh' ? '' : '  → ' + (row.fix ?? '需重建')))
          }
        } else {
          lines.push('  门禁: ' + tailLines(guard.text, 6))
        }
        const gates = guardJson?.gates
        if (gates) {
          lines.push('  放行: mix=' + (gates.mix?.length ? '否' : '是') + '  mux=' + (gates.mux?.length ? '否' : '是'))
        }
        const latest = await runAicut(cfg, runner, ['runs', 'latest', '--project', project, '--json'], { timeoutMs: TIMEOUTS.status, signal: exec?.signal })
        const latestJson = firstJson(latest.text)
        if (latestJson?.run_id) {
          lines.push('最新归档: ' + latestJson.run_id + '  标签=' + (latestJson.label ?? '-') + '  ' + (latestJson.created_at ?? ''))
        } else {
          lines.push('最新归档: 还没有（runs snapshot 归档后才有）')
        }
        lines.push('产物:')
        for (const artifact of artifactSummary(project)) {
          lines.push('  ' + (artifact.exists
            ? artifact.file + '  ' + (artifact.bytes / 1048576).toFixed(1) + 'MB  ' + artifact.mtime
            : artifact.file + '  缺失'))
        }
        const grids = qcGrids(project)
        if (grids.length) lines.push('质检宫格: ' + grids.length + ' 张（' + grids[0] + ' …）')
        return { exitCode: 0, text: lines.join('\n') }
      },
      timeoutMs: TIMEOUTS.status,
    },
    {
      name: 'aicut_check',
      description: '工程契约体检：lint、窗口时间轴、旁白/TTS 一致性、台词源指纹。可选抽高清帧 / OCR 参考画面数字（只作参考，剧情以台词为准）。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          number_frames: { type: 'boolean', description: '抽 1280 宽帧到 .numframes/，供读图核对画面浮字。' },
          ocr: { type: 'boolean', description: '用 OCR 识别这些帧，回答「画面当时写了什么」（需装 rapidocr-onnxruntime）。' },
          ocr_dense: { type: 'boolean', description: '配合 ocr：对没读到数字的句子按 1 帧/秒补抽。' },
        },
        required: ['project'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const args = ['check', '--project', project]
        if (rawArgs.number_frames === true) args.push('--number-frames')
        if (rawArgs.ocr === true) args.push('--ocr')
        if (rawArgs.ocr_dense === true) args.push('--ocr-dense')
        return callTool(cfg, runner, args, { timeoutMs: TIMEOUTS.check, signal: exec?.signal })
      },
      timeoutMs: TIMEOUTS.check,
    },
    {
      name: 'aicut_refit',
      description: '局部重跑闭环：改几句旁白后，只重合成这几句并重出成片（narrate → r5 --only → elastic_fit → 盖章 → r6 → r7，可选归档）。画面不动，比整链 run 快一个数量级。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          sentences: { type: 'string', description: '改好的句子稿绝对路径（需带 src，历史稿先用 aicut_upgrade_sents 升级）。' },
          only: { type: 'array', items: { type: 'integer' }, description: '只重跑这些句号；省略则与当前 narration 文本比对自动求差。' },
          snapshot: { type: 'boolean', description: '完成后归档到 runs/。' },
          label: { type: 'string', description: '归档标签，如 v3.7。' },
          dry_run: { type: 'boolean', description: '只列出将执行的步骤与命中的句子。' },
        },
        required: ['project', 'sentences'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const sentences = String(rawArgs.sentences ?? '').trim()
        if (!sentences || !existsSync(sentences)) throw new Error('句子稿不存在：' + sentences)
        const result = await refit({
          project, sentences,
          only: Array.isArray(rawArgs.only) ? rawArgs.only : undefined,
          snapshot: rawArgs.snapshot === true,
          label: typeof rawArgs.label === 'string' ? rawArgs.label : undefined,
          dryRun: rawArgs.dry_run === true,
          signal: exec?.signal,
        })
        return { exitCode: result.ok ? 0 : 2, changed: result.changed, text: result.text }
      },
      timeoutMs: TIMEOUTS.render,
    },
    {
      name: 'aicut_upgrade_sents',
      description: '把没有 src 的历史句子稿升级为 src-v2：按 ws/we 从 cutplan 补源片区间，先只读契约校验，通过才落盘。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          sentences: { type: 'string', description: '旧句子稿路径（相对工程目录或绝对路径）。' },
          out: { type: 'string', description: '输出文件名，如 my_sentences_v36.json。' },
          apply: { type: 'boolean', description: '校验通过后真正写盘；默认只预览。' },
          force: { type: 'boolean', description: '输出已存在时覆盖。' },
        },
        required: ['project', 'sentences', 'out'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        // upgrade_sents 不在 `-m aicutplan` 门面里，直接用模块路径跑
        const argv = [cfg.python, '-X', 'utf8', '-m', 'aicutplan.legacy.upgrade_sents',
          '--project', project, '--sentences', String(rawArgs.sentences),
          '--out', String(rawArgs.out)]
        if (rawArgs.apply === true) argv.push('--apply')
        if (rawArgs.force === true) argv.push('--force')
        const result = await runner.run(argv, {
          cwd: cfg.repoRoot, timeoutMs: TIMEOUTS.check, signal: exec?.signal,
        })
        const text = [result.stdout, result.stderr].filter((x) => x && x.trim() !== '').join('\n')
        return {
          exitCode: result.exitCode,
          text: argv.join(' ') + '\n退出码 ' + String(result.exitCode) + '\n' +
            (tailLines(text, 24) || '（无输出）') +
            (result.exitCode === 0 ? '' : '\n' + (exitHint(result) ?? '看上面日志定位。')),
        }
      },
      timeoutMs: TIMEOUTS.check,
    },

    // ---------- 台词源 / 画面先定 / 旁白 ----------
    {
      name: 'aicut_subs',
      description: '内嵌字幕优先：探测源片字幕轨并（可选）提取为台词源，跳过 ASR。apply=true 才写盘。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          apply: { type: 'boolean', description: 'true 则落盘替换台词源；默认只探测对比。' },
          prefer: { type: 'string', description: '语言优先级，逗号分隔，默认 ja,zh,en。' },
          refresh_derived: { type: 'boolean', description: 'apply 时同时重建依赖产物（译中缓存迁移）。' },
          check_only: { type: 'boolean', description: 'true 只校验现有台词源指纹是否过期。' },
        },
        required: ['project'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const args = ['subs', '--project', project]
        if (rawArgs.apply === true) args.push('--apply')
        if (rawArgs.refresh_derived === true) args.push('--refresh-derived')
        if (rawArgs.check_only === true) args.push('--check')
        if (typeof rawArgs.prefer === 'string' && rawArgs.prefer.trim() !== '') args.push('--prefer', rawArgs.prefer.trim())
        return callTool(cfg, runner, args, { timeoutMs: TIMEOUTS.subs, signal: exec?.signal })
      },
      timeoutMs: TIMEOUTS.subs,
    },
    {
      name: 'aicut_plan',
      description: '画面先定：重新生成 json/cutplan.json（因果图谱 + 连续爽点钩子 + 权重选窗）。已定型工程不要跑。',
      parameters: { type: 'object', properties: { project: projectParam }, required: ['project'], additionalProperties: false },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        return callTool(cfg, runner, ['plan', '--project', project], { timeoutMs: TIMEOUTS.plan, signal: exec?.signal })
      },
      timeoutMs: TIMEOUTS.plan,
    },
    {
      name: 'aicut_narrate',
      description: '旁白层：export=true 导出逐窗写作表（window_table.md）；给 sentences 则落盘句子稿并在写盘前校验契约。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          export: { type: 'boolean', description: 'true 只导出写作输入表，不写 narration。' },
          sentences: { type: 'string', description: '句子稿 JSON 绝对路径；每句需带 src:[起,止] 绑定窗口。' },
        },
        required: ['project'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const wantsExport = rawArgs.export === true
        const sentences = typeof rawArgs.sentences === 'string' && rawArgs.sentences.trim() !== '' ? rawArgs.sentences.trim() : undefined
        if (wantsExport === Boolean(sentences)) {
          throw new Error('export 与 sentences 二选一：先 export 拿写作表，写完稿再用 sentences 落盘。')
        }
        const args = ['narrate', '--project', project]
        if (wantsExport) args.push('--export')
        if (sentences) {
          if (!existsSync(sentences)) throw new Error('句子稿不存在：' + sentences)
          args.push('--sentences', sentences)
        }
        const result = await callTool(cfg, runner, args, { timeoutMs: TIMEOUTS.narrate, signal: exec?.signal })
        if (wantsExport && result.exitCode === 0) {
          const table = join(projectDir(project), 'window_table.md')
          if (existsSync(table)) result.text += '\n写作表: ' + table + '（用 read 打开，逐窗对着画面写）'
        }
        if (sentences && result.exitCode === 0) {
          result.text += '\n下一步: aicut_check 看覆盖率，再 aicut_render 出片（或 aicut_qc 先质检）。'
        }
        return result
      },
      timeoutMs: TIMEOUTS.narrate,
    },

    // ---------- 质检 ----------
    {
      name: 'aicut_qc',
      description: '会话内逐句质检：plan 生成 2×3 宫格图批次（返回图片路径供读图）；merge 合并判定；probe 对失配句抽多帧复核。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          plan: { type: 'integer', description: '每批句数，>0 时重抽宫格并生成 plan_XX.json。' },
          merge: { type: 'boolean', description: 'true 合并会话内 verdict_XX.json。' },
          probe: { type: 'boolean', description: 'true 对失配句抽 3 帧复核，排除单帧假阳性。' },
        },
        required: ['project'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const modes = [Number(rawArgs.plan) > 0, rawArgs.merge === true, rawArgs.probe === true].filter(Boolean)
        if (modes.length !== 1) throw new Error('plan / merge / probe 必须且只能选一个。')
        const args = ['qc', '--project', project]
        if (Number(rawArgs.plan) > 0) args.push('--plan', String(Math.trunc(Number(rawArgs.plan))))
        if (rawArgs.merge === true) args.push('--merge')
        if (rawArgs.probe === true) args.push('--probe')
        const result = await callTool(cfg, runner, args, { timeoutMs: TIMEOUTS.qc, signal: exec?.signal })
        if (Number(rawArgs.plan) > 0 && result.exitCode === 0) {
          const grids = qcGrids(project)
          const run = grids.filter((file) => {
            const age = Date.now() - statSync(file).mtimeMs
            return age < 10 * 60 * 1000
          })
          if (run.length) {
            result.text += '\n宫格图（' + run.length + ' 张，用 read_image 逐格看）：\n' + run.map((file) => '  ' + file).join('\n')
            result.text += '\n判定写到 .qcframes/session/verdict_XX.json：{"i":3,"match":0,"why":"…"}，再 aicut_qc merge。'
          }
        }
        return result
      },
      timeoutMs: TIMEOUTS.qc,
    },

    {
      name: 'aicut_qc_compare',
      description: '质检交叉验证的机械比对：给两套模型各自跑出的 verdict 目录，算一致率、列分歧、给出需要 probe 的并集，并写 json/qc_cross.json。派发模型本身仍由会话层做。',
      parameters: {
        type: 'object',
        properties: {
          a_dir: { type: 'string', description: '模型 A 的 verdict 目录（含 verdict_*.json），绝对路径。' },
          b_dir: { type: 'string', description: '模型 B 的 verdict 目录，绝对路径。' },
          project: { type: 'string', description: '可选：给工程路径则把结果写进 <工程>/json/qc_cross.json。' },
        },
        required: ['a_dir', 'b_dir'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const args = ['--a', String(rawArgs.a_dir), '--b', String(rawArgs.b_dir)]
        if (typeof rawArgs.project === 'string' && rawArgs.project.trim() !== '') {
          args.push('--project', projectFile(rawArgs.project))
        }
        // qc_compare 不在 `-m aicutplan` 门面里，直接用模块路径跑
        const argv = [cfg.python, '-X', 'utf8', '-m', 'aicutplan.chain.qc_compare', ...args]
        const result = await runner.run(argv, {
          cwd: cfg.repoRoot, timeoutMs: TIMEOUTS.check, signal: exec?.signal,
        })
        const text = [result.stdout, result.stderr].filter((x) => x && x.trim() !== '').join('\n')
        return {
          exitCode: result.exitCode,
          text: (tailLines(text, 30) || '（无输出）') +
            (result.exitCode === 0 ? '' : '\n' + (exitHint(result) ?? '看上面日志定位。')),
        }
      },
      timeoutMs: TIMEOUTS.check,
    },

    // ---------- 渲染 / 批量 ----------
    {
      name: 'aicut_render',
      description: '跑 cut-first 主链出片（cf_pipeline）：可选跳过规划/TTS/渲染、预演、质检闭环、归档标签。耗时数分钟到数十分钟。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          sentences: { type: 'string', description: '会话内已写好的句子稿绝对路径；不传则走 LLM 自主写作。' },
          skip_plan: { type: 'boolean', description: '跳过因果+画面先定（cutplan 已定型时）。' },
          skip_tts: { type: 'boolean', description: '跳过 TTS。' },
          skip_render: { type: 'boolean', description: '跳过混音合片，只到 TTS 为止。' },
          qc: { type: 'boolean', description: '出片后接着生成质检批次。' },
          qc_per_batch: { type: 'integer', description: '质检每批句数，默认 6。' },
          subs: { type: 'string', enum: ['auto', 'yes', 'no'], description: '台词源：auto 只提示、yes 先替换、no 不动。默认 auto。' },
          label: { type: 'string', description: '本次归档标签，如 v36。' },
          allow_stale: { type: 'boolean', description: '门禁不过也强行渲染（会混入旧素材），仅调试用。' },
          no_snapshot: { type: 'boolean', description: '不归档到 runs/。' },
          dry_run: { type: 'boolean', description: '只打印要执行的命令。' },
        },
        required: ['project'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const args = ['run', '--project', project]
        if (typeof rawArgs.sentences === 'string' && rawArgs.sentences.trim() !== '') {
          const sentences = rawArgs.sentences.trim()
          if (!existsSync(sentences)) throw new Error('句子稿不存在：' + sentences)
          args.push('--sentences', sentences)
        }
        for (const [flag, key] of [['--skip-plan', 'skip_plan'], ['--skip-tts', 'skip_tts'], ['--skip-render', 'skip_render'],
          ['--qc', 'qc'], ['--allow-stale', 'allow_stale'], ['--no-snapshot', 'no_snapshot'], ['--dry-run', 'dry_run']]) {
          if (rawArgs[key] === true) args.push(flag)
        }
        if (Number(rawArgs.qc_per_batch) > 0) args.push('--qc-per-batch', String(Math.trunc(Number(rawArgs.qc_per_batch))))
        if (typeof rawArgs.subs === 'string') args.push('--subs', rawArgs.subs)
        if (typeof rawArgs.label === 'string' && rawArgs.label.trim() !== '') args.push('--label', rawArgs.label.trim())
        const result = await callTool(cfg, runner, args, { timeoutMs: TIMEOUTS.render, signal: exec?.signal })
        if (result.exitCode === 0 && rawArgs.dry_run !== true) {
          const final = join(projectDir(project), 'out', '03-rapid-narration.mp4')
          if (existsSync(final)) {
            const stat = statSync(final)
            result.text += '\n成片: ' + final + '（' + (stat.size / 1048576).toFixed(1) + 'MB, ' + stat.mtime.toISOString() + '）'
          }
          result.text += '\n出片后建议: aicut_qc plan → 逐格读图判定 → aicut_qc merge → 需要改词时 aicut_render qc_apply 或重跑。'
        }
        return result
      },
      timeoutMs: TIMEOUTS.render,
    },
    {
      name: 'aicut_batch',
      description: '按导演稿批量出片（aicutplan batch）：导演稿是数据，链路含断点续跑与前置完整性校验。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          script: { type: 'string', description: '导演稿 JSON 绝对路径（_batch/director_EP0X.json）。' },
          from_step: { type: 'string', description: '断点续跑：从哪一步开始（如 narrate）。' },
          dry_run: { type: 'boolean', description: '只打印命令。' },
        },
        required: ['project', 'script'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const script = String(rawArgs.script ?? '').trim()
        if (!existsSync(script)) throw new Error('导演稿不存在：' + script)
        const args = ['batch', '--project', project, '--script', script]
        if (typeof rawArgs.from_step === 'string' && rawArgs.from_step.trim() !== '') args.push('--from-step', rawArgs.from_step.trim())
        if (rawArgs.dry_run === true) args.push('--dry-run')
        return callTool(cfg, runner, args, { timeoutMs: TIMEOUTS.batch, signal: exec?.signal })
      },
      timeoutMs: TIMEOUTS.batch,
    },

    // ---------- 版本 / 清理 ----------
    {
      name: 'aicut_runs',
      description: '生成版本管理：snapshot 归档当前成片、list 列版本、latest 打印带指纹校验的最新指针、verify 校验归档完整性。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          action: { type: 'string', enum: ['snapshot', 'list', 'latest', 'verify'], description: '要做的动作。' },
          label: { type: 'string', description: 'snapshot 的标签，如 v36。' },
        },
        required: ['project', 'action'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const action = String(rawArgs.action ?? '')
        if (!['snapshot', 'list', 'latest', 'verify'].includes(action)) throw new Error('action 只能是 snapshot / list / latest / verify。')
        const args = ['runs', action, '--project', project]
        if (action === 'snapshot' && typeof rawArgs.label === 'string' && rawArgs.label.trim() !== '') {
          args.push('--label', rawArgs.label.trim())
        }
        return callTool(cfg, runner, args, { timeoutMs: TIMEOUTS.runs, signal: exec?.signal })
      },
      timeoutMs: TIMEOUTS.runs,
    },
    {
      name: 'aicut_clean',
      description: '冗余盘点与可恢复回收：默认只列清单；apply 移入 .trash；restore 放回最近一批；empty 真正释放空间。',
      parameters: {
        type: 'object',
        properties: {
          project: projectParam,
          apply: { type: 'boolean', description: 'true 才真正移动文件（进 .trash，可恢复）。' },
          deep: { type: 'boolean', description: '连同可再生的中间大件一起扫描。' },
          restore: { type: 'boolean', description: '把最近一批从 .trash 放回。' },
          empty: { type: 'boolean', description: '清空 .trash（不可恢复）。' },
        },
        required: ['project'],
        additionalProperties: false,
      },
      output: TEXT_OUTPUT,
      async execute(rawArgs, exec) {
        const project = projectFile(rawArgs.project)
        const args = ['clean', '--project', project]
        if (rawArgs.restore === true) args.push('--restore')
        else if (rawArgs.empty === true) args.push('--empty')
        else {
          if (rawArgs.apply === true) args.push('--apply')
          if (rawArgs.deep === true) args.push('--deep')
        }
        return callTool(cfg, runner, args, { timeoutMs: TIMEOUTS.clean, signal: exec?.signal })
      },
      timeoutMs: TIMEOUTS.clean,
    },
  ]
}
