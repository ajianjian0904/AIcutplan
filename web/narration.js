/* 旁白工作台 · 无构建前端（第一版：L1 剧情级） */
'use strict';

let S = null, BEAT = null, PARSED = null, PROMPT = null, MODELS = [], toastTimer = null;
let renderTimer = null;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, method = 'GET', body) {
  const opt = {method, headers: {'Content-Type': 'application/json'}};
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || '请求失败');
  return j;
}
function toast(msg, bad = false) {
  const t = $('toast');
  t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3600);
}
const guard = fn => async (...a) => {
  try { await fn(...a); } catch (e) { toast(e.message, true); }
};
const varsOn = () => [...document.querySelectorAll('[data-v]:checked')].map(x => x.dataset.v);
const payload = () => ({
  template_id: $('tplSel').value, beat: BEAT, vars_on: varsOn(),
  user_note: $('userNote').value.trim(), model: $('modelSel').value || undefined,
});

// ---------------------------------------------------------------- render
function render() {
  if (!S) return;
  $('projName').textContent = S.project.name;
  $('beatCount').textContent = `共 ${S.project.beats} 块`;
  $('btnAll').classList.toggle('active', BEAT === null);
  $('btnFinalize').textContent = S.state.finalized ? '取消定稿' : '定稿';

  $('beatList').innerHTML = S.draft.beats.map(b => `
    <div class="beat ${b.id === BEAT ? 'current' : ''}" data-b="${b.id}">
      <span class="beat-id">${String(b.id).padStart(2, '0')}</span>
      <span class="beat-label">${esc(b.label)}</span>
      <span class="beat-cuts">${b.text.length}字</span>
      <span class="dot ${b.edited ? 'moved' : (S.state.finalized ? 'done' : '')}"></span>
    </div>`).join('');
  $('beatList').querySelectorAll('.beat').forEach(el =>
    el.onclick = () => { BEAT = +el.dataset.b; render(); schedulePrompt(); });

  const tpls = S.templates;
  const cur = $('tplSel').value;
  $('tplSel').innerHTML = tpls.map(t =>
    `<option value="${esc(t.id)}">${esc(t.name)} · ${t.stage}</option>`).join('');
  $('tplSel').value = tpls.some(t => t.id === cur) ? cur : (tpls.find(t => t.id === 'full') || tpls[0]).id;
  $('tplList').innerHTML = tpls.map(t =>
    `<div class="${t.id === $('tplSel').value ? 'on' : ''}" data-id="${esc(t.id)}">
       <span>${esc(t.name)}</span><span class="muted">${t.stage}${t.builtin ? ' · 内置' : ''}</span>
     </div>`).join('');
  $('tplList').querySelectorAll('div[data-id]').forEach(el =>
    el.onclick = () => { $('tplSel').value = el.dataset.id; render(); schedulePrompt(); });

  const c = S.check, b = S.budget;
  $('checkBox').innerHTML = `
    <div>字数 <b>${c.chars}</b> / 预算 <b>${c.budget}</b></div>
    <div>切句后 <b>${c.sentences}</b> 句 · 句长中位 <b>${c.len_median}</b> 字 · 超 18 字 <b>${c.over18}</b> 句</div>
    <div>转折词 <b>${c.turn_words}</b> 次（每 ${c.turn_interval ?? '—'}s）· 反转词 <b>${c.twist_words}</b> 次</div>
    <div class="muted">${esc(c.note)}</div>`;
  $('calibBox').innerHTML = `
    <div>有效语速 <b class="ok">${c.cps}</b> 字/秒（含首尾静音）</div>
    <div>预算 = ${b.out_dur}s × ${b.coverage}% × ${b.cps} = <b>${b.total}</b> 字</div>
    <div class="muted">${esc(S.calibration.source)}</div>`;
  const A = S.asr || {};
  $('asrBox').innerHTML = A.source_exists
    ? `<div>源 ASR：<b>${esc(A.lang_name)}</b> · ${A.segments} 段（${esc(A.asr_model || '未标注模型')}）</div>
       <div>已译 <b class="${A.ready ? 'ok' : ''}">${A.translated}</b> / ${A.segments} 段${A.incomplete ? ' · 未完成，可续传' : (A.ready ? ' · 已就绪' : '')}</div>
       <div class="muted">${A.ready ? '旁白模板已自动改用中文台词' : '模板暂用原文，译完自动切换'}</div>
       <div class="muted">${esc(A.output || '')}</div>`
    : '<div class="muted">工程里没有 asr_*.json，请先完成音转字</div>';
  $('btnAsrTranslate').disabled = !A.source_exists || !A.key_present;
  $('btnAsrClear').disabled = !A.has_output;
  $('gateBox').className = 'gate-box' + (S.locked ? '' : ' unlocked');
  $('gateBox').innerHTML = S.locked
    ? '未定稿 · 锚点校对台写回已<b>锁定</b>'
    : (S.state.finalized ? '已定稿 · 锚点校对台可写回' : '尚无草稿修改 · 锚点校对台不受影响');
  $('applyBox').className = 'gate-box' + (S.state.finalized ? ' unlocked' : '');
  $('applyBox').innerHTML = S.state.finalized
    ? '已定稿 · 可写回 <code>script.json</code>（自动备份）'
    : '未定稿 · 写回与重跑暂不可用';
  $('btnApplyScript').disabled = !S.state.finalized;
  $('snapList').innerHTML = S.snapshots.length
    ? S.snapshots.map(s => `<div data-name="${esc(s.name)}" title="点击回滚"><span class="nm">${esc(s.label)}</span><span class="muted">${esc(s.at)}</span></div>`).join('')
    : '<div class="muted">暂无快照</div>';
  $('snapList').querySelectorAll('div[data-name]').forEach(el =>
    el.onclick = () => restore(el.dataset.name));
  $('footStatus').innerHTML = S.state.finalized
    ? `<b>已定稿</b> · ${esc(S.state.finalized_at || '')}`
    : `<b>草稿</b> · ${S.draft.updated ? '更新于 ' + esc(S.draft.updated) : '尚未修改'}`;
  $('beatEditor').value = BEAT === null ? '' : (S.draft.beats[BEAT]?.text || '');
  $('editorHead').textContent = BEAT === null ? '当前块编辑（先点左侧块）' : `当前块 · ${String(BEAT).padStart(2, '0')} ${esc(S.draft.beats[BEAT].label)}`;
  $('beatEditor').disabled = BEAT === null;
  $('btnSaveBeat').disabled = BEAT === null;
  $('cfgKey').textContent = S.llm.key_present ? '环境变量 DEEPSEEK_API_KEY 已就绪' : '环境变量 DEEPSEEK_API_KEY 未设置';
}

