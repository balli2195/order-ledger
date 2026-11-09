#!/usr/bin/env python3
"""Per-brand summary of the built catalog files -> prints a table and writes summary.json."""
import json, os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CAT = os.environ.get("SEPH_DATA") or os.path.join(os.path.dirname(HERE), ".data")   # data lives outside git (see ../.gitignore)


def main():
    cfg = json.load(open(os.path.join(os.path.dirname(HERE), "brands.json")))
    rows = []
    for b in cfg["brands"]:
        p = os.path.join(CAT, b["slug"] + ".json")
        if not os.path.exists(p):
            rows.append({"brand": b["name"], "missing": True})
            continue
        d = json.load(open(p))
        ms = d["matches"]
        conf = Counter(m["confidence"] for m in ms)
        sell = Counter(m["sellable"] for m in ms)
        ok = [m for m in ms if m["sellable"] == "open" and m["roi_now"] is not None]
        rows.append({
            "brand": b["name"], "sephora_products": len(d["sephora_products"]),
            "asins_found": d["keepa_meta"].get("fetched"), "asins_finder_total": d["keepa_meta"].get("finder_total"),
            "matched": len(ms), "high": conf["high"], "medium": conf["medium"], "low": conf["low"],
            "unmatched_asins": len(d["unmatched_asins"]), "unmatched_products": len(d["unmatched_products"]),
            "needs_size": sum(1 for m in ms if m["needs_size"]),
            "open": sell["open"], "approval": sell["approval_required"], "restricted": sell["restricted"],
            "unchecked": sell["unchecked"] + sell["error"],
            "roi15": sum(1 for m in ok if m["roi_now"] >= 0.15), "roi21": sum(1 for m in ok if m["roi_now"] >= 0.21),
            "roi21_hm": sum(1 for m in ok if m["roi_now"] >= 0.21 and m["confidence"] in ("high", "medium")),
            "roi21_solid": sum(1 for m in ok if m["roi_now"] >= 0.21 and m["confidence"] in ("high", "medium")
                               and m.get("demand_ok") and not m.get("thin_price")),
            "keepa_tokens": d["keepa_meta"].get("tokens_used"),
        })
    json.dump(rows, open(os.path.join(CAT, "summary.json"), "w"), indent=1)
    hdr = ["brand", "sephora_products", "asins_found", "matched", "high", "medium", "low", "needs_size", "open", "approval",
           "restricted", "roi15", "roi21", "roi21_hm", "roi21_solid", "keepa_tokens"]
    print("\t".join(hdr))
    for r in rows:
        print("\t".join(str(r.get(h, "")) for h in hdr))
    print("TOTAL keepa tokens:", sum(r.get("keepa_tokens") or 0 for r in rows))


if __name__ == "__main__":
    main()
