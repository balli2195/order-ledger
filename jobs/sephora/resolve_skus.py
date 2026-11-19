#!/usr/bin/env python3
"""Pick the Sephora SKU (shade / size / scent) for catalog pairs that don't have one yet (Nov 19, 2026).

  python3 -I resolve_skus.py [--dry-run] [--max-tokens 600] [--max-photo-pairs 150] [--recheck]

1. Hub catalog (active, not marked wrong) + store_skus (the SKU lists read from Sephora pages, RUNBOOK 4d).
   Pairs whose product has 2+ SKUs and no store_size yet; --recheck also re-checks pairs that have one (report only).
2. Amazon side: the Keepa collector's latest pull (GET /api/engine/keepa/meta, no tokens); only ASINs it lacks go to
   Keepa /product (history=0, 1 token per ASIN). Cached 30 days in $SEPH_DATA/state/keepa_meta.json:
   title, color, the listing's own variation attributes (the selector), model / partNumber, bullets, photos.
3. shade.resolve_text: Sephora item number in model/partNumber; the code and the name matched separately in the
   selector, color, title, bullets; size SKUs on ounces; same shade in several sizes -> the size from the title.
   On Nick's leads (316 with one clear lead shade): decides 84%, 96% agree with the lead link.
4. Shade pairs the text can't decide and that are near profit (ROI now >= 0 or 90-day >= 5%): photos
   (shade_image.resolve: Sephora's SKU photo vs the Amazon photos; only near-identical renders decide).
5. Decided -> POST /catalog/mark {store_size} (doesn't change who checked the match). Undecided -> extra.shade_hint:
   {not_sold: <Amazon shade>} when the listing names a shade that shares no word or code with any Sephora SKU (the
   daily job doesn't watch it: 4 of 316 lead shades were flagged on the test), else {guess: [best guesses]}, which the
   daily job puts in the candidate note ("Shade not confirmed: ...").
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import shade as SH  # noqa: E402

META_DAYS = 30


def keepa_meta(asins, max_tokens, kp):
    cache = C.load_json("state/keepa_meta.json", {}) or {}
    now = time.time()
    todo = [a for a in asins if a not in cache or now - cache[a].get("_ts", 0) > META_DAYS * 86400]
    keep = ["asin", "title", "brand", "color", "size", "model", "partNumber", "features", "description", "images",
            "variations", "parentAsin", "itemTypeKeyword", "scent", "style"]
    # the Keepa collector already holds a full pull of every Lead DB ASIN: read those first (no tokens)
    hub_n = 0
    for i in range(0, len(todo), 300):
        try:
            got = C.hub("GET", "/api/engine/keepa/meta", params={"asins": ",".join(todo[i:i + 300])}).get("products") or {}
        except Exception as e:  # noqa: BLE001
            C.log(f"collector read failed ({e!r}); using Keepa"[:200])
            break
        for a_, rec in got.items():
            rec["_ts"] = now
            cache[a_] = rec
            hub_n += 1
    C.log(f"listing details from the Keepa collector: {hub_n}")
    todo = [a for a in todo if a not in cache or now - cache[a].get("_ts", 0) > META_DAYS * 86400]
    used = 0
    for i in range(0, len(todo), 100):
        ch = todo[i:i + 100]
        if used + len(ch) > max_tokens:
            break
        d = kp.call("product", {"domain": 1, "asin": ",".join(ch), "history": 0}, need=len(ch) + 5)
        for p in d.get("products") or []:
            rec = {k: p.get(k) for k in keep}
            rec["features"] = (rec.get("features") or [])[:5]
            rec["description"] = (rec.get("description") or "")[:1500]
            rec["_ts"] = now
            cache[p["asin"]] = rec
        used += len(ch)
    C.save_json("state/keepa_meta.json", cache)
    return cache, used, len(todo) - min(len(todo), used)


def _get(url, path, ua=True):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"} if ua else {})
        data = urllib.request.urlopen(req, timeout=30).read()
        open(path, "wb").write(data)
        return path
    except Exception:  # noqa: BLE001
        return None


def photo_pick(asin, k, skus):
    import shade_image as SI
    d = os.path.join(C.DATA, "img")
    os.makedirs(d, exist_ok=True)
    amz = []
    for i, im in enumerate((k.get("images") or [])[:4]):
        f = im.get("m") or im.get("l")
        if f:
            p = _get("https://m.media-amazon.com/images/I/" + f, os.path.join(d, f"a_{asin}_{i}.jpg"), ua=False)
            if p:
                amz.append(p)
    imgs = {}
    for s in skus:
        sid = s["sku_id"]
        m = _get(f"https://www.sephora.com/productimages/sku/s{sid}-main-grid.jpg?imwidth=315", os.path.join(d, f"s_{sid}_main.jpg"))
        imgs[sid] = {"main": m} if m else {}   # swatch chips were only right 5 times in 21 on the leads: not fetched
        time.sleep(0.1)
    return SI.resolve(amz, imgs)


def label(s):
    v, z = (s.get("value") or "").strip(), (s.get("size") or "").strip()
    return (v if not z or z in v else f"{v} {z}".strip())[:60] or z[:60]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=600)
    ap.add_argument("--max-photo-pairs", type=int, default=150)
    ap.add_argument("--recheck", action="store_true", help="also check pairs that already have a store size (report only)")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY", "KEEPA_KEY")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog"))
    import keepa_pull as kp
    cat = [r for r in C.hub_catalog() if r.get("active") and r.get("match_ok") is not False]
    sk = {r["product_id"]: r for r in C.hub("GET", "/api/engine/store-skus").get("rows") or []}
    def unsaved(r):   # 'shade not matched · <size>' = only the size was saved (Nov 18): the shade is still open
        return not r.get("store_sku_id") or str(r.get("store_size") or "").startswith("shade not matched")
    todo = [r for r in cat if r["product_id"] in sk and len(sk[r["product_id"]]["skus"]) > 1 and (a.recheck or unsaved(r))]
    # near-profit pairs first, so the Keepa budget goes where a lead is possible
    def reach(r):
        return max(C.fnum(r.get("roi_now")) or -9, (C.fnum(r.get("roi_avg90")) or -9) - 0.05)
    todo.sort(key=lambda r: -reach(r))
    C.log(f"{len(todo)} pairs to resolve ({len({r['asin'] for r in todo})} ASINs)")
    meta, used, left = keepa_meta(list(dict.fromkeys(r["asin"] for r in todo)), a.max_tokens, kp)
    C.log(f"Keepa: {used} tokens; {left} ASINs wait for the next run")
    pn = {}
    for f in os.listdir(os.path.join(C.DATA, "raw", "sephora")) if os.path.isdir(os.path.join(C.DATA, "raw", "sephora")) else []:
        try:
            for p in json.load(open(os.path.join(C.DATA, "raw", "sephora", f)))["products"]:
                pn[p["productId"]] = p.get("name")
        except Exception:  # noqa: BLE001
            pass
    marks, hints, stats, recheck, photos = [], [], collections.Counter(), [], 0
    for r in todo:
        k = meta.get(r["asin"])
        if not k or not (k.get("title") or "").strip():
            stats["no Amazon data"] += 1
            continue
        s = sk[r["product_id"]]
        res = SH.resolve_text(k, s["skus"], s.get("variation_type"), r["asin"], pn.get(r["product_id"]) or r.get("sephora_name"))
        how = "text"
        if not res["sku_id"] and "olor" in (s.get("variation_type") or "") and photos < a.max_photo_pairs and reach(r) >= 0:
            photos += 1
            # text printed on the listing photos (box bottoms, stickers) first, then the photo comparison
            d_ = os.path.join(C.DATA, "img"); os.makedirs(d_, exist_ok=True)
            big = [_get("https://m.media-amazon.com/images/I/" + (im.get("l") or im.get("m")), os.path.join(d_, f"A_{r['asin']}_{i}.jpg"), ua=False)
                   for i, im in enumerate((k.get("images") or [])[:6]) if im.get("l") or im.get("m")]
            kk = dict(k); kk["_ocr"] = SH.keep_ocr(SH.ocr_photos([b for b in big if b]), s["skus"])
            ro = SH.resolve_text(kk, s["skus"], s.get("variation_type"), r["asin"], pn.get(r["product_id"]) or r.get("sephora_name"))
            if ro["sku_id"]:
                res, how = ro, "photo text"
        if not res["sku_id"] and "olor" in (s.get("variation_type") or "") and photos <= a.max_photo_pairs and reach(r) >= 0:
            pr = photo_pick(r["asin"], k, s["skus"])
            if pr.get("sku_id"):
                res = {"sku_id": pr["sku_id"], "how": pr["how"]}; how = "photo"
            else:
                res["photo_hint"] = pr.get("hint")
        by = {x["sku_id"]: x for x in s["skus"]}
        if not unsaved(r) and a.recheck:
            if res["sku_id"] and res["sku_id"] != r["store_sku_id"] and label(by[res["sku_id"]]) != (r.get("store_size") or ""):
                recheck.append((r["asin"], r["product_id"], r.get("store_size"), label(by[res["sku_id"]]), res["how"][:100]))
            continue
        if res["sku_id"]:
            x = by[res["sku_id"]]
            stats[f"decided ({how})"] += 1
            marks.append({"asin": r["asin"], "product_id": r["product_id"],
                          "store_size": {"sku_id": x["sku_id"], "size": label(x), "price": x.get("price")}, "_how": res["how"]})
            alts = [sid for sid in (res.get("alts") or []) if sid in by]
            if alts or (r.get("extra") or {}).get("shade_hint"):   # alternates for the stock check; an old hint no longer applies
                ex = {k_: v for k_, v in (r.get("extra") or {}).items() if k_ != "shade_hint"}
                if alts:
                    ex["store_sku_alts"] = alts
                if res.get("two_shades"):   # the candidate note asks Nick to check which one the photo shows
                    ex["shade_hint"] = {"two_shades": [label(x)] + [label(by[a_]) for a_ in alts], "checked": C.today().isoformat()}
                    stats["two shades named (both kept)"] += 1
                hints.append({"asin": r["asin"], "product_id": r["product_id"], "active": r["active"], "extra": ex})
        else:
            ns = SH.shade_not_sold(k, s["skus"], r["asin"]) if "olor" in (s.get("variation_type") or "") or "cent" in (s.get("variation_type") or "") else None
            if ns:   # the listing names a shade Sephora's list doesn't have: not a lead (the daily job skips it)
                stats["shade not sold at Sephora"] += 1
                hints.append({"asin": r["asin"], "product_id": r["product_id"], "active": r["active"],
                              "extra": {**(r.get("extra") or {}), "shade_hint": {"not_sold": ns[:80], "checked": C.today().isoformat(),
                                                                                 "sephora": [label(x) for x in s["skus"]][:12]}}})
                continue
            stats["undecided"] += 1
            top = [by[sid] for sc_, sid, _w in (res.get("ranked") or [])[:3] if sc_ > 0]
            close = SH.close_skus(k, s["skus"], r["asin"])
            if close:   # 'Noir' vs 'Black': kept as a lead, the note says it's a close shade to check
                stats["close shade (review)"] += 1
                top = close + [t for t in top if t not in close]
            if res.get("photo_hint") and res["photo_hint"] in by and by[res["photo_hint"]] not in top:
                top.insert(0, by[res["photo_hint"]])
            hints.append({"asin": r["asin"], "product_id": r["product_id"], "active": r["active"],
                          "extra": {**(r.get("extra") or {}), "shade_hint": {"guess": [label(t) for t in top[:3]],
                                                                             "close": bool(close),
                                                                             "checked": C.today().isoformat()}}})
    C.log("results:", dict(stats))
    C.save_json(f"out/{C.today().isoformat()}/resolve_skus.json", {"marks": marks, "hints": hints, "recheck": recheck})
    if recheck:
        C.log(f"recheck: {len(recheck)} saved sizes differ from the resolver:")
        for x in recheck[:60]:
            C.log("  ", *x)
    if a.dry_run:
        return
    ok = fail = 0
    for m in marks:
        try:
            C.hub("POST", "/api/engine/sephora/catalog/mark", {k: v for k, v in m.items() if not k.startswith("_")})
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            C.log("mark failed", m["asin"], m["product_id"], repr(e)[:120])
    if hints:
        for i in range(0, len(hints), 500):
            C.hub_upsert_catalog(hints[i:i + 500])
    C.log(f"saved {ok} store sizes ({fail} failed), {len(hints)} shade hints")


if __name__ == "__main__":
    main()
