/* 台词台 · 前端 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let D = null;
let cur = 0;
const dirty = new Map();     // i -> {orig?, zh?}
let polling = null, jobId = null, seen = 0;

function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (err ? ' err' : '');
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.hidden = true), 2500);
}
async function api(url, opt) {
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.error || `请求失败 ${r.status}`);
  return j;
}
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const tc = s => `${Math.floor(s / 60)}:${(s - Math.floor(s / 60) * 60).toFixed(2).padStart(5, '0')}`;

async function load() {
  try {
    const r = await api('/api/asr/segments');
    D = r.data;
  } catch (e) { toast(e.message, true); return; }
  dirty.clear();
  renderHead();
  renderRows();
  renderCur();
  syncDirty();
}

function renderHead() {
  const chip = $('#stChip');
  if (!D.count) { chip.className = 'chip bad'; chip.textContent = '无台词'; }
  else if (D.translated >= D.count) { chip.className = 'chip ok'; chip.textContent = `已译 ${D.count}/${D.count}`; }
  else { chip.className = 'chip warn'; chip.textContent = `已译 ${D.translated}/${D.count}`; }
  $('#cntText').textContent = D.count ? `${D.count} 段 · ${D.source_name} · 语言 ${D.lang}` : '';
  $('#srcName').textContent = D.source_name || 'asr_*.json';
  $('#asEmpty').hidden = D.count > 0;
}

function renderRows() {
  const body = $('#asBody');
  body.innerHTML = '';
  if (!D.count) return;
  const flt = $('#fltSel').value;
  const q = $('#qInput').value.trim().toLowerCase();
  D.rows.forEach(r => {
    if (flt === 'untrans' && r.translated) return;
    if (flt === 'long' && (r.t1 - r.t0) < 8) return;
    if (flt === 'edited' && !dirty.has(r.i)) return;
    if (q && !(r.orig.toLowerCase().includes(q) || (r.zh || '').toLowerCase().includes(q))) return;
    const tr = document.createElement('tr');
    tr.dataset.i = r.i;
    if (r.i === cur) tr.className = 'sel';
    tr.innerHTML = `
      <td class="mono" style="padding-top:8px">${r.i + 1}</td>
      <td class="tc">${tc(r.t0)}<br>${tc(r.t1)}</td>
      <td><div class="orig">${esc(r.orig)}</div></td>
      <td><textarea class="zh ${r.zh ? '' : 'miss'}" rows="1" data-i="${r.i}" spellcheck="false">${esc(r.zh)}</textarea></td>`;
    const ta = tr.querySelector('textarea');
    autoGrow(ta);
    ta.addEventListener('input', () => {
      autoGrow(ta);
      markDirty(r.i, 'zh', ta.value, r);
    });
    ta.addEventListener('focus', () => { selectRow(r.i); });
    tr.onclick = e => { if (e.target.tagName !== 'TEXTAREA') selectRow(r.i); };
    body.appendChild(tr);
  });
  // 字体与列宽落定后再量一次高度，避免长句被截成两行
  requestAnimationFrame(() => $$('#asBody textarea.zh').forEach(autoGrow));
}

function autoGrow(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(200, ta.scrollHeight + 2) + 'px';
}

function markDirty(i, field, val, row) {
  const orig = field === 'orig' ? row.orig : (dirty.get(i)?.orig ?? row.orig);
  const zh = field === 'zh' ? val : (dirty.get(i)?.zh ?? row.zh);
  const changed = (field === 'zh' && val !== row.zh) || (field === 'orig' && val !== row.orig);
  const cur0 = dirty.get(i) || {};
  if (changed) cur0[field] = val; else delete cur0[field];
  if (Object.keys(cur0).length) dirty.set(i, cur0); else dirty.delete(i);
  syncDirty();
  const ta = $(`textarea[data-i="${i}"]`);
  if (ta) {
    ta.classList.toggle('dirty', changed);
    if (field === 'zh') ta.classList.toggle('miss', !val.trim());
  }
}

function syncDirty() {
  $('#btnSave').disabled = dirty.size === 0;
  $('#saveTip').textContent = dirty.size ? `${dirty.size} 段待保存` : '';
  $('#btnSave').textContent = dirty.size ? `保存修改（${dirty.size}）` : '保存修改';
}

function selectRow(i) {
  cur = i;
  $$('#asBody tr').forEach(x => x.classList.toggle('sel', +x.dataset.i === i));
  renderCur();
}

function renderCur() {
  const r = D.rows.find(x => x.i === cur);
  if (!r) { $('#curIdx').textContent = '—'; $('#curBox').innerHTML = '<div class="muted">点左侧任一行</div>'; return; }
  $('#curIdx').textContent = `#${r.i + 1}`;
  const zh = dirty.get(r.i)?.zh ?? r.zh;
  const orig = dirty.get(r.i)?.orig ?? r.orig;
  $('#curBox').innerHTML = `
    <div class="quote zh">${esc(zh) || '<span class="faint">（未译）</span>'}</div>
    <div class="quote orig">${esc(orig)}</div>
    <div class="row" style="margin-top:8px">
      <span class="chip">${tc(r.t0)} – ${tc(r.t1)}</span>
      <span class="chip">${(r.t1 - r.t0).toFixed(1)}s</span>
      <button class="sm" id="btnPlayHere">看这一段（3 秒）</button>
    </div>
    <div id="curClip" style="margin-top:8px"></div>`;
  $('#btnPlayHere').onclick = () => playHere(r);
  const times = [];
  const span = r.t1 - r.t0;
  const mid = (r.t0 + r.t1) / 2;
  times.push(Math.max(0, r.t0), mid, Math.max(0, r.t1 - 0.1));
  $('#curShots').innerHTML = times.map((t, k) =>
    `<figure><img src="/api/frame?t=${t.toFixed(2)}" alt=""><figcaption>${k === 0 ? '起' : k === 1 ? '中' : '尾'}</figcaption></figure>`).join('');
}

function playHere(r) {
  const mid = ((r.t0 + r.t1) / 2).toFixed(2);
  $('#curClip').innerHTML =
    `<video src="/api/abclip?t=${mid}&tag=asr" controls autoplay muted playsinline
       style="width:100%;border-radius:6px;border:1px solid var(--line);background:#111"></video>
     <p class="as-note">源片 ${tc(r.t0)} 起 3 秒（无声，只判画面与谁在说话）</p>`;
  const v = $('#curClip video');
  v.onerror = () => { $('#curClip').innerHTML = '<p class="as-note">片段生成失败（源片缺失或 ffmpeg 忙）</p>'; };
}

/* ---------------------------------------------------------------- 保存 */
$('#btnSave').onclick = async () => {
  const changes = [...dirty.entries()].map(([i, v]) => ({ i: +i, ...v }));
  if (!changes.length) return;
  try {
    const r = await api('/api/asr/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ changes }),
    });
    D = r.data.table;
    dirty.clear();
    renderHead(); renderRows(); renderCur(); syncDirty();
    const d = r.data;
    toast(`已保存：原文 ${d.orig_changed} 处 · 译文 ${d.zh_changed} 处（旧文件已备份）`);
  } catch (e) { toast(e.message, true); }
};

