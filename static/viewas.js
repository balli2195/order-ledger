/* Owner: "View as…" — see the whole Hub the way another login sees it (read-only), with a banner to go back. */
window.ViewAs = (() => {
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const call = async (method, url, body) => {
    const r = await fetch(url, { method, headers: { 'X-Requested-With': 'ledger', 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || r.status + ' error'); return d;
  };
  const back = async () => { await call('DELETE', '/api/view-as'); location.reload(); };

  async function picker() {
    let users;
    try { ({ users } = await call('GET', '/api/view-as')); } catch (e) { alert(e.message); return; }
    const wrap = document.createElement('div'); wrap.className = 'confirm-back';
    wrap.innerHTML = `<div class="confirm-box" role="dialog" aria-modal="true" style="max-width:480px">
      <h3>View the Hub as…</h3>
      <p class="muted" style="font-size:13px;margin:0 0 10px">You'll see exactly the tabs and data that login sees. Nothing can be changed while you're viewing.</p>
      <div style="display:grid;gap:6px;max-height:50vh;overflow:auto">${users.length ? users.map((u) => `<button class="btn" data-va="${u.id}" style="justify-content:space-between;text-align:left;width:100%;flex-wrap:wrap;gap:2px 10px;white-space:normal">
        <span><b>${esc(u.label)}</b> <span class="muted" style="font-weight:400">${esc(u.role)}</span></span>
        <span class="muted" style="font-weight:400;font-size:12px">${esc(u.tabs.join(', ') || 'no tabs')}</span></button>`).join('') : '<p class="muted">No other logins yet.</p>'}</div>
      <div class="confirm-actions"><button class="btn" data-va-x>Cancel</button></div></div>`;
    document.body.appendChild(wrap);
    wrap.addEventListener('click', async (e) => {
      if (e.target === wrap || e.target.closest('[data-va-x]')) return wrap.remove();
      const b = e.target.closest('[data-va]'); if (!b) return;
      try { const r = await call('POST', '/api/view-as', { user_id: +b.dataset.va }); location.href = r.home && r.home !== '/no-access' ? (canStay(r.user) ? location.pathname : r.home) : '/no-access'; }
      catch (err) { alert(err.message); }
    });
  }
  const canStay = (u) => { const a = document.querySelector('nav.tabs a.on[data-tab]'); return !a || (u.tabs || []).includes(a.dataset.tab); };

  function init(me) {
    if (!me) return;
    const viewing = me.viewed_by;
    if (viewing) {
      document.body.classList.add('viewing-as');
      const bar = document.createElement('div'); bar.className = 'viewas-bar';
      bar.innerHTML = `<span>Viewing the Hub as <b>${esc(me.name || me.email)}</b> <span class="muted">(${esc(me.role)})</span>. Read-only: nothing you click here changes anything.</span>
        <span style="flex:1"></span><button class="btn small" data-va-switch>Someone else…</button><button class="btn small primary" data-va-back>Back to my view</button>`;
      document.body.prepend(bar);
      bar.querySelector('[data-va-back]').addEventListener('click', back);
      bar.querySelector('[data-va-switch]').addEventListener('click', picker);
    }
    if (me.role === 'owner' || viewing) {
      const menu = document.getElementById('menu'); const hr = menu?.querySelector('hr');
      if (menu && !menu.querySelector('[data-va-open]')) {
        const b = document.createElement('button'); b.dataset.vaOpen = '1'; b.type = 'button';
        b.textContent = viewing ? 'Back to my view' : 'View as someone…';
        b.addEventListener('click', viewing ? back : picker);
        menu.insertBefore(b, hr || null);
      }
    }
  }
  return { init, back };
})();
