"""Match Sephora Sale-page items that aren't in the Lead DB yet, right away (Nick, Nov 24, 2026: for Black Friday,
"half off select gifts" from Nov 26 6 pm PT, mostly new holiday sets, match them the same night instead of waiting for
Sunday's weekly build).

  python3 -I sale_new_products.py [--max-tokens 2000] [--max-products 60] [--dry-run]

Reads job state `new_products` (the daily run's Sale-page items not in the catalog), keeps those of brands in the
configured brand list (not excluded or restricted), deepest discount first, and for each one searches Keepa for Amazon
listings by brand + name words, then runs the usual matcher / restrictions / offers / build for that brand
(weekly_new_products.build_brand). New pairs are upserted to the Lead DB; they still go through the match check gate
before reaching To review (run daily_watch.py afterwards, and the check queue). Products searched leave `new_products`
the same way the weekly run does. Writes .data/sale_new_summary.md.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import weekly_new_products as W  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=2000)
    ap.add_argument("--max-products", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY", "KEEPA_KEY", "SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")
    import keepa_pull as kp
    import match as M
    t0, day = time.time(), C.today()
    C.DRY_RUN = a.dry_run
    cfg = C.load_brands()
    excluded = {b.lower() for b in cfg.get("exclude_brands", [])}
    problems = []
    cat = C.hub_catalog()
    have_pid = {r["product_id"] for r in cat}
    hub_asins = {r["asin"] for r in cat}
    np_file = C.state_get("new_products", []) or []
    cand = []
    for x in np_file:
        b = W.config_brand(x.get("brand"), cfg)
        if not b or x["productId"] in have_pid or (x.get("brand") or "").lower() in excluded or b["name"].lower() in excluded:
            continue
        lo, sale = C.fnum(x.get("price_lo")) or 0, C.fnum(x.get("sale_price"))
        off = (1 - sale / lo) if sale and lo else 0
        cand.append((off, x, b))
    cand.sort(key=lambda t: -t[0])
    C.log(f"{len(np_file)} sale items not in the Lead DB; {len(cand)} of configured, sellable brands")
    budget, used, searched = a.max_tokens, 0, []
    by_brand = collections.defaultdict(list)
    for off, x, b in cand:
        if len(searched) >= a.max_products or budget < 50:
            break
        drop = set()
        for n in [b["name"]] + b.get("sephora_names", []) + b.get("keepa_brands", []) + b.get("strip", []):
            drop |= set(M.base_text(n).split())
        words = [w for w in M.tokens(x.get("name") or "", drop) if w not in M.GENERIC][:3] or M.tokens(x.get("name") or "", drop)[:3]
        if not words:
            continue
        sel = {"brand": b["keepa_brands"], "title": " ".join(words), "current_SALES_gte": 1,
               "sort": [["current_SALES", "asc"]], "perPage": 50, "page": 0}
        u0 = kp.state["used"]
        try:
            d = kp.call("query", {"domain": 1, "selection": json.dumps(sel)}, need=15)
            asins = [q for q in (d.get("asinList") or []) if q not in hub_asins][:10]
            if asins:
                d2 = kp.call("product", {"domain": 1, "asin": ",".join(asins), "stats": 90, "buybox": 1, "history": 0},
                             need=len(asins) * 3 + 5)
                by_brand[b["slug"]] += [kp.compact(p) for p in d2.get("products") or []]
        except Exception as e:  # noqa: BLE001
            problems.append(f"Keepa search for {x.get('name')}: {e!r}"[:200])
        spent = kp.state["used"] - u0
        budget -= spent
        used += spent
        searched.append(x["productId"])
    new_rows = []
    for slug, recs in by_brand.items():
        b = next(q for q in cfg["brands"] if q["slug"] == slug)
        try:
            if not C.sephora_brand(b)["ok"]:   # the brand's current Sephora listing, for the matcher
                problems.append(f"Sephora listing failed for {b['name']}")
                continue
            uniq = {r_["asin"]: r_ for r_ in recs}
            json.dump({"brand": b["name"], "pulled_at": C.now_iso() if hasattr(C, "now_iso") else day.isoformat(),
                       "finder_total": len(uniq), "finder_root_beauty": False, "fetched": len(uniq), "truncated": False,
                       "tokens_used": 0, "asins": list(uniq.values())},
                      open(os.path.join(C.DATA, "raw", "keepa", slug + ".json"), "w"), indent=1)
            rows = [r_ for r_ in W.build_brand(slug, b, excluded) if r_["asin"] not in hub_asins]
            new_rows += rows
            hub_asins |= {r_["asin"] for r_ in rows}
        except Exception as e:  # noqa: BLE001
            problems.append(f"matching new {b['name']} products failed: {e!r}"[:300])
    C.save_json(f"out/{day.isoformat()}/sale_new_rows.json", new_rows)
    if new_rows and not a.dry_run:
        try:
            C.log("new pairs upsert:", C.hub_upsert_catalog(new_rows))
        except Exception as e:  # noqa: BLE001
            problems.append(f"new pairs upsert failed: {e!r}"[:300])
    if not a.dry_run:
        done = set(searched) | {r_["product_id"] for r_ in new_rows}
        left = [x for x in np_file if x["productId"] not in done]
        C.state_put("new_products", left)
        C.save_json("new_products.json", left)
    on_sale = [r_ for r_ in new_rows if r_["product_id"] in {x["productId"] for _, x, _ in cand}]
    L = [f"# Sale items matched to Amazon, {day.isoformat()}{' (DRY RUN)' if a.dry_run else ''}", "",
         f"- Sale items not in the Lead DB: {len(np_file)}; of configured brands: {len(cand)}; searched on Keepa: {len(searched)}",
         f"- New pairs added: {len(new_rows)} ({len(on_sale)} for the sale items themselves); Keepa tokens: {used} (budget {a.max_tokens})",
         f"- Runtime {round((time.time() - t0) / 60, 1)} min", ""]
    if new_rows:
        L += ["| ASIN | Brand | Sephora product | Amazon title | Conf | ROI now |", "|---|---|---|---|---|---|"]
        for r_ in sorted(new_rows, key=lambda r_: -(r_.get("roi_now") or -9))[:40]:
            L.append(f"| {r_['asin']} | {r_.get('brand')} | {r_.get('sephora_name')} | {(r_.get('amazon_title') or '')[:60]} | "
                     f"{r_.get('confidence')} | {C.pct(r_.get('roi_now'))} |")
        L.append("")
    L += ["## Problems", ""] + [f"- {p}" for p in problems] if problems else ["No problems."]
    open(os.path.join(C.DATA, "sale_new_summary.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))
    return 0 if not problems or new_rows else 2


if __name__ == "__main__":
    sys.exit(main())
