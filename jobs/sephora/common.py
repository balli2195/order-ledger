"""Shared helpers for the Sephora -> Amazon jobs (daily_watch.py, weekly_new_products.py, load_catalog.py).

Standalone: stdlib only, never imported by the web app (app/). Credentials come only from environment variables:
  ENGINE_KEY                      Myrddin Hub engine key (header X-Engine-Key)
  KEEPA_KEY                       Keepa API key (shared with other jobs: keep to the per-run budget)
  SPAPI_CLIENT_ID / SPAPI_CLIENT_SECRET / SPAPI_REFRESH_TOKEN   SP-API (read-only calls only)
  HUB_URL     (optional) default https://order-ledger-3mt6.onrender.com
  SEPH_DATA   (optional) working data dir, default jobs/sephora/.data (git-ignored). Nothing here is the source of
              truth: the catalog lives in the Hub table (GET /api/engine/sephora/catalog).
  SEPH_STATE  (optional) 'hub' (default): job state in the Hub doc store /api/engine/docs/sephora_jobs/*; 'local': files only.
  KEEPA_RESERVE (optional) tokens always left on the shared Keepa key for Nick's other jobs (default 1500).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("SEPH_DATA") or os.path.join(HERE, ".data")
os.environ["SEPH_DATA"] = DATA                      # the catalog/ scripts read it at import
for d in ("raw/sephora", "raw/keepa", "raw/match", "raw/sp", "state", "out"):
    os.makedirs(os.path.join(DATA, d), exist_ok=True)
sys.path.insert(0, os.path.join(HERE, "catalog"))
KEEPA_RESERVE = int(os.environ.get("KEEPA_RESERVE") or 1500)
os.environ["KEEPA_RESERVE"] = str(KEEPA_RESERVE)       # catalog/keepa_pull.py waits instead of going below it

HUB = (os.environ.get("HUB_URL") or "https://order-ledger-3mt6.onrender.com").rstrip("/")
CENTRAL = ZoneInfo("America/Chicago")
UPLIFT, PREP, TARGET, FLOOR = 1.05, 1.40, 0.21, 0.15   # $1.40 prep per unit (Nick's Seller Snap figure)
BRANDS_JSON = os.path.join(HERE, "brands.json")


def today() -> dt.date:
    return dt.datetime.now(CENTRAL).date()


def log(*a):
    print(f"[{dt.datetime.now(CENTRAL):%H:%M:%S}]", *a, flush=True)


def need_env(*names):
    miss = [n for n in names if not os.environ.get(n)]
    if miss:
        sys.exit(f"Missing environment variables: {', '.join(miss)} (see RUNBOOK.md)")


def load_brands() -> dict:
    """brands.json plus brands the jobs added since (state 'brands_added': auto-added by the weekly brand screen or found
    in Nick's leads), so a fresh session that can't push brands.json still covers them."""
    cfg = json.load(open(BRANDS_JSON))
    have = {b["name"].lower() for b in cfg["brands"]}
    for b in (state_get("brands_added", {}) or {}).values():
        if b["name"].lower() not in have:
            cfg["brands"].append(b)
            have.add(b["name"].lower())
    return cfg


# ---------------------------------------------------------------- job state (survives a fresh session only with SEPH_STATE=hub)
# Kept in the Hub's engine doc store (GET/PUT /api/engine/docs/sephora_jobs/<name>, approved Nov 9 2026) so a fresh
# scheduled session continues where the last one stopped; a local copy goes to $SEPH_DATA/state. SEPH_STATE=local: files only.
DRY_RUN = False   # set by the scripts; no state writes to the Hub in a dry run


def state_get(name: str, default=None):
    if os.environ.get("SEPH_STATE", "hub") == "hub":
        try:
            d = hub("GET", f"/api/engine/docs/sephora_jobs/{name}")
            v = d.get("data")
            if isinstance(v, dict) and set(v) == {"items"}:
                v = v["items"]
            return v if v is not None else default
        except Exception as e:  # noqa: BLE001
            log(f"state {name}: Hub read failed ({e!r}); using the local copy")
    return load_json(f"state/{name}.json", default)


def state_put(name: str, obj):
    save_json(f"state/{name}.json", obj)
    if os.environ.get("SEPH_STATE", "hub") == "hub" and not DRY_RUN:
        hub("PUT", f"/api/engine/docs/sephora_jobs/{name}", obj if isinstance(obj, dict) else {"items": obj})


def fnum(v):
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def fbool(v):
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return None
    return str(v).strip().lower() in ("1", "true", "yes", "t")


# ---------------------------------------------------------------- Hub engine API

def hub(method: str, path: str, body=None, params=None, timeout=120, tries=4):
    """Call the Hub engine API. Retries 5xx / network errors (Render cold start); 4xx raises with the Hub's message."""
    url = HUB + path + ("?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-Engine-Key": os.environ["ENGINE_KEY"], "Content-Type": "application/json",
                                              "Accept": "application/json", "User-Agent": "sephora-jobs/1"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            msg = e.read()[:500].decode(errors="replace")
            if e.code < 500 and e.code != 429:
                raise RuntimeError(f"Hub {method} {path} -> HTTP {e.code}: {msg}")
            last = f"HTTP {e.code}: {msg}"
        except Exception as e:   # timeout / connection reset
            last = repr(e)
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"Hub {method} {path} failed after {tries} tries: {last}")


def hub_catalog(active: str | None = None) -> list[dict]:
    rows, off = [], 0
    while True:
        d = hub("GET", "/api/engine/sephora/catalog", params={"limit": 1000, "offset": off, "active": active})
        rows += d["rows"]
        if d.get("next_offset") is None:
            return rows
        off = d["next_offset"]


def hub_upsert_catalog(rows: list[dict]) -> dict:
    """Upsert catalog pairs. NOTE: the Hub sets active=true on any pair sent without 'active', so updates of existing
    pairs must carry their current 'active' value (new pairs may leave it out)."""
    tot = {"upserted": 0, "inserted": 0, "updated": 0}
    for i in range(0, len(rows), 1000):
        r = hub("POST", "/api/engine/sephora/catalog", {"rows": rows[i:i + 1000]}, timeout=300)
        for k in tot:
            tot[k] += r.get(k, 0)
    return tot


# ---------------------------------------------------------------- economics (same rules as catalog/build.py)

def referral_frac(pct_frac, price):
    if pct_frac is not None and pct_frac > 0:
        return pct_frac
    return 0.08 if (price or 0) <= 10 else 0.15


def roi(cost, rev, ref, fee):
    if not cost or not rev or fee is None:
        return None
    return round((rev - rev * ref - fee - PREP - cost) / cost, 4)


def watch_price(cost_full, ref, fee):
    if not cost_full or fee is None:
        return None
    return round((cost_full * (1 + TARGET) + fee + PREP) / (1 - ref), 2)


def sale_size(lo, hi, sale):
    """Which size Sephora's salePrice belongs to (it is the listing's default SKU): lo if below the small size's list
    price, hi if between the two, None if it is no discount at all."""
    if not sale:
        return None
    if lo and sale < lo - 0.004:
        return "lo"
    if hi and sale < hi - 0.004:
        return "hi"
    return None


def pick_price(row: dict, lo, hi, sale, mode: str, promo: tuple | None = None):
    """Entered (pre-uplift) Sephora price for one Amazon unit of this pair.
    mode 'catalog': the catalog's convention for the stored roi_now (unknown size -> hi, conservative).
    mode 'buy':     the buying rule: Nick's size_confirmed wins; else needs_size rows only with size_guess=hi.
    promo = (fraction_off, label) for a broad Sephora promo (code discount off the list price; not stacked on a markdown:
    the lower of the two is used).
    Returns (unit_price_entered, full_price_entered, label, sale_applied, promo_applied) or None when it can't be priced."""
    pq = int(fnum((row.get("extra") or {}).get("pack_qty")) or 1)
    needs = bool(row.get("needs_size")) and lo is not None and hi is not None and hi > lo
    if needs:
        conf = row.get("size_confirmed")
        guess = row.get("size_guess")
        if conf in ("lo", "hi"):
            basis, why = conf, "Nick confirmed " + ("mini/smaller size" if conf == "lo" else "full size")
        elif mode == "buy":
            if guess != "hi":
                return None
            basis, why = "hi", "full size (guess)"
        else:
            basis, why = (guess if guess in ("lo", "hi") else "hi"), f"size guess {guess or 'unknown'}"
        full = lo if basis == "lo" else hi
        use_sale = sale_size(lo, hi, sale) == basis
    else:
        full = hi if hi is not None else lo
        basis, why = "single", None
        use_sale = bool(sale and full and sale < full - 0.004)
    if not full:
        return None
    price = sale if use_sale else full
    label = f"sale ${price:.2f} (list ${full:.2f})" if use_sale else f"list ${full:.2f}"
    promo_used = False
    if promo and promo[0] and round(full * (1 - promo[0]), 2) < price - 0.004:
        price, use_sale, promo_used = round(full * (1 - promo[0]), 2), False, True
        label = f"${price:.2f} = list ${full:.2f} - {promo[0] * 100:.0f}% ({promo[1]})"
    label += f", {why}" if why else ""
    if pq > 1:
        label += f", x{pq} (Amazon {pq}-pack)"
    return round(price * pq, 2), round(full * pq, 2), label, use_sale, promo_used


def economics(row: dict, lo, hi, sale, amz: dict, mode: str, promo: tuple | None = None):
    """ROI for a catalog pair from Sephora prices and Amazon numbers amz = {buybox, fba_low, bb_avg90, referral_pct
    (fraction), fba_fee}. Returns dict or None."""
    p = pick_price(row, lo, hi, sale, mode, promo)
    if not p:
        return None
    entered, full_entered, label, sale_applied, promo_applied = p
    rev = amz.get("buybox") or amz.get("fba_low")
    ref = referral_frac(amz.get("referral_pct"), rev)
    fee = amz.get("fba_fee")
    cost = round(entered * UPLIFT, 2)
    return {"entered": entered, "cost": cost, "label": label, "sale_applied": sale_applied, "promo_applied": promo_applied,
            "full_entered": full_entered, "revenue": rev,
            "revenue_src": "buybox" if amz.get("buybox") else ("fba_low" if amz.get("fba_low") else None),
            "roi_now": roi(cost, rev, ref, fee), "roi_avg90": roi(cost, amz.get("bb_avg90"), ref, fee),
            "watch_price": watch_price(round(full_entered * UPLIFT, 2), ref, fee), "referral": ref}


# ---------------------------------------------------------------- catalog build row -> Hub row

EXTRA_KEYS = ("demand_ok", "thin_price", "pack_qty", "sales_rank", "roi_now_lo", "watch_price_lo", "cost_basis", "cost",
              "revenue_src", "amazon_size", "score", "offers_fbm", "fbm_low", "restriction_reasons", "size_guess_reason",
              "sale_note", "fba_fee_src")


def hub_row(r: dict, brand: str, product: dict | None = None) -> dict:
    """Map one catalog/build.py row (dict from the per-brand JSON or all_matches.csv) to the Hub's catalog fields.
    product = the raw Sephora product (for the product-level sale price, which build.py drops on non-mini rows)."""
    lo, hi = fnum(r.get("price_lo")), fnum(r.get("price_hi"))
    sale = fnum((product or {}).get("sale_price")) if product is not None else fnum(r.get("sale_price"))
    on_sale = bool((product or {}).get("on_sale")) if product is not None else bool(sale)
    ref = fnum(r.get("referral_pct"))
    sg = r.get("size_guess") or None
    extra = {}
    for k in EXTRA_KEYS:
        v = r.get(k)
        if v in (None, ""):
            continue
        if k in ("demand_ok", "thin_price"):
            v = fbool(v)
        elif k in ("pack_qty", "sales_rank", "offers_fbm"):
            v = int(float(v))
        elif k in ("roi_now_lo", "watch_price_lo", "cost", "fbm_low", "score"):
            v = fnum(v)
        extra[k] = v
    extra.setdefault("pack_qty", 1)
    out = {
        "asin": r["asin"], "product_id": r["productId"], "sku_id": r.get("skuId") or None, "brand": brand,
        "sephora_name": r.get("sephora_name"), "sephora_url": r.get("sephora_url"), "shade": r.get("shade") or None,
        "amazon_title": r.get("title"), "match_reason": r.get("reason"),
        "price_lo": lo, "price_hi": hi, "sale_price": sale, "on_sale": on_sale,
        "buybox": fnum(r.get("buybox")), "fba_low": fnum(r.get("fba_low")), "bb_avg90": fnum(r.get("bb_avg90")),
        "fba_fee": fnum(r.get("fba_fee")), "watch_price": fnum(r.get("watch_price")),
        "referral_pct": round(ref / 100, 4) if ref is not None else None,      # stored as a fraction (0.15 = 15%)
        "roi_now": fnum(r.get("roi_now")), "roi_avg90": fnum(r.get("roi_avg90")),
        "monthly_sold": int(fnum(r.get("monthly_sold"))) if fnum(r.get("monthly_sold")) is not None else None,
        "offers_fba": int(fnum(r.get("offers_fba"))) if fnum(r.get("offers_fba")) is not None else None,
        "needs_size": bool(fbool(r.get("needs_size"))), "amazon_on": bool(fbool(r.get("amazon_on"))), "active": True,
        "size_guess": sg if sg in ("lo", "hi", "unknown") else None,          # 'single' (one price) -> null
        "confidence": r.get("confidence"),
        "sellable": r.get("sellable") if r.get("sellable") in ("open", "approval_required", "restricted") else None,
        "extra": extra,
    }
    return out


# ---------------------------------------------------------------- Sephora listing pass (gentle: >= 1.1 s per request)

def sephora_brand(b: dict) -> dict:
    """One brand's current Sephora listing via the brand page API (or keyword search when the brand has no page).
    Returns {ok, products: {pid: record}, requests, error}. Never raises."""
    import sephora_pull as sp
    names = {sp.norm_brand(x) for x in b["sephora_names"]}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    pages = [(s, True) for s in b.get("sephora_brand_pages") or []]
    queries = [(q, False) for q in b.get("sephora_queries") or []]
    prods, reqs, errs = {}, 0, []
    for q, is_brand in pages + queries:
        if is_brand is False and pages and prods:
            break   # the brand page worked: it lists the whole brand; keyword search only when it didn't
        page, seen = 1, 0
        while True:
            reqs += 1
            try:
                d = sp.get(q, page, is_brand)
            except Exception as e:   # noqa: BLE001
                d, _ = None, errs.append(repr(e))
            if d is None:
                if page == 1:
                    errs.append(f"{q!r}: no response")
                break
            ps = d.get("products") or []
            if not ps:
                if page == 1 and is_brand:
                    errs.append(f"{q!r}: no products")
                break
            seen += len(ps)
            for p in ps:
                if sp.norm_brand(p.get("brandName")) in names and p.get("productId") not in prods:
                    rec = sp.record(p, now)
                    rec["source"] = ("brand:" if is_brand else "") + q
                    prods[p["productId"]] = rec
            tot = d.get("totalProducts")
            if (tot and seen >= int(tot)) or len(ps) < 60 or page >= 20:
                break
            page += 1
    ok = bool(prods)
    if ok and errs and pages:
        errs = [e for e in errs if not any(e.startswith(repr(p)) for p, _ in pages)]   # page failed, search covered it
    if ok:   # same file format as catalog/sephora_pull.py, so match.py can use it
        json.dump({"brand": b["name"], "pulled_at": now, "queries": {}, "products": list(prods.values())},
                  open(os.path.join(DATA, "raw", "sephora", b["slug"] + ".json"), "w"), indent=1)
    return {"ok": ok, "products": prods, "requests": reqs, "error": "; ".join(errs) or None}


def sephora_sale_page() -> dict:
    """Sephora's Sale page (sephora.com/sale is the special search 'sale': sid specialsearch_sale), every page.
    About 2 requests (120 products per page). Returns {ok, products: {pid: record}, total, requests, error}. Never raises."""
    import sephora_pull as sp
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    prods, reqs, seen, total, err, page = {}, 0, 0, None, None, 1
    while page <= 20:
        reqs += 1
        try:
            d = sp.get("sale", page)
        except Exception as e:  # noqa: BLE001
            d, err = None, repr(e)
        if d is None:
            err = err or f"page {page}: no response"
            break
        ps = d.get("products") or []
        if not ps:
            break
        total = int(d.get("totalProducts") or 0) or total
        seen += len(ps)
        for p in ps:
            rec = sp.record(p, now)
            rec["source"] = "sale_page"
            prods[p["productId"]] = rec
        if (total and seen >= total) or len(ps) < 60:
            break
        page += 1
    if total and len(prods) < total * 0.9:
        err = (err + "; " if err else "") + f"only {len(prods)} of {total} sale items read"
    return {"ok": bool(prods), "products": prods, "total": total, "requests": reqs, "error": err}


SEPHORA_CATS = {"Makeup": "cat140006", "Skincare": "cat150006", "Fragrance": "cat160006", "Hair": "cat130038",
                "Bath & Body": "cat140014", "Men": "cat130044"}


def _sget(path: str):
    """GET https://www.sephora.com<path> as JSON with the same pacing as catalog/sephora_pull.py (>= 1.1 s)."""
    import sephora_pull as sp
    import subprocess
    for attempt in range(3):
        w = sp._last[0] + 1.1 - time.time()
        if w > 0:
            time.sleep(w)
        sp._last[0] = time.time()
        r = subprocess.run(["curl", "-sS", "--compressed", "-m", "40", "-A", sp.UA, "-H", "Accept: application/json",
                            "-w", "\n%{http_code}", "https://www.sephora.com" + path], capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        if code == "200":
            try:
                return json.loads(body)
            except ValueError:
                pass
        if code == "404":
            return None
        time.sleep(4 * (attempt + 1))
    return None


def sephora_new_arrivals() -> dict:
    """Sephora's New products (category product API with the isNew refinement, every top-level category, every page).
    Returns {ok, products: {pid: record + category}, errors}."""
    import sephora_pull as sp
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    prods, errs = {}, []
    for name, cat in SEPHORA_CATS.items():
        page, seen = 1, 0
        while page <= 15:
            d = _sget(f"/api/v2/catalog/categories/{cat}/products?pageSize=120&currentPage={page}&ref=filters%5BisNew%5D%3Dtrue")
            if d is None:
                errs.append(f"{name} page {page}")
                break
            ps = d.get("products") or []
            for p in ps:
                rec = sp.record(p, now)
                rec["source"], rec["category"] = "new_arrivals", name
                prods.setdefault(p["productId"], rec)
            seen += len(ps)
            if not ps or seen >= int(d.get("totalProducts") or 0) or len(ps) < 60:
                break
            page += 1
    return {"ok": bool(prods), "products": prods, "errors": errs}


def sephora_brand_snapshot() -> dict:
    """Every brand Sephora lists: the union of the Brand refinement of each top-level category (one small request each).
    Returns {ok, brands: {name: [categories]}, errors}."""
    out, errs = {}, []
    for name, cat in SEPHORA_CATS.items():
        d = _sget(f"/api/v2/catalog/categories/{cat}/products?pageSize=1&currentPage=1")
        if d is None:
            errs.append(name)
            continue
        for r in d.get("refinements") or []:
            if r.get("displayName") == "Brand":
                for v in r.get("values") or []:
                    bn = (v.get("refinementValueDisplayName") or "").strip()
                    if bn:
                        out.setdefault(bn, []).append(name)
    return {"ok": bool(out), "brands": out, "errors": errs}


# ---------------------------------------------------------------- site-wide / category sale signal

_MD = re.compile(r"(\d{1,2})/(\d{1,2})")


def _md_date(m, d, ref: dt.date) -> dt.date | None:
    try:
        x = dt.date(ref.year, int(m), int(d))
    except ValueError:
        return None
    if (x - ref).days < -200:   # 1/5 seen in December -> next year
        x = dt.date(ref.year + 1, int(m), int(d))
    elif (x - ref).days > 200:
        x = dt.date(ref.year - 1, int(m), int(d))
    return x


def _strings(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)
    elif isinstance(o, str):
        yield o


def _content(path: str):
    import sephora_pull as sp
    import subprocess
    for attempt in range(3):
        w = sp._last[0] + 1.1 - time.time()
        if w > 0:
            time.sleep(w)
        sp._last[0] = time.time()
        r = subprocess.run(["curl", "-sS", "--compressed", "-m", "40", "-A", sp.UA, "-H", "Accept: application/json",
                            "-w", "\n%{http_code}", "https://www.sephora.com/api/content/" + path], capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        if code == "200":
            try:
                return json.loads(body)
            except ValueError:
                pass
        time.sleep(4 * (attempt + 1))
    return None


def detect_sale(ref_day: dt.date | None = None) -> tuple[bool, dict | None, list[dict]]:
    """Sephora's promo content API (/api/content/beauty/savings-event and /beauty-offers; reachable from here).
    Returns (checked, sale, promos): checked=False when neither page could be read (leave 'sale' out of the run);
    sale=None when read and no current code-based promo; else {text, code, ends}. promos = every current code-based
    promo found: [{code, ends, text, blob, event}]; event=True for the site-wide Savings Event; blob is the raw banner
    text (tiers / brand % off) for discount parsing."""
    ref_day = ref_day or today()
    se, bo = _content("beauty/savings-event"), _content("beauty/beauty-offers")
    if se is None and bo is None:
        return False, None, []
    found = []
    if se:
        nav = ((se.get("data") or {}).get("navigation") or {})
        desc = nav.get("description") or ""
        alt = ((nav.get("media") or {}).get("altText") or "")
        m = re.search(r"\bcode\s+([A-Z0-9]{4,})", desc) or re.search(r"\bcode\s+([A-Z0-9]{4,})", alt)
        ends = [_md_date(*x, ref_day) for x in re.findall(r"[Ee]nds\s+(\d{1,2})/(\d{1,2})", desc + " " + alt)]
        ends = [x for x in ends if x]
        if m and ends and max(ends) >= ref_day:
            tiers = re.findall(r"([A-Z][A-Z ]{2,30}?)\s+(\d{1,2})% OFF", alt)
            tier_txt = ", ".join(f"{t.strip().title()} {p}%" for t, p in tiers)
            txt = f"{nav.get('label') or 'Sephora Savings Event'}: {desc.strip()}" + (f" ({tier_txt})" if tier_txt else "")
            found.append({"ends": max(ends), "code": m.group(1), "text": txt, "blob": desc + " | " + alt, "event": True})
    if bo:
        for item in _items(bo):
            if "promofinancial" not in str(item.get("sid") or "").lower():
                continue
            blob = " | ".join(s for s in _strings({k: item.get(k) for k in ("title", "description", "media")}) if not s.startswith("/"))
            m = re.search(r"\bcode\s+([A-Z0-9]{4,})", blob)
            rng = re.findall(r"(\d{1,2})/(\d{1,2})\s*[-–]\s*(\d{1,2})/(\d{1,2})", blob)
            ends = [_md_date(c, d, ref_day) for _, _, c, d in rng] + \
                   [_md_date(*x, ref_day) for x in re.findall(r"[Ee]nds\s+(\d{1,2})/(\d{1,2})", blob)]
            ends = [x for x in ends if x]
            if m and ends and max(ends) >= ref_day:
                title = re.sub(r"[\^‡†]\d?", "", str(item.get("title") or item.get("description") or "")).strip()
                found.append({"ends": max(ends), "code": m.group(1), "text": title, "blob": blob,
                              "event": "savings event" in blob.lower() and "everything" in (title + blob).lower()})
    promos = [{**f, "ends": f["ends"].isoformat()} for f in found]
    if not found:
        return True, None, promos
    found.sort(key=lambda f: (f["event"], f["ends"], len(f["text"])), reverse=True)
    main = found[0]
    extra = [f["text"] for f in found[1:] if f["text"] and f["code"] == main["code"] and f["text"].lower() not in main["text"].lower()]
    text = main["text"] + ("; also " + "; ".join(dict.fromkeys(extra)) if extra else "")
    text = re.sub(r"\s+", " ", re.sub(r"[‡†]", "", text)).strip()[:300]
    return True, {"text": text, "code": main["code"], "ends": main["ends"].isoformat()}, promos


def promo_discounts(promos: list[dict], brands: list[str], event_pct: float, event_exclude=()) -> dict:
    """Flat discounts by catalog brand from the current promos: {'*': (frac, label)} for the site-wide event (Nick's
    configured event rate) and {brand: (frac, label)} where a banner says 'NN% off <brand>' / '<BRAND> NN% OFF'.
    Tier banners (Rouge / VIB / Insider) are not brands and are ignored; the event rate covers them."""
    out = {}
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    for p in promos:
        lab = f"code {p['code']} until {p['ends']}"
        if p.get("event") and event_pct:
            out["*"] = max(out.get("*", (0, "")), (event_pct, f"Savings Event {event_pct * 100:.0f}%, {lab}"))
        blob = p.get("blob") or p.get("text") or ""
        for b in brands:
            nb = norm(b)
            for m in re.finditer(r"(\d{1,2})%\s*off\W{0,3}(?:all\s+)?([A-Za-z0-9 &'./-]{3,40})|([A-Za-z0-9 &'./-]{3,40}?)\s+(\d{1,2})%\s*off",
                                 blob, re.I):
                pct_, name = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
                if nb and nb in norm(name):
                    out[b] = max(out.get(b, (0, "")), (int(pct_) / 100, f"{b} {pct_}% off, {lab}"))
    if "*" in out and event_exclude:   # brands the event's fine print excludes: no event rate (own banner still counts)
        for b in brands:
            if b.lower() in {x.lower() for x in event_exclude} and b not in out:
                out[b] = (0.0, "excluded from the event")
    return out
def _items(o):
    if isinstance(o, dict):
        if "sid" in o and ("title" in o or "description" in o or "media" in o):
            yield o
        for v in o.values():
            yield from _items(v)
    elif isinstance(o, list):
        for v in o:
            yield from _items(v)


# ---------------------------------------------------------------- Keepa (budgeted, shared key)

def keepa_tokens() -> dict:
    import gzip
    req = urllib.request.Request("https://api.keepa.com/token?key=" + os.environ["KEEPA_KEY"], headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def keepa_products(asins: list[str], budget: int, reserve: int = KEEPA_RESERVE, max_wait_s: int = 1800) -> tuple[dict, int, str | None]:
    """Keepa /product (stats=90, buybox) for asins in order, stopping before the run's token budget is exceeded or
    when waiting for refill would take longer than max_wait_s. Never takes the shared key below `reserve` tokens (keepa_pull.call waits for refill).
    Returns ({asin: compact record}, tokens_used, stop_reason)."""
    import keepa_pull as kp
    out, t_start, stop = {}, time.time(), None
    used0 = kp.state["used"]
    try:
        tk = keepa_tokens()
        kp.state["left"], kp.state["refill_rate"] = tk.get("tokensLeft"), tk.get("refillRate") or 125
    except Exception as e:  # noqa: BLE001
        return out, 0, f"Keepa token check failed: {e!r}"
    i = 0
    while i < len(asins):
        spent = kp.state["used"] - used0
        room = budget - spent
        n = min(50, len(asins) - i, room // 3)
        if n <= 0:
            stop = f"token budget reached ({spent}/{budget})"
            break
        left = kp.state["left"] if kp.state["left"] is not None else 0
        wait = max(0, (n * 3 + reserve - left)) / (kp.state["refill_rate"] or 125) * 60
        if time.time() - t_start + wait > max_wait_s:
            stop = f"would wait {wait:.0f}s more for Keepa tokens (time cap {max_wait_s}s)"
            break
        batch = asins[i:i + n]
        try:
            d = kp.call("product", {"domain": 1, "asin": ",".join(batch), "stats": 90, "buybox": 1, "history": 0},
                        need=n * 3)   # keepa_pull adds its RESERVE
        except Exception as e:  # noqa: BLE001
            stop = f"Keepa error: {e!r}"
            break
        for p in d.get("products") or []:
            c = kp.compact(p)
            out[c["asin"]] = c
        i += n
    return out, kp.state["used"] - used0, stop


# ---------------------------------------------------------------- misc

def save_json(name: str, obj):
    p = os.path.join(DATA, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, p)
    return p


def load_json(name: str, default=None):
    p = os.path.join(DATA, name)
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return default


def pct(v):
    return "?" if v is None else f"{v * 100:.0f}%"
