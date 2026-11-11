/* Wrong match dialog (Nov 11, 2026), shared by the Sephora leads tab and the Sephora relist page.
   WrongMatch.ask({title, asin, sephora}) -> Promise<{tags, note, correct_asin} | null>
   The tag keys must match app/sephora.py WRONG_TAGS; the Sephora jobs turn repeated tags into match rules. */
(function () {
  const TAGS = [
    ['size', 'Wrong size', 'mini vs full size, oz / ml differ'],
    ['shade', 'Wrong shade / color', ''],
    ['concentration', 'Wrong scent or type', 'EDT vs EDP, Intense, L\'Eau, hair mist vs perfume…'],
    ['set', 'Set vs single', 'one side is a set, kit or duo'],
    ['bonus', 'Bundle / bonus item', 'Amazon listing adds a bonus or extra item'],
    ['multipack', 'Pack count differs', '2-pack, 3-pack…'],
    ['version', 'Old version / reformulated', 'discontinued, new packaging or formula'],
    ['product', 'Different product', 'different line or product altogether'],
    ['other', 'Other', 'say what in the note'],
  ];
  const css = `
  .wm-back { position: fixed; inset: 0; background: rgba(0,0,0,.35); z-index: 1000; display: flex; align-items: center; justify-content: center; padding: 16px; }
  .wm-box { background: var(--panel, #fff); color: var(--ink, #111); border: 1px solid var(--line, #ddd); border-radius: 12px; width: 100%; max-width: 480px;
            box-shadow: 0 12px 40px rgba(0,0,0,.25); padding: 16px 18px; max-height: calc(100vh - 32px); overflow: auto; }
  .wm-box h3 { margin: 0 0 2px; font-size: 16px; }
  .wm-sub { margin: 0 0 12px; font-size: 12.5px; color: var(--muted, #666); }
  .wm-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
  .wm-tags button { border: 1px solid var(--line, #ccc); background: transparent; color: var(--ink, #111); border-radius: 99px; padding: 4px 11px;
                    font-size: 13px; cursor: pointer; }
  .wm-tags button.on { background: var(--bad, #c0392b); border-color: var(--bad, #c0392b); color: #fff; }
  .wm-box label { display: block; font-size: 12px; font-weight: 600; color: var(--muted, #666); margin: 8px 0 4px; }
  .wm-box textarea, .wm-box input { width: 100%; box-sizing: border-box; font: inherit; font-size: 13.5px; padding: 7px 9px; }
  .wm-box textarea { min-height: 64px; resize: vertical; }
  .wm-hint { font-size: 12px; color: var(--muted, #666); margin-top: 3px; min-height: 15px; }
  .wm-acts { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
  .wm-err { color: var(--bad, #c0392b); font-size: 12.5px; margin-top: 6px; min-height: 16px; }`;
  function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function ask(opt = {}) {
    if (!document.getElementById('wm-style')) {
      const st = document.createElement('style'); st.id = 'wm-style'; st.textContent = css; document.head.appendChild(st);
    }
    return new Promise((resolve) => {
      const back = document.createElement('div');
      back.className = 'wm-back';
      back.innerHTML = `<div class="wm-box" role="dialog" aria-modal="true" aria-label="Wrong match">
        <h3>${esc(opt.title || 'What\'s wrong with this match?')}</h3>
        <p class="wm-sub">${opt.sephora ? esc(opt.sephora) + ' ↔ ' : ''}${esc(opt.asin || '')}. It won't be suggested again, and the matcher learns from the reason.</p>
        <div class="wm-tags">${TAGS.map(([k, l, h]) => `<button type="button" data-k="${k}" title="${esc(h)}">${esc(l)}</button>`).join('')}</div>
        <div class="wm-hint" id="wmHint">Pick one or more.</div>
        <label for="wmNote">What should it have looked for? (optional)</label>
        <textarea id="wmNote" maxlength="1000" placeholder="e.g. Amazon listing is the 1.7 oz, Sephora item is the 3.4 oz"></textarea>
        <label for="wmAsin">Correct ASIN, if you know it (optional)</label>
        <input id="wmAsin" maxlength="10" placeholder="B0…" autocomplete="off">
        <div class="wm-err" id="wmErr"></div>
        <div class="wm-acts"><button type="button" class="btn" data-x="cancel">Cancel</button>
          <button type="button" class="btn danger" data-x="save">Mark wrong match</button></div></div>`;
      document.body.appendChild(back);
      const box = back.querySelector('.wm-box');
      const sel = new Set();
      const hint = back.querySelector('#wmHint');
      const close = (v) => { back.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
      const save = () => {
        const note = back.querySelector('#wmNote').value.trim();
        const ca = back.querySelector('#wmAsin').value.trim().toUpperCase();
        if (!sel.size && !note) { back.querySelector('#wmErr').textContent = 'Pick a reason or write a note.'; return; }
        if (sel.has('other') && !note) { back.querySelector('#wmErr').textContent = 'Say what was wrong in the note.'; return; }
        if (ca && !/^[A-Z0-9]{10}$/.test(ca)) { back.querySelector('#wmErr').textContent = 'An ASIN is 10 letters/digits.'; return; }
        close({ tags: [...sel], note, correct_asin: ca || null });
      };
      const onKey = (e) => { if (e.key === 'Escape') close(null); if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save(); };
      document.addEventListener('keydown', onKey);
      back.addEventListener('click', (e) => {
        if (e.target === back) return close(null);
        const t = e.target.closest('.wm-tags button');
        if (t) {
          const k = t.dataset.k;
          if (sel.has(k)) sel.delete(k); else sel.add(k);
          t.classList.toggle('on', sel.has(k));
          const h = TAGS.find((x) => x[0] === k);
          hint.textContent = sel.has(k) && h && h[2] ? h[2] : (sel.size ? '' : 'Pick one or more.');
          back.querySelector('#wmErr').textContent = '';
          return;
        }
        const x = e.target.closest('[data-x]');
        if (x) return x.dataset.x === 'save' ? save() : close(null);
      });
      box.querySelector('.wm-tags button').focus();
    });
  }
  const label = (k) => (TAGS.find((x) => x[0] === k) || [k, k])[1];
  window.WrongMatch = { ask, TAGS, label, text: (tags, note) => [(tags || []).map(label).join(', '), note].filter(Boolean).join(' — ') };
})();
