#!/usr/bin/env python3
"""Pull a brand's Amazon ASINs and current economics from Keepa (read-only).

Usage: KEEPA_KEY=... python3 -I keepa_pull.py [--max-asins N] [slug ...]
1. /query (product finder): brand = keepa_brands variants, rootCategory 3760911 (Beauty & Personal Care),
   at least one new offer, sorted by current sales rank. Falls back to no rootCategory when < 20 results.
2. /product?stats=90&buybox=1&history=0 in batches of 100 for the top --max-asins (default 420) ASINs by rank
   (about 3 tokens per ASIN, so <= ~1,300 tokens per brand plus the finder).
Waits for token refill instead of erroring. Writes raw/keepa/<slug>.json with a compact record per ASIN.
"""
import json, os, sys, time, datetime, urllib.parse, urllib.request, gzip

HERE = os.path.dirname(os.path.abspath(__file__))
CAT = os.environ.get("SEPH_DATA") or os.path.join(os.path.dirname(HERE), ".data")   # data lives outside git (see ../.gitignore)
OUT = os.path.join(CAT, "raw", "keepa")
KEY = os.environ.get("KEEPA_KEY", "")
USAGE_LOG = os.path.join(OUT, "_token_usage.jsonl")
state = {"left": None, "refill_rate": 125, "used": 0}
RESERVE = int(os.environ.get("KEEPA_RESERVE") or 0)   # tokens always left on the shared key (the jobs set 1500)


def call(path, params, need=0):
    """GET api.keepa.com/<path>; waits until roughly `need` tokens are available."""
    params = dict(params, key=KEY)
    need = need + RESERVE   # never take the shared key below the reserve: wait for refill instead
    if state["left"] is None and RESERVE:   # unknown balance: ask the free /token endpoint first
        try:
            with urllib.request.urlopen(urllib.request.Request(f"https://api.keepa.com/token?key={KEY}",
                                                               headers={"Accept-Encoding": "gzip"}), timeout=30) as r:
                raw = r.read()
            tk = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
            state["left"], state["refill_rate"] = tk.get("tokensLeft"), tk.get("refillRate") or 125
        except Exception:  # noqa: BLE001
            pass
    url = f"https://api.keepa.com/{path}?" + urllib.parse.urlencode(params)
    for attempt in range(12):
        if state["left"] is not None and need and state["left"] < need:
            wait = (need - state["left"]) / state["refill_rate"] * 60 + 3
            print(f"    waiting {wait:.0f}s for Keepa tokens (left {state['left']}, need {need})", flush=True)
            time.sleep(wait)
            state["left"] = None
        try:
            req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                d = json.loads(raw)
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
            try:
                d = json.loads(body)
            except ValueError:
                d = {}
            if e.code == 429:
                state["left"] = d.get("tokensLeft", 0)
                wait = max(d.get("refillIn", 0) / 1000, 0) + max(need - max(state["left"], 0), 0) / state["refill_rate"] * 60 + 5
                print(f"    429 from Keepa, waiting {wait:.0f}s", flush=True)
                time.sleep(wait)
                state["left"] = None
                continue
            print(f"    Keepa HTTP {e.code}: {str(d)[:200]}", flush=True)
            time.sleep(10)
            continue
        except Exception as e:  # network hiccup
            print(f"    Keepa error {e}", flush=True)
            time.sleep(10)
            continue
        state["left"] = d.get("tokensLeft")
        state["refill_rate"] = d.get("refillRate") or 125
        state["used"] += d.get("tokensConsumed") or 0
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps({"t": time.time(), "path": path, "consumed": d.get("tokensConsumed"), "left": d.get("tokensLeft")}) + "\n")
        return d
    raise RuntimeError("Keepa call failed repeatedly")


def finder(brands, root=True, title=None):
    sel = {"current_COUNT_NEW_gte": 1, "sort": [["current_SALES", "asc"]], "perPage": 2000, "page": 0}
    if title:
        sel["title"] = title
    else:
        sel["brand"] = brands
    if root:
        sel["rootCategory"] = [3760911]
    d = call("query", {"domain": 1, "selection": json.dumps(sel)}, need=20)
    return d.get("asinList") or [], d.get("totalResults")


def cents(v):
    return round(v / 100, 2) if isinstance(v, (int, float)) and v > 0 else None


