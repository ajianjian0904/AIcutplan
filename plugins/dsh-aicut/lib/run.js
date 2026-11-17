/**
 * 进程执行层：统一成 Promise 式 runner。
 *
 * 优先走 DSH 的 subprocess 服务（宿主管理的进程范围、树级终止、tail 收集）；
 * 该服务不可用时（例如在普通 Node 里跑冒烟测试）退化为 node:child_process，
 * 两者都是 argv 数组、不经 shell，避免命令拼接与注入。
 *
 * @module @local/dsh-aicut/run
 */
import { spawn } from 'node:child_process'

const COLLECT_BYTES = 4 * 1024 * 1024

/** 有界收集器：只保留尾部 maxBytes，避免长渲染日志把宿主内存吃掉。 */
class TailCollector {
  constructor(maxBytes) {
    this.maxBytes = maxBytes
    this.chunks = []
    this.total = 0
  }

  push(chunk) {
    this.chunks.push(chunk)
    this.total += chunk.length
    while (this.total > this.maxBytes * 2 && this.chunks.length > 1) {
      const head = this.chunks.shift()
      this.total -= head.length
    }
  }

  text() {
    const buffer = Buffer.concat(this.chunks)
    const tail = buffer.length > this.maxBytes ? buffer.subarray(buffer.length - this.maxBytes) : buffer
    return tail.toString('utf8')
  }
}

/** 子进程环境：强制 UTF-8，否则 Windows 控制台代码页会把中文日志变成乱码。 */
export function childEnv(cfg) {
  const env = {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    PYTHONUNBUFFERED: '1',
  }
  if (cfg.repoRoot) env.AICUTPLAN_REPO = cfg.repoRoot
  if (cfg.libraryRoot) env.AICUTPLAN_LIBRARY = cfg.libraryRoot
  if (cfg.ffmpeg) env.AICUTPLAN_FFMPEG = cfg.ffmpeg
  if (cfg.ttsPython) env.AICUTPLAN_TTS_PY = cfg.ttsPython
  return env
}

/**
 * @param {any} ctx
 * @param {import('./config.js').AicutConfig} cfg
 */
export function createRunner(ctx, cfg) {
  // Cordis 里未注入的服务一旦访问就会抛错；这里兜一层，保住「退化到 node:child_process」
  // 的路径（冒烟测试就是靠它跑的）。正常部署在 index.js 里已把 subprocess 写进 inject。
  let seam
  try {
    seam = ctx?.subprocess
  } catch {
    seam = undefined
  }
  const useSeam = seam !== null && typeof seam === 'object' && typeof seam.spawn === 'function'

  return {
    /**
     * @param {string[]} argv
     * @param {{ timeoutMs?: number, cwd?: string, signal?: AbortSignal }} [options]
     */
    async run(argv, options = {}) {
      const request = {
        argv,
        cwd: options.cwd ?? cfg.repoRoot,
        timeoutMs: options.timeoutMs ?? cfg.timeoutMs,
        graceMs: cfg.graceMs,
        maxOutputBytes: cfg.maxOutputBytes,
        signal: options.signal,
      }
      return useSeam ? runViaSeam(seam, cfg, request) : runLocal(cfg, request)
    },
  }
}

function runLocal(cfg, request) {
  return new Promise((resolve, reject) => {
    const startedAt = Date.now()
    const stdout = new TailCollector(request.maxOutputBytes)
    const stderr = new TailCollector(request.maxOutputBytes)
    let timedOut = false
    let child
    try {
      child = spawn(request.argv[0], request.argv.slice(1), {
        cwd: request.cwd,
        env: childEnv(cfg),
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch (error) {
      reject(error)
      return
    }
    const kill = () => {
      if (child.exitCode !== null) return
      child.kill('SIGTERM')
      setTimeout(() => { if (child.exitCode === null) child.kill('SIGKILL') }, request.graceMs).unref()
    }
    const timer = setTimeout(() => { timedOut = true; kill() }, request.timeoutMs)
    const onAbort = () => kill()
    if (request.signal) {
      if (request.signal.aborted) { kill() } else { request.signal.addEventListener('abort', onAbort, { once: true }) }
    }
    child.stdout?.on('data', (chunk) => stdout.push(chunk))
    child.stderr?.on('data', (chunk) => stderr.push(chunk))
    child.on('error', (error) => { clearTimeout(timer); reject(error) })
    child.on('close', (exitCode, signalCode) => {
      clearTimeout(timer)
      request.signal?.removeEventListener?.('abort', onAbort)
      resolve({
        exitCode,
        signalCode,
        stdout: stdout.text(),
        stderr: stderr.text(),
        timedOut,
        aborted: request.signal?.aborted === true,
        durationMs: Date.now() - startedAt,
      })
    })
  })
}

async function runViaSeam(seam, cfg, request) {
  const controller = new AbortController()
  const timer = setTimeout(
    () => controller.abort(new Error('aicutplan 操作超时（' + request.timeoutMs + 'ms）')),
    request.timeoutMs,
  )
  const signal = request.signal === undefined
    ? controller.signal
    : AbortSignal.any([request.signal, controller.signal])
  const startedAt = Date.now()
  try {
    const handle = seam.spawn({
      argv: request.argv,
      cwd: request.cwd,
      env: childEnv(cfg),
      stdio: {
        stdin: 'ignore',
        stdout: { maxBytes: Math.min(COLLECT_BYTES, request.maxOutputBytes) },
        stderr: { maxBytes: Math.min(COLLECT_BYTES, request.maxOutputBytes) },
      },
      graceMs: request.graceMs,
      signal,
    })
    const outcome = await handle.done
    if (signal.aborted) throw signal.reason
    const out = handle.collected?.stdout?.readFrom(0)
    const err = handle.collected?.stderr?.readFrom(0)
    return {
      exitCode: outcome.exitCode,
      signalCode: outcome.signal,
      stdout: out?.text ?? '',
      stderr: err?.text ?? '',
      timedOut: controller.signal.aborted && request.signal?.aborted !== true,
      aborted: request.signal?.aborted === true,
      durationMs: Date.now() - startedAt,
    }
  } finally {
    clearTimeout(timer)
  }
}
