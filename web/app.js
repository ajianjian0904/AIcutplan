/* 锚点校对台 · 无构建前端 */
'use strict';

let S = null;                 // 后端 state
let SHOTS = [];
let SENTS = [];               // 当前块的旁白句（含 wins / 此刻台词）
let SEL_SENT = null;          // 选中的句 id
let SENT_VIEW = null;         // 选中句详情
let ANC_CENTER = null;        // 句级编辑弹窗的拼板中心
let PV_LAST = null;
let sheetKey = '';
let toastTimer = null;
let drag = null;

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
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}
const guard = fn => async (...a) => {
  try { await fn(...a); } catch (e) { toast(e.message, true); }
};

// 关闭弹窗时必须显式暂停视频：hidden 掉的 <video autoplay loop> 仍会继续解码/出声
function closeModal(id) {
  const box = $(id);
  if (!box) return;
  box.querySelectorAll('video').forEach(v => {
    try {
      v.pause();
      v.removeAttribute('autoplay');
      v.removeAttribute('loop');
    } catch (e) { /* 已经卸载的元素忽略 */ }
  });
  box.hidden = true;
}

function cur() {
  if (!S) return null;
  return S.beats.find(b => b.id === S.current) || S.beats[0] || null;
}

// ---------------------------------------------------------------- render
function render() {
  if (!S) return;
  const p = S.project, b = cur();
  $('projName').textContent = p.name;
  $('projMeta').textContent = `${p.out_dur}s 成片 · ${p.windows} 刀 · ${p.beats} 块 · ${p.shots} 镜头`;
  $('dirInput').value = p.dir;
  $('beatCount').textContent = `共 ${p.beats} 块`;
  $('srcDur').textContent = `${p.src_dur}s`;
  $('outMeta').textContent = `${p.out_dur}s / ${p.windows} 刀`;
  $('btnSave').disabled = !S.dirty || !!S.narr_locked;
  $('btnSave').title = S.narr_locked ? '旁白未定稿，锚点写回已锁定' : '';
  $('btnSave').textContent = S.dirty ? '写回 *' : '写回';

  $('beatList').innerHTML = S.beats.map(x => {
    const done = S.confirmed.includes(x.id);
    const moved = Math.abs(x.delta) > 1e-6;
    const dot = moved ? 'moved' : (done ? 'done' : '');
    return `<div class="beat ${x.id === S.current ? 'current' : ''}" data-b="${x.id}">
      <span class="beat-id">${String(x.id).padStart(2, '0')}</span>
      <span class="beat-label">${esc(x.label)}</span>
      <span class="beat-cuts">${x.cuts}刀</span>
      <span class="dot ${dot}"></span>
    </div>`;
  }).join('');
  $('beatList').querySelectorAll('.beat').forEach(el =>
    el.onclick = () => setCurrent(+el.dataset.b));

  renderTimeline();
  renderDetail(b);
  renderStrip();
  loadSheet();
  loadSentences();
  loadDialogueTrack();
  renderSentMarks();
}

const ZOOM_SPAN = 80;   // 局部轨窗口：原始起点 ±40s

function positionBlocks(a, z) {
  const p = S.project;
  const blk = $('tlBlock');
  blk.style.left = (a / p.src_dur * 100) + '%';
  blk.style.width = Math.max(0.5, (z - a) / p.src_dur * 100) + '%';
  const center = cur().src[0];       // 窗口中心固定为磁盘上的原始起点
  const left = (a - (center - ZOOM_SPAN / 2)) / ZOOM_SPAN * 100;
  $('tlZoomBlock').style.left = Math.max(-4, Math.min(104, left)) + '%';
}

function renderTimeline() {
  if (!S || !cur()) return;
  const p = S.project, b = cur(), d = b.delta;
  const a = b.src[0] + d, z = b.src[1] + d;
  positionBlocks(a, z);
  const moved = Math.abs(d) > 1e-6;
  $('tlBlock').classList.toggle('pending', moved);
  $('tlZoomBlock').classList.toggle('pending', moved);
  $('tlTicks').innerHTML = Array.from({length: 14}, (_, i) =>
    `<i style="left:${(i * 100 / p.src_dur * 100).toFixed(3)}%"></i>`).join('');
  const total = S.applied[S.current] || 0;
  $('tlLabel').textContent =
    `${a.toFixed(2)} → ${z.toFixed(2)}s　·　${b.cuts} 刀　·　本次 ${d >= 0 ? '+' : ''}${d.toFixed(2)}s` +
    (Math.abs(total) > 1e-6 ? `　·　累计已写回 ${total >= 0 ? '+' : ''}${total.toFixed(2)}s` : '');
}