function schedulePrompt() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(doRender, 260);
}

const doRender = guard(async () => {
  const r = await api('/api/narr/render', 'POST', payload());
  PROMPT = r.rendered;
  $('promptView').textContent = `【系统】\n${r.rendered.system}\n\n【用户】\n${r.rendered.user}`;
  $('promptMeta').textContent = `· ${esc(r.meta.template)} · ${r.meta.label} · 用户段 ${r.meta.user_chars} 字 · 预算 ${r.meta.budget.total} 字`;
});

// ---------------------------------------------------------------- actions
const doLLM = guard(async () => {
  const btn = $('btnLLM');
  btn.disabled = true;
  $('llmStatus').textContent = '生成中…（最长 180 秒，请勿关闭页面）';
  try {
    const r = await api('/api/narr/llm', 'POST', payload());
    $('replyBox').value = r.text;
    $('llmStatus').textContent = `${r.model} · ${r.usage.total_tokens ?? '?'} tokens · 提示词 ${r.prompt_chars} 字`;
    await doParse();
  } finally {
    btn.disabled = false;
  }
});

const doParse = guard(async () => {
  const text = $('replyBox').value.trim();
  if (!text) return toast('AI 回复为空', true);
  const r = await api('/api/narr/parse', 'POST', {text, beat: BEAT});
  PARSED = r;
  if (!r.items.length) {
    $('parseResult').innerHTML = '<span class="muted">未识别到「[00 块名] 文案」格式。审稿/梳理类模板属正常，可直接复制结果；如需应用，请把文本粘到左上块编辑器。</span>';
    return;
  }
  $('parseResult').innerHTML = `<div class="muted">解析到 ${r.parsed} 块${r.mode === 'multi' ? ` / 预期 ${r.expected}${r.complete ? ' · 完整' : ' · 不完整，可只应用已解析部分'}` : ' · 单块模式'}</div>` +
    r.items.map(x => `<div class="pr"><b>[${String(x.id).padStart(2, '0')}] ${esc(x.label || '')}</b><span>${esc(x.text)}</span></div>`).join('');
});

