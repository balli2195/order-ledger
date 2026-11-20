"""Apply the verify pass's results (verify_prompt.md) to the Lead DB.

    python3 -I verify_apply.py [--results .data/verify_results.json] [--dry-run]

Only high-confidence results with at least one source are applied; the rest go to Nick (Hub doc sephora_jobs/verify_for_nick).
Pairs Nick has decided (match_ok set) are never touched. Writes are marks by "Claude" with the evidence and sources in the
note, so Nick can see why. Every change is logged with the pair's state before it (.data/verify_undo.json), and each
result becomes a test label for the matcher (Hub doc sephora_jobs/verify_labels).
  same       extra.verified = {date, sources, evidence}; the store size when sku_id is given
  other      the pair is marked wrong (tags product); the right pair is added and its size set, when Sephora sells it
  not_sold   the pair is marked wrong (tags shade or size, from the case's kinds), with the evidence
  cant_tell  left as is, listed for Nick
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def seph_product(pid: str):
    """The Sephora product from this session's brand listings (.data/raw/sephora/*.json, refreshed weekly)."""
    d = os.path.join(C.DATA, "raw", "sephora")
    for f in (os.listdir(d) if os.path.isdir(d) else []):
        for p in json.load(open(os.path.join(d, f))).get("products", []):
            if p.get("productId") == pid:
                return p
    return None


def label(x: dict) -> str:
    v, z = (x.get("value") or "").strip(), (x.get("size") or "").strip()
    return v if v == z or not z else " · ".join(p for p in (v, z) if p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(C.DATA, "verify_results.json"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    res = json.load(open(a.results))
    queue = {(c["asin"], c["product_id"]): c for c in (C.state_get("verify_queue", {}) or {}).get("cases", [])}
    skus = {r["product_id"]: r for r in C.hub("GET", "/api/engine/store-skus")["rows"]}
    undo, for_nick, labels, done = [], [], [], {"same": 0, "other": 0, "not_sold": 0, "for Nick": 0}
    today = C.today().isoformat()

    def mark(body):
        print("  mark", body["asin"], body["product_id"], {k: v for k, v in body.items() if k not in ("asin", "product_id", "note")})
        return None if a.dry_run else C.hub("POST", "/api/engine/sephora/catalog/mark", body)

    for r in res:
        rows = C.hub("GET", "/api/engine/sephora/catalog", params={"asin": r["asin"]})["rows"]
        pair = next((x for x in rows if x["product_id"] == r["product_id"]), None)
        labels.append({**r, "checked": today})
        if not pair or pair.get("match_ok") is not None:
            continue
        ok = r.get("confidence") == "high" and r.get("sources")
        if not ok or r["verdict"] == "cant_tell":
            for_nick.append(r); done["for Nick"] += 1
            continue
        undo.append({"asin": r["asin"], "before": rows, "result": r})
        note = f"Verified {today}: {r.get('evidence', '')} ({r.get('principle', '')}) Sources: " + " ".join(r["sources"][:3])
        if r["verdict"] == "same":
            ex = {**(pair.get("extra") or {}), "verified": {"date": today, "sources": r["sources"][:3], "evidence": r.get("evidence")}}
            if not a.dry_run:
                C.hub_upsert_catalog([{"asin": r["asin"], "product_id": r["product_id"], "active": pair["active"], "extra": ex}])
            x = next((s for s in (skus.get(r["product_id"]) or {}).get("skus", []) if s["sku_id"] == r.get("sku_id")), None)
            if x:
                mark({"asin": r["asin"], "product_id": r["product_id"], "store_size": {"sku_id": x["sku_id"], "size": label(x), "price": x.get("price")}})
            done["same"] += 1
        elif r["verdict"] in ("other", "not_sold"):
            kinds = (queue.get((r["asin"], r["product_id"])) or {}).get("kinds", [])
            tags = ["product"] if r["verdict"] == "other" else (["shade"] if "shade" in kinds else ["size"] if "size" in kinds or "mini" in kinds else ["product"])
            mark({"asin": r["asin"], "product_id": r["product_id"], "match_ok": False, "tags": tags, "note": note[:1000]})
            new = r.get("correct_product_id")
            if r["verdict"] == "other" and new:
                src = next((x for x in rows if x["product_id"] == new), None)
                if not src:
                    p = seph_product(new)
                    if not p:
                        print("  ", new, "not in this session's Sephora listing data; add the pair after the weekly refresh")
                    if p and not a.dry_run:
                        C.hub_upsert_catalog([{**{k: pair.get(k) for k in ("asin", "brand", "amazon_title", "buybox", "fba_low", "bb_avg90",
                                                                             "fba_fee", "referral_pct", "monthly_sold", "offers_fba", "amazon_on", "sellable")},
                                               "product_id": new, "sephora_name": p["name"], "sephora_url": p.get("url"),
                                               "price_lo": p.get("price_lo"), "price_hi": p.get("price_hi"), "confidence": "high", "active": True,
                                               "match_reason": note[:2000],
                                               "extra": {"pack_qty": 1, "verified": {"date": today, "sources": r["sources"][:3], "evidence": r.get("evidence")}}}])
                x = next((s for s in (skus.get(new) or {}).get("skus", []) if s["sku_id"] == r.get("sku_id")), None)
                if x:
                    mark({"asin": r["asin"], "product_id": new, "store_size": {"sku_id": x["sku_id"], "size": label(x), "price": x.get("price")}})
            done[r["verdict"]] += 1
    os.makedirs(C.DATA, exist_ok=True)
    if not a.dry_run:
        old = json.load(open(os.path.join(C.DATA, "verify_undo.json"))) if os.path.exists(os.path.join(C.DATA, "verify_undo.json")) else []
        json.dump(old + undo, open(os.path.join(C.DATA, "verify_undo.json"), "w"), indent=0)
        prev = (C.state_get("verify_labels", {}) or {}).get("items", [])
        C.state_put("verify_labels", {"items": prev + labels})
        C.state_put("verify_for_nick", {"built": today, "items": for_nick})
    print(f"applied: {done}")


if __name__ == "__main__":
    main()
