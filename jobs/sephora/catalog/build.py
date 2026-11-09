#!/usr/bin/env python3
"""Combine Sephora + Keepa matches + Listing Restrictions into the catalog files (no network).

Usage: python3 -I build.py [slug ...]
Reads raw/match/<slug>.json (match.py) and raw/sp/restrictions.json (sp_restrictions.py).
Writes <catalog>/<slug>.json and <catalog>/all_matches.csv (all brands that have a built file).

Economics per matched ASIN (Nick's rules):
  cost      = Sephora price x 1.05 x pack_qty   (sale_price when on sale, else the size-basis price)
  revenue   = current Buy Box, else lowest FBA offer
  profit    = revenue - revenue x referral% - FBA pick&pack fee - $1.40 prep - cost
  roi       = profit / cost              (roi_now; roi_avg90 uses the 90-day average Buy Box)
  watch_price = Buy Box at which ROI = 21% at full (non-sale) Sephora price
Unknown size on a multi-size product: both lo and hi are computed; roi_now / watch_price use the hi (conservative) cost.
"""
import csv, json, os, re, sys, datetime
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CAT = os.environ.get("SEPH_DATA") or os.path.join(os.path.dirname(HERE), ".data")   # data lives outside git (see ../.gitignore)
sys.path.insert(0, HERE)
from match import size_ml, flags, base_text  # noqa: E402

UPLIFT, PREP, TARGET = 1.05, 1.40, 0.21   # $1.40 prep (Nick, Nov 2026)
SAMPLE_RE = re.compile(r"\b(vial|sample|tester|decant|carded)\b")


def referral(pct, price):
    if pct is not None and pct > 0:
        return pct / 100, "keepa"
    return (0.08 if (price or 0) <= 10 else 0.15), "default_beauty"


def econ(cost, rev, ref, fee):
    if not cost or not rev or fee is None:
        return None
    profit = rev - rev * ref - fee - PREP - cost
    return round(profit / cost, 4)


def watch(cost_full, ref, fee):
    if not cost_full or fee is None:
        return None
    return round((cost_full * (1 + TARGET) + fee + PREP) / (1 - ref), 2)


def size_guesses(matches):
    """Per Sephora product with a price range: rank the distinct Amazon sizes seen among its matched ASINs."""
    by_pid = defaultdict(list)
    for m in matches:
        k = m["keepa"]
        sz = size_ml(" ".join(x for x in [k.get("size") or "", k.get("title") or ""] if x))
        m["_size_ml"] = sz
        by_pid[m["productId"]].append(m)
    out = {}
    for pid, ms in by_pid.items():
        all_sizes = sorted({m["_size_ml"] for m in ms if m["_size_ml"]})
        big = all_sizes[-1] if all_sizes else None
        for m in ms:
            t = base_text((m["keepa"].get("title") or "") + " " + (m["keepa"].get("size") or ""))
            f = flags(t)
            sz = m["_size_ml"]
            g, why = "unknown", "no size signal"
            if f["mini"]:
                g, why = "lo", "Amazon title says mini/travel"
            elif re.search(r"\b(full size|full-size|standard size|jumbo|large|value size|supersize)\b", t):
                g, why = "hi", "Amazon title says full/large size"
            elif sz and len(all_sizes) >= 2 and sz >= big * 0.7:
                g, why = "hi", f"largest of {len(all_sizes)} Amazon sizes for this product ({sz} ml/g)"
            elif sz:
                g, why = "unknown", f"Amazon size {sz} ml/g; Sephora's size list is not in the search data"
            out[m["asin"]] = (g, why)
    return out


