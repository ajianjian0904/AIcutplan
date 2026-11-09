/* 制作流水线 · 前端 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let S = null;            // /api/flow/state
let cur = null;          // 选中的步骤 id
let polling = null;
let jobId = null;
let seen = 0;
let logLines = [];

const STATE_TEXT = { ok: '已完成', missing: '待做', blocked: '缺源片', running: '运行中', error: '失败' };

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

/* ---------------------------------------------------------------- 载入 */
async function load() {
  try {
    S = await api('/api/flow/state');
  } catch (e) { toast(e.message, true); return; }
  renderHead();
  renderSteps();
  renderDoctor();
  if (!cur) {
    const c = S.summary && S.summary.current;
    select(c || (S.steps[0] && S.steps[0].id));
  } else {
    renderDetail();
  }
  if (S.job) {
    // 刷新页面后也要能看到上一次/正在跑的日志
    logLines = S.job.lines || [];
    seen = S.job.total || 0;
    jobId = S.job.id;
    paintLog();
  }
  syncJob(S.job);
}

function renderHead() {
  const p = S.project;
  $('#projName').textContent = p ? p.name : '未载入工程';
  if (p) $('#dirInput').value = p.dir;
  const sm = S.summary || { done: 0, total: 0 };
  $('#sumText').textContent = p ? `${sm.done}/${sm.total} 步就绪` : '先新建或载入一个工程';
  $('#progBar').style.width = sm.total ? `${(sm.done / sm.total) * 100}%` : '0';
}

function renderDoctor() {
  const d = S.doctor || {};
  const miss = [];
  if (!d.ffmpeg || !d.ffprobe) miss.push('ffmpeg');
  if (!d.faster_whisper) miss.push('faster-whisper');
  if (!d.edge_tts) miss.push('edge-tts');
  if (!d.deepseek_key) miss.push('DEEPSEEK_API_KEY');
  const el = $('#docChip');
  if (miss.length) {
    el.className = 'chip warn';
    el.textContent = `环境缺 ${miss.join(' / ')}`;
    el.title = `Python ${d.python_ver} · ${d.python}\n缺：${miss.join('、')}`;
  } else {
    el.className = 'chip ok';
    el.textContent = '环境就绪';
    el.title = `Python ${d.python_ver} · ffmpeg/ffprobe 在 PATH · whisper / edge-tts / DeepSeek Key 都可用`;
  }
}