const doApply = guard(async () => {
  if (!PARSED || !PARSED.items.length) return toast('请先解析 AI 回复', true);
  const wasFinal = S.state.finalized;
  const r = await api('/api/narr/apply', 'POST', {items: PARSED.items, source: 'AI'});
  S = r.data; render(); schedulePrompt();
  toast(wasFinal
    ? `已应用 ${PARSED.items.length} 块；定稿已撤销，锚点页写回重新锁定`
    : `已应用 ${PARSED.items.length} 块到草稿（未写工程文件）`);
});

const saveBeat = guard(async () => {
  if (BEAT === null) return;
  const wasFinal = S.state.finalized;
  const r = await api('/api/narr/apply', 'POST', {
    items: [{id: BEAT, text: $('beatEditor').value}], source: '人工'});
  S = r.data; render(); schedulePrompt();
  toast(wasFinal ? '已保存；定稿已撤销，锚点页写回重新锁定' : '本块已保存到草稿');
});

const doSnapshot = guard(async () => {
  const label = prompt('快照备注：', '手动快照');
  if (label === null) return;
  const r = await api('/api/narr/snapshot', 'POST', {label});
  S = r.data; render();
  toast('已存快照：' + r.snapshot.name);
});

const restore = guard(async (name) => {
  if (!confirm(`回滚到快照 ${name}？当前草稿会被覆盖，且会回到未定稿状态。`)) return;
  const r = await api('/api/narr/restore', 'POST', {name});
  S = r.data; render(); schedulePrompt();
  toast('已回滚');
});

const doDiscard = guard(async () => {
  if (!confirm('放弃当前草稿？快照会保留，锚点校对台将解锁。')) return;
  await api('/api/narr/discard', 'POST', {});
  BEAT = null; PARSED = null;
  await refresh();
  toast('草稿已放弃，锚点校对台已解锁');
});

const doFinalize = guard(async () => {
  if (S.state.finalized) {
    if (!confirm('取消定稿？锚点校对台会重新锁定。')) return;
    const r = await api('/api/narr/finalize', 'POST', {value: false});
    S = r.data; render();
    return toast('已取消定稿');
  }
  $('finalModal').hidden = false;
});

const openCommands = guard(async () => {
  const r = await api('/api/narr/commands');
  $('cmdBody').textContent = r.commands.join('\n');
  $('cmdModal').hidden = false;
});

const doApplyScript = guard(async () => {
  const info = (await api('/api/narr/script_info')).info;
  if (!info.finalized) return toast('请先定稿，再写回 script.json', true);
  if (!info.aligned) return toast(`块数不一致（script ${info.script_beats} / 草稿 ${info.draft_beats}），已中止`, true);
  if (!confirm(`确认把定稿文案写回工程 json/script.json？\n\n· 只改 ${info.draft_beats} 块的 text 字段，anchor / anchor_new 等不动\n· 写前自动备份为 script.json.bak.<时间戳>\n· 这是旁白工作台唯一会修改工程文件的动作\n· 写回后必须重跑 7 步链路，锚点平移会被新 cutplan 覆盖`)) return;
  const r = await api('/api/narr/apply_script', 'POST', {});
  toast(r.data.changed ? `已写回 ${r.data.changed} 块，备份已生成` : r.data.hint);
  await refresh();
});

