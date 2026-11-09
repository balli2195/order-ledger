#!/usr/bin/env python3
"""Recompute every catalog pair's roi_now / roi_avg90 / watch_price from the values already stored in the Hub
(no Sephora / Keepa / SP-API calls). Use after a change to the economics rules in common.py (e.g. the prep fee).

  python3 -I recompute_roi.py [--dry-run]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY")
    rows, changed = C.hub_catalog(), []
    for r in rows:
        lo, hi = C.fnum(r.get("price_lo")), C.fnum(r.get("price_hi"))
        sale = C.fnum(r.get("sale_price")) if r.get("on_sale") else None
        amz = {k: C.fnum(r.get(k)) for k in ("buybox", "fba_low", "bb_avg90", "referral_pct", "fba_fee")}
        e = C.economics(r, lo, hi, sale, amz, "catalog")
        if not e:
            continue
        ex = dict(r.get("extra") or {})
        ex.update(prep=C.PREP, cost=e["cost"], cost_basis=e["label"])
        if r.get("needs_size") and r.get("size_guess") == "unknown" and not r.get("size_confirmed"):
            el = C.economics({**r, "size_guess": "lo"}, lo, hi, sale, amz, "catalog")
            if el:
                ex["roi_now_lo"], ex["watch_price_lo"] = el["roi_now"], el["watch_price"]
        changed.append({"asin": r["asin"], "product_id": r["product_id"], "active": r["active"], "roi_now": e["roi_now"], "roi_avg90": e["roi_avg90"],
                        "watch_price": e["watch_price"], "extra": ex})
    print(f"{len(rows)} pairs, {len(changed)} recomputed at prep ${C.PREP:.2f}")
    if not a.dry_run and changed:
        print(C.hub_upsert_catalog(changed))


if __name__ == "__main__":
    main()
