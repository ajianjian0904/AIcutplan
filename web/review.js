/* 逐句校对台 · 旁白 / 画面 / 人物台词三轨对齐（无构建前端） */
'use strict';

let STATE = null;   // /api/state
let ROWS = null;    // 轻量句列表 + 进度
let CUR = null;     // 当前句三轨详情
let CAND = null;    // 候选首镜
let SEL = null;     // 选中的候选首镜编号
let BASE = null;    // 候选基准首镜
let VIDEO = null;
let toastTimer = null;
let beatsReady = false;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

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

/* ---------------------------------------------------------------- 状态 */
async function loadState() {
  STATE = (await api('/api/state')).data;
  const p = ROWS ? ROWS.progress : {confirmed: 0, total: 0};
  const done = p.confirmed, total = p.total || 1;
  $('progText').textContent =
    `已确认 ${done} / ${p.total} 句　·　需复核 ${p.flagged}　·　已改画面 ${p.edited}`;
  $('progBar').style.width = (100 * done / total).toFixed(1) + '%';
  const tip = $('gateTip');
  if (STATE.narr_locked) {
    tip.className = 'gate-tip locked';
    tip.textContent = '旁白未定稿 · 写回已锁';
  } else if (STATE.dirty) {
    tip.className = 'gate-tip';
    tip.textContent = `${STATE.sent_count} 处句级改动待写回`;
  } else {
    tip.className = 'gate-tip';
    tip.textContent = '会话干净';
  }
}

async function loadRows() {
  const u = new URLSearchParams();
  if (beatsReady) {
    u.set('beat', $('beatSel').value || 'all');
    u.set('only', $('onlySel').value);
    u.set('q', $('qInput').value.trim());
  }
  ROWS = await api('/api/review/rows?' + u.toString());
  if (!beatsReady) {
    $('beatSel').innerHTML = '<option value="all">全部剧情块</option>' +
      ROWS.beats.map(b => `<option value="${b.id}">${esc(b.label)} · ${b.count} 句</option>`).join('');
    beatsReady = true;
  }
  $('listCount').textContent = `${ROWS.matched} / ${ROWS.total} 句`;
  renderList();
  await loadState();
}

function renderList() {
  if (!ROWS) return;
  if (!ROWS.rows.length) {
    $('sentList').innerHTML = '<div class="muted" style="padding:16px 6px">没有符合条件的句子</div>';
    return;
  }
  $('sentList').innerHTML = ROWS.rows.map(x => {
    const dot = x.confirmed ? 'ok' : x.edited ? 'ed' :
      x.flags.some(f => ['info', 'blank', 'gap', 'conflict'].includes(f)) ? 'watch' : '';
    return `<div class="rv-item ${CUR && x.id === CUR.id ? 'on' : ''}" data-id="${x.id}">
      <span class="no">${x.id}</span>
      <div class="body">
        <div class="tx">${esc(x.text)}</div>
        <div class="sub">块${x.beat} · ${x.cuts}刀 · ${x.dur.toFixed(2)}s${x.edited ? ' · 已改' : ''}${x.delta ? ' · Δ' + x.delta + 's' : ''}</div>
      </div>
      <span class="dot ${dot}" title="${dot === 'ok' ? '已确认' : dot === 'ed' ? '已改画面' : dot === 'watch' ? '需复核' : ''}"></span>
    </div>`;
  }).join('');
  $('sentList').querySelectorAll('.rv-item').forEach(el =>
    el.onclick = () => selectSent(+el.dataset.id));
}

/* ---------------------------------------------------------------- 当前句 */
async function loadRow(sid) {
  CUR = (await api('/api/review/row?sid=' + sid)).data;
  renderHead();
  renderTracks();
  renderFlags();
  renderList();
  scrollListTo(sid);
}

function scrollListTo(sid) {
  const el = $('sentList').querySelector(`.rv-item[data-id="${sid}"]`);
  if (el) el.scrollIntoView({block: 'nearest'});
}

