"""Keep listings with no sales rank and no sales out of the Lead DB (Nick, Nov 24, 2026; his B07NV8X97F example).

  python3 -I norank_sweep.py [--dry-run] [--recheck-tokens 300]

A watched listing is parked (moved out of the Lead DB into sephora_parked_pairs, whole, nothing deleted) when all are true:
  - it has been on Amazon at least MIN_AGE_DAYS (90) days (Keepa listedSince, else trackingSince);
  - it had no sales rank at any point in the last RANK_DAYS (730, Nick: "the last 2 years"), read from the full Keepa
    history (csv[3], a rank still standing when the window opened counts);
  - Keepa shows no monthly-sales estimate (monthlySold now, or any in monthlySoldHistory in the same window).
Never parked: pairs Nick marked OK, and ASINs in his leads (he bought them). Data: the Keepa collector's latest pull
(no tokens). Candidates are the watched ASINs whose latest pull shows no rank and no monthly sold.
Parked ones are re-checked each run with a light Keepa pull (stats only, ~1 token, at most --recheck-tokens): one that
shows a rank or sales again goes back into the Lead DB. Writes .data/norank_summary.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

MIN_AGE_DAYS, RANK_DAYS = 90, 730
REASON = f"No sales rank in {RANK_DAYS // 365} years and no sales estimate (Nick's filter)"
EPOCH = dt.datetime(2011, 1, 1, tzinfo=dt.timezone.utc)


def verdict(p: dict, now: dt.datetime) -> dict:
    cut = (now - dt.timedelta(days=RANK_DAYS) - EPOCH).total_seconds() / 60
    cs = p.get("csv") or []
    rk = (cs[3] if len(cs) > 3 else None) or []
    pts = [(rk[i], rk[i + 1]) for i in range(0, len(rk) - 1, 2)]
    rank = any(v > 0 for t, v in pts if t >= cut)
    before = [v for t, v in pts if t < cut]
    rank = rank or bool(before and before[-1] > 0)
    msh = p.get("monthlySoldHistory") or []
    sold = (p.get("monthlySold") or 0) > 0 or any(msh[i + 1] > 0 for i in range(0, len(msh) - 1, 2) if msh[i] >= cut)
    since = p.get("listedSince") or p.get("trackingSince") or 0
    age = (now - (EPOCH + dt.timedelta(minutes=since))).days if since and since > 0 else None
    return {"age_days": age, "rank": rank, "sold": sold,
            "park": age is not None and age >= MIN_AGE_DAYS and not rank and not sold}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--recheck-tokens", type=int, default=300)
    a = ap.parse_args()
    C.need_env("ENGINE_KEY")
    now = dt.datetime.now(dt.timezone.utc)
    cat = C.hub_catalog()
    watched = sorted({r["asin"] for r in cat if r.get("active") and r.get("sellable") == "open" and r.get("match_ok") is not False})
    nick_ok = {r["asin"] for r in cat if r.get("match_ok") is True}
    leads = set()
    try:
        off = 0
        while True:
            d = C.hub("GET", "/api/engine/leads", params={"limit": 5000, "offset": off})
            rows = d.get("leads") or d.get("rows") or []
            leads |= {str(x.get("asin") or "").strip().upper() for x in rows}
            if len(rows) < 5000:
                break
            off += 5000
    except Exception as e:  # noqa: BLE001
        sys.exit(f"leads unreadable, nothing held: {e!r}")
    # 1. candidates: latest pull shows no rank and no monthly sold (collector, no tokens)
    cand = []
    for i in range(0, len(watched), 50):
        got = C.hub("POST", "/api/engine/keepa/latest", {"asins": watched[i:i + 50], "max_age_hours": 24 * 9}, timeout=300).get("products") or {}
        for k, p in got.items():
            st = p.get("stats") or {}
            cur = st.get("current") or []
            rank_now = cur[3] if len(cur) > 3 else -1
            if (rank_now is None or rank_now <= 0) and not (p.get("monthlySold") or 0) > 0:
                cand.append(k)
    # 2. full history for the candidates
    park, keep_nick, stay = [], [], []
    for asin in cand:
        r = C.hub("GET", "/api/engine/keepa/raw", params={"asin": asin, "max_age_hours": 24 * 9})
        if not r.get("found"):
            continue
        v = verdict(r["product"], now)
        if not v["park"]:
            stay.append((asin, v))
        elif asin in nick_ok or asin in leads:
            keep_nick.append((asin, v))
        else:
            park.append((asin, v, (r["product"].get("title") or "")[:70]))
    parked_now = 0
    if park and not a.dry_run:
        keys = [{"asin": r["asin"], "product_id": r["product_id"]} for r in cat if r["asin"] in {p_[0] for p_ in park}]
        out = C.hub("POST", "/api/engine/sephora/catalog/park", {"keys": keys, "reason": REASON}, timeout=300)
        parked_now = out["parked"]
        C.save_json(f"out/{now.date().isoformat()}/norank_parked.json", out["pairs"])
    # 3. re-check parked ones (light Keepa pull): back when a rank or sales shows up
    restored, rechecked = [], 0
    try:
        pk = []
        off = 0
        while True:
            d = C.hub("GET", "/api/engine/sephora/parked", params={"offset": off, "limit": 2000})
            pk += [x for x in d["rows"] if str(x.get("reason") or "").startswith("No sales rank")]
            off += len(d["rows"])
            if not d["rows"] or off >= d["total"]:
                break
        asins = sorted({x["asin"] for x in pk})[: max(0, a.recheck_tokens)]
        if asins and os.environ.get("KEEPA_KEY"):
            got, _tok, _stop = C.keepa_products(asins, a.recheck_tokens)
            rechecked = len(got)
            for k, v in got.items():
                if (v.get("salesRank") or 0) > 0 or (v.get("monthlySold") or 0) > 0:
                    restored.append(k)
            if restored and not a.dry_run:
                C.hub("POST", "/api/engine/sephora/unpark", {"items": [{"asin": x, "sellable": "open"} for x in restored],
                                                             "reason_prefix": "No sales rank"})
    except Exception as e:  # noqa: BLE001
        C.log(f"re-check of held listings failed: {e!r}")
    L = [f"# No-rank, no-sales filter, {now.date().isoformat()}{' (DRY RUN)' if a.dry_run else ''}", "",
         f"- Watched listings: {len(watched)}; no rank and no sales in their latest pull: {len(cand)}",
         f"- Held now (on Amazon {MIN_AGE_DAYS}+ days, no rank in {RANK_DAYS // 365} years, no sales): {len(park)} listings, {parked_now} pairs",
         f"- Kept because Nick marked them OK or bought them: {len(keep_nick)}; kept (young, or a rank / sales in the window): {len(stay)}",
         f"- Held listings re-checked: {rechecked}; back in the Lead DB (rank or sales again): {len(restored)}", ""]
    if park:
        L += ["| ASIN | Days on Amazon | Title |", "|---|---|---|"] + [f"| {x} | {v['age_days']} | {t} |" for x, v, t in park[:60]]
    open(os.path.join(C.DATA, "norank_summary.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
