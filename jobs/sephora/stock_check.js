// Sephora stock check (Nick, Nov 17, 2026). Paste into a sephora.com tab in Nick's Chrome with javascript_tool, then call
//   await sephStock([{id, url}, ...])            // at most 3 per call (each page is ~8 s apart; tool calls time out at 45 s)
// Returns [{id, skus: [{sku_id, name, in_stock}]} | {id, error}] to POST to the Hub as {results: [...]} at
// /api/engine/sephora/stock-watch/results (from the Claude session, not from this page). Reads only; changes nothing.
window.sephStock = async (items, gapMs = 8000) => {
  const out = [];
  const parse = (html) => {
    const m = html.match(/<script[^>]*id="linkStore"[^>]*>([\s\S]*?)<\/script>/);
    if (!m) return null;
    const p = JSON.parse(m[1]).page.product;
    const seen = {};
    return [p.currentSku, ...(p.regularChildSkus || [])].filter(Boolean).filter((s) => !seen[s.skuId] && (seen[s.skuId] = 1))
      .map((s) => ({ sku_id: String(s.skuId), name: [s.variationValue, s.size].filter(Boolean).join(' · ') || null, in_stock: s.isOutOfStock === undefined ? null : !s.isOutOfStock }));
  };
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    try {
      const u = new URL(it.url, location.origin);
      if (u.origin !== location.origin) throw new Error('not a sephora.com URL');
      const r = await fetch(u.pathname + u.search, { credentials: 'include' });
      if (r.status !== 200) throw new Error('HTTP ' + r.status);
      const skus = parse(await r.text());
      out.push(skus ? { id: it.id, skus } : { id: it.id, error: 'no product data on the page (blocked or changed?)' });
    } catch (e) {
      out.push({ id: it.id, error: String(e.message || e) });
    }
    if (i < items.length - 1) await new Promise((res) => setTimeout(res, gapMs));
  }
  return out;
};
'sephStock ready';
