(() => {
  'use strict';
  const { $, $$, esc, money, num, pct, fmtDate, api, toast, fail, can } = LG;
  const PAGE = 200;
  const S = { hl: null, sel: new Set(), meta: null, rows: [], byId: new Map(), totals: null, dayTotals: {}, sort: 'date', desc: true, offset: 0, days: 30, openId: null, order: [] };

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
    asin: ['ASIN', null, 'tight', (r) => r.asin ? `<a class="asin-btn" href="https://www.amazon.com/dp/${esc(r.asin)}" target="_blank" rel="noopener" data-stop title="Open on Amazon">${esc(r.asin)}</a>${hazBadge(r)}` : '<span class="muted">—</span>'],
    product: ['Product', null, 'prod', (r) => `<span>${esc(r.product_name || '')}</span>`],
    store: ['Store', null, 'tags', storeCell],
    brand: ['Brand', null, 'tags', (r) => (r.brands || []).length ? `<span class="pill-list">${r.brands.map((b) => `<span>${esc(b)}</span>`).join('')}</span>` : '<span class="muted">—</span>'],
    cost: ['Cost', 'cost', 'num', (r) => money(r.cost)],
    buy_qty: ['Buy qty', 'buy_qty', 'num', (r) => numEditCell(r, 'buy_qty')],
    qty_ordered: ['Ordered', 'qty_ordered', 'num', (r) => numEditCell(r, 'qty_ordered')],
    left: ['Left', 'left', 'num', (r) => num(r.left_to_buy)],
    spend: ['Spend', 'spend', 'num', (r) => r.actual_spend ? money(r.actual_spend, 0) : '—'],
    band: ['Band', 'band', 'center', (r) => band(r.lead_score_band)],
    roi: ['Pred ROI', 'roi', 'num', (r) => pct(r.lead_score_roi)],
    claude_qty: ['Claude qty', 'claude_qty', 'num', (r) => num(r.claude_buy_qty)],
    by: ['By', null, 'tight', (r) => esc(r.sourced_by || '')],
    promo: ['Promo', null, 'promo', (r) => r.promo_code ? `<span class="promo-code">${esc(r.promo_code)}</span>` : '<span class="muted">—</span>'],
    va_notes: ['VA notes', null, 'note-cell', (r) => noteCell(r, 'va_notes')],
    buyer_notes: ['Buyer notes', null, 'note-cell', (r) => noteCell(r, 'buyer_notes')],
    feedback: ['Feedback', null, 'note-cell', (r) => noteCell(r, 'feedback')],
  };
  const DEFAULT_ORDER = ['product', 'asin', 'store', 'brand', 'cost', 'promo', 'va_notes', 'buyer_notes', 'feedback', 'buy_qty', 'qty_ordered', 'left', 'spend', 'band', 'roi', 'claude_qty', 'by'];
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
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#entryOrder')) return;
    S.sort = 'date'; S.desc = true; saveSort(); closeHeaderFilter(); load().catch(fail);
  });
  function saveSort() { try { localStorage.setItem('leadSort', JSON.stringify({ sort: S.sort, desc: S.desc })); } catch { /* ignore */ } }
  try { const v = JSON.parse(localStorage.getItem('leadSort') || 'null'); if (v && v.sort) { S.sort = v.sort; S.desc = !!v.desc; } } catch { /* ignore */ }
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
    $('#summary') && ($('#summary').textContent = `${num(t.n)} leads match · ${num(t.bought)} bought`);
    const selOk = canOrder();
    const eo = $('#entryOrder'); if (eo) { const on = S.sort === 'date' && S.desc; eo.classList.toggle('on', on); eo.disabled = on; }
    $('#head').innerHTML = (selOk ? `<th class="sel-col"><input type="checkbox" id="selAll" title="Select all shown" ${S.rows.length && S.rows.every((r) => S.sel.has(r.id)) ? 'checked' : ''}></th>` : '') + S.order.map((k) => {
      const [label, sk, cls] = COLS[k];
      const fk = HEADER_FILTERS[k];
      const fval = fk ? $('#' + fk.input).value : '';
      const arrow = (sk && S.sort === sk) || (fk && S.sort === k) ? `<span class="arrow">${S.desc ? '▼' : '▲'}</span>` : '';
      const ftag = fk ? `<span class="hf-btn ${fval ? 'on' : ''}" aria-label="Filter">${FUNNEL(!!fval)}</span>` : '';
      const attrs = fk ? `data-hfilter="${k}"` : (sk ? `data-sort="${sk}"` : '');
      const tip = fk ? (fval ? `Filtered: ${fk.show(fval)} (click to change)` : 'Click to filter' + (sk ? ' or sort' : '')) : (sk ? 'Click to sort' : '');
      return `<th draggable="true" data-col="${k}" class="${cls} ${fk || sk ? 'sortable' : ''} ${fval ? 'hf-on' : ''}" ${attrs} title="${tip}; drag to move">${label}${arrow}${ftag}</th>`;
    }).join('') + '<th class="row-act" aria-label="Delete"></th>';
    let html = '', lastDay = null;
    for (const r of S.rows) {
      const day = r.lead_date || '';
      if (day !== lastDay) { html += dayHtml(day); lastDay = day; }
      html += rowHtml(r);
    }
    $('#rows').innerHTML = html;
    updateSelBar();
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
    const cs = t.claude_n ? `<span class="day-claude" title="Sum of Claude buy qty × cost for this day's leads">Claude suggested spend <b>${money(t.claude_spend, 0)}</b></span>` : '';
    return `<tr class="day-row"><td colspan="${S.order.length + 1 + (canOrder() ? 1 : 0)}">${cs}<b>${esc(label)}</b><span class="muted"> · ${bits.join(' · ')}</span></td></tr>`;
  }

  function rowHtml(r) {
    return `<tr data-id="${esc(r.id)}" class="${S.openId === r.id ? 'sel' : ''} ${S.sel.has(r.id) ? 'picked' : ''} ${S.hl === r.id ? 'hl' : ''}">` +
      (canOrder() ? `<td class="sel-col" data-stop><input type="checkbox" data-pick="${esc(r.id)}" ${S.sel.has(r.id) ? 'checked' : ''} aria-label="Select lead"></td>` : '') +
      S.order.map((k) => `<td class="${COLS[k][2]}" data-k="${k}">${COLS[k][3](r)}</td>`).join('') +
      `<td class="row-act">${can('editor') ? `<button class="icon-act danger" data-del="${esc(r.id)}" data-stop title="Delete lead" aria-label="Delete lead">${TRASH}</button>` : ''}</td></tr>`;
  }

  // ------------------------------------------------------------ row delete with confirmation
  const TRASH = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>`;
  // ------------------------------------------------------------ hazmat warning (flags from Keepa, cached per ASIN)
  function hazBadge(r) {
    const dup = (r.same_day_n || 0) > 1, bad = r.fba_eligible === false, tp = !!r.transparency;
    if (bad || dup || tp) {
      const tip = [tp ? 'Enrolled in Transparency: units need Transparency codes' : '', bad ? "Amazon says you can't send this to FBA right now" : '', dup ? `Entered ${r.same_day_n} times on this day` : ''].filter(Boolean).join(' · ');
      return `<button class="hz-badge ${bad || tp ? '' : 'warn'}" data-hz="${esc(r.asin)}" data-hz-day="${esc(r.lead_date || '')}" data-stop title="${esc(tip)}. Click for details." aria-label="Warnings">${tp && !bad ? 'T' : dup && !bad ? r.same_day_n + '×' : '!'}</button>`;
    }
    if (r.fba_eligible === true) return `<span class="ok-badge" title="Amazon says you can send this to FBA${r.fba_checked_at ? ' (checked ' + new Date(r.fba_checked_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ')' : ''}" aria-label="Can sell via FBA">✓</span>`;
    return '';
  }
  async function hazPopup(asin, intro, day) {
    let f = null, tp = null; try { const h = await api('GET', '/api/hazmat/' + encodeURIComponent(asin)); f = h.flag; tp = h.transparency; } catch { /* show the general text */ }
    let same = [];
    if (day) { try { same = (await api('GET', '/api/leads?' + new URLSearchParams({ asin, date_from: day, date_to: day, sort: 'date', desc: 'false', limit: 50 }))).leads; } catch { /* skip */ } }
    const bad = f?.fba_eligible === false, dup = same.length > 1;
    const nWarn = [bad, dup, !!tp].filter(Boolean).length;
    const shortD = (d) => d ? new Date(d + 'T12:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '';
    const reasons = f?.reason_text || [];
    const KEEP = ['transportation_regulatory_class', 'united_nations_regulatory_id', 'proper_shipping_name'];
    const kinfo = (f?.hazmat_info || []).filter((x) => KEEP.includes(x.aspect)).map((x) => `${esc(String(x.aspect).replace(/_/g, ' '))}: ${esc(x.value)}`).join(' · ');
    const wrap = document.createElement('div'); wrap.className = 'confirm-back';
    wrap.innerHTML = `<div class="confirm-box hz-box" role="alertdialog" aria-modal="true" aria-labelledby="hzT">
      <h3 id="hzT"><span class="hz-icon ${bad || tp ? '' : 'warn'}">!</span> ${nWarn > 1 ? nWarn + ' warnings' : tp ? 'Enrolled in Transparency' : bad ? (f?.hazmat_reason ? "Hazmat: can't go to FBA yet" : "You can't send this to FBA right now") : 'Entered more than once today'}</h3>
      ${intro ? `<p>${intro}</p>` : ''}
      <p><a class="mono" href="https://www.amazon.com/dp/${esc(asin)}" target="_blank" rel="noopener">${esc(asin)}</a>${f?.title ? ` · ${esc(f.title)}` : ''}</p>
      ${tp ? `<div class="hz-sec"><p><b>Amazon told you this ASIN is enrolled in Transparency.</b> Every unit needs the brand's Transparency (or serial number) code from the manufacturer. Units without one are marked unfulfillable at the warehouse, so <b>don't buy it to send to FBA</b>.</p>
        <ul class="hz-reasons">${(tp.emails || []).map((m) => `<li>${m.kind === 'rejected' ? `<b>${num(m.units_rejected)} units rejected</b> at Amazon warehouses (email of ${esc(shortD(m.email_date))})` : `Enrollment notice emailed ${esc(shortD(m.email_date))}${m.effective_date ? `; units without codes can't be sold after ${esc(shortD(m.effective_date))}` : ''}`}</li>`).join('')}</ul></div>` : ''}
      ${dup ? `<div class="hz-sec"><p><b>This ASIN was entered ${same.length} times on ${esc(fmtDate(day))}.</b> Check you're not buying the same thing twice (it can be fine if it's from different stores).</p>
        <table class="hz-dups"><thead><tr><th>Added</th><th>Store</th><th class="num">Cost</th><th class="num">Buy qty</th><th class="num">Ordered</th><th>By</th></tr></thead><tbody>
        ${same.map((x) => `<tr><td>${esc(new Date(x.created_at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }))}</td><td>${esc((x.stores || []).join(', ') || '—')}</td><td class="num">${x.cost == null ? '—' : money(x.cost)}</td><td class="num">${num(x.buy_qty)}</td><td class="num">${num(x.qty_ordered)}</td><td>${esc(x.sourced_by || '')}</td></tr>`).join('')}
        </tbody></table></div>` : ''}
      ${!bad ? (f?.fba_eligible === true && !tp ? '<p class="hz-ok">✓ Amazon says you can send this to FBA.</p>' : '') : `<div class="hz-sec">
      <p>Amazon's eligibility check for your seller account says this ASIN <b>isn't eligible for FBA</b>. Amazon's reason${reasons.length === 1 ? '' : 's'}:</p>
      <ul class="hz-reasons">${reasons.length ? reasons.map((x) => `<li>${x.text ? esc(x.text) : 'Amazon gave a code without details.'} <span class="muted mono">${esc(x.code)}</span></li>`).join('') : '<li>No reason given.</li>'}</ul>
      ${f?.hazmat_reason ? `<p>To fix it, upload a <b>safety data sheet (SDS) or exemption sheet</b> in Seller Central (Dangerous Goods document upload). Reviews take days to weeks and can be refused, and the stock can't be sent in until it's cleared.</p>` : ''}
      <p class="muted" style="font-size:13px">Before buying: make sure you can clear this with Amazon, or plan to sell it merchant-fulfilled.</p>
      <p class="muted" style="font-size:12px">Checked with Amazon ${f?.fba_checked_at ? esc(new Date(f.fba_checked_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })) : ''} (re-checked every 2 weeks)${f?.hazmat ? ` · Keepa lists it as hazmat${kinfo ? ': ' + kinfo : ''}` : ''}.</p></div>`}
      <div class="confirm-actions"><button class="btn primary" data-cf="no">Got it</button></div></div>`;
    document.body.appendChild(wrap);
    const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey, true); };
    const onKey = (e) => { if (e.key === 'Escape' || e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); close(); } };
    document.addEventListener('keydown', onKey, true);
    wrap.querySelector('[data-cf="no"]').focus();
    wrap.addEventListener('click', (e) => { if (e.target === wrap || e.target.closest('[data-cf="no"]')) close(); });
  }
  document.addEventListener('click', (e) => { const b = e.target.closest('[data-hz]'); if (b) { e.preventDefault(); e.stopPropagation(); hazPopup(b.dataset.hz, '', b.dataset.hzDay || ''); } }, true);
  function hazAfterAdd(lead) {
    if (!lead || !lead.asin) return;
    const bad = lead.fba_eligible === false, dup = (lead.same_day_n || 0) > 1;
    if (bad || dup || lead.transparency) hazPopup(lead.asin, `Check the lead you just added (<b>${esc(lead.asin)}</b>) before buying.`, lead.lead_date);
  }

  // ------------------------------------------------------------ select rows -> create an order
  function canOrder() { const me = LG.st.me; return !!me && ['owner', 'editor'].includes(me.role) && (me.tabs || []).includes('orders'); }
  let lastPick = null;
  function updateSelBar() {
    const n = S.sel.size, bar = $('#selBar'); if (!bar) return;
    bar.hidden = !canOrder() || n === 0;
    $('#selCount').textContent = `${n} selected`;
    const all = $('#selAll'); if (all) { const shown = S.rows.filter((r) => S.sel.has(r.id)).length; all.checked = shown > 0 && shown === S.rows.length; all.indeterminate = shown > 0 && shown < S.rows.length; }
  }
  function setPick(id, on) {
    if (on) S.sel.add(id); else S.sel.delete(id);
    const tr = $(`#rows tr[data-id="${CSS.escape(id)}"]`);
    if (tr) { tr.classList.toggle('picked', on); const cb = tr.querySelector('[data-pick]'); if (cb) cb.checked = on; }
  }
  document.addEventListener('click', (e) => {
    const cb = e.target.closest('input[data-pick]');
    if (cb) {
      e.stopPropagation();
      const id = cb.dataset.pick;
      if (e.shiftKey && lastPick && lastPick !== id) {   // shift-click selects the run between
        const ids = S.rows.map((r) => r.id); const a = ids.indexOf(lastPick), b = ids.indexOf(id);
        if (a >= 0 && b >= 0) ids.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((x) => setPick(x, cb.checked));
      } else setPick(id, cb.checked);
      lastPick = id; updateSelBar(); return;
    }
    if (e.target.id === 'selAll') { const on = e.target.checked; S.rows.forEach((r) => setPick(r.id, on)); updateSelBar(); return; }
    if (e.target.closest('#selClear')) { [...S.sel].forEach((id) => setPick(id, false)); S.sel.clear(); updateSelBar(); return; }
    if (e.target.closest('#selOrder')) { createOrderDialog().catch(fail); }
  }, true);

  async function createOrderDialog() {
    const leads = [...S.sel].map((id) => S.byId.get(id)).filter(Boolean);
    if (!leads.length) { toast('Select some leads first', true); return; }
    let meta = { stores: [], emails: [], set_types: [] };
    try { meta = await api('GET', '/api/meta'); } catch { /* suggestions only */ }
    const storeCount = new Map();
    leads.forEach((l) => { const s0 = (l.stores || [])[0]; if (s0) storeCount.set(s0, (storeCount.get(s0) || 0) + 1); });
    const store = [...storeCount].sort((a, b) => b[1] - a[1])[0]?.[0] || '';
    const stores = [...storeCount.keys()];
    const today = todayIso();
    const itemRow = (l) => `<tr data-lead="${esc(l.id)}">
      <td class="c-asin"><input class="mono" data-f="asin" value="${esc(l.asin || '')}" maxlength="10"></td>
      <td><input data-f="product_title" value="${esc(l.product_name || '')}" placeholder="Title"></td>
      <td class="c-shade"><input data-f="shade" placeholder="Shade"></td>
      <td class="c-set"><input data-f="set_type" list="coSets" placeholder="Set type"></td>
      <td class="c-qty"><input data-f="qty_ordered" class="num" inputmode="numeric" required pattern="[0-9]*[1-9][0-9]*" title="Units ordered"></td>
      <td class="c-cost"><input data-f="cost_per_unit" class="num" inputmode="decimal" placeholder="0.00"></td>
      <td><button type="button" class="icon-btn" data-co-drop title="Remove this item">✕</button></td></tr>`;
    const wrap = document.createElement('div'); wrap.className = 'confirm-back co-back';
    wrap.innerHTML = `<form class="confirm-box co-box" role="dialog" aria-modal="true" aria-labelledby="coT">
      <div style="display:flex;align-items:center;gap:10px"><h3 id="coT" style="flex:1;margin:0">New order <span class="muted" style="font-size:16px">from ${leads.length} lead${leads.length === 1 ? '' : 's'}</span></h3>
        <button type="button" class="icon-btn" data-co-close title="Close (Esc)" style="font-size:20px">✕</button></div>
      ${stores.length > 1 ? `<p class="recon bad" style="margin:10px 0 0">These leads come from ${stores.length} stores (${stores.map(esc).join(', ')}). One order is one store checkout, so check the store below.</p>` : ''}
      <div class="grid two" style="margin-top:14px">
        <label>Date<input type="date" name="order_date" value="${today}" required></label>
        <label>Store<input name="store" list="coStores" value="${esc(store)}" required></label>
        <label>Email<input name="email" list="coEmails" value="${esc(meta.emails?.[0] || '')}"></label>
        <label>Order total<input name="order_total" class="num" inputmode="decimal" placeholder="0.00"></label>
        <label class="wide">Order ID<input name="order_ref" class="mono" placeholder="Order ID"></label>
        <label class="wide">Notes<textarea name="notes" rows="2" placeholder="Notes"></textarea></label>
      </div>
      <div class="section-title">Items</div>
      <div class="items"><table><thead><tr><th>ASIN</th><th>Title</th><th>Shade</th><th>Set type</th><th class="num">Qty</th><th class="num">Cost/unit</th><th></th></tr></thead>
        <tbody id="coItems">${leads.map(itemRow).join('')}</tbody></table></div>
      <datalist id="coStores">${[...new Set([...stores, ...(meta.stores || [])])].map((x) => `<option value="${esc(x)}">`).join('')}</datalist>
      <datalist id="coEmails">${(meta.emails || []).map((x) => `<option value="${esc(x)}">`).join('')}</datalist>
      <datalist id="coSets">${(meta.set_types || []).map((x) => `<option value="${esc(x)}">`).join('')}</datalist>
      <div class="confirm-actions"><button type="button" class="btn" data-co-close>Cancel</button><button class="btn primary" type="submit">Create order</button></div>
    </form>`;
    document.body.appendChild(wrap);
    const form = wrap.querySelector('form');
    const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey, true); };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    document.addEventListener('keydown', onKey, true);
    form.querySelector('[name="order_total"]').focus();
    wrap.addEventListener('click', (e) => {
      if (e.target === wrap || e.target.closest('[data-co-close]')) { close(); return; }
      const d = e.target.closest('[data-co-drop]'); if (d) { d.closest('tr').remove(); if (!$('#coItems tr')) close(); }
    });
    form.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') e.preventDefault(); });   // Enter doesn't submit by accident
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(form).entries());
      const rows = $$('#coItems tr').map((tr) => ({ lead: tr.dataset.lead, it: Object.fromEntries($$('input', tr).map((i) => [i.dataset.f, i.value.trim()])) }))
        .filter((x) => x.it.asin || x.it.product_title);
      body.items = rows.map((x) => ({ ...Object.fromEntries(Object.entries(x.it).filter(([, v]) => v !== '')), lead_id: x.lead }));   // the server adds each qty to its lead's Ordered
      if (!body.items.length) { toast('Add at least one item', true); return; }
      const btn = form.querySelector('[type="submit"]'); btn.disabled = true; btn.textContent = 'Creating…';
      try {
        const o = await api('POST', '/api/orders', body);
        close(); S.sel.clear();
        location.href = '/#order=' + encodeURIComponent(o.id);   // open it on the Orders page
      } catch (err) { btn.disabled = false; btn.textContent = 'Create order'; fail(err); }
    });
  }

  function confirmDelete(id) {
    const r = S.byId.get(id) || {};
    const wrap = document.createElement('div'); wrap.className = 'confirm-back';
    wrap.innerHTML = `<div class="confirm-box" role="alertdialog" aria-modal="true" aria-labelledby="cfT">
      <h3 id="cfT">Delete this lead?</h3>
      <p><b class="mono">${esc(r.asin || '(no ASIN)')}</b> · ${esc(fmtDate(r.lead_date))}<br><span class="muted">${esc((r.product_name || '').slice(0, 120))}</span></p>
      <p class="muted" style="font-size:13px">This removes it from Myrddin Hub. It can't be undone here${r.airtable_id ? ', and the copy from Airtable won\'t bring it back (Airtable itself isn\'t changed)' : ''}.</p>
      <div class="confirm-actions"><button class="btn" data-cf="no">Cancel</button><button class="btn danger-solid" data-cf="yes">Delete</button></div></div>`;
    document.body.appendChild(wrap);
    const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey, true); };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    document.addEventListener('keydown', onKey, true);
    wrap.querySelector('[data-cf="no"]').focus();
    wrap.addEventListener('click', async (e) => {
      if (e.target === wrap || e.target.closest('[data-cf="no"]')) { close(); return; }
      const yes = e.target.closest('[data-cf="yes"]'); if (!yes) return;
      yes.disabled = true; yes.textContent = 'Deleting…';
      try {
        await api('DELETE', '/api/leads/' + id);
        close();
        S.rows = S.rows.filter((x) => x.id !== id); S.byId.delete(id);
        if (S.openId === id) closeDrawer();
        render(); toast('Lead deleted');
      } catch (err) { yes.disabled = false; yes.textContent = 'Delete'; fail(err); }
    });
  }
  document.addEventListener('click', (e) => { const b = e.target.closest('[data-del]'); if (b) { e.preventDefault(); e.stopPropagation(); confirmDelete(b.dataset.del); } }, true);

  // ------------------------------------------------------------ header filters
  const FUNNEL = (filled) => `<svg width="15" height="15" viewBox="0 0 24 24" fill="${filled ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4.5h18l-7 8.6V19l-4 2v-7.9z"/></svg>`;
  const HEADER_FILTERS = {
    store: { input: 'store', show: (v) => v },
    brand: { input: 'brand', show: (v) => v },
    by: { input: 'sourced', show: (v) => v },
    band: { input: 'band', show: (v) => v + '+' },
  };
  let hPop = null;
  function closeHeaderFilter() { const el = hPop; hPop = null; if (el) el.remove(); }
  function setHeaderFilter(key, val) {
    $('#' + HEADER_FILTERS[key].input).value = val;
    load().catch(fail);
  }
  function openHeaderFilter(th) {
    const key = th.dataset.hfilter;
    if (hPop && hPop.dataset.key === key) { closeHeaderFilter(); return; }
    closeHeaderFilter();
    const fk = HEADER_FILTERS[key]; const cur = $('#' + fk.input).value;
    const el = document.createElement('div'); el.className = 'hpop'; el.dataset.key = key;
    const opt = (v, label) => `<button class="hp-opt ${cur === v ? 'on' : ''}" data-hp-val="${esc(v)}">${esc(label)}</button>`;
    if (key === 'store' || key === 'brand') {
      const other = key === 'store' ? 'brand' : 'store';
      el.innerHTML = `<input class="hp-text" list="${key === 'store' ? 'dlStores' : 'dlBrands'}" placeholder="Type a ${key} name" value="${esc(cur)}">
        <div class="hp-foot"><button class="btn small ghost" data-hp-val="">Clear</button><button class="btn small primary" data-hp-apply>Apply</button></div>
        <div class="hp-label">Sort within each day</div>
        <button class="hp-opt ${S.sort === key && !S.desc ? 'on' : ''}" data-hp-sort="asc" data-hp-key="${key}">A → Z, then ${other}</button>
        <button class="hp-opt ${S.sort === key && S.desc ? 'on' : ''}" data-hp-sort="desc" data-hp-key="${key}">Z → A, then ${other}</button>
        ${S.sort === key ? '<button class="hp-opt" data-hp-sort="off">Newest first (default)</button>' : ''}`;
    } else if (key === 'by') {
      el.innerHTML = opt('', 'Anyone') + (S.meta?.sourcers || []).map((x) => opt(x, x)).join('');
    } else if (key === 'band') {
      el.innerHTML = `<div class="hp-label">Show</div>${opt('', 'Any band')}${opt('4', 'Band 4+ (buy line)')}${opt('7', 'Band 7+')}${opt('9', 'Band 9+')}
        <div class="hp-label">Sort</div><button class="hp-opt ${S.sort === 'band' && S.desc ? 'on' : ''}" data-hp-sort="desc">High → low</button>
        <button class="hp-opt ${S.sort === 'band' && !S.desc ? 'on' : ''}" data-hp-sort="asc">Low → high</button>`;
    }
    document.body.appendChild(el);
    const r = th.getBoundingClientRect();
    el.style.left = Math.max(8, Math.min(r.left + window.scrollX, window.scrollX + window.innerWidth - el.offsetWidth - 8)) + 'px';
    el.style.top = (r.bottom + window.scrollY + 4) + 'px';
    hPop = el;
    const txt = el.querySelector('.hp-text');
    if (txt) {
      txt.focus(); txt.select();
      txt.addEventListener('keydown', (e) => { if (e.key === 'Enter') { setHeaderFilter(key, txt.value.trim()); closeHeaderFilter(); } });
      txt.addEventListener('change', () => { if (hPop !== el) return; if (S.meta && [...(key === 'store' ? S.meta.stores : S.meta.brands)].some((x) => x.name === txt.value)) { setHeaderFilter(key, txt.value.trim()); closeHeaderFilter(); } });
    }
    el.addEventListener('click', (e) => {
      const v = e.target.closest('[data-hp-val]'); if (v) { setHeaderFilter(key, v.dataset.hpVal); closeHeaderFilter(); return; }
      if (e.target.closest('[data-hp-apply]')) { setHeaderFilter(key, txt.value.trim()); closeHeaderFilter(); return; }
      const so = e.target.closest('[data-hp-sort]');
      if (so) {
        if (so.dataset.hpSort === 'off') { S.sort = 'date'; S.desc = true; }
        else { S.sort = so.dataset.hpKey || 'band'; S.desc = so.dataset.hpSort === 'desc'; }
        saveSort(); closeHeaderFilter(); load().catch(fail);
      }
    });
  }
  document.addEventListener('mousedown', (e) => { if (hPop && !hPop.contains(e.target) && !e.target.closest('th[data-hfilter]')) closeHeaderFilter(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && hPop) { e.stopPropagation(); closeHeaderFilter(); } }, true);

  // ------------------------------------------------------------ inline number cells (Buy qty, Ordered)
  function numEditCell(r, field) {
    const v = r[field];
    if (!can('editor')) return num(v);
    return `<button class="num-edit" data-numedit="${field}" data-stop title="Click to edit">${v == null ? '<span class="muted">—</span>' : num(v)}</button>`;
  }
  let numEd = null;
  function openNumEdit(btn) {
    const tr = btn.closest('tr[data-id]'); const id = tr.dataset.id; const field = btn.dataset.numedit;
    const row = S.byId.get(id) || {}; const orig = row[field] == null ? '' : String(row[field]);
    const td = btn.closest('td');
    td.innerHTML = `<input class="num-input" inputmode="numeric" value="${esc(orig)}">`;
    const inp = td.querySelector('input'); inp.focus(); inp.select();
    numEd = { id, field, orig, inp, td, done: false };
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finishNumEdit(false); }
      else if (e.key === 'Enter') { e.preventDefault(); finishNumEdit(true, 'down'); }
      else if (e.key === 'Tab') { e.preventDefault(); finishNumEdit(true, e.shiftKey ? 'left' : 'right'); }
    });
    inp.addEventListener('blur', () => finishNumEdit(true));
  }
  async function finishNumEdit(save, move) {
    const ed = numEd; if (!ed || ed.done) return; ed.done = true; numEd = null;
    const val = ed.inp.value.trim().replace(/,/g, '');
    const row = S.byId.get(ed.id);
    const redraw = () => { const tr = $(`#rows tr[data-id="${CSS.escape(ed.id)}"]`); if (tr && row) tr.outerHTML = rowHtml(row); };
    let next = null;
    if (move) {
      const tr = $(`#rows tr[data-id="${CSS.escape(ed.id)}"]`);
      if (move === 'down') { let n = tr?.nextElementSibling; while (n && !n.dataset.id) n = n.nextElementSibling; next = n ? { id: n.dataset.id, field: ed.field } : null; }
      else { const f = { right: { buy_qty: 'qty_ordered' }, left: { qty_ordered: 'buy_qty' } }[move][ed.field]; next = f ? { id: ed.id, field: f } : null; }
    }
    const openNext = () => { if (next) { const b = $(`#rows tr[data-id="${CSS.escape(next.id)}"] [data-numedit="${next.field}"]`); if (b) openNumEdit(b); } };
    if (!(save && val !== ed.orig)) { redraw(); openNext(); return; }
    if (val !== '' && !/^\d+$/.test(val)) { toast('Enter a whole number', true); redraw(); return; }
    const prev = row ? row[ed.field] : null;
    if (row) row[ed.field] = val === '' ? null : Number(val);
    redraw(); openNext();   // move on right away; the save runs in the background
    // Refresh the computed cells (Left, Spend) without disturbing an editor open in the same row.
    const refreshComputed = () => {
      const tr = $(`#rows tr[data-id="${CSS.escape(ed.id)}"]`); if (!tr || !row) return;
      if (numEd && numEd.id === ed.id) {
        const off = canOrder() ? 1 : 0;
        S.order.forEach((k, idx) => { if (['left', 'spend'].includes(k) && tr.children[idx + off]) tr.children[idx + off].innerHTML = COLS[k][3](row); });
      } else tr.outerHTML = rowHtml(row);
    };
    try {
      await api('PATCH', '/api/leads', [{ id: ed.id, [ed.field]: val }]);
      const fresh = await api('GET', '/api/leads/' + ed.id);
      if (row) { const keepOpen = numEd && numEd.id === ed.id ? numEd.field : null; for (const [k, v] of Object.entries(fresh)) if (k !== keepOpen) row[k] = v; }
      refreshComputed();
    } catch (e) {
      fail(e);
      if (row) { row[ed.field] = prev; refreshComputed(); const tr = $(`#rows tr[data-id="${CSS.escape(ed.id)}"]`); if (tr && !(numEd && numEd.id === ed.id)) tr.outerHTML = rowHtml(row); }
    }
  }
  document.addEventListener('click', (e) => { const b = e.target.closest('[data-numedit]'); if (b) { e.preventDefault(); openNumEdit(b); } }, true);

  // ------------------------------------------------------------ highlight the row whose ASIN / store link was clicked, until the next click
  function setHl(id) {
    if (S.hl === id) return;
    if (S.hl) $(`#rows tr[data-id="${CSS.escape(S.hl)}"]`)?.classList.remove('hl');
    S.hl = id;
    if (id) $(`#rows tr[data-id="${CSS.escape(id)}"]`)?.classList.add('hl');
  }
  document.addEventListener('click', (e) => {
    const link = e.target.closest?.('#rows a.asin-btn, #rows a.store-link, #rows a.pill-link');
    setHl(link ? link.closest('tr[data-id]')?.dataset.id || null : null);
  }, true);
  document.addEventListener('auxclick', (e) => {   // middle-click opens the link too
    const link = e.target.closest?.('#rows a.asin-btn, #rows a.store-link, #rows a.pill-link');
    if (link) setHl(link.closest('tr[data-id]')?.dataset.id || null);
  }, true);

  // ------------------------------------------------------------ instant hover text (product name and notes), anywhere in the cell
  const HOVER_FIELD = { promo: 'promo_code', product: 'product_name', va_notes: 'va_notes', buyer_notes: 'buyer_notes', feedback: 'feedback' };
  let hov = null, hovTd = null;
  function hideHover() { if (hov) hov.hidden = true; hovTd = null; }
  document.addEventListener('mouseover', (e) => {
    const td = e.target.closest?.('#rows td[data-k]');
    if (!td || !HOVER_FIELD[td.dataset.k]) { if (hovTd && !(td && td === hovTd)) hideHover(); return; }
    if (td === hovTd) return;
    if (notePop) { hideHover(); return; }   // an editor is open
    const r = S.byId.get(td.closest('tr')?.dataset.id || ''); const text = r && (r[HOVER_FIELD[td.dataset.k]] || '').trim();
    if (!text) { hideHover(); return; }
    if (!hov) { hov = document.createElement('div'); hov.className = 'hover-tip'; document.body.appendChild(hov); }
    hov.textContent = text; hov.hidden = false; hovTd = td;
    const c = td.getBoundingClientRect(); const w = Math.min(420, window.innerWidth - 16);
    hov.style.maxWidth = w + 'px';
    const left = Math.max(8, Math.min(c.left, window.innerWidth - hov.offsetWidth - 8));
    const below = c.bottom + 6 + hov.offsetHeight < window.innerHeight;
    hov.style.left = left + window.scrollX + 'px';
    hov.style.top = (below ? c.bottom + 4 : c.top - hov.offsetHeight - 4) + window.scrollY + 'px';
  });
  document.addEventListener('mouseleave', hideHover);
  addEventListener('scroll', hideHover, true);
  document.addEventListener('mousedown', hideHover, true);
  // a click anywhere in a notes cell opens its editor (not only on the text)
  document.addEventListener('click', (e) => {
    const td = e.target.closest?.('#rows td.note-cell');
    if (td && !e.target.closest('[data-note]')) { const b = td.querySelector('[data-note]'); if (b) { e.preventDefault(); e.stopPropagation(); b.click(); } }
  }, true);

  // ------------------------------------------------------------ note cells + pop-up editor
  function noteCell(r, field) {
    const v = r[field] || '';
    return `<button class="note-btn ${v ? '' : 'empty'}" data-note="${field}" data-stop>${v ? esc(v) : (can('editor') ? '+ add' : '')}</button>`;
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
    const label = { va_notes: 'VA notes', buyer_notes: 'Buyer notes', feedback: 'Feedback' }[field] || field;
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
    const lead0 = canOrder() ? '<td class="sel-col"></td>' : '';
    const fallback = best.len ? '' : `<tr class="new-row new-more">${lead0}<td colspan="${S.order.length + 1}">${extras}</td></tr>`;
    tb.innerHTML = `<tr class="new-row">${lead0}<td colspan="${S.order.length + 1}" class="new-title"><b>New lead</b> <span class="muted">— type across, then press Enter. The product name is filled in from Keepa.</span></td></tr>
      <tr class="new-row new-main">${lead0}${cells}<td class="row-act"></td></tr>${fallback}`;
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
      hazAfterAdd(lead);
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
        ${editable() ? `<span id="delWrap"><button class="btn small danger" data-act="del">Delete</button></span>` : ''}
        <button class="icon-btn" data-act="close" style="font-size:20px">✕</button>
      </div>
      <div class="drawer-body">
        ${l.transparency ? `<div class="hz-banner"><b>! Enrolled in Transparency.</b> Units need Transparency codes from the manufacturer; without them Amazon marks them unfulfillable${l.transparency.units_rejected ? ` (${num(l.transparency.units_rejected)} of your units already rejected)` : ''}. <button class="btn small" data-hz="${esc(l.asin)}" data-hz-day="${esc(l.lead_date || '')}">Details</button></div>` : ''}
        ${l.fba_eligible === false ? `<div class="hz-banner"><b>! You can't send this to FBA right now.</b> Amazon's eligibility check says it isn't eligible${(l.fba_reasons || []).length ? ` (${(l.fba_reasons || []).map(esc).join(', ')})` : ''}. <button class="btn small" data-hz="${esc(l.asin)}" data-hz-day="${esc(l.lead_date || '')}">Why?</button></div>`
          : l.fba_eligible === true && !l.transparency ? `<div class="hz-banner ok"><b>✓ Amazon says you can send this to FBA.</b></div>` : ''}
        ${(l.same_day_n || 0) > 1 ? `<div class="hz-banner warn"><b>Entered ${l.same_day_n} times on ${esc(fmtDate(l.lead_date))}.</b> Check it's not a duplicate. <button class="btn small" data-hz="${esc(l.asin)}" data-hz-day="${esc(l.lead_date || '')}">See them</button></div>` : ''}
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
      try { const l = await api('POST', '/api/leads', body); toast('Lead saved'); await load(); await openLead(l.id); hazAfterAdd(l); } catch (err) { fail(err); }
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
      else {
        const ds = [...new Set((m.ids || []).map((id) => S.byId.get(id)?.lead_date).filter(Boolean))];
        const p = new URLSearchParams({ ...filters(), limit: 1, day_totals: ds.join(',') });
        api('GET', '/api/leads?' + p).then((r) => { S.totals = r.totals; S.dayTotals = { ...S.dayTotals, ...(r.days || {}) }; render(); });
      }
      if (m.src !== 'airtable' && m.by && m.by !== (LG.st.me?.name || LG.st.me?.email)) toast(`${m.by} updated a lead`);
    }
  }

  // ------------------------------------------------------------ events
  document.addEventListener('click', async (e) => {
    const t = e.target;
    if (t.closest('[data-stop]')) return;   // ASIN / store links open in a new tab
    if (t.closest('[data-act="reset-cols"]')) { S.order = DEFAULT_ORDER.slice(); saveOrder(); render(); toast('Columns reset'); return; }
    const hf = t.closest('th[data-hfilter]');
    if (hf && !S.justDragged) { openHeaderFilter(hf); return; }
    const th = t.closest('th[data-sort]');
    if (th && !S.justDragged) { const k = th.dataset.sort; if (S.sort === k) { if (S.desc === !['asin'].includes(k)) S.desc = !S.desc; else { S.sort = 'date'; S.desc = true; } } else { S.sort = k; S.desc = !['asin'].includes(k); } saveSort(); load().catch(fail); return; }
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
  $('#q').addEventListener('input', reload);
  ['ordered', 'left', 'unscored'].forEach((id) => $('#' + id).addEventListener('change', reload));
  $$('#range .chip').forEach((c) => c.addEventListener('click', () => { $$('#range .chip').forEach((x) => x.classList.toggle('on', x === c)); S.days = Number(c.dataset.days); reload(); }));
  $('#clear').addEventListener('click', () => { ['q', 'store', 'brand', 'sourced', 'band'].forEach((id) => { $('#' + id).value = ''; }); ['ordered', 'left', 'unscored'].forEach((id) => { $('#' + id).checked = false; }); reload(); });
  $('#moreBtn').addEventListener('click', () => { S.offset += PAGE; load(true).catch(fail); });

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
