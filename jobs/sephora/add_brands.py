#!/usr/bin/env python3
"""Screen and add Sephora brands by Nick's rule (Nov 22, 2026): match every brand he can sell.

    python3 -I add_brands.py [--eligible] [--rescreen] [--brands "Dieux,Kosas"] [--dry-run]

--eligible  every brand of the Nov 2, 2026 sellability screen (Hub doc brand_sellers/latest: open / mostly open) that has
            no pairs in the Lead DB yet
--rescreen  brands the old weekly screen left out for a reason other than "restricted" (demand, no current sellers,
            the retired can-source switch)
Each brand gets weekly_new_products.screen_brand (restrictions re-checked today on its top 5 Amazon listings; ~10 Keepa
tokens): open -> added to job state 'brands_added' (built by build_backlog.py / the weekly run); restricted -> screened
out; approval required -> listed for Nick. Writes only the Hub job docs sephora_jobs/brands_added and brands_screened.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import weekly_new_products as W  # noqa: E402

norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eligible", action="store_true")
    ap.add_argument("--rescreen", action="store_true")
    ap.add_argument("--brands")
    ap.add_argument("--aliases", help="JSON file {brand: [Amazon brand spellings]} to try as well")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    day = C.today()
    cfg = C.load_brands()
    excl = {norm(b) for b in cfg.get("exclude_brands", [])}
    have = {norm(n) for b in cfg["brands"] for n in [b["name"]] + (b.get("sephora_names") or [])}
    built = {norm(r.get("brand")) for r in C.hub_catalog()}
    added = C.state_get("brands_added", {}) or {}
    screened = C.state_get("brands_screened", {}) or {}
    todo = []
    if a.eligible:
        d = C.hub("GET", "/api/engine/docs/brand_sellers/latest")
        rows = (d.get("data") or d)["rows"]
        todo += [r["brand"] for r in sorted(rows, key=lambda r: r.get("rank") or 999) if norm(r["brand"]) not in built and norm(r["brand"]) not in have]
    if a.rescreen:
        todo += [bn for bn, v in screened.items() if v.get("status") != "auto_added" and "restricted" not in str(v.get("reason"))
                 and "approval required" not in str(v.get("reason"))]
    if a.brands:
        todo += [x.strip() for x in a.brands.split(",") if x.strip()]
    import json
    aliases = json.load(open(a.aliases)) if a.aliases else {}
    todo = [bn for bn in dict.fromkeys(todo) if bn not in added]
    print(len(todo), "brands to screen", flush=True)
    out = {"auto_added": [], "for_nick": [], "screened_out": []}
    for i, bn in enumerate(todo):
        try:
            ex = aliases.get(bn) or []
            stt, why, det = W.screen_brand(bn, excl, day, ex)
            entry = None
            if stt == "auto_added":
                stt, why, entry = W.brand_entry(bn, why, det, day, ex)
        except Exception as e:  # noqa: BLE001
            print(f"  {bn}: failed {e!r}"[:200], flush=True)
            continue
        out[stt].append((bn, why))
        screened[bn] = {"status": stt, "reason": why, "date": day.isoformat(), **det}
        if entry:
            added[bn] = entry
        print(f"  [{i + 1}/{len(todo)}] {bn}: {stt} - {why}", flush=True)
        if not a.dry_run and (i + 1) % 10 == 0:
            C.state_put("brands_screened", screened)
            C.state_put("brands_added", added)
    if not a.dry_run:
        C.state_put("brands_screened", screened)
        C.state_put("brands_added", added)
    print({k: len(v) for k, v in out.items()})
    for k in ("for_nick", "screened_out"):
        for bn, why in out[k]:
            print(f"{k}: {bn} - {why}")


if __name__ == "__main__":
    C.need_env("ENGINE_KEY", "KEEPA_KEY", "SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")
    main()