function renderDetail(b) {
  if (!b) return;
  $('detailHead').textContent = `当前块 · ${String(b.id).padStart(2, '0')} ${b.label}`;
  const good = b.sec_per_cut >= 1.20 && b.sec_per_cut <= 1.40;
  $('metrics').innerHTML = `
    <div class="metric"><span>旁白 / 刀数</span><b>${b.sents} 句 / ${b.cuts} 刀</b></div>
    <div class="metric ${good ? 'good' : ''}"><span>秒 / 刀</span><b>${b.sec_per_cut.toFixed(3)}</b></div>
    <div class="metric"><span>源片区间</span><b>${b.src_shifted[0].toFixed(1)} – ${b.src_shifted[1].toFixed(1)}</b></div>
    <div class="metric"><span>成片区间</span><b>${b.new[0].toFixed(1)} – ${b.new[1].toFixed(1)}</b></div>`;
  const n = $('notice');
  const moved = Math.abs(b.delta) > 1e-6;
  const ns = S.sent_count || 0;
  n.className = 'notice' + ((moved || S.narr_locked || ns) ? ' warn' : '');
  n.textContent = S.narr_locked
    ? '旁白尚未定稿：锚点写回已锁定。请先到「旁白工作台」定稿，并重跑 7 步链路后再校对。'
    : (ns
      ? `待写回：${ns} 句使用了句级锚点${moved ? `，另有整块平移 ${b.delta > 0 ? '+' : ''}${b.delta.toFixed(2)}s` : ''}。句级优先，只改本句那几刀。`
      : (moved
        ? `待写回：整块平移 ${b.delta > 0 ? '+' : ''}${b.delta.toFixed(2)}s。刀数／成片时长／旁白均不变。`
        : '块级 = 整块平移；点旁白句可只读联动，并可编辑本句首/尾镜。刀数与成片时长锁死。'));
}

function renderStrip() {
  if (!S) return;
  const total = S.project.out_dur || 1;
  $('strip').innerHTML = S.beats.map(b => {
    const w = Math.max(0.6, (b.new[1] - b.new[0]) / total * 100);
    const cls = b.id === S.current ? 'current'
      : (Math.abs(b.delta) > 1e-6 ? 'moved' : (S.confirmed.includes(b.id) ? 'done' : ''));
    return `<div class="${cls}" style="width:${w}%" title="${esc(b.label)} · ${b.cuts}刀"></div>`;
  }).join('');
}

async function loadSheet(force, center) {
  if (!S || !cur()) return;
  const c = (center === undefined || center === null) ? cur().src_shifted[0] : center;
  const key = `${S.current}|${c.toFixed(2)}|${(cur().delta || 0).toFixed(2)}`;
  if (!force && key === sheetKey) return;
  sheetKey = key;
  $('sheet').innerHTML = '<span class="muted">抽帧中…</span>';
  try {
    const r = await api(`/api/shots?beat=${S.current}&center=${c.toFixed(2)}`);
    SHOTS = r.shots;
    $('sheet').innerHTML = r.shots.map(s => `
      <div class="cell ${s.active ? 'active' : ''}" data-idx="${s.idx}"
           title="镜头 ${s.idx} · ${s.start}s–${s.end}s&#10;此刻台词：${esc(s.line || '（无）')}">
        <img loading="lazy" src="${s.thumb}" alt="${s.idx}">
        <span class="num">${s.idx}</span>
        ${s.ost ? '<span class="ost" title="原声段"></span>' : ''}
      </div>`).join('');
    $('sheet').querySelectorAll('.cell').forEach(el => {
      el.onclick = () => jump(+el.dataset.idx);
      el.onmouseenter = () => showShotLine(SHOTS.find(x => x.idx === +el.dataset.idx));
    });
    $('sheet').onmouseleave = () => defaultShotLine();
    $('sheetHint').textContent =
      `显示 ${r.center}s 附近 ${r.shots.length} 个镜头 · 原声段（红标）不由机器选 · 台词${r.zh_ready ? '已译中' : '为原文'}`;
  } catch (e) {
    $('sheet').innerHTML = '<span class="muted">抽帧失败：' + esc(e.message) + '</span>';
  }
}

function showShotLine(s) {
  if (!s) return;
  $('shotLine').innerHTML =
    `<span class="lbl">镜头 ${s.idx} · ${s.start.toFixed(1)}–${s.end.toFixed(1)}s</span>` +
    (s.line ? `此刻台词：<b>${esc(s.line)}</b>` + (s.line_orig ? ` <span class="muted">（原：${esc(s.line_orig)}）</span>` : '')
            : '<span class="muted">此刻无人物台词</span>');
}
function defaultShotLine() {
  $('shotLine').textContent = '把鼠标移到格子上，看这个镜头里人物在说什么';
}

