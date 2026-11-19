"""Two weekly steps that find leads the regular matching misses (Nov 19, 2026; RUNBOOK step 5b / 5c).

5b. rematch_wrong: ASINs whose every Sephora pair Nick (or Claude) marked wrong. The matcher kept one product per ASIN and
    never tried the runner-up, so a wrong mark ended the ASIN (e.g. CHANEL No.5 L'Eau matched to N°5 EDT, then marked
    wrong). Each such ASIN is scored again against its brand's current Sephora listing with the wrong products left out;
    a high / medium match is added as a new pair ("re-matched after wrong mark"), low ones are only reported.
    No Keepa or SP-API calls: the Amazon fields are copied from the wrong pair and the daily job refreshes them.

5c. brand_sweep: the new-products search only looks for Amazon titles containing a Sephora product's first name words,
    so listings titled differently ("CHANEL Bleu De Paris Cologne") were never found. This takes a few brands per run
    (oldest swept first) and pulls their best-selling Amazon ASINs from the Keepa finder (one page of 50 per brand,
    a deeper page each rotation), then matches the ASINs not in the catalog yet with the usual pipeline.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import os
import re

import common as C

SWEEP_PAGES = 4          # brand finder pages walked before starting again at page 0
SWEEP_PER_BRAND = 20     # ASINs per brand fetched with /product (the ones not in the catalog, best sellers first)


def _keepa_rec(r: dict) -> dict:
    """A catalog row's Amazon side in the raw/keepa record shape match.py reads."""
    ex = r.get("extra") or {}
    return {"asin": r["asin"], "title": r.get("amazon_title") or "", "color": r.get("shade"), "size": ex.get("amazon_size"),
            "parentAsin": None, "variation_attrs": {}}


IGNORE = set("spray vaporisateur vapo pour homme femme women men natural edition new size travel the".split())
CONC_WORDS = {"eau", "toilette", "parfum", "cologne", "extrait"}
# product-form words: on the Amazon side they count even when Sephora never uses them (a body cream, deodorant or tester
# of a perfume is not the perfume)
FORM_WORDS = set("deo deodorant body cream creme lotion hand shower gel soap oil veil balm mist stick wash tester "
                 "aftershave shave refill sample vial".split())
MEN_RE = r"\b(men|man|mens|homme|him|masculine)\b"


def _word_gap(M, w: dict, sephora_name: str, drop: set, vocab: set) -> str | None:
    title = w.get("amazon_title") or ""
    skip = IGNORE | set(M.tokens(w.get("shade") or "", drop))
    a = set(M.tokens(title, drop))
    if not M.conc(title) and not ("parfum" in a and "eau" not in a):   # "Miss Parfum Spray" names a concentration too
        skip |= CONC_WORDS
    s = set(M.tokens(sephora_name or "", drop))
    extra_a = sorted(((a & vocab) | (a & FORM_WORDS)) - s - skip)
    extra_s = sorted(s - a - skip)
    ma, ms = bool(re.search(MEN_RE, M.base_text(title))), bool(re.search(MEN_RE, M.base_text(sephora_name or "")))
    if ma != ms:   # Allure vs Allure Homme: the men's line is another product
        (extra_a if ma else extra_s).append("homme/men")
    if not (extra_a or extra_s):
        return None
    return "names differ: " + "; ".join(x for x in (extra_a and "Amazon has " + " ".join(extra_a),
                                                    extra_s and "Sephora has " + " ".join(extra_s)) if x)