def build(slug, cfg_b, restr, excluded, live):
    r = json.load(open(os.path.join(CAT, "raw", "match", slug + ".json")))
    seph = r["sephora"]
    prods = {p["productId"]: p for p in seph["products"]}
    sg = size_guesses(r["matches"])
    rows = []
    for m in r["matches"]:
        k = m["keepa"]
        if (k.get("brand") or "").strip().lower() in excluded:
            continue
        p = prods[m["productId"]]
        lo, hi = p.get("price_lo"), p.get("price_hi")
        needs_size = bool(lo and hi and hi > lo)
        guess, guess_why = sg.get(m["asin"], ("unknown", ""))
        if not needs_size:
            guess, guess_why = "single", "single Sephora price"
        pq = m.get("pack_qty") or 1
        conf, reason = m["confidence"], m["reason"]
        t = base_text(k.get("title") or "")
        if SAMPLE_RE.search(t):
            conf = "low"
            reason += "; Amazon title looks like a sample/vial/tester (not the Sephora item)"
        lv = live.get(k["asin"]) or {}
        bb_now = k.get("buybox") or lv.get("buybox")
        fba_low = lv.get("fba_low") if lv else k.get("fba_low")
        offers_fba = lv.get("offers_fba") if lv else k.get("offers_fba")
        rev = bb_now or fba_low
        rev_src = ("buybox" if k.get("buybox") else "buybox_sp") if bb_now else ("fba_low" if fba_low else None)
        ref, ref_src = referral(k.get("referral_pct"), rev)
        fee = k.get("fba_fee")
        fee_src = "keepa" if fee is not None else None
        if fee is None and m.get("_fam_fee") is not None:
            fee, fee_src = m["_fam_fee"], "sibling"
        # price basis
        if guess == "lo":
            full = lo
        elif guess in ("hi", "single"):
            full = hi
        else:
            full = hi
        sale = p.get("sale_price") if p.get("on_sale") else None
        sale_note = None
        if sale and needs_size and guess not in ("lo",) and sale < (lo or 0):
            # Sephora's salePrice belongs to the listing's default SKU (usually the small size); don't apply it to other sizes
            sale_note = f"sale ${sale:.2f} applies to Sephora's default (likely smallest) size only"
            sale = None
        price_used = sale if sale else full
        cost = round(price_used * UPLIFT * pq, 2) if price_used else None
        cost_full = round(full * UPLIFT * pq, 2) if full else None
        bb90 = k.get("bb_avg90") or k.get("fba_low_avg90")
        row = {
            "asin": k["asin"], "productId": m["productId"], "skuId": m["skuId"], "sephora_name": p["name"],
            "sephora_url": p["url"], "confidence": conf, "score": m["score"], "reason": reason,
            "needs_size": needs_size, "size_guess": guess, "size_guess_reason": guess_why,
            "amazon_size_ml": m.get("_size_ml"), "pack_qty": pq,
            "price_lo": lo, "price_hi": hi, "sale_price": sale, "on_sale": bool(sale), "sale_note": sale_note,
            "sellable": (restr.get(k["asin"]) or {}).get("status", "unchecked"),
            "restriction_reasons": "; ".join(f"{x.get('code')}: {x.get('message')}" for x in (restr.get(k["asin"]) or {}).get("reasons", []))[:300],
            "title": k.get("title"), "shade": k.get("color") or (k.get("variation_attrs") or {}).get("Color"),
            "amazon_size": k.get("size"), "parentAsin": k.get("parentAsin"),
            "buybox": bb_now, "buybox_is_fba": k.get("buybox_is_fba"), "fba_low": fba_low, "fbm_low": lv.get("fbm_low"),
            "bb_avg90": bb90, "referral_pct": round(ref * 100, 2), "referral_src": ref_src,
            "fba_fee": fee, "fba_fee_src": fee_src, "monthly_sold": k.get("monthlySold"), "sales_rank": k.get("salesRank"),
            "offers_fba": offers_fba, "offers_fbm": lv.get("offers_fbm") if lv else k.get("offers_fbm"),
            "offers_new": k.get("offers_new"), "amazon_on": bool(k.get("amazon_on") or lv.get("amazon_offer")),
            "live_checked_at": lv.get("checked_at"),
            "revenue": rev, "revenue_src": rev_src,
            "cost_basis": (f"sale ${sale:.2f}" if sale else
                           (f"{guess} ${full:.2f}" if full else "no price")) + (f" x{pq}" if pq > 1 else "") + " x1.05",
            "cost": cost, "cost_full": cost_full,
            "roi_now": econ(cost, rev, ref, fee), "roi_avg90": econ(cost, bb90, ref, fee),
            "watch_price": watch(cost_full, ref, fee),
            "demand_ok": bool((k.get("monthlySold") or 0) >= 10 or (k.get("salesRank") and k["salesRank"] <= 150000)),
            "thin_price": bool(rev_src == "fba_low" and (offers_fba or 0) <= 1) or (rev_src and rev_src.startswith("buybox") and (offers_fba or 0) == 0 and not k.get("monthlySold")),
        }
        if needs_size and guess == "unknown":
            c_lo = round(((sale or lo) * UPLIFT * pq), 2)
            row.update(cost_lo=c_lo, cost_hi=cost, roi_now_lo=econ(c_lo, rev, ref, fee), roi_now_hi=row["roi_now"],
                       watch_price_lo=watch(round(lo * UPLIFT * pq, 2), ref, fee))
        rows.append(row)
    rows.sort(key=lambda x: (x["productId"], -(x["monthly_sold"] or 0)))
    out = {"brand": cfg_b["name"], "slug": slug,
           "pulled_at": {"sephora": seph["pulled_at"], "keepa": r["keepa_meta"].get("pulled_at"),
                         "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")},
           "sephora_queries": seph.get("queries"), "keepa_meta": r["keepa_meta"],
           "sephora_products": seph["products"], "matches": rows,
           "unmatched_asins": r["unmatched_asins"], "unmatched_products": r["unmatched_products"]}
    json.dump(out, open(os.path.join(CAT, slug + ".json"), "w"), indent=1)
    return out


