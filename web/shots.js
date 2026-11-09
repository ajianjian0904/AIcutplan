/* 镜头台 · 前端 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

let T = null;          // 表格数据
let cur = 0;           // 当前镜号
let io = null;         // 缩略图懒加载

function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (err ? ' err' : '');
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.hidden = true), 2400);
}
async function api(url, opt) {
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.error || `请求失败 ${r.status}`);
  return j;
}
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const tc = s => {
  const m = Math.floor(s / 60), x = s - m * 60;
  return `${m}:${x.toFixed(2).padStart(5, '0')}`;
};
const dur = s => (s < 1 ? `${(s * 1000).toFixed(0)}ms` : `${s.toFixed(2)}s`);

/* ---------------------------------------------------------------- 载入 */
async function load() {
  try {
    const r = await api('/api/shots/table');
    T = r.data;
  } catch (e) { toast(e.message, true); return; }
  renderHead();
  renderRows();
  renderHist();
  renderCur();
}

function renderHead() {
  $('#statText').textContent = T.count
    ? `${T.count} 镜 · 中位 ${T.median}s · p90 ${T.p90}s · 最长 ${T.longest}s · 有原声 ${T.ost_count} 镜`
    : '暂无镜头表';
  $('#srcChip').className = 'chip ' + (T.src ? 'ok' : 'warn');
  $('#srcChip').textContent = T.src ? `源片 ${T.source_name}` : '找不到源片';
  $('#srcChip').title = T.src || 'project.json 里的 source_video 不存在';
  $('#validInput').value = T.valid || `0-${Math.round(T.total)}`;
  $('#shEmpty').hidden = T.count > 0;
}

function renderRows() {
  const body = $('#shBody');
  body.innerHTML = '';
  if (io) { io.disconnect(); io = null; }
  const flt = $('#fltSel').value;
  const maxDur = Math.max(4, T.longest || 4);
  let shown = 0;
  T.rows.forEach(r => {
    if (flt === 'long' && r.dur < 4) return;
    if (flt === 'short' && r.dur > 0.3) return;
    if (flt === 'ost' && !r.ost) return;
    if (flt === 'noost' && r.ost) return;
    shown++;
    const tr = document.createElement('tr');
    tr.dataset.idx = r.idx;
    if (r.idx === cur) tr.className = 'sel';
    const w = Math.min(100, (r.dur / maxDur) * 100);
    const cls = r.dur > 6 ? 'warn' : r.dur < 0.2 ? 'bad' : '';
    tr.innerHTML = `
      <td><b class="mono">${r.idx}</b></td>
      <td class="tc">${tc(r.start)} – ${tc(r.end)}</td>
      <td class="num">${dur(r.dur)}</td>
      <td><div class="bar"><i class="${cls}" style="width:${w}%"></i></div></td>
      <td>${r.ost ? '<span class="chip ok">有</span>' : '<span class="chip">无</span>'}</td>
      <td><img class="thumb" data-t="${((r.start + r.end) / 2).toFixed(2)}" alt=""></td>
      <td>
        <div class="ops">
          <button data-act="merge" title="与下一镜合并">合</button>
          <button data-act="delete" title="删掉这一镜（时间并入相邻镜）">删</button>
          <button data-act="split" title="在这一镜中间切一刀">切</button>
          <button data-act="ost" title="切换「有原声」标记">原声</button>
        </div>
      </td>`;
    tr.onclick = e => {
      if (e.target.closest('button')) return;
      cur = r.idx;
      $$('#shBody tr').forEach(x => x.classList.toggle('sel', +x.dataset.idx === cur));
      renderCur();
    };
    tr.querySelectorAll('button[data-act]').forEach(b => {
      b.onclick = e => { e.stopPropagation(); act(b.dataset.act, r); };
    });
    tr.querySelector('.thumb').onclick = e => { e.stopPropagation(); openFrame(r); };
    body.appendChild(tr);
  });

  if ($('#thumbChk').checked) observeThumbs();
  $('#actTip').textContent = shown !== T.count ? `筛选出 ${shown} / ${T.count} 镜` : '';
}

function observeThumbs() {
  io = new IntersectionObserver(entries => {
    entries.forEach(en => {
      if (!en.isIntersecting) return;
      const img = en.target;
      io.unobserve(img);
      img.src = `/api/frame?t=${img.dataset.t}`;
    });
  }, { root: $('#shScroll'), rootMargin: '300px' });
  $$('#shBody .thumb').forEach(img => io.observe(img));
}

