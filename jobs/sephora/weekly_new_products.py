#!/usr/bin/env python3
"""Weekly Sephora catalog job: brand-listing price refresh, new Nick leads, new products, new brands, feedback.

  python3 -I weekly_new_products.py [--dry-run] [--max-tokens 4000] [--screen-tokens 500] [--max-products 80] [--skip-seed]

 1. Hub catalog (all pairs).
 2. Sephora brand listings for every brand (brands.json + job state 'brands_added'; >= 1.1 s per request; one brand
    failing doesn't stop the run): price_lo / price_hi / sale_price / on_sale per catalog product, extra.listing_seen
    (the daily job skips pairs whose product is gone).
 2b. Sephora New arrivals (category product API, isNew refinement, 6 top categories) and the full brand list (Brand
    refinement of each top category) -> snapshot diff; brands in New arrivals / Sale feeds that brands.json lacks.
 3. Nick's new Sephora leads (last 14 days) -> seed_from_leads.py (human-verified pairs; may add brands).
 4. Brands with no matcher rows in the Hub yet (added by hand, auto-added or lead-found): full catalog build (Keepa
    brand finder, catalog/match.py, sp_restrictions.py, sp_offers.py, build.py) sized to the remaining Keepa budget.
 4b. New-brand screen (<= --screen-tokens Keepa tokens): unseen Sephora brands, feed brands first. (a) can_source=false
    -> screened out; (b) Keepa finder: any of the top 5 ASINs with >= 10 sold/mo or rank <= 150k; (c) SP-API listing
    restrictions on those 5. Pass all -> auto-added (state 'brands_added', status 'auto-added <date>', built next run);
    thin demand / approval required -> 'for Nick'; else 'screened out' with the reason. Re-screened after 90 days.
 5. New products of known brands (Sephora New flag in listings, New arrivals, the daily job's Sale-page finds):
    Keepa finder on brand + name words (<= 10 ASINs each), the same match/build pipeline, new ASINs upserted.
    Keepa budget <= --max-tokens for the whole run; what doesn't fit waits a week.
 6. Feedback (GET /api/engine/sephora/feedback): approved vs rejected / wrong match per brand -> brand_weights
    section of weekly_summary.md (report only; no automatic brand removal).
 7. POST /run {kind: 'weekly'}.
--dry-run: nothing is written to the Hub (payloads saved under $SEPH_DATA/out/<date>/).
To add a brand by hand: copy an entry in brands.json (name, slug, sephora_brand_pages = the slug in
sephora.com/brand/<slug>, sephora_queries, sephora_names = Sephora's brandName, keepa_brands = Amazon brand spellings).
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def merged_extra(r, **kw):
    ex = dict(r.get("extra") or {})
    ex.update(kw)
    return ex


def M_search(q):
    """One Sephora keyword search page (brand discovery)."""
    import sephora_pull as sp
    d = sp.get(q, 1) or {}
    return {p["productId"]: p for p in d.get("products") or []}


def build_brand(slug, b, excluded):
    """catalog/match.py + restrictions + offers + build.py for one brand whose raw/sephora and raw/keepa files exist."""
    import match as M
    import build as B
    import sp_restrictions as spr
    import sp_offers
    r = M.match_brand(slug, b)
    json.dump(r, open(os.path.join(C.DATA, "raw", "match", slug + ".json"), "w"), indent=1)
    asins = [m["asin"] for m in r["matches"]]
    restr = spr.run(asins) if asins else {}
    offers = sp_offers.offers(asins) if asins else {}
    r2 = B.fill_family_fees(slug)
    json.dump(r2, open(os.path.join(C.DATA, "raw", "match", slug + ".json"), "w"), indent=1)
    out = B.build(slug, b, restr, excluded, offers)
    prods = {p["productId"]: p for p in out["sephora_products"]}
    return [C.hub_row(m, b["name"], prods.get(m["productId"])) for m in out["matches"]]


norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())


def config_brand(name, cfg):
    """The brands.json entry a Sephora brand name belongs to (exact or prefix match on its names), else None."""
    n = norm(name)
    for b in cfg["brands"]:
        for x in [b["name"]] + b.get("sephora_names", []) + b.get("sephora_queries", []) + b.get("lead_brands", []):
            k = norm(x)
            if k and (k == n or (len(k) >= 5 and len(n) >= 5 and (n.startswith(k) or k.startswith(n)))):
                return b
    return None


def screen_brand(name, excl, day):
    """Cheap screen of a brand Sephora sells that brands.json doesn't have: can_source, Amazon demand (Keepa), restrictions.
    Returns (status, reason, details); status auto_added | for_nick | screened_out."""
    import keepa_pull as kp
    import sp_restrictions as spr
    if norm(name) in excl:
        return "screened_out", "can't source (Hub brands.can_source=false)", {}
    variants = list(dict.fromkeys([name, re.split(r"\s+by\s+", name, flags=re.I)[0].strip(), name.title(), name.upper()]))
    sel = {"brand": variants, "rootCategory": [3760911], "current_COUNT_NEW_gte": 1, "sort": [["current_SALES", "asc"]],
           "perPage": 50, "page": 0}
    d = kp.call("query", {"domain": 1, "selection": json.dumps(sel)}, need=15)
    asins = d.get("asinList") or []
    if not asins:
        return "screened_out", "no Amazon listings with new offers (Keepa)", {}
    top = asins[:5]
    d2 = kp.call("product", {"domain": 1, "asin": ",".join(top), "stats": 90, "history": 0}, need=len(top) + 5)
    recs = [kp.compact(x) for x in d2.get("products") or []]
    ranks = [r["salesRank"] for r in recs if r.get("salesRank")]
    demand = [r for r in recs if (r.get("monthlySold") or 0) >= 10 or (r.get("salesRank") and r["salesRank"] <= 150000)]
    st = collections.Counter(spr.check(a_)["status"] for a_ in top)
    det = {"amazon_asins": len(asins), "best_rank": min(ranks) if ranks else None,
           "monthly_sold_max": max([r.get("monthlySold") or 0 for r in recs], default=0), "restrictions_top5": dict(st),
           "amazon_brands": sorted({r.get("brand") for r in recs if r.get("brand")})}
    n = len(top)
    if not demand:
        if ranks and min(ranks) <= 500000:
            return "for_nick", f"thin demand (best rank {min(ranks):,}, nothing >= 10 sold/mo)", det
        return "screened_out", "no real Amazon demand (no rank <= 150k or >= 10 sold/mo in the top 5)", det
    if st["open"] >= max(1, round(n * 0.6)):
        return "auto_added", f"demand ok (best rank {min(ranks) if ranks else '?'}), {st['open']}/{n} top ASINs open", det
    if st["restricted"] > n / 2:
        return "screened_out", f"restricted (not eligible) on {st['restricted']}/{n} top ASINs", det
    return "for_nick", f"approval required on {st['approval_required']}/{n} top ASINs (open {st['open']})", det


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--max-products", type=int, default=80, help="new products to search on Keepa per run")
    ap.add_argument("--skip-seed", action="store_true")
    ap.add_argument("--screen-tokens", type=int, default=500, help="Keepa tokens for new-brand screening per run")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY", "KEEPA_KEY", "SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")
    import keepa_pull as kp
    import match as M
    t0, day = time.time(), C.today()
    C.DRY_RUN = a.dry_run
    cfg = C.load_brands()
    excluded = {b.lower() for b in cfg.get("exclude_brands", [])}
    problems, counts, notes = [], {}, []
    tokens_used = 0

    # ---- 1. catalog
    cat = C.hub_catalog()
    by_pid = collections.defaultdict(list)
    for r in cat:
        by_pid[r["product_id"]].append(r)
    hub_asins = {r["asin"] for r in cat}
    C.log(f"catalog: {len(cat)} pairs, {len(by_pid)} products")

    # ---- 1b. learned match rules (Nick's wrong-match marks): written to .data/match_rules.json for catalog/match.py,
    # then existing unconfirmed pairs that break one are downgraded to confidence low (the daily watch skips low)
    rules, wrong, rule_down = None, [], []
    try:
        wrong = C.hub("GET", "/api/engine/sephora/feedback").get("wrong_matches") or []
        rules = C.learned_rules(wrong)
        for r in cat:
            if not r.get("active") or r.get("match_ok") is not None or r.get("confidence") not in ("high", "medium"):
                continue
            br = C.rule_breaks(rules, r)
            if br:
                rule_down.append({"asin": r["asin"], "product_id": r["product_id"], "active": r["active"], "confidence": "low",
                                  "match_reason": "; ".join(x for x in [r.get("match_reason"), *br] if x)[:2000],
                                  "_name": r.get("sephora_name"), "_why": br[0]})
        if rule_down and not a.dry_run:
            C.hub_upsert_catalog([{k: v for k, v in x.items() if not k.startswith("_")} for x in rule_down])
    except Exception as e:  # noqa: BLE001
        problems.append(f"match rules step failed: {e!r}")
    counts["rule_downgraded"] = len(rule_down)

    # ---- 2. brand listings
    listing, brand_ok = {}, {}
    for b in cfg["brands"]:
        try:
            r = C.sephora_brand(b)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "products": {}, "error": repr(e), "requests": 0}
        brand_ok[b["name"]] = r["ok"]
        if not r["ok"]:
            problems.append(f"Sephora listing failed for {b['name']}: {r['error']}")
            continue
        for pid, p in r["products"].items():
            listing[pid] = (b, p)
        C.log(f"{b['name']}: {len(r['products'])} Sephora products")
    upd, price_changes, gone = [], 0, 0
    for pid, rows in by_pid.items():
        brand = rows[0].get("brand")
        if pid in listing:
            _, p = listing[pid]
            for r in rows:
                ch = {}
                for f, v in (("price_lo", p.get("price_lo")), ("price_hi", p.get("price_hi")), ("sale_price", p.get("sale_price"))):
                    if v is not None or f == "sale_price":
                        if (C.fnum(r.get(f)) or 0) != (v or 0):
                            ch[f] = v
                if bool(r.get("on_sale")) != bool(p.get("on_sale")):
                    ch["on_sale"] = bool(p.get("on_sale"))
                if ch:
                    price_changes += 1
                if ch or (r.get("extra") or {}).get("listing_seen") is not True:
                    upd.append({"asin": r["asin"], "product_id": pid, "active": r["active"], **ch,
                                "extra": merged_extra(r, listing_seen=True, listing_checked=day.isoformat())})
        elif brand_ok.get(brand):
            for r in rows:
                if (r.get("extra") or {}).get("listing_seen") is not False:
                    gone += 1
                    upd.append({"asin": r["asin"], "product_id": pid, "active": r["active"], "on_sale": False, "sale_price": None,
                                "extra": merged_extra(r, listing_seen=False, listing_checked=day.isoformat())})
    counts.update(brands=len(cfg["brands"]), brands_failed=sum(1 for v in brand_ok.values() if not v),
                  listing_products=len(listing), price_changes=price_changes, products_gone=gone)
    C.log(f"listing refresh: {price_changes} pairs changed price/sale, {gone} pairs' products no longer listed")
    if upd and not a.dry_run:
        try:
            C.log("catalog upsert:", C.hub_upsert_catalog(upd))
        except Exception as e:  # noqa: BLE001
            problems.append(f"listing upsert failed: {e!r}"[:300])
    C.save_json(f"out/{day.isoformat()}/weekly_listing_upsert.json", upd)

    # ---- 2b. Sephora New arrivals + brand list snapshot
    na = C.sephora_new_arrivals()
    if not na["ok"]:
        problems.append("Sephora New arrivals unreadable: " + "; ".join(na["errors"]))
    snap = C.sephora_brand_snapshot()
    if not snap["ok"]:
        problems.append("Sephora brand list unreadable: " + "; ".join(snap["errors"]))
    prev_snap = C.state_get("brand_snapshot", {}) or {}
    snap_new = sorted(set(snap["brands"]) - set(prev_snap.get("brands") or [])) if prev_snap.get("brands") else []
    feed_brands = collections.Counter()   # brands in New arrivals / Sale page that brands.json doesn't have
    for p in na["products"].values():
        if not config_brand(p.get("brandName"), cfg):
            feed_brands[p.get("brandName")] += 1
    for x in C.state_get("new_products", []) or []:
        if not config_brand(x.get("brand"), cfg):
            feed_brands[x.get("brand")] += 1
    counts.update(new_arrivals=len(na["products"]), sephora_brands=len(snap["brands"]), brands_new_on_sephora=len(snap_new),
                  feed_brands_unconfigured=len(feed_brands))

    # ---- 3. Nick's new Sephora leads
    if not a.skip_seed:
        cmd = [sys.executable, "-I", os.path.join(C.HERE, "seed_from_leads.py"), "--since", (day - dt.timedelta(days=14)).isoformat(),
               "--max-tokens", str(max(0, a.max_tokens // 4))] + (["--dry-run"] if a.dry_run else [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        tail = (r.stdout + r.stderr).strip().splitlines()[-12:]
        notes.append("Nick's new Sephora leads (last 14 days):\n" + "\n".join("    " + x for x in tail))
        for x in tail:
            if x.startswith("[") and "Keepa:" in x and "tokens" in x:
                try:
                    tokens_used += int(x.split(",")[-1].split()[0])
                except ValueError:
                    pass
        if r.returncode:
            problems.append("seed_from_leads.py failed (see summary notes)")
        cfg = C.load_brands()   # it may have added brands
        cat = C.hub_catalog()
        hub_asins = {x["asin"] for x in cat}
        by_pid = collections.defaultdict(list)
        for x in cat:
            by_pid[x["product_id"]].append(x)

    budget = a.max_tokens - tokens_used
    new_rows = []
    reserve = min(a.screen_tokens, budget // 4) + 200   # keep some budget for brand screening and new products

    # ---- 4. brands never built by the matcher (build_backlog.py, highest lead spend first)
    import build_backlog
    bl = build_backlog.run(max(0, budget - reserve), a.dry_run, cfg=cfg, cat=cat, log=C.log)
    budget -= bl["tokens"]
    tokens_used += bl["tokens"]
    problems += bl["problems"]
    built_now, deferred = bl["built"], bl["left"]
    if not a.dry_run:
        cat = C.hub_catalog()
        hub_asins = {x["asin"] for x in cat}
        by_pid = collections.defaultdict(list)
        for x in cat:
            by_pid[x["product_id"]].append(x)
    if deferred:
        notes.append(f"Brands waiting for Keepa budget ({len(deferred)}, built by later runs): " + ", ".join(deferred))

    # ---- 4b. new brands: screen unseen ones (feeds first, then new on Sephora, then the rest), <= --screen-tokens
    screened = C.state_get("brands_screened", {}) or {}
    added_state = C.state_get("brands_added", {}) or {}
    exclu = {norm(x) for x in cfg.get("exclude_brands", [])}

    def due(bn):
        x = screened.get(bn)
        return not x or (dt.date.fromisoformat(x["date"]) < day - dt.timedelta(days=90))
    pool = [bn for bn, _ in feed_brands.most_common() if bn] + snap_new + sorted(snap["brands"])
    pool = [bn for bn in dict.fromkeys(pool) if not config_brand(bn, cfg) and bn not in added_state and due(bn)]
    s_budget = min(a.screen_tokens, max(0, budget - 100))
    this_screen = {"auto_added": [], "for_nick": [], "screened_out": []}
    s_used = 0
    for bn in pool:
        if s_used + 20 > s_budget:
            break
        used0 = kp.state["used"]
        try:
            stt, why, det = screen_brand(bn, exclu, day)
        except Exception as e:  # noqa: BLE001
            problems.append(f"screening {bn} failed: {e!r}"[:200])
            continue
        s_used += kp.state["used"] - used0
        if stt == "auto_added":   # learn Sephora's own brandName and check the listing reads
            names = collections.Counter(p_["brandName"] for p_ in M_search(bn).values() if p_.get("brandName"))
            sn = [x for x, _ in names.most_common(3) if norm(x)[:5] == norm(bn)[:5]] or [bn]
            entry = {"name": bn, "slug": re.sub(r"[^a-z0-9]+", "_", bn.lower()).strip("_"),
                     "sephora_brand_pages": [re.sub(r"[^a-z0-9]+", "-", bn.lower()).strip("-")], "sephora_queries": [bn.lower()],
                     "sephora_names": list(dict.fromkeys(sn + [bn])),
                     "keepa_brands": list(dict.fromkeys([bn] + det.get("amazon_brands", []))), "status": f"auto-added {day.isoformat()}",
                     "screen": why}
            if not C.sephora_brand(entry)["ok"]:
                stt, why = "for_nick", why + "; but its Sephora listing couldn't be read by name"
            else:
                added_state[bn] = entry
        screened[bn] = {"status": stt, "reason": why, "date": day.isoformat(), **det}
        this_screen[stt].append((bn, why, feed_brands.get(bn, 0)))
    tokens_used += s_used
    budget -= s_used
    if not a.dry_run:
        C.state_put("brands_screened", screened)
        C.state_put("brands_added", added_state)
        if snap["ok"]:
            C.state_put("brand_snapshot", {"date": day.isoformat(), "brands": sorted(snap["brands"])})
    counts.update(brands_screened=sum(len(v) for v in this_screen.values()), brands_auto_added=len(this_screen["auto_added"]),
                  brands_for_nick=len(this_screen["for_nick"]), brands_left_to_screen=max(0, len(pool) - sum(len(v) for v in this_screen.values())))

    # ---- 5. new products of known brands
    np_file = C.state_get("new_products", []) or []
    cand = {}
    for pid, (b, p) in listing.items():
        if pid not in by_pid and (p.get("flags") or {}).get("new"):
            cand[pid] = (b, p, "new on Sephora")
    for pid, p in na["products"].items():
        b = config_brand(p.get("brandName"), cfg)
        if b and pid not in by_pid and pid not in cand and p.get("brandName", "").lower() not in {x.lower() for x in cfg.get("exclude_brands", [])}:
            cand[pid] = (b, listing.get(pid, (b, p))[1], "Sephora New arrivals")
    names = {n.lower(): b for b in cfg["brands"] for n in b["sephora_names"]}
    for x in np_file:
        b = names.get((x.get("brand") or "").lower())
        if b and x["productId"] not in by_pid and x["productId"] not in cand:
            p = listing.get(x["productId"], (b, None))[1] or {"productId": x["productId"], "name": x["name"], "url": x["url"],
                                                                 "price_lo": x["price_lo"], "price_hi": x["price_hi"],
                                                                 "sale_price": x.get("sale_price"), "on_sale": bool(x.get("sale_price"))}
            cand[x["productId"]] = (b, p, "on the Sale page")
    tried = C.state_get("weekly_tried", {}) or {}
    wk = day.isocalendar()
    order = sorted(cand, key=lambda pid: (pid in tried, hashlib.md5(f"{pid}{wk[0]}{wk[1]}".encode()).hexdigest()))
    searched, found_rows, carry = 0, 0, 0
    by_brand = collections.defaultdict(list)
    for pid in order:
        b, p, why = cand[pid]
        if searched >= a.max_products or budget < 50:
            carry += 1
            continue
        drop = set()
        for n in [b["name"]] + b.get("sephora_names", []) + b.get("keepa_brands", []) + b.get("strip", []):
            drop |= set(M.base_text(n).split())
        words = [w for w in M.tokens(p.get("name") or "", drop) if w not in M.GENERIC][:3] or M.tokens(p.get("name") or "", drop)[:3]
        if not words:
            continue
        sel = {"brand": b["keepa_brands"], "title": " ".join(words), "current_COUNT_NEW_gte": 1,
               "sort": [["current_SALES", "asc"]], "perPage": 50, "page": 0}
        used0 = kp.state["used"]
        try:
            d = kp.call("query", {"domain": 1, "selection": json.dumps(sel)}, need=15)
            asins = [x for x in (d.get("asinList") or []) if x not in hub_asins][:10]
            if asins:
                d2 = kp.call("product", {"domain": 1, "asin": ",".join(asins), "stats": 90, "buybox": 1, "history": 0},
                             need=len(asins) * 3 + 5)
                by_brand[b["slug"]] += [kp.compact(x) for x in d2.get("products") or []]
        except Exception as e:  # noqa: BLE001
            problems.append(f"Keepa search for {p.get('name')}: {e!r}"[:200])
        spent = kp.state["used"] - used0
        budget -= spent
        tokens_used += spent
        searched += 1
        tried[pid] = day.isoformat()
    for slug, recs in by_brand.items():
        b = next(x for x in cfg["brands"] if x["slug"] == slug)
        uniq = {r_["asin"]: r_ for r_ in recs}
        json.dump({"brand": b["name"], "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "finder_total": len(uniq), "finder_root_beauty": False, "fetched": len(uniq), "truncated": False,
                   "tokens_used": 0, "asins": list(uniq.values())},
                  open(os.path.join(C.DATA, "raw", "keepa", slug + ".json"), "w"), indent=1)
        try:
            rows = [x for x in build_brand(slug, b, excluded) if x["asin"] not in hub_asins]
            new_rows += rows
            hub_asins |= {x["asin"] for x in rows}
            found_rows += len(rows)
        except Exception as e:  # noqa: BLE001
            problems.append(f"matching new {b['name']} products failed: {e!r}"[:300])
    if not a.dry_run:
        C.state_put("weekly_tried", tried)
    counts.update(new_products=len(cand), products_searched=searched, products_carried_over=carry,
                  new_pairs=len(new_rows), brands_built=len(built_now), keepa_tokens=tokens_used)
    C.save_json(f"out/{day.isoformat()}/weekly_new_rows.json", new_rows)
    if new_rows and not a.dry_run:
        try:
            C.log("new pairs upsert:", C.hub_upsert_catalog(new_rows))
        except Exception as e:  # noqa: BLE001
            problems.append(f"new pairs upsert failed: {e!r}"[:300])
    # products matched or searched leave new_products.json; the rest carry over
    left = [x for x in np_file if x["productId"] not in tried and x["productId"] not in {r_["product_id"] for r_ in new_rows}]
    if not a.dry_run:
        C.state_put("new_products", left)
        C.save_json("new_products.json", left)

    # ---- 6. feedback
    bw_lines = []
    try:
        fb = C.hub("GET", "/api/engine/sephora/feedback")
        for x in fb.get("brands") or []:
            tot = (x.get("approved") or 0) + (x.get("rejected") or 0)
            flag = " <- Nick keeps rejecting" if tot >= 5 and (x.get("rejected") or 0) / tot >= 0.8 else ""
            bw_lines.append(f"| {x.get('brand')} | {x.get('approved')} | {x.get('rejected')} | {x.get('wrong_match')} | "
                            f"{x.get('units_ordered')} | ${C.fnum(x.get('spend')) or 0:,.0f} |{flag}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"feedback read failed: {e!r}")

    counts["minutes"] = round((time.time() - t0) / 60, 1)
    counts["problems"] = len(problems)
    if not a.dry_run:
        try:
            C.hub("POST", "/api/engine/sephora/run", {"kind": "weekly", "counts": counts})
        except Exception as e:  # noqa: BLE001
            problems.append(f"run record failed: {e!r}")
    L = [f"# Sephora weekly catalog job, {day.isoformat()}{' (DRY RUN: nothing written to the Hub)' if a.dry_run else ''}", "",
         f"- Brand listings: {len(cfg['brands']) - counts['brands_failed']}/{len(cfg['brands'])} brands, {len(listing)} products; "
         f"{price_changes} catalog pairs changed price/sale; {gone} pairs' products no longer on Sephora",
         f"- New brands built: {', '.join(built_now) or 'none'}" + (f"; {len(deferred)} waiting for budget" if deferred else ""),
         f"- New products: {len(cand)} candidates, {searched} searched on Keepa, {carry} carried to next week; {len(new_rows)} new pairs added",
         f"- Keepa tokens: {tokens_used} (budget {a.max_tokens}); runtime {counts['minutes']} min", ""]
    L += ["## New products matched", ""]
    if new_rows:
        L += ["| ASIN | Brand | Sephora product | Amazon title | Conf | ROI now |", "|---|---|---|---|---|---|"]
        for x in sorted(new_rows, key=lambda x: -(x.get("roi_now") or -9)):
            flag = " **>= 21%**" if (x.get("roi_now") or -9) >= 0.21 else ""
            L.append(f"| {x['asin']} | {x['brand']} | {x['sephora_name']} | {(x.get('amazon_title') or '')[:70]} | {x['confidence']} | "
                     f"{C.pct(x.get('roi_now'))}{flag} |")
        L.append("")
    else:
        L += ["None this week.", ""]
    L += [f"## New brands (Sephora lists {len(snap['brands'])}; {len(snap_new)} new since the last snapshot; "
          f"{len(feed_brands)} in New arrivals / Sale not in brands.json; {counts['brands_left_to_screen']} still to screen)", ""]
    for k_, title in (("auto_added", "Auto-added (built by the next weekly run)"), ("for_nick", "For Nick to decide"),
                      ("screened_out", "Screened out")):
        L.append(f"**{title}:** " + ("; ".join(f"{bn} ({why}{', ' + str(n_) + ' feed items' if n_ else ''})" for bn, why, n_ in this_screen[k_]) or "none"))
        L.append("")
    if added_state:
        L += ["To keep auto-added / lead-found brands permanently, copy them from job state 'brands_added' into brands.json "
              "(they are already used by every run while the state lasts).", ""]
    L += notes + [""]
    rstate = C.state_get("match_rules", {}) or {}
    seen_thru = rstate.get("notes_reviewed_through") or ""
    new_notes = [w for w in wrong if C.after_ts(w.get("created_at") or "", seen_thru) and (w.get("note") or set(w.get("tags") or []) & {"version", "product", "other"})]
    L += ["## Match rules learned from Nick's wrong-match marks", ""]
    if rules:
        L += [f"- {len(wrong)} wrong-match marks; automatic checks: " +
              (", ".join(f"{r_['kind']} ({r_['brand']}, {r_['marks']} mark{'s' if r_['marks'] != 1 else ''})" for r_ in rules["auto"]) or "none") +
              f"; manual rules: {len(rules['manual'])}",
              f"- Existing pairs downgraded to low by a rule this week: {len(rule_down)}"]
        L += [f"  - {x['asin']} {x['_name'] or ''}: {x['_why']}" for x in rule_down[:20]]
    L += ["", f"**Notes for Claude to review (RUNBOOK step 4b): {len(new_notes)}**"]
    for w in new_notes[:40]:
        L.append(f"- {w.get('created_at', '')[:10]} {w.get('brand') or ''} | {w.get('asin')} ↔ {w.get('sephora_name') or w.get('sephora_url') or ''} | "
                 f"tags: {', '.join(w.get('tags') or []) or '-'} | note: {w.get('note') or '-'}"
                 + (f" | correct ASIN {w['correct_asin']}" if w.get("correct_asin") else ""))
    L.append("")
    L += ["## brand_weights (Nick's decisions on Sephora candidates)", ""]
    if bw_lines:
        L += ["| Brand | Approved | Rejected | Wrong match | Units ordered | Spend |", "|---|---|---|---|---|---|"] + bw_lines + [""]
    else:
        L += ["No decisions yet.", ""]
    L += (["## Problems", ""] + [f"- {p}" for p in problems] + [""]) if problems else ["No problems.", ""]
    txt = "\n".join(L)
    open(os.path.join(C.DATA, "weekly_summary.md"), "w").write(txt)
    print(txt)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        try:
            open(os.path.join(C.DATA, "weekly_summary.md"), "w").write("# Sephora weekly job FAILED\n\n```\n" + traceback.format_exc()[-3000:] + "\n```\n")
        except OSError:
            pass
        sys.exit(1)
