(() => {
  'use strict';
  const { $, $$, esc, money, num, pct, fmtDate, api, toast, fail, can } = LG;
  const PAGE = 200;
  const S = { meta: null, rows: [], byId: new Map(), totals: null, dayTotals: {}, sort: 'date', desc: true, offset: 0, days: 30, openId: null, order: [] };

  // Column definitions: key, header, sort key (or null), cell class, renderer.
  const band = (b) => b == null ? '<span class="muted">—</span>' : `<span class="band b${b}">${b}</span>`;
  const safeUrl = (u) => { if (!u) return null; let x = String(u).trim(); if (!/^https?:\/\//i.test(x)) x = 'https://' + x; try { const v = new URL(x); return /^https?:$/.test(v.protocol) ? v.href : null; } catch { return null; } };
  const storeCell = (r) => {
    const names = r.stores || []; const url = safeUrl(r.store_link);
    if (!names.length) return url ? `<a class="store-link" href="${esc(url)}" target="_blank" rel="noopener" data-stop>store ↗</a>` : '<span class="muted">—</span>';
    return `<span class="pill-list">${names.map((n) => url
      ? `<a class="pill-link" href="${esc(url)}" target="_blank" rel="noopener" data-stop title="Open the store page">${esc(n)} ↗</a>`
      : `<span>${esc(n)}</span>`).join('')}</span>`;
  };
  const COLS = {
    asin: ['ASIN', 'asin', 'tight', (r) => r.asin ? `<a class="asin-btn" href="https://www.amazon.com/dp/${esc(r.asin)}" target="_blank" rel="noopener" data-stop title="Open on Amazon">${esc(r.asin)}</a>` : '<span class="muted">—</span>'],
    product: ['Product', null, 'prod', (r) => `<span title="${esc(r.product_name || '')}">${esc(r.product_name || '')}</span>`],
    store: ['Store', null, 'tags', storeCell],
    brand: ['Brand', null, 'tags', (r) => (r.brands || []).length ? `<span class="pill-list">${r.brands.map((b) => `<span>${esc(b)}</span>`).join('')}</span>` : '<span class="muted">—</span>'],
    cost: ['Cost', 'cost', 'num', (r) => money(r.cost)],
    buy_qty: ['Buy qty', 'buy_qty', 'num', (r) => num(r.buy_qty)],
    qty_ordered: ['Ordered', 'qty_ordered', 'num', (r) => num(r.qty_ordered)],
    left: ['Left', 'left', 'num', (r) => num(r.left_to_buy)],
    spend: ['Spend', 'spend', 'num', (r) => r.actual_spend ? money(r.actual_spend, 0) : '—'],
    band: ['Band', 'band', 'center', (r) => band(r.lead_score_band)],
    roi: ['Pred ROI', 'roi', 'num', (r) => pct(r.lead_score_roi)],
    claude_qty: ['Claude qty', 'claude_qty', 'num', (r) => num(r.claude_buy_qty)],
    by: ['By', null, 'tight', (r) => esc(r.sourced_by || '')],
    promo: ['Promo', null, 'promo', (r) => r.promo_code ? `<span class="promo-code" title="${esc(r.promo_code)}">${esc(r.promo_code)}</span>` : '<span class="muted">—</span>'],
    va_notes: ['VA notes', null, 'note-cell', (r) => noteCell(r, 'va_notes')],
    buyer_notes: ['Buyer notes', null, 'note-cell', (r) => noteCell(r, 'buyer_notes')],
  };
  const DEFAULT_ORDER = ['product', 'asin', 'store', 'brand', 'cost', 'promo', 'va_notes', 'buyer_notes', 'buy_qty', 'qty_ordered', 'left', 'spend', 'band', 'roi', 'claude_qty', 'by'];
  const ORDER_KEY = 'leadColOrder.v2';
  function loadOrder() {
    try {
      let saved = JSON.parse(localStorage.getItem(ORDER_KEY) || 'null');
      if (!saved) {   // carry over an older arrangement, with ASIN moved next to Store
        const old = JSON.parse(localStorage.getItem('leadColOrder.v1') || 'null');
        if (Array.isArray(old) && old.includes('store')) {
          saved = old.filter((k) => k !== 'asin'); saved.splice(saved.indexOf('store'), 0, 'asin');
        }
      }
      if (Array.isArray(saved)) {
        // Keep the saved arrangement; slot any newly added column in after its neighbour in the default order.
        const ok = saved.filter((k) => COLS[k]);
        DEFAULT_ORDER.forEach((k, i) => { if (!ok.includes(k)) { const prev = DEFAULT_ORDER[i - 1]; const at = prev && ok.includes(prev) ? ok.indexOf(prev) + 1 : ok.length; ok.splice(at, 0, k); } });
        return ok;
      }
    } catch { /* storage blocked */ }
    return DEFAULT_ORDER.slice();
  }
  function saveOrder() { try { localStorage.setItem(ORDER_KEY, JSON.stringify(S.order)); } catch { /* ignore */ } }

  const isHome = () => S.meta?.mode === 'home';
  const editable = () => can('editor');

  function filters() {
    const f = {};
    const q = $('#q').value.trim(); if (q) f.q = q;
    if (S.days) { const d = new Date(); d.setDate(d.getDate() - S.days + 1); f.date_from = d.toISOString().slice(0, 10); }
    for (const [id, key] of [['store', 'store'], ['brand', 'brand'], ['sourced', 'sourced_by'], ['band', 'band_min']]) { const v = $('#' + id).value.trim(); if (v) f[key] = v; }
    for (const id of ['ordered', 'left', 'unscored']) if ($('#' + id).checked) f[id] = '1';
    return f;
  }

  async function load(append = false) {
    if (!append) S.offset = 0;
    const p = new URLSearchParams({ ...filters(), sort: S.sort, desc: S.desc, limit: PAGE, offset: S.offset });
    const res = await api('GET', '/api/leads?' + p);
    S.rows = append ? S.rows.concat(res.leads) : res.leads;
    S.byId = new Map(S.rows.map((r) => [r.id, r]));
    S.totals = res.totals;
    S.dayTotals = append ? { ...S.dayTotals, ...res.days } : (res.days || {});
    render();
  }

  async function loadMeta() {
    S.meta = await api('GET', '/api/leads-meta');
    $('#dlStores').innerHTML = S.meta.stores.map((s) => `<option value="${esc(s.name)}">`).join('');
    $('#dlBrands').innerHTML = S.meta.brands.map((s) => `<option value="${esc(s.name)}">`).join('');
    const cur = $('#sourced').value;
    $('#sourced').innerHTML = '<option value="">Anyone</option>' + (S.meta.sourcers || []).map((s) => `<option ${s === cur ? 'selected' : ''}>${esc(s)}</option>`).join('');
    $('#addBtn').classList.toggle('hidden', !can('editor'));
    S.newRowOrder = null;
    renderBanner();
  }

  function renderBanner() {
    const b = $('#banner'), m = S.meta;
    if (m.mode === 'home') { b.classList.add('hidden'); return; }
    const last = m.last_sync ? new Date(m.last_sync).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }) : 'not yet';
    const err = m.sync?.last_error ? ` <span class="err" title="${esc(m.sync.last_error)}">Last sync had a problem: ${esc(m.sync.last_error.split(' for url')[0].slice(0, 90))}</span>` : '';
    b.className = 'banner mirror';
    b.innerHTML = `<span><b>Copied live from Airtable</b> (checked every minute; last ${esc(last)}). New leads and edits you make here stay on this site (Airtable isn't changed), and the copy never overwrites them.${err}</span>
      <span class="spacer"></span>
      ${can('owner') ? `<button class="btn small" data-act="sync">Sync now</button><span id="homeWrap"><button class="btn small primary" data-act="home">Make this the home for leads…</button></span>` : ''}`;
  }

  function render() {
    const t = S.totals || {};
    $('#summary').textContent = `${num(t.n)} leads match · ${num(t.bought)} bought`;
    $('#head').innerHTML = S.order.map((k) => {
      const [label, sk, cls] = COLS[k];
      const arrow = sk && S.sort === sk ? `<span class="arrow">${S.desc ? '▼' : '▲'}</span>` : '';
      return `<th draggable="true" data-col="${k}" class="${cls} ${sk ? 'sortable' : ''}" ${sk ? `data-sort="${sk}"` : ''} title="Drag to move${sk ? ', click to sort' : ''}">${label}${arrow}</th>`;
    }).join('');
    let html = '', lastDay = null;
    for (const r of S.rows) {
      const day = r.lead_date || '';
      if (day !== lastDay) { html += dayHtml(day); lastDay = day; }
      html += rowHtml(r);
    }
    $('#rows').innerHTML = html;
    if (S.newRowOrder !== S.order.join()) renderNewRow();
    $('#empty').classList.toggle('hidden', S.rows.length > 0);
    const left = (t.n || 0) - S.rows.length;
    $('#more').classList.toggle('hidden', left <= 0);
    $('#moreBtn').textContent = `Show more (${num(left)} left)`;
  }

  function dayHtml(day) {
    const t = S.dayTotals[day] || {};
    const label = day ? new Date(day + 'T12:00:00').toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }) : 'No date';
    const bits = [`${num(t.n)} lead${t.n === 1 ? '' : 's'}`, `${num(t.bought)} bought`];
    if (t.units) bits.push(`${num(t.units)} units`);
    if (t.actual) bits.push(`${money(t.actual, 0)} spent`);
    if (t.expected) bits.push(`${money(t.expected, 0)} planned`);
    return `<tr class="day-row"><td colspan="${S.order.length}"><b>${esc(label)}</b><span class="muted"> · ${bits.join(' · ')}</span></td></tr>`;
  }

  function rowHtml(r) {
    return `<tr data-id="${esc(r.id)}" class="${S.openId === r.id ? 'sel' : ''}">` +
      S.order.map((k) => `<td class="${COLS[k][2]}">${COLS[k][3](r)}</td>`).join('') + '</tr>';
  }

  // ------------------------------------------------------------ note cells + pop-up editor
  function noteCell(r, field) {
    const v = r[field] || '';
    return `<button class="note-btn ${v ? '' : 'empty'}" data-note="${field}" data-stop title="${esc(v || (can('editor') ? 'Click to add' : ''))}">${v ? esc(v) : (can('editor') ? '+ add' : '')}</button>`;
  }
  let notePop = null;
  function closeNote(save) {
    if (!notePop) return;
    const { el, id, field, orig } = notePop;
    const val = el.querySelector('textarea').value;
    el.remove(); notePop = null;
    if (save && can('editor') && val !== orig) saveNote(id, field, val);
  }
  async function saveNote(id, field, val) {
    const row = S.byId.get(id); const prev = row ? row[field] : null;
    if (row) { row[field] = val; const tr = $(`#rows tr[data-id="${CSS.escape(id)}"]`); if (tr) tr.outerHTML = rowHtml(row); }
    try { await api('PATCH', '/api/leads', [{ id, [field]: val }]); toast('Saved'); }
    catch (e) { fail(e); if (row) { row[field] = prev; const tr = $(`#rows tr[data-id="${CSS.escape(id)}"]`); if (tr) tr.outerHTML = rowHtml(row); } }
  }
  function openNote(btn) {
    closeNote(true);
    const tr = btn.closest('tr[data-id]'); const id = tr.dataset.id; const field = btn.dataset.note;
    const row = S.byId.get(id) || {}; const orig = row[field] || '';
    const label = field === 'va_notes' ? 'VA notes' : 'Buyer notes';
    const el = document.createElement('div');
    el.className = 'note-pop';
    el.innerHTML = `<div class="note-pop-head"><b>${label}</b> <span class="muted mono">${esc(row.asin || '')}</span></div>
      <textarea rows="7" ${can('editor') ? '' : 'readonly'}>${esc(orig)}</textarea>
      <div class="note-pop-foot"><span class="muted">${can('editor') ? '⌘/Ctrl+Enter to save · Esc to cancel' : ''}</span>
      ${can('editor') ? '<button class="btn small" data-note-act="cancel">Cancel</button><button class="btn small primary" data-note-act="save">Save</button>' : '<button class="btn small" data-note-act="cancel">Close</button>'}</div>`;
    document.body.appendChild(el);
    const r = btn.getBoundingClientRect(); const w = Math.min(420, window.innerWidth - 24);
    el.style.width = w + 'px';
    el.style.left = Math.max(12, Math.min(r.left + window.scrollX, window.scrollX + window.innerWidth - w - 12)) + 'px';
    const below = r.bottom + 6 + 260 < window.innerHeight;
    el.style.top = (below ? r.bottom + window.scrollY + 6 : r.top + window.scrollY - 6 - el.offsetHeight) + 'px';
    notePop = { el, id, field, orig };
    const ta = el.querySelector('textarea'); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
  }
  document.addEventListener('mousedown', (e) => {
    if (notePop && !notePop.el.contains(e.target) && !e.target.closest('[data-note]')) closeNote(true);
  });
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-note]'); if (b) { e.preventDefault(); openNote(b); return; }
    const a = e.target.closest('[data-note-act]'); if (a) closeNote(a.dataset.noteAct === 'save');
  }, true);
  document.addEventListener('keydown', (e) => {
    if (!notePop) return;
    if (e.key === 'Escape') { e.stopPropagation(); closeNote(false); }
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); closeNote(true); }
  }, true);

  // ------------------------------------------------------------ inline entry row (like Airtable's blank row)
  const todayIso = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const NEW_INPUTS = {
    asin: () => `<input size="4" data-n="asin" class="mono" placeholder="ASIN" maxlength="10">`,
    store: () => `<input size="4" data-n="stores" list="dlStores" placeholder="Store">`,
    brand: () => `<input size="4" data-n="brands" list="dlBrands" placeholder="Brand">`,
    cost: () => `<input size="2" data-n="cost" class="num" inputmode="decimal" placeholder="0.00">`,
    promo: () => `<input size="4" data-n="promo_code" placeholder="Promo">`,
    by: () => `<select data-n="sourced_by"><option value=""></option>${(S.meta?.sourcers || []).map((x) => `<option>${esc(x)}</option>`).join('')}</select>`,
  };
  function renderNewRow() {
    const tb = $('#newrow'); if (!tb) return;
    if (!can('editor')) { tb.innerHTML = ''; return; }
    const keep = {}; $$('#newrow [data-n]').forEach((el) => { keep[el.dataset.n] = el.value; });
    // One line: date sits in the Product column; link, notes and the Add button fill the longest run of empty columns.
    const content = S.order.map((k) => k === 'product' ? `<input data-n="lead_date" type="date" style="width:150px">` : (NEW_INPUTS[k] ? NEW_INPUTS[k]() : null));
    let best = { start: -1, len: 0 };
    for (let i = 0, start = -1; i <= content.length; i++) {
      if (i < content.length && content[i] === null) { if (start < 0) start = i; }
      else if (start >= 0) { if (i - start > best.len) best = { start, len: i - start }; start = -1; }
    }
    const extras = `<div class="new-inline">
        <input size="4" data-n="store_link" type="url" placeholder="Store link" class="new-link" title="Store link">
        <input size="4" data-n="va_notes" placeholder="VA notes" class="new-notes">
        <button class="btn primary small" data-act="new-save">Add</button></div>`;
    let cells = '';
    for (let i = 0; i < content.length; i++) {
      const cls = COLS[S.order[i]][2];
      if (i === best.start) { cells += `<td colspan="${best.len}">${extras}</td>`; i += best.len - 1; continue; }
      cells += `<td class="${cls}">${content[i] ?? ''}</td>`;
    }
    const fallback = best.len ? '' : `<tr class="new-row new-more"><td colspan="${S.order.length}">${extras}</td></tr>`;
    tb.innerHTML = `<tr class="new-row"><td colspan="${S.order.length}" class="new-title"><b>New lead</b> <span class="muted">— type across, then press Enter. The product name is filled in from Keepa.</span></td></tr>
      <tr class="new-row new-main">${cells}</tr>${fallback}`;
    const defaults = { lead_date: todayIso(), sourced_by: 'Ariane' };
    $$('#newrow [data-n]').forEach((el) => { const v = keep[el.dataset.n] ?? defaults[el.dataset.n]; if (v != null) el.value = v; });
    S.newRowOrder = S.order.join();
  }
  async function saveNewRow() {
    const vals = {}; $$('#newrow [data-n]').forEach((el) => { if (el.value.trim() !== '') vals[el.dataset.n] = el.value.trim(); });
    if (!vals.asin && !vals.store_link) { toast('Enter at least an ASIN or a store link', true); $('#newrow [data-n="asin"]')?.focus(); return; }
    const btn = $('#newrow [data-act="new-save"]'); if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    try {
      const lead = await api('POST', '/api/leads', vals);
      // Keep date, store, brand and who sourced it for the next entry; clear the rest.
      ['asin', 'cost', 'promo_code', 'store_link', 'va_notes'].forEach((n) => { const el = $(`#newrow [data-n="${n}"]`); if (el) el.value = ''; });
      toast(`Added ${lead.asin || 'lead'}`);
      $('#newrow [data-n="asin"]')?.focus();
      await load();
      flashLead(lead.id);
    } catch (e) { fail(e); }
    finally { if (btn) { btn.disabled = false; btn.textContent = 'Add lead'; } }
  }
  function flashLead(id) {
    const tr = $(`#rows tr[data-id="${CSS.escape(id)}"]`); if (!tr) return;
    tr.classList.add('flash'); setTimeout(() => tr.classList.remove('flash'), 1500);
  }

  // ------------------------------------------------------------ drawer
  function closeDrawer() {
    S.openId = null; $('#scrim').classList.add('hidden'); $('#drawer').classList.add('hidden'); $('#drawer').innerHTML = '';
    if (location.hash) history.replaceState(null, '', location.pathname);
    $$('#rows tr.sel').forEach((tr) => tr.classList.remove('sel'));
  }

  async function openLead(id) {
    S.openId = id; history.replaceState(null, '', '#lead=' + id);
    $$('#rows tr').forEach((tr) => tr.classList.toggle('sel', tr.dataset.id === id));
    $('#scrim').classList.remove('hidden'); $('#drawer').classList.remove('hidden');
    $('#drawer').innerHTML = '<div class="drawer-body muted">Loading…</div>';
    try { renderDrawer(await api('GET', '/api/leads/' + id)); } catch (e) { fail(e); closeDrawer(); }
  }

  function field(label, key, val, opts = {}) {
    const dis = editable() && !opts.ro ? '' : 'disabled';
    const cls = opts.cls || '';
    if (opts.area) return `<label class="${opts.wide ? 'wide' : ''}">${label}<textarea data-k="${key}" rows="${opts.rows || 2}" ${dis}>${esc(val ?? '')}</textarea></label>`;
    return `<label ${opts.span ? `style="grid-column: span ${opts.span}"` : ''}>${label}<input data-k="${key}" class="${cls}" ${opts.type ? `type="${opts.type}"` : ''} ${opts.list ? `list="${opts.list}"` : ''} value="${esc(val ?? '')}" ${dis}></label>`;
  }

  function renderDrawer(l) {
    const link = l.store_link && /^https?:\/\//i.test(l.store_link) ? l.store_link : null;
    const sourcers = S.meta?.sourcers || [];
    $('#drawer').innerHTML = `
      <div class="drawer-head">
        <h2>${esc(l.asin || 'Lead')} <span class="muted" style="font-size:15px">${esc(fmtDate(l.lead_date))}</span></h2>
        ${link ? `<a class="btn small" href="${esc(link)}" target="_blank" rel="noopener">Open store ↗</a>` : ''}
        ${l.asin ? `<a class="btn small" href="https://www.amazon.com/dp/${esc(l.asin)}" target="_blank" rel="noopener">Amazon ↗</a>` : ''}
        ${editable() && (isHome() || !l.airtable_id) ? `<span id="delWrap"><button class="btn small danger" data-act="del">Delete</button></span>` : ''}
        <button class="icon-btn" data-act="close" style="font-size:20px">✕</button>
      </div>
      <div class="drawer-body">
        <div class="grid">
          ${field('Date', 'lead_date', l.lead_date, { type: 'date' })}
          ${field('ASIN', 'asin', l.asin, { cls: 'mono' })}
          <label>Sourced by<select data-k="sourced_by" ${editable() ? '' : 'disabled'}><option value=""></option>${sourcers.concat(l.sourced_by && !sourcers.includes(l.sourced_by) ? [l.sourced_by] : []).map((s) => `<option ${s === l.sourced_by ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select></label>
          ${field('Product', 'product_name', l.product_name, { area: true, wide: true, rows: 2 })}
          ${field('Store(s), comma-separated', 'stores', (l.stores || []).join(', '), { list: 'dlStores', span: 2 })}
          ${field('Cost / unit', 'cost', l.cost == null ? '' : Number(l.cost).toFixed(2), { cls: 'num' })}
          ${field('Brand(s), comma-separated', 'brands', (l.brands || []).join(', '), { list: 'dlBrands', span: 2 })}
          ${field('Promo code', 'promo_code', l.promo_code)}
          ${field('Store link', 'store_link', l.store_link, { span: 3 })}
          ${field('Buy qty', 'buy_qty', l.buy_qty, { cls: 'num' })}
          ${field('Qty ordered', 'qty_ordered', l.qty_ordered, { cls: 'num' })}
          <label>Left to buy<input value="${esc(l.left_to_buy ?? '')}" class="num" disabled></label>
          ${field('VA notes', 'va_notes', l.va_notes, { area: true, wide: true })}
          ${field('Buyer notes', 'buyer_notes', l.buyer_notes, { area: true, wide: true })}
          ${field('Feedback', 'feedback', l.feedback, { area: true, wide: true })}
        </div>
        <div class="stats">
          <div class="stat"><div class="k">Expected spend</div><div class="v">${money(l.expected_spend)}</div></div>
          <div class="stat"><div class="k">Actual spend</div><div class="v">${money(l.actual_spend)}</div></div>
          <div class="stat"><div class="k">Bonus</div><div class="v">${money(l.bonus)}</div></div>
          <div class="stat"><div class="k">Potential bonus</div><div class="v">${money(l.potential_bonus)}</div></div>
        </div>

        <div class="section-title">Lead Score ${band(l.lead_score_band)} <span style="text-transform:none;letter-spacing:0;font-weight:400">${l.lead_score_roi != null ? 'predicted ROI ' + pct(l.lead_score_roi) : ''}</span>
          ${editable() ? `<button class="btn small" data-act="score">Score now</button>` : ''}</div>
        ${l.lead_score_notes ? `<pre class="notes">${esc(l.lead_score_notes)}</pre>` : '<p class="muted">Not scored yet.</p>'}

        <div class="section-title">Claude buy qty <span style="text-transform:none;letter-spacing:0;font-weight:400">${l.claude_buy_qty ?? '—'}</span></div>
        ${l.claude_buy_note ? `<pre class="notes">${esc(l.claude_buy_note)}</pre>` : '<p class="muted">No suggestion recorded.</p>'}

        <div class="section-title">Other leads for this ASIN</div>
        ${(l.same_asin || []).length ? `<div class="items"><table><thead><tr><th>Date</th><th>Store</th><th class="num">Cost</th><th class="num">Buy</th><th class="num">Ordered</th><th>By</th><th>Buyer notes</th></tr></thead><tbody>
          ${l.same_asin.map((x) => `<tr data-open="${esc(x.id)}" style="cursor:pointer"><td>${esc(fmtDate(x.lead_date))}</td><td>${esc((x.stores || []).join(', '))}</td><td class="num">${money(x.cost)}</td><td class="num">${num(x.buy_qty)}</td><td class="num">${num(x.qty_ordered)}</td><td>${esc(x.sourced_by || '')}</td><td class="muted">${esc(x.buyer_notes || '')}</td></tr>`).join('')}
          </tbody></table></div>` : '<p class="muted">None.</p>'}

        ${Object.keys(l.extra || {}).length ? `<div class="section-title">Other Airtable columns</div><div class="muted" style="font-size:13px">${Object.entries(l.extra).map(([k, v]) => `<div><b>${esc(k)}:</b> ${esc(Array.isArray(v) ? v.join(', ') : v)}</div>`).join('')}</div>` : ''}

        <div class="section-title">History</div>
        <div class="history" id="hist"><span class="muted">Loading…</span></div>
        <p class="muted" style="font-size:12px;margin-top:14px">${l.airtable_id ? 'Airtable record ' + esc(l.airtable_id) : 'Added here'}</p>
      </div>`;
    api('GET', '/api/activity?limit=40&entity_id=' + encodeURIComponent(l.id)).then(({ changes }) => {
      const el = $('#hist'); if (!el || S.openId !== l.id) return;
      el.innerHTML = changes.length ? '<ul>' + changes.map(LG.changeLine).join('') + '</ul>' : `<span class="muted">${isHome() ? 'No changes recorded here yet.' : 'Changes are made in Airtable until the switch-over.'}</span>`;
    }).catch(() => {});
  }

  async function saveField(el) {
    const k = el.dataset.k; if (!S.openId || !k) return;
    if (el.dataset.orig === el.value) return;
    try { await api('PATCH', '/api/leads', [{ id: S.openId, [k]: el.value }]); }
    catch (e) { fail(e); el.value = el.dataset.orig || ''; }
  }

  function newLead() {
    S.openId = null; $('#scrim').classList.remove('hidden'); $('#drawer').classList.remove('hidden');
    const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    $('#drawer').innerHTML = `<div class="drawer-head"><h2>New lead</h2><button class="icon-btn" data-act="close" style="font-size:20px">✕</button></div>
      <div class="drawer-body"><form id="newForm" class="grid">
        <label>Date<input type="date" name="lead_date" value="${today}"></label>
        <label>ASIN<input name="asin" class="mono" required></label>
        <label>Sourced by<select name="sourced_by"><option value=""></option>${(S.meta.sourcers || []).map((s) => `<option>${esc(s)}</option>`).join('')}</select></label>
        <label class="wide">Product<input name="product_name"></label>
        <label style="grid-column: span 2">Store<input name="stores" list="dlStores"></label>
        <label>Cost / unit<input name="cost" class="num" inputmode="decimal"></label>
        <label style="grid-column: span 2">Brand<input name="brands" list="dlBrands"></label>
        <label>Promo code<input name="promo_code"></label>
        <label class="wide">Store link<input name="store_link" type="url"></label>
        <label>Buy qty<input name="buy_qty" class="num" inputmode="numeric"></label>
        <label>Qty ordered<input name="qty_ordered" class="num" inputmode="numeric"></label>
        <span></span>
        <label class="wide">VA notes<textarea name="va_notes" rows="2"></textarea></label>
        <div class="wide" style="display:flex;gap:8px;justify-content:flex-end"><button type="button" class="btn" data-act="close">Cancel</button><button class="btn primary">Save lead</button></div>
      </form><p class="muted">It's scored automatically once it has an ASIN, cost and store link.</p></div>`;
    $('#newForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = Object.fromEntries([...new FormData(e.target).entries()].filter(([, v]) => v !== ''));
      try { const l = await api('POST', '/api/leads', body); toast('Lead saved'); await load(); openLead(l.id); } catch (err) { fail(err); }
    });
  }

  // ------------------------------------------------------------ live
  async function onLive(m) {
    if (m.t === 'store') { loadMeta(); return; }
    if (m.t !== 'lead') return;
    if (m.op === 'mode') { await loadMeta(); await load(); return; }
    if (m.op === 'reload') { await load(); loadMeta(); return; }
    if (m.op === 'delete') { await load(); if (m.ids?.includes(S.openId)) closeDrawer(); return; }
    if (m.op === 'upsert') {
      let changedVisible = false;
      for (const id of m.ids || []) {
        if (S.byId.has(id)) {
          try {
            const fresh = await api('GET', '/api/leads/' + id);
            const i = S.rows.findIndex((r) => r.id === id); if (i >= 0) S.rows[i] = { ...S.rows[i], ...fresh };
            S.byId.set(id, S.rows[i]); changedVisible = true;
            const tr = $(`tr[data-id="${CSS.escape(id)}"]`); if (tr) { tr.outerHTML = rowHtml(S.rows[i]); const n = $(`tr[data-id="${CSS.escape(id)}"]`); n.classList.add('flash'); setTimeout(() => n.classList.remove('flash'), 1200); }
            if (S.openId === id) { const a = document.activeElement; if (!(a && $('#drawer').contains(a) && /INPUT|TEXTAREA|SELECT/.test(a.tagName))) renderDrawer(fresh); }
          } catch { /* gone */ }
        }
      }
      if (!changedVisible && S.offset === 0 && S.sort === 'date') await load();   // probably a new lead at the top
      else { const p = new URLSearchParams({ ...filters(), limit: 1 }); api('GET', '/api/leads?' + p).then((r) => { S.totals = r.totals; render(); }); }
      if (m.src !== 'airtable' && m.by && m.by !== (LG.st.me?.name || LG.st.me?.email)) toast(`${m.by} updated a lead`);
    }
  }

  // ------------------------------------------------------------ events
  document.addEventListener('click', async (e) => {
    const t = e.target;
    if (t.closest('[data-stop]')) return;   // ASIN / store links open in a new tab
    if (t.closest('[data-act="reset-cols"]')) { S.order = DEFAULT_ORDER.slice(); saveOrder(); render(); toast('Columns reset'); return; }
    const th = t.closest('th[data-sort]');
    if (th && !S.justDragged) { const k = th.dataset.sort; if (S.sort === k) { if (S.desc === !['asin'].includes(k)) S.desc = !S.desc; else { S.sort = 'date'; S.desc = true; } } else { S.sort = k; S.desc = !['asin'].includes(k); } load().catch(fail); return; }
    const tr = t.closest('#rows tr[data-id]'); if (tr) { openLead(tr.dataset.id); return; }
    if (t.closest('#rows tr.day-row')) return;
    const o = t.closest('[data-open]'); if (o) { openLead(o.dataset.open); return; }
    if (t.id === 'scrim') { closeDrawer(); return; }
    const b = t.closest('[data-act]'); if (!b) return;
    try {
      switch (b.dataset.act) {
        case 'close': closeDrawer(); break;
        case 'new-save': await saveNewRow(); break;
        case 'sync': { b.disabled = true; const r = await api('POST', '/api/leads-admin/sync-now'); toast(`Synced: ${r.changed} changed`); await loadMeta(); b.disabled = false; break; }
        case 'home':
          $('#homeWrap').innerHTML = `<span class="confirm"><span>Copy the latest from Airtable one last time and edit leads here from now on? Turn off the Airtable automations and have your VA use this page afterwards.</span>
            <button class="btn small" data-act="home-no">Not yet</button><button class="btn small primary" data-act="home-yes">Switch over</button></span>`;
          break;
        case 'home-no': renderBanner(); break;
        case 'home-yes': { b.disabled = true; b.textContent = 'Copying…'; const r = await api('POST', '/api/leads-admin/make-home'); toast('Leads now live here' + (r.final_sync ? ` (final copy: ${r.final_sync.changed} updated)` : '')); await loadMeta(); await load(); break; }
        case 'score': { b.disabled = true; b.textContent = 'Scoring… (10–30 s)'; const r = await api('POST', `/api/leads/${S.openId}/score`); toast(r.ok ? `Band ${r.band}` : `Not scored: ${r.why}`, !r.ok); b.disabled = false; b.textContent = 'Score now'; break; }
        case 'del': $('#delWrap').innerHTML = `<span class="confirm"><button class="btn small" data-act="del-no">Keep</button><button class="btn small danger" data-act="del-yes">Delete lead</button></span>`; break;
        case 'del-no': $('#delWrap').innerHTML = `<button class="btn small danger" data-act="del">Delete</button>`; break;
        case 'del-yes': await api('DELETE', '/api/leads/' + S.openId); closeDrawer(); toast('Lead deleted'); break;
      }
    } catch (err) { fail(err); }
  });
  let dragCol = null;
  const head = () => $('#head');
  document.addEventListener('dragstart', (e) => { const th = e.target.closest?.('#head th[data-col]'); if (!th) return; dragCol = th.dataset.col; th.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', dragCol); } catch { /* ignore */ } });
  document.addEventListener('dragover', (e) => {
    const th = e.target.closest?.('#head th[data-col]'); if (!th || !dragCol) return;
    e.preventDefault(); $$('#head th').forEach((x) => x.classList.remove('drop-before', 'drop-after'));
    const rect = th.getBoundingClientRect(); th.classList.add(e.clientX < rect.left + rect.width / 2 ? 'drop-before' : 'drop-after');
  });
  document.addEventListener('drop', (e) => {
    const th = e.target.closest?.('#head th[data-col]'); if (!th || !dragCol) return;
    e.preventDefault();
    const target = th.dataset.col; const before = th.classList.contains('drop-before');
    if (target !== dragCol) {
      const order = S.order.filter((k) => k !== dragCol);
      order.splice(order.indexOf(target) + (before ? 0 : 1), 0, dragCol);
      S.order = order; saveOrder(); render();
    }
  });
  document.addEventListener('dragend', () => {
    dragCol = null; $$('#head th').forEach((x) => x.classList.remove('dragging', 'drop-before', 'drop-after'));
    S.justDragged = true; setTimeout(() => { S.justDragged = false; }, 50);
  });
  document.addEventListener('focusin', (e) => { if (e.target.matches?.('#drawer [data-k]')) e.target.dataset.orig = e.target.value; });
  document.addEventListener('change', (e) => { if (e.target.matches('#drawer [data-k]')) saveField(e.target); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#drawer').classList.contains('hidden')) closeDrawer();
    if (e.key === 'Enter' && e.target.matches('#drawer input[data-k]')) e.target.blur();
    if (e.key === 'Enter' && e.target.matches('#newrow [data-n]')) { e.preventDefault(); saveNewRow(); }
  });
  let qt; const reload = () => { clearTimeout(qt); qt = setTimeout(() => load().catch(fail), 200); };
  ['q', 'store', 'brand'].forEach((id) => $('#' + id).addEventListener('input', reload));
  ['sourced', 'band', 'ordered', 'left', 'unscored'].forEach((id) => $('#' + id).addEventListener('change', reload));
  $$('#range .chip').forEach((c) => c.addEventListener('click', () => { $$('#range .chip').forEach((x) => x.classList.toggle('on', x === c)); S.days = Number(c.dataset.days); reload(); }));
  $('#clear').addEventListener('click', () => { ['q', 'store', 'brand', 'sourced', 'band'].forEach((id) => { $('#' + id).value = ''; }); ['ordered', 'left', 'unscored'].forEach((id) => { $('#' + id).checked = false; }); reload(); });
  $('#moreBtn').addEventListener('click', () => { S.offset += PAGE; load(true).catch(fail); });
  $('#addBtn').addEventListener('click', newLead);

  S.order = loadOrder();
  (async () => {
    await LG.header();
    try {   // coming from the Stores & brands page
      const pre = JSON.parse(sessionStorage.getItem('leadFilter') || 'null');
      if (pre) { sessionStorage.removeItem('leadFilter'); if (pre.store) $('#store').value = pre.store; if (pre.brand) $('#brand').value = pre.brand;
        S.days = 0; $$('#range .chip').forEach((x) => x.classList.toggle('on', x.dataset.days === '0')); }
    } catch { /* storage blocked */ }
    await loadMeta(); await load();
    LG.live(onLive);
    const m = location.hash.match(/^#lead=(.+)$/); if (m) openLead(m[1]);
    setInterval(() => { if (!isHome()) loadMeta().catch(() => {}); }, 60000);
  })().catch(fail);
})();