function renderHead() {
  if (!CUR) return;
  $('rowHead').innerHTML = `
    <span class="sid">句 ${CUR.id} / 块 ${CUR.beat} · ${esc(CUR.label)}</span>
    <span class="quote">${esc(CUR.text)}</span>
    <button id="btnOk" class="primary ${CUR.confirmed ? 'btn-ok done' : 'btn-ok'}">
      ${CUR.confirmed ? '✓ 已确认 · 点此取消' : '确认本句'}</button>
    <div class="meta">
      成片 ${CUR.new[0].toFixed(2)}–${CUR.new[1].toFixed(2)}s（${CUR.duration.toFixed(2)}s）
      　·　${CUR.wins.length} 刀 [${CUR.wins.join(', ')}]
      　·　首镜 ${CUR.first_shot ?? '-'}
      ${CUR.delta ? `　·　块级平移 ${CUR.delta > 0 ? '+' : ''}${CUR.delta}s` : ''}
      ${CUR.edited ? '　·　本句已有句级锚点' : ''}
      ${CUR.zh_ready ? '　·　中文台词就绪' : '　·　台词未译中'}
    </div>`;
  $('btnOk').onclick = () => doConfirm(!CUR.confirmed);
}

function renderTracks() {
  if (!CUR) return;
  const A = CUR.new[0], Z = CUR.new[1], d = (Z - A) || 1;
  $('ruler').innerHTML = [0, .25, .5, .75, 1].map(f =>
    `<i style="left:${(f * 100).toFixed(2)}%">${(A + f * d).toFixed(2)}s</i>`).join('');

  $('narrLane').innerHTML =
    `<div class="narr-block"><span id="said"></span><span id="unsaid">${esc(CUR.text)}</span></div>` +
    `<span class="narr-tip">按字数均分的朗读进度，仅用于定位</span>`;

  $('videoLane').innerHTML = '<div class="cut-row">' + CUR.cuts.map(c => {
    const grow = Math.max(0.1, (c.new[1] - c.new[0]) / d);
    return `<div class="cut ${c.by === 'sent' || c.by === 'other_sent' ? 'by-sent' : ''}"
                 data-win="${c.win}" style="flex:${grow.toFixed(4)} 1 0; width:0"
                 title="刀${c.win} · 源 ${c.src[0].toFixed(2)}–${c.src[1].toFixed(2)}s · ${esc(c.line)}">
      <img loading="lazy" src="${c.thumb}" alt="刀${c.win}">
      <div class="cap"><b>刀${c.win}</b> · 镜${c.shot ?? '-'}<br>源 ${c.src[0].toFixed(1)}–${c.src[1].toFixed(1)}s</div>
      ${c.ost ? '<span class="ost" title="该镜头标为有原声"></span>' : ''}
    </div>`;
  }).join('') + '</div>';

  $('lineLane').innerHTML = CUR.track.length
    ? CUR.track.map((s, i) => {
        const l = Math.max(0, (s.new[0] - A) / d * 100);
        const w = Math.max(1.2, (s.new[1] - s.new[0]) / d * 100);
        return `<div class="dlg-seg ${s.virtual ? 'virtual' : ''}" data-i="${i}"
          style="left:${l.toFixed(3)}%;width:${Math.min(w, 100 - l).toFixed(3)}%"
          title="[源片 ${s.t0}–${s.t1}s] ${esc(s.text)}${s.orig ? '\n原文：' + esc(s.orig) : ''}">
          <b>${s.t0.toFixed(1)}–${s.t1.toFixed(1)}s${s.virtual ? ' · 近邻' : ''}</b>${esc(String(s.text).slice(0, 30))}
        </div>`;
      }).join('')
    : '<div class="no-line">这句覆盖的画面没有人物台词</div>';
  $('playhead').hidden = true;
}

function renderFlags() {
  if (!CUR) return;
  $('flags').innerHTML = CUR.flags.length
    ? CUR.flags.map(f => `<span class="flag ${f.watch ? 'watch' : ''} ${f.k === 'edited' ? 'edited' : ''}">${esc(f.text)}</span>`).join('')
    : '<span class="muted">无特别提示 · 顺序顺延即可</span>';
}

/* ---------------------------------------------------------------- 播放联动 */
function clearHighlights() {
  document.querySelectorAll('.cut.on,.dlg-seg.on').forEach(el => el.classList.remove('on'));
  const a = $('said'), b = $('unsaid');
  if (a && b && CUR) { a.textContent = ''; b.textContent = CUR.text; }
  $('playhead').hidden = true;
}

function stopPlay() {
  if (VIDEO) {
    try { VIDEO.pause(); VIDEO.removeAttribute('autoplay'); VIDEO.removeAttribute('loop'); } catch (e) { /* noop */ }
  }
  VIDEO = null;
  $('videoBox').innerHTML = '<div class="pv-empty">点「播放这句」看三者同步对齐：旁白读到哪、当前是哪一刀、这一刻人物在说什么</div>';
  $('btnPlay').textContent = '▶ 播放这句';
  clearHighlights();
}

