/* 剧情台 · 前端 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let D = null;          // /api/story/plan 返回
let blocks = [];       // 工作副本
let cur = -1;
let dirty = false;
let polling = null, jobId = null, seen = 0, logLines = [];

const TYPE_CLS = { '钩子': 'hook', '铺垫': 'setup', '冲突': 'clash', '反转': 'twist', '爽点': 'peak', '收尾': 'setup', '收录': 'skip' };
const TYPE_COLOR = { hook: '#F0997B', setup: '#B5D4F4', clash: '#F09595', twist: '#AFA9EC', peak: '#5DCAA5', skip: '#D3D1D7' };

function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (err ? ' err' : '');
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.hidden = true), 2600);
}
async function api(url, opt) {
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.error || `请求失败 ${r.status}`);
  return j;
}
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const tc = s => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;

/* ---------------------------------------------------------------- 载入 */
async function load() {
  try {
    D = await api('/api/story/plan');
  } catch (e) { toast(e.message, true); return; }
  blocks = (D.blocks || []).map(b => ({ ...b }));
  if (cur < 0 && blocks.length) cur = 0;
  if (cur >= blocks.length) cur = blocks.length - 1;
  renderHead();
  renderBlocks();
  renderTimeline();
  renderCur();
  renderBudget();
  dirty = false;
  syncSave();
}

function renderHead() {
  const p = D.project || {};
  $('#projName').textContent = p.name || '';
  const chip = $('#planChip');
  const n = (D.blocks || []).length;
  if (!n) { chip.className = 'chip warn'; chip.textContent = '无骨架'; }
  else {
    const ev = (D.events || []).length;
    chip.className = 'chip ok';
    chip.textContent = `${n} 块 · ${ev} 事件 · 目标 ${Math.round(D.cfg.target)}s`;
  }
  $('#segInput').value = D.cfg.seg_sec || 200;
  $('#frameInput').value = D.cfg.frames || 12;
}