// ------------------------------------------------ 句画联动（只读） + 台词轨
async function loadSentences() {
  if (!S) return;
  try {
    const r = await api('/api/sentences?beat=' + S.current);
    SENTS = r.sentences;
    SEL_SENT = (r.selected !== null && SENTS.some(x => x.id === r.selected)) ? r.selected : null;
    $('sentList').innerHTML = SENTS.length ? SENTS.map(s => `
      <div class="sent ${s.id === SEL_SENT ? 'sel' : ''} ${s.edited ? 'edited' : ''}"
           data-s="${s.id}" title="刀 ${s.wins.join(', ')} · 源片 ${s.src[0]}–${s.src[1]}s">
        <div class="s-main">
          <span class="s-txt">${esc(s.text)}</span>
          <span class="s-ln">${s.line ? '台词：' + esc(s.line) : '此刀无台词'}</span>
        </div>
        <span class="s-cut">${s.cuts}刀<br>${Number(s.dur).toFixed(1)}s</span>
      </div>`).join('') : '<span class="muted">该块无旁白句</span>';
    $('sentList').querySelectorAll('.sent').forEach(el =>
      el.onclick = () => selectSentence(+el.dataset.s));
    if (SEL_SENT !== null) await loadSentDetail(SEL_SENT);
    else { SENT_VIEW = null; $('sentDetail').hidden = true; renderSentMarks(); }
  } catch (e) {
    $('sentList').innerHTML = '<span class="muted">句列表载入失败：' + esc(e.message) + '</span>';
  }
}

const selectSentence = guard(async (sid) => {
  const r = await api('/api/sent/select', 'POST', {sent: sid});
  S = r.data;
  await loadSentences();
});

async function loadSentDetail(sid) {
  try {
    SENT_VIEW = (await api('/api/sentence?sid=' + sid)).data;
    renderSentDetail();
    renderSentMarks();
  } catch (e) { toast(e.message, true); }
}

function renderSentDetail() {
  const v = SENT_VIEW, box = $('sentDetail');
  if (!v) { box.hidden = true; return; }
  box.hidden = false;
  const shared = (v.shared || []).filter(x => x.wins.length);
  box.innerHTML = `
    <div class="sd-head">句 ${v.id} · ${v.cuts.length} 刀 · 成片 ${v.t0.toFixed(2)}s +${v.dur.toFixed(2)}s
      ${v.anchor ? ` · 首镜 ${v.anchor.first} / 尾镜 ${v.anchor.last}` : ''}
      ${v.zh_ready ? ' · 台词已译中' : ''}</div>
    <div class="sd-cuts">${v.cuts.map(c => `
      <div class="sd-cut ${c.by === 'sent' ? 'by-sent' : ''}"
           title="刀 ${c.win} · 源片 ${c.src[0].toFixed(2)}–${c.src[1].toFixed(2)}s · ${c.by}">
        <img src="${c.thumb}" alt="刀 ${c.win}">
        <div class="sd-t">刀${c.win} ${c.src[0].toFixed(1)}s ${c.by === 'other_sent' ? '邻句' : c.by === 'sent' ? '★句锚' : ''}</div>
        <div class="sd-l">${esc(c.line || '（无台词）')}</div>
      </div>`).join('')}</div>
    ${shared.length ? `<div class="sd-head" style="margin-top:6px;color:var(--amber-600)">
      与句 ${shared.map(x => x.sent + (x.edited ? '(已编辑)' : '')).join('、')} 共用边界刀；同时编辑时后写的句优先</div>` : ''}
    <div class="sd-foot">
      <button id="sdEdit">编辑本句画面</button>
      <button id="sdPreview">渲染这句</button>
      <button id="sdClear">取消选中</button>
    </div>`;
  $('sdEdit').onclick = openSentEditor;
  $('sdPreview').onclick = () => {
    document.querySelector('input[name="pvScope"][value="sentence"]').checked = true;
    openPreview();
  };
  $('sdClear').onclick = () => selectSentence(-1);
}

function renderSentMarks() {
  const layer = $('sentMarks');
  if (!S || !SENT_VIEW) { layer.innerHTML = ''; return; }
  const total = S.project.src_dur || 1;
  layer.innerHTML = SENT_VIEW.cuts.map(c =>
    `<i style="left:${(c.src[0] / total * 100).toFixed(4)}%;width:${Math.max(0.06, (c.src[1] - c.src[0]) / total * 100).toFixed(4)}%"
        title="刀 ${c.win} · ${c.src[0]}–${c.src[1]}s"></i>`).join('');
}