def rematch_wrong(cat: list, cfg: dict, brand_of, listing: dict, log=print) -> tuple[list, list]:
    """Returns (new catalog rows to upsert, low-confidence suggestions for the summary).
    brand_of(name) -> brands.json entry; listing = {productId: (brand entry, Sephora product)} from this run's step 2."""
    import match as M
    by_asin = collections.defaultdict(list)
    for r in cat:
        by_asin[r["asin"]].append(r)
    targets = {a: rows for a, rows in by_asin.items() if rows and all(x.get("match_ok") is False for x in rows)}
    corpus = collections.defaultdict(dict)    # slug -> {asin: rec}: the brand's Amazon titles (word weights need more than the targets)
    want = collections.defaultdict(dict)      # slug -> {asin: wrong row}
    excl, cfg_of = {}, {}
    for a, rows in targets.items():
        b = brand_of(rows[0].get("brand") or "")
        if not b or not os.path.exists(os.path.join(C.DATA, "raw", "sephora", b["slug"] + ".json")):
            continue
        cfg_of[b["slug"]] = b
        want[b["slug"]][a] = max(rows, key=lambda x: str(x.get("marked_at") or ""))
        excl[a] = {x["product_id"] for x in rows}
    for r in cat:
        b = brand_of(r.get("brand") or "")
        if b and b["slug"] in want:
            corpus[b["slug"]].setdefault(r["asin"], _keepa_rec(r))
    rows_out, low = [], []
    for slug, tg in want.items():
        b = cfg_of[slug]
        try:
            res = M.match_brand(slug, b, keepa={"asins": list(corpus[slug].values())}, exclude=excl)
        except Exception as e:  # noqa: BLE001
            log(f"rematch {b['name']} failed: {e!r}")
            continue
        drop = set()
        for n in [b["name"]] + b.get("sephora_names", []) + b.get("keepa_brands", []) + b.get("strip", []):
            drop |= set(M.base_text(n).split())
        vocab = {w for x in res["sephora"]["products"] for w in M.tokens(x.get("name") or "", drop)}
        for m in res["matches"]:
            w = tg.get(m["asin"])
            if not w or m["productId"] in excl[m["asin"]]:
                continue
            # stricter than a first match: the runner-up after a wrong mark is usually a sibling (EDP for EDT, Allure Homme
            # for Allure Homme Sport, the perfume for the body cream, Overcurl for Overcurl Waterproof). Both names must
            # carry the same words: every word of the Sephora name is in the Amazon title, and every Amazon title word that
            # Sephora uses in this brand's names is in the Sephora name (shade, spray / for men words and, when the Amazon
            # title names no concentration, eau de toilette / parfum don't count). Anything else is only a suggestion.
            why = _word_gap(M, w, m["sephora_name"], drop, vocab)
            if why and m["confidence"] != "low":
                m["confidence"] = "low"
                m["reason"] = why + "; " + m["reason"]
            p = (listing.get(m["productId"]) or (None, None))[1] or next(
                (x for x in res["sephora"]["products"] if x["productId"] == m["productId"]), None)
            if not p:
                continue
            if m["confidence"] == "low":
                low.append({"asin": m["asin"], "brand": b["name"], "sephora_name": m["sephora_name"], "product_id": m["productId"],
                            "amazon_title": w.get("amazon_title"), "reason": m["reason"][:200]})
                continue
            lo, hi = C.fnum(p.get("price_lo")), C.fnum(p.get("price_hi"))
            wx = w.get("extra") or {}
            ex = {k: wx[k] for k in ("demand_ok", "thin_price", "sales_rank", "amazon_size", "offers_fbm", "fbm_low") if k in wx}
            ex.update(pack_qty=m.get("pack_qty") or 1, score=m.get("score"), listing_seen=True,
                      rematched_from=sorted(excl[m["asin"]]), rematched_on=C.today().isoformat())
            rows_out.append({
                "asin": m["asin"], "product_id": m["productId"], "sku_id": m.get("skuId") or p.get("skuId"), "brand": b["name"],
                "sephora_name": m["sephora_name"], "sephora_url": p.get("url"), "shade": w.get("shade"),
                "amazon_title": w.get("amazon_title"),
                "match_reason": ("re-matched after wrong mark (" + ", ".join(sorted(excl[m["asin"]])) + "): " + m["reason"])[:2000],
                "price_lo": lo, "price_hi": hi, "sale_price": C.fnum(p.get("sale_price")), "on_sale": bool(p.get("on_sale")),
                "buybox": C.fnum(w.get("buybox")), "fba_low": C.fnum(w.get("fba_low")), "bb_avg90": C.fnum(w.get("bb_avg90")),
                "fba_fee": C.fnum(w.get("fba_fee")), "referral_pct": C.fnum(w.get("referral_pct")),
                "monthly_sold": w.get("monthly_sold"), "offers_fba": w.get("offers_fba"), "amazon_on": bool(w.get("amazon_on")),
                "sellable": w.get("sellable"), "needs_size": bool(lo and hi and hi > lo),
                "size_guess": "unknown" if (lo and hi and hi > lo) else None, "confidence": m["confidence"], "active": True,
                "extra": ex})
    log(f"rematch: {len(targets)} ASINs with every pair marked wrong; {sum(len(v) for v in want.values())} in a configured brand; "
        f"{len(rows_out)} re-matched (high/medium), {len(low)} low-confidence suggestions")
    return rows_out, low


