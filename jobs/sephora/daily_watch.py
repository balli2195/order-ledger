#!/usr/bin/env python3
"""Daily Sephora -> Amazon lead job. One command; safe to re-run the same day (candidates are keyed (asin, found_on)).

  python3 -I daily_watch.py [--dry-run] [--max-tokens 2500] [--max-score N] [--score] [--no-keepa]

Steps
 1. Read the catalog from the Hub (all pairs; active ones are watched).
 2. Sephora (about 4 requests, >= 1.1 s apart): the Sale page (search 'sale', every page) and the promo pages
    (Savings Event / Beauty Offers) for site-wide or brand-wide code discounts -> sale {text, code, ends}.
    Sale-page items join the catalog by productId (markdown price, newly on sale / lower than stored = price drop);
    catalog products that were on sale but are off the Sale page now get on_sale=false. Sale items not in the catalog
    go to new_products.json for the weekly matcher. Full brand listings are refreshed weekly, not here.
 3. Watch rows: active, sellable=open, match_ok not false (any confidence; learned rules only flag, Nick Nov 19 2026),
    and priceable by the buying rule. No demand or thin-price gate (Nick, Nov 23, 2026): those rows are posted with a
    "No competition / restock" note; with no offer at all the 90-day average Buy Box prices them. Amazon prices (Buy Box, 90-day avg)
    come from the Keepa collector's latest pull (<= 36 h old; no tokens); only ASINs it hasn't pulled get a Keepa
    refresh, in priority order (price drops / promo rows first, then ROI closest to or above 15%) within --max-tokens.
 4. ROI prefilter >= 15%: cost = Sephora price x pack (no store uplift, Nick Nov 23 2026) (markdown only for the size it belongs to; a broad promo
    takes list x (1 - discount) when lower; Nick's size_confirmed wins; needs_size rows only at size_guess=hi unless
    confirmed); revenue = Buy Box else lowest FBA; minus referral, FBA fee, $1 prep. SP-API getItemOffersBatch re-checks
    the Buy Box / FBA low of the rows that pass.
 5. Lead Score (Hub proxy, <= 25 per call, sequential) on every row that passed the prefilter; no daily cap (Nick,
    Nov 23, 2026; --max-score N sets one for a test run); candidates = pred ROI >= 10% (or ROI >= 10% marked 'not scored' when scoring fails).
 6. Upsert refreshed catalog fields, the candidates and the run record; write daily_summary.md.
--dry-run: no Hub writes (payloads are saved under $SEPH_DATA/out/<date>/); Lead Score is skipped unless --score.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import shade  # noqa: E402

CONF_OK = ("high", "medium")
OOS_DAYS = 10   # an item list older than this doesn't hold a pair back as out of stock
COLLECTOR_HOURS = 36       # pulls this fresh price anything
COLLECTOR_MAX_HOURS = 192  # the collector's slower tiers (Nick, Nov 24, 2026): 3-day and weekly listings far from profit
# To review filter (Nick, Nov 24, 2026): a listing under RANK_RULE_ROI needs a 30-day average sales rank under RANK30_MAX;
# no rank in 30 days fails only when Keepa has no monthly sales estimate either. RANK_RULE_ROI and up passes on ROI alone
RANK_RULE_ROI, RANK30_MAX = 0.50, 300_000   # a Keepa collector pull younger than this prices the row (Nick, Nov 23, 2026)
SIZE_TOL, SIZE_TOL_ONE = 0.25, 0.35   # hard size check (Nick, Nov 24, 2026)
HEALTH_COVERAGE, HEALTH_DONE_BY = 0.95, 7   # alert below 95% priced, or a morning run still going at 7 am Central
from zoneinfo import ZoneInfo  # noqa: E402
CT = ZoneInfo("America/Chicago")
CAND_MIN, PREFILTER = 0.10, 0.15   # posting floor: predicted ROI 10% (Nick, Nov 19, 2026; was 21%)
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
    ap.add_argument("--post-only", action="store_true", help="no new Lead Score calls: post the rows already scored today")
    ap.add_argument("--rescore", action="store_true", help="ignore today's Lead Scores and score again (after a scoring change)")
    ap.add_argument("--max-score", type=int, default=0)   # 0 = no cap (Nick, Nov 23, 2026)
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
    # Sephora item lists (store-skus, read in Nick's Chrome): {product_id: {sku_id: price}} for products sold in sizes
    sizes, stock, item_sizes = {}, {}, {}   # item_sizes: {product_id: {sku_id: item}} for the hard size check; stock: {product_id: (read date, {sku_id: out_of_stock})}
    try:
        for row in C.hub("GET", "/api/engine/store-skus", timeout=300)["rows"]:
            if row.get("store") != "Sephora":
                continue
            stock[row["product_id"]] = (str(row.get("read_at") or "")[:10], {str(x["sku_id"]): bool(x.get("oos")) for x in row.get("skus") or []})
            item_sizes[row["product_id"]] = {str(x["sku_id"]): x for x in row.get("skus") or []}
            if "size" in str(row.get("variation_type") or "").lower():
                sizes[row["product_id"]] = {str(x["sku_id"]): C.fnum(x.get("price")) for x in row.get("skus") or []
                                            if C.fnum(x.get("price"))}
    except Exception as e:  # noqa: BLE001
        problems.append(f"store item lists unavailable (multi-size sale prices not checked): {e!r}")
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

    # Every banner on Sephora's deal pages, for the Claude review in the daily task, and the deals the job can act on
    # (Nick, Nov 24, 2026: "make sure we don't miss anything", e.g. "Buy 2 minis, get both 50% off").
    try:
        banners = C.promo_banners()
    except Exception as e:  # noqa: BLE001
        banners = []
        problems.append(f"deal banners not read: {e!r}"[:200])
    deals = C.parse_deals(banners, brands, day)
    minis = next((d_ for d_ in deals if d_["kind"] == "multibuy" and d_["what"] == "minis"), None)
    C.log(f"deal banners: {len(banners)}; deals: {[(d_['kind'], d_.get('brand') or d_.get('what'), d_['pct']) for d_ in deals]}")
    with open(os.path.join(C.DATA, "promo_banners.md"), "w") as f_:
        f_.write(f"# Sephora deal banners, {day.isoformat()}\n\nEvery promo-looking text on Sephora's deal pages today. "
                 "Review them against the run summary's Deals section.\n\n")
        for pg in dict.fromkeys(b_["page"] for b_ in banners):
            f_.write(f"## {pg}\n" + "".join(f"- {b_['text']}\n" for b_ in banners if b_["page"] == pg) + "\n")
    if not a.dry_run and banners:
        try:
            C.hub("PUT", f"/api/engine/docs/sephora_jobs/promo_banners/{day.isoformat()}", {"banners": banners, "deals": deals})
        except Exception as e:  # noqa: BLE001
            problems.append(f"banner doc not saved: {e!r}"[:200])

    # Deals the website shows but doesn't price (Nick, Nov 24, 2026: app-only and checkout discounts "you can see them on
    # the website and then apply the discount manually but then i purchase in the app"). Hub doc sephora_jobs/manual_deals
    # = {"deals": [{"pct": 0.5, "brand": "..."} or {"pct": .., "product_ids": ["P123", ...]}, "ends": "YYYY-MM-DD",
    # "note": "Black Friday: half off in the app"}]}, written by Claude from the deal pages or from Nick's message.
    manual = []
    try:
        for x in (C.hub("GET", "/api/engine/docs/sephora_jobs/manual_deals").get("data") or {}).get("deals") or []:
            if C.fnum(x.get("pct")) and str(x.get("ends") or "9999") >= day.isoformat():
                manual.append(x)
    except Exception as e:  # noqa: BLE001
        if "404" not in repr(e):
            problems.append(f"manual deals unreadable: {e!r}"[:200])
    counts["manual_deals"] = len(manual)

    def promo_for(r):
        """The broad promo for this pair: its brand's code discount (or the site-wide event), a manual app/checkout deal
        that names its product or brand, and for a mini, a live 'Buy 2 minis, get both N% off' (Nick always buys 2+
        units). The biggest discount is kept; the lower of it and the sale price wins in pick_price."""
        d = disc.get(r["brand"]) or disc.get("*")
        best = d if d and d[0] > 0 else None
        if minis and C.is_mini(r) and (best is None or minis["pct"] > best[0]):
            best = (minis["pct"], f"Buy {minis['buy']} minis, get {minis['pct'] * 100:.0f}% off — confirm this mini qualifies")
        for x in manual:
            hit = r["product_id"] in (x.get("product_ids") or []) or (x.get("brand") and x["brand"].lower() == (r.get("brand") or "").lower())
            if hit and (best is None or float(x["pct"]) > best[0]):
                best = (float(x["pct"]), f"{x.get('note') or 'App / checkout deal'}: {float(x['pct']) * 100:.0f}% off — buy in the Sephora app, confirm at checkout")
        return best

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
                               "why": ", ".join(why), "old_eff": old_sale or old_hi,
                               # the Sale page's own list and sale prices: one price, or a range across the product's
                               # sizes ("$33.00 - $96.00" on sale "$24.75 - $72.00")
                               "plo": p.get("price_lo"), "phi": p.get("price_hi"), "slo": sale_p,
                               "shi": p.get("sale_hi") or sale_p}
            sz = sizes.get(pid)
            if sz and len(sz) > 1:
                # The Sale page shows ONE item per product (the one on sale, with its skuId). For a product sold in several
                # sizes its list/sale price is that item's, not the product's range (Nov 23, 2026: a 20 mL mini's $10.20
                # was priced against the 50 mL Ultra Violette listing). Keep the range from the item list.
                seph_today[pid]["lo"], seph_today[pid]["hi"] = min(sz.values()), max(sz.values())
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

    def sale_for(t, price):
        """The Sale page's sale price for the item whose list price is `price`, or None. The page gives one list price or
        a range across sizes, and the sale price the same way: the low end belongs to the cheapest size and the high end
        to the dearest. An item that isn't one of those ends gets no markdown (Nov 23, 2026: the 10 mL's $24.75 was
        applied to the $96 50 mL)."""
        if not (t and t.get("slo") and price):
            return None
        near = lambda a, b: a is not None and b is not None and abs(a - b) < 0.015
        if near(price, t.get("phi")):
            sale = t.get("shi")
        elif near(price, t.get("plo")):
            sale = t.get("slo")
        else:
            return None
        return sale if sale and sale < price - 0.009 else None

    def seph(r):
        t = seph_today.get(r["product_id"])
        sp = C.fnum(r.get("store_size_price"))
        if sp:   # the store size this ASIN matches was read from the product page (Nick, Nov 17, 2026): price that size only
            return sp, sp, sale_for(t, sp)
        if t:
            sz = sizes.get(r["product_id"])
            if t.get("sale") and sz and len(sz) > 1:
                # several sizes, the pair's size not known: priced at the full size (the buying rule for unknown sizes),
                # with the full size's own markdown only
                return t["lo"], t["hi"], sale_for(t, t["hi"])
            return t["lo"], t["hi"], t["sale"]
        return C.fnum(r.get("price_lo")), C.fnum(r.get("price_hi")), (C.fnum(r.get("sale_price")) if r.get("on_sale") else None)

    def out_of_stock(r):
        """Read date when the pair's own Sephora item (its store size, else the product's only item) was out of stock on
        an item list read in the last OOS_DAYS days, else None (in stock, unknown, or too old to trust)."""
        rd, items = stock.get(r["product_id"]) or (None, {})
        if not rd or not items or rd < (day - dt.timedelta(days=OOS_DAYS)).isoformat():
            return None
        sku = str(r.get("store_sku_id") or "") or (next(iter(items)) if len(items) == 1 else "")
        return rd if sku and items.get(sku) is True else None

    # ---- 3. watch rows
    try:   # rules learned from Nick's wrong-match marks: a pair that breaks one isn't proposed until he marks it OK
        rules = C.learned_rules()
    except Exception as e:  # noqa: BLE001
        rules = None
        problems.append(f"match rules unavailable (wrong-match feedback read failed): {e!r}")
    rule_skipped = []

    def watchable(r):
        # Nick, Nov 19, 2026: every pair not marked wrong is watched. The match gate (learned rules, confidence, product
        # still on Sephora's brand listing) was removed; Nick judges the match on the To review tab.
        if not r["active"] or r.get("sellable") != "open" or r.get("match_ok") is False:
            return False
        ex = r.get("extra") or {}
        if ex.get("listing_dead"):   # Amazon has no detail page for the ASIN any more (dog page; listing_check.py)
            return False
        if (ex.get("shade_hint") or {}).get("not_sold") and not r.get("store_size") and r.get("match_ok") is not True:
            return False   # resolve_skus.py: the Amazon shade isn't on Sephora's shade list (Nov 19, 2026)
        if (ex.get("shade_hint") or {}).get("bundle") and not ex.get("bundle_skus") and int(C.fnum(ex.get("pack_qty")) or 1) < 2:
            return False   # a set of Sephora singles priced as one single: the cost would be wrong (checker round 3)
        # Demand and thin-price gates removed (Nick, Nov 23, 2026, "option 2"): low-demand and no-Buy-Box / one-seller
        # listings are posted too, flagged "No competition / restock" (no Buy Box can mean no competition, and sales
        # can come back when restocked). Lead Score and Nick judge them on To review.
        return True

    # Every watchable pair is priced, not only one per ASIN (Nov 19, 2026): an ASIN with two Sephora pairs (Nick's lead and
    # the matcher's pick, a mini and a full size, ...) is posted from the most trusted pair that is profitable. Before,
    # the job kept one pair per ASIN by confidence, so a profitable lead pair could sit behind a set or a wrong size.
    rank = {"high": 0, "medium": 1, "low": 2}

    def trust(r):
        return (r.get("match_ok") is not True, not str(r.get("match_reason") or "").startswith("Nick lead"),
                rank.get(r.get("confidence"), 3), -(C.fnum((r.get("extra") or {}).get("score")) or 0))

    watch = [r for r in active if watchable(r)]
    pri = []
    for r in watch:
        lo, hi, sale_p = seph(r)
        e = C.economics(r, lo, hi, sale_p, amz_of(r), "buy", promo_for(r))
        if not e:
            continue
        top = max([x for x in (e["roi_now"], e["roi_avg90"]) if x is not None], default=None)
        hot = bool((seph_today.get(r["product_id"]) or {}).get("dropped")) or e["sale_applied"] or e["promo_applied"]
        ex_ = r.get("extra") or {}
        restock = not (ex_.get("demand_ok") is True and ex_.get("thin_price") is not True)
        # Keepa budget: sale/promo rows, then listings with demand and a real price, then the restock / low-demand ones
        pri.append((0 if hot else 1, (1 if restock else 0, -(top if top is not None else -9)), r))
    # open candidates on To review are re-priced every day (Nick, Nov 17, 2026): their pairs go first in the Keepa queue,
    # even when they aren't watchable today (rule-held, thin price...), so a lead whose sale ended or whose Buy Box fell expires
    try:
        open_cands = C.hub("GET", "/api/engine/sephora/candidates", params={"status": "new"}).get("candidates") or []
    except Exception as e:  # noqa: BLE001
        open_cands = []
        problems.append(f"open candidates read failed (no recheck today): {e!r}")
    in_pri = {(r["asin"], r["product_id"]) for _, _, r in pri}
    by_key = {(r["asin"], r["product_id"]): r for r in active}
    for oc in open_cands:
        k_ = (oc["asin"], oc["product_id"])
        if k_ in by_key and k_ not in in_pri:
            pri.append((-1, (0, 0), by_key[k_])); in_pri.add(k_)
    pri = [(-1 if (r["asin"], r["product_id"]) in {(o["asin"], o["product_id"]) for o in open_cands} else t, v, r) for t, v, r in pri]
    pri.sort(key=lambda x: (x[0], x[1]))
    counts["watch_rows"] = len({r["asin"] for r in watch})
    counts["watch_pairs"] = len(watch)
    counts["watch_priceable"] = len(pri)
    C.log(f"watch rows: {counts['watch_rows']} ASINs, {len(watch)} pairs ({len(pri)} priceable by the buying rule)")

    # Keepa (cache: today's refreshes are reused by a re-run). The Keepa collector pulls every watched ASIN within ~24 h,
    # so its latest pull (<= COLLECTOR_HOURS old) prices the row; the job's own tokens only go to ASINs the collector
    # hasn't pulled recently (Nick, Nov 23, 2026).
    kcache = C.load_json(f"state/keepa_{day.isoformat()}.json", {}) or {}
    order = list(dict.fromkeys(r["asin"] for _, _, r in pri))   # Keepa is per ASIN: each once, in priority order
    from_collector = 0
    need = [x for x in order if x not in kcache]
    must_be_fresh = {o["asin"] for o in open_cands} | {r["asin"] for r in active if (seph_today.get(r["product_id"]) or {}).get("dropped")}
    try:
        import keepa_pull as kp
        for i in range(0, len(need), 50):   # small calls: 300 at once ran the Hub out of memory (Nov 24, 2026)
            got_ = C.hub("POST", "/api/engine/keepa/latest", {"asins": need[i:i + 50], "max_age_hours": COLLECTOR_MAX_HOURS},
                         timeout=300).get("products") or {}
            for k, p_ in got_.items():
                try:
                    ts_ = dt.datetime.fromisoformat(p_["pulled_at"]).timestamp()
                except Exception:  # noqa: BLE001
                    ts_ = time.time()
                # a pull older than COLLECTOR_HOURS only prices a listing whose Sephora side didn't change today and that
                # isn't on To review: a new markdown or an open lead gets a fresh pull from this job's own tokens
                if time.time() - ts_ > COLLECTOR_HOURS * 3600 and k in must_be_fresh:
                    continue
                kcache[k] = {**kp.compact(p_), "_ts": ts_, "_src": "collector"}
                from_collector += 1
        C.save_json(f"state/keepa_{day.isoformat()}.json", kcache)
    except Exception as e:  # noqa: BLE001
        problems.append(f"Keepa collector read failed (own Keepa pulls only): {e!r}"[:300])
    C.log(f"Keepa collector: {from_collector} of {len(need)} ASINs priced from its pulls ({len(must_be_fresh)} needed one under {COLLECTOR_HOURS} h)")
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
    from_collector = sum(1 for x in refreshed if kcache[x].get("_src") == "collector")
    C.log(f"Keepa: {len(refreshed)} of {len(order)} watch ASINs fresh ({from_collector} from the collector, "
          f"{len(refreshed) - from_collector} pulled by this job: {tokens} tokens; {kstop or 'all done'})")

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
        meta[r["asin"]] = {"monthly_sold": ms, "sales_rank": rk, "offers_fba": of, "amazon_on": aon, "keepa": bool(k),
                           "rank_avg30": (k or {}).get("rank_avg30") if k else (r.get("extra") or {}).get("rank_avg30")}

    # ---- 4. prefilter + SP-API
    def evaluate(r):
        lo, hi, sale_p = seph(r)
        e = C.economics(r, lo, hi, sale_p, amz[r["asin"]], "buy", promo_for(r))
        m = meta[r["asin"]]
        d, thin = flags_of(amz[r["asin"]], m["monthly_sold"], m["sales_rank"], m["offers_fba"])
        return e, d, thin

    def qual_roi(e):
        """ROI that qualifies a pair: today's (Buy Box, else lowest FBA offer); with no offer at all (out of stock on
        Amazon, the restock case) the 90-day average Buy Box. None = not priceable."""
        if not e:
            return None
        return e["roi_now"] if e["roi_now"] is not None else e["roi_avg90"]

    pre = []
    for _, _, r in pri:
        e, d, thin = evaluate(r)
        q = qual_roi(e)
        if q is not None and q >= PREFILTER:
            pre.append(r)
    C.log(f"prefilter ROI >= 15%: {len(pre)} rows; SP-API offers check")
    sp_live = {}
    if pre:
        try:
            import sp_offers
            sp_live = sp_offers.offers(list(dict.fromkeys(r["asin"] for r in pre)))
        except Exception as e:  # noqa: BLE001
            problems.append(f"SP-API offers failed (Keepa prices used): {e!r}")
    passed = []
    dead_today = []
    for r in pre:
        lv = sp_live.get(r["asin"]) or {}
        if lv.get("invalid"):   # SP-API: "invalid ASIN" = the listing is gone (dog page), Nick Nov 19, 2026
            dead_today.append(r)
            continue
        if lv and lv.get("status") in (None, 200):
            if lv.get("buybox"):
                amz[r["asin"]]["buybox"] = lv["buybox"]
            if lv.get("fba_low"):
                amz[r["asin"]]["fba_low"] = lv["fba_low"]
            meta[r["asin"]]["offers_fba"] = lv.get("offers_fba", meta[r["asin"]]["offers_fba"])
            meta[r["asin"]]["amazon_on"] = bool(meta[r["asin"]]["amazon_on"] or lv.get("amazon_offer"))
            meta[r["asin"]]["sp"] = True
        e, d, thin = evaluate(r)
        q = qual_roi(e)
        if q is not None and q >= PREFILTER:
            passed.append((q, r, e))
    # one pair per ASIN from here on (no duplicate candidates): an open candidate's pair first (so To review keeps one row),
    # then the most trusted pair (marked OK, Nick's lead, confidence, match score), then the best ROI
    open_keys = {(o["asin"], o["product_id"]) for o in open_cands}
    pick = {}
    for roi_, r, e in passed:
        k_ = ((r["asin"], r["product_id"]) not in open_keys, trust(r), -roi_)
        if r["asin"] not in pick or k_ < pick[r["asin"]][0]:
            pick[r["asin"]] = (k_, (roi_, r, e))
    n_pairs = len(passed)
    passed = sorted((v for _, v in pick.values()), key=lambda x: -x[0])
    C.log(f"after SP-API: {len(passed)} ASINs >= 15% ({n_pairs} pairs)")

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
    try:   # leads rejected as not in stock (Nick, Nov 17, 2026): held until the Chrome stock check finds them back in stock
        for w_ in C.hub("GET", "/api/engine/sephora/stock-watch", params={"status": "all"}).get("rows") or []:
            if w_["status"] == "watching":
                decided[w_["asin"]] = ("rejected: not in stock at Sephora", None)   # no time limit, any price
            elif w_["status"] == "restocked" and C.after_ts(w_.get("restocked_at") or "", w_.get("rejected_at") or ""):
                if decided.get(w_["asin"], ("",))[0] == "rejected":
                    decided.pop(w_["asin"], None)   # back in stock: may be proposed again
    except Exception as e:  # noqa: BLE001
        problems.append(f"stock watch read failed: {e!r}")
    pool, skipped_decided, size_check, held_oos, held_rank = [], [], [], [], []
    held_check, held_wrong = [], []
    # Match-check gate (Nick, Nov 24, 2026): nothing reaches To review until its pair has been checked. Checked = Nick
    # marked it OK, or the check passed it (extra.screened / extra.verified), or its research verdict waiting for Nick
    # says the same product. A research verdict of another product / not sold holds it (Nick decides those on the
    # for-Nick list); an unchecked pair goes on the check queue (Hub doc sephora_jobs/check_queue) and is posted after
    # the daily task has checked it.
    fn_ = C.state_get("verify_for_nick", {}) or {}
    research = {(x["asin"], x["product_id"]): x for x in ((fn_.get("items") if isinstance(fn_, dict) else fn_) or [])}

    def check_state(r):
        ex_ = r.get("extra") or {}
        if r.get("match_ok") is True or ex_.get("verified") or ex_.get("screened"):
            return "ok", None
        v_ = research.get((r["asin"], r["product_id"]))
        if v_:
            if v_.get("verdict") in ("other", "not_sold"):
                return "wrong", v_
            return "ok", v_    # same / cant_tell: posted; the note carries the research's doubt
        return "unchecked", None

    def rank_fails(roi, asin):
        """Nick's rule (Nov 24, 2026): under 50% ROI needs a 30-day average rank under 300k. Returns the reason, or None."""
        if roi is not None and roi >= RANK_RULE_ROI:
            return None
        rk30 = (meta.get(asin) or {}).get("rank_avg30")
        if rk30 is None:
            # no rank fails only without a sales estimate too (Nick, Nov 24, 2026)
            if ((meta.get(asin) or {}).get("monthly_sold") or 0) > 0:
                return None
            return "no sales rank in the last 30 days and no monthly sales estimate"
        if rk30 >= RANK30_MAX:
            return f"30-day average rank {rk30:,} (needs under {RANK30_MAX:,} below {RANK_RULE_ROI * 100:.0f}% ROI)"
        return None
    def size_fails(r):
        """Hard size check (Nick, Nov 24, 2026: size was 10 of the 15 wrong matches he rejected Nov 17-24). The size in the
        Amazon title must be one Sephora sells: the pair's own item when its store size is known, else any single item of
        the product. Nearest Sephora size within 25% (label rounding: MAC 0.1 vs 0.12 oz), 35% for a one-size product
        (markets and versions differ: MAC Studio Fix 15 g vs 12 g, MUFE Artist Blush 4 g vs 5 g). Skipped when Nick
        marked the pair OK, when either side is a set or has no size, and for sizes under 0.05 oz. Keepa's size field is
        not used (too often another listing's). Tested on Nov 24 against Nick's decisions: 10 of his 14 size rejects
        caught (the other 4 have no size in the title), none of his 33 OK pairs or 7 approvals held. Returns the reason."""
        if r.get("match_ok") is True:
            return None
        a = C.distinct_sizes(shade.oz_amounts(r.get("amazon_title") or ""))
        if len(a) != 1:
            return None
        it = item_sizes.get(r["product_id"]) or {}
        amounts = {k: C.distinct_sizes(shade.oz_amounts(str(x.get("size") or "")) or shade.oz_amounts(str(x.get("value") or "")))
                   for k, x in it.items()}
        own = amounts.get(str(r.get("store_sku_id") or ""))
        if own is not None:
            if len(own) != 1:
                return None
            sz, which = own, "its item"
        else:
            sz, which = sorted({v[0] for v in amounts.values() if len(v) == 1}), "the product's items"
            if not sz:
                sz, which = C.distinct_sizes(shade.oz_amounts(r.get("store_size") or "")), "its store size"
                if len(sz) != 1:
                    return None
        if a[0] < 0.05 or max(sz) < 0.05:
            return None
        every = C.distinct_sizes([v for vs in amounts.values() for v in vs]) if amounts else sz
        tol = SIZE_TOL_ONE if len(every) <= 1 else SIZE_TOL
        if any(abs(a[0] - y) / max(a[0], y) <= tol for y in sz):
            return None
        return f"Amazon title says {a[0]:.2f} oz; Sephora sells {', '.join(f'{y:.2f}' for y in sz)} oz ({which})"

    for roi_, r, e in passed:
        dd = decided.get(r["asin"])
        if dd and (dd[1] is None or e["entered"] >= dd[1] - 0.01):
            skipped_decided.append((r["asin"], dd[0]))
            continue
        od = out_of_stock(r)
        if od:   # Nick, Nov 23, 2026: out-of-stock items aren't posted; the Chrome stock check re-reads them daily
            held_oos.append((roi_, r, od))
            continue
        rf = rank_fails(roi_, r["asin"])
        if rf:
            held_rank.append((roi_, r, rf))
            continue
        sf = size_fails(r)
        if sf:
            size_check.append((roi_, r, {**e, "size_why": sf}))
            continue
        cs, v_ = check_state(r)
        if cs == "wrong":
            held_wrong.append((roi_, r, v_))
            continue
        if cs == "unchecked":
            held_check.append((roi_, r))
            continue
        pool.append((roi_, r, e))
    counts["held_out_of_stock"] = len(held_oos)
    counts["held_rank"] = len(held_rank)
    counts["held_unchecked"], counts["held_check_says_wrong"] = len(held_check), len(held_wrong)
    C.log(f"held for the match check: {len(held_check)} unchecked, {len(held_wrong)} the check says are another product / not sold")
    if not a.dry_run:
        try:
            C.hub("PUT", "/api/engine/docs/sephora_jobs/check_queue",
                  {"items": [f"{r['asin']}|{r['product_id']}" for _, r in sorted(held_check, key=lambda x: -(x[0] or 0))],
                   "at": day.isoformat(), "why": "profitable pairs held from To review until the match check runs"})
        except Exception as e:  # noqa: BLE001
            problems.append(f"check queue not saved: {e!r}"[:200])
    C.log(f"held back by the rank rule (under {RANK_RULE_ROI * 100:.0f}% ROI, 30-day avg rank {RANK30_MAX:,}+ or none): {len(held_rank)}")
    C.log(f"held back as out of stock at Sephora: {len(held_oos)}")

    # Live restriction check (Nick, Nov 24, 2026): the monthly check can be weeks old and a listing can turn gated in between
    # (6 of 20 rejections in two weeks were "Can't sell / gated"). Every listing about to be posted, and every open lead on
    # To review, is asked again right before posting (Amazon Listing Restrictions, <= 4 a second, cached for the day).
    # Restricted -> its pairs leave the Lead DB (the Hub parks them; the monthly check puts them back if they open up);
    # approval required -> held, and its pairs' sellable is updated so the job stops watching them until approved.
    held_gated, gated = [], {}
    live_path = os.path.join(outdir, "live_restrictions.json")
    live = json.load(open(live_path)) if os.path.exists(live_path) else {}
    ask = list(dict.fromkeys([r["asin"] for _, r, _ in pool] + [o["asin"] for o in open_cands]))
    todo_ = [x for x in ask if (live.get(x) or {}).get("status") in (None, "error")]
    if todo_:
        try:
            import sp_restrictions as spr
            C.log(f"live restriction check: {len(todo_)} listings")
            for x in todo_:
                live[x] = spr.check(x)
            json.dump(live, open(live_path, "w"))
        except Exception as e:  # noqa: BLE001  (a failed check never blocks the run; the monthly status stands)
            problems.append(f"live restriction check failed (monthly status used): {e!r}"[:300])
    for x in ask:
        st_ = (live.get(x) or {}).get("status")
        if st_ in ("restricted", "approval_required"):
            gated[x] = st_
    if gated:
        keep = []
        for roi_, r, e in pool:
            if r["asin"] in gated:
                held_gated.append((roi_, r, gated[r["asin"]]))
            else:
                keep.append((roi_, r, e))
        pool = keep
    counts["held_gated"] = len(held_gated)
    counts["gated_today"] = dict(collections.Counter(gated.values()))
    C.log(f"held as gated on Amazon today: {len(held_gated)} ({counts['gated_today']})")

    # ---- 5. Lead Score
    # Listings with demand and a Buy Box are scored first, then the "No competition / restock" ones (their ROI is often
    # inflated by a lone seller's price). No daily cap (Nick, Nov 23, 2026); --max-score N only for test runs.
    def _restock(r):
        m_ = meta[r["asin"]]
        d_, thin_ = flags_of(amz[r["asin"]], m_.get("monthly_sold"), m_.get("sales_rank"), m_.get("offers_fba"))
        return bool(thin_ or not d_ or not amz[r["asin"]].get("buybox"))
    pool.sort(key=lambda x: (_restock(x[1]), -x[0]))
    top = pool[:a.max_score] if a.max_score else pool
    scache = {} if a.rescore else (C.load_json(f"state/scores_{day.isoformat()}.json", {}) or {})
    try:   # scores already on today's candidates (e.g. an earlier run in another session)
        ex = {} if a.rescore else C.hub("GET", "/api/engine/sephora/candidates", params={"since": day.isoformat(), "status": "all"})
        for c_ in ex.get("candidates") or []:
            if c_.get("found_on") == day.isoformat() and c_.get("lead_score_band") is not None:
                key = f"{c_['asin']}|{C.fnum(c_.get('cost')):.2f}"
                scache.setdefault(key, {"ok": True, "band": c_["lead_score_band"], "pred_roi": C.fnum(c_.get("lead_score_pred_roi")),
                                        "notes": c_.get("lead_score_notes"), "from": "hub"})
    except Exception as e:  # noqa: BLE001
        problems.append(f"couldn't read today's candidates: {e!r}")
    scored_today = sum(1 for v in scache.values() if v.get("from") != "hub")
    want = [(r, e) for _, r, e in top if f"{r['asin']}|{e['entered']:.2f}" not in scache]
    room = max(0, a.max_score - scored_today) if a.max_score else len(want)
    if a.post_only:   # e.g. while another run is still scoring (never two scorers at once)
        room = 0
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
        problems.append(f"Lead Score: {len(want) - room} rows not scored yet" + (" (--post-only)" if a.post_only else " (--max-score cap)"))

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
        A_ = amz[r["asin"]]
        rev_txt = (f"Buy Box ${A_['buybox']:.2f}" if A_.get("buybox") else
                   f"no Buy Box, lowest FBA ${A_['fba_low']:.2f}" if A_.get("fba_low") else
                   f"no offers now, 90d avg Buy Box ${(C.fnum(A_.get('bb_avg90')) or 0):.2f}")
        bits = [price_txt, rev_txt, f"ROI {C.pct(roi_)}", f"90d avg ROI {C.pct(e['roi_avg90'])}"]
        d_, thin_ = flags_of(A_, m.get("monthly_sold"), m.get("sales_rank"), m.get("offers_fba"))
        if thin_ or not A_.get("buybox") or not d_:
            why_ = []
            if not A_.get("buybox"):
                why_.append("no Buy Box now")
            if (m.get("offers_fba") or 0) <= 1:
                why_.append(f"{m.get('offers_fba') or 0} FBA seller" + ("" if (m.get("offers_fba") or 0) == 1 else "s"))
            if not d_:
                why_.append(f"low demand ({m.get('monthly_sold') or 0} sold/mo, rank {m.get('sales_rank') or '-'})")
            bits.insert(0, "No competition / restock: " + ", ".join(why_) + " — check sales history and the price others sell at")
        if m.get("monthly_sold"):
            bits.append(f"~{m['monthly_sold']} sold/mo")
        if st.get("why"):
            bits.append(st["why"])
        if e["promo_applied"]:
            bits.append(promo_for(r)[1])
        if m.get("amazon_on"):
            bits.append("Amazon sells it")
        asz = (r.get("extra") or {}).get("amazon_size")
        if asz:
            bits.append(f"Amazon size {asz}")
        if r.get("store_size"):
            bits.append(f"buy the {r['store_size']} (${C.fnum(r.get('store_size_price')) or 0:.2f} list)")
        elif r.get("needs_size") and r.get("size_confirmed") in ("lo", "hi"):
            bits.append("buy the " + ("mini/smaller size" if "lo" == r.get("size_confirmed") else "full size") + " (Nick confirmed)")
        elif r.get("needs_size"):   # no size hold any more (Nick, Nov 19, 2026): posted, flagged
            bits.insert(0, "Size not checked: confirm which Sephora size the Amazon listing is")
        sh_ = (r.get("extra") or {}).get("shade_hint")
        if not r.get("store_size") and sh_ and sh_.get("guess"):   # resolve_skus.py couldn't tell the shade
            bits.insert(0, ("Close shade, check it: Amazon's shade is near " if sh_.get("close") else "Shade not confirmed: looks like ")
                        + " or ".join(sh_["guess"][:3]))
        elif sh_ and sh_.get("bundle"):   # a set of Sephora singles, priced as one of each (Nick, Nov 20, 2026)
            bits.insert(0, "Bundle: buy one each of " + " + ".join(sh_["bundle"][:4]) + " for one Amazon unit")
        elif sh_ and sh_.get("two_shades"):   # the listing names two Sephora shades: both kept (Nick, Nov 19, 2026)
            bits.insert(0, "Two shades named: " + " or ".join(sh_["two_shades"][:2]) + " (check the photo)")
        if sc_note:
            bits.append(sc_note)
        if rules and r.get("match_ok") is not True:   # learned match rules no longer hold a pair back, they flag it (Nov 19, 2026)
            br = C.rule_breaks(rules, r)
            if br:
                bits.insert(0, f"Check the match: {br[0]}")
                rule_skipped.append((r["asin"], r.get("sephora_name"), br[0]))
        v_ = research.get((r["asin"], r["product_id"]))
        if v_ and r.get("match_ok") is not True and not (r.get("extra") or {}).get("verified"):
            if v_.get("verdict") == "cant_tell":
                bits.insert(0, "Match check couldn't tell: " + str(v_.get("lesson") or v_.get("evidence") or "")[:140])
            elif v_.get("confidence") != "high":
                bits.insert(0, f"Match check: same product ({v_.get('confidence') or '?'} confidence)")
        if e["promo_applied"]:   # cost from a site-wide / brand event code, not the item's own sale price
            bits.insert(0, "Promo price — confirm it applies at checkout")
        row = {"asin": r["asin"], "product_id": r["product_id"], "found_on": day.isoformat(),
               "reason": "sale" if hot else "price_watch", "cost": round(e["entered"], 2), "price_basis": e["label"][:200],
               "buybox": amz[r["asin"]]["buybox"] or amz[r["asin"]]["fba_low"], "roi_now": roi_,
               "lead_score_band": band, "lead_score_pred_roi": round(pred, 4) if pred is not None else None,
               "lead_score_notes": notes, "note": ", ".join(bits)[:1000]}
        cands.append(row)
        cand_view.append({**row, "brand": r["brand"], "sephora_name": r["sephora_name"], "amazon_title": r.get("amazon_title"),
                          "shade": r.get("shade"), "confidence": r.get("confidence")})
    C.log(f"candidates: {len(cands)} (pred ROI >= {C.pct(CAND_MIN)}{' or unscored ROI >= ' + C.pct(CAND_MIN) if any(c['lead_score_band'] is None for c in cands) else ''})")

    # ---- 6. catalog upserts
    upd = {}

    def put(r, **kw):
        k = (r["asin"], r["product_id"])
        upd.setdefault(k, {"asin": r["asin"], "product_id": r["product_id"], "active": r["active"]}).update(kw)

    for pid, t in seph_today.items():
        for r in by_pid.get(pid, []):
            if r.get("match_ok") is False:   # wrong matches are kept for review only, never updated (Nick, Nov 19, 2026)
                continue
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
    for r in dead_today:
        put(r, extra={**(r.get("extra") or {}), **(upd.get((r["asin"], r["product_id"]), {}).get("extra") or {}),
                      "listing_dead": day.isoformat()})
    counts["listings_dead"] = len({r["asin"] for r in dead_today})
    for r in active:   # today's live restriction answers (a restricted pair is parked by the Hub when this upsert lands)
        if r["asin"] in gated and r.get("sellable") != gated[r["asin"]]:
            put(r, sellable=gated[r["asin"]])
    cat_rows = list(upd.values())

    # ---- 5b. re-price open candidates (Nick, Nov 17, 2026); the ones posted again today were replaced by today's row
    posted_today = {c_["asin"] for c_ in cands}
    recheck, rechecked_expired = [], []
    for oc in open_cands:
        r = by_key.get((oc["asin"], oc["product_id"]))
        if not r or oc["asin"] in posted_today:
            continue
        if oc["asin"] in gated:   # live restriction check today (an approval-required lead; a restricted one is parked)
            why = (f"Gated on Amazon on {day:%b %-d}: "
                   + ("restricted for your account" if gated[oc["asin"]] == "restricted" else "approval required in Seller Central"))
            recheck.append({"id": oc["id"], "expire": True, "why": why})
            rechecked_expired.append((r, why))
            continue
        if oc["asin"] not in amz:
            continue
        od = out_of_stock(r)
        if od:
            why = f"Out of stock at Sephora (item list read {od}); comes back when the daily stock check finds it in stock"
            recheck.append({"id": oc["id"], "expire": True, "why": why})
            rechecked_expired.append((r, why))
            held_oos.append((C.fnum(oc.get("roi_now")) or 0, r, od))
            continue
        e, d, thin = evaluate(r)
        if not e or e["roi_now"] is None:
            continue
        bb = amz[r["asin"]]["buybox"] or amz[r["asin"]]["fba_low"]
        cs, v_ = check_state(r)
        if cs != "ok":
            why = (f"Match check says {'another product' if v_ and v_.get('verdict') == 'other' else 'not sold at Sephora'}: "
                   f"{str((v_ or {}).get('lesson') or (v_ or {}).get('evidence') or '')[:200]} (decide it on the for-Nick list)"
                   if cs == "wrong" else "Waiting on the match check; posted again once the pair has been checked")
            recheck.append({"id": oc["id"], "expire": True, "why": why})
            rechecked_expired.append((r, why))
            continue
        sf = size_fails(r)
        if sf:
            why = f"Size check on {day:%b %-d}: {sf}; mark the pair OK in the Lead DB if the size is right"
            recheck.append({"id": oc["id"], "expire": True, "why": why})
            rechecked_expired.append((r, why))
            continue
        rf = rank_fails(e["roi_now"], r["asin"])
        if rf and e["roi_now"] >= PREFILTER:
            why = f"Rank rule on {day:%b %-d}: ROI {e['roi_now'] * 100:.0f}% with {rf}"
            recheck.append({"id": oc["id"], "expire": True, "why": why})
            rechecked_expired.append((r, why))
            continue
        if e["roi_now"] < PREFILTER:
            why = (f"No longer profitable on {day:%b %-d}: cost ${e['entered']:.2f} ({e['label']}), Buy Box ${bb or 0:.2f}, "
                   f"ROI {e['roi_now'] * 100:.0f}% (was {C.fnum(oc.get('roi_now')) * 100 if C.fnum(oc.get('roi_now')) is not None else 0:.0f}% "
                   f"at ${C.fnum(oc.get('cost')) or 0:.2f})")
            recheck.append({"id": oc["id"], "expire": True, "why": why})
            rechecked_expired.append((r, why))
        else:
            recheck.append({"id": oc["id"], "cost": round(e["entered"], 2), "price_basis": e["label"][:200], "buybox": bb,
                            "roi_now": e["roi_now"]})
    counts["open_rechecked"], counts["open_expired"] = len(recheck), len(rechecked_expired)
    C.save_json(f"out/{day.isoformat()}/recheck.json", recheck)

    # Products held back as out of stock go on the Sephora reader's to-do list (Hub doc reader/sephora_todo, served by
    # /api/reader/todo?list=sephora): the daily Chrome stock check re-reads their item lists, most profitable first,
    # <= 60 pages, so a restocked item is posted again by the next run (Nick, Nov 23, 2026).
    oos_todo, seen_p = [], set()
    for _, r, _ in sorted(held_oos, key=lambda x: -(x[0] or 0)):
        url = str(r.get("sephora_url") or "").split("?")[0]
        if r["product_id"] in seen_p or not url.startswith("https://www.sephora.com/product/"):
            continue
        seen_p.add(r["product_id"])
        oos_todo.append(r["product_id"] + "|" + url.replace("https://www.sephora.com", ""))
    oos_todo = oos_todo[:60]
    C.save_json(f"out/{day.isoformat()}/oos_recheck_todo.json", oos_todo)
    if not a.dry_run:
        try:
            C.hub("PUT", "/api/engine/docs/reader/sephora_todo", {"items": oos_todo, "why": "held out of stock", "at": day.isoformat()})
        except Exception as e:  # noqa: BLE001
            problems.append(f"out-of-stock recheck list not saved: {e!r}")

    counts.update(brands=len(brands), products_checked=len(sp["products"]), sale_page_items=len(sp["products"]),
                  sale_page_in_catalog=len(seph_today) - len(sale_ended), sale_ended=len(sale_ended),
                  price_drops=price_drops, new_products=len(new_products),
                  new_products_in_config=sum(1 for x in new_products if x["brand_in_config"] and not x["excluded_brand"]),
                  promo_brands=sorted(disc), keepa_tokens=tokens, watched=len(refreshed), keepa_from_collector=from_collector, prefilter_15=len(passed),
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
        try:
            if recheck:
                C.log("open candidates recheck:", C.hub("POST", "/api/engine/sephora/candidates/recheck", {"rows": recheck}, timeout=300))
        except Exception as e:  # noqa: BLE001
            problems.append(f"open candidates recheck failed: {e!r}"[:300])
        if posted_res:
            counts["hub_inserted"], counts["hub_updated"] = posted_res.get("inserted"), posted_res.get("updated")
            counts["hub_skipped"] = len(posted_res.get("skipped") or [])
        counts["problems"] = len(problems)
        try:
            C.log("run:", C.hub("POST", "/api/engine/sephora/run", run_body))
        except Exception as e:  # noqa: BLE001
            problems.append(f"run record failed: {e!r}"[:300])

    # ---- 7. summary
    # markdowns by brand (from the Sale page) and the banner deals, with what the Lead DB can do with them (Nov 24, 2026)
    brand_rows = {}
    for r_ in cat:
        brand_rows.setdefault(r_.get("brand"), []).append(r_)
    def brand_state(b_):
        rs_ = brand_rows.get(b_) or []
        if not rs_:
            return "not in your Lead DB"
        if all(x.get("sellable") == "restricted" for x in rs_):
            return "restricted for you"
        return ""
    mk = {}
    for pid_, t_ in seph_today.items():
        if not t_.get("on_sale") or pid_ not in by_pid:
            continue
        b_ = by_pid[pid_][0].get("brand")
        lo_ = C.fnum(t_.get("plo")) or C.fnum(t_.get("lo"))
        mk.setdefault(b_, []).append((C.fnum(t_.get("slo")) or 0) / lo_ if lo_ else None)
    md_parts = []
    for b_, v_ in sorted(mk.items(), key=lambda kv: -len(kv[1])):
        rat = sorted(x for x in v_ if x)
        pct_ = f"{round((1 - rat[len(rat) // 2]) * 100)}% off" if rat else "?"
        st_ = brand_state(b_)
        md_parts.append(f"{b_} {len(v_)} on sale, ~{pct_}" + (f" ({st_})" if st_ else ""))
    md_txt = "; ".join(md_parts[:25]) + (f"; +{len(md_parts) - 25} more brands" if len(md_parts) > 25 else "")
    dl_parts = []
    for d_ in deals:
        p_ = f"{'up to ' if d_.get('up_to') else ''}{d_['pct'] * 100:.0f}% off"
        if d_["kind"] == "brand":
            b_ = d_["brand"]
            st_ = brand_state(b_) if d_.get("in_lead_db") else "not in your Lead DB"
            seen_ = f"{len(mk.get(b_, []))} on sale" if b_ in mk else "no markdowns seen — check"
            dl_parts.append(f"{b_} {p_}{' today only' if d_.get('today_only') else ''} ({st_ or seen_})")
        elif d_["kind"] == "multibuy":
            dl_parts.append(f"buy {d_['buy']} {d_['what']}, {p_} (minis priced at the deal, flagged to confirm)")
        else:
            dl_parts.append(f"{d_['what']} {p_} (category deal; shows as markdowns)")
    deal_txt = "; ".join(dl_parts)
    lines = [f"# Sephora daily watch, {day.isoformat()}{' (DRY RUN: nothing written to the Hub)' if a.dry_run else ''}", "",
             f"- Promo codes: {sale['text'] + ' (code ' + str(sale['code']) + ', ends ' + str(sale['ends']) + ')' if sale else ('none found' if sale_checked else 'NOT CHECKED (promo pages unreachable)')}",
             "- Markdowns today by brand (Sephora sale prices, priced by the job): " + (md_txt or "none"),
             "- Deals on Sephora's banners: " + (deal_txt or "none found"),
             f"- Promo discounts applied: " + (", ".join(f"{'all other brands' if k == '*' else k}: {v[0] * 100:.0f}%" for k, v in disc.items()) or "none"),
             f"- Sephora Sale page: {len(sp['products'])} items; {counts['sale_page_in_catalog']} in the catalog "
             f"({price_drops} newly on sale / cheaper), {len(sale_ended)} catalog sales ended, {len(new_products)} not in the "
             f"catalog ({counts['new_products_in_config']} from configured brands) -> new_products.json",
             f"- Watched {len(watch)} ASINs; Amazon prices fresh for {len(refreshed)} ({from_collector} from the Keepa collector's pulls, "
             f"{len(refreshed) - from_collector} pulled by this job with {tokens} tokens; {kstop or 'all done'})",
             f"- ROI >= 15% after live check: {len(passed)}; skipped (Nick decided in the last {DECIDED_DAYS} days): {len(skipped_decided)}; "
             f"scored: {counts['scored']}; candidates posted: {len(cands)}; held for a size check: {len(size_check)}; held as out of stock at Sephora: {len(held_oos)}; held by the rank rule (under {RANK_RULE_ROI * 100:.0f}% ROI needs 30-day avg rank under {RANK30_MAX:,}): {len(held_rank)}; "
             f"held for the match check: {len(held_check)} unchecked (queued), {len(held_wrong)} the check says are wrong (for Nick)",
             f"- Live Amazon restriction check: {len(gated)} gated today ({', '.join(f'{v_} {k_}' for k_, v_ in counts.get('gated_today', {}).items()) or 'none'}); "
             f"{len(held_gated)} leads held" + (": " + "; ".join(f"{r['brand']} {r['asin']} ({g})" for _, r, g in held_gated[:8]) if held_gated else ""),
             f"- Catalog rows updated: {len(cat_rows)}; runtime {counts['minutes']} min",
             f"- Posted with a match warning from the learned rules (Nick's wrong-match marks): {len(rule_skipped)}",
             f"- Open leads on To review re-priced: {len(recheck)}; expired as no longer profitable (< 15% ROI): {len(rechecked_expired)}"]
    lines += [f"  - {r['asin']} {r.get('sephora_name') or ''}: {why}" for r, why in rechecked_expired[:15]] + [""]
    if cand_view:
        lines += ["## Candidates", "", "| ASIN | Brand | Sephora product | Cost | Buy Box | ROI | Band | Pred ROI | Reason |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c_ in sorted(cand_view, key=lambda x: -((x["lead_score_pred_roi"] if x["lead_score_pred_roi"] is not None else x["roi_now"]) or 0)):
            lines.append(f"| {c_['asin']} | {c_['brand']} | {c_['sephora_name']}{' (' + c_['shade'] + ')' if c_.get('shade') else ''} | "
                         f"${c_['cost']:.2f} | ${(c_['buybox'] or 0):.2f} | {C.pct(c_['roi_now'])} | {c_['lead_score_band'] if c_['lead_score_band'] is not None else '-'} | "
                         f"{C.pct(c_['lead_score_pred_roi']) if c_['lead_score_pred_roi'] is not None else '-'} | {c_['reason']} |")
        lines.append("")
    if size_check:
        lines += ["## Held by the size check (posted once Nick marks the pair OK in the Lead DB)", "",
                  "| ASIN | Brand | Sephora product | Why | Buy Box | ROI |", "|---|---|---|---|---|---|"]
        for roi_, r, e in sorted(size_check, key=lambda x: -(x[0] or 0))[:40]:
            lines.append(f"| {r['asin']} | {r['brand']} | {r['sephora_name']} | {e.get('size_why') or e.get('label')} | "
                         f"${(amz[r['asin']]['buybox'] or amz[r['asin']]['fba_low'] or 0):.2f} | {C.pct(roi_)} |")
        lines.append("")
    if rule_skipped:
        lines += ["## Posted with a match warning (learned rules from Nick's wrong-match marks)", ""]
        lines += [f"- {a_} {n_ or ''}: {why}" for a_, n_, why in rule_skipped[:30]] + [""]
    if fatal:
        lines.insert(1, f"**RUN FAILED: {fatal}.**")
    if problems:
        lines += ["## Problems", ""] + [f"- {p}" for p in problems] + [""]
    else:
        lines += ["No problems.", ""]
    # Health checks (Nick, Nov 24, 2026: failures must announce themselves; that morning's run priced 64% of the listings
    # and nothing said so). Any ALERT line goes to Nick's phone from the daily task.
    alerts = []
    cov = len(refreshed) / len(order) if order else 1.0
    if cov < HEALTH_COVERAGE:
        alerts.append(f"Amazon prices fresh for only {len(refreshed):,} of {len(order):,} watched listings ({cov * 100:.0f}%, needs {HEALTH_COVERAGE * 100:.0f}%+)")
    ls_fail = [p_ for p_ in problems if p_.startswith("Lead Score")]
    if ls_fail:
        alerts.append("Lead Score: " + "; ".join(p_[:120] for p_ in ls_fail[:2]))
    hub_fail = [p_ for p_ in problems if ("Hub" in p_ or "failed" in p_) and not p_.startswith("Lead Score")]
    if hub_fail:
        alerts.append(f"{len(hub_fail)} step(s) failed: " + "; ".join(p_[:120] for p_ in hub_fail[:3]))
    if fatal:
        alerts.append(f"RUN FAILED: {fatal}")
    now_ct = dt.datetime.now(CT)
    start_ct = dt.datetime.fromtimestamp(t0, CT)
    if not a.no_keepa and start_ct.hour < HEALTH_DONE_BY and now_ct.hour >= HEALTH_DONE_BY and now_ct.date() == start_ct.date():   # the main morning run, not a --no-keepa re-run
        alerts.append(f"run finished at {now_ct:%-I:%M %p} Central, after {HEALTH_DONE_BY} am")
    counts["alerts"] = len(alerts)
    lines.insert(1, ("**ALERT:** " + " | ".join(alerts)) if alerts else "Health: OK (prices fresh for "
                     f"{cov * 100:.0f}% of watched listings, Lead Score and Hub steps fine)")
    lines.insert(2, "")
    if not a.dry_run:
        try:
            C.hub("PUT", "/api/engine/docs/sephora_jobs/health",
                  {"day": day.isoformat(), "at": now_ct.isoformat(timespec="seconds"), "ok": not alerts, "alerts": alerts,
                   "coverage": round(cov, 4), "watched": len(order), "priced": len(refreshed), "posted": len(cands),
                   "minutes": counts.get("minutes")})
        except Exception as e:  # noqa: BLE001
            C.log(f"health doc not saved: {e!r}")
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
