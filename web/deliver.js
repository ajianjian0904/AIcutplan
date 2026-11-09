/* 交付台 · 前端 */
const $ = (s, r = document) => r.querySelector(s);

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
const mb = n => (n / 1048576).toFixed(1) + 'MB';
const dur = s => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;

let E = null;        // 导出状态
let finalOk = false;

/* ---------------------------------------------------------------- 载入 */
async function load() {
  try {
    E = await api('/api/export/state');
  } catch (e) { toast(e.message, true); return; }
  const c = E.collect || {};
  renderFiles(c);
  renderEnv(E.env || {});
  renderExports(E.exports || []);
  $('#cutCount').textContent = c.cuts || '—';
  if (!$('#exName').value) {
    $('#exName').value = ((c.project && c.project.name) || 'recap') + ' · 逐刀';
  }
  await loadFinal();
  await loadPreviews();
}

async function loadFinal() {
  // 成片规格直接从 jianying collect + preview final 信息拼，避免多开接口
  const c = (E.collect || {});
  const f = (c.files || []).find(x => x.key === 'final');
  finalOk = !!(f && f.exists);
  $('#readyChip').className = 'chip ' + (finalOk ? 'ok' : 'warn');
  $('#readyChip').textContent = finalOk ? '成片已就绪' : '成片未生成';
  if (!finalOk) {
    $('#noFinal').hidden = false;
    return;
  }
  $('#noFinal').hidden = true;
  $('#finalName').textContent = (f.path || '').split(/[\\/]/).pop();
  const box = $('#playerBox');
  if (!box.querySelector('video')) {
    box.innerHTML = `<video id="finalVideo" controls preload="metadata" src="/api/final/file"></video>`;
  }
  const probe = await fetch('/api/preview/list').then(r => r.json()).catch(() => ({}));
  $('#specBox').innerHTML = `
    <span class="chip">${c.project ? esc(c.project.name) : ''}</span>
    <span class="chip">${(c.canvas && c.canvas.width) || '?'}×${(c.canvas && c.canvas.height) || '?'}</span>
    <span class="chip">${dur(c.duration || 0)}</span>
    <span class="chip">${c.cuts} 刀</span>
    <span class="chip">${mb(f.size)}</span>
    <span class="chip info">画面流直复制，不重编码</span>`;
}

function renderFiles(c) {
  const labels = { source_video: '源片（逐刀素材）', whole_video: '无声成片', mix: '混音音轨', srt: '成片字幕', final: '成片' };
  const opt = { final: true };
  $('#filesBox').innerHTML = (c.files || []).map(f => `
    <div class="dl-item">
      <span class="dot ${f.exists ? 'ok' : (opt[f.key] ? '' : 'stale')}"></span>
      <span class="nowrap">${labels[f.key] || f.key}</span>
      <span class="p" title="${esc(f.path)}">${esc(f.path.split(/[\\/]/).slice(-1)[0])}</span>
      <span class="muted nowrap">${f.exists ? mb(f.size) : '缺失'}</span>
    </div>`).join('') +
    `<p class="dl-note" style="margin-top:8px">${c.ready ? '链路产物齐了，可以导出。' : '有产物缺失：先回「流水线」补齐切片 / 混音 / 合流。'}</p>`;
}

function renderEnv(env) {
  const chip = $('#envChip');
  if (env.worker_ok) { chip.className = 'chip ok'; chip.textContent = '剪映环境可用'; }
  else if (env.worker_python) { chip.className = 'chip warn'; chip.textContent = '有解释器但缺 worker'; }
  else { chip.className = 'chip bad'; chip.textContent = '找不到 pyJianYingDraft'; }
  chip.title = `解释器：${env.worker_python || '未找到'}\n草稿目录：${env.draft_dir || '未找到'}\n明文 ${env.plain} / 密文 ${env.encrypted}`;
}

function renderExports(list) {
  if (!list.length) { $('#exHistory').innerHTML = '<p class="dl-note">工作区还没有草稿。</p>'; return; }
  $('#exHistory').innerHTML = '<p class="dl-note" style="margin:8px 0 0">已生成的草稿：</p>' +
    list.map(x => `<div class="dl-item">
      <span class="dot ok"></span><span class="nowrap">${esc(x.name)}</span>
      <span class="p">${esc(x.at)}</span>
      <span class="chip ${x.installed ? 'ok' : ''}">${x.installed ? '已写入剪映' : '仅工作区'}</span>
    </div>`).join('');
}

