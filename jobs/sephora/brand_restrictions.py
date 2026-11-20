#!/usr/bin/env python3
"""Monthly Amazon restriction check for the Lead DB (Nick, Nov 20, 2026).

Nick: "i don't want extra brand restriction data in my hub that is conflicting with my restriction check from earlier.
the once a month sephora run should update this data". So the only source of which brands he can sell is Amazon's
Listing Restrictions check on his seller account (the method of the Nov 2, 2026 brand eligibility run), refreshed here
once a month. The old Airtable "can source" brand switches were removed from the Hub (migration 69).

1. Every active Lead DB pair (GET /api/engine/sephora/catalog?active=true); each distinct ASIN is checked live with
   GET /listings/2021-08-01/restrictions (catalog/sp_restrictions.py, <= 4 requests a second).
2. Pairs whose result changed get the new `sellable` (open / approval_required / restricted) written back with
   POST /api/engine/sephora/catalog (only asin, product_id, sellable and their current active value are sent).
   The daily job only watches `sellable = open` rows, so a listing Nick gets approved for starts being watched, and
   one that becomes restricted stops.
3. Per-brand summary -> Hub doc sephora_jobs/brand_restrictions:
   {checked_at, asins_checked, errors, brands: {name: {listings, open, approval_required, restricted, error, status}}}
   status: restricted (>= 90% of its checked listings restricted, at least 3), mostly_restricted (>= 50%),
   approval (>= 50% need approval), open (otherwise), unknown (fewer than 3 checked).
   common.load_brands() turns the 'restricted' brands into the jobs' brand skip list, in place of the old switches.
4. .data/brand_restrictions_summary.md for Nick's message: counts, brands whose status changed since the last run,
   and listings that opened up or became restricted.

If more than 20% of the checks fail (token or API trouble), nothing is written and the run exits 2.
Usage: ENGINE_KEY=.. SPAPI_CLIENT_ID=.. SPAPI_CLIENT_SECRET=.. SPAPI_REFRESH_TOKEN=.. python3 -I brand_restrictions.py [--dry-run] [--limit N]
"""
import argparse, collections, datetime as dt, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "catalog")]
import common as C  # noqa: E402

MIN_LISTINGS = 3


def brand_status(c: dict) -> str:
    n = c["open"] + c["approval_required"] + c["restricted"]
    if n < MIN_LISTINGS:
        return "unknown"
    if c["restricted"] >= 0.9 * n:
        return "restricted"
    if c["restricted"] >= 0.5 * n:
        return "mostly_restricted"
    if c["approval_required"] >= 0.5 * n:
        return "approval"
    return "open"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="check and summarize, write nothing to the Hub")
    ap.add_argument("--limit", type=int, default=0, help="check only the first N ASINs (testing)")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY", "SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")
    import sp_restrictions as spr
    C.DRY_RUN = a.dry_run
    rows = C.hub_catalog(active="true")
    rows = [r for r in rows if r.get("active")]
    asins = list(dict.fromkeys(r["asin"] for r in rows))
    if a.limit:
        asins = asins[:a.limit]
    C.log(f"{len(rows)} active pairs, {len(asins)} ASINs to check")
    res = {}
    for i, asin in enumerate(asins):
        res[asin] = spr.check(asin)
        if (i + 1) % 250 == 0:
            C.log(f"  {i + 1}/{len(asins)}")
    errors = sum(1 for v in res.values() if v["status"] == "error")
    if asins and errors > 0.2 * len(asins):
        C.log(f"FAILED: {errors}/{len(asins)} checks failed; nothing written")
        sys.exit(2)

    # 2. write back changed pairs
    upd, opened, closed = [], [], []
    for r in rows:
        v = res.get(r["asin"])
        if not v or v["status"] == "error" or v["status"] == r.get("sellable"):
            continue
        upd.append({"asin": r["asin"], "product_id": r["product_id"], "sellable": v["status"], "active": r.get("active")})
        item = (r.get("brand"), r["asin"], (r.get("amazon_title") or r.get("sephora_name") or "")[:70], r.get("sellable"), v["status"])
        (opened if v["status"] == "open" else closed if v["status"] == "restricted" else []).append(item)
    if upd and not a.dry_run:
        C.log("catalog update:", C.hub_upsert_catalog(upd))

    # 3. per-brand summary (one count per ASIN and brand)
    seen, cnt = set(), collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        v = res.get(r["asin"])
        b = (r.get("brand") or "").strip()
        if not v or not b or (b, r["asin"]) in seen:
            continue
        seen.add((b, r["asin"]))
        cnt[b][v["status"]] += 1
    brands = {}
    for b, c in sorted(cnt.items()):
        c = {k: c.get(k, 0) for k in ("open", "approval_required", "restricted", "error")}
        brands[b] = {"listings": sum(c.values()), **c, "status": brand_status(c)}
    prev = (C.state_get("brand_restrictions", {}) or {}).get("brands", {})
    changed = [(b, (prev.get(b) or {}).get("status"), x["status"]) for b, x in brands.items()
               if prev and (prev.get(b) or {}).get("status") != x["status"]]
    doc = {"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "asins_checked": len(asins),
           "errors": errors, "pairs_updated": len(upd), "min_listings": MIN_LISTINGS, "brands": brands,
           "note": "Amazon Listing Restrictions on Nick's seller account (new condition), monthly. 'restricted' brands are "
                   "skipped by the Sephora jobs; per-listing sellable decides what is watched."}
    if not a.dry_run and not a.limit:
        C.state_put("brand_restrictions", doc)

    # 4. summary for the message
    st = collections.Counter(x["status"] for x in brands.values())
    L = [f"# Monthly Amazon restriction check ({C.today()})", "",
         f"- {len(asins)} Lead DB listings checked ({errors} failed); {len(upd)} pairs changed sellable.",
         f"- Brands: {dict(st)}",
         f"- Restricted brands (skipped by the Sephora jobs): {', '.join(b for b, x in brands.items() if x['status'] == 'restricted') or 'none'}"]
    L.append(f"- Brand status changes since last run: " + ("; ".join(f"{b}: {o or 'new'} → {n}" for b, o, n in changed) or "none"))
    for title, items in (("Listings that opened up", opened), ("Listings that became restricted", closed)):
        L.append(f"- {title}: {len(items)}")
        for b, asin, t, o, n in items[:10]:
            L.append(f"  - {b}: {t} (https://www.amazon.com/dp/{asin}) {o} → {n}")
    if a.dry_run:
        L.append("- Dry run: nothing written.")
    C.save_json("brand_restrictions_summary.json", doc)
    open(os.path.join(C.DATA, "brand_restrictions_summary.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
