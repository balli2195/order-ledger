/* Shared helpers for the Leads and Stores pages. */
window.LG = (() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (n, dp = 2) => n == null || n === '' ? '—' : Number(n).toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: dp, maximumFractionDigits: dp });
  const num = (n) => n == null || n === '' ? '—' : Number(n).toLocaleString('en-US');
  const pct = (n) => n == null || n === '' ? '—' : (Number(n) * 100).toFixed(1) + '%';
  const fmtDate = (d) => { if (!d) return ''; const [y, m, day] = d.slice(0, 10).split('-').map(Number); return new Date(y, m - 1, day).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }); };
  const RANK = { viewer: 1, editor: 2, owner: 3 };
  const st = { me: null };
  const can = (role) => RANK[st.me?.role] >= RANK[role];

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
    t.className = 'toast' + (err ? ' err' : ''); t.textContent = msg;
    $('#toasts').appendChild(t); setTimeout(() => t.remove(), err ? 6000 : 3500);
  }
  const fail = (e) => toast(e.message || String(e), true);


  function applyTabs(me) {   // hide tabs this login can't open
    const ok = new Set(me.tabs || []);
    document.querySelectorAll('nav.tabs a[data-tab]').forEach((a) => { a.hidden = !ok.has(a.dataset.tab); });
    document.querySelectorAll('[data-needs-tab]').forEach((el) => { el.hidden = !ok.has(el.dataset.needsTab); });
  }

  async function header() {
    const me = await api('GET', '/api/me');
    st.me = me.user; st.connectorUrl = me.connector_url;
    $('#whoami').textContent = st.me.name || st.me.email;
    applyTabs(st.me);
    $$('.owner-only').forEach((el) => el.classList.toggle('hidden', st.me.role !== 'owner'));
    $('#menuBtn').addEventListener('click', () => $('#menu').classList.toggle('hidden'));
    document.addEventListener('click', async (e) => {
      if (!e.target.closest('#menu') && e.target !== $('#menuBtn')) $('#menu').classList.add('hidden');
      if (e.target.closest('[data-act="logout"]')) { await api('POST', '/api/logout'); location.href = '/login'; }
      const go = e.target.closest('#menu [data-go]'); if (go) location.href = go.dataset.go;
    });
    return st.me;
  }

  function live(onMsg) {
    let es;
    const connect = () => {
      es = new EventSource('/api/events');
      es.onopen = () => { $('#live')?.classList.add('on'); };
      es.onerror = () => { $('#live')?.classList.remove('on'); };
      es.onmessage = (ev) => { try { onMsg(JSON.parse(ev.data)); } catch { /* ignore */ } };
    };
    connect();
  }

  function changeLine(c) {
    const when = new Date(c.at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    const skip = new Set(['id', 'created_at', 'updated_at', 'created_by', 'updated_by', 'extra', 'airtable_id']);
    const v = (x) => x == null || x === '' ? '∅' : Array.isArray(x) ? x.join(', ') : String(x).slice(0, 80);
    let what;
    if (c.action === 'create') what = 'added the lead';
    else if (c.action === 'delete') what = 'deleted the lead';
    else what = 'changed ' + Object.keys(c.after || {}).filter((k) => !skip.has(k)).map((k) => `${esc(k.replace(/_/g, ' '))} ${esc(v(c.before?.[k]))} → ${esc(v(c.after?.[k]))}`).join('; ');
    return `<li><span class="tag ${c.source === 'chat' ? 'chat' : ''}">${esc(c.source)}</span> <b>${esc(c.who || 'Someone')}</b> ${what} <span class="muted">· ${esc(when)}</span></li>`;
  }

  return { $, $$, esc, money, num, pct, fmtDate, api, toast, fail, header, live, can, st, changeLine };
})();
