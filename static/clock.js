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
    el.innerHTML = open
      ? `<span class="cm-state" title="Clocked in at ${tFmt.format(open)}"><span class="cm-dot on"></span>In since ${tFmt.format(open)} · <b>${hm(since)}</b></span>
         <button class="btn small cm-btn out" data-cm="out" ${viewing ? 'disabled title="Off while you view the Hub as someone else"' : ''}>Clock out</button>`
      : `<span class="cm-state"><span class="cm-dot"></span>${off ? 'Time clock off' : 'Clocked out'}${d.today_hours ? ` · today ${hm(d.today_hours)}` : ''}</span>
         <button class="btn small primary cm-btn" data-cm="in" ${viewing || off ? 'disabled' : ''} ${viewing ? 'title="Off while you view the Hub as someone else"' : ''}>Clock in</button>`;
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

  function init(me) {
    st.me = me;
    if (!me || !(me.tabs || []).includes('timeclock')) return;
    const who = document.querySelector('.who'); if (!who || st.el) return;
    st.el = document.createElement('div'); st.el.className = 'clock-mini'; st.el.hidden = true;
    who.appendChild(st.el);
    st.el.addEventListener('click', (e) => { const b = e.target.closest('[data-cm]'); if (b) punch(b.dataset.cm, b); });
    refresh();
    clearInterval(st.timer); st.timer = setInterval(() => { if (!document.hidden) { if (st.data?.open) draw(); } }, 30000);
    setInterval(() => { if (!document.hidden) refresh(); }, 120000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  }
  return { init, refresh };
})();