const doAsrTranslate = guard(async () => {
  const A = S.asr || {};
  if (!A.key_present) return toast('未检测到环境变量 DEEPSEEK_API_KEY', true);
  const resume = A.incomplete;
  const redo = A.ready;
  if (!confirm(resume
    ? `继续翻译剩余 ${A.segments - A.translated} 段？\n\n已完成的批次会跳过（断点续传），译文只写工作区。`
    : `${redo ? '已有完整译文，清除并重新' : ''}翻译 ${A.segments} 段${A.lang_name}台词为中文？\n\n· 分批调用 DeepSeek，约需 1 分钟\n· 每批结果缓存，中断可续传\n· 译文只写工作区，不改工程文件\n· 完成后旁白模板自动改用中文台词`)) return;
  const btn = $('btnAsrTranslate');
  btn.disabled = true; btn.textContent = '翻译中…（约 1 分钟）';
  try {
    const r = await api('/api/asr/translate', 'POST', {batch: 24, reset: redo});
    toast(`完成 ${r.data.translated}/${r.data.total} 段 · ${r.data.calls} 次调用 / ${r.data.tokens} tokens / ${r.data.elapsed}s`);
    if (r.data.errors.length) toast(`有 ${r.data.errors.length} 批失败，再点一次可续传`, true);
    await refresh();
  } finally {
    btn.disabled = false; btn.textContent = '译为中文（DeepSeek 分批）';
  }
});

const doAsrClear = guard(async () => {
  if (!confirm('清除中文译文与分批缓存？工程文件不受影响，旁白模板会回退到原文。')) return;
  await api('/api/asr/clear', 'POST', {});
  await refresh();
  toast('译文已清除');
});

const doRecalib = guard(async () => {
  const r = await api('/api/narr/recalibrate', 'POST', {});
  S = r.data; render(); schedulePrompt();
  toast(`已标定：有效语速 ${r.calibration.effective_cps} 字/秒`);
});

// ---------------------------------------------------------------- templates
function currentTpl() { return S.templates.find(t => t.id === $('tplSel').value) || S.templates[0]; }

function openTpl(mode) {
  const t = mode === 'new'
    ? {id: '', name: '新模板', stage: 'L1', system: '你是抖音动漫速讲编导。',
       user: '【文案规则】\n{style_rules}\n\n【剧情块骨架】\n{beats_skeleton}\n\n【补充要求】\n{user_note}'}
    : (mode === 'copy' ? {...currentTpl(), id: '', name: currentTpl().name + ' 副本', builtin: false}
                       : currentTpl());
  $('tplModal').dataset.mode = mode;
  $('tplModal').dataset.origId = t.id || '';
  $('tplName').value = t.name; $('tplStage').value = t.stage;
  $('tplSystem').value = t.system; $('tplUser').value = t.user;
  $('varHint').textContent = ['style_rules','beats_skeleton','asr_context','char_budget','current_text','beat_label','user_note'].map(x => `{${x}}`).join(' ');
  $('tplModal').hidden = false;
}

const saveTpl = guard(async () => {
  const t = {
    id: $('tplModal').dataset.origId,
    name: $('tplName').value.trim() || '未命名模板',
    stage: $('tplStage').value,
    system: $('tplSystem').value, user: $('tplUser').value,
    builtin: $('tplModal').dataset.mode === 'edit' && (currentTpl().builtin || false),
  };
  await api('/api/narr/template', 'POST', {template: t});
  $('tplModal').hidden = true;
  await refresh(); schedulePrompt();
  toast('模板已保存');
});

// ---------------------------------------------------------------- config
const openCfg = guard(async () => {
  $('cfgBase').value = S.llm.base_url;
  $('cfgTemp').value = S.llm.temperature;
  const r = await api('/api/narr/models');
  MODELS = r.models;
  $('cfgModel').innerHTML = MODELS.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
  $('cfgModel').value = MODELS.includes(S.llm.model) ? S.llm.model : (MODELS[0] || '');
  $('cfgModal').hidden = false;
});

const saveCfg = guard(async () => {
  const r = await api('/api/narr/config', 'POST', {
    base_url: $('cfgBase').value.trim(), model: $('cfgModel').value,
    temperature: parseFloat($('cfgTemp').value || '0.7')});
  $('modelSel').innerHTML = MODELS.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
  $('modelSel').value = r.llm.model;
  $('cfgModal').hidden = true;
  toast('模型配置已保存（Key 始终只读环境变量）');
});

