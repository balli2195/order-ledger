/* Clock in / Clock out under the Menu button, on every page, for anyone on the time clock. */
window.TimeClock = (() => {
  const st = { el: null, data: null, timer: null, me: null };
  const call = async (method, url, body) => {
    const r = await fetch(url, { method, headers: { 'X-Requested-With': 'ledger', 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || r.status + ' error'); return d;
  };
  const hm = (h) => { const m = Math.max(0, Math.round(h * 60)); return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`; };
  const tFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', hour: 'numeric', minute: '2-digit' });
  const say = (msg, err) => {
    const box = document.getElementById('toasts'); if (!box) { if (err) alert(msg); return; }
    const t = document.createElement('div'); t.className = 'toast' + (err ? ' err' : ''); t.textContent = msg; box.appendChild(t); setTimeout(() => t.remove(), err ? 6000 : 3500);
  };

  function draw() {
    const d = st.data, el = st.el; if (!el) return;
    if (!d || !d.show) { el.hidden = true; document.querySelector('.who')?.classList.remove('has-clock'); return; }
    el.hidden = false; document.querySelector('.who')?.classList.add('has-clock');
    const open = d.open ? new Date(d.open) : null;
    const viewing = !!st.me?.viewed_by;
    const off = d.person && d.active === false;
    const since = open ? (Date.now() - open) / 3.6e6 : 0;
    const team = (d.team || []).map((t) => {
      const at = t.open ? new Date(t.open) : null;
      const tip = at ? `${t.name}: clocked in since ${tFmt.format(at)} (${hm((Date.now() - at) / 3.6e6)})` : `${t.name}: clocked out`;
      return `<a class="tm-dot ${at ? 'on' : ''}" href="/timeclock" title="${tip}" aria-label="${tip}">${t.initial}</a>`;
    }).join('');
    if (d.team && !d.person) { el.innerHTML = `<span class="tm-row">${team}</span>`; return; }
    el.innerHTML = (team ? `<span class="tm-row">${team}</span>` : '') + (open
      ? `<span class="cm-state" title="Clocked in at ${tFmt.format(open)}"><span class="cm-dot on"></span>In since ${tFmt.format(open)} · <b>${hm(since)}</b></span>
         <button class="btn small cm-btn out" data-cm="out" ${viewing ? 'disabled title="Off while you view the Hub as someone else"' : ''}>Clock out</button>`
      : `<span class="cm-state"><span class="cm-dot"></span>${off ? 'Time clock off' : 'Clocked out'}${d.today_hours ? ` · today ${hm(d.today_hours)}` : ''}</span>
         <button class="btn small primary cm-btn" data-cm="in" ${viewing || off ? 'disabled' : ''} ${viewing ? 'title="Off while you view the Hub as someone else"' : ''}>Clock in</button>`);
  }

  async function refresh() {
    try { st.data = await call('GET', '/api/timeclock/status'); } catch { return; }
    draw();
  }

  async function punch(dir, btn) {
    btn.disabled = true;
    try {
      await call('POST', `/api/timeclock/${dir}`, {});
      say(dir === 'in' ? 'Clocked in' : 'Clocked out');
      await refresh();
      window.dispatchEvent(new CustomEvent('timeclock:changed'));
    } catch (e) { say(e.message, true); btn.disabled = false; }
  }

  function ownerMenu(me) {   // owner: Time clock and Commissions live in the Menu instead of the tab bar (Nick, Nov 2 / Nov 4 2026)
    if (me?.role !== 'owner') return;
    const menu = document.getElementById('menu');
    [['commissions', '/commissions', 'Commissions'], ['timeclock', '/timeclock', 'Time clock']].forEach(([key, href, label]) => {
      const tab = document.querySelector(`nav.tabs a[data-tab="${key}"]`);
      if (tab && !tab.classList.contains('on')) tab.hidden = true;   // still shown while you're on the page
      if (menu && !menu.querySelector(`[data-go="${href}"]`)) {
        const b = document.createElement('button'); b.type = 'button'; b.dataset.go = href; b.textContent = label;
        b.addEventListener('click', () => { location.href = href; });
        menu.insertBefore(b, menu.firstChild);
      }
    });
    bonusSwitch();
  }

  // Owner: turn the order bonus on for today only (Nick, Nov 5 2026) — a button in the Menu, and one on the Time clock page
  const money0 = (n) => '$' + Number(n || 0).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const bon = { s: null, btns: [] };
  function bonusLabel() {
    const s = bon.s;
    for (const [b, compact] of bon.btns) {
      if (!s) { b.textContent = 'Bonus today'; continue; }
      b.classList.toggle('primary', compact && s.on);
      b.title = `${money0(s.pay)} per ${money0(s.per)} ordered, today only`;
      b.innerHTML = compact
        ? `${s.on ? '🎉 ' : ''}${s.user}'s bonus today: <b>${s.on ? 'On' : 'Off'}</b>`
        : `${s.on ? '🎉 ' : ''}${s.user}'s bonus today: <b>${s.on ? 'On' : 'Off'}</b><span class="sub" style="display:block;font-size:11.5px;opacity:.75">${money0(s.pay)} per ${money0(s.per)} ordered · ${s.on ? 'click to turn off' : 'click to turn on'}</span>`;
    }
  }
  async function bonusBind(b, compact) {
    if (!b || bon.btns.some(([x]) => x === b)) return;
    bon.btns.push([b, compact]);
    if (!bon.s) { try { bon.s = await call('GET', '/api/order-bonus/switch'); } catch { return; } }
    b.hidden = false; bonusLabel();
    b.addEventListener('click', async (e) => {
      e.stopPropagation();
      try { bon.s = await call('POST', '/api/order-bonus/switch', { on: !bon.s.on }); bonusLabel(); say(`${bon.s.user}'s bonus is ${bon.s.on ? 'on for today' : 'off'}`); }
      catch (err) { say('Could not change the bonus: ' + (err.message || err), true); }
    });
  }
  function bonusSwitch() {   // Nick, Nov 5 2026: only on the Time clock page, not in the Menu
    const page = document.getElementById('bonusBtn');   // Time clock page
    if (page) bonusBind(page, true);
  }

  async function taskBadge() {   // open-task count on the Tasks tab
    const a = document.querySelector('nav.tabs a[data-tab="tasks"]'); if (!a || a.hidden) return;
    try {
      const { open } = await call('GET', '/api/tasks/count');
      let b = a.querySelector('.tab-count'); if (!b) { b = document.createElement('span'); b.className = 'tab-count'; a.appendChild(b); }
      b.textContent = open || ''; b.hidden = !open;
    } catch { /* ignore */ }
  }

  function init(me) {
    st.me = me;
    ownerMenu(me);
    taskBadge(); setInterval(() => { if (!document.hidden) taskBadge(); }, 120000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) taskBadge(); });
    window.addEventListener('tasks:changed', taskBadge);
    if (!me || !(me.tabs || []).includes('timeclock')) return;
    const who = document.querySelector('.who'); if (!who || st.el) return;
    st.el = document.createElement('div'); st.el.className = 'clock-mini'; st.el.hidden = true;
    who.appendChild(st.el);
    st.el.addEventListener('click', (e) => { const b = e.target.closest('[data-cm]'); if (b) punch(b.dataset.cm, b); });
    refresh();
    clearInterval(st.timer); st.timer = setInterval(() => { if (!document.hidden) { if (st.data?.open) draw(); } }, 30000);
    setInterval(() => { if (!document.hidden) refresh(); }, me.role === 'owner' ? 45000 : 120000);
    if (me.role === 'owner') {   // the team circles change when someone clocks in or out
      try {
        const es = new EventSource('/api/events'); let t = null;
        es.onmessage = (ev) => { try { if (JSON.parse(ev.data).t === 'timeclock') { clearTimeout(t); t = setTimeout(refresh, 800); } } catch { /* ignore */ } };
      } catch { /* ignore */ }
    }
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  }
  return { init, refresh, taskBadge };
})();