def brand_sweep(cfg: dict, hub_asins: set, budget: int, kp, build_brand, excluded: set, dry_run: bool, log=print) -> dict:
    """Best-selling Amazon ASINs of a few brands (oldest swept first) -> the regular match/build pipeline.
    Returns {rows, brands, tokens, asins_new}. budget = Keepa tokens this step may spend."""
    st = C.state_get("brand_sweep", {}) or {}
    order = sorted(cfg["brands"], key=lambda b: ((st.get(b["slug"]) or {}).get("at") or "", b["slug"]))
    rows, done, tokens, n_new = [], [], 0, 0
    for b in order:
        if budget - tokens < 120 or not b.get("keepa_brands"):
            continue
        prev = st.get(b["slug"]) or {}
        page = (prev.get("page", -1) + 1) % SWEEP_PAGES
        sel = {"brand": b["keepa_brands"], "current_COUNT_NEW_gte": 1, "current_SALES_gte": 1,
               "sort": [["current_SALES", "asc"]], "perPage": 50, "page": page}
        used0 = kp.state["used"]
        try:
            d = kp.call("query", {"domain": 1, "selection": json.dumps(sel)}, need=15)
            new = [x for x in (d.get("asinList") or []) if x not in hub_asins][:SWEEP_PER_BRAND]
            if not (d.get("asinList") or []) and page:
                page = SWEEP_PAGES - 1   # ran past the brand's last page: start again at 0 next time
            recs = []
            if new:
                d2 = kp.call("product", {"domain": 1, "asin": ",".join(new), "stats": 90, "buybox": 1, "history": 0},
                             need=len(new) * 3 + 5)
                recs = [kp.compact(x) for x in d2.get("products") or []]
        except Exception as e:  # noqa: BLE001
            log(f"brand sweep {b['name']}: Keepa failed {e!r}"[:200])
            tokens += kp.state["used"] - used0
            continue
        tokens += kp.state["used"] - used0
        st[b["slug"]] = {"at": C.today().isoformat(), "page": page, "found": len(new)}
        done.append(f"{b['name']} p{page}: {len(new)} new ASINs")
        n_new += len(new)
        if not recs:
            continue
        json.dump({"brand": b["name"], "pulled_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "finder_total": len(recs), "finder_root_beauty": False, "fetched": len(recs), "truncated": False,
                   "tokens_used": 0, "asins": recs},
                  open(os.path.join(C.DATA, "raw", "keepa", b["slug"] + ".json"), "w"), indent=1)
        try:
            got = [x for x in build_brand(b["slug"], b, excluded) if x["asin"] not in hub_asins]
            for x in got:
                x["match_reason"] = ("brand best-seller sweep: " + (x.get("match_reason") or ""))[:2000]
            rows += got
            hub_asins |= {x["asin"] for x in got}
        except Exception as e:  # noqa: BLE001
            log(f"brand sweep {b['name']}: matching failed {e!r}"[:300])
    if not dry_run:
        C.state_put("brand_sweep", st)
    log(f"brand sweep: {len(done)} brands, {n_new} ASINs not in the catalog, {len(rows)} matched, {tokens} Keepa tokens")
    return {"rows": rows, "brands": done, "tokens": tokens, "asins_new": n_new}