/* ---------------------------------------------------------------- 译中 */
$('#btnTranslate').onclick = async () => {
  const body = { step: 'translate', params: {
    batch: parseInt($('#batchInput').value || '24', 10),
    reset: $('#resetChk').checked,
  } };
  $('#resetChk').checked = false;
  try {
    const r = await api('/api/flow/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    jobId = r.job.id; seen = 0;
    $('#jobBarWrap').hidden = false;
    $('#btnTranslate').disabled = true;
    pollJob();
    polling = setInterval(pollJob, 1000);
  } catch (e) { toast(e.message, true); }
};

async function pollJob() {
  try {
    const r = await api(`/api/flow/job?after=${seen}`);
    const j = r.job;
    if (!j) return;
    if (j.id !== jobId) { jobId = j.id; seen = 0; }
    seen = j.total;
    $('#jobText').textContent = `译中 · ${j.status === 'running' ? '进行中' : j.status === 'done' ? '完成' : '失败'} · ${j.elapsed}s`;
    $('#jobBar').style.width = (j.status === 'done' ? 100 : j.percent || 0) + '%';
    if (j.status !== 'running') {
      clearInterval(polling); polling = null;
      $('#btnTranslate').disabled = false;
      setTimeout(() => { $('#jobBarWrap').hidden = true; }, 1500);
      toast(j.status === 'done' ? '译中完成' : '译中结束（可能有失败批）', j.status !== 'done');
      await load();
    }
  } catch (e) { clearInterval(polling); polling = null; }
}

$('#btnStop').onclick = async () => {
  try { await api('/api/flow/stop', { method: 'POST' }); toast('已请求停止'); } catch (e) { toast(e.message, true); }
};
$('#btnRefresh').onclick = () => { if (dirty.size && !confirm('有未保存的修改，刷新会丢掉。继续？')) return; load(); };
$('#fltSel').onchange = () => renderRows();
$('#qInput').addEventListener('input', () => renderRows());

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); $('#btnSave').click(); }
});

load();