/* ---------------------------------------------------------------- 预览 */
async function loadPreviews() {
  try {
    const r = await api('/api/preview/list');
    const items = r.items || [];
    $('#pvList').innerHTML = items.length
      ? '<p class="dl-note" style="margin:8px 0 0">最近渲染：</p>' + items.slice(0, 6).map(x =>
        `<div class="dl-item"><span class="dot ok"></span>
         <a href="${x.url}" target="_blank" class="p">${esc(x.key)}</a>
         <span class="muted">${mb(x.size)}</span></div>`).join('')
      : '';
  } catch { /* 列表是附加信息，失败不吵 */ }
}

$('#btnRender').onclick = async () => {
  const scope = (document.querySelector('input[name=scope]:checked') || {}).value || 'beat';
  const body = {
    scope, quality: $('#qualitySel').value,
    subs: $('#subsChk').checked, audio: $('#audioChk').checked,
    force: $('#forceChk').checked,
  };
  $('#btnRender').disabled = true;
  $('#pvStatus').textContent = '渲染中…（整块大约 10–40 秒）';
  try {
    const r = await api('/api/preview/render', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const d = r.data;
    $('#pvStatus').textContent = `${d.label || d.scope || ''} · ${dur(d.duration || 0)} · ${d.quality_name || d.quality || ''} · ${d.width}×${d.height}${d.cached ? ' · 命中缓存' : ''}${d.subs_fallback ? ' · 字幕降级为无' : ''}`;
    $('#pvBox').innerHTML = `<video src="${d.url}" controls autoplay playsinline
      style="width:100%;margin-top:8px;border-radius:8px;border:1px solid var(--line)"></video>`;
    loadPreviews();
  } catch (e) {
    $('#pvStatus').textContent = e.message;
    toast(e.message, true);
  } finally { $('#btnRender').disabled = false; }
};

/* ---------------------------------------------------------------- 导出 */
$('#exBuild').onclick = async () => {
  const mode = (document.querySelector('input[name=exmode]:checked') || {}).value || 'segments';
  const name = $('#exName').value.trim();
  if (!name) return toast('先填草稿名', true);
  $('#exBuild').disabled = true;
  $('#exHealth').innerHTML = '<p class="dl-note">生成中…（逐刀 245 段大约 10–30 秒）</p>';
  try {
    const r = await api('/api/export/build', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, name }),
    });
    const d = r.data;
    const h = d.health || {};
    $('#exHealth').innerHTML =
      `<p class="dl-note" style="margin:8px 0 0">草稿：<b>${esc(d.name)}</b> · ${d.clips} 段 · ${dur(d.duration || 0)}</p>
       <p class="dl-note mono" style="font-size:10.5px">${esc(d.folder)}</p>` +
      (h.checks || []).map(c => `<div class="dl-item"><span class="dot ok"></span><span class="p">${esc(c)}</span></div>`).join('') +
      (h.issues || []).map(c => `<div class="dl-item"><span class="dot bad"></span><span class="p">${esc(c)}</span></div>`).join('');
    $('#exPath').textContent = d.folder;
    $('#exInstall').disabled = !h.ok;
    toast(h.ok ? '草稿已生成且自检通过' : '草稿生成了，但自检有告警', !h.ok);
    await load();
  } catch (e) {
    $('#exHealth').innerHTML = `<p class="dl-note" style="color:var(--red-600)">${esc(e.message)}</p>`;
    toast(e.message, true);
  } finally { $('#exBuild').disabled = false; }
};

$('#exInstall').onclick = async () => {
  const name = $('#exName').value.trim();
  if (!name) return;
  if (!confirm(`把「${name}」写入剪映草稿目录？\n同名草稿存在时会${$('#exReplace').checked ? '先备份再覆盖' : '拒绝写入'}。`)) return;
  try {
    const r = await api('/api/export/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, replace: $('#exReplace').checked }),
    });
    toast(r.data.hint || '已写入剪映草稿目录');
    await load();
  } catch (e) { toast(e.message, true); }
};

/* ---------------------------------------------------------------- 其他 */
$('#btnRefresh').onclick = () => load();
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
  catch { toast('复制失败', true); }
};

load();