async function loadDialogueTrack() {
  if (!S || !cur()) return;
  const b = cur(), total = S.project.src_dur;
  const a = Math.max(0, b.src_shifted[0] - 15);
  const z = Math.min(total, b.src_shifted[1] + 15);
  try {
    const r = await api(`/api/dialogue/range?a=${a.toFixed(2)}&z=${z.toFixed(2)}&limit=60`);
    const span = Math.max(1, z - a);
    $('dlgTrack').innerHTML = r.items.length ? r.items.map(x => {
      const l = Math.max(a, x.t0), rr = Math.min(z, x.t1);
      const here = x.t1 >= b.src_shifted[0] && x.t0 <= b.src_shifted[1];
      const pct = Math.max(1.1, (rr - l) / span * 100);
      const inner = pct < 3.2 ? '·'
        : pct < 7 ? `<b>${x.t0.toFixed(0)}</b>`
        : `<b>${x.t0.toFixed(0)}s</b>${esc(String(x.text).slice(0, 9))}`;
      return `<div class="dlg-item ${here ? 'here' : ''} ${pct < 3.2 ? 'tiny' : ''}"
        style="left:${((l - a) / span * 100).toFixed(3)}%;width:${pct.toFixed(3)}%"
        data-t="${x.t0}" title="[${x.t0}s–${x.t1}s] ${esc(x.text)}${x.orig ? '\n原文：' + esc(x.orig) : ''}">
        ${inner}</div>`;
    }).join('') : '<div class="dlg-empty">当前区间无人物台词</div>';
    $('dlgTrack').querySelectorAll('.dlg-item').forEach(el =>
      el.onclick = () => {
        const t = +el.dataset.t;
        loadSheet(true, t);
        $('shotLine').innerHTML = `<span class="lbl">台词起点 ${t.toFixed(1)}s</span><b>${esc(el.title.split('] ').slice(1).join('] ').split('\n')[0])}</b>`;
      });
  } catch (e) {
    $('dlgTrack').innerHTML = '<div class="dlg-empty">台词载入失败：' + esc(e.message) + '</div>';
  }
}

// ---------------------------------------------------------------- actions
async function refresh() {
  const r = await api('/api/state');
  S = r.data;
  render();
}

async function setCurrent(b) {
  await api('/api/current', 'POST', {beat: b});
  sheetKey = '';
  await refresh();
}

async function shift(delta, snap) {
  const r = await api('/api/shift', 'POST', {
    beat: S.current, delta, snap: !!snap
  });
  S = r.data;
  sheetKey = '';
  render();
}

const doShiftBtn = guard(async (step) => {
  const b = cur();
  await shift(b.delta + step, false);   // 微调不吸附，否则 ±0.1s 会被拉回原镜头
});

const jump = guard(async (idx) => {
  const r = await api('/api/jump', 'POST', {beat: S.current, idx});
  S = r.data; sheetKey = ''; render();
  toast('首镜已跳到镜头 ' + idx + '（待写回）');
});

const doSave = guard(async () => {
  if (!S.dirty) return toast('没有待写回的平移');
  const r = await api('/api/save', 'POST', {});
  S = r.state; sheetKey = ''; render();
  toast(`已写回 ${r.written} 块 · 备份 ${r.backup.split(/[\\/]/).pop()}`);
});

const doConfirm = guard(async () => {
  const b = cur();
  const val = !S.confirmed.includes(b.id);
  const r = await api('/api/confirm', 'POST', {beat: b.id, value: val});
  S = r.data; render();
  toast(val ? '已确认本块' : '已取消确认');
});

// ---------------------------------------------------------------- A/B
const openAB = guard(async (sid) => {
  const q = '/api/ab?beat=' + S.current + (sid !== undefined && sid !== null && sid !== '' ? '&sid=' + sid : '');
  const r = await api(q);
  const rows = r.pairs.map(p => `
    <div class="ab-row">
      <div class="lab">第 ${p.cut} 刀<br>${p.before.toFixed(2)}s</div>
      <div class="pic"><img src="${p.before_url}"><div class="cap">改前</div></div>
      <div class="pic"><img src="${p.after_url}"><div class="cap">改后 ${p.after.toFixed(2)}s</div></div>
      <div class="ab-line">改前台词：${esc(p.before_line || '（无）')}　｜　改后台词：${esc(p.after_line || '（无）')}</div>
    </div>`).join('');
  $('abBody').innerHTML =
    `<div class="ab-head"><span></span><span>改前</span><span>改后</span></div>${rows}` +
    (r.sid !== null && r.sid !== undefined
      ? `<div class="cmd-note" style="margin-top:8px">句 ${r.sid} 的刀 · 台词${r.zh_ready ? '为中文译文' : '为原文'}</div>` : '') +
    (r.sent_count ? `<div class="cmd-note">当前有 ${r.sent_count} 句使用句级锚点，改后画面已按最终合并结果展示</div>` : '');
  $('abClips').innerHTML = '';
  $('abModal').dataset.before = r.clip_before;
  $('abModal').dataset.after = r.clip_after;
  $('abModal').hidden = false;
});