function onTimeUpdate() {
  if (!VIDEO || !CUR) return;
  const A = CUR.new[0], Z = CUR.new[1], d = (Z - A) || 1;
  const T = A + VIDEO.currentTime;
  const p = Math.max(0, Math.min(1, (T - A) / d));

  const lane = $('videoLane').getBoundingClientRect();
  const box = $('trackBox').getBoundingClientRect();
  const ph = $('playhead');
  ph.hidden = false;
  ph.style.left = (lane.left - box.left + p * lane.width).toFixed(1) + 'px';

  const txt = CUR.text;
  const n = Math.floor(p * txt.length);
  const a = $('said'), b = $('unsaid');
  if (a && b) { a.textContent = txt.slice(0, n); b.textContent = txt.slice(n); }

  document.querySelectorAll('.cut').forEach(el => {
    const c = CUR.cuts.find(x => x.win === +el.dataset.win);
    el.classList.toggle('on', !!c && T >= c.new[0] && T < c.new[1]);
  });
  document.querySelectorAll('.dlg-seg').forEach(el => {
    const s = CUR.track[+el.dataset.i];
    el.classList.toggle('on', !!s && T >= s.new[0] && T <= s.new[1]);
  });
}

const doPlay = guard(async () => {
  if (!CUR) return;
  $('btnPlay').disabled = true;
  $('pvInfo').textContent = '渲染中…';
  try {
    const r = await api('/api/preview/render', 'POST', {
      scope: 'sentence', beat: CUR.beat, sent: CUR.id,
      subs: true, audio: true, quality: 'high'
    });
    const d = r.data;
    $('videoBox').innerHTML =
      `<video controls autoplay preload="auto" ${d.poster ? `poster="${d.poster}"` : ''}
         src="${d.url}&v=${Date.now()}"></video>`;
    VIDEO = $('videoBox').querySelector('video');
    VIDEO.addEventListener('timeupdate', onTimeUpdate);
    VIDEO.addEventListener('ended', clearHighlights);
    $('btnPlay').textContent = '↻ 重新播放';
    $('pvInfo').textContent =
      `${d.cuts} 刀 · ${d.duration}s · ${d.cached ? '命中缓存' : d.elapsed + 's'} · ` +
      `${d.quality_name} · 含混音/字幕 · 已按当前未写回改动渲染`;
  } finally {
    $('btnPlay').disabled = false;
  }
});

/* ---------------------------------------------------------------- 候选卡 */
async function loadCands(base) {
  if (!CUR) return;
  const u = '/api/review/candidates?sid=' + CUR.id + (base ? '&base=' + base : '');
  CAND = await api(u);
  BASE = CAND.first_shot;
  $('firstInput').value = BASE;
  const pick = CAND.candidates.find(x => x.is_baseline) || CAND.candidates[0];
  SEL = pick ? pick.first : null;
  renderCands();
  renderCompare();
}

function renderCands() {
  if (!CAND) return;
  $('candList').innerHTML = CAND.candidates.length
    ? CAND.candidates.map(x => {
        const first = x.cuts[0];
        return `<div class="cand ${SEL === x.first ? 'on' : ''}" data-first="${x.first}">
          <img loading="lazy" src="${first ? first.thumb : ''}" alt="">
          <div class="info">
            <div class="t1">首镜 ${x.first} → ${x.last}
              ${x.is_current ? '<span class="tag">当前锚点</span>' : ''}
              ${x.is_baseline ? '<span class="tag">当前起点</span>' : ''}</div>
            <div class="t2">${esc(x.cuts.map(c => c.line ? c.line.slice(0, 10) : '—').join(' ｜ '))}</div>
          </div>
        </div>`;
      }).join('')
    : '<div class="muted">这个位置没有可用候选，点「← / →」换个范围</div>';
  $('candList').querySelectorAll('.cand').forEach(el =>
    el.onclick = () => { SEL = +el.dataset.first; renderCands(); renderCompare(); });
}