function renderBlocks() {
  const body = $('#blkBody');
  body.innerHTML = '';
  $('#blkEmpty').hidden = blocks.length > 0;
  const flt = $('#typeFlt').value;
  blocks.forEach((b, i) => {
    if (flt === 'live' && b.type === '收录') return;
    if (flt === 'skip' && b.type !== '收录') return;
    const tr = document.createElement('tr');
    tr.dataset.i = i;
    tr.className = (i === cur ? 'sel ' : '') + (b.type === '收录' ? 'skip' : '');
    tr.innerHTML = `
      <td class="mono">${i + 1}</td>
      <td><input class="cell" data-f="label" value="${esc(b.label)}" spellcheck="false"></td>
      <td>
        <select class="cell" data-f="type">
          ${['钩子', '铺垫', '冲突', '反转', '爽点', '收尾', '收录'].map(t =>
            `<option ${t === b.type ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
      </td>
      <td><div class="tc-pair">
        <input class="cell num" data-f="t0" type="number" step="0.5" value="${b.t0}"><i>–</i><input class="cell num" data-f="t1" type="number" step="0.5" value="${b.t1}">
      </div></td>
      <td><input class="cell num" data-f="target" type="number" step="0.5" value="${b.target}" style="width:52px"></td>
      <td>
        <div style="display:flex;align-items:flex-start;gap:6px">
          <input class="cell" data-f="summary" value="${esc(b.summary)}" spellcheck="false">
          <span class="ops">
            <button data-act="merge" title="与下一块合并">合</button>
            <button data-act="del" title="删除这一块">删</button>
          </span>
        </div>
      </td>`;
    tr.onclick = e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.closest('button')) return;
      cur = i; renderBlocks(); renderCur();
    };
    tr.querySelectorAll('input[data-f],select[data-f]').forEach(inp => {
      inp.oninput = () => {
        const f = inp.dataset.f;
        const v = inp.type === 'number' || inp.tagName === 'SELECT' ? inp.value : inp.value;
        blocks[i][f] = (f === 't0' || f === 't1' || f === 'target') ? parseFloat(v || '0') : v;
        if (f === 'type') { tr.className = (i === cur ? 'sel ' : '') + (v === '收录' ? 'skip' : ''); }
        dirty = true; syncSave(); renderBudget(); renderTimeline();
        if (i === cur) renderCur();
      };
      inp.onchange = () => { if (inp.dataset.f === 'type') renderBlocks(); };
      inp.onfocus = () => { if (cur !== i) { cur = i; renderCur(); } };
    });
    tr.querySelectorAll('button[data-act]').forEach(b2 => {
      b2.onclick = e => { e.stopPropagation(); act(b2.dataset.act, i); };
    });
    body.appendChild(tr);
  });
  const live = blocks.filter(b => b.type !== '收录');
  $('#blkCount').textContent = `${blocks.length} 块（正片 ${live.length}）`;
  const tot = live.reduce((s, b) => s + (+b.target || 0), 0);
  $('#sumText').textContent = `正片合计 ${tot.toFixed(1)}s / 目标 ${D.cfg.target}s（${(tot - D.cfg.target) >= 0 ? '+' : ''}${(tot - D.cfg.target).toFixed(1)}s）`;
}

function act(kind, i) {
  const b = blocks[i];
  if (kind === 'del') {
    if (!confirm(`删除第 ${i + 1} 块「${b.label}」？`)) return;
    blocks.splice(i, 1);
    cur = Math.min(cur, blocks.length - 1);
  } else if (kind === 'merge') {
    const nx = blocks[i + 1];
    if (!nx) return toast('已经是最后一块', true);
    b.t1 = nx.t1;
    b.target = Math.round((b.target + nx.target) * 10) / 10;
    b.summary = (b.summary + '；' + nx.summary).slice(0, 80);
    if (b.type === '收录' && nx.type !== '收录') b.type = nx.type;
    blocks.splice(i + 1, 1);
  }
  dirty = true;
  renderBlocks(); renderTimeline(); renderCur(); syncSave(); renderBudget();
}

function syncSave() {
  $('#btnSave').disabled = !dirty;
  $('#btnSave').textContent = dirty ? '保存修改 •' : '保存修改';
}

/* ---------------------------------------------------------------- 时间轴 */
function renderTimeline() {
  const total = D.cfg.src || 1;
  const track = $('#tlTrack');
  track.innerHTML = blocks.map((b, i) => {
    const l = (b.t0 / total) * 100;
    const w = Math.max(0.4, ((b.t1 - b.t0) / total) * 100);
    const c = TYPE_COLOR[TYPE_CLS[b.type] || 'setup'];
    const showLabel = w > 4.2;
    return `<div class="blk ${i === cur ? 'sel' : ''}" data-i="${i}" title="${esc(b.label)} · ${tc(b.t0)}–${tc(b.t1)} · ${b.target}s"
      style="left:${l}%;width:${w}%;background:${c}22;border-left:2px solid ${c}">
      ${showLabel ? `<b>${esc(b.label)}</b>` : ''}</div>`;
  }).join('');
  $$('#tlTrack .blk').forEach(el => {
    el.onclick = () => { cur = +el.dataset.i; renderBlocks(); renderTimeline(); renderCur(); };
  });
  const evs = (D.events || []).map(e => `<i style="left:${(e.t0 / total) * 100}%"></i>`).join('');
  $('#tlEvents').innerHTML = evs;
  const mins = total / 60;
  const step = mins > 18 ? 4 : mins > 9 ? 2 : 1;
  const ticks = [];
  for (let m = 0; m <= mins + 0.01; m += step) {
    ticks.push(`<span style="left:${((m * 60) / total) * 100}%">${m}′</span>`);
  }
  $('#tlTicks').innerHTML = ticks.join('');
}

/* ---------------------------------------------------------------- 当前块 */
async function renderCur() {
  const b = blocks[cur];
  if (!b) { $('#curLabel').textContent = '—'; $('#curBox').innerHTML = '<div class="muted">点左侧任一块</div>'; return; }
  $('#curLabel').textContent = b.label;
  const mid = (b.t0 + b.t1) / 2;
  const times = [b.t0 + (b.t1 - b.t0) * 0.2, mid, b.t0 + (b.t1 - b.t0) * 0.8];
  $('#curBox').innerHTML = `
    <div class="cur-grid">
      <div><span>源片</span><b class="mono">${tc(b.t0)} – ${tc(b.t1)}</b></div>
      <div><span>成片</span><b class="mono">${(+b.target).toFixed(1)}s</b></div>
      <div><span>性质</span>${esc(b.type)}</div>
      <div><span>刀数</span><b class="mono">${Math.round(b.target / 1.32)}</b></div>
    </div>
    <div class="frames">${times.map(t =>
      `<img src="/api/frame?t=${t.toFixed(2)}" alt="" title="${tc(t)}">`).join('')}</div>
    <p class="st-note">${esc(b.summary) || '<span class="faint">（无概要）</span>'}</p>
    <div class="panel-sub" style="font-size:11px;color:var(--muted);margin:6px 0 4px">这块区间里的台词</div>
    <div class="q-list" id="curLines"><div class="muted">载入中…</div></div>`;
  try {
    const r = await api(`/api/story/lines?t0=${b.t0}&t1=${b.t1}&limit=40`);
    const lines = r.lines || [];
    $('#curLines').innerHTML = lines.length
      ? lines.map(l => `<div><span class="t">${l.t0.toFixed(0)}s</span><span>${esc(l.text)}</span></div>`).join('')
      : '<div class="muted">这块没有台词（过场或纯动作）</div>';
  } catch { $('#curLines').innerHTML = '<div class="muted">台词读取失败</div>'; }
}

/* ---------------------------------------------------------------- 预算 */
function renderBudget() {
  const cps = (D.cfg.cps || 4.95), cov = 0.938;
  const live = blocks.filter(b => b.type !== '收录');
  const sec = live.reduce((s, b) => s + (+b.target || 0), 0);
  const chars = Math.round(sec * cov * cps);
  const target = D.cfg.target || 324;
  const diff = sec - target;
  const pct = Math.min(100, (sec / target) * 100);
  const ok = Math.abs(diff) <= target * 0.03;
  $('#budgetBox').innerHTML = `
    <div class="bg-row ${ok ? 'ok' : 'warn'}"><span>正片时长</span><b>${sec.toFixed(1)}s / ${target}s</b></div>
    <div class="bg-bar"><i class="${ok ? '' : 'warn'}" style="width:${pct}%"></i></div>
    <div class="bg-row ${ok ? 'ok' : 'warn'}"><span>偏差</span><b>${diff >= 0 ? '+' : ''}${diff.toFixed(1)}s</b></div>
    <div class="bg-row"><span>旁白预算</span><b>${chars} 字</b></div>
    <div class="bg-row"><span>语速 / 覆盖</span><b>${cps.toFixed(2)} 字/秒 · ${(cov * 100).toFixed(0)}%</b></div>
    <div class="bg-list">${live.map(b =>
      `<div><span>${esc(b.label)}</span><b class="mono">${(+b.target).toFixed(0)}s · ${Math.round(b.target * cov * cps)}字</b></div>`).join('')}</div>
    <p class="st-hint">偏差超 3% 时，对齐脚本会把块压/拉回目标；先在这里调平更省事。</p>`;
}

/* ---------------------------------------------------------------- 保存 / 落骨架 */
$('#btnSave').onclick = async () => {
  try {
    const r = await api('/api/story/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ blocks }),
    });
    D.blocks = r.blocks; D.budget = r.budget;
    blocks = r.blocks.map(b => ({ ...b }));
    dirty = false; syncSave();
    renderBlocks(); renderTimeline(); renderBudget();
    toast(`已保存 ${blocks.length} 块`);
  } catch (e) { toast(e.message, true); }
};

$('#btnAdd').onclick = () => {
  const at = cur >= 0 ? cur + 1 : blocks.length;
  const prev = blocks[at - 1];
  const t0 = prev ? prev.t1 : 0;
  blocks.splice(at, 0, { label: '新块', type: '铺垫', t0, t1: t0 + 30, target: 10, summary: '' });
  cur = at; dirty = true;
  renderBlocks(); renderTimeline(); renderCur(); renderBudget(); syncSave();
};

$('#btnScript').onclick = async () => {
  const ow = $('#ovChk').checked;
  if (ow && !confirm('覆盖已有 script.json？会先备份，但旁白台里未定稿的草稿会失去对应块。')) return;
  try {
    const r = await api('/api/story/build', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ what: 'script', overwrite: ow }),
    });
    const d = r.data;
    toast(d.written ? `已写 ${d.blocks} 块骨架 · ${d.duration}s` : (d.reason || '未写入'), !d.written);
    $('#opTip').textContent = d.written ? '骨架已写，去旁白台逐块生成文案' : (d.reason || '');
  } catch (e) { toast(e.message, true); }
};

$('#btnCutplan').onclick = async () => {
  if (!confirm('生成初版 cutplan？会覆盖 json/cutplan.json（自动备份），之后要重跑对齐/切片。')) return;
  try {
    const r = await api('/api/story/build', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ what: 'cutplan' }),
    });
    const d = r.data;
    toast(`cutplan 初版：${d.cuts} 刀 · ${d.duration}s`);
    $('#opTip').textContent = `已生成 ${d.cuts} 刀；下一步去流水线跑「旁白落稿」`;
  } catch (e) { toast(e.message, true); }
};

/* ---------------------------------------------------------------- 跑解析 */
async function runJob(params) {
  try {
    const r = await api('/api/flow/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step: 'story', params }),
    });
    jobId = r.job.id; seen = 0; logLines = [];
    $('#logBox').textContent = '';
    syncJob(r.job);
    polling = setInterval(poll, 1200);
  } catch (e) { toast(e.message, true); }
}

$('#btnParse').onclick = () => runJob({
  segment: parseFloat($('#segInput').value || '200'),
  frames: parseInt($('#frameInput').value || '12', 10),
  force: $('#forceChk').checked,
});
$('#btnCompose').onclick = () => {
  if (blocks.length && !confirm('重新合成会用 AI 结果覆盖当前块表（包括你手改的边界与秒数）。\n事件表不动，只重跑「事件 → 块」这一步。继续？')) return;
  runJob({ compose_only: true });
};

async function poll() {
  try {
    const r = await api(`/api/flow/job?after=${seen}`);
    const j = r.job;
    if (!j) { clearInterval(polling); polling = null; return; }
    if (j.id !== jobId) { jobId = j.id; seen = 0; logLines = []; }
    (j.lines || []).forEach(l => logLines.push(l));
    seen = j.total;
    paintLog();
    syncJob(j);
    if (j.status !== 'running') {
      clearInterval(polling); polling = null;
      toast(j.status === 'done' ? '解析完成' : `结束：${j.status}`, j.status !== 'done');
      await load();
    }
  } catch (e) { clearInterval(polling); polling = null; }
}

function paintLog() {
  $('#logBox').innerHTML = logLines.slice(-400).map(l =>
    `<span class="${/error|失败|Traceback|Error/i.test(l) ? 'err' : ''}">${esc(l)}</span>`).join('\n');
  $('#logBox').scrollTop = $('#logBox').scrollHeight;
}

function syncJob(j) {
  const chip = $('#jobChip'), stop = $('#btnStop');
  if (!j) { chip.className = 'chip'; chip.textContent = '空闲'; stop.disabled = true; return; }
  const map = { running: ['warn', '解析中'], done: ['ok', '完成'], error: ['bad', '失败'], stopped: ['bad', '已停止'] };
  const [cls, txt] = map[j.status] || ['', j.status];
  chip.className = 'chip ' + cls;
  chip.textContent = `${txt} · ${j.elapsed}s${j.percent != null ? ` · ${j.percent}%` : ''}`;
  stop.disabled = j.status !== 'running';
  if (j.status === 'running' && !polling) { jobId = j.id; seen = 0; polling = setInterval(poll, 1200); }
}

$('#btnStop').onclick = async () => {
  try { await api('/api/flow/stop', { method: 'POST' }); toast('已请求停止'); } catch (e) { toast(e.message, true); }
};
$('#typeFlt').onchange = () => renderBlocks();
$('#btnRefresh').onclick = () => { if (dirty && !confirm('有未保存的修改，刷新会丢掉')) return; load(); };

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); if (dirty) $('#btnSave').click(); }
});

load();
