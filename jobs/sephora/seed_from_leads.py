#!/usr/bin/env python3
"""Seed / top up the catalog from Nick's own Sephora leads (human-verified ASIN <-> Sephora product pairs).

  python3 -I seed_from_leads.py [--dry-run] [--since YYYY-MM-DD] [--max-tokens 2500] [--max-slug-searches 300]

1. Leads from the Hub (GET /api/engine/leads, read-only): Sephora store and a productId (P1234...) in store_link.
   One pair per (asin, productId): latest lead date / cost, total spend (cost x qty_ordered).
2. Sephora listings (gentle, cached 20 h): every brands.json brand; lead brands not in brands.json are found with a
   keyword search on the brand name and recorded in job state 'brands_added' (merged into brands.json by load_brands,
   so the weekly job and the sale watcher cover them; --write-brands-json also writes them into brands.json);
   productIds still unknown get one keyword search on their product-link words. Unknown after that = not on Sephora
   now: priced at the last lead cost, extra.listing_seen=false (the daily job skips those).
3. Brands Nick can't source (brands.json exclude_brands = Hub brands.can_source=false) are skipped and logged.
4. Pairs not yet in the Hub get a row: confidence high, match_reason 'Nick lead <date>'. One active pair per ASIN:
   when the lead's product is live on Sephora, the matcher's other pairs for that ASIN are deactivated (never pairs
   Nick marked match_ok=true); extra lead pairs for the same ASIN are stored inactive.
5. Enrichment for the new ASINs: Keepa /product (stats=90, buybox; within --max-tokens), SP-API listing restrictions and
   getItemOffersBatch, then the catalog economics (same rules as catalog/build.py).
6. Upsert + report: coverage of 2026 Sephora lead spend before / after.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

PID_RE = re.compile(r"\bP\d{4,}\b")
norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def brand_aliases(cfg):
    al = {}
    for b in cfg["brands"]:
        for x in [b["name"], b["slug"]] + b.get("sephora_names", []) + b.get("keepa_brands", []) + b.get("sephora_queries", []) \
                + b.get("lead_brands", []):
            if norm(x):
                al.setdefault(norm(x), b["name"])
    return al


def map_brand(lead_brand, al):
    n = norm(lead_brand)
    if not n:
        return None
    if n in al:
        return al[n]
    for k, v in al.items():   # 'HUDA' -> HUDA BEAUTY, 'Fenty' -> Fenty Beauty by Rihanna, 'Sephora' -> SEPHORA COLLECTION
        if len(n) >= 4 and len(k) >= 4 and (k.startswith(n) or n.startswith(k)):
            return v
    return None


def listings(cfg, max_age_h=20):
    """{pid: (brand_name, record)} for every brands.json brand (cached raw/sephora/<slug>.json younger than max_age_h)."""
    out, fails = {}, []
    for b in cfg["brands"]:
        p = os.path.join(C.DATA, "raw", "sephora", b["slug"] + ".json")
        d = None
        if os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_h * 3600:
            d = {pr["productId"]: pr for pr in json.load(open(p))["products"]}
        else:
            r = C.sephora_brand(b)
            if r["ok"]:
                d = r["products"]
            else:
                fails.append(f"{b['name']}: {r['error']}")
        for pid, pr in (d or {}).items():
            out[pid] = (b["name"], pr)
    return out, fails


def search(q, pages=5):
    import sephora_pull as sp
    prods, seen, total = {}, 0, None
    for page in range(1, pages + 1):
        d = sp.get(q, page)
        if not d:
            break
        ps = d.get("products") or []
        if not ps:
            break
        total = int(d.get("totalProducts") or 0)
        seen += len(ps)
        for p in ps:
            prods[p["productId"]] = sp.record(p, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        if seen >= total or len(ps) < 60:
            break
    return prods


def size_guess_from_cost(cost, lo, hi):
    if not (lo and hi and hi > lo) or not cost:
        return None
    if cost > lo + 0.01:
        return "hi"             # paid more than the small size's list price: the bigger size
    if abs(cost - lo) <= 0.01:
        return "lo"
    return "unknown"            # below both list prices (a sale): can't tell


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", help="only leads on/after this date (default: all)")
    ap.add_argument("--max-tokens", type=int, default=2500)
    ap.add_argument("--max-slug-searches", type=int, default=300)
    ap.add_argument("--write-brands-json", action="store_true", help="also write new brands into brands.json (for a commit)")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY", "KEEPA_KEY", "SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")
    C.DRY_RUN = a.dry_run
    cfg = C.load_brands()
    excl = {norm(x) for x in cfg.get("exclude_brands", [])}
    log = []

    # ---- 1. leads
    leads, off = [], 0
    while True:
        d = C.hub("GET", "/api/engine/leads", params={"limit": 5000, "offset": off, "since": a.since})
        leads += d["leads"]
        if d.get("next_offset") is None:
            break
        off = d["next_offset"]
    seph = [l for l in leads if any("sephora" in (s or "").lower() for s in (l.get("stores") or []))]
    spend = lambda l: float(l.get("cost") or 0) * float(l.get("qty_ordered") or 0)
    pairs = collections.defaultdict(lambda: {"spend": 0.0, "n": 0, "spend_2026": 0.0, "date": ""})
    for l in seph:
        m = PID_RE.search(l.get("store_link") or "")
        asin = (l.get("asin") or "").strip().upper()
        if not m or not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        v = pairs[(asin, m.group(0))]
        v["spend"] += spend(l); v["n"] += 1
        if (l.get("date") or "")[:4] == "2026":
            v["spend_2026"] += spend(l)
        if (l.get("date") or "") >= v["date"]:
            v.update(date=l.get("date") or "", cost=C.fnum(l.get("cost")), brands=l.get("brands") or [], link=l.get("store_link"))
    C.log(f"{len(seph)} Sephora leads -> {len(pairs)} (asin, productId) pairs")

    # ---- 2. catalog + Sephora listings
    cat = C.hub_catalog()
    cat_pairs = {(r["asin"], r["product_id"]): r for r in cat}
    by_asin = collections.defaultdict(list)
    for r in cat:
        by_asin[r["asin"]].append(r)
    todo = {k: v for k, v in pairs.items() if k not in cat_pairs}
    C.log(f"{len(todo)} lead pairs not in the catalog; pulling Sephora listings for {len(cfg['brands'])} brands")
    live, fails = listings(cfg)
    log += [f"listing failed: {x}" for x in fails]

    # new brands: lead brands that map to none of brands.json
    import difflib
    al = brand_aliases(cfg)
    unknown_brands = collections.Counter()
    for (asin, pid), v in todo.items():
        if pid in live:
            continue
        lb = next((x for x in v["brands"] if x), None)
        if lb and not map_brand(lb, al) and norm(lb) not in excl:
            unknown_brands[lb] += v["spend"]
    added_brands = []
    for lb, _ in unknown_brands.most_common():
        if map_brand(lb, brand_aliases(cfg)):
            continue   # an earlier new brand already covers this spelling
        found = search(lb, pages=1)
        names = collections.Counter(p["brandName"] for p in found.values() if p.get("brandName"))
        if not names:
            log.append(f"lead brand '{lb}': Sephora search found nothing (not sold at Sephora now?)")
            continue
        bn = names.most_common(1)[0][0]
        if norm(bn) in excl:
            log.append(f"lead brand '{lb}' is Sephora '{bn}': can't source, skipped")
            continue
        if norm(bn) in brand_aliases(cfg):
            ex = next(b for b in cfg["brands"] if b["name"] == brand_aliases(cfg)[norm(bn)])
            ex.setdefault("lead_brands", []).append(lb)
            continue
        lead_pids = {pid for (asin, pid), v in todo.items() if lb in (v["brands"] or [])}
        same_name = difflib.SequenceMatcher(None, norm(lb), norm(bn)).ratio() >= 0.85 or norm(lb) in norm(bn)
        slug = slugify(bn)
        guess = re.sub(r"[^a-z0-9]+", "-", bn.lower()).strip("-")
        entry = {"name": bn, "slug": slug, "sephora_brand_pages": [guess], "sephora_queries": [bn.lower()],
                 "sephora_names": [bn], "keepa_brands": list(dict.fromkeys([bn, lb])), "lead_brands": [lb],
                 "added": f"{C.today().isoformat()} from Nick's Sephora leads"}
        r = C.sephora_brand(entry)
        if not r["ok"]:
            log.append(f"new brand '{bn}': listing failed ({r['error']})")
            continue
        if not (lead_pids & set(r["products"]) or same_name):
            log.append(f"lead brand '{lb}': Sephora search suggests '{bn}', a different name with none of the {len(lead_pids)} "
                       f"lead products; not added (check by hand)")
            continue
        if not any(p.get("source", "").startswith("brand:") for p in r["products"].values()):
            entry["sephora_brand_pages"] = []    # guessed brand page didn't exist; keyword search it is
        cfg["brands"].append(entry)
        added_brands.append(bn)
        for pid, pr in r["products"].items():
            live[pid] = (bn, pr)
        C.log(f"new brand {bn} ({lb}): {len(r['products'])} Sephora products")

    al = brand_aliases(cfg)
    by_brand = collections.defaultdict(dict)
    for pid, (bn, pr) in live.items():
        by_brand[al.get(norm(bn), bn)][pid] = pr

    def slug_words(link):
        path = urllib.parse.urlparse(link or "").path
        return re.sub(r"[-_/]+", " ", re.sub(r"-?P\d{4,}.*$", "", path.replace("/product/", ""))).strip()

    def relink(text, brand, pool):
        """Same brand, same product type: the current Sephora product whose name best matches the old link's words."""
        import match as M
        b = next((x for x in cfg["brands"] if x["name"] == brand), {"name": brand})
        drop = set()
        for n in [brand] + b.get("sephora_names", []) + b.get("keepa_brands", []) + b.get("strip", []) + b.get("lead_brands", []):
            drop |= set(re.sub(r"[^a-z0-9]+", " ", M.base_text(n)).split())
        q = set(M.tokens(text, drop))
        if len(q) < 2 or not pool:
            return None, 0
        names = {pid: set(M.tokens(pr.get("name") or "", drop)) for pid, pr in pool.items()}
        df = collections.Counter(w for s in names.values() for w in s)
        idf = lambda w: __import__("math").log(1 + len(names) / (df.get(w, 0) + 1)) * (0.35 if w in M.GENERIC else 1)
        qt, qc, qf = M.ptypes(text), M.conc(text), M.flags(text)
        sc = []
        for pid, t in names.items():
            nm = pool[pid].get("name") or ""
            if not t or M.type_conflict(qt, M.ptypes(nm)) or (qc and M.conc(nm) and not (qc & M.conc(nm))):
                continue
            f = M.flags(nm)
            if f["set"] or qf["set"] or f["refill"] != qf["refill"] or f["mini"] != qf["mini"]:
                continue   # never relink sets / kits / calendars (contents change every season) or across sizes
            if any((x in M.ptypes(nm)) != (x in qt) for x in ("brush", "palette")):
                continue
            inter = sum(idf(w) for w in q & t)
            s = inter / max(sum(idf(w) for w in q | t), 1e-9)
            sc.append((s, pid))
        sc.sort(reverse=True)
        if not sc:
            return None, 0
        best, second = sc[0], (sc[1][0] if len(sc) > 1 else 0)
        return (best[1], best[0]) if best[0] >= 0.6 and best[0] - second >= 0.12 else (None, best[0])

    # resolve every lead pair: live / relinked / discontinued / can't source
    slug_n = 0
    res = {}   # (asin, pid) -> (status, new_pid, brand)
    for (asin, pid), v in sorted(pairs.items(), key=lambda kv: -kv[1]["spend"]):
        lb = next((x for x in v["brands"] if x), "")
        if pid in live:
            bn = al.get(norm(live[pid][0]), live[pid][0])
            st = "cant_source" if norm(bn) in excl or norm(lb) in excl else "live"
            res[(asin, pid)] = (st, pid, bn)
            continue
        brand = map_brand(lb, al)
        if norm(lb) in excl or (brand and norm(brand) in excl):
            res[(asin, pid)] = ("cant_source", None, brand or lb)
            continue
        words = slug_words(v["link"])
        new_pid = None
        if (asin, pid) in todo and words and slug_n < a.max_slug_searches:   # a current search result may still list it
            slug_n += 1
            found = search(words, pages=1)
            if pid in found:
                bn = found[pid]["brandName"]
                live[pid] = (al.get(norm(bn), bn), found[pid])
                res[(asin, pid)] = ("live", pid, al.get(norm(bn), bn))
                continue
            if not brand:
                bns = collections.Counter(p["brandName"] for p in found.values())
                brand = next((al.get(norm(x), x) for x, _ in bns.most_common() if norm(x) in al), None)
        if brand and words:
            new_pid, s = relink(words, brand, by_brand.get(brand, {}))
        if new_pid:
            res[(asin, pid)] = ("relinked", new_pid, brand)
        else:
            res[(asin, pid)] = ("discontinued", None, brand or lb)
    C.log(f"slug searches: {slug_n}; " + ", ".join(f"{k} {v}" for k, v in collections.Counter(x[0] for x in res.values()).items()))

    # ---- 3/4. rows (one active pair per ASIN)
    by_lead_asin = collections.defaultdict(list)
    for (asin, pid), v in pairs.items():
        st, npid, bn = res[(asin, pid)]
        if st in ("live", "relinked"):
            by_lead_asin[asin].append((npid, st, bn, v, pid))
    new_rows, deact = {}, []
    for asin, plist in by_lead_asin.items():
        plist.sort(key=lambda x: (x[1] == "live", x[3]["date"]), reverse=True)
        primary = plist[0][0]
        others = [r for r in by_asin.get(asin, []) if r["product_id"] != primary and r["active"]]
        nick_ok = any(r.get("match_ok") is True for r in others)
        if (asin, primary) not in cat_pairs and not nick_ok:
            deact += [{"asin": r["asin"], "product_id": r["product_id"]} for r in others
                      if (r.get("extra") or {}).get("source") != "nick_lead"]
        for npid, st, bn, v, old_pid in plist:
            if (asin, npid) in cat_pairs or (asin, npid) in new_rows:
                continue
            pr = live[npid][1]
            lo, hi = pr.get("price_lo"), pr.get("price_hi")
            needs = bool(lo and hi and hi > lo)
            new_rows[(asin, npid)] = {
                "asin": asin, "productId": npid, "skuId": pr.get("skuId"), "brand": bn,
                "sephora_name": pr.get("name"), "sephora_url": pr.get("url"),
                "confidence": "high" if st == "live" else "medium",
                "reason": f"Nick lead {v['date']}" + (f", relinked (old product {old_pid} is gone from Sephora)" if st == "relinked" else ""),
                "needs_size": needs, "size_guess": size_guess_from_cost(v.get("cost"), lo, hi) if needs else None,
                "price_lo": lo, "price_hi": hi, "sale_price": pr.get("sale_price"), "on_sale": bool(pr.get("on_sale")),
                "_active": npid == primary and not nick_ok,
                "_lead": {"lead_spend": round(v["spend"], 2), "lead_spend_2026": round(v["spend_2026"], 2), "leads": v["n"],
                          "lead_last": v["date"], "lead_cost": v.get("cost"), "source": "nick_lead", "listing_seen": True,
                          **({"relinked_from": old_pid} if st == "relinked" else {})},
            }
    disc = [(k, v) for k, v in pairs.items() if res[k][0] == "discontinued"]
    for (asin, pid), v in sorted(disc, key=lambda kv: -kv[1]["spend_2026"])[:40]:
        log.append(f"discontinued at Sephora: {asin} {pid} {slug_words(v['link'])[:60]!r} (2026 spend ${v['spend_2026']:,.0f})")
    cs = collections.Counter()
    for k, v in pairs.items():
        if res[k][0] == "cant_source":
            cs[res[k][2]] += v["spend_2026"]
    if cs:
        log.append("can't-source skipped (2026 spend): " + ", ".join(f"{k} ${v:,.0f}" for k, v in cs.most_common()))
    C.log(f"new rows: {len(new_rows)} ({sum(1 for r in new_rows.values() if r['_active'])} active); "
          f"matcher pairs to deactivate: {len(deact)}")

    # ---- 5. enrichment
    asins = sorted({r["asin"] for r in new_rows.values()})
    import match as M
    import sp_restrictions as spr
    import sp_offers
    kc = C.load_json("state/keepa_seed.json", {}) or {}
    kc = {k: v for k, v in kc.items() if time.time() - v.get("_ts", 0) < 3 * 86400}
    need = [x for x in asins if x not in kc]
    got, tokens, kstop = C.keepa_products(need, a.max_tokens) if need else ({}, 0, None)
    for k_, v_ in got.items():
        kc[k_] = {**v_, "_ts": time.time()}
    C.save_json("state/keepa_seed.json", kc)
    keepa = {x: kc[x] for x in asins if x in kc}
    if kstop:
        log.append(f"Keepa: {kstop}")
    C.log(f"Keepa: {len(keepa)} of {len(asins)} ASINs, {tokens} tokens")
    restr = spr.run(asins) if asins else {}
    offers = sp_offers.offers(asins) if asins else {}
    def relink_ok(title, size, name, multi):
        """A relinked pair must agree with the Amazon title: product type, liquid/powder form, mini, set, brush, year."""
        t = M.base_text((title or "") + " " + (size or ""))
        n = M.base_text(name or "")
        if not title:
            return False, "no Amazon title to check"
        if M.type_conflict(M.ptypes(t), M.ptypes(n)):
            return False, "product type differs"
        fa, fn = M.forms(t), M.forms(n)
        if fa and fn and not (fa & fn) and ({"liquid", "powder"} <= (fa | fn)):
            return False, "liquid vs powder"
        ft, fl = M.flags(t), M.flags(n)
        if ft["set"] or fl["set"]:
            return False, "set / kit"
        if ft["mini"] != fl["mini"] and not multi:
            return False, "mini vs full size"
        if any((x in M.ptypes(t)) != (x in M.ptypes(n)) for x in ("brush", "palette")):
            return False, "brush / palette"
        if re.search(r"\b20[12]\d\b", t):
            return False, "dated (year) listing"
        return True, ""

    out, dropped_relinks = [], []
    for (asin, pid), r in new_rows.items():
        k = keepa.get(asin) or {}
        if r["confidence"] == "medium":
            ok, why = relink_ok(k.get("title"), k.get("size"), r["sephora_name"], r["needs_size"])
            if not ok:
                dropped_relinks.append(f"{asin} -> {r['sephora_name']} ({why}; Amazon: {(k.get('title') or '')[:60]})")
                continue
        lv = offers.get(asin) or {}
        pq = M.pack_qty(k.get("title") or "", k.get("size")) if k else 1
        bb = k.get("buybox") or lv.get("buybox")
        fba_low = lv.get("fba_low") if lv else k.get("fba_low")
        offers_fba = lv.get("offers_fba") if lv else k.get("offers_fba")
        rev = bb or fba_low
        ref = (k.get("referral_pct") / 100) if k.get("referral_pct") else None
        amz = {"buybox": bb, "fba_low": fba_low, "bb_avg90": k.get("bb_avg90") or k.get("fba_low_avg90"),
               "referral_pct": ref, "fba_fee": k.get("fba_fee")}
        rowlike = {"needs_size": r["needs_size"], "size_guess": r["size_guess"], "extra": {"pack_qty": pq}}
        e = C.economics(rowlike, r["price_lo"], r["price_hi"], r["sale_price"] if r["on_sale"] else None, amz, "catalog")
        el = C.economics({**rowlike, "size_guess": "lo"}, r["price_lo"], r["price_hi"], r["sale_price"] if r["on_sale"] else None,
                         amz, "catalog") if r["needs_size"] and r["size_guess"] == "unknown" else None
        ms, rank = k.get("monthlySold"), k.get("salesRank")
        rs = restr.get(asin) or {}
        b = {**{x: r[x] for x in ("asin", "productId", "skuId", "sephora_name", "sephora_url", "confidence", "reason", "needs_size",
                                  "size_guess", "price_lo", "price_hi")},
             "pack_qty": pq, "sellable": rs.get("status"),
             "restriction_reasons": "; ".join(f"{x.get('code')}: {x.get('message')}" for x in rs.get("reasons", []))[:300],
             "title": k.get("title"), "shade": k.get("color"), "amazon_size": k.get("size"), "buybox": bb, "fba_low": fba_low,
             "fbm_low": lv.get("fbm_low"), "bb_avg90": amz["bb_avg90"], "referral_pct": k.get("referral_pct"),
             "fba_fee": k.get("fba_fee"), "monthly_sold": ms, "sales_rank": rank, "offers_fba": offers_fba,
             "offers_fbm": lv.get("offers_fbm") if lv else k.get("offers_fbm"), "amazon_on": bool(k.get("amazon_on") or lv.get("amazon_offer")),
             "roi_now": e and e["roi_now"], "roi_avg90": e and e["roi_avg90"], "watch_price": e and e["watch_price"],
             "cost": e and e["cost"], "cost_basis": e and e["label"], "revenue_src": e and e["revenue_src"],
             "roi_now_lo": el and el["roi_now"], "watch_price_lo": el and el["watch_price"],
             "demand_ok": bool((ms or 0) >= 10 or (rank and rank <= 150000)),
             "thin_price": bool((not bb and fba_low and (offers_fba or 0) <= 1) or (bb and (offers_fba or 0) == 0 and not ms))}
        h = C.hub_row(b, r["brand"], {"sale_price": r["sale_price"], "on_sale": r["on_sale"]})
        if r["size_guess"] is None and r["needs_size"]:
            h["size_guess"] = "unknown"
        h["active"] = r["_active"]
        h["extra"].update(r["_lead"])
        if not k:
            h["extra"]["keepa_missing"] = True
        out.append(h)

    kept = {x["asin"] for x in out if x["active"]}
    deact = [d_ for d_ in deact if d_["asin"] in kept]   # only replace matcher pairs by a lead pair that is really added
    if dropped_relinks:
        log.append(f"relinks rejected by the Amazon-title check ({len(dropped_relinks)}), counted as discontinued: " + "; ".join(dropped_relinks))
        for x in dropped_relinks:
            a_ = x.split(" ")[0]
            for k_ in [k for k in res if k[0] == a_ and res[k][0] == "relinked"]:
                res[k_] = ("discontinued", None, res[k_][2])
    # ---- 6. write + report
    if added_brands:
        cfg["_doc"] = cfg.get("_doc", "")
        if not a.dry_run:
            st = C.state_get("brands_added", {}) or {}
            for b in cfg["brands"]:
                if b["name"] in added_brands:
                    st[b["name"]] = b
            C.state_put("brands_added", st)
            if a.write_brands_json:
                base = json.load(open(C.BRANDS_JSON))
                have = {b["name"].lower() for b in base["brands"]}
                base["brands"] += [b for b in cfg["brands"] if b["name"] in added_brands and b["name"].lower() not in have]
                json.dump(base, open(C.BRANDS_JSON, "w"), indent=1, ensure_ascii=False)
        log.append(f"added to brands.json{' (dry run: not saved)' if a.dry_run else ''}: {', '.join(added_brands)}")
    C.save_json(f"out/seed_{C.today().isoformat()}.json", {"rows": out, "deactivate": deact, "log": log})
    if not a.dry_run:
        if out:
            C.log("upsert:", C.hub_upsert_catalog(out))
        if deact:
            C.log("deactivate matcher pairs:", C.hub("POST", "/api/engine/sephora/catalog/deactivate", {"keys": deact}))
        C.hub("POST", "/api/engine/sephora/run", {"kind": "seed_leads", "counts": {"lead_pairs": len(pairs), "new_rows": len(out),
              "active": sum(1 for x in out if x["active"]), "deactivated": len(deact), "keepa_tokens": tokens,
              "new_brands": len(added_brands)}})
    # coverage of 2026 Sephora lead spend
    after = C.hub_catalog() if not a.dry_run else None
    act_asins = {r["asin"] for r in (after or cat) if r["active"]}
    if a.dry_run:
        act_asins -= {k["asin"] for k in deact} - {x["asin"] for x in out if x["active"]}
        act_asins |= {x["asin"] for x in out if x["active"]}
    by = collections.Counter()
    cov = collections.Counter()
    for l in seph:
        if (l.get("date") or "")[:4] != "2026":
            continue
        m = PID_RE.search(l.get("store_link") or "")
        asin = (l.get("asin") or "").strip().upper()
        st = res.get((asin, m.group(0)), ("no_product_link",))[0] if m else "no_product_link"
        by[st] += spend(l)
        if asin in act_asins:
            cov[st] += spend(l)
    tot = sum(by.values())
    sells = by["live"] + by["relinked"]
    counts_pairs = collections.Counter(v[0] for v in res.values())
    print(f"\nLead pairs: {dict(counts_pairs)}; new catalog rows {len(out)} ({sum(1 for x in out if x['active'])} active), "
          f"{sum(1 for x in out if x['confidence'] == 'medium')} relinked; matcher pairs deactivated {len(deact)}")
    print(f"2026 Sephora lead spend ${tot:,.0f}: " + ", ".join(f"{k} ${v:,.0f}" for k, v in by.most_common()))
    print(f"Coverage of products Sephora still sells (live + relinked, ${sells:,.0f}): ASIN in the active catalog "
          f"${cov['live'] + cov['relinked']:,.0f} = {(cov['live'] + cov['relinked']) / max(sells, 1) * 100:.1f}%")
    print(f"(All 2026 Sephora lead spend whose ASIN is in the active catalog: {sum(cov.values()) / max(tot, 1) * 100:.1f}%)")
    for x in log:
        print(" -", x)


if __name__ == "__main__":
    main()