/* ---------------------------------------------------------------- 编辑 */
async function act(action, r) {
  let at = null;
  if (action === 'split') {
    const v = prompt(`在第 ${r.idx} 镜内哪一秒切？（${r.start.toFixed(2)} – ${r.end.toFixed(2)}）`,
      ((r.start + r.end) / 2).toFixed(2));
    if (v == null) return;
    at = parseFloat(v);
    if (!isFinite(at)) return toast('时间不对', true);
  }
  if (action === 'delete' && !confirm(`删除第 ${r.idx} 镜？时间会并入相邻镜，总长不变。`)) return;
  try {
    const res = await api('/api/shots/act', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, idx: r.idx, at }),
    });
    T = res.data.table;
    if (action === 'delete') cur = Math.max(1, r.idx - 1);
    renderHead(); renderRows(); renderHist(); renderCur();
    toast('已更新镜头表');
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- 侧栏 */
function renderCur() {
  const r = T.rows.find(x => x.idx === cur);
  const box = $('#curBox');
  if (!r) { $('#curIdx').textContent = '—'; box.innerHTML = '<div class="muted">点左侧任一行为当前镜头</div>'; return; }
  $('#curIdx').textContent = `#${r.idx}`;
  const pos = T.rows.findIndex(x => x.idx === r.idx);
  const nb = [];
  for (let k = Math.max(0, pos - 2); k <= Math.min(T.rows.length - 1, pos + 2); k++) {
    const x = T.rows[k];
    nb.push(`<figure><img src="/api/frame?t=${((x.start + x.end) / 2).toFixed(2)}" alt=""><figcaption>${x.idx}</figcaption></figure>`);
  }
  box.innerHTML = `
    <img class="thumb big" id="curThumb" src="/api/frame?t=${((r.start + r.end) / 2).toFixed(2)}" alt="">
    <div class="cur-grid" style="margin-top:8px">
      <div><span>起点</span><b class="mono">${tc(r.start)}</b></div>
      <div><span>终点</span><b class="mono">${tc(r.end)}</b></div>
      <div><span>时长</span><b class="mono">${dur(r.dur)}</b></div>
      <div><span>原声</span>${r.ost ? '<span class="chip ok">有</span>' : '<span class="chip">无</span>'}</div>
    </div>
    <div class="nb">${nb.join('')}</div>
    <div class="row" style="margin-top:9px">
      <button class="sm" id="btnNudge">挪到中间</button>
      <button class="sm" id="btnSplit">中间切一刀</button>
      <button class="sm danger" id="btnDel">删这一镜</button>
    </div>
    <p class="sh-note" style="margin-top:8px">相邻镜预览带镜号；挪边界只在相邻两镜之间生效。</p>`;
  $('#btnNudge').onclick = () => nudge(r);
  $('#btnSplit').onclick = () => act('split', r);
  $('#btnDel').onclick = () => act('delete', r);
}

async function nudge(r) {
  const nx = T.rows.find(x => x.idx === r.idx + 1);
  if (!nx) return toast('最后一镜没有下边界', true);
  const mid = ((r.start + nx.end) / 2).toFixed(2);
  const v = prompt(`把 #${r.idx} 与 #${r.idx + 1} 的边界挪到哪一秒？（${r.start.toFixed(2)} – ${nx.end.toFixed(2)}）`, mid);
  if (v == null) return;
  const at = parseFloat(v);
  if (!isFinite(at)) return toast('时间不对', true);
  try {
    const res = await api('/api/shots/act', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'nudge', idx: r.idx, at }),
    });
    T = res.data.table;
    renderHead(); renderRows(); renderCur();
    toast('边界已挪');
  } catch (e) { toast(e.message, true); }
}

function openFrame(r) {
  const w = window.open('', '_blank', 'width=980,height=680');
  if (!w) return;
  w.document.write(`<title>镜头 ${r.idx}</title><body style="margin:0;background:#111;color:#ddd;font:13px system-ui">
  <div style="padding:8px 12px">#${r.idx} · ${tc(r.start)} – ${tc(r.end)} · ${dur(r.dur)}</div>
  <img src="/api/frame?t=${((r.start + r.end) / 2).toFixed(2)}" style="width:100%;display:block">
  </body>`);
}

function renderHist() {
  const h = T.hist || {};
  const keys = ['0-0.5', '0.5-1', '1-2', '2-3', '3-4', '4-99'];
  const max = Math.max(1, ...keys.map(k => h[k] || 0));
  $('#histBox').innerHTML = `<div class="hist">${keys.map(k => `
    <div class="h"><b>${k}s</b><div class="t"><i style="width:${((h[k] || 0) / max) * 100}%"></i></div><b>${(h[k] || 0).toFixed(1)}%</b></div>
  `).join('')}</div>
  <p class="sh-note" style="margin-top:8px">碎镜（≤0.3s）多了说明场景阈值偏低；长镜（≥6s）多说明漏切。</p>`;
}

/* ---------------------------------------------------------------- 交互 */
$('#btnRefresh').onclick = () => load();
$('#thumbChk').onchange = () => renderRows();
$('#fltSel').onchange = () => renderRows();
$('#qInput').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  const n = parseInt($('#qInput').value, 10);
  if (!isFinite(n)) return;
  const r = T.rows.find(x => x.idx === n);
  if (!r) return toast('没有这一镜', true);
  cur = n;
  $$('#shBody tr').forEach(x => x.classList.toggle('sel', +x.dataset.idx === cur));
  const tr = $$('#shBody tr').find(x => +x.dataset.idx === n);
  if (tr) tr.scrollIntoView({ block: 'center', behavior: 'smooth' });
  renderCur();
});
$('#btnValid').onclick = async () => {
  try {
    const res = await api('/api/shots/act', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'valid', valid: $('#validInput').value.trim() }),
    });
    T = res.data.table;
    toast('有效段已保存');
  } catch (e) { toast(e.message, true); }
};
$('#btnQuickValid').onclick = () => {
  const trs = $$('#shBody tr');
  if (!trs.length) return;
  const first = T.rows.find(x => x.idx === +trs[0].dataset.idx);
  const last = T.rows.find(x => x.idx === +trs[trs.length - 1].dataset.idx);
  $('#validInput').value = `${Math.round(first.start)}-${Math.round(last.end)}`;
};

load();