function playClips() {
  const m = $('abModal');
  $('abClips').innerHTML = `
    <div class="wrap"><div class="cap">改前 · ${m.dataset.before}s 起 3 秒</div>
      <video controls autoplay loop src="/api/abclip?t=${m.dataset.before}&tag=before"></video></div>
    <div class="wrap"><div class="cap">改后 · ${m.dataset.after}s 起 3 秒</div>
      <video controls autoplay loop src="/api/abclip?t=${m.dataset.after}&tag=after"></video></div>`;
  toast('片段生成中，首次会慢几秒');
}

// ------------------------------------------------ 句级锚点编辑弹窗
let ANC_SHOTS = [];

const openSentEditor = guard(async () => {
  if (!SENT_VIEW) return toast('请先在右栏点选一句旁白', true);
  $('sentModal').hidden = false;
  ANC_CENTER = SENT_VIEW.cuts[0].src[0];
  await loadSentEditor(true);
});

async function loadSentEditor(resetInputs) {
  if (!SENT_VIEW) return;
  const v = (await api('/api/sentence?sid=' + SENT_VIEW.id)).data;
  SENT_VIEW = v;
  renderSentDetail();
  $('sentQuote').textContent = `「${v.text}」`;
  $('sentMeta').textContent =
    `句 ${v.id} · 剧情块 ${v.beat} · ${v.cuts.length} 刀 · 成片 ${v.t0.toFixed(2)}s +${v.dur.toFixed(2)}s · 刀号 ${v.wins.join(', ')}`;
  const shared = (v.shared || []).filter(x => x.wins.length);
  const cs = (v.conflicts || []).filter(x => (x.sents || []).includes(v.id));
  const warns = [];
  if (shared.length) warns.push(`与句 ${shared.map(x => x.sent).join('、')} 共用边界刀 ${shared.flatMap(x => x.wins).join(',')}；两句都编辑时，句号大的优先。`);
  if (cs.length) warns.push(`冲突刀 ${cs.map(x => x.win).join(',')} 被多句锚点同时指定，已按后写优先处理。`);
  if (S.narr_locked) warns.push('旁白尚未定稿，锚点写回当前被锁定。');
  $('sentWarn').hidden = !warns.length;
  $('sentWarn').textContent = warns.join('　');
  $('cutCards').innerHTML = v.cuts.map((c, i) => `
    <div class="cut-card ${c.by === 'sent' ? 'by-sent' : c.by === 'other_sent' ? 'by-other' : ''}">
      <img src="${c.thumb}" alt="刀 ${c.win}">
      <div class="cc-body">
        <div class="cc-t">第 ${i + 1} 刀 · 全局刀 ${c.win} · 源 ${c.src[0].toFixed(2)}–${c.src[1].toFixed(2)}s</div>
        <div class="cc-line">${esc(c.line || '（此刻无人物台词）')}</div>
        <span class="cc-tag">${c.by === 'sent' ? '本句锚点' : c.by === 'other_sent' ? `句 ${c.by_sent} 覆盖` : c.by === 'beat' ? '块级平移' : '原始'}</span>
      </div>
    </div>`).join('');
  if (resetInputs) {
    const f = v.anchor ? v.anchor.first : (v.first_shot || 0);
    const l = v.anchor ? v.anchor.last : (v.suggest_last || f);
    $('ancFirst').value = f;
    $('ancLast').value = l;
  }
  updateAncHint();
  await loadAncSheet();
}

function updateAncHint() {
  const n = SENT_VIEW ? SENT_VIEW.cuts.length : 0;
  $('ancHint').textContent = `本句 ${n} 刀 · 首镜 → 尾镜之间按刀数铺满（不挪其他刀）`;
}

async function loadAncSheet() {
  const c = Math.max(0, Math.min(S.project.src_dur, ANC_CENTER ?? (SENT_VIEW ? SENT_VIEW.cuts[0].src[0] : 0)));
  const r = await api(`/api/shots?beat=${S.current}&center=${c.toFixed(2)}`);
  ANC_SHOTS = r.shots;
  const f = +$('ancFirst').value, l = +$('ancLast').value;
  $('sentSheet').innerHTML = r.shots.map(s => `
    <div class="cell ${s.idx >= f && s.idx <= l ? 'active' : ''}" data-idx="${s.idx}"
         title="镜头 ${s.idx} · ${s.start}s–${s.end}s&#10;此刻台词：${esc(s.line || '（无）')}">
      <img loading="lazy" src="${s.thumb}" alt="${s.idx}">
      <span class="num">${s.idx}</span>
      ${s.ost ? '<span class="ost" title="原声段"></span>' : ''}
    </div>`).join('');
  $('sentSheet').querySelectorAll('.cell').forEach(el =>
    el.onclick = () => {
      const idx = +el.dataset.idx;
      $('ancFirst').value = idx;
      $('ancLast').value = idx + (SENT_VIEW ? SENT_VIEW.cuts.length : 1) - 1;
      $('sentSheet').querySelectorAll('.cell').forEach(x => x.classList.remove('active'));
      for (let k = idx; k <= +$('ancLast').value; k++)
        $('sentSheet').querySelector(`.cell[data-idx="${k}"]`)?.classList.add('active');
      const s = ANC_SHOTS.find(x => x.idx === idx);
      showShotLine(s);
    });
}

