/* Myrddin Hub front end. Plain JS, no build step. */
(() => {
  'use strict';

  // ------------------------------------------------------------ state
  const S = {
    me: null, connectorUrl: '', orders: [], byId: new Map(), meta: { daily_target: 6800, stores: [], emails: [], set_types: [] },
    q: '', filter: 'all', shown: 300, openId: null, isNew: false, pendingDrawerRefresh: false,
  };
  const PAGE = 300;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (n) => n == null || n === '' ? '—' : Number(n).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
  const moneyWhole = (n) => Number(n || 0).toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
  const fmtDate = (d) => { if (!d) return ''; const [y, m, day] = d.slice(0, 10).split('-').map(Number); return new Date(y, m - 1, day).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }); };
  const can = (role) => ({ viewer: 1, editor: 2, owner: 3 })[S.me?.role] >= ({ viewer: 1, editor: 2, owner: 3 })[role];

  // ------------------------------------------------------------ api
  async function api(method, url, body) {
    const opt = { method, headers: { 'X-Requested-With': 'ledger' } };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    if (r.status === 401) { location.href = '/login'; throw new Error('Signed out'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : (r.status + ' error'));
    return data;
  }

  function toast(msg, err = false) {
    const t = document.createElement('div');
    t.className = 'toast' + (err ? ' err' : '');
    t.textContent = msg;
    $('#toasts').appendChild(t);
    setTimeout(() => t.remove(), err ? 6000 : 3500);
  }
  const fail = (e) => toast(e.message || String(e), true);

  // ------------------------------------------------------------ load

  function applyTabs(me) {   // hide tabs this login can't open
    const ok = new Set(me.tabs || []);
    document.querySelectorAll('nav.tabs a[data-tab]').forEach((a) => { a.hidden = !ok.has(a.dataset.tab); });
    document.querySelectorAll('[data-needs-tab]').forEach((el) => { el.hidden = !ok.has(el.dataset.needsTab); });
  }

  async function boot() {
    const me = await api('GET', '/api/me');
    S.me = me.user; S.connectorUrl = me.connector_url;
    $('#whoami').textContent = S.me.name || S.me.email;
    applyTabs(S.me);
    document.body.classList.toggle('is-viewer', !can('editor'));
    $$('.owner-only').forEach((el) => el.classList.toggle('hidden', !can('owner')));
    $$('.editor-only').forEach((el) => el.classList.toggle('hidden', !can('editor')));
    await Promise.all([loadOrders(), loadMeta()]);
    connectLive();
    const hash = location.hash.match(/^#order=(.+)$/);
    if (hash) openOrder(hash[1]);
    const page = { '#users': () => can('owner') && showUsers(), '#connect': showConnect, '#activity': showActivity }[location.hash];
    if (page) { history.replaceState(null, '', location.pathname); page(); }
  }

  async function loadOrders() {
    const { orders } = await api('GET', '/api/orders');
    S.orders = orders; S.byId = new Map(orders.map((o) => [o.id, o]));
    render();
  }

  async function loadMeta() {
    S.meta = await api('GET', '/api/meta');
    fillDatalist('#dlStores', S.meta.stores); fillDatalist('#dlEmails', S.meta.emails); fillDatalist('#dlSets', S.meta.set_types);
    renderTiles();
  }

  function fillDatalist(sel, vals) { $(sel).innerHTML = (vals || []).map((v) => `<option value="${esc(v)}">`).join(''); }

  // ------------------------------------------------------------ live updates
  let es = null, liveTimer = null;
  function connectLive() {
    if (es) es.close();
    es = new EventSource('/api/events');
    es.onopen = () => { $('#live')?.classList.add('on'); };
    es.onerror = () => { $('#live')?.classList.remove('on'); };
    es.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch { return; } onLive(m); };
  }

  async function onLive(m) {
    if (m.t && m.t !== 'order') return;
    if (m.op === 'reload') { await loadOrders(); loadMeta(); if (S.openId) refreshDrawer(); return; }
    if (m.op === 'settings') { loadMeta(); return; }
    const mine = m.by && S.me && (m.by === (S.me.name || S.me.email)) && m.src === 'web';
    if (m.op === 'delete') {
      const old = S.byId.get(m.id);
      S.orders = S.orders.filter((o) => o.id !== m.id); S.byId.delete(m.id);
      render();
      if (!mine && old) toast(`${m.by || 'Someone'} deleted ${old.store} ${old.order_ref || ''}`);
      if (S.openId === m.id) { closeDrawer(); }
      scheduleMeta();
      return;
    }
    if (m.op === 'upsert') {
      let fresh;
      try { fresh = await api('GET', '/api/orders/' + m.id); } catch { return; }
      upsertLocal(fresh);
      render();
      flashRow(m.id);
      if (!mine) toast(`${m.by || 'Someone'} updated ${fresh.store} ${fresh.order_ref || ''}`.trim());
      if (S.openId === m.id) refreshDrawer(fresh);
      scheduleMeta();
    }
  }

  function scheduleMeta() { clearTimeout(liveTimer); liveTimer = setTimeout(loadMeta, 800); }

  function upsertLocal(o) {
    const summary = { ...o }; delete summary.items;
    if (S.byId.has(o.id)) {
      S.orders = S.orders.map((x) => x.id === o.id ? summary : x);
    } else {
      S.orders.push(summary);
    }
    S.byId.set(o.id, summary);
    S.orders.sort((a, b) => (b.order_date || '').localeCompare(a.order_date || '') || (b.created_at || '').localeCompare(a.created_at || ''));
  }

  function flashRow(id) {
    const tr = $(`tr[data-id="${CSS.escape(id)}"]`);
    if (!tr) return;
    tr.classList.add('flash');
    setTimeout(() => tr.classList.remove('flash'), 1200);
  }

  // ------------------------------------------------------------ list rendering
  function matches(o, q) {
    if (!q) return true;
    return [o.store, o.email, o.order_ref, o.notes, o.asins, o.order_date].some((v) => (v || '').toLowerCase().includes(q));
  }

  function filtered() {
    const q = S.q.trim().toLowerCase();
    return S.orders.filter((o) => matches(o, q));
  }

  function render() {
    if (oe && !oe.done) { S.pendingRender = true; return; }   // don't redraw the table under an open cell editor
    S.pendingRender = false;
    const base = filtered();
    const disc = base.filter((o) => o.discrepancy), miss = base.filter((o) => o.missing.length);
    $('#cDisc').textContent = disc.length || ''; $('#cMiss').textContent = miss.length || '';
    const list = S.filter === 'discrepancy' ? disc : S.filter === 'missing' ? miss : base;
    const total = list.reduce((s, o) => s + (o.order_total || 0), 0);
    if ($('#summary')) $('#summary').textContent = `${S.orders.length.toLocaleString()} orders tracked · ${money(total)} shown` +
      (list.length !== S.orders.length ? ` (${list.length.toLocaleString()} matching)` : '');
    const rows = list.slice(0, S.shown).map(rowHtml).join('');
    $('#rows').innerHTML = rows;
    $('#empty').classList.toggle('hidden', list.length > 0);
    $('#more').classList.toggle('hidden', list.length <= S.shown);
    $('#moreBtn').textContent = `Show more (${(list.length - S.shown).toLocaleString()} left)`;
    renderTiles(base);
  }

  // ------------------------------------------------------------ inline cell editing on the orders table
  const OE_FIELDS = ['order_date', 'store', 'email', 'order_total', 'order_ref', 'notes'];
  function oeCell(o, f, shown) {
    if (!can('editor')) return shown;
    return `<span class="oe" data-oe="${f}" title="Click to edit">${shown === '' ? '<span class="muted">—</span>' : shown}</span>`;
  }
  let oe = null;
  const oeVal = (o, f) => o[f] == null ? '' : f === 'order_total' ? Number(o[f]).toFixed(2) : String(o[f]);
  function openOE(span) {
    if (oe && !oe.done) finishOE(true);
    const tr = span.closest('tr[data-id]'); if (!tr) return;
    const id = tr.dataset.id, f = span.dataset.oe, o = S.byId.get(id); if (!o) return;
    const orig = oeVal(o, f), td = span.closest('td');
    const type = f === 'order_date' ? 'date' : 'text';
    const list = f === 'store' ? 'list="dlStores"' : f === 'email' ? 'list="dlEmails"' : '';
    td.innerHTML = `<input class="oe-input ${f === 'order_total' ? 'num' : ''} ${f === 'order_ref' ? 'mono' : ''}" type="${type}" ${list} ${f === 'order_total' ? 'inputmode="decimal"' : ''} value="${esc(orig)}" autocomplete="off">`;
    const inp = td.querySelector('input'); inp.focus(); if (type === 'text') inp.select();
    const me = { id, f, orig, inp, done: false }; oe = me;
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finishOE(false); }
      else if (e.key === 'Enter') { e.preventDefault(); finishOE(true, 'down'); }
      else if (e.key === 'Tab') { e.preventDefault(); finishOE(true, e.shiftKey ? 'left' : 'right'); }
    });
    inp.addEventListener('blur', () => setTimeout(() => { if (oe === me) finishOE(true); }, 0));   // only close this cell's own editor
  }
  async function finishOE(save, move) {
    const ed = oe; if (!ed || ed.done) return; ed.done = true;
    const o = S.byId.get(ed.id); const val = ed.inp.value.trim();
    let next = null;
    if (move) {
      const tr = $(`#rows tr[data-id="${CSS.escape(ed.id)}"]`);
      if (move === 'down') { const n = tr?.nextElementSibling; if (n?.dataset.id) next = { id: n.dataset.id, f: ed.f }; }
      else { const f = OE_FIELDS[OE_FIELDS.indexOf(ed.f) + (move === 'right' ? 1 : -1)]; if (f) next = { id: ed.id, f }; }
    }
    const openNext = () => { if (next) { const sp = $(`#rows tr[data-id="${CSS.escape(next.id)}"] [data-oe="${next.f}"]`); if (sp) openOE(sp); } };
    const redraw = () => { const tr = $(`#rows tr[data-id="${CSS.escape(ed.id)}"]`); if (tr && o) { tr.outerHTML = rowHtml(o); } };
    if (!save || val === ed.orig || (ed.f === 'order_total' && val !== '' && Number(val.replace(/[$,]/g, '')) === Number(ed.orig))) {
      redraw(); if (S.pendingRender) render(); openNext(); return;
    }
    if (ed.f === 'store' && !val) { toast("Store can't be blank", true); redraw(); return; }
    const body = { [ed.f]: ed.f === 'order_total' ? val.replace(/[$,]/g, '') : val };
    const prev = o[ed.f]; o[ed.f] = ed.f === 'order_total' ? (val === '' ? null : Number(body.order_total)) : val || null;
    redraw(); openNext();   // move on right away; the save runs in the background
    try {
      const fresh = await api('PATCH', '/api/orders/' + ed.id, body);
      upsertLocal(fresh); render();
      if (S.openId === ed.id) refreshDrawer(fresh);
    } catch (e) { fail(e); o[ed.f] = prev; redraw(); render(); }
  }
  document.addEventListener('click', (e) => {
    const sp = e.target.closest('#rows [data-oe]');
    if (sp) { e.preventDefault(); e.stopPropagation(); openOE(sp); return; }
    if (e.target.closest('#rows .oe-input')) { e.stopPropagation(); return; }
    const op = e.target.closest('[data-open-row]');
    if (op) { e.preventDefault(); e.stopPropagation(); openOrder(op.dataset.openRow); }
  }, true);

  function rowHtml(o) {
    const discTitle = o.discrepancy ? `Items total ${money(o.items_total)} vs order total ${money(o.order_total)}` : 'Items total matches order total';
    const missTitle = o.missing.length ? 'Missing: ' + o.missing.join(', ') : 'All item fields filled in';
    return `<tr data-id="${esc(o.id)}" class="${S.openId === o.id ? 'sel' : ''}">
      <td>${oeCell(o, 'order_date', esc(fmtDate(o.order_date)))}</td><td class="store-name">${oeCell(o, 'store', esc(o.store))}</td>
      <td class="email">${oeCell(o, 'email', esc(o.email || ''))}</td>
      <td class="num">${oeCell(o, 'order_total', money(o.order_total))}</td><td class="mono">${oeCell(o, 'order_ref', esc(o.order_ref || ''))}</td>
      <td class="notes" title="${esc(o.notes || '')}">${oeCell(o, 'notes', esc(o.notes || '—'))}</td>
      <td class="center"><span class="dot ${o.discrepancy ? 'bad' : ''}" title="${esc(discTitle)}"></span></td>
      <td class="center"><span class="dot ${o.missing.length ? 'bad' : ''}" title="${esc(missTitle)}"></span></td>
      <td class="order-act">${can('editor') ? `<button class="icon-act" data-add-items="${esc(o.id)}" title="Add items" aria-label="Add items">${I_BOX}</button>` : ''}<button class="icon-act" data-open-row="${esc(o.id)}" title="${can('editor') ? 'Open / edit order' : 'Open order'}" aria-label="Open order">${I_PEN}</button>${can('editor') ? `<button class="icon-act" data-dup-order="${esc(o.id)}" title="Duplicate order" aria-label="Duplicate order">${I_COPY}</button><button class="icon-act danger" data-del-order="${esc(o.id)}" title="Delete order" aria-label="Delete order">${I_TRASH}</button>` : ''}</td></tr>`;
  }

  const I_BOX = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>`;
  const I_PEN = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.17 6.81a1 1 0 0 0-3.99-3.99L3.84 16.17a2 2 0 0 0-.5.83l-1.32 4.35a.5.5 0 0 0 .62.62l4.35-1.32a2 2 0 0 0 .83-.5z"/><path d="m15 5 4 4"/></svg>`;
  const I_COPY = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>`;
  const I_TRASH = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>`;


  async function duplicateOrder(id) {
    let o = S.orders.find((x) => x.id === id);
    if (!o || S.openId === id) { try { o = await api('GET', '/api/orders/' + id); } catch (e) { fail(e); return; } }
    const nItems = o.item_count ?? (o.items || []).length;
    const wrap = document.createElement('div'); wrap.className = 'confirm-back';
    wrap.innerHTML = `<form class="confirm-box" role="dialog" aria-modal="true" aria-labelledby="dpT">
      <h3 id="dpT">Duplicate order</h3>
      <p><b>${esc(o.store || '')}</b> · ${esc(fmtDate(o.order_date))} · ${money(o.order_total)}${o.order_ref ? ` · <span class="mono">${esc(o.order_ref)}</span>` : ''}<br>
        <span class="muted">${nItems} item line${nItems === 1 ? '' : 's'}${o.email ? ' · ' + esc(o.email) : ''}</span></p>
      <label style="display:flex;align-items:center;gap:10px;margin:12px 0 4px">How many copies?
        <input name="count" type="number" min="1" max="50" step="1" value="1" required style="width:80px" class="num"></label>
      <p class="muted" style="font-size:13px">Each copy gets the same date, store, email, order total and items. Order ID and notes start blank, so add each copy's own order ID.</p>
      <div class="confirm-actions"><button type="button" class="btn" data-cf="no">Cancel</button><button class="btn primary" type="submit" data-cf="yes">Make copies</button></div></form>`;
    document.body.appendChild(wrap);
    const form = wrap.querySelector('form'), inp = form.elements.count;
    const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey, true); };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
    document.addEventListener('keydown', onKey, true);
    inp.select(); inp.focus();
    const yes = form.querySelector('[data-cf="yes"]');
    const upd = () => { const n = Number(inp.value); yes.textContent = n >= 1 ? `Make ${n} cop${n === 1 ? 'y' : 'ies'}` : 'Make copies'; };
    inp.addEventListener('input', upd); upd();
    wrap.addEventListener('click', (e) => { if (e.target === wrap || e.target.closest('[data-cf="no"]')) close(); });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const n = Math.floor(Number(inp.value));
      if (!(n >= 1 && n <= 50)) { toast('Enter a number from 1 to 50', true); inp.focus(); return; }
      yes.disabled = true; yes.textContent = 'Copying…';
      try {
        const { ids } = await api('POST', `/api/orders/${id}/duplicate`, { count: n });
        close(); await loadOrders();
        toast(`Made ${ids.length} cop${ids.length === 1 ? 'y' : 'ies'}`);
        ids.forEach((x) => flashRow(x));
      } catch (err) { yes.disabled = false; upd(); fail(err); }
    });
  }

  function confirmDeleteOrder(id) {
    const o = S.orders.find((x) => x.id === id) || {};
    const wrap = document.createElement('div'); wrap.className = 'confirm-back';
    wrap.innerHTML = `<div class="confirm-box" role="alertdialog" aria-modal="true" aria-labelledby="cfT">
      <h3 id="cfT">Delete this order?</h3>
      <p><b>${esc(o.store || '(no store)')}</b> · ${esc(fmtDate(o.order_date))} · ${money(o.order_total)}${o.order_ref ? `<br><span class="muted mono">${esc(o.order_ref)}</span>` : ''}</p>
      <p class="muted" style="font-size:13px">This deletes the order and all of its items from Myrddin Hub. It can't be undone.</p>
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
        await api('DELETE', '/api/orders/' + id);
        close();
        S.orders = S.orders.filter((x) => x.id !== id);
        if (S.openId === id) closeDrawer();
        render(); toast('Order deleted');
      } catch (err) { yes.disabled = false; yes.textContent = 'Delete'; fail(err); }
    });
  }

  async function addItemTo(id) {
    if (S.openId !== id || $('#drawer').classList.contains('hidden')) await openOrder(id);
    const n = $('#newItem'); if (!n) return;
    n.scrollIntoView({ block: 'center' }); $('[data-n="asin"]', n).focus();
  }

  async function saveNewItem() {
    const n = $('#newItem'); if (!n || n.dataset.busy) return;
    const v = Object.fromEntries($$('[data-n]', n).map((x) => [x.dataset.n, x.value.trim()]));
    if (!v.asin && !v.product_title) { $('[data-n="asin"]', n).focus(); toast('Enter an ASIN (or a title) first', true); return; }
    const body = Object.fromEntries(Object.entries(v).filter(([, x]) => x !== ''));
    n.dataset.busy = '1'; const btn = $('[data-act="save-new-item"]', n); btn.disabled = true; btn.textContent = 'Adding…';
    try {
      const o = await api('POST', `/api/orders/${S.openId}/items`, [body]);
      upsertLocal(o); render();
      $$('#newItem [data-n]').forEach((x) => { x.value = x.dataset.n === 'qty_ordered' ? '1' : ''; });
      renderDrawer(o);
      $('#newItem [data-n="asin"]')?.focus();
      toast('Item added');
    } catch (err) { fail(err); btn.disabled = false; btn.textContent = '+ Add'; delete n.dataset.busy; }
  }

  document.addEventListener('click', (e) => {
    const dup = e.target.closest('[data-dup-order]');
    if (dup) { e.preventDefault(); e.stopPropagation(); duplicateOrder(dup.dataset.dupOrder); return; }
    const d = e.target.closest('[data-del-order]');
    if (d) { e.preventDefault(); e.stopPropagation(); confirmDeleteOrder(d.dataset.delOrder); return; }
    const a = e.target.closest('[data-add-items]');
    if (a) { e.preventDefault(); e.stopPropagation(); addItemTo(a.dataset.addItems).catch(fail); }
  }, true);

  function renderTiles(base) {
    base = base || filtered();
    const target = Number(S.meta.daily_target || 6800);
    const byMonth = new Map();
    for (const o of base) { const k = (o.order_date || '').slice(0, 7); if (k) byMonth.set(k, (byMonth.get(k) || 0) + (o.order_total || 0)); }
    const now = new Date();
    const html = [...byMonth.entries()].sort((a, b) => b[0].localeCompare(a[0])).map(([k, sum]) => {
      const [y, m] = k.split('-').map(Number);
      const label = new Date(y, m - 1, 1).toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
      const dim = new Date(y, m, 0).getDate();
      const cur = now.getFullYear() === y && now.getMonth() === m - 1;
      const fut = y > now.getFullYear() || (y === now.getFullYear() && m - 1 > now.getMonth());
      const days = cur ? now.getDate() : fut ? 0 : dim;
      const need = target * days - sum;
      const targetBtn = can('owner') ? `<button data-act="target" title="Change the daily target">${moneyWhole(target)}</button>` : moneyWhole(target);
      return `<div class="tile-pair"><div class="tile"><div class="k">Spent · ${esc(label)}</div><div class="v">${moneyWhole(sum)}</div></div>
        <div class="tile"><div class="k">Need to spend · ${esc(label)}</div><div class="v ${need > 0 ? 'bad' : 'ok'}">${moneyWhole(need)}</div>
        <div class="f">${days} day${days === 1 ? '' : 's'} × ${targetBtn}</div></div></div>`;
    }).join('');
    $('#tiles').innerHTML = html || '<span class="muted">No spending yet.</span>';
  }

  // ------------------------------------------------------------ drawer
  function openDrawerShell() {
    $('#scrim').classList.remove('hidden'); $('#drawer').classList.remove('hidden');
  }

  function closeDrawer() {
    S.openId = null; S.isNew = false;
    $('#scrim').classList.add('hidden'); $('#drawer').classList.add('hidden'); $('#drawer').innerHTML = '';
    if (location.hash) history.replaceState(null, '', location.pathname);
    $$('#rows tr.sel').forEach((tr) => tr.classList.remove('sel'));
  }

  async function openOrder(id) {
    S.openId = id; S.isNew = false; $('#drawer').classList.remove('narrow');
    $$('#rows tr').forEach((tr) => tr.classList.toggle('sel', tr.dataset.id === id));
    history.replaceState(null, '', '#order=' + id);
    openDrawerShell();
    $('#drawer').innerHTML = '<div class="drawer-body muted">Loading…</div>';
    try { renderDrawer(await api('GET', '/api/orders/' + id)); } catch (e) { fail(e); closeDrawer(); }
  }

  async function refreshDrawer(fresh) {
    if (!S.openId) return;
    const active = document.activeElement;
    if (active && $('#drawer').contains(active) && /INPUT|TEXTAREA|SELECT/.test(active.tagName)) {
      S.pendingDrawerRefresh = true;  // don't yank a field out from under the person typing
      return;
    }
    try { renderDrawer(fresh || await api('GET', '/api/orders/' + S.openId)); } catch { /* deleted */ }
  }

  const ro = () => can('editor') ? '' : 'disabled';

  function renderDrawer(o) {
    const keep = $('#newItem') && $('#drawer').dataset.order === String(o.id) ? Object.fromEntries($$('#newItem [data-n]').map((x) => [x.dataset.n, x.value])) : null;
    const keepFocus = document.activeElement?.closest?.('#newItem') ? document.activeElement.dataset.n : null;
    _renderDrawer(o);
    $('#drawer').dataset.order = String(o.id);
    if (keep) $$('#newItem [data-n]').forEach((x) => { if (keep[x.dataset.n] != null) x.value = keep[x.dataset.n]; });
    if (keepFocus) $(`#newItem [data-n="${keepFocus}"]`)?.focus();
  }

  function _renderDrawer(o) {
    const items = o.items || [];
    const itemsTotal = items.reduce((s, i) => s + (i.qty_ordered || 0) * (i.cost_per_unit || 0), 0);
    const units = items.reduce((s, i) => s + (i.qty_ordered || 0), 0);
    const diff = itemsTotal - (o.order_total || 0);
    const off = Math.abs(diff) > units * 0.01 + 1e-9;
    const blank = (v) => v == null || String(v).trim() === '';
    const itemRow = (i) => `<tr data-item="${esc(i.id)}">
      <td class="c-asin"><input class="mono ${blank(i.asin) ? 'missing' : ''}" data-f="asin" value="${esc(i.asin || '')}" ${ro()} placeholder="ASIN"></td>
      <td><input data-f="product_title" class="${blank(i.product_title) ? 'missing' : ''}" value="${esc(i.product_title || '')}" ${ro()} placeholder="Title"></td>
      <td class="c-shade"><input data-f="shade" class="${blank(i.shade) ? 'missing' : ''}" value="${esc(i.shade || '')}" ${ro()} placeholder="Shade"></td>
      <td class="c-set"><input data-f="set_type" list="dlSets" class="${blank(i.set_type) ? 'missing' : ''}" value="${esc(i.set_type || '')}" ${ro()} placeholder="Set type"></td>
      <td class="c-qty"><input data-f="qty_ordered" class="num ${!(i.qty_ordered >= 1) ? 'missing' : ''}" inputmode="numeric" value="${esc(i.qty_ordered ?? '')}" ${ro()}></td>
      <td class="c-cost"><input data-f="cost_per_unit" class="num ${blank(i.cost_per_unit) ? 'missing' : ''}" inputmode="decimal" value="${i.cost_per_unit == null ? '' : esc(Number(i.cost_per_unit).toFixed(2))}" ${ro()}></td>
      <td class="c-line num">${money((i.qty_ordered || 0) * (i.cost_per_unit || 0))}</td>
      <td>${i.asin ? `<a class="icon-btn" href="https://www.amazon.com/dp/${esc(i.asin)}" target="_blank" rel="noopener" title="Open on Amazon">↗</a>` : ''}
        ${can('editor') ? `<button class="icon-btn" data-act="del-item" title="Remove item">✕</button>` : ''}</td></tr>`;

    $('#drawer').innerHTML = `
      <div class="drawer-head">
        <h2>${esc(o.store)} <span class="muted mono" style="font-size:15px">${esc(o.order_ref || '')}</span></h2>
        ${can('editor') ? `<button class="btn small" data-act="dup-order">Duplicate</button><span id="delWrap"><button class="btn small danger" data-act="del-order">Delete order</button></span>` : ''}
        <button class="icon-btn" data-act="close" title="Close (Esc)" style="font-size:20px">✕</button>
      </div>
      <div class="drawer-body">
        <div class="grid" id="orderFields">
          <label>Date<input type="date" data-o="order_date" value="${esc(o.order_date || '')}" ${ro()}></label>
          <label>Store<input data-o="store" list="dlStores" value="${esc(o.store || '')}" ${ro()}></label>
          <label>Order total<input data-o="order_total" class="num" inputmode="decimal" value="${o.order_total == null ? '' : esc(Number(o.order_total).toFixed(2))}" ${ro()}></label>
          <label>Order ID<input data-o="order_ref" class="mono" value="${esc(o.order_ref || '')}" ${ro()}></label>
          <label style="grid-column: span 2">Email<input data-o="email" list="dlEmails" value="${esc(o.email || '')}" ${ro()}></label>
          <label class="wide">Notes<textarea data-o="notes" rows="2" ${ro()}>${esc(o.notes || '')}</textarea></label>
        </div>

        <div class="section-title">Items <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400">${items.length} line${items.length === 1 ? '' : 's'} · ${units} unit${units === 1 ? '' : 's'}</span></div>
        <div class="items"><table>
          <thead><tr><th>ASIN</th><th>Title</th><th>Shade</th><th>Set type</th><th class="num">Qty</th><th class="num">Cost/unit</th><th class="num">Line</th><th></th></tr></thead>
          <tbody>${items.map(itemRow).join('') || `<tr><td colspan="8" class="muted" style="padding:14px">No items yet.</td></tr>`}</tbody>
          ${can('editor') ? `<tbody><tr class="new-item" id="newItem">
            <td class="c-asin"><input class="mono" data-n="asin" placeholder="ASIN" maxlength="10" autocomplete="off"></td>
            <td><input data-n="product_title" placeholder="Title (filled from Keepa if blank)"></td>
            <td class="c-shade"><input data-n="shade" placeholder="Shade"></td>
            <td class="c-set"><input data-n="set_type" list="dlSets" placeholder="Set type"></td>
            <td class="c-qty"><input data-n="qty_ordered" class="num" inputmode="numeric" value="1"></td>
            <td class="c-cost"><input data-n="cost_per_unit" class="num" inputmode="decimal" placeholder="0.00"></td>
            <td colspan="2"><button class="btn small primary" data-act="save-new-item">+ Add</button></td></tr></tbody>` : ''}
          <tfoot><tr><td colspan="6">Items total</td><td class="num">${money(itemsTotal)}</td><td></td></tr></tfoot>
        </table></div>
        <div class="recon ${off ? 'bad' : 'ok'}">${off
          ? `Items total ${money(itemsTotal)} is ${money(Math.abs(diff))} ${diff > 0 ? 'over' : 'under'} the order total ${money(o.order_total)} (tolerance ${money(units * 0.01)}).`
          : `Items total matches the order total.`}
          ${o.missing && o.missing.length ? `<br>Missing: ${esc(o.missing.join(', '))}` : ''}</div>

        <div class="section-title">History</div>
        <div class="history" id="hist"><span class="muted">Loading…</span></div>
      </div>`;
    S.pendingDrawerRefresh = false;
    loadHistory(o.id);
  }

  async function loadHistory(orderId) {
    try {
      const { changes } = await api('GET', '/api/activity?limit=40&order_id=' + encodeURIComponent(orderId));
      const el = $('#hist'); if (!el || S.openId !== orderId) return;
      el.innerHTML = changes.length ? '<ul>' + changes.map(changeLine).join('') + '</ul>' : '<span class="muted">No changes recorded.</span>';
    } catch { /* ignore */ }
  }

  function describe(c) {
    const fields = (obj) => Object.keys(obj || {}).filter((k) => !['id', 'order_id', 'created_at', 'updated_at', 'created_by', 'updated_by', 'legacy_id'].includes(k));
    if (c.entity === 'batch') return `imported ${c.after?.orders ?? ''} orders, ${c.after?.items ?? ''} items`;
    if (c.entity === 'setting') return c.after?.daily_target != null ? `set daily target to ${moneyWhole(c.after.daily_target)}` : c.after?.leads_mode === 'home' ? 'made Myrddin Hub the home for leads' : 'changed a setting';
    if (c.entity === 'prepos') return `${c.after?.ok === false ? 'tried to send' : 'sent'} ${c.after?.lines} line${c.after?.lines === 1 ? '' : 's'} (${c.after?.units} units) to PrepOS${c.after?.kind === 'nightly' ? ' (nightly)' : ''}`;
    if (c.entity === 'store' || c.entity === 'brand') return `marked ${c.entity} ${esc(c.after?.name || '')} as ${c.after?.can_source ? 'OK to source' : "can't source"}`;
    let what;
    if (c.entity === 'lead') {
      const asin = c.lead_asin || c.after?.asin || c.before?.asin || '';
      what = c.action === 'delete' || !c.entity_id ? `lead ${esc(asin)}` : `lead <a href="/leads#lead=${esc(c.entity_id)}" class="mono">${esc(asin || '(no ASIN)')}</a>`;
    } else what = c.entity === 'item' ? `item ${esc(c.after?.asin || c.before?.asin || '')}` : 'order';
    if (c.action === 'create') return `added ${what}`;
    if (c.action === 'delete') return `deleted ${what}`;
    return `changed ${what}: ` + fields(c.after).map((k) => `${esc(k.replace(/_/g, ' '))} ${esc(fmtVal(c.before?.[k]))} → ${esc(fmtVal(c.after?.[k]))}`).join('; ');
  }
  const fmtVal = (v) => { if (v == null || v === '') return '∅'; const t = Array.isArray(v) ? v.join(', ') : String(v); return t.length > 80 ? t.slice(0, 80) + '…' : t; };

  function changeLine(c) {
    const when = new Date(c.at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    const tag = `<span class="tag ${c.source === 'chat' ? 'chat' : ''}">${esc(c.source)}</span>`;
    return `<li>${tag} <b>${esc(c.who || (c.source === 'import' ? 'Import' : c.source === 'system' ? (c.entity === 'prepos' ? 'Myrddin Hub' : 'Cleanup') : 'Someone'))}</b> ${describe(c)} <span class="muted">· ${esc(when)}</span></li>`;
  }

  // field saves
  async function saveOrderField(input) {
    const f = input.dataset.o;
    const id = S.openId; if (!id || S.isNew) return;
    const o = S.byId.get(id) || {};
    const val = input.value;
    const cur = o[f] == null ? '' : (f === 'order_total' ? Number(o[f]).toFixed(2) : String(o[f]));
    if (val === cur) return;
    try { const fresh = await api('PATCH', '/api/orders/' + id, { [f]: val }); upsertLocal(fresh); render(); }
    catch (e) { fail(e); input.value = cur; }
  }

  async function saveItemField(input) {
    const tr = input.closest('tr[data-item]'); const itemId = tr.dataset.item; const f = input.dataset.f;
    if (input.dataset.orig === input.value) return;
    try { await api('PATCH', '/api/items', [{ id: itemId, [f]: input.value }]); }
    catch (e) { fail(e); input.value = input.dataset.orig || ''; }
  }

  // ------------------------------------------------------------ new order
  function newOrder() {
    S.openId = null; S.isNew = true; openDrawerShell();
    const today = new Date(); const iso = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    $('#drawer').classList.add('narrow');
    $('#drawer').innerHTML = `
      <div class="drawer-head"><h2>New order</h2><button class="icon-btn" data-act="close" title="Close (Esc)" style="font-size:20px">✕</button></div>
      <div class="drawer-body">
        <form id="newForm">
        <div class="grid two">
          <label>Date<input type="date" name="order_date" value="${iso}" required></label>
          <label>Store<input name="store" list="dlStores" required autofocus placeholder="Store"></label>
          <label>Email<input name="email" list="dlEmails" value="${esc(S.meta.emails?.[0] || '')}" placeholder="you@example.com"></label>
          <label>Order total<input name="order_total" class="num" inputmode="decimal" placeholder="0.00"></label>
          <label class="wide">Order ID<input name="order_ref" class="mono" placeholder="Order ID"></label>
          <label class="wide">Notes<textarea name="notes" rows="3" placeholder="Notes"></textarea></label>
        </div>
        <div class="dlg-actions"><button type="button" class="btn" data-act="close">Cancel</button><button class="btn primary" type="submit">Add order</button></div>
        </form>
      </div>`;
    setTimeout(() => $('#newForm [name="store"]')?.focus(), 30);
    $('#newForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(e.target).entries());
      const btn = e.target.querySelector('[type="submit"]'); btn.disabled = true; btn.textContent = 'Adding…';
      try {
        const o = await api('POST', '/api/orders', body);
        upsertLocal(o); render(); toast('Order added — now add its items');
        await addItemTo(o.id);   // the order dialog opens with the item row ready
      } catch (err) { fail(err); btn.disabled = false; btn.textContent = 'Add order'; }
    });
  }

  // ------------------------------------------------------------ modals
  function openModal(html) { $('#modal').innerHTML = html; $('#modal').classList.remove('hidden'); $('#scrim').classList.remove('hidden'); }
  function closeModal() { $('#modal').classList.remove('wide'); $('#modal').classList.add('hidden'); $('#modal').innerHTML = ''; if ($('#drawer').classList.contains('hidden')) $('#scrim').classList.add('hidden'); }

  // ------------------------------------------------------------ PrepOS (Network Prep)
  async function showPrepos(through) {
    const d = await api('GET', '/api/prepos' + (through ? '?through=' + encodeURIComponent(through) : ''));
    const p = d.pending, lines = p.lines;
    const units = lines.reduce((s, x) => s + x.qty, 0);
    const cost = lines.reduce((s, x) => s + x.qty * (x.unit_cost || 0), 0);
    const day = (iso) => fmtDate(iso);
    const span = (x) => x.first_date === x.last_date ? day(x.first_date) : `${day(x.first_date)} – ${day(x.last_date)}`;
    const lineRow = (x) => `<tr><td class="mono"><a href="https://www.amazon.com/dp/${esc(x.asin)}" target="_blank" rel="noopener">${esc(x.asin)}</a>${x.multipack ? ` <span class="tag">${x.multipack}-pack</span>` : ''}</td>
      <td class="pp-title" title="${esc(x.title || '')}">${esc(x.title || '—')}</td><td class="num">${x.qty}</td>
      <td class="num">${x.unit_cost == null ? '<span class="bad-text">no cost</span>' : money(x.unit_cost)}${x.cost_missing_units ? `<br><span class="muted" style="font-size:11px">${x.cost_missing_units} u no cost</span>` : ''}</td>
      <td>${esc(x.supplier)}</td><td class="muted" style="white-space:nowrap">${x.orders} order${x.orders === 1 ? '' : 's'} · ${esc(span(x))}</td></tr>`;
    const batches = []; const byB = new Map();
    for (const h of d.history) { if (!byB.has(h.batch)) { const b = { ...h, rows: [] }; byB.set(h.batch, b); batches.push(b); } byB.get(h.batch).rows.push(h); }
    const hist = batches.slice(0, 30).map((b) => {
      const u = b.rows.reduce((s, x) => s + x.qty, 0);
      const when = new Date(b.sent_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      return `<details class="pp-batch"><summary><b>${esc(when)}</b> · ${b.kind === 'nightly' ? 'nightly' : 'button' + (b.who ? ' (' + esc(b.who) + ')' : '')} · ${b.rows.length} line${b.rows.length === 1 ? '' : 's'}, ${u} units
        ${b.status === 'sent' ? '<span class="tag ok-tag">sent</span>' : '<span class="tag bad-tag">refused</span>'}</summary>
        <table><thead><tr><th>ASIN</th><th>Title</th><th class="num">Qty</th><th class="num">Cost</th><th class="num">Sell</th><th>Store</th><th>Order IDs</th></tr></thead><tbody>
        ${b.rows.map((x) => `<tr><td class="mono">${esc(x.asin)}${x.multipack ? ` <span class="tag">${x.multipack}-pack</span>` : ''}</td><td class="pp-title" title="${esc(x.title || '')}">${esc(x.title || '')}</td><td class="num">${x.qty}</td>
          <td class="num">${money(x.unit_cost)}</td><td class="num">${x.sell == null ? '—' : money(x.sell)}</td><td>${esc(x.supplier || '')}</td><td class="mono pp-refs" title="${esc(x.order_refs || '')}">${esc(x.order_refs || '')}</td></tr>`).join('')}
        </tbody></table></details>`;
    }).join('');
    openModal(`<div class="pp">
      <div style="display:flex;align-items:center;gap:10px"><h2 style="flex:1;margin:0">PrepOS <span class="muted" style="font-size:18px">Network Prep</span></h2>
        <button class="icon-btn" data-act="close-modal" title="Close (Esc)" style="font-size:20px">✕</button></div>
      ${d.enabled ? '' : '<p class="recon bad">PrepOS isn\'t connected yet (no API token on the server), so nothing can be sent.</p>'}
      ${d.last_error ? `<p class="recon bad">Last problem: ${esc(d.last_error)}</p>` : ''}
      <p class="muted" style="margin:8px 0 14px">Every night at 11:45 PM the Hub sends order items from orders dated ${d.lag_days} days earlier or before (from ${esc(day(d.start))} on), so cancelled orders can be deleted first.
        Items of the same ASIN are combined into one line (total qty, average cost; Sell = Buy Box or last Buy Box + 10%); a 2-pack, 3-pack… set type is sent as Multi-Pack and kept on its own line. An ASIN gets at most one line a day.
        ${d.last_nightly ? `Last nightly run: ${esc(day(d.last_nightly))}.` : ''}</p>
      <div class="pp-bar"><label>Orders through <input type="date" id="ppThrough" value="${esc(p.through)}" min="${esc(d.start)}" max="${esc(d.today)}"></label>
        <span class="muted">${p.through === d.default_through ? '(tonight\'s cutoff)' : ''}</span><span class="spacer"></span>
        <span id="ppSendWrap">${can('editor') && d.enabled && lines.length ? `<button class="btn primary" data-act="pp-send">Send ${lines.length} line${lines.length === 1 ? '' : 's'} now</button>` : ''}</span></div>
      <div class="section-title">Waiting to send · ${lines.length} line${lines.length === 1 ? '' : 's'} · ${units} units · ${money(cost)}</div>
      ${lines.length ? `<div class="items"><table><thead><tr><th>ASIN</th><th>Title</th><th class="num">Qty</th><th class="num">Avg cost</th><th>Store</th><th>Orders</th></tr></thead><tbody>${lines.map(lineRow).join('')}</tbody></table></div>`
        : '<p class="muted">Nothing waiting for orders through this date.</p>'}
      ${p.held.length ? `<p class="muted" style="margin-top:10px">Already sent today, so these wait for the next run: ${p.held.map((x) => `${esc(x.asin)} (${x.qty})`).join(', ')}.</p>` : ''}
      ${p.no_asin.length ? `<p class="recon bad" style="margin-top:10px">${p.no_asin.length} item line${p.no_asin.length === 1 ? '' : 's'} can't be sent without an ASIN: ${p.no_asin.slice(0, 8).map((x) => `${esc(x.store)} ${esc(x.order_ref || '')} (${esc(day(x.order_date))})`).join('; ')}${p.no_asin.length > 8 ? '…' : ''}</p>` : ''}
      <div class="section-title">Sent</div>
      ${hist || '<p class="muted">Nothing sent yet.</p>'}
    </div>`);
    $('#modal').classList.add('wide');
    $('#ppThrough').addEventListener('change', (e) => showPrepos(e.target.value).catch(fail));
    S.ppThrough = p.through; S.ppCount = lines.length; S.ppUnits = units;
  }

  async function showActivity() {
    openModal('<h2>Activity log</h2><div class="history" id="act"><span class="muted">Loading…</span></div>');
    const { changes } = await api('GET', '/api/activity?limit=200');
    $('#act').innerHTML = '<ul>' + changes.map((c) => {
      const order = c.order_id ? ` <a href="#order=${esc(c.order_id)}" data-open="${esc(c.order_id)}">${esc(c.store || '')} ${esc(c.order_ref || '')}</a>` : '';
      return changeLine(c).replace('</li>', `${order}</li>`);
    }).join('') + '</ul>';
  }

  function showConnect() {
    openModal(`<h2>Connect Claude</h2>
      <p>Once connected, you can say things like <i>"add a Sephora order from today, #12345, $86.40, 2× B0XXXXXXX at $43.20"</i> in any Claude chat and it shows up here instantly.</p>
      <ol class="steps">
        <li>In Claude, open <b>Customize → Connectors</b> and choose <b>Add → Add custom connector</b>.</li>
        <li>Name it <b>Myrddin Hub</b> and paste this URL:
          <div class="copybox"><input readonly value="${esc(S.connectorUrl)}" id="cu"><button class="btn" data-act="copy" data-target="cu">Copy</button></div></li>
        <li>Click <b>Connect</b>. You'll land on this site's sign-in page; sign in and click <b>Allow</b>.</li>
      </ol>
      <p class="muted">Claude acts with your login's role (${esc(S.me.role)}), and every change is logged as "${esc(S.me.name || S.me.email)} via Claude".</p>`);
  }

  async function showUsers() {
    const { users, tabs } = await api('GET', '/api/users');
    const tabBoxes = (u) => u.role === 'owner' ? '<span class="muted">all tabs (owner)</span>'
      : `<div class="tabboxes">${tabs.map((t) => `<label><input type="checkbox" data-act="utab" value="${t.key}" ${(u.tabs || []).includes(t.key) ? 'checked' : ''}> ${esc(t.label)}</label>`).join('')}</div>`;
    openModal(`<h2>Users &amp; logins</h2>
      <table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Tabs</th><th>Status</th><th></th></tr></thead><tbody>
      ${users.map((u) => `<tr data-user="${u.id}"><td><input data-act="uname" value="${esc(u.name || '')}" placeholder="Name" style="min-width:120px"></td><td>${esc(u.email)}</td>
        <td><select data-act="role" style="min-width:92px" ${u.id === S.me.id ? 'disabled' : ''}>${['owner', 'editor', 'viewer'].map((r) => `<option ${r === u.role ? 'selected' : ''}>${r}</option>`).join('')}</select></td>
        <td>${tabBoxes(u)}</td>
        <td>${!u.active ? '<span class="tag">disabled</span>' : u.has_password ? 'active' : '<span class="tag">invite sent</span>'}</td>
        <td style="white-space:nowrap"><button class="btn small" data-act="reset">New link</button>
          ${u.id !== S.me.id ? `<button class="btn small ${u.active ? 'danger' : ''}" data-act="toggle">${u.active ? 'Disable' : 'Enable'}</button>` : ''}</td></tr>`).join('')}
      </tbody></table>
      <div class="section-title">Invite someone</div>
      <form id="inviteForm" class="grid">
        <label>Name<input name="name" placeholder="e.g. Rayven (prep)"></label>
        <label>Email<input name="email" type="email" required></label>
        <label>Role<select name="role"><option value="editor">editor — add &amp; edit orders</option><option value="viewer">viewer — read only</option><option value="owner">owner — everything + users</option></select></label>
        <div class="wide"><div class="muted" style="font-size:12px;margin-bottom:4px">Tabs they can open (owners see everything)</div>
          <div class="tabboxes">${tabs.map((t) => `<label><input type="checkbox" name="tabs" value="${t.key}"> ${esc(t.label)}</label>`).join('')}</div></div>
        <div class="wide"><button class="btn primary">Create invite link</button></div>
      </form>
      <div id="inviteOut"></div>
      <p class="muted">Send the link to the person yourself; they set their own password with it. Links expire in 7 days.</p>`);
    $('#modal').classList.add('wide');
    $('#inviteForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        const fd = new FormData(e.target); const body = Object.fromEntries(fd.entries()); body.tabs = fd.getAll('tabs');
        if (body.role !== 'owner' && !body.tabs.length && !confirm('No tabs are ticked, so this person will see nothing until you give them some. Create the link anyway?')) return;
        const { link } = await api('POST', '/api/users', body);
        showLink(link); e.target.reset();
      } catch (err) { fail(err); }
    });
  }

  function showLink(link) {
    $('#inviteOut').innerHTML = `<div class="copybox"><input readonly id="il" value="${esc(link)}"><button class="btn" data-act="copy" data-target="il">Copy</button></div>`;
  }

  function exportCsv() {
    const cols = ['order_date', 'store', 'email', 'order_total', 'order_ref', 'notes', 'items_total', 'units', 'discrepancy', 'missing'];
    const q = (v) => { const s = Array.isArray(v) ? v.join('; ') : String(v ?? ''); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    const lines = [cols.join(',')].concat(filtered().map((o) => cols.map((c) => q(o[c])).join(',')));
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/csv' }));
    a.download = `order-ledger-${new Date().toISOString().slice(0, 10)}.csv`; a.click();
  }

  // ------------------------------------------------------------ events
  document.addEventListener('click', async (e) => {
    const t = e.target;
    if (!t.closest('#menu') && t !== $('#menuBtn')) $('#menu').classList.add('hidden');
    const tr = t.closest('#rows tr[data-id]');
    if (tr) { openOrder(tr.dataset.id); return; }
    const open = t.closest('[data-open]');
    if (open) { e.preventDefault(); closeModal(); openOrder(open.dataset.open); return; }
    if (t.id === 'scrim') { if (!$('#modal').classList.contains('hidden')) closeModal(); else closeDrawer(); return; }

    const btn = t.closest('[data-act]'); if (!btn) return;
    const act = btn.dataset.act;
    try {
      switch (act) {
        case 'close': closeDrawer(); break;
        case 'activity': $('#menu').classList.add('hidden'); await showActivity(); break;
        case 'connect': $('#menu').classList.add('hidden'); showConnect(); break;
        case 'users': $('#menu').classList.add('hidden'); await showUsers(); break;
        case 'export': $('#menu').classList.add('hidden'); exportCsv(); break;
        case 'logout': await api('POST', '/api/logout'); location.href = '/login'; break;
        case 'copy': { const inp = document.getElementById(btn.dataset.target); inp.select(); await navigator.clipboard.writeText(inp.value).catch(() => document.execCommand('copy')); toast('Copied'); break; }
        case 'target': {
          const v = window.prompt('Daily spend target ($)', S.meta.daily_target);
          if (v != null && v !== '') { await api('PUT', '/api/settings/daily_target', { value: v }); toast('Daily target updated'); }
          break;
        }
        case 'add-item': await addItemTo(S.openId); break;
        case 'save-new-item': await saveNewItem(); break;
        case 'del-item': {
          const itemTr = btn.closest('tr[data-item]');
          if (btn.dataset.armed) { await api('DELETE', '/api/items/' + itemTr.dataset.item); }
          else { btn.dataset.armed = '1'; btn.textContent = 'Remove?'; btn.style.color = 'var(--bad)'; setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.textContent = '✕'; btn.style.color = ''; } }, 3000); }
          break;
        }
        case 'del-order': confirmDeleteOrder(S.openId); break;
        case 'dup-order': duplicateOrder(S.openId); break;
        case 'close-modal': closeModal(); break;
        case 'pp-send':
          $('#ppSendWrap').innerHTML = `<span class="confirm"><span class="muted">Send ${S.ppCount} line${S.ppCount === 1 ? '' : 's'} (${S.ppUnits} units) to PrepOS? Lines can't be changed there afterwards.</span>
            <button class="btn small" data-act="pp-cancel">Cancel</button><button class="btn small primary" data-act="pp-yes">Send</button></span>`;
          break;
        case 'pp-cancel': await showPrepos(S.ppThrough); break;
        case 'pp-yes': {
          btn.disabled = true; btn.textContent = 'Sending…';
          try { const r = await api('POST', '/api/prepos/send', { through: S.ppThrough }); toast(`Sent ${r.sent} line${r.sent === 1 ? '' : 's'} (${r.units} units) to PrepOS`); }
          finally { await showPrepos(S.ppThrough); }
          break;
        }
        case 'del-cancel': $('#delWrap').innerHTML = `<button class="btn small danger" data-act="del-order">Delete order</button>`; break;
        case 'del-yes': { const id = S.openId; await api('DELETE', '/api/orders/' + id); closeDrawer(); toast('Order deleted'); break; }
        case 'reset': { const { link } = await api('POST', `/api/users/${btn.closest('tr').dataset.user}/reset`); if (!$('#inviteOut')) break; showLink(link); toast('New sign-in link created below'); break; }
        case 'toggle': { const uid = btn.closest('tr').dataset.user; const enable = btn.textContent.trim() === 'Enable'; await api('PATCH', `/api/users/${uid}`, { active: enable }); await showUsers(); break; }
      }
    } catch (err) { fail(err); }
  });

  document.addEventListener('focusin', (e) => {
    const i = e.target;
    if (i.matches?.('#drawer tr[data-item] input')) i.dataset.orig = i.value;
  });

  document.addEventListener('change', async (e) => {
    const i = e.target;
    if (i.matches('#drawer [data-o]')) await saveOrderField(i);
    else if (i.matches('#drawer tr[data-item] input')) await saveItemField(i);
    else if (i.matches('input[data-act="utab"]')) {
      const tr = i.closest('tr'); const tabsOn = $$('input[data-act="utab"]', tr).filter((x) => x.checked).map((x) => x.value);
      try { await api('PATCH', `/api/users/${tr.dataset.user}`, { tabs: tabsOn }); toast('Tabs updated'); } catch (err) { fail(err); i.checked = !i.checked; }
    }
    else if (i.matches('input[data-act="uname"]')) {
      try { await api('PATCH', `/api/users/${i.closest('tr').dataset.user}`, { name: i.value }); toast('Name updated'); } catch (err) { fail(err); }
    }
    else if (i.matches('select[data-act="role"]')) {
      try { await api('PATCH', `/api/users/${i.closest('tr').dataset.user}`, { role: i.value }); toast('Role updated'); await showUsers(); } catch (err) { fail(err); }
    }
  });

  document.addEventListener('focusout', () => {
    // Apply a live update that arrived while the person was typing in the drawer.
    setTimeout(() => {
      const a = document.activeElement;
      if (S.pendingDrawerRefresh && !(a && $('#drawer').contains(a) && /INPUT|TEXTAREA|SELECT/.test(a.tagName))) refreshDrawer();
    }, 250);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { if (!$('#modal').classList.contains('hidden')) closeModal(); else if (!$('#drawer').classList.contains('hidden')) closeDrawer(); }
    if (e.key === 'Enter' && e.target.matches('#drawer tr[data-item] input, #drawer input[data-o]')) e.target.blur();
    if (e.key === 'Enter' && e.target.matches('#newItem input')) { e.preventDefault(); saveNewItem(); }
    if (e.key === '/' && document.activeElement === document.body) { e.preventDefault(); $('#q').focus(); }
  });

  // Search and the All / Discrepancy / Missing info filter are remembered per browser (Nick, Nov 1 2026).
  const saveFilters = () => { try { localStorage.setItem('orderFilters', JSON.stringify({ q: S.q, filter: S.filter })); } catch { /* storage blocked */ } };
  try {
    const v = JSON.parse(localStorage.getItem('orderFilters') || 'null');
    if (v) {
      if (typeof v.q === 'string') { S.q = v.q; $('#q').value = v.q; }
      if (['all', 'discrepancy', 'missing'].includes(v.filter)) { S.filter = v.filter; $$('.chip[data-filter]').forEach((x) => x.classList.toggle('on', x.dataset.filter === v.filter)); }
    }
  } catch { /* storage blocked or old value */ }
  let qTimer;
  $('#q').addEventListener('input', (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.q = e.target.value; S.shown = PAGE; saveFilters(); render(); }, 120); });
  $$('.chip').forEach((c) => c.addEventListener('click', () => {
    $$('.chip').forEach((x) => x.classList.toggle('on', x === c)); S.filter = c.dataset.filter; S.shown = PAGE; saveFilters(); render();
  }));
  $('#moreBtn').addEventListener('click', () => { S.shown += PAGE; render(); });
  $('#addBtn').addEventListener('click', newOrder);
  $('#preposBtn').addEventListener('click', () => showPrepos().catch(fail));
  $('#menuBtn').addEventListener('click', () => $('#menu').classList.toggle('hidden'));
  window.addEventListener('hashchange', () => { const m = location.hash.match(/^#order=(.+)$/); if (m && m[1] !== S.openId) openOrder(m[1]); });

  boot().catch(fail);
})();
