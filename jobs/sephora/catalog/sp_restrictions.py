#!/usr/bin/env python3
"""Listing Restrictions (read-only) for ASINs on Nick's seller account.

Usage: SPAPI_CLIENT_ID=.. SPAPI_CLIENT_SECRET=.. SPAPI_REFRESH_TOKEN=.. python3 -I sp_restrictions.py ASIN [ASIN ...]
   or: ... python3 -I sp_restrictions.py --file asins.txt
GET /listings/2021-08-01/restrictions?asin=&sellerId=A2UYRB3AYUMGAJ&marketplaceIds=ATVPDKIKX0DER&conditionType=new_new
Results are cached in raw/sp/restrictions.json ({asin: {status, reasons, checked_at}}); cached rows younger than
--max-age-days (default 7) are not re-asked. Pace <= 4 req/s, backoff on 429.
status: open (no restrictions) | approval_required (APPROVAL_REQUIRED) | restricted (NOT_ELIGIBLE / other) | error
"""
import json, os, sys, time, datetime, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CAT = os.environ.get("SEPH_DATA") or os.path.join(os.path.dirname(HERE), ".data")   # data lives outside git (see ../.gitignore)
CACHE = os.path.join(CAT, "raw", "sp", "restrictions.json")
SP = "https://sellingpartnerapi-na.amazon.com"
SELLER, MP = "A2UYRB3AYUMGAJ", "ATVPDKIKX0DER"
_tok = {"v": None, "exp": 0}
_last = [0.0]


def token():
    if _tok["v"] and time.time() < _tok["exp"] - 60:
        return _tok["v"]
    data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": os.environ["SPAPI_REFRESH_TOKEN"],
                                   "client_id": os.environ["SPAPI_CLIENT_ID"],
                                   "client_secret": os.environ["SPAPI_CLIENT_SECRET"]}).encode()
    with urllib.request.urlopen("https://api.amazon.com/auth/o2/token", data=data, timeout=30) as r:
        d = json.loads(r.read())
    _tok.update(v=d["access_token"], exp=time.time() + d.get("expires_in", 3600))
    return _tok["v"]


def check(asin):
    q = urllib.parse.urlencode({"asin": asin, "sellerId": SELLER, "marketplaceIds": MP, "conditionType": "new_new"})
    for i in range(7):
        w = _last[0] + 0.25 - time.time()
        if w > 0:
            time.sleep(w)
        _last[0] = time.time()
        req = urllib.request.Request(f"{SP}/listings/2021-08-01/restrictions?{q}", headers={"x-amz-access-token": token()})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 * (i + 1))
                continue
            if e.code in (500, 502, 503):
                time.sleep(3)
                continue
            return {"status": "error", "reasons": [{"code": f"HTTP {e.code}", "message": e.read()[:200].decode(errors="replace")}]}
        except Exception as e:
            time.sleep(3)
            continue
        reasons = []
        for x in d.get("restrictions") or []:
            for rr in x.get("reasons") or []:
                reasons.append({"code": rr.get("reasonCode"), "message": (rr.get("message") or "")[:300]})
        if not reasons:
            st = "open"
        elif all(r["code"] == "APPROVAL_REQUIRED" for r in reasons):
            st = "approval_required"
        else:
            st = "restricted"
        return {"status": st, "reasons": reasons}
    return {"status": "error", "reasons": [{"code": "retries", "message": "gave up"}]}


def run(asins, max_age_days=7):
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    now = time.time()
    todo = [a for a in dict.fromkeys(asins)
            if not (a in cache and cache[a].get("status") != "error" and now - cache[a].get("ts", 0) < max_age_days * 86400)]
    print(f"restrictions: {len(asins)} asked, {len(todo)} to check", flush=True)
    for i, a in enumerate(todo):
        r = check(a)
        r["ts"] = time.time()
        r["checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        cache[a] = r
        if (i + 1) % 50 == 0:
            json.dump(cache, open(CACHE, "w"))
            print(f"  {i + 1}/{len(todo)}", flush=True)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(cache, open(CACHE, "w"))
    return cache


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--file"]:
        args = [l.strip() for l in open(args[1]) if l.strip()]
    c = run(args)
    from collections import Counter
    print(Counter(c[a]["status"] for a in args if a in c))