const applyAnchor = guard(async () => {
  const f = parseInt($('ancFirst').value, 10), l = parseInt($('ancLast').value, 10);
  if (!Number.isFinite(f) || !Number.isFinite(l)) return toast('请填首镜 / 尾镜编号', true);
  const r = await api('/api/sent/anchor', 'POST', {sent: SENT_VIEW.id, first: f, last: l});
  S = r.data;
  render();
  await loadSentEditor(false);
  toast(`句 ${SENT_VIEW.id} 已定首镜 ${f} / 尾镜 ${l}（待写回）`);
});

const clearAnchor = guard(async () => {
  const r = await api('/api/sent/clear', 'POST', {sent: SENT_VIEW.id});
  S = r.data;
  render();
  await loadSentEditor(true);
  toast('已清除本句锚点');
});

// ------------------------------------------------ 临时渲染预览
const openPreview = guard(async () => {
  $('previewModal').hidden = false;
  if (PV_LAST) {
    $('pvStatus').textContent =
      `上次：${PV_LAST.label} · ${PV_LAST.cuts} 刀 · ${PV_LAST.duration}s · ${PV_LAST.quality_name || ''}（已暂停，点播放继续）`;
    if (PV_LAST.final && PV_LAST.final.exists) {
      $('pvFinalName').textContent = PV_LAST.final.name;
      $('pvFinalSpec').textContent = `（${PV_LAST.final.width}×${PV_LAST.final.height} ${PV_LAST.final.codec.toUpperCase()}）`;
    }
  }
});

const doRenderPreview = guard(async () => {
  const scope = document.querySelector('input[name="pvScope"]:checked').value;
  if (scope === 'sentence' && SEL_SENT === null)
    return toast('请先在右栏点选一句旁白', true);
  $('pvRender').disabled = true;
  $('pvStatus').textContent = '渲染中…（按刀切片拼接，首次约 3–20 秒）';
  try {
    const quality = document.querySelector('input[name="pvQuality"]:checked').value;
    const r = await api('/api/preview/render', 'POST', {
      scope, beat: S.current, sent: SEL_SENT,
      subs: $('pvSubs').checked, audio: $('pvAudio').checked,
      force: $('pvForce').checked, quality
    });
    PV_LAST = r.data;
    $('pvStatus').textContent =
      `${r.data.label} · ${r.data.cuts} 刀 · ${r.data.duration}s · ` +
      `${r.data.quality_name} (CRF ${r.data.crf}) · ` +
      `${r.data.cached ? '命中缓存' : '耗时 ' + r.data.elapsed + 's'} · ` +
      `${r.data.audio ? '含混音' : '无声'} · ${r.data.subs ? '含字幕' : '无字幕'}` +
      (r.data.subs_fallback ? '（字幕滤镜降级）' : '') +
      (r.data.dirty_cuts ? ` · 应用了 ${r.data.dirty_cuts} 处未写回改动` : ' · 原始 cutplan');
    if (r.data.final && r.data.final.exists) {
      $('pvFinalName').textContent = r.data.final.name;
      $('pvFinalSpec').textContent =
        `（${r.data.final.width}×${r.data.final.height} ${r.data.final.codec.toUpperCase()}，画面流不重编码）`;
    }
    $('pvVideo').innerHTML =
      `<video controls autoplay loop preload="auto" playsinline
         ${r.data.poster ? `poster="${r.data.poster}"` : ''}
         src="${r.data.url}&v=${Date.now()}"></video>` +
      `<div class="pv-meta">成片区间 ${r.data.new[0]}–${r.data.new[1]}s　·　产物 <code>${esc(r.data.file)}</code>` +
      `<br>预览 ${r.data.width}×${r.data.height} 临时转码；关闭窗口会暂停播放。仅预览，未写入任何工程文件。</div>`;
  } finally { $('pvRender').disabled = false; }
});

const openCmd = guard(async () => {
  const r = await api('/api/rebuild');
  $('cmdBody').textContent = r.commands.join('\r\n');
  $('cmdModal').hidden = false;
});