// ---------------------------------------------------------------- bind
async function refresh() {
  S = (await api('/api/narr/state')).data;
  render();
}
function init() {
  $('btnAll').onclick = () => { BEAT = null; render(); schedulePrompt(); };
  $('tplSel').onchange = () => { render(); schedulePrompt(); };
  document.querySelectorAll('[data-v]').forEach(el => el.onchange = schedulePrompt);
  $('userNote').oninput = schedulePrompt;
  $('btnRender').onclick = doRender;
  $('btnCopy').onclick = () => {
    navigator.clipboard.writeText($('promptView').textContent);
    toast('提示词全文已复制');
  };
  $('btnLLM').onclick = doLLM;
  $('btnPaste').onclick = guard(async () => {
    const t = await navigator.clipboard.readText();
    $('replyBox').value = t; await doParse(); toast('已粘贴并解析');
  });
  $('btnParse').onclick = doParse;
  $('btnApply').onclick = doApply;
  $('btnSnapshot').onclick = doSnapshot;
  $('btnSaveBeat').onclick = saveBeat;
  $('btnDiscard').onclick = doDiscard;
  $('btnFinalize').onclick = doFinalize;
  $('btnRecalib').onclick = doRecalib;
  $('btnEditTpl').onclick = () => openTpl('edit');
  $('btnNewTpl').onclick = () => openTpl('new');
  $('btnSaveAs').onclick = () => openTpl('copy');
  $('btnConfig').onclick = openCfg;
  $('btnApplyScript').onclick = doApplyScript;
  $('btnCommands').onclick = openCommands;
  $('btnAsrTranslate').onclick = doAsrTranslate;
  $('btnAsrClear').onclick = doAsrClear;
  $('cmdClose').onclick = () => { $('cmdModal').hidden = true; };
  $('cmdCopy').onclick = () => { navigator.clipboard.writeText($('cmdBody').textContent); toast('已复制'); };
  $('cmdSave').onclick = () => {
    const text = '@echo off\nchcp 65001 >nul\n' + $('cmdBody').textContent + '\npause\n';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob(['\ufeff' + text], {type: 'text/plain;charset=utf-8'}));
    a.download = '重跑_旁白定稿后.bat';
    a.click();
    URL.revokeObjectURL(a.href);
    toast('已下载 .bat，请在工程目录自行执行');
  };
  $('cmdModal').onclick = e => { if (e.target.id === 'cmdModal') e.currentTarget.hidden = true; };
  $('tplClose').onclick = () => { $('tplModal').hidden = true; };
  $('tplSave').onclick = saveTpl;
  $('cfgClose').onclick = () => { $('cfgModal').hidden = true; };
  $('cfgSave').onclick = saveCfg;
  $('finalClose').onclick = () => { $('finalModal').hidden = true; };
  $('finalOk').onclick = guard(async () => {
    const r = await api('/api/narr/finalize', 'POST', {value: true});
    S = r.data; render(); $('finalModal').hidden = true;
    toast('已定稿，锚点校对台写回已解锁。请自行重跑 7 步链路。');
  });
  ['tplModal', 'cfgModal', 'finalModal', 'cmdModal'].forEach(id =>
    $(id).onclick = e => { if (e.target.id === id) e.currentTarget.hidden = true; });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') ['tplModal', 'cfgModal', 'finalModal', 'cmdModal'].forEach(id => $(id).hidden = true);
  });
}

init();
(async () => {
  try {
    const m = await api('/api/narr/models');
    MODELS = m.models;
    $('modelSel').innerHTML = MODELS.map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join('');
    await refresh();
    $('modelSel').value = MODELS.includes(S.llm.model) ? S.llm.model : (MODELS[0] || '');
    doRender();
  } catch (e) {
    document.querySelector('.narr-workspace').innerHTML =
      `<div class="panel" style="grid-column:1/-1;padding:24px"><h3>载入失败</h3><p>${esc(e.message)}</p></div>`;
  }
})();
