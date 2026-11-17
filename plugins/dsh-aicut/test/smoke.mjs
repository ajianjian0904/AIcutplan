/**
 * 冒烟测试：不启动 DSH，用假 ctx 直接调用工具，验证 argv、路径校验、输出解析。
 *
 * 用法（任意 Node 22/24）：
 *   node test/smoke.mjs "E:\workbuddy\漫剪作品库\被驱逐的加强师\EP01"
 */
import { apply } from '../index.js'

const project = process.argv[2]

const tools = []
const ctx = {
  tools: { register(definition) { tools.push(definition); return () => {} } },
  on() { return () => {} },
}

apply(ctx, {})
console.log('注册工具:', tools.map((tool) => tool.name).join(', '))

async function show(name, args) {
  const tool = tools.find((item) => item.name === name)
  if (!tool) throw new Error('没有这个工具: ' + name)
  const started = Date.now()
  const value = await tool.execute(args ?? {}, {})
  const text = value?.text ?? ''
  console.log('\n=== ' + name + ' (' + (Date.now() - started) + 'ms, exit=' + value?.exitCode + ') ===')
  console.log(text.split('\n').slice(0, 24).join('\n'))
  return value
}

await show('aicut_env')
await show('aicut_projects')
if (project) {
  const status = await show('aicut_status', { project })
  await show('aicut_check', { project })
  // 局部重跑闭环：dry_run 只列步骤，不落盘（与当前 narration 一致时应当报「没有可重跑的句子」）
  const sents = project.replace(/[\\/]project\.json$/i, '') + '\\my_sentences_v37.json'
  await show('aicut_refit', { project, sentences: sents, dry_run: true })
  // 历史稿升级：预览模式（不写盘），应报契约通过
  await show('aicut_upgrade_sents', {
    project, sentences: 'my_sentences_v35.json', out: '_smoke_v38.json',
  })
  // 路径校验：目录里没有 project.json 应给出可行动的报错
  try {
    const tool = tools.find((item) => item.name === 'aicut_status')
    await tool.execute({ project: 'E:\\不存在\\EP99' }, {})
    console.log('\n[FAIL] 不存在的路径没有被拒绝')
    process.exitCode = 1
  } catch (error) {
    console.log('\n路径校验: ' + error.message)
  }
  if (status?.exitCode !== 0) process.exitCode = 1
} else {
  console.log('\n（未传工程路径，跳过 aicut_status / aicut_check）')
}