// ---------------------------------------------------------------- drag
function initDrag() {
  // 全局轨：粗定位（1px ≈ 源片 1~2s）；局部轨：精细（1px ≈ 0.09s）
  const bind = (el, widthEl, span) => {
    el.addEventListener('mousedown', e => {
      if (!S || !cur()) return;
      e.preventDefault();
      drag = {x: e.clientX, base: cur().delta,
              width: widthEl.clientWidth || 1, span: span(), moved: false};
    });
  };
  bind($('tlBlock'), $('tlTrack'), () => S.project.src_dur);
  bind($('tlZoomBlock'), $('tlZoom'), () => ZOOM_SPAN);

  document.addEventListener('mousemove', e => {
    if (!drag || !cur()) return;
    const dx = e.clientX - drag.x;
    if (Math.abs(dx) < 2 && !drag.moved) return;
    drag.moved = true;
    const raw = drag.base + dx / drag.width * drag.span;
    const b = cur(), a = b.src[0] + raw, z = b.src[1] + raw;
    positionBlocks(a, z);
    $('tlLabel').textContent =
      `${a.toFixed(2)} → ${z.toFixed(2)}s　·　预览平移 ${raw >= 0 ? '+' : ''}${raw.toFixed(2)}s（松手生效）`;
    drag.raw = raw;
  });
  document.addEventListener('mouseup', () => {
    if (!drag) return;
    const d = drag; drag = null;
    if (!d.moved) return;
    shift(d.raw, $('snapChk').checked).catch(e => toast(e.message, true));
  });
}

// ------------------------------------------------------------ 剪映草稿导出
let EX = null;

const openExport = guard(async () => {
  const r = await api('/api/export/state');
  EX = r;
  $('exportModal').hidden = false;
  if (!$('exName').value.trim()) {
    const base = (S.project.dir || '').split(/[\\/]/).filter(Boolean).pop() || 'recap';
    $('exName').value = base + '_漫剪';
  }
  renderExport();
});

function renderExport() {
  const d = EX || {}, env = d.env || {}, c = d.collect || {};
  $('exCutCount').textContent = c.cuts || '—';
  $('exFiles').innerHTML = (c.files || []).map(f =>
    `<div class="ex-file"><span class="nm">${esc(f.key)}</span>` +
    `<span>${f.exists ? (f.size / 1e6).toFixed(1) + ' MB' : '缺失'}</span></div>`).join('') +
    `<div class="muted" style="margin-top:5px">${c.ready ? '产物齐全，可导出' : '产物不全，请先跑完混音合片链路'} · ${c.duration}s · ${c.cuts} 刀</div>`;
  $('exEnv').innerHTML =
    `<div>草稿目录：<code>${esc(env.draft_dir || '未找到')}</code></div>` +
    `<div>现有草稿：明文 ${env.plain ?? '—'} 个 / 密文 ${env.encrypted ?? '—'} 个 · 生成环境 ${env.worker_ok ? '可用' : '不可用'}</div>` +
    `<div class="muted">${esc(env.note || '')}</div>` +
    ((d.exports || []).length
      ? `<div>已生成：${d.exports.map(x => esc(x.name) + (x.installed ? '（已写入）' : '')).join('、')}</div>` : '');
  if (d.last) renderExHealth(d.last);
  else $('exHealth').innerHTML = '<span class="muted">尚未生成</span>';
}

function renderExHealth(x) {
  const h = x.health || {};
  $('exHealth').innerHTML =
    `<div>目录 <code>${esc(x.folder)}</code> · ${x.clips} 段 · ${x.duration}s</div>` +
    (h.checks || []).map(t => `<div class="ok">✓ ${esc(t)}</div>`).join('') +
    (h.issues || []).map(t => `<div class="bad">✗ ${esc(t)}</div>`).join('') +
    `<div class="muted">${x.installed ? '已写入剪映草稿目录' : '仅在工作区，尚未写入剪映'}</div>`;
}

const doBuild = guard(async () => {
  const name = $('exName').value.trim();
  const mode = document.querySelector('input[name="exMode"]:checked').value;
  if (!name) return toast('请填草稿名', true);
  $('exBuild').disabled = true;
  $('exHealth').textContent = '生成中…（逐刀模式约 20 秒）';
  try {
    const r = await api('/api/export/build', 'POST', {name, mode});
    EX.last = r.data;
    renderExHealth(r.data);
    $('exInstall').disabled = !r.data.ok;
    $('exPath').textContent = '';
    toast(r.data.ok ? '草稿已生成并通过自检' : '草稿已生成，但自检有问题');
  } finally { $('exBuild').disabled = false; }
});

const doInstall = guard(async () => {
  const name = $('exName').value.trim();
  if (!EX || !EX.last) return toast('请先生成草稿', true);
  if (!confirm(`确认把草稿「${name}」写入剪映草稿目录？\n\n· 不覆盖同名草稿，不修改剪映索引\n· 写入后重启剪映（或进出一次其他草稿）刷新列表\n· 剪映保存后草稿变密文，重复导出会覆盖你在剪映里的人工修改`)) return;
  const r = await api('/api/export/install', 'POST', {name});
  $('exPath').textContent = r.data.path;
  toast('已写入剪映草稿目录，请重启剪映刷新列表');
  const s = await api('/api/export/state');
  EX = {...s, last: EX.last};
  EX.last.installed = true;
  renderExport();
});