function renderSteps() {
  const box = $('#stepList');
  box.innerHTML = '';
  if (!S.steps.length) {
    box.innerHTML = `<div class="empty">还没有工程<br><span class="faint">点右上「新建工程」，或填目录后载入</span></div>`;
    return;
  }
  let grp = '';
  S.steps.forEach((st, i) => {
    if (st.group !== grp) {
      grp = st.group;
      const g = document.createElement('div');
      g.className = 'grp';
      g.textContent = grp;
      box.appendChild(g);
    }
    const running = S.job && S.job.status === 'running' && S.job.step === st.id;
    const cls = ['step'];
    if (st.state === 'ok') cls.push('ok');
    if (st.state === 'missing' || st.state === 'blocked') cls.push('todo');
    if (running) cls.push('run');
    if (st.id === cur) cls.push('on');
    const el = document.createElement('div');
    el.className = cls.join(' ');
    el.dataset.id = st.id;
    const arts = st.artifacts.map(a =>
      `<span class="chip ${a.ok ? 'ok' : (a.opt ? '' : 'warn')}">${esc(a.label)} · ${esc(a.extra || '')}</span>`
    ).join('');
    el.innerHTML = `
      <div class="no">${running ? '•' : (st.state === 'ok' ? '✓' : i + 1)}</div>
      <div>
        <div class="nm">${esc(st.name)}${st.manual ? ' <span class="faint">· 人工</span>' : ''}</div>
        <div class="sub">${arts || '<span class="hint">尚无产物</span>'}</div>
      </div>
      <div class="rt">${running ? '<span class="chip warn">跑着</span>' : ''}</div>`;
    el.onclick = () => select(st.id);
    box.appendChild(el);
  });
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------------------------------------------------------------- 详情 */
function findStep(id) { return S.steps.find(s => s.id === id); }

function select(id) {
  cur = id;
  $$('.step').forEach(e => e.classList.toggle('on', e.dataset.id === id));
  renderDetail();
}

function renderDetail() {
  const st = findStep(cur);
  const box = $('#detailPanel');
  if (!st) { box.innerHTML = '<div class="empty">左侧点一步</div>'; return; }
  const idx = S.steps.findIndex(s => s.id === st.id) + 1;
  const arts = st.artifacts.map(a => `
    <div class="art">
      <span class="dot ${a.ok ? 'ok' : (a.opt ? '' : 'stale')}"></span>
      <span class="nowrap">${esc(a.label)}</span>
      <span class="p" title="${esc(a.path)}">${esc(a.path)}</span>
      <span class="${a.ok ? 'muted' : 'faint'} nowrap">${esc(a.extra || '')}</span>
    </div>`).join('');
  const depText = st.deps.length ? st.deps.map(d => {
    const s = findStep(d);
    return s ? `<span class="chip ${s.state === 'ok' ? 'ok' : 'warn'}">${esc(s.name.split(' · ')[0])}</span>` : '';
  }).join(' ') : '<span class="faint">无前置</span>';

  box.innerHTML = `
    <div class="panel-head">
      第 ${idx} 步
      <span class="chip ${st.state === 'ok' ? 'ok' : 'warn'}">${STATE_TEXT[st.state] || st.state}</span>
      <span class="grow"></span>
      <span class="faint mono">${esc(st.id)}</span>
    </div>
    <div class="panel-body">
      <div class="d-title">${esc(st.name)}</div>
      <div class="d-hint">${esc(st.hint)}</div>
      <div class="arts">${arts || '<span class="muted">这一步没有独立产物</span>'}</div>
      <div class="d-meta">${depText}</div>
      <div class="d-acts">
        ${st.page ? `<a class="navlink" href="${st.page}">${esc(st.run_label)} →</a>` : ''}
        ${st.manual ? '' : `<button class="primary" data-run="${st.id}">${esc(st.run_label)}</button>`}
      </div>
    </div>`;
  const b = box.querySelector('[data-run]');
  if (b) b.onclick = () => runStep(st.id);
  renderForm(st);
}

function renderForm(st) {
  const f = $('#runForm');
  const parts = [];
  (st.needs || []).forEach(k => {
    if (k === 'scene') parts.push(`<label>场景阈值<input id="pf_scene" type="number" step="0.05" min="0.05" max="0.95" value="0.30" style="width:78px"></label>`);
    if (k === 'lang') parts.push(`<label>语言<select id="pf_lang">
      <option value="ja">日语</option><option value="en">英语</option><option value="ko">韩语</option>
      <option value="zh">中文</option><option value="es">西语</option><option value="fr">法语</option></select></label>`);
    if (k === 'size') parts.push(`<label>模型<select id="pf_size">
      <option value="tiny">tiny（最快）</option><option value="small" selected>small（推荐）</option>
      <option value="medium">medium</option><option value="large-v3">large-v3（最准最慢）</option></select></label>`);
    if (k === 'redo') parts.push(`<label class="chk"><input id="pf_redo" type="checkbox"> 全部重新合成（忽略缓存）</label>`);
    if (k === 'segment') parts.push(`<label>分段时长<input id="pf_segment" type="number" step="20" min="60" max="400" value="200" style="width:74px"></label>`);
    if (k === 'frames') parts.push(`<label>每段抽帧<input id="pf_frames" type="number" min="4" max="16" value="12" style="width:64px"></label>`);
    if (k === 'force') parts.push(`<label class="chk"><input id="pf_force" type="checkbox"> 忽略缓存重解析</label>`);
  });
  if (st.manual) {
    f.innerHTML = `<div class="muted" style="font-size:11.5px">这一步必须在页面上人工完成，流水线不代跑。${st.page ? `点上面的「${esc(st.run_label)}」进入。` : ''}</div>`;
    return;
  }
  f.innerHTML = parts.join('') || '<span class="muted" style="font-size:11.5px">这一步不需要参数</span>';
}

/* ---------------------------------------------------------------- 运行 */
function collectParams() {
  const p = {};
  const g = id => $('#' + id);
  if (g('pf_scene')) p.scene = parseFloat(g('pf_scene').value || '0.3');
  if (g('pf_lang')) p.lang = g('pf_lang').value;
  if (g('pf_size')) p.size = g('pf_size').value;
  if (g('pf_redo')) p.redo = g('pf_redo').checked;
  if (g('pf_segment')) p.segment = parseFloat(g('pf_segment').value || '200');
  if (g('pf_frames')) p.frames = parseInt(g('pf_frames').value || '12', 10);
  if (g('pf_force')) p.force = g('pf_force').checked;
  return p;
}

async function runStep(id) {
  const st = findStep(id);
  if (!st) return;
  if (st.deps.some(d => (findStep(d) || {}).state !== 'ok') && !confirm('前置步骤还没就绪，仍要跑吗？')) return;
  try {
    const r = await api('/api/flow/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step: id, params: collectParams() }),
    });
    logLines = [];
    seen = 0;
    jobId = r.job.id;
    $('#logBox').textContent = '';
    syncJob(r.job);
    startPoll();
  } catch (e) { toast(e.message, true); }
}

function startPoll() {
  stopPoll();
  polling = setInterval(poll, 900);
}

function stopPoll() {
  if (polling) { clearInterval(polling); polling = null; }
}