def compact(p):
    s = p.get("stats") or {}
    cur = s.get("current") or []
    avg90 = s.get("avg90") or []
    g = lambda a, i: a[i] if len(a) > i else -1
    bb = s.get("buyBoxPrice")
    bb = cents(bb) if isinstance(bb, int) and bb > 0 else cents(g(cur, 18))
    fba_cnt, fbm_cnt = g(cur, 34), g(cur, 35)
    n_new = g(cur, 11)
    attrs = {}
    for v in p.get("variations") or []:
        if v.get("asin") == p.get("asin"):
            attrs = {a["dimension"]: a["value"] for a in v.get("attributes") or []}
    fees = p.get("fbaFees") or {}
    return {
        "asin": p.get("asin"),
        "title": p.get("title"),
        "brand": p.get("brand"),
        "parentAsin": p.get("parentAsin"),
        "parentTitle": p.get("parentTitle"),
        "color": p.get("color"),
        "size": p.get("size"),
        "style": p.get("style"),
        "variation_attrs": attrs,
        "itemWeight_g": p.get("itemWeight") if (p.get("itemWeight") or 0) > 0 else None,
        "packageQuantity": p.get("packageQuantity") if (p.get("packageQuantity") or 0) > 0 else None,
        "numberOfItems": p.get("numberOfItems") if (p.get("numberOfItems") or 0) > 0 else None,
        "unitCount": p.get("unitCount"),
        "itemForm": p.get("itemForm"),
        "productType": p.get("productType"),
        "salesRank": g(cur, 3) if g(cur, 3) > 0 else None,
        "monthlySold": p.get("monthlySold") if (p.get("monthlySold") or 0) > 0 else None,
        "buybox": bb,
        "buybox_is_fba": s.get("buyBoxIsFBA"),
        "buybox_is_amazon": s.get("buyBoxIsAmazon"),
        "fba_low": cents(g(cur, 10)),
        "new_low": cents(g(cur, 1)),
        "amazon_price": cents(g(cur, 0)),
        "bb_avg90": cents(g(avg90, 18)),
        "fba_low_avg90": cents(g(avg90, 10)),
        "offers_new": n_new if n_new >= 0 else None,
        "offers_fba": fba_cnt if fba_cnt >= 0 else (0 if n_new >= 0 else None),
        "offers_fbm": fbm_cnt if fbm_cnt >= 0 else (0 if n_new >= 0 else None),
        "amazon_on": bool(g(cur, 0) > 0 or s.get("buyBoxIsAmazon")
                         or (p.get("availabilityAmazon") if p.get("availabilityAmazon") is not None else -1) >= 0),
        "referral_pct": p.get("referralFeePercentage") if p.get("referralFeePercentage") is not None else p.get("referralFeePercent"),
        "fba_fee": cents(fees.get("pickAndPackFee")),
        "eanList": p.get("eanList"), "upcList": p.get("upcList"),
        "listedSince": p.get("listedSince"),
    }


def pull_brand(b, max_asins):
    t0 = state["used"]
    asins, total = finder(b["keepa_brands"], root=True)
    used_root = True
    if len(asins) < 20:
        asins2, total2 = finder(b["keepa_brands"], root=False)
        if len(asins2) > len(asins):
            asins, total, used_root = asins2, total2, False
    title_q = None
    if len(asins) < 20 and b.get("keepa_title"):
        # some brands are indexed by Keepa under an odd brand value; search titles instead and filter on brand text below
        title_q = b["keepa_title"]
        asins3, total3 = finder(None, root=True, title=title_q)
        if len(asins3) > len(asins):
            asins, total, used_root = asins3, total3, True
    print(f"  [{b['name']}] finder: {len(asins)} ASINs with offers (rootCategory={'Beauty' if used_root else 'any'}"
          f"{', title search' if title_q else ''})", flush=True)
    take = asins[:max_asins]
    recs = []
    for i in range(0, len(take), 100):
        batch = take[i:i + 100]
        d = call("product", {"domain": 1, "asin": ",".join(batch), "stats": 90, "buybox": 1, "history": 0}, need=len(batch) * 3 + 5)
        for p in d.get("products") or []:
            c = compact(p)
            if title_q:   # title search: keep only items whose title starts with / brand field names the brand
                tl = (c["title"] or "").lower()
                if title_q.lower() not in tl[:60] and title_q.lower() not in (c["brand"] or "").lower():
                    continue
                c["found_by"] = "title_search"
            recs.append(c)
        print(f"    products {i + len(batch)}/{len(take)} (tokens left {state['left']})", flush=True)
    out = {"brand": b["name"], "pulled_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "finder_total": total, "finder_root_beauty": used_root, "fetched": len(recs), "truncated": len(asins) > max_asins,
           "tokens_used": state["used"] - t0, "asins": recs}
    json.dump(out, open(os.path.join(OUT, b["slug"] + ".json"), "w"), indent=1)
    print(f"{b['name']}: {len(recs)} ASINs fetched, {out['tokens_used']} tokens", flush=True)


def main():
    args = sys.argv[1:]
    max_asins = 420
    if "--max-asins" in args:
        i = args.index("--max-asins")
        max_asins = int(args[i + 1])
        del args[i:i + 2]
    cfg = json.load(open(os.path.join(os.path.dirname(HERE), "brands.json")))
    os.makedirs(OUT, exist_ok=True)
    for b in cfg["brands"]:
        if args and b["slug"] not in args:
            continue
        pull_brand(b, max_asins)
    print(f"TOTAL Keepa tokens used this run: {state['used']}", flush=True)


if __name__ == "__main__":
    main()