// ---------------------------------------------------------------- bind
function init() {
  $('btnLoad').onclick = guard(async () => {
    const dir = $('dirInput').value.trim();
    if (!dir) return toast('请填工程目录', true);
    const r = await api('/api/load', 'POST', {dir});
    S = r.data; sheetKey = ''; render();
    toast('工程已载入：' + S.project.name);
  });
  $('dirInput').onkeydown = e => { if (e.key === 'Enter') $('btnLoad').click(); };
  $('btnUndo').onclick = guard(async () => { S = (await api('/api/undo', 'POST', {})).data; sheetKey = ''; render(); });
  $('btnRedo').onclick = guard(async () => { S = (await api('/api/redo', 'POST', {})).data; sheetKey = ''; render(); });
  $('btnSave').onclick = doSave;
  $('btnConfirm').onclick = doConfirm;
  $('btnReset').onclick = guard(async () => { S = (await api('/api/reset', 'POST', {beat: S.current})).data; sheetKey = ''; render(); });
  $('btnAB').onclick = () => openAB();
  $('btnRebuild').onclick = openCmd;
  $('btnPreview').onclick = openPreview;
  $('abClose').onclick = () => closeModal('abModal');
  $('cmdClose').onclick = () => closeModal('cmdModal');
  $('abClip').onclick = playClips;
  $('cmdCopy').onclick = () => { navigator.clipboard.writeText($('cmdBody').textContent); toast('已复制'); };
  document.querySelectorAll('[data-shift]').forEach(el =>
    el.onclick = () => doShiftBtn(parseFloat(el.dataset.shift)));
  $('btnExport').onclick = openExport;
  $('exClose').onclick = () => closeModal('exportModal');
  $('exBuild').onclick = doBuild;
  $('exInstall').onclick = doInstall;
  $('sentClose').onclick = () => closeModal('sentModal');
  $('ancApply').onclick = applyAnchor;
  $('ancClear').onclick = clearAnchor;
  $('ancAB').onclick = () => openAB(SENT_VIEW ? SENT_VIEW.id : null);
  $('ancPreset').onclick = () => {
    const f = parseInt($('ancFirst').value, 10);
    if (!Number.isFinite(f)) return toast('请先填首镜编号', true);
    $('ancLast').value = f + (SENT_VIEW ? SENT_VIEW.cuts.length : 1) - 1;
    loadAncSheet().catch(e => toast(e.message, true));
  };
  $('ancPrev').onclick = () => { ANC_CENTER = Math.max(0, (ANC_CENTER || 0) - 26); loadAncSheet().catch(e => toast(e.message, true)); };
  $('ancNext').onclick = () => { ANC_CENTER = Math.min(S.project.src_dur, (ANC_CENTER || 0) + 26); loadAncSheet().catch(e => toast(e.message, true)); };
  ['ancFirst', 'ancLast'].forEach(id => $(id).onchange = () => loadAncSheet().catch(e => toast(e.message, true)));
  $('pvClose').onclick = () => closeModal('previewModal');
  $('pvRender').onclick = doRenderPreview;
  ['abModal', 'cmdModal', 'exportModal', 'sentModal', 'previewModal'].forEach(id =>
    $(id).onclick = e => { if (e.target.id === id) closeModal(id); });

  document.addEventListener('keydown', e => {
    if (/INPUT|TEXTAREA/.test(document.activeElement.tagName)) return;
    if (!S) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const i = S.beats.findIndex(b => b.id === S.current);
      const n = Math.min(S.beats.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)));
      setCurrent(S.beats[n].id);
    } else if (e.code === 'Space' && ['abModal', 'cmdModal', 'exportModal', 'sentModal', 'previewModal'].every(id => $(id).hidden)) {
      e.preventDefault(); doConfirm();
    } else if (e.key === 'Escape') {
      ['abModal', 'cmdModal', 'exportModal', 'sentModal', 'previewModal'].forEach(id => closeModal(id));
    }
  });
  initDrag();
}

init();
refresh().then(async () => {
  if (location.hash === '#export') openExport();
  if (location.hash === '#sent') { await selectSentence(1); await openSentEditor(); }
  if (location.hash === '#preview') { await openPreview(); await doRenderPreview(); }
}).catch(e => {
  document.querySelector('.workspace').innerHTML =
    `<div class="panel" style="grid-column:1/-1;padding:24px">
      <h3>工程载入失败</h3><p>${esc(e.message)}</p>
      <p class="muted">在右上角填入完整工程目录后点「载入工程」。</p></div>`;
});
