"""Build the verify queue: catalog pairs whose evidence conflicts (MATCHING_PRINCIPLES.md §3).

    python3 -I verify_queue.py [--limit 40] [--dry-run]

The matcher's scripts can't search the web, so they don't settle conflicts by score. They list them here, and a
research pass (RUNBOOK section 10, verify_prompt.md) settles each one with outside evidence and a source.
Only pairs nobody has decided are queued (match_ok is null), active and not a dead listing, and not already verified.

Conflict kinds:
  sibling       the title names all of a sibling product's distinguishing words
  concentration the title's EDT / EDP / parfum differs from the Sephora product's
  form / type   format or product type differs but the pair still matched
  line          the Sephora name's line word is missing from the title
  close_call    another product scored almost as high (margin < 0.06) or the match is low confidence
  shade         the shade is a guess, a close shade, or two shades are named
  size          multi-size product with no store size and no size guess
Output: Hub doc sephora_jobs/verify_queue {built, cases: [...]} and .data/verify_queue.json. Most valuable first:
pairs near profit, then the rest by kind (each kind represented, so the research also teaches the matcher).
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

KINDS = [
    ("sibling", r"names a sibling product"),
    ("concentration", r"concentration differs|scent concentratio"),
    ("form", r"form differs|brow format differs"),
    ("type", r"type differs"),
    ("line", r"line word '[^']+' missing"),
    ("mini", r"Sephora page is a mini|mini mismatch|mini vs full"),
    ("set", r"set mismatch|set vs single"),
]


def margin(reason: str):
    m = re.search(r"margin (-?\d+\.\d+)", reason or "")
    return float(m.group(1)) if m else None


def kinds_of(r: dict) -> list:
    why = r.get("match_reason") or ""
    out = [k for k, rx in KINDS if re.search(rx, why)]
    if "mini" in out and r.get("store_size"):
        out.remove("mini")   # the size is settled on the pair
    mg = margin(why)
    if r.get("confidence") == "low" and mg is not None and mg < 0.06:
        out.append("close_call")   # a low match with another product almost as close
    sh = (r.get("extra") or {}).get("shade_hint") or {}
    if not r.get("store_size") and (sh.get("guess") or sh.get("two_shades")):
        out.append("shade")
    if r.get("needs_size") and not r.get("store_size") and r.get("size_guess") in (None, "unknown"):
        out.append("size")
    return out


def question(r: dict, ks: list) -> str:
    t, n = r.get("amazon_title") or "", r.get("sephora_name") or ""
    q = []
    if {"sibling", "line", "close_call", "type", "form", "set", "mini"} & set(ks):
        q.append(f"Is the Amazon listing '{t[:120]}' the same product as Sephora's '{n}' (brand, line, scent/variant name, "
                 f"format, single vs set), or another product, or something Sephora doesn't sell?")
    if "concentration" in ks:
        q.append("Does the product exist in the strength the Amazon title says, or is the title wrong?")
    if "shade" in ks:
        sh = (r.get("extra") or {}).get("shade_hint") or {}
        q.append("Which Sephora shade is the listing? Candidates: " + ", ".join((sh.get("guess") or sh.get("two_shades") or [])[:4]))
    if "size" in ks:
        q.append(f"Which Sephora size is the listing (Sephora ${r.get('price_lo')}–${r.get('price_hi')})?")
    return " ".join(q)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cat = C.hub_catalog(active="true")
    cases = []
    for r in cat:
        ex = r.get("extra") or {}
        if r.get("match_ok") is not None or ex.get("listing_dead") or ex.get("verified"):
            continue
        ks = kinds_of(r)
        if not ks:
            continue
        cases.append({"asin": r["asin"], "product_id": r["product_id"], "brand": r.get("brand"),
                      "sephora_name": r.get("sephora_name"), "sephora_url": r.get("sephora_url"),
                      "amazon_title": r.get("amazon_title"), "amazon_url": f"https://www.amazon.com/dp/{r['asin']}",
                      "store_size": r.get("store_size"), "roi_now": r.get("roi_now"), "confidence": r.get("confidence"),
                      "kinds": ks, "question": question(r, ks), "matcher_reason": (r.get("match_reason") or "")[:300]})
    # most valuable first: near profit (roi >= -10%), then one of each kind in turn, so research covers every kind
    near = sorted([c for c in cases if (C.fnum(c["roi_now"]) or -9) >= -0.10], key=lambda c: -(C.fnum(c["roi_now"]) or -9))
    rest = [c for c in cases if c not in near]
    by_kind = {}
    for c in sorted(rest, key=lambda c: -(C.fnum(c["roi_now"]) or -9)):
        by_kind.setdefault(c["kinds"][0], []).append(c)
    spread = []
    while any(by_kind.values()):
        for k in list(by_kind):
            if by_kind[k]:
                spread.append(by_kind[k].pop(0))
    queue = (near + spread)[:a.limit]
    counts = {}
    for c in cases:
        for k in c["kinds"]:
            counts[k] = counts.get(k, 0) + 1
    doc = {"built": C.today().isoformat(), "conflicts_found": len(cases), "by_kind": counts, "cases": queue}
    os.makedirs(C.DATA, exist_ok=True)
    json.dump(doc, open(os.path.join(C.DATA, "verify_queue.json"), "w"), indent=1)
    if not a.dry_run:
        C.state_put("verify_queue", doc)
    print(f"{len(cases)} pairs with conflicting evidence; queued {len(queue)} ({len(near)} near profit). By kind: {counts}")


if __name__ == "__main__":
    main()