function renderCompare() {
  if (!CAND || !SEL) return;
  const p = CAND.candidates.find(x => x.first === SEL);
  if (!p) return;
  $('cmpHint').textContent = `首镜 ${p.first}–${p.last}`;
  $('btnApply').disabled = false;
  const col = (items, key) => `<div class="cmp-col">` + items.map(c => `
    <div class="cmp-cut">
      <img loading="lazy" src="${c.thumb}" alt="">
      <div class="c1">刀${c.win} · 镜${c.shot ?? '-'} · 源 ${Number(c.src[0]).toFixed(1)}–${Number(c.src[1]).toFixed(1)}s</div>
      <div class="c2" title="${esc(c.line_orig || c.line)}">${esc(c.line || '（此刻无人物台词）')}</div>
    </div>`).join('') + '</div>';
  $('compare').innerHTML =
    `<div class="cmp-head"><span>改前 · 当前生效</span><span>改后 · 首镜 ${p.first}</span></div>` +
    `<div class="cmp-grid">${col(CAND.before)}${col(p.cuts)}</div>` +
    `<p class="rv-note">逐刀对比：换过去之后人物台词有没有被切在半句、画面有没有跳到别的场景。这里只算不写，点「应用到本句」才进会话。</p>`;
}

/* ---------------------------------------------------------------- 动作 */
const selectSent = guard(async sid => {
  stopPlay();
  await api('/api/sent/select', 'POST', {sent: sid});
  await loadRow(sid);
  await loadCands();
});

const doConfirm = guard(async value => {
  if (!CUR) return;
  await api('/api/review/confirm', 'POST', {sent: CUR.id, value});
  CUR.confirmed = value;
  await loadRows();
  renderHead();
  if (value) {
    toast(`句 ${CUR.id} 已确认`);
    await nextSent(1);
  } else {
    toast(`句 ${CUR.id} 已取消确认`);
  }
});

async function nextSent(step) {
  if (!ROWS || !ROWS.rows.length) return;
  const i = ROWS.rows.findIndex(x => x.id === CUR.id);
  const j = i < 0 ? 0 : Math.max(0, Math.min(ROWS.rows.length - 1, i + step));
  const id = ROWS.rows[j].id;
  if (id === CUR.id) {
    toast(step > 0 ? '已经是最后一句' : '已经是第一句');
    return;
  }
  await selectSent(id);
}

const doApply = guard(async () => {
  if (!CUR || !SEL || !CAND) return;
  const p = CAND.candidates.find(x => x.first === SEL);
  await api('/api/sent/anchor', 'POST', {sent: CUR.id, first: p.first, last: p.last});
  toast(`句 ${CUR.id} 已设锚点：首镜 ${p.first} → ${p.last}（未写工程文件）`);
  await Promise.all([loadRow(CUR.id), loadRows()]);
  await loadCands();
});

const doClear = guard(async () => {
  if (!CUR) return;
  await api('/api/sent/clear', 'POST', {sent: CUR.id});
  toast(`句 ${CUR.id} 锚点已清除`);
  await Promise.all([loadRow(CUR.id), loadRows()]);
  await loadCands();
});

/* ---------------------------------------------------------------- init */
function init() {
  ['beatSel', 'onlySel'].forEach(id =>
    $(id).onchange = guard(async () => { await loadRows(); renderList(); }));
  let qTimer = null;
  $('qInput').oninput = () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => loadRows().catch(e => toast(e.message, true)), 220);
  };
  $('btnPlay').onclick = doPlay;
  $('btnStop').onclick = stopPlay;
  $('btnApply').onclick = doApply;
  $('btnClear').onclick = doClear;
  $('candGo').onclick = guard(async () => loadCands(+$('firstInput').value || BASE));
  $('candPrev').onclick = guard(async () => loadCands(Math.max(1, (BASE || 1) - 8)));
  $('candNext').onclick = guard(async () => loadCands((BASE || 1) + 8));
  $('firstInput').onkeydown = e => { if (e.key === 'Enter') $('candGo').click(); };

  document.addEventListener('keydown', e => {
    if (e.target.matches('input,select,textarea')) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); nextSent(1).catch(x => toast(x.message, true)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); nextSent(-1).catch(x => toast(x.message, true)); }
    else if (e.key === 'Enter') { e.preventDefault(); doConfirm(CUR ? !CUR.confirmed : true); }
    else if (e.code === 'Space') {
      e.preventDefault();
      if (VIDEO && !VIDEO.paused) stopPlay(); else doPlay();
    }
  });
  window.addEventListener('resize', () => { if (VIDEO && !VIDEO.paused) onTimeUpdate(); });
}

init();
(async () => {
  try {
    STATE = (await api('/api/state')).data;
    $('projName').textContent = STATE.project.name;
    await loadRows();
    const sid = STATE.selected_sent != null ? STATE.selected_sent : (ROWS.rows[0] || {}).id;
    if (sid != null) {
      await loadRow(sid);
      await loadCands();
    }
  } catch (e) {
    toast('载入失败：' + e.message, true);
  }
})();
