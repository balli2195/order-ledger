// Myrddin Hub — Macy's gift card balance check (Nov 10 2026)
// Paste into the browser console on https://www.macys.com/account/giftcardbalance (your own signed-in Chrome).
// It asks the Hub for cards to check (oldest card first), fills Macy's own form one card at a time,
// reads the balance Macy's shows, and saves it to the Gift cards page.
// Safety:
//  * Macy's locks the form after 3 rejected tries, so the script STOPS at the first card Macy's rejects.
//  * It never solves or works around a CAPTCHA. If one appears, or Macy's stops answering, it pauses and waits
//    for you: solve it in this tab yourself, then press Resume.
//  * One card every 90–120 seconds. If Macy's doesn't answer, it waits 3–5 minutes and tries that card once more,
//    then pauses for you if there's still no answer.
(async () => {
  const HUB = '__HUB__', KEY = '__KEY__';
  const PACE = [90, 120];           // seconds between cards (at least 1.5 minutes)
  const RETRY = [180, 300];         // seconds before the one automatic retry when Macy's doesn't answer
  const RESULT_WAIT = 20;           // seconds to wait for Macy's answer
  const FETCH = 25;                 // cards fetched from the Hub per batch
  if (window.__mgc && window.__mgc.running) { alert('The balance check is already running in this tab.'); return; }
  const S = window.__mgc = { running: true, paused: false, stop: false, done: 0, used: 0, left: 0, sum: 0, log: [], last: null };

  // ---------- panel
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;left:16px;bottom:16px;z-index:2147483647;width:330px;background:#111;color:#fff;font:13px/1.4 system-ui,sans-serif;border-radius:10px;padding:12px 14px;box-shadow:0 6px 24px rgba(0,0,0,.35)';
  box.innerHTML = `<div style="font-weight:600;margin-bottom:6px">Myrddin · Macy's balance check</div>
    <div id="mgc-st">Starting…</div><div id="mgc-ct" style="opacity:.8;margin-top:4px"></div>
    <div style="margin-top:8px;display:flex;gap:6px"><button id="mgc-pause">Pause</button><button id="mgc-stop">Stop</button></div>`;
  box.querySelectorAll('button').forEach(b => b.style.cssText = 'background:#fff;color:#111;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit');
  document.body.appendChild(box);
  const st = (t, warn) => { const e = box.querySelector('#mgc-st'); e.textContent = t; e.style.color = warn ? '#ffb4a8' : '#fff'; };
  const ct = () => box.querySelector('#mgc-ct').textContent =
    `${S.done} checked · ${S.used} empty · ${S.left} with money ($${S.sum.toFixed(2)})`;
  box.querySelector('#mgc-pause').onclick = (e) => { S.paused = !S.paused; e.target.textContent = S.paused ? 'Resume' : 'Pause'; if (!S.paused) st('Resuming…'); };
  box.querySelector('#mgc-stop').onclick = () => { S.stop = true; st('Stopping after this card…'); };
  const pauseFor = async (msg) => {
    S.paused = true; box.querySelector('#mgc-pause').textContent = 'Resume'; st(msg, true);
    while (S.paused && !S.stop) await sleep(1000);
  };

  // ---------- helpers
  // Chrome slows a hidden or covered tab's timers to about one tick a minute, so waits are timed by a background
  // worker (whose timers Chrome doesn't slow) and checked against the real clock.
  let W = null, wseq = 0; const wake = new Map();
  try {
    W = new Worker(URL.createObjectURL(new Blob(['onmessage=e=>setTimeout(()=>postMessage(e.data.id),e.data.ms)'], { type: 'text/javascript' })));
    W.onmessage = (e) => { const f = wake.get(e.data); if (f) { wake.delete(e.data); f(); } };
  } catch (e) { W = null; }
  const sleep = (ms) => new Promise(r => { if (!W) return setTimeout(r, ms); const id = ++wseq; wake.set(id, r); W.postMessage({ id, ms }); });
  const mmss = (sec) => `${Math.floor(sec / 60)}:${String(Math.max(0, Math.round(sec % 60))).padStart(2, '0')}`;
  // Count down to a real-clock deadline (pausing pushes the deadline back); label(t) gives the panel text.
  const countdown = async (secs, label, warn) => {
    let end = Date.now() + secs * 1000;
    while (!S.stop && Date.now() < end) {
      if (S.paused) { const p0 = Date.now(); while (S.paused && !S.stop) await sleep(1000); end += Date.now() - p0; continue; }
      st(label((end - Date.now()) / 1000), warn); await sleep(1000);
    }
  };
  // Holding a Web Lock tells Chrome not to freeze this tab while the check runs.
  let unlock = null;
  try { navigator.locks && navigator.locks.request('myrddin-macys-balance-check', () => new Promise(r => { unlock = r; })); } catch (e) {}
  const hub = (path, opt = {}) => fetch(HUB + path, { ...opt, headers: { 'Content-Type': 'application/json', 'X-GC-Key': KEY, ...(opt.headers || {}) } })
    .then(async r => { if (!r.ok) throw new Error(`Hub ${r.status}: ${await r.text()}`); return r.json(); });
  const save = (row) => hub('/api/engine/giftcards/check-results', { method: 'POST', body: JSON.stringify({ rows: [row], who: "Macy's balance check" }) });
  const setv = (el, v) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, v);
    ['input', 'change', 'blur'].forEach(t => el.dispatchEvent(new Event(t, { bubbles: true })));
  };
  const $num = () => document.getElementById('gift-card-number'), $cid = () => document.getElementById('gift-card-cid-number');
  const $btn = () => document.getElementById('form-submit');
  const area = () => { const b = $btn() || $num(); return (b && (b.closest('section') || b.closest('form')?.parentElement)) || document.querySelector('main') || document.body; };
  const visibleChallenge = () => [...document.querySelectorAll('iframe')].some(f => /recaptcha\/(enterprise|api2)\/bframe/.test(f.src) && f.getBoundingClientRect().height > 100 && getComputedStyle(f).visibility !== 'hidden');

  // Macy's answer can come back as JSON; keep the last gift-card response the page receives.
  const ofetch = window.fetch;
  window.fetch = async function (...a) {
    const res = await ofetch.apply(this, a);
    try { const u = String(a[0]?.url || a[0]); if (/gift|balance/i.test(u) && !u.startsWith(HUB)) res.clone().text().then(t => S.last = { u, s: res.status, t }); } catch (e) {}
    return res;
  };
  const oopen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u, ...r) {
    if (/gift|balance/i.test(u)) this.addEventListener('load', () => { S.last = { u, s: this.status, t: this.responseText }; });
    return oopen.call(this, m, u, ...r);
  };

  // Read Macy's answer: a balance, a rejection, a lockout, a challenge, or nothing yet.
  // Only text that is new since the click counts (an old message can linger on the page), and nothing is
  // read until Macy's has answered this card's request or the page text has changed.
  const readAnswer = (before) => {
    const full = area().innerText, old = new Set(before.split('\n'));
    const fresh = full.split('\n').filter(l => !old.has(l)).join('\n');
    if (!S.last && !fresh) return visibleChallenge() ? { k: 'challenge' } : null;
    // Macy's own answer (/account-xapi/api/giftcardbalance):
    // {"giftCard":{"cardBalance":"$0.00",...},"captchaError":false,"cardError":false}
    if (S.last && S.last.t && /giftcardbalance/i.test(S.last.u)) {
      try {
        const j = JSON.parse(S.last.t);
        if (j.captchaError) return { k: 'challenge', api: true };
        if (j.cardError) return { k: 'rejected', msg: (fresh.match(/[^\n]*(invalid|not valid|try again)[^\n]*/i) || [])[0] || "Macy's says the card number or CID is invalid" };
        const cb = j.giftCard && j.giftCard.cardBalance;
        if (cb != null && /\d/.test(String(cb))) return { k: 'balance', v: Number(String(cb).replace(/[^\d.]/g, '')) };
      } catch (e) {}
    }
    if (/locked out|too many attempts|temporarily (locked|unavailable)/i.test(fresh) && !/chances? left/i.test(fresh))
      return { k: 'locked', msg: fresh.match(/[^\n]*(locked|too many|temporarily)[^\n]*/i)?.[0] };
    if (/invalid|not valid|could not be found|try again/i.test(fresh))
      return { k: 'rejected', msg: fresh.match(/[^\n]*(invalid|not valid|could not be found|try again)[^\n]*/i)?.[0] };
    const m = fresh.match(/balance[^$]{0,80}\$\s?([\d,]+\.\d{2})/i) || fresh.match(/\$\s?([\d,]+\.\d{2})/);
    if (m) return { k: 'balance', v: Number(m[1].replace(/,/g, '')) };
    if (visibleChallenge()) return { k: 'challenge' };
    return null;
  };

  const backToForm = async () => {
    if ($num() && $cid()) return true;
    const again = [...document.querySelectorAll('button, a')].find(b => /check (another|a different)|new card|check balance|start over/i.test(b.textContent));
    if (again) { again.click(); for (let i = 0; i < 20 && !$num(); i++) await sleep(250); }
    return !!($num() && $cid());
  };

  // ---------- main loop
  try {
    while (!S.stop) {
      st('Getting cards from Myrddin Hub…');
      const { cards } = await hub(`/api/engine/giftcards/to-check?store=macys&limit=${FETCH}`);
      if (!cards.length) { st('All done — no Macy\'s cards left to check.'); break; }
      for (const c of cards) {
        while (S.paused && !S.stop) await sleep(1000);
        if (S.stop) break;
        if (!(await backToForm())) { await pauseFor("Can't find Macy's balance form. Open the Gift Card Balance page in this tab, then Resume."); if (S.stop) break; }
        st(`Checking card ending ${c.n.slice(-4)} (face $${Number(c.face).toFixed(2)})…`);
        S.last = null;
        setv($num(), c.n); setv($cid(), c.pin);
        await sleep(400);
        const before = area().innerText;
        $btn().click();
        let ans = null;
        for (const end = Date.now() + RESULT_WAIT * 1000; Date.now() < end && !ans;) { await sleep(250); ans = readAnswer(before); }
        if (!ans && !c._retried) {
          // No answer: wait 3–5 minutes, then try this card once more.
          c._retried = true; setv($num(), ''); setv($cid(), '');
          const back = RETRY[0] + Math.random() * (RETRY[1] - RETRY[0]);
          await countdown(back, (t) => `No answer from Macy's for card ending ${c.n.slice(-4)}. Trying it again in ${mmss(t)}…`, true);
          if (S.stop) break;
          cards.splice(cards.indexOf(c) + 1, 0, c); continue;
        }
        if (!ans) {
          await pauseFor(`Still no answer from Macy's for card ending ${c.n.slice(-4)} after a retry. If there's a challenge, solve it, then press Resume (the card is tried again).`);
          if (S.stop) break;
          c._retried = false; cards.splice(cards.indexOf(c) + 1, 0, c); continue;
        }
        if (ans.k === 'challenge') {
          await pauseFor(ans.api ? 'Macy\'s turned down this try at its reCAPTCHA check. Wait a few minutes (or solve any challenge it shows you in this tab), then press Resume to retry the same card.'
                                 : 'Macy\'s is showing a verification challenge. Solve it yourself in this tab, then press Resume.');
          if (S.stop) break;
          cards.splice(cards.indexOf(c) + 1, 0, c); continue;
        }
        if (ans.k === 'locked') { st(`Macy's locked the balance form: "${ans.msg || ''}". Stopped. Try again later.`, true); S.stop = true; break; }
        if (ans.k === 'rejected') {
          await save({ id: c.id, error: ans.msg || 'Macy\'s rejected the card number or CID' });
          S.log.push({ id: c.id, last4: c.n.slice(-4), rejected: ans.msg });
          st(`Macy's rejected card ending ${c.n.slice(-4)} (marked "invalid" on the Hub). Stopped so the form doesn't lock — check that card, then run the script again.`, true);
          S.stop = true; break;
        }
        await save({ id: c.id, balance: ans.v });
        S.done++; if (ans.v > 0) { S.left++; S.sum += ans.v; } else S.used++;
        S.log.push({ id: c.id, last4: c.n.slice(-4), balance: ans.v }); ct();
        setv($num(), ''); setv($cid(), '');
        const wait = PACE[0] + Math.random() * (PACE[1] - PACE[0]);
        const lastAt = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
        await countdown(wait, (t) => `Next card in ${mmss(t)}… (last check ${lastAt})`);
      }
    }
    if (S.stop && !/Stopped|locked|rejected/.test(box.querySelector('#mgc-st').textContent)) st('Stopped.');
  } catch (e) {
    st('Error: ' + e.message, true); console.error(e);
  } finally {
    S.running = false; window.fetch = ofetch; XMLHttpRequest.prototype.open = oopen; ct();
    try { W && W.terminate(); } catch (e) {} try { unlock && unlock(); } catch (e) {}
  }
})();
