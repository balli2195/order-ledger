/* Order bonus day (Nick, Nov 4 2026): a big bonus figure under the tabs that throws confetti at every $1,000 milestone.
   Shown to the person earning it and to the owner, only on the bonus day (GET /api/order-bonus). */
window.OrderBonus = (() => {
  const st = { el: null, data: null, timer: null };
  const money = (n, dp = 2) => Number(n || 0).toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: dp, maximumFractionDigits: dp });
  const key = (b) => `ob:${b.date}:${(b.user || '').toLowerCase()}:${b.self ? 'me' : 'owner'}`;
  const seen = (b) => { try { return Number(localStorage.getItem(key(b)) || 0); } catch { return 0; } };
  const mark = (b) => { try { localStorage.setItem(key(b), String(b.milestones)); } catch { /* ignore */ } };

  const css = `
  .ob { position: relative; overflow: hidden; margin: 14px 0 4px; border-radius: 16px; padding: 18px 22px;
        background: linear-gradient(120deg, #2b1a0c 0%, #6b2a10 45%, #b4441c 75%, #e08a2c 100%); color: #fff8f2;
        display: grid; grid-template-columns: auto 1fr; gap: 6px 26px; align-items: center; box-shadow: 0 10px 30px -12px rgba(180,68,28,.55); }
  .ob::after { content: ''; position: absolute; inset: 0; background: linear-gradient(100deg, transparent 30%, rgba(255,255,255,.18) 50%, transparent 70%);
        transform: translateX(-100%); animation: ob-shine 4.5s ease-in-out infinite; pointer-events: none; }
  @keyframes ob-shine { 0%, 55% { transform: translateX(-100%); } 85%, 100% { transform: translateX(100%); } }
  .ob-k { font-size: 12px; letter-spacing: .14em; text-transform: uppercase; opacity: .85; font-weight: 600; }
  .ob-v { font: 600 64px/1 var(--serif, Georgia, serif); letter-spacing: -.01em; text-shadow: 0 2px 18px rgba(255,200,120,.45); font-variant-numeric: tabular-nums; }
  .ob-v.pop { animation: ob-pop .9s cubic-bezier(.2,1.6,.4,1); }
  @keyframes ob-pop { 0% { transform: scale(1); } 35% { transform: scale(1.35) rotate(-3deg); color: #ffe08a; } 100% { transform: scale(1); } }
  .ob-side { min-width: 0; }
  .ob-line { font-size: 15px; margin-bottom: 8px; }
  .ob-line b { font-weight: 700; }
  .ob-bar { height: 12px; border-radius: 99px; background: rgba(255,255,255,.18); overflow: hidden; }
  .ob-bar i { display: block; height: 100%; border-radius: 99px; background: linear-gradient(90deg, #ffd36b, #fff1c2); transition: width .8s ease; box-shadow: 0 0 12px rgba(255,220,140,.8); }
  .ob-sub { font-size: 12.5px; opacity: .85; margin-top: 6px; }
  .ob-badges { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 8px; }
  .ob-badges span { font-size: 11px; font-weight: 700; background: rgba(255,255,255,.16); border-radius: 99px; padding: 2px 8px; }
  @media (max-width: 640px) { .ob { grid-template-columns: 1fr; } .ob-v { font-size: 52px; } }
  .ob-confetti { position: fixed; inset: 0; pointer-events: none; z-index: 9999; }
  .ob-toast { position: fixed; left: 50%; top: 22%; transform: translate(-50%, -50%) scale(.6); opacity: 0; z-index: 10000; pointer-events: none;
        background: #fff8f2; color: #6b2a10; border-radius: 18px; padding: 18px 28px; text-align: center; box-shadow: 0 20px 60px -10px rgba(0,0,0,.35);
        font: 600 30px/1.15 var(--serif, Georgia, serif); transition: transform .35s cubic-bezier(.2,1.6,.4,1), opacity .25s; }
  .ob-toast small { display: block; font: 500 14px/1.3 var(--sans, system-ui); color: #8a5a3c; margin-top: 4px; }
  .ob-toast.on { transform: translate(-50%, -50%) scale(1); opacity: 1; }`;

  function confetti() {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
    const c = document.createElement('canvas'); c.className = 'ob-confetti'; document.body.appendChild(c);
    const ctx = c.getContext('2d'), dpr = window.devicePixelRatio || 1;
    const W = c.width = innerWidth * dpr, H = c.height = innerHeight * dpr;
    const colors = ['#ffd36b', '#ff7a45', '#b4441c', '#4fc08d', '#58a6ff', '#f778ba', '#fff1c2'];
    const ps = [];
    for (const side of [0, 1]) for (let i = 0; i < 110; i++) {
      const a = (side ? -1 : 1) * (Math.PI / 3 + Math.random() * Math.PI / 5);
      const v = (9 + Math.random() * 11) * dpr;
      ps.push({ x: side ? W : 0, y: H * .75, vx: Math.sin(a) * v, vy: -Math.cos(a) * v * 1.1, r: (4 + Math.random() * 5) * dpr,
        c: colors[(Math.random() * colors.length) | 0], rot: Math.random() * 6, vr: (Math.random() - .5) * .4, shape: Math.random() < .3 });
    }
    const t0 = performance.now();
    (function frame(t) {
      ctx.clearRect(0, 0, W, H);
      for (const p of ps) {
        p.vy += .32 * dpr; p.vx *= .99; p.vy *= .99; p.x += p.vx; p.y += p.vy; p.rot += p.vr;
        ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot); ctx.fillStyle = p.c;
        if (p.shape) { ctx.beginPath(); ctx.arc(0, 0, p.r * .6, 0, 7); ctx.fill(); } else ctx.fillRect(-p.r, -p.r * .45, p.r * 2, p.r * .9);
        ctx.restore();
      }
      if (t - t0 < 4200) requestAnimationFrame(frame); else c.remove();
    })(t0);
  }

  function celebrate(b) {
    confetti(); setTimeout(confetti, 450);
    const t = document.createElement('div'); t.className = 'ob-toast';
    t.innerHTML = `${money(b.milestones * b.per, 0)} ordered! <small>${b.self ? 'Your' : (b.user + "'s")} bonus is now ${money(b.bonus, 0)} 🎉</small>`;
    document.body.appendChild(t); requestAnimationFrame(() => t.classList.add('on'));
    setTimeout(() => { t.classList.remove('on'); setTimeout(() => t.remove(), 400); }, 2600);
    st.el?.querySelector('.ob-v')?.classList.remove('pop'); void st.el?.offsetWidth; st.el?.querySelector('.ob-v')?.classList.add('pop');
  }

  function draw(b) {
    const into = (b.total - b.milestones * b.per) / b.per;
    st.el.innerHTML = `<div><div class="ob-k">${b.self ? 'Your bonus today' : esc(b.user) + "'s bonus today"}</div><div class="ob-v">${money(b.bonus, 0)}</div></div>
      <div class="ob-side">
        <div class="ob-line"><b>${money(b.pay, 0)}</b> for every <b>${money(b.per, 0)}</b> ordered today · <b>${money(b.total)}</b> so far (${b.orders} order${b.orders === 1 ? '' : 's'})</div>
        <div class="ob-bar"><i style="width:${Math.max(2, Math.min(100, into * 100)).toFixed(1)}%"></i></div>
        <div class="ob-sub">${money(b.to_next)} more to reach ${money(b.next_at, 0)} and make it ${money(b.bonus + b.pay, 0)}</div>
        ${b.milestones ? `<div class="ob-badges">${Array.from({ length: Math.min(b.milestones, 40) }, (_, i) => `<span>${money((i + 1) * b.per / 1000, 0)}k ✓</span>`).join('')}</div>` : ''}
      </div>`;
  }
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  async function refresh() {
    let b;
    try { const r = await fetch('/api/order-bonus', { headers: { 'X-Requested-With': 'ledger' } }); if (!r.ok) return; b = (await r.json()).bonus; } catch { return; }
    if (!b || !b.self) { if (st.el) st.el.hidden = true; return; }   // Nick, Nov 4 2026: only the person earning it sees the bar
    if (!st.el) {
      const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s);
      st.el = document.createElement('div'); st.el.className = 'ob';
      const nav = document.querySelector('nav.tabs'); if (!nav) return;
      nav.insertAdjacentElement('afterend', st.el);
    }
    st.el.hidden = false;
    const before = seen(b);
    draw(b); st.data = b;
    if (b.milestones > before) { celebrate(b); }
    mark(b);
  }

  function init() {
    refresh();
    setInterval(() => { if (!document.hidden) refresh(); }, 45000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    try {
      const es = new EventSource('/api/events');
      let t = null;
      es.onmessage = (ev) => { try { const m = JSON.parse(ev.data); if (!m.t || m.t === 'order' || m.t === 'bonus') { clearTimeout(t); t = setTimeout(refresh, 1200); } } catch { /* ignore */ } };
    } catch { /* ignore */ }
  }
  return { init, refresh, celebrate: () => st.data && celebrate(st.data) };
})();
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => OrderBonus.init()); else OrderBonus.init();
