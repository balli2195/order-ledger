#!/usr/bin/env python3
"""Daily Sephora -> Amazon lead job. One command; safe to re-run the same day (candidates are keyed (asin, found_on)).

  python3 -I daily_watch.py [--dry-run] [--max-tokens 2500] [--max-score 60] [--score] [--no-keepa]

Steps
 1. Read the catalog from the Hub (all pairs; active ones are watched).
 2. Sephora (about 4 requests, >= 1.1 s apart): the Sale page (search 'sale', every page) and the promo pages
    (Savings Event / Beauty Offers) for site-wide or brand-wide code discounts -> sale {text, code, ends}.
    Sale-page items join the catalog by productId (markdown price, newly on sale / lower than stored = price drop);
    catalog products that were on sale but are off the Sale page now get on_sale=false. Sale items not in the catalog
    go to new_products.json for the weekly matcher. Full brand listings are refreshed weekly, not here.
 3. Watch rows: active, sellable=open, confidence high/medium (or Nick's match_ok=true), match_ok not false,
    extra.demand_ok, not extra.thin_price, and priceable by the buying rule. Keepa refresh (Buy Box, 90-day avg) in
    priority order (price drops / promo rows first, then ROI closest to or above 15%) within --max-tokens.
 4. ROI prefilter >= 15%: cost = Sephora price x 1.05 x pack (markdown only for the size it belongs to; a broad promo
    takes list x (1 - discount) when lower; Nick's size_confirmed wins; needs_size rows only at size_guess=hi unless
    confirmed); revenue = Buy Box else lowest FBA; minus referral, FBA fee, $1 prep. SP-API getItemOffersBatch re-checks
    the Buy Box / FBA low of the rows that pass.
 5. Lead Score (Hub proxy, <= 25 per call, sequential, <= --max-score rows a day incl. earlier runs today) on the top
    rows by ROI; candidates = pred ROI >= 21% (or ROI >= 21% marked 'not scored' when scoring fails).
 6. Upsert refreshed catalog fields, the candidates and the run record; write daily_summary.md.
--dry-run: no Hub writes (payloads are saved under $SEPH_DATA/out/<date>/); Lead Score is skipped unless --score.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

CONF_OK = ("high", "medium")
CAND_MIN, PREFILTER = 0.21, 0.15
DECIDED_DAYS = 21


def amz_of(r: dict) -> dict:
    return {"buybox": C.fnum(r.get("buybox")), "fba_low": C.fnum(r.get("fba_low")), "bb_avg90": C.fnum(r.get("bb_avg90")),
            "referral_pct": C.fnum(r.get("referral_pct")), "fba_fee": C.fnum(r.get("fba_fee"))}


def flags_of(amz: dict, monthly_sold, sales_rank, offers_fba) -> tuple[bool, bool]:
    demand = bool((monthly_sold or 0) >= 10 or (sales_rank and sales_rank <= 150000))
    src = "buybox" if amz.get("buybox") else ("fba_low" if amz.get("fba_low") else None)
    thin = bool((src == "fba_low" and (offers_fba or 0) <= 1) or (src == "buybox" and (offers_fba or 0) == 0 and not monthly_sold))
    return demand, thin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=2500)
    ap.add_argument("--max-score", type=int, default=60)
    ap.add_argument("--score", action="store_true", help="call Lead Score even in --dry-run")
    ap.add_argument("--no-keepa", action="store_true")
    ap.add_argument("--keepa-wait", type=int, default=1800, help="max seconds to wait for Keepa token refill")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY", "KEEPA_KEY", "SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")
    t0 = time.time()
    C.DRY_RUN = a.dry_run
    day = C.today()
    outdir = os.path.join(C.DATA, "out", day.isoformat())
    os.makedirs(outdir, exist_ok=True)
    cfg = C.load_brands()
    excluded = {b.lower() for b in cfg.get("exclude_brands", [])}
    problems, counts = [], {}

    # ---- 1. catalog
    C.log("reading the Hub catalog")
    cat = C.hub_catalog()
    active = [r for r in cat if r["active"]]
    by_pid = {}
    for r in active:
        by_pid.setdefault(r["product_id"], []).append(r)
    known_pids = {r["product_id"] for r in cat}
    brands = sorted({r["brand"] for r in active if r.get("brand")})
    C.log(f"catalog: {len(cat)} pairs, {len(active)} active, {len(brands)} brands, {len(by_pid)} products")

    # ---- 2. Sephora: sale page + promos
    C.log("Sephora sale page")
    sp = C.sephora_sale_page()
    if not sp["ok"]:
        problems.append(f"Sephora Sale page failed: {sp['error']}")
    elif sp["error"]:
        problems.append(f"Sephora Sale page: {sp['error']}")
    C.log(f"sale page: {len(sp['products'])} items (total {sp['total']})")
    C.log("Sephora promo pages")
    try:
        sale_checked, sale, promos = C.detect_sale(day)
    except Exception as e:  # noqa: BLE001
        sale_checked, sale, promos = False, None, []
        problems.append(f"promo check failed: {e!r}")
    if not sale_checked:
        problems.append("Sephora promo pages unreachable: sale not checked")
    pcfg = cfg.get("promo") or {}
    disc = C.promo_discounts(promos, brands, float(pcfg.get("savings_event_pct") or 0), pcfg.get("event_exclude_brands") or [])
    C.log(f"sale: {sale}  discounts: {disc}")

    def promo_for(brand):
        d = disc.get(brand) or disc.get("*")
        return d if d and d[0] > 0 else None

    # sale-page items -> catalog products / new products
    seph_today = {}        # pid -> {lo, hi, sale, on_sale, dropped, why}
    new_products = []
    for pid, p in sp["products"].items():
        if pid in by_pid:
            r0 = by_pid[pid][0]
            old_sale = C.fnum(r0.get("sale_price")) if r0.get("on_sale") else None
            old_lo, old_hi = C.fnum(r0.get("price_lo")), C.fnum(r0.get("price_hi"))
            lo, hi, sale_p = p.get("price_lo") or old_lo, p.get("price_hi") or old_hi, p.get("sale_price")
            why = []
            if sale_p and old_sale is None:
                why.append(f"newly on sale ${sale_p:.2f}")
            elif sale_p and old_sale and sale_p < old_sale - 0.009:
                why.append(f"sale ${old_sale:.2f} -> ${sale_p:.2f}")
            if lo and old_lo and lo < old_lo - 0.009:
                why.append(f"list ${old_lo:.2f} -> ${lo:.2f}")
            seph_today[pid] = {"lo": lo, "hi": hi, "sale": sale_p, "on_sale": bool(sale_p), "dropped": bool(why),
                               "why": ", ".join(why), "old_eff": old_sale or old_hi}
        elif pid not in known_pids:
            br = (p.get("brandName") or "")
            new_products.append({"productId": pid, "brand": br, "name": p.get("name"), "url": p.get("url"),
                                 "price_lo": p.get("price_lo"), "price_hi": p.get("price_hi"), "sale_price": p.get("sale_price"),
                                 "flags": p.get("flags"), "found_on": day.isoformat(), "source": "sale_page",
                                 "brand_in_config": any(br.lower() in [n.lower() for n in b["sephora_names"]] for b in cfg["brands"]),
                                 "excluded_brand": br.lower() in excluded})
    sale_ended = []
    if sp["ok"]:
        for pid, rows in by_pid.items():
            if pid not in sp["products"] and any(r.get("on_sale") for r in rows):
                sale_ended.append(pid)
                seph_today[pid] = {"lo": C.fnum(rows[0].get("price_lo")), "hi": C.fnum(rows[0].get("price_hi")), "sale": None,
                                   "on_sale": False, "dropped": False, "why": "sale ended", "old_eff": None}
    prev_new = C.state_get("new_products", []) or []
    seen_np = {x["productId"] for x in prev_new}
    merged_new = prev_new + [x for x in new_products if x["productId"] not in seen_np]
    merged_new = [x for x in merged_new if x["productId"] not in known_pids]
    C.save_json("new_products.json", merged_new)
    if not a.dry_run:
        C.state_put("new_products", merged_new)
    price_drops = sum(1 for v in seph_today.values() if v["dropped"])
    C.log(f"sale page: {len(seph_today) - len(sale_ended)} catalog products on sale ({price_drops} new/lower), "
          f"{len(sale_ended)} sales ended, {len(new_products)} sale items not in the catalog")

    def seph(r):
        t = seph_today.get(r["product_id"])
        if t:
            return t["lo"], t["hi"], t["sale"]
        return C.fnum(r.get("price_lo")), C.fnum(r.get("price_hi")), (C.fnum(r.get("sale_price")) if r.get("on_sale") else None)

    # ---- 3. watch rows
    try:   # rules learned from Nick's wrong-match marks: a pair that breaks one isn't proposed until he marks it OK
        rules = C.learned_rules()
    except Exception as e:  # noqa: BLE001
        rules = None
        problems.append(f"match rules unavailable (wrong-match feedback read failed): {e!r}")
    rule_skipped = []

    def watchable(r):
        if not r["active"] or r.get("sellable") != "open" or r.get("match_ok") is False:
            return False
        if rules and r.get("match_ok") is not True:
            br = C.rule_breaks(rules, r)
            if br:
                rule_skipped.append((r["asin"], r.get("sephora_name"), br[0]))
                return False
        if r.get("confidence") not in CONF_OK and r.get("match_ok") is not True:
            return False
        ex = r.get("extra") or {}
        if ex.get("listing_seen") is False:   # product not on Sephora's brand listing at the last weekly refresh
            return False
        return ex.get("demand_ok") is True and ex.get("thin_price") is not True

    best = {}   # one pair per ASIN: Nick-confirmed first, then confidence, then match score
    rank = {"high": 0, "medium": 1, "low": 2}
    for r in active:
        if not watchable(r):
            continue
        k = (r.get("match_ok") is not True, rank.get(r.get("confidence"), 3), -(C.fnum((r.get("extra") or {}).get("score")) or 0))
        if r["asin"] not in best or k < best[r["asin"]][0]:
            best[r["asin"]] = (k, r)
    watch = [r for _, r in best.values()]
    pri = []
    for r in watch:
        lo, hi, sale_p = seph(r)
        e = C.economics(r, lo, hi, sale_p, amz_of(r), "buy", promo_for(r["brand"]))
        if not e:
            continue
        top = max([x for x in (e["roi_now"], e["roi_avg90"]) if x is not None], default=None)
        hot = bool((seph_today.get(r["product_id"]) or {}).get("dropped")) or e["sale_applied"] or e["promo_applied"]
        pri.append((0 if hot else 1, -(top if top is not None else -9), r))
    pri.sort(key=lambda x: (x[0], x[1]))
    counts["watch_rows"] = len(watch)
    counts["watch_priceable"] = len(pri)
    C.log(f"watch rows: {len(watch)} ASINs ({len(pri)} priceable by the buying rule)")

    # Keepa refresh (cache: today's refreshes are reused by a re-run)
    kcache = C.load_json(f"state/keepa_{day.isoformat()}.json", {}) or {}
    order = [r["asin"] for _, _, r in pri]
    todo = [x for x in order if x not in kcache]
    tokens, kstop = 0, None
    if a.no_keepa:
        kstop = "--no-keepa"
    elif todo:
        C.log(f"Keepa: {len(todo)} ASINs to refresh, budget {a.max_tokens} tokens")
        try:
            got, tokens, kstop = C.keepa_products(todo, a.max_tokens, max_wait_s=a.keepa_wait)
            now = time.time()
            for k, v in got.items():
                kcache[k] = {**v, "_ts": now}
            C.save_json(f"state/keepa_{day.isoformat()}.json", kcache)
        except Exception as e:  # noqa: BLE001
            kstop = f"Keepa failed: {e!r}"
    if kstop and not a.no_keepa and "budget" not in kstop:
        problems.append(kstop)
    refreshed = [x for x in order if x in kcache]
    C.log(f"Keepa: {tokens} tokens used, {len(refreshed)} of {len(order)} watch ASINs fresh today ({kstop or 'all done'})")

    amz, meta = {}, {}
    for _, _, r in pri:
        base = amz_of(r)
        ms, rk, of = r.get("monthly_sold"), (r.get("extra") or {}).get("sales_rank"), r.get("offers_fba")
        aon = r.get("amazon_on")
        k = kcache.get(r["asin"])
        if k:
            base["buybox"] = k.get("buybox")
            base["fba_low"] = k.get("fba_low") or base["fba_low"]
            base["bb_avg90"] = k.get("bb_avg90") or k.get("fba_low_avg90") or base["bb_avg90"]
            if k.get("referral_pct"):
                base["referral_pct"] = round(k["referral_pct"] / 100, 4)
            if k.get("fba_fee") is not None:
                base["fba_fee"] = k["fba_fee"]
            ms, rk, of, aon = k.get("monthlySold"), k.get("salesRank"), k.get("offers_fba"), k.get("amazon_on")
        amz[r["asin"]] = base
        meta[r["asin"]] = {"monthly_sold": ms, "sales_rank": rk, "offers_fba": of, "amazon_on": aon, "keepa": bool(k)}

    # ---- 4. prefilter + SP-API
    def evaluate(r):
        lo, hi, sale_p = seph(r)
        e = C.economics(r, lo, hi, sale_p, amz[r["asin"]], "buy", promo_for(r["brand"]))
        m = meta[r["asin"]]
        d, thin = flags_of(amz[r["asin"]], m["monthly_sold"], m["sales_rank"], m["offers_fba"])
        return e, d, thin

    pre = []
    for _, _, r in pri:
        e, d, thin = evaluate(r)
        if e and e["roi_now"] is not None and e["roi_now"] >= PREFILTER and d and not thin:
            pre.append(r)
    C.log(f"prefilter ROI >= 15%: {len(pre)} rows; SP-API offers check")
    sp_live = {}
    if pre:
        try:
            import sp_offers
            sp_live = sp_offers.offers([r["asin"] for r in pre])
        except Exception as e:  # noqa: BLE001
            problems.append(f"SP-API offers failed (Keepa prices used): {e!r}")
    passed = []
    for r in pre:
        lv = sp_live.get(r["asin"]) or {}
        if lv and lv.get("status") in (None, 200):
            if lv.get("buybox"):
                amz[r["asin"]]["buybox"] = lv["buybox"]
            if lv.get("fba_low"):
                amz[r["asin"]]["fba_low"] = lv["fba_low"]
            meta[r["asin"]]["offers_fba"] = lv.get("offers_fba", meta[r["asin"]]["offers_fba"])
            meta[r["asin"]]["amazon_on"] = bool(meta[r["asin"]]["amazon_on"] or lv.get("amazon_offer"))
            meta[r["asin"]]["sp"] = True
        e, d, thin = evaluate(r)
        if e and e["roi_now"] is not None and e["roi_now"] >= PREFILTER and d and not thin:
            passed.append((e["roi_now"], r, e))
    passed.sort(key=lambda x: -x[0])
    C.log(f"after SP-API: {len(passed)} rows >= 15%")

    # skip ASINs Nick decided recently unless the cost is now lower
    decided = {}
    try:
        fb = C.hub("GET", "/api/engine/sephora/feedback", params={"since": (day - dt.timedelta(days=DECIDED_DAYS)).isoformat()})
        for d_ in fb.get("decisions") or []:
            c_ = C.fnum(d_.get("cost"))
            prev = decided.get(d_["asin"])
            if prev is None or (c_ is not None and (prev[1] is None or c_ < prev[1])):
                decided[d_["asin"]] = (d_["status"], c_)
    except Exception as e:  # noqa: BLE001
        problems.append(f"feedback read failed (no recent-decision filter): {e!r}")
    pool, skipped_decided, size_check = [], [], []
    for roi_, r, e in passed:
        if r.get("needs_size") and r.get("size_confirmed") not in ("lo", "hi"):
            if roi_ >= CAND_MIN:   # never posted until Nick confirms the size on the tab; listed in the summary
                size_check.append((roi_, r, e))
            continue
        dd = decided.get(r["asin"])
        if dd and (dd[1] is None or e["entered"] >= dd[1] - 0.01):
            skipped_decided.append((r["asin"], dd[0]))
            continue
        pool.append((roi_, r, e))

    # ---- 5. Lead Score
    top = pool[:a.max_score]
    scache = C.load_json(f"state/scores_{day.isoformat()}.json", {}) or {}
    try:   # scores already on today's candidates (e.g. an earlier run in another session)
        ex = C.hub("GET", "/api/engine/sephora/candidates", params={"since": day.isoformat(), "status": "all"})
        for c_ in ex.get("candidates") or []:
            if c_.get("found_on") == day.isoformat() and c_.get("lead_score_band") is not None:
                key = f"{c_['asin']}|{C.fnum(c_.get('cost')):.2f}"
                scache.setdefault(key, {"ok": True, "band": c_["lead_score_band"], "pred_roi": C.fnum(c_.get("lead_score_pred_roi")),
                                        "notes": c_.get("lead_score_notes"), "from": "hub"})
    except Exception as e:  # noqa: BLE001
        problems.append(f"couldn't read today's candidates: {e!r}")
    scored_today = sum(1 for v in scache.values() if v.get("from") != "hub")
    want = [(r, e) for _, r, e in top if f"{r['asin']}|{e['entered']:.2f}" not in scache]
    room = max(0, a.max_score - scored_today)
    to_score = want[:room]
    failed_keys = set()
    if to_score and (not a.dry_run or a.score):
        C.log(f"Lead Score: {len(to_score)} rows ({len(top) - len(want)} already scored today)")
        fails = 0
        for i in range(0, len(to_score), 25):
            chunk = to_score[i:i + 25]
            body = {"rows": [{"asin": r["asin"], "cost": f"{e['entered']:.2f}", "url": r["sephora_url"], "date": day.isoformat()}
                             for r, e in chunk]}
            try:
                res = C.hub("POST", "/api/engine/sephora/score", body, timeout=900, tries=2)
                for (r, e), s in zip(chunk, res.get("rows") or []):
                    scache[f"{r['asin']}|{e['entered']:.2f}"] = {**s, "from": "run"}
            except Exception as ex_:  # noqa: BLE001
                fails += 1
                failed_keys |= {f"{r['asin']}|{e['entered']:.2f}" for r, e in chunk}
                problems.append(f"Lead Score batch failed: {ex_!r}"[:300])
            C.save_json(f"state/scores_{day.isoformat()}.json", scache)
    elif to_score:
        C.log(f"dry run: Lead Score skipped for {len(to_score)} rows (use --score to call it)")
    if len(want) > room:
        problems.append(f"Lead Score daily cap: {len(want) - room} rows not scored")

    cands, cand_view = [], []
    for roi_, r, e in top:
        s = scache.get(f"{r['asin']}|{e['entered']:.2f}")
        m = meta[r["asin"]]
        st = seph_today.get(r["product_id"]) or {}
        hot = bool(st.get("dropped")) or e["sale_applied"] or e["promo_applied"]
        if s and s.get("ok"):
            if (s.get("pred_roi") or -9) < CAND_MIN:
                continue
            band, pred, notes, sc_note = s.get("band"), s.get("pred_roi"), s.get("notes"), None
        else:
            key = f"{r['asin']}|{e['entered']:.2f}"
            attempted = key in scache or key in failed_keys or (a.dry_run and not a.score)
            if roi_ < CAND_MIN or not attempted:   # past today's Lead Score cap: not posted (unscored only when scoring failed)
                continue
            band = pred = notes = None
            sc_note = "not scored" + (f" ({s.get('why')})" if s and s.get("why") else (" (dry run)" if a.dry_run and not a.score else ""))
        was = st.get("old_eff")
        price_txt = f"Sephora ${e['entered']:.2f}" + (f" (was ${was * (C.fnum((r.get('extra') or {}).get('pack_qty')) or 1):.2f})"
                                                       if was and st.get("dropped") else "")
        bits = [price_txt, f"Buy Box ${(amz[r['asin']]['buybox'] or amz[r['asin']]['fba_low'] or 0):.2f}",
                f"ROI {C.pct(roi_)}", f"90d avg ROI {C.pct(e['roi_avg90'])}"]
        if m.get("monthly_sold"):
            bits.append(f"~{m['monthly_sold']} sold/mo")
        if st.get("why"):
            bits.append(st["why"])
        if e["promo_applied"]:
            bits.append(promo_for(r["brand"])[1])
        if m.get("amazon_on"):
            bits.append("Amazon sells it")
        asz = (r.get("extra") or {}).get("amazon_size")
        if asz:
            bits.append(f"Amazon size {asz}")
        if r.get("needs_size"):
            bits.append("buy the " + ("mini/smaller size" if "lo" == r.get("size_confirmed") else "full size") + " (Nick confirmed)")
        if sc_note:
            bits.append(sc_note)
        if e["promo_applied"]:   # cost from a site-wide / brand event code, not the item's own sale price
            bits.insert(0, "Event code — confirm brand qualifies at checkout")
        row = {"asin": r["asin"], "product_id": r["product_id"], "found_on": day.isoformat(),
               "reason": "sale" if hot else "price_watch", "cost": round(e["entered"], 2), "price_basis": e["label"][:200],
               "buybox": amz[r["asin"]]["buybox"] or amz[r["asin"]]["fba_low"], "roi_now": roi_,
               "lead_score_band": band, "lead_score_pred_roi": round(pred, 4) if pred is not None else None,
               "lead_score_notes": notes, "note": ", ".join(bits)[:1000]}
        cands.append(row)
        cand_view.append({**row, "brand": r["brand"], "sephora_name": r["sephora_name"], "amazon_title": r.get("amazon_title"),
                          "shade": r.get("shade"), "confidence": r.get("confidence")})
    C.log(f"candidates: {len(cands)} (pred ROI >= 21%{' or unscored ROI >= 21%' if any(c['lead_score_band'] is None for c in cands) else ''})")

    # ---- 6. catalog upserts
    upd = {}

    def put(r, **kw):
        k = (r["asin"], r["product_id"])
        upd.setdefault(k, {"asin": r["asin"], "product_id": r["product_id"], "active": r["active"]}).update(kw)

    for pid, t in seph_today.items():
        for r in by_pid.get(pid, []):
            put(r, price_lo=t["lo"], price_hi=t["hi"], sale_price=t["sale"], on_sale=t["on_sale"])
    for _, _, r in pri:
        m = meta[r["asin"]]
        if not (m.get("keepa") or m.get("sp")) and r["product_id"] not in seph_today:
            continue
        lo, hi, sale_p = seph(r)
        A = amz[r["asin"]]
        e = C.economics(r, lo, hi, sale_p, A, "catalog")
        d, thin = flags_of(A, m["monthly_sold"], m["sales_rank"], m["offers_fba"])
        ex = dict(r.get("extra") or {})
        ex.update(demand_ok=d, thin_price=thin, live_checked=day.isoformat())
        if m["sales_rank"]:
            ex["sales_rank"] = m["sales_rank"]
        if e:
            ex["cost"], ex["cost_basis"], ex["revenue_src"] = e["cost"], e["label"], e["revenue_src"]
        if r.get("needs_size") and r.get("size_guess") == "unknown" and not r.get("size_confirmed"):
            el = C.economics({**r, "size_guess": "lo"}, lo, hi, sale_p, A, "catalog")
            if el:
                ex["roi_now_lo"], ex["watch_price_lo"] = el["roi_now"], el["watch_price"]
        fields = dict(buybox=A["buybox"], fba_low=A["fba_low"], bb_avg90=A["bb_avg90"], fba_fee=A["fba_fee"],
                      referral_pct=A["referral_pct"], monthly_sold=m["monthly_sold"], offers_fba=m["offers_fba"],
                      amazon_on=bool(m["amazon_on"]), extra=ex)
        if e:
            fields.update(roi_now=e["roi_now"], roi_avg90=e["roi_avg90"], watch_price=e["watch_price"])
        put(r, **fields)
    cat_rows = list(upd.values())

    counts.update(brands=len(brands), products_checked=len(sp["products"]), sale_page_items=len(sp["products"]),
                  sale_page_in_catalog=len(seph_today) - len(sale_ended), sale_ended=len(sale_ended),
                  price_drops=price_drops, new_products=len(new_products),
                  new_products_in_config=sum(1 for x in new_products if x["brand_in_config"] and not x["excluded_brand"]),
                  promo_brands=sorted(disc), keepa_tokens=tokens, watched=len(refreshed), prefilter_15=len(passed),
                  skipped_recent_decision=len(skipped_decided), scored=len(to_score) if (not a.dry_run or a.score) else 0,
                  posted=len(cands), needs_size_check=len(size_check), catalog_rows_updated=len(cat_rows),
                  minutes=round((time.time() - t0) / 60, 1))
    run_body = {"kind": "daily", "counts": counts}
    if sale_checked:
        run_body["sale"] = sale
    C.save_json(f"out/{day.isoformat()}/catalog_upsert.json", cat_rows)
    C.save_json(f"out/{day.isoformat()}/candidates.json", cand_view)
    C.save_json(f"out/{day.isoformat()}/run.json", run_body)

    posted_res, fatal = None, None
    if not a.dry_run:
        try:
            if cat_rows:
                C.log("catalog upsert:", C.hub_upsert_catalog(cat_rows))
        except Exception as e:  # noqa: BLE001
            problems.append(f"catalog upsert failed: {e!r}"[:300])
        try:
            if cands:
                posted_res = C.hub("POST", "/api/engine/sephora/candidates", {"rows": cands}, timeout=300)
                C.log("candidates:", {k: v for k, v in posted_res.items() if k != "skipped"}, "skipped:", posted_res.get("skipped"))
        except Exception as e:  # noqa: BLE001
            problems.append(f"posting candidates failed: {e!r}"[:300])
            fatal = "the day's candidates could not be posted to the Hub"
        if posted_res:
            counts["hub_inserted"], counts["hub_updated"] = posted_res.get("inserted"), posted_res.get("updated")
            counts["hub_skipped"] = len(posted_res.get("skipped") or [])
        counts["problems"] = len(problems)
        try:
            C.log("run:", C.hub("POST", "/api/engine/sephora/run", run_body))
        except Exception as e:  # noqa: BLE001
            problems.append(f"run record failed: {e!r}"[:300])

    # ---- 7. summary
    lines = [f"# Sephora daily watch, {day.isoformat()}{' (DRY RUN: nothing written to the Hub)' if a.dry_run else ''}", "",
             f"- Sale: {sale['text'] + ' (code ' + str(sale['code']) + ', ends ' + str(sale['ends']) + ')' if sale else ('none found' if sale_checked else 'NOT CHECKED (promo pages unreachable)')}",
             f"- Promo discounts applied: " + (", ".join(f"{'all other brands' if k == '*' else k}: {v[0] * 100:.0f}%" for k, v in disc.items()) or "none"),
             f"- Sephora Sale page: {len(sp['products'])} items; {counts['sale_page_in_catalog']} in the catalog "
             f"({price_drops} newly on sale / cheaper), {len(sale_ended)} catalog sales ended, {len(new_products)} not in the "
             f"catalog ({counts['new_products_in_config']} from configured brands) -> new_products.json",
             f"- Watched {len(watch)} ASINs; Keepa refreshed {len(refreshed)} today ({tokens} tokens this run; {kstop or 'all done'})",
             f"- ROI >= 15% after live check: {len(passed)}; skipped (Nick decided in the last {DECIDED_DAYS} days): {len(skipped_decided)}; "
             f"scored: {counts['scored']}; candidates posted: {len(cands)}; held for a size check: {len(size_check)}",
             f"- Catalog rows updated: {len(cat_rows)}; runtime {counts['minutes']} min",
             f"- Held back by learned match rules (Nick's wrong-match marks): {len(rule_skipped)}", ""]
    if cand_view:
        lines += ["## Candidates", "", "| ASIN | Brand | Sephora product | Cost | Buy Box | ROI | Band | Pred ROI | Reason |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c_ in sorted(cand_view, key=lambda x: -((x["lead_score_pred_roi"] if x["lead_score_pred_roi"] is not None else x["roi_now"]) or 0)):
            lines.append(f"| {c_['asin']} | {c_['brand']} | {c_['sephora_name']}{' (' + c_['shade'] + ')' if c_.get('shade') else ''} | "
                         f"${c_['cost']:.2f} | ${(c_['buybox'] or 0):.2f} | {C.pct(c_['roi_now'])} | {c_['lead_score_band'] if c_['lead_score_band'] is not None else '-'} | "
                         f"{C.pct(c_['lead_score_pred_roi']) if c_['lead_score_pred_roi'] is not None else '-'} | {c_['reason']} |")
        lines.append("")
    if size_check:
        lines += ["## Needs your size check (not posted until Nick confirms the size on the Sephora leads tab)", "",
                  "| ASIN | Brand | Sephora product | Sephora prices | Priced at | Amazon size | Buy Box | ROI |", "|---|---|---|---|---|---|---|---|"]
        for roi_, r, e in size_check:
            lines.append(f"| {r['asin']} | {r['brand']} | {r['sephora_name']} | ${C.fnum(r.get('price_lo')) or 0:.0f}-${C.fnum(r.get('price_hi')) or 0:.0f} | "
                         f"{e['label']} | {(r.get('extra') or {}).get('amazon_size') or '?'} | "
                         f"${(amz[r['asin']]['buybox'] or amz[r['asin']]['fba_low'] or 0):.2f} | {C.pct(roi_)} |")
        lines.append("")
    if rule_skipped:
        lines += ["## Held back by learned match rules (marked OK on the Sephora leads tab -> proposed again)", ""]
        lines += [f"- {a_} {n_ or ''}: {why}" for a_, n_, why in rule_skipped[:30]] + [""]
    if fatal:
        lines.insert(1, f"**RUN FAILED: {fatal}.**")
    if problems:
        lines += ["## Problems", ""] + [f"- {p}" for p in problems] + [""]
    else:
        lines += ["No problems.", ""]
    txt = "\n".join(lines)
    open(os.path.join(C.DATA, "daily_summary.md"), "w").write(txt)
    open(os.path.join(outdir, "daily_summary.md"), "w").write(txt)
    print(txt)
    return 2 if fatal else 0


def _fail(msg):
    try:
        open(os.path.join(C.DATA, "daily_summary.md"), "w").write(f"# Sephora daily watch FAILED ({C.today().isoformat()})\n\n```\n{msg[-3000:]}\n```\n")
    except OSError:
        pass


if __name__ == "__main__":
    # Exit codes: 0 = ran (problems, if any, are listed in daily_summary.md); 1 = crashed / missing env / Hub unreadable;
    # 2 = ran but couldn't post the candidates. daily_summary.md is always written.
    try:
        sys.exit(main())
    except SystemExit as e:
        if e.code not in (0, None, 2):
            _fail(str(e.code))
            sys.exit(1)
        raise
    except Exception:
        traceback.print_exc()
        _fail(traceback.format_exc())
        sys.exit(1)
