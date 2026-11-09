#!/usr/bin/env python3
"""Pull every Sephora product for the given brands from the public search JSON.

Usage: python3 -I sephora_pull.py [slug ...]   (no slugs = all brands in brands.json)
Writes <catalog>/raw/sephora/<slug>.json = {brand, pulled_at, queries:{q: n_found}, products:[...]}.
Endpoint: https://www.sephora.com/api/v2/catalog/search?type=keyword&q=<q>&pageSize=120&currentPage=<n>
~1 request/second. Only products whose brandName matches the brand's sephora_names are kept.
"""
import json, os, re, sys, time, subprocess, urllib.parse, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CAT = os.environ.get("SEPH_DATA") or os.path.join(os.path.dirname(HERE), ".data")   # data lives outside git (see ../.gitignore)
OUT = os.path.join(CAT, "raw", "sephora")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_last = [0.0]


def get(q, page, brand_page=False):
    if brand_page:
        url = f"https://www.sephora.com/api/v2/catalog/brands/{q}?pageSize=120&currentPage={page}"
    else:
        url = ("https://www.sephora.com/api/v2/catalog/search?type=keyword&q=" + urllib.parse.quote(q)
               + f"&pageSize=120&currentPage={page}")
    for attempt in range(4):
        wait = _last[0] + 1.1 - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        r = subprocess.run(["curl", "-sS", "--compressed", "-m", "40", "-A", UA, "-H", "Accept: application/json",
                            "-w", "\n%{http_code}", url], capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        if code == "200":
            try:
                return json.loads(body)
            except ValueError:
                pass
        if code == "404":
            return None
        time.sleep(5 * (attempt + 1))
    return None


def money(s):
    if not s:
        return None
    v = re.findall(r"\$?([\d,]+\.?\d*)", str(s))
    return [float(x.replace(",", "")) for x in v] if v else None


def norm_brand(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def record(p, now):
    sku = p.get("currentSku") or {}
    lp = money(sku.get("listPrice")) or []
    sp = money(sku.get("salePrice"))
    vp = money(sku.get("valuePrice"))
    mc = p.get("moreColors")
    return {
        "productId": p.get("productId"),
        "skuId": sku.get("skuId"),
        "name": p.get("displayName"),
        "brandName": p.get("brandName"),
        "url": "https://www.sephora.com" + (p.get("targetUrl") or ""),
        "list_price_raw": sku.get("listPrice"),
        "price_lo": min(lp) if lp else None,
        "price_hi": max(lp) if lp else None,
        "sale_price": min(sp) if sp else None,
        "on_sale": bool(sp) or (p.get("onSaleData") not in (None, "NONE")),
        "on_sale_data": p.get("onSaleData"),
        "value_price": max(vp) if vp else None,
        "n_shades": (mc + 1) if isinstance(mc, int) else None,
        "flags": {"new": bool(sku.get("isNew")), "limited": bool(sku.get("isLimitedEdition")),
                  "exclusive": bool(sku.get("isSephoraExclusive")), "app_only": bool(sku.get("isAppExclusive")),
                  "online_only": bool(sku.get("isOnlineOnly"))},
        "rating": p.get("rating"), "reviews": p.get("reviews"),
        "pulled_at": now,
    }


def pull_brand(b):
    names = {norm_brand(x) for x in b["sephora_names"]}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    prods, qstat, other_brands = {}, {}, {}
    jobs = [(s, True) for s in b.get("sephora_brand_pages") or []] + [(q, False) for q in b["sephora_queries"]]
    for q, is_brand in jobs:
        if is_brand:
            q_label = "brand:" + q
        else:
            q_label = q
        page, found, total, seen = 1, 0, None, 0
        while True:
            d = get(q, page, is_brand)
            if d is None:
                qstat[q_label] = "error/404" if page == 1 else qstat.get(q_label, 0)
                break
            ps = d.get("products")
            if not ps:
                qstat[q_label] = qstat.get(q_label, 0) if page > 1 else "no products key"
                break
            total = d.get("totalProducts")
            seen += len(ps)
            for p in ps:
                bn = norm_brand(p.get("brandName"))
                if bn in names:
                    pid = p.get("productId")
                    if pid not in prods:
                        prods[pid] = record(p, now)
                        prods[pid]["source"] = q_label
                        found += 1
                else:
                    other_brands[p.get("brandName")] = other_brands.get(p.get("brandName"), 0) + 1
            qstat[q_label] = found
            if (total and seen >= int(total)) or len(ps) < 60 or page >= 20:
                break
            page += 1
        print(f"  [{b['name']}] {q_label!r}: +{qstat[q_label]} (total {total})", flush=True)
    out = {"brand": b["name"], "pulled_at": now, "queries": qstat, "products": list(prods.values()),
           "other_brands_seen": dict(sorted(other_brands.items(), key=lambda x: -x[1])[:15])}
    json.dump(out, open(os.path.join(OUT, b["slug"] + ".json"), "w"), indent=1)
    print(f"{b['name']}: {len(prods)} products", flush=True)
    return out


def main():
    cfg = json.load(open(os.path.join(os.path.dirname(HERE), "brands.json")))
    want = set(sys.argv[1:])
    os.makedirs(OUT, exist_ok=True)
    for b in cfg["brands"]:
        if want and b["slug"] not in want:
            continue
        pull_brand(b)


if __name__ == "__main__":
    main()
