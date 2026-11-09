#!/usr/bin/env python3
"""Live New-condition offer summary from SP-API (read-only), plus FBA fee estimates where Keepa lacks a fee.

Usage: SPAPI_* env ... python3 -I sp_offers.py --file asins.txt [--fees fee_asins.txt]
Offers: POST /batches/products/pricing/v0/itemOffers (20 ASINs per call, ~0.5 call/s) ->
        raw/sp/offers.json {asin: {buybox, fba_low, fbm_low, offers_fba, offers_fbm, checked_at}}
Fees:   POST /products/fees/v0/feesEstimate (batch of up to 20) at the listed price, FBA ->
        raw/sp/fees.json {asin: {fba_fee, referral_fee, price, checked_at}}
"""
import json, os, sys, time, datetime, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sp_restrictions as spr  # noqa: E402  (token(), SP host)

CAT = os.environ.get("SEPH_DATA") or os.path.join(os.path.dirname(HERE), ".data")   # data lives outside git (see ../.gitignore)
OFF = os.path.join(CAT, "raw", "sp", "offers.json")
FEES = os.path.join(CAT, "raw", "sp", "fees.json")


def post(path, body, min_gap=2.1):
    for i in range(8):
        w = spr._last[0] + min_gap - time.time()
        if w > 0:
            time.sleep(w)
        spr._last[0] = time.time()
        req = urllib.request.Request(spr.SP + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"x-amz-access-token": spr.token(), "content-type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(req, timeout=60).read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                time.sleep(3 * (i + 1))
                continue
            print("HTTP", e.code, e.read()[:300], flush=True)
            return None
        except Exception as e:
            print("err", e, flush=True)
            time.sleep(5)
    return None


def amt(x):
    try:
        return float(x["LandedPrice"]["Amount"])
    except (KeyError, TypeError):
        return None


def offers(asins):
    cache = json.load(open(OFF)) if os.path.exists(OFF) else {}
    now = time.time()
    todo = [a for a in dict.fromkeys(asins) if now - cache.get(a, {}).get("ts", 0) > 6 * 3600]
    print(f"offers: {len(todo)} to fetch", flush=True)
    for i in range(0, len(todo), 20):
        chunk = todo[i:i + 20]
        body = {"requests": [{"uri": f"/products/pricing/v0/items/{a}/offers", "method": "GET", "MarketplaceId": "ATVPDKIKX0DER",
                              "ItemCondition": "New", "CustomerType": "Consumer"} for a in chunk]}
        d = post("/batches/products/pricing/v0/itemOffers", body)
        if not d:
            continue
        for r, a in zip(d.get("responses") or [], chunk):
            pl = (r.get("body") or {}).get("payload") or {}
            a = pl.get("ASIN") or a
            sm = pl.get("Summary") or {}
            lows = sm.get("LowestPrices") or []
            fba = [amt(x) for x in lows if x.get("fulfillmentChannel") == "Amazon" and (x.get("condition") or "").lower() == "new"]
            fbm = [amt(x) for x in lows if x.get("fulfillmentChannel") == "Merchant" and (x.get("condition") or "").lower() == "new"]
            bb = [amt(x) for x in sm.get("BuyBoxPrices") or [] if (x.get("condition") or "").lower() == "new"]
            cnt = {x.get("fulfillmentChannel"): x.get("OfferCount") for x in sm.get("NumberOfOffers") or []
                   if (x.get("condition") or "").lower() == "new"}
            amazon = any(o.get("SellerId") == "ATVPDKIKX0DER" for o in pl.get("Offers") or [])
            cache[a] = {"buybox": next((x for x in bb if x), None), "fba_low": min([x for x in fba if x], default=None),
                        "fbm_low": min([x for x in fbm if x], default=None), "offers_fba": cnt.get("Amazon", 0),
                        "offers_fbm": cnt.get("Merchant", 0), "amazon_offer": amazon,
                        "status": (r.get("status") or {}).get("statusCode"), "ts": time.time(),
                        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
        if (i // 20) % 10 == 0:
            json.dump(cache, open(OFF, "w"))
            print(f"  offers {i + len(chunk)}/{len(todo)}", flush=True)
    json.dump(cache, open(OFF, "w"))
    return cache


def fees(pairs):
    """pairs: [(asin, price)]"""
    cache = json.load(open(FEES)) if os.path.exists(FEES) else {}
    todo = [(a, p) for a, p in pairs if a not in cache and p]
    print(f"fees: {len(todo)} to fetch", flush=True)
    for i in range(0, len(todo), 20):
        chunk = todo[i:i + 20]
        body = [{"FeesEstimateRequest": {"MarketplaceId": "ATVPDKIKX0DER", "IsAmazonFulfilled": True, "Identifier": a,
                                         "PriceToEstimateFees": {"ListingPrice": {"CurrencyCode": "USD", "Amount": p}}},
                 "IdType": "ASIN", "IdValue": a} for a, p in chunk]
        d = post("/products/fees/v0/feesEstimate", body, min_gap=1.1)
        if not d:
            continue
        for r in d if isinstance(d, list) else []:
            a = (r.get("FeesEstimateIdentifier") or {}).get("IdValue")
            fe = r.get("FeesEstimate") or {}
            det = {x.get("FeeType"): (x.get("FinalFee") or {}).get("Amount") for x in fe.get("FeeDetailList") or []}
            cache[a] = {"fba_fee": det.get("FBAFees"), "referral_fee": det.get("ReferralFee"), "status": r.get("Status"),
                        "price": dict(chunk).get(a), "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    json.dump(cache, open(FEES, "w"))
    return cache


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--file" in args:
        offers([l.strip() for l in open(args[args.index("--file") + 1]) if l.strip()])
    if "--fees" in args:
        pairs = []
        for l in open(args[args.index("--fees") + 1]):
            if l.strip():
                a, p = l.split()
                pairs.append((a, float(p)))
        fees(pairs)