async function poll() {
  try {
    const r = await api(`/api/flow/job?after=${seen}`);
    if (!r.job) { stopPoll(); return; }
    const j = r.job;
    if (j.id !== jobId) { jobId = j.id; seen = 0; logLines = []; }
    (j.lines || []).forEach(l => logLines.push(l));
    seen = j.total;
    paintLog();
    syncJob(j);
    if (j.status !== 'running') {
      stopPoll();
      toast(j.status === 'done' ? '完成' : `结束：${j.status}`, j.status !== 'done');
      await load();
    }
  } catch (e) { stopPoll(); toast(e.message, true); }
}

function paintLog() {
  const box = $('#logBox');
  const tail = logLines.slice(-600);
  box.innerHTML = tail.map(l => {
    const bad = /error|traceback|失败|Error|exception/i.test(l);
    return `<span class="${bad ? 'err' : ''}">${esc(l)}</span>`;
  }).join('\n');
  box.scrollTop = box.scrollHeight;
}

function syncJob(j) {
  const chip = $('#jobChip');
  const bar = $('#jobBar');
  const meta = $('#jobMeta');
  const stop = $('#btnStop');
  if (!j) {
    chip.className = 'chip'; chip.textContent = '空闲';
    bar.style.width = '0'; meta.textContent = '';
    stop.disabled = true;
    return;
  }
  const map = { running: ['warn', '运行中'], done: ['ok', '完成'], error: ['bad', '失败'], stopped: ['bad', '已停止'] };
  const [cls, txt] = map[j.status] || ['', j.status];
  chip.className = 'chip ' + cls;
  chip.textContent = `${txt} · ${j.step}`;
  stop.disabled = j.status !== 'running';
  const pct = j.status === 'done' ? 100 : (j.percent || 0);
  bar.style.width = pct + '%';
  bar.className = 'job-bar' + (j.status === 'done' ? ' done' : j.status === 'error' ? ' bad' : '');
  meta.textContent = `${fmtDur(j.elapsed)}${j.percent != null ? ` · ${j.percent}%` : ''} · ${j.total} 行日志`;
  if (j.status === 'running' && !polling) { jobId = j.id; seen = 0; startPoll(); }
}

function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`;
}

/* ---------------------------------------------------------------- 交互 */
$('#btnLoad').onclick = async () => {
  const dir = $('#dirInput').value.trim();
  if (!dir) return toast('先填工程目录', true);
  try {
    await api('/api/load', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ dir }) });
    cur = null;
    await load();
    toast('已载入');
  } catch (e) { toast(e.message, true); }
};
$('#dirInput').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btnLoad').click(); });
$('#btnRefresh').onclick = () => load();
$('#btnStop').onclick = async () => {
  try { await api('/api/flow/stop', { method: 'POST' }); toast('已请求停止'); } catch (e) { toast(e.message, true); }
};

$('#btnNew').onclick = () => { $('#newModal').hidden = false; };
$('#newClose').onclick = () => { $('#newModal').hidden = true; };
$('#nwVideo').addEventListener('dragover', e => e.preventDefault());
$('#nwVideo').addEventListener('drop', e => {
  e.preventDefault();
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  const p = f.path || '';
  if (p) $('#nwVideo').value = p.replace(/\\/g, '/');
  if (!$('#nwName').value) $('#nwName').value = f.name.replace(/\.[^.]+$/, '');
});
$('#btnCreate').onclick = async () => {
  const body = {
    dir: $('#nwDir').value.trim(), video: $('#nwVideo').value.trim(),
    name: $('#nwName').value.trim(), lang: $('#nwLang').value,
    target_dur: parseFloat($('#nwTarget').value || '324'),
    fps: parseFloat($('#nwFps').value || '24'),
    force: $('#nwForce').checked,
  };
  $('#nwTip').textContent = '正在建…';
  try {
    const r = await api('/api/flow/new', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    await api('/api/load', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ dir: r.data.dir }) });
    $('#newModal').hidden = true;
    cur = null;
    await load();
    toast(`工程已建：${r.data.dir}`);
  } catch (e) {
    $('#nwTip').textContent = e.message;
  }
};

$('#btnRebuild').onclick = async () => {
  try {
    const r = await api('/api/rebuild');
    $('#cmdBody').textContent = (r.commands || []).join('\n');
    $('#cmdModal').hidden = false;
  } catch (e) { toast(e.message, true); }
};
$('#cmdClose').onclick = () => { $('#cmdModal').hidden = true; };
$('#cmdCopy').onclick = async () => {
  try { await navigator.clipboard.writeText($('#cmdBody').textContent); toast('已复制'); }
  catch { toast('复制失败，手动选吧', true); }
};

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { $('#newModal').hidden = true; $('#cmdModal').hidden = true; }
});

load();
