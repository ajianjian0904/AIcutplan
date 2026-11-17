/**
 * 配置解析：默认值 + patch 覆盖 + 环境变量兜底。
 *
 * Loader 的覆盖语义是「整段替换 config」，所以这里必须逐字段与默认值合并，
 * 用户在 cordis.patch.yml 只写一项也不会把其余默认值清空。
 *
 * @module @local/dsh-aicut/config
 */
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'

/** @typedef {{
 *   repoRoot: string, python: string, ttsPython: string, libraryRoot: string,
 *   ffmpeg: string, timeoutMs: number, graceMs: number, maxOutputBytes: number
 * }} AicutConfig */

const DEFAULTS = {
  repoRoot: 'E:/workbuddy/漫剪软件开发',
  python: 'D:/Python312/python.exe',
  ttsPython: 'C:/Users/10451/.workbuddy/binaries/python/envs/default/Scripts/python.exe',
  libraryRoot: 'E:/workbuddy/漫剪作品库',
  ffmpeg: '',
  timeoutMs: 900000,
  graceMs: 3000,
  maxOutputBytes: 1048576,
}

const ENV = {
  repoRoot: 'AICUTPLAN_REPO',
  python: 'AICUTPLAN_PY',
  ttsPython: 'AICUTPLAN_TTS_PY',
  libraryRoot: 'AICUTPLAN_LIBRARY',
  ffmpeg: 'AICUTPLAN_FFMPEG',
}

/** 单次工具调用的默认预算按工具名分档，避免把 15 分钟浪费在体检上。 */
export const TIMEOUTS = {
  env: 60000,
  projects: 60000,
  status: 120000,
  check: 120000,
  subs: 600000,
  plan: 900000,
  narrate: 600000,
  qc: 900000,
  render: 2700000,
  batch: 3600000,
  runs: 300000,
  clean: 300000,
}

/**
 * @param {any} raw
 * @returns {AicutConfig}
 */
export function resolveConfig(raw) {
  const cfg = { ...DEFAULTS }
  if (raw !== null && typeof raw === 'object') {
    for (const key of Object.keys(DEFAULTS)) {
      const value = raw[key]
      if (typeof value === typeof DEFAULTS[key] && value !== '') cfg[key] = value
    }
  }
  for (const [key, envName] of Object.entries(ENV)) {
    const value = process.env[envName]
    if (typeof value === 'string' && value.trim() !== '') cfg[key] = value.trim()
  }
  for (const key of ['repoRoot', 'python', 'ttsPython', 'libraryRoot', 'ffmpeg']) {
    // 含分隔符才算路径；裸名字（如 ffmpeg）留给 PATH 解析
    if (cfg[key] && /[\\/]/.test(cfg[key])) cfg[key] = resolve(cfg[key])
  }
  return cfg
}

/** 启动前的一次性自检：只报告，不抛错（工具调用时才给出可行动的失败指引）。 */
export function inspectConfig(cfg) {
  return {
    repoRoot: { value: cfg.repoRoot, exists: existsSync(cfg.repoRoot) },
    python: { value: cfg.python, exists: existsSync(cfg.python) },
    ttsPython: { value: cfg.ttsPython, exists: existsSync(cfg.ttsPython) },
    libraryRoot: { value: cfg.libraryRoot, exists: existsSync(cfg.libraryRoot) },
    ffmpeg: { value: cfg.ffmpeg || '(PATH)', exists: cfg.ffmpeg ? existsSync(cfg.ffmpeg) : true },
  }
}
