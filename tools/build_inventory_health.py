"""Build static/inventory-health-frame.html from a saved copy of the old claude.ai Inventory Hub page.

Usage: python3 tools/build_inventory_health.py <hub.html>
Takes the Inventory health tab (section#liquidation) with its styles and the scripts that draw it, drops the old
liquidation chat block, and adds a small shim so the scripts' `window.claude.use("db")` reads Myrddin Hub's
/api/docs instead (read-only, live-updated through /api/events).
"""
import re
import sys
from pathlib import Path

src = Path(sys.argv[1]).read_text()
head_end = src.index('<header class="topbar">')
styles = "\n".join(m.group(0) for m in re.finditer(r"<style[^>]*>.*?</style>", src[:head_end], re.S))
fonts = "\n".join(m.group(0) for m in re.finditer(r'<link rel="(?:preconnect|stylesheet)"[^>]*>', src[:head_end]))

i = src.index('<section class="tab" id="liquidation"')
j = src.index("</section>", i) + len("</section>")
sec = src[i:j].replace('<section class="tab" id="liquidation" hidden>', '<section class="tab" id="liquidation">', 1)


def drop_div(html: str, marker: str) -> str:
    k = html.find(marker)
    if k < 0:
        return html
    depth, p = 0, k
    for m in re.finditer(r"<div\b|</div>", html[k:]):
        depth += 1 if m.group(0) == "<div" else -1
        if depth == 0:
            return html[:k] + html[k + m.end():]
    raise SystemExit("unbalanced div for " + marker)


sec = drop_div(sec, '<div class="lqws" id="lqWs"')   # the liquidation chat (removed by Nick, Oct 29 2026)

scripts = [m.group(0) for m in re.finditer(r"<script[^>]*>.*?</script>", src, re.S)]
want = []
for s in scripts:
    body = s
    if "lqWs" in body or "lqMore" in body or "liq_chats" in body:
        continue   # chat workspace scripts
    uses = any(x in body for x in ('"liqTable"', "liquidation/latest", "liquidation_trend/history", "doi/latest",
                                    "health_trend/history", 'getElementById("liqTable")'))
    other = any(x in body for x in ("replenish", "inbound_cases", "ibpTable", "rpTable", "importBtn"))
    if uses and not other:
        want.append(s)

SHIM = r"""<script>
/* Myrddin Hub: stand-in for the claude.ai page database. Reads /api/docs (read-only) and refreshes on /api/events. */
(() => {
  const H = { 'X-Requested-With': 'ledger' };
  const subs = new Map(), colSubs = new Map(); let es = null;
  const add = (m, k, f) => { if (!m.has(k)) m.set(k, new Set()); m.get(k).add(f); return () => m.get(k).delete(f); };
  function listen() {
    if (es) return; es = new EventSource('/api/events');
    es.onmessage = (ev) => { try {
      const m = JSON.parse(ev.data);
      if (m.t === 'doc' && m.path) { (subs.get(m.path) || []).forEach((f) => f()); (colSubs.get(m.path.split('/')[0]) || []).forEach((f) => f()); }
      else if (m.op === 'reload' && !m.t) { subs.forEach((s) => s.forEach((f) => f())); colSubs.forEach((s) => s.forEach((f) => f())); }
    } catch (e) {} };
  }
  const snap = (path, d) => ({ id: path.split('/').pop(), exists: !!(d && d.data), data: () => (d ? d.data : undefined) });
  async function fetchDoc(path) { const r = await fetch('/api/docs/' + path, { headers: H }); if (r.status === 401) { top.location.href = '/login'; } if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }
  const ro = async () => { throw new Error('This page is read-only'); };
  function doc(path) {
    return {
      get: async () => snap(path, await fetchDoc(path)),
      onSnapshot(cb, err) { const run = () => fetchDoc(path).then((d) => cb(snap(path, d)), (e) => err && err(e)); run(); listen(); return add(subs, path, run); },
      set: ro, update: ro, delete: ro,
    };
  }
  function collection(name) {
    return {
      doc: (id) => doc(name + '/' + id),
      onSnapshot(cb, err) {
        const run = () => fetch('/api/docs?collection=' + encodeURIComponent(name), { headers: H }).then((r) => r.json())
          .then((j) => cb({ docs: (j.docs || []).map((d) => ({ id: d.id, exists: true, data: () => d.data })) }), (e) => err && err(e));
        run(); listen(); return add(colSubs, name, run);
      },
    };
  }
  window.claude = { use: async (cap) => (cap === 'db' ? { doc, collection } : null) };
})();
</script>"""

out = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inventory health · Myrddin Hub</title><base target="_blank">
{fonts}
{styles}
<style>main{{padding-top:18px}}</style>
</head><body>
<main class="wide">
{sec}
</main>
{SHIM}
{chr(10).join(want)}
</body></html>
"""
Path("static/inventory-health-frame.html").write_text(out)
print("scripts kept", len(want), "of", len(scripts), "| bytes", len(out))
