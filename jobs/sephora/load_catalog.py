#!/usr/bin/env python3
"""One-off / disaster-recovery load of a built catalog (catalog/build.py output) into the Hub.

Usage: ENGINE_KEY=... python3 -I load_catalog.py --catalog-dir DIR [--dry-run]
DIR holds all_matches.csv plus the per-brand <slug>.json files (for the product-level Sephora sale price).
Upserts every row (<= 1000 per call; Nick's match_ok / size_confirmed are never sent), applies the known fixes below,
then checks the Hub's total with GET.
"""
import argparse, csv, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

# Known mismatches found in review (Nov 9, 2026): deactivate.
DEACTIVATE = {
    "B0FRYM47FD": "Amazon 'Sephora Favorites 25 Days of Beauty' calendar is not the SEPHORA COLLECTION Advent Calendar",
    "B0GX72Z6V4": "Amazon 'Vacay All Day 9-piece' set is not the All Day Setting Spray Duo Set",
}
# Pairs to downgrade to low confidence (never watched) with a reason.
DOWNGRADE = {
    "B0BL42D18M": ("Amazon listing is the 2022 holiday 'On 'Til Dawn Setting Spray Duo' (Buy Box ~$84, likely two full-size "
                   "bottles); Sephora P526221 is the $36 Big & Bitty duo (3.4 oz + 1 oz), matched correctly by B0FTWJ66SW / "
                   "B0HM13P9NK. Not the same item: low confidence until Nick checks."),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY")
    cfg = C.load_brands()
    prods = {}
    for b in cfg["brands"]:
        p = os.path.join(a.catalog_dir, b["slug"] + ".json")
        if os.path.exists(p):
            for sp in json.load(open(p))["sephora_products"]:
                prods[sp["productId"]] = sp
    rows = []
    for r in csv.DictReader(open(os.path.join(a.catalog_dir, "all_matches.csv"))):
        h = C.hub_row(r, r["brand"], prods.get(r["productId"]))
        if r["asin"] in DOWNGRADE:
            h["confidence"] = "low"
            h["match_reason"] = (h["match_reason"] or "") + "; REVIEW: " + DOWNGRADE[r["asin"]]
        rows.append(h)
    print(f"{len(rows)} rows, {len({(x['asin'], x['product_id']) for x in rows})} pairs, {len({x['asin'] for x in rows})} ASINs")
    if a.dry_run:
        C.save_json("out/load_catalog_rows.json", rows)
        print("dry run: rows saved to", os.path.join(C.DATA, "out/load_catalog_rows.json"))
        return
    print("upsert:", C.hub_upsert_catalog(rows))
    keys = [{"asin": x["asin"], "product_id": x["product_id"]} for x in rows if x["asin"] in DEACTIVATE]
    if keys:
        print("deactivate:", keys, C.hub("POST", "/api/engine/sephora/catalog/deactivate", {"keys": keys}))
    allr = C.hub_catalog()
    act = [x for x in allr if x["active"]]
    print(f"Hub now: {len(allr)} pairs ({len(act)} active); file had {len(rows)}")
    want = {(x["asin"], x["product_id"]) for x in rows}
    have = {(x["asin"], x["product_id"]) for x in allr}
    print("missing in Hub:", len(want - have), "extra in Hub:", len(have - want))


if __name__ == "__main__":
    main()
