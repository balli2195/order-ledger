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
  async function boot() {
    const me = await api('GET', '/api/me');
    S.me = me.user; S.connectorUrl = me.connector_url;
    $('#whoami').textContent = S.me.name || S.me.email;
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

  function rowHtml(o) {
    const discTitle = o.discrepancy ? `Items total ${money(o.items_total)} vs order total ${money(o.order_total)}` : 'Items total matches order total';
    const missTitle = o.missing.length ? 'Missing: ' + o.missing.join(', ') : 'All item fields filled in';
    return `<tr data-id="${esc(o.id)}" class="${S.openId === o.id ? 'sel' : ''}">
      <td>${esc(fmtDate(o.order_date))}</td><td>${esc(o.store)}</td><td class="email">${esc(o.email || '')}</td>
      <td class="num">${money(o.order_total)}</td><td class="mono">${esc(o.order_ref || '')}</td>
      <td class="notes" title="${esc(o.notes || '')}">${esc(o.notes || '—')}</td>
      <td class="center"><span class="dot ${o.discrepancy ? 'bad' : ''}" title="${esc(discTitle)}"></span></td>
      <td class="center"><span class="dot ${o.missing.length ? 'bad' : ''}" title="${esc(missTitle)}"></span></td>
      <td class="order-act">${can('editor') ? `<button class="row-add" data-add-items="${esc(o.id)}" title="Add items to this order">+ Items</button><button class="row-del" data-del-order="${esc(o.id)}" title="Delete order" aria-label="Delete order">${TRASH}</button>` : ''}</td></tr>`;
  }

  const TRASH = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/></svg>`;

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
    const o = await api('POST', `/api/orders/${id}/items`, [{ qty_ordered: 1 }]);
    upsertLocal(o); render(); renderDrawer(o);
    const rows = $$('#drawer tr[data-item]'); $('input[data-f="asin"]', rows[rows.length - 1])?.focus();
  }

  document.addEventListener('click', (e) => {
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
    S.openId = id; S.isNew = false;
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
        ${can('editor') ? `<span id="delWrap"><button class="btn small danger" data-act="del-order">Delete order</button></span>` : ''}
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
          <tfoot><tr><td colspan="6">Items total</td><td class="num">${money(itemsTotal)}</td><td></td></tr></tfoot>
        </table></div>
        ${can('editor') ? `<div style="margin-top:8px"><button class="btn small" data-act="add-item">+ Add item</button></div>` : ''}
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
    if (c.entity === 'setting') return `set daily target to ${moneyWhole(c.after?.daily_target)}`;
    const what = c.entity === 'item' ? `item ${esc(c.after?.asin || c.before?.asin || '')}` : 'order';
    if (c.action === 'create') return `added ${what}`;
    if (c.action === 'delete') return `deleted ${what}`;
    return `changed ${what}: ` + fields(c.after).map((k) => `${esc(k.replace(/_/g, ' '))} ${esc(fmtVal(c.before?.[k]))} → ${esc(fmtVal(c.after?.[k]))}`).join('; ');
  }
  const fmtVal = (v) => v == null || v === '' ? '∅' : String(v);

  function changeLine(c) {
    const when = new Date(c.at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    const tag = `<span class="tag ${c.source === 'chat' ? 'chat' : ''}">${esc(c.source)}</span>`;
    return `<li>${tag} <b>${esc(c.who || (c.source === 'import' ? 'Import' : 'Someone'))}</b> ${describe(c)} <span class="muted">· ${esc(when)}</span></li>`;
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
    const blankRow = () => `<tr class="new-item">
      <td class="c-asin"><input class="mono" data-f="asin" placeholder="ASIN"></td><td><input data-f="product_title" placeholder="Title"></td>
      <td class="c-shade"><input data-f="shade" placeholder="Shade"></td><td class="c-set"><input data-f="set_type" list="dlSets" placeholder="Set type"></td>
      <td class="c-qty"><input data-f="qty_ordered" class="num" value="1"></td><td class="c-cost"><input data-f="cost_per_unit" class="num" placeholder="0.00"></td>
      <td><button class="icon-btn" data-act="drop-new-row">✕</button></td></tr>`;
    $('#drawer').innerHTML = `
      <div class="drawer-head"><h2>New order</h2><button class="icon-btn" data-act="close" style="font-size:20px">✕</button></div>
      <div class="drawer-body">
        <form id="newForm">
        <div class="grid">
          <label>Date<input type="date" name="order_date" value="${iso}" required></label>
          <label>Store<input name="store" list="dlStores" required autofocus></label>
          <label>Order total<input name="order_total" class="num" inputmode="decimal" placeholder="0.00"></label>
          <label>Order ID<input name="order_ref" class="mono"></label>
          <label style="grid-column: span 2">Email<input name="email" list="dlEmails" value="${esc(S.meta.emails?.[0] || '')}"></label>
          <label class="wide">Notes<textarea name="notes" rows="2"></textarea></label>
        </div>
        <div class="section-title">Items</div>
        <div class="items"><table><thead><tr><th>ASIN</th><th>Title</th><th>Shade</th><th>Set type</th><th class="num">Qty</th><th class="num">Cost/unit</th><th></th></tr></thead>
          <tbody id="newItems">${blankRow()}</tbody></table></div>
        <div style="margin-top:8px;display:flex;gap:8px"><button type="button" class="btn small" data-act="add-new-row">+ Another item</button>
          <span class="spacer"></span><button type="button" class="btn" data-act="close">Cancel</button><button class="btn primary" type="submit">Save order</button></div>
        </form>
      </div>`;
    $('#newForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target); const body = Object.fromEntries(fd.entries());
      body.items = $$('#newItems tr').map((tr) => Object.fromEntries($$('input', tr).map((i) => [i.dataset.f, i.value])))
        .filter((it) => (it.asin || it.product_title || it.cost_per_unit));
      try {
        const o = await api('POST', '/api/orders', body);
        upsertLocal(o); render(); toast('Order saved'); openOrder(o.id);
      } catch (err) { fail(err); }
    });
    S._blankRow = blankRow;
  }

  // ------------------------------------------------------------ modals
  function openModal(html) { $('#modal').innerHTML = html; $('#modal').classList.remove('hidden'); $('#scrim').classList.remove('hidden'); }
  function closeModal() { $('#modal').classList.add('hidden'); $('#modal').innerHTML = ''; if ($('#drawer').classList.contains('hidden')) $('#scrim').classList.add('hidden'); }

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
    const { users } = await api('GET', '/api/users');
    openModal(`<h2>Users &amp; logins</h2>
      <table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th></th></tr></thead><tbody>
      ${users.map((u) => `<tr data-user="${u.id}"><td>${esc(u.name)}</td><td>${esc(u.email)}</td>
        <td><select data-act="role" ${u.id === S.me.id ? 'disabled' : ''}>${['owner', 'editor', 'viewer'].map((r) => `<option ${r === u.role ? 'selected' : ''}>${r}</option>`).join('')}</select></td>
        <td>${!u.active ? '<span class="tag">disabled</span>' : u.has_password ? 'active' : '<span class="tag">invite sent</span>'}</td>
        <td style="white-space:nowrap"><button class="btn small" data-act="reset">New link</button>
          ${u.id !== S.me.id ? `<button class="btn small ${u.active ? 'danger' : ''}" data-act="toggle">${u.active ? 'Disable' : 'Enable'}</button>` : ''}</td></tr>`).join('')}
      </tbody></table>
      <div class="section-title">Invite someone</div>
      <form id="inviteForm" class="grid">
        <label>Name<input name="name" placeholder="e.g. Rayven (prep)"></label>
        <label>Email<input name="email" type="email" required></label>
        <label>Role<select name="role"><option value="editor">editor — add &amp; edit orders</option><option value="viewer">viewer — read only</option><option value="owner">owner — everything + users</option></select></label>
        <div class="wide"><button class="btn primary">Create invite link</button></div>
      </form>
      <div id="inviteOut"></div>
      <p class="muted">Send the link to the person yourself; they set their own password with it. Links expire in 7 days.</p>`);
    $('#inviteForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        const { link } = await api('POST', '/api/users', Object.fromEntries(new FormData(e.target).entries()));
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
        case 'del-item': {
          const itemTr = btn.closest('tr[data-item]');
          if (btn.dataset.armed) { await api('DELETE', '/api/items/' + itemTr.dataset.item); }
          else { btn.dataset.armed = '1'; btn.textContent = 'Remove?'; btn.style.color = 'var(--bad)'; setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.textContent = '✕'; btn.style.color = ''; } }, 3000); }
          break;
        }
        case 'del-order': confirmDeleteOrder(S.openId); break;
        case 'del-cancel': $('#delWrap').innerHTML = `<button class="btn small danger" data-act="del-order">Delete order</button>`; break;
        case 'del-yes': { const id = S.openId; await api('DELETE', '/api/orders/' + id); closeDrawer(); toast('Order deleted'); break; }
        case 'add-new-row': $('#newItems').insertAdjacentHTML('beforeend', S._blankRow()); break;
        case 'drop-new-row': btn.closest('tr').remove(); break;
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
    else if (i.matches('select[data-act="role"]')) {
      try { await api('PATCH', `/api/users/${i.closest('tr').dataset.user}`, { role: i.value }); toast('Role updated'); } catch (err) { fail(err); }
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
    if (e.key === '/' && document.activeElement === document.body) { e.preventDefault(); $('#q').focus(); }
  });

  let qTimer;
  $('#q').addEventListener('input', (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.q = e.target.value; S.shown = PAGE; render(); }, 120); });
  $$('.chip').forEach((c) => c.addEventListener('click', () => {
    $$('.chip').forEach((x) => x.classList.toggle('on', x === c)); S.filter = c.dataset.filter; S.shown = PAGE; render();
  }));
  $('#moreBtn').addEventListener('click', () => { S.shown += PAGE; render(); });
  $('#addBtn').addEventListener('click', newOrder);
  $('#menuBtn').addEventListener('click', () => $('#menu').classList.toggle('hidden'));
  window.addEventListener('hashchange', () => { const m = location.hash.match(/^#order=(.+)$/); if (m && m[1] !== S.openId) openOrder(m[1]); });

  boot().catch(fail);
})();