def fill_family_fees(slug):
    """FBA fee fallback: a variation child without a Keepa fee takes its siblings' median fee (same size)."""
    path = os.path.join(CAT, "raw", "match", slug + ".json")
    r = json.load(open(path))
    allk = json.load(open(os.path.join(CAT, "raw", "keepa", slug + ".json")))["asins"]
    fam = defaultdict(list)
    for k in allk:
        if k.get("parentAsin") and k.get("fba_fee") is not None:
            fam[k["parentAsin"]].append(k["fba_fee"])
    for m in r["matches"]:
        k = m["keepa"]
        if k.get("fba_fee") is None and fam.get(k.get("parentAsin")):
            v = sorted(fam[k["parentAsin"]])
            m["_fam_fee"] = v[len(v) // 2]
    return r


def main():
    cfg = json.load(open(os.path.join(os.path.dirname(HERE), "brands.json")))
    excluded = {b.lower() for b in cfg["exclude_brands"]}
    rp = os.path.join(CAT, "raw", "sp", "restrictions.json")
    restr = json.load(open(rp)) if os.path.exists(rp) else {}
    op = os.path.join(CAT, "raw", "sp", "offers.json")
    live = json.load(open(op)) if os.path.exists(op) else {}
    fees_path = os.path.join(CAT, "raw", "sp", "fees.json")
    sp_fees = json.load(open(fees_path)) if os.path.exists(fees_path) else {}
    want = set(sys.argv[1:])
    for b in cfg["brands"]:
        if want and b["slug"] not in want:
            continue
        if not os.path.exists(os.path.join(CAT, "raw", "match", b["slug"] + ".json")):
            continue
        r = fill_family_fees(b["slug"])
        for m in r["matches"]:
            f = sp_fees.get(m["asin"])
            if m["keepa"].get("fba_fee") is None and m.get("_fam_fee") is None and f and f.get("fba_fee") is not None:
                m["_fam_fee"] = f["fba_fee"]
        json.dump(r, open(os.path.join(CAT, "raw", "match", b["slug"] + ".json"), "w"), indent=1)
        out = build(b["slug"], b, restr, excluded, live)
        print(f"{b['name']}: {len(out['matches'])} rows", flush=True)
    # combined CSV
    cols = ["brand", "asin", "productId", "skuId", "sephora_name", "confidence", "score", "needs_size", "size_guess", "pack_qty",
            "sellable", "title", "shade", "amazon_size", "price_lo", "price_hi", "sale_price", "cost_basis", "cost",
            "buybox", "fba_low", "fbm_low", "bb_avg90", "revenue_src", "referral_pct", "fba_fee", "fba_fee_src", "monthly_sold",
            "sales_rank", "demand_ok", "thin_price", "offers_fba", "offers_fbm", "amazon_on", "roi_now", "roi_now_lo", "roi_avg90", "watch_price", "watch_price_lo",
            "sephora_url", "reason", "restriction_reasons"]
    with open(os.path.join(CAT, "all_matches.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for b in cfg["brands"]:
            p = os.path.join(CAT, b["slug"] + ".json")
            if os.path.exists(p):
                for row in json.load(open(p))["matches"]:
                    w.writerow({"brand": b["name"], **row})


if __name__ == "__main__":
    main()
