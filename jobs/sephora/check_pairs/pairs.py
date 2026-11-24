"""Weekly check of new Lead DB pairs (Nick, Nov 20, 2026): the shared steps.

    python3 -I pairs.py prep    <work> [--max 300] [--queue]   pick the pairs (--queue: the daily run's held pairs,
                                                 Hub doc sephora_jobs/check_queue), put their ASINs on the reader's list
    python3 -I pairs.py wait    <work> [max_min]     wait until the reader has saved >= 95% of the Amazon pages
    python3 -I pairs.py screen  <work>               quick-screen case files + prompts (50 pairs each)
    python3 -I pairs.py collect <work>               read the screen answers; record passes (extra.screened)
    python3 -I pairs.py research <work>              research case files + prompts (20 ASINs each)
    python3 -I pairs.py finish  <work> <NNN>         cross-check one research answer file and apply it (verify_apply.py)
    python3 -I pairs.py summary <work>               counts for Nick's message

Which pairs: active, sellable == 'open', not decided by Nick (match_ok null), not yet researched (extra.verified) or
screened (extra.screened), and not already waiting on Nick (Hub doc sephora_jobs/verify_for_nick). Pairs whose ROI
(now or 90-day average) is at least -5% go straight to research; the rest are screened first and only the ones the
screen doesn't clearly pass are researched. The method and its validation: claude/sephora-catalog/CHECK_ALL_2026-11-20.md.

Never loads sephora.com or amazon.com from here: Amazon pages are read by reader.js in Nick's old MacBook Chrome (signed-out
"Lead search" profile) and come back through the Hub; Sephora SKU lists come from the Hub (store-skus).
Env: ENGINE_KEY (never printed).
"""
import collections
import datetime
import glob
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import common as C  # noqa: E402

NEAR = -0.05


def jload(p, default=None):
    return json.load(open(p)) if os.path.exists(p) else default


def jsave(p, obj):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    json.dump(obj, open(p, "w"), indent=0)


def catalog_all():
    rows, off = [], 0
    while True:
        d = C.hub("GET", "/api/engine/sephora/catalog", params={"limit": 1000, "offset": off}, timeout=300)["rows"]
        rows += d
        off += 1000
        if len(d) < 1000:
            return rows


def for_nick():
    d = C.state_get("verify_for_nick", {}) or {}
    return {(x["asin"], x["product_id"]) for x in ((d.get("items") if isinstance(d, dict) else d) or [])}


PID_RE = re.compile(r"(P\d{5,7})\b")


def bought_pairs():
    """{(asin, pid): note} for leads Nick bought (qty_ordered > 0) from a Sephora product page."""
    out, off = {}, 0
    while off is not None:
        d = C.hub("GET", "/api/engine/leads", params={"limit": 5000, "offset": off}, timeout=300)
        for l in d["leads"]:
            m = PID_RE.search(l.get("store_link") or "")
            if not (m and "sephora" in (l.get("store_link") or "").lower() and (l.get("qty_ordered") or 0) > 0 and l.get("asin")):
                continue
            k = (l["asin"].upper(), m.group(1))
            sku = re.search(r"skuId=(\d+)", l.get("store_link") or "")
            prev = out.get(k)
            units = (prev or {}).get("units", 0) + int(l["qty_ordered"])
            last = max(filter(None, [(prev or {}).get("last"), l.get("date")]))
            out[k] = {"units": units, "last": last, "sku": (sku.group(1) if sku else (prev or {}).get("sku"))}
        off = d.get("next_offset")
    return {k: f"last bought {v['last']}, {v['units']} units" + (f", sku {v['sku']}" if v.get("sku") else "") for k, v in out.items()}


def sephora_products(rows):
    """Sephora products per brand from the Lead DB itself (every paired product), written as raw/sephora/<brand>.json so
    verify_apply.py can add a re-pointed pair. Products no ASIN is paired with are missing; research can name them, but
    a pair to one of them is added by the weekly Sephora run, not here."""
    by = collections.defaultdict(dict)
    for r in rows:
        if r.get("product_id") and r.get("brand"):
            by[r["brand"]].setdefault(r["product_id"], {"productId": r["product_id"], "name": r.get("sephora_name"), "url": r.get("sephora_url"),
                                                         "price_lo": r.get("price_lo"), "price_hi": r.get("price_hi"), "brandName": r["brand"]})
    d = os.path.join(C.DATA, "raw", "sephora")
    for b, ps in by.items():
        json.dump({"brand": b, "products": list(ps.values())}, open(os.path.join(d, re.sub(r"[^a-z0-9]+", "_", b.lower()).strip("_") + ".json"), "w"))
    return by


def prep(work, mx, queue=False):
    rows = catalog_all()
    nick = for_nick()
    if queue:   # the daily run's held pairs (Hub doc sephora_jobs/check_queue; Nick, Nov 24, 2026)
        q = set((C.hub("GET", "/api/engine/docs/sephora_jobs/check_queue").get("data") or {}).get("items") or [])
        pick = [r for r in rows if f"{r['asin']}|{r['product_id']}" in q and r.get("match_ok") is not False]
    else:
        pick = [r for r in rows if r["active"] and r.get("sellable") == "open" and r.get("match_ok") is None
                and not (r.get("extra") or {}).get("verified") and not (r.get("extra") or {}).get("screened")
                and (r["asin"], r["product_id"]) not in nick]
    roi = lambda r: max(x for x in (r.get("roi_now"), r.get("roi_avg90"), -9) if x is not None)
    pick.sort(key=roi, reverse=True)
    left = len(pick) - mx if len(pick) > mx else 0
    pick = pick[:mx]
    near = [f"{r['asin']}|{r['product_id']}" for r in pick if roi(r) >= NEAR]
    rest = [f"{r['asin']}|{r['product_id']}" for r in pick if roi(r) < NEAR]
    asins = list(dict.fromkeys(r["asin"] for r in pick))
    have, cutoff = set(), (C.today() - datetime.timedelta(days=7)).isoformat()
    for i in range(0, len(asins), 60):   # pages read in the last week are reused
        have |= {x["asin"] for x in C.hub("GET", "/api/engine/amazon-pages", params={"asins": ",".join(asins[i:i + 60])})["rows"]
                 if (x.get("data") or {}).get("title") and str(x.get("read_at") or "")[:10] >= cutoff}
    todo = [a for a in asins if a not in have]
    jsave(f"{work}/near_pairs.json", near)
    jsave(f"{work}/rest_pairs.json", rest)
    jsave(f"{work}/bought.json", [{"asin": a, "pid": p, "note": n} for (a, p), n in bought_pairs().items()])
    jsave(f"{work}/brands.json", {b: list(ps.values()) for b, ps in sephora_products(rows).items()})
    jsave(f"{work}/meta.json", {"started": C.today().isoformat(), "pairs": len(pick), "near": len(near), "rest": len(rest), "left_for_next_week": left,
                                "asins": len(asins), "reader_todo": len(todo)})
    C.hub("PUT", "/api/engine/docs/reader/amazon_todo", {"items": todo})
    print(json.dumps(jload(f"{work}/meta.json")))


def wait(work, mx_min):
    A = sorted({p.split("|")[0] for p in jload(f"{work}/near_pairs.json") + jload(f"{work}/rest_pairs.json")})
    t0 = time.time()
    while True:
        have = set()
        for i in range(0, len(A), 60):
            try:
                have |= {r["asin"] for r in C.hub("GET", "/api/engine/amazon-pages", params={"asins": ",".join(A[i:i + 60])})["rows"] if (r.get("data") or {}).get("title")}
            except Exception as e:  # noqa: BLE001
                print("hub error", e)
        print(time.strftime("%H:%M"), len(have), "/", len(A), flush=True)
        if len(have) >= 0.95 * len(A) or time.time() - t0 > mx_min * 60:
            return
        time.sleep(120)


def _fill(template, out, **kw):
    s = open(os.path.join(HERE, template)).read().replace("__PRINCIPLES__", os.path.join(os.path.dirname(HERE), "MATCHING_PRINCIPLES.md")) \
        .replace("__VERIFY_FORMAT__", os.path.join(os.path.dirname(HERE), "verify_prompt.md"))
    for k, v in kw.items():
        s = s.replace(f"__{k}__", v)
    open(out, "w").write(s)


def _amazon(asins, work=None):
    """Keepa collector data + the Chrome page per ASIN. <work>/keepa_extra.json (fill_keepa) covers ASINs the collector lacks."""
    meta, amz = dict(jload(f"{work}/keepa_extra.json", {}) or {}) if work else {}, {}
    for i in range(0, len(asins), 50):
        meta.update(C.hub("GET", "/api/engine/keepa/meta", params={"asins": ",".join(asins[i:i + 50])})["products"])
        for r in C.hub("GET", "/api/engine/amazon-pages", params={"asins": ",".join(asins[i:i + 50])})["rows"]:
            amz[r["asin"]] = r["data"]
    return meta, amz


def fill_keepa(work):
    """Keepa product data (photos, color, size, model, barcodes) for this run's ASINs the collector doesn't have yet:
    ~1 token per ASIN, saved to <work>/keepa_extra.json. Env KEEPA_KEY, KEEPA_RESERVE."""
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "catalog"))
    import keepa_pull as kp
    A = sorted({p.split("|")[0] for p in (jload(f"{work}/near_pairs.json") or []) + (jload(f"{work}/rest_pairs.json") or [])})
    have = {}
    for i in range(0, len(A), 50):
        have.update(C.hub("GET", "/api/engine/keepa/meta", params={"asins": ",".join(A[i:i + 50])})["products"])
    extra = jload(f"{work}/keepa_extra.json", {}) or {}
    todo = [a for a in A if not (have.get(a) or {}).get("images") and a not in extra]
    print(len(A), "ASINs;", len(todo), "need Keepa data", flush=True)
    for i in range(0, len(todo), 100):
        ch = todo[i:i + 100]
        d = kp.call("product", {"domain": 1, "asin": ",".join(ch), "stats": 1, "history": 0}, need=len(ch) + 5)
        for p in d.get("products") or []:
            imgs = [{"l": x} for x in (p.get("imagesCSV") or "").split(",") if x]
            if not imgs and p.get("images"):
                imgs = [{"l": x.get("l") or x.get("m"), "variant": x.get("variant")} for x in p["images"] if x.get("l") or x.get("m")]
            extra[p["asin"]] = {"title": p.get("title"), "brand": p.get("brand"), "color": p.get("color"), "size": p.get("size"),
                                "scent": p.get("scent"), "style": p.get("style"), "model": p.get("model"), "partNumber": p.get("partNumber"),
                                "upcList": p.get("upcList"), "eanList": p.get("eanList"), "parentAsin": p.get("parentAsin"),
                                "features": p.get("features"), "images": imgs}
        jsave(f"{work}/keepa_extra.json", extra)
        print(f"  {min(i + 100, len(todo))}/{len(todo)}", flush=True)


def _skus():
    return {r["product_id"]: r for r in C.hub("GET", "/api/engine/store-skus", timeout=300)["rows"]}


def screen(work):
    pairs = [p.split("|") for p in jload(f"{work}/rest_pairs.json")]
    d = f"{work}/screen"
    os.makedirs(d, exist_ok=True)
    if not pairs:
        print("0 pairs to screen")
        return
    skus = _skus()
    names = {p["productId"]: p for ps in jload(f"{work}/brands.json").values() for p in ps}
    meta, amz = _amazon(sorted({a for a, _ in pairs}), work)
    cat = {}
    for a in sorted({a for a, _ in pairs}):
        for r in C.hub("GET", "/api/engine/sephora/catalog", params={"asin": a})["rows"]:
            cat[(a, r["product_id"])] = r
    cases = []
    for a, pid in pairs:
        m, r, pg = meta.get(a, {}), cat.get((a, pid), {}), amz.get(a) or {}
        ss = r.get("store_size")
        sp = names.get(pid) or {}
        cases.append({"id": f"{a}|{pid}", "asin": a, "product_id": pid,
                      "amazon": {"title": pg.get("title") or m.get("title"), "selected_variation": pg.get("selected"), "details": pg.get("details"),
                                 "bullets": (pg.get("bullets") or m.get("features") or [])[:4], "keepa_color": m.get("color"), "keepa_size": m.get("size"),
                                 "keepa_scent": m.get("scent"), "model": m.get("model"),
                                 # MAIN photo first, large or medium size (Nov 24, 2026: B0CFM41RR9's serum photos had
                                 # no large size, so only a third photo of another product was used)
                                 "main_image": next((i.get("l") or i.get("m") for i in _photos(m) if i.get("variant") == "MAIN"), None)
                                               or next((i.get("l") or i.get("m") for i in _photos(m)), None) or pg.get("image")},
                      "sephora": {"name": r.get("sephora_name") or sp.get("name"), "brand": r.get("brand"),
                                  "saved_item": ss if isinstance(ss, str) else None,
                                  "items": [" · ".join(x for x in (s.get("value"), s.get("desc"), s.get("size")) if x)[:90] + (f" ${s['price']}" if s.get("price") else "")
                                            for s in (skus.get(pid) or {}).get("skus", [])][:40],
                                  "price_range": [sp.get("price_lo"), sp.get("price_hi")]}})
    for i in range(0, len(cases), 50):
        n = f"{i // 50 + 1:03d}"
        jsave(f"{d}/case_{n}.json", cases[i:i + 50])
        os.makedirs(f"{d}/work_{n}", exist_ok=True)
        _fill("screen_prompt.md", f"{d}/prompt_{n}.txt", CASES=f"{d}/case_{n}.json", OUT=f"{d}/screen_{n}.json", WORK=f"{d}/work_{n}")
        print(f"{d}/prompt_{n}.txt")
    print(len(cases), "cases;", sum(1 for c in cases if amz.get(c["asin"])), "with a Chrome page")


def screen_item_sku(it, skus):
    """The one Sephora item a screen label names ('Candy · sheer pink · natural · 0.33 oz / 9.5 g $38.0', '1.7 oz / 50 ml'):
    shade name = the item's value and size = its size. Items with the same value and size are interchangeable (the
    cheaper is taken). Returns {sku_id, size, price} or None."""
    norm = lambda x: re.sub(r"[^a-z0-9.]+", "", str(x or "").lower())
    seg = [q.strip() for q in re.sub(r"\s*\$[\d.,]+\s*$", "", it).split("·") if q.strip()] or [it]
    first, last, hits = norm(seg[0]), norm(seg[-1]), []
    for x in skus:
        v, sz, v0 = norm(x.get("value")), norm(x.get("size")), norm(str(x.get("value") or "").split(" - ")[0])
        if (first in (v, v0) or (len(seg) == 1 and first == sz)) and (len(seg) == 1 or not sz or last in (sz, v)):
            hits.append(x)
    if len({(norm(h.get("value")), norm(h.get("size"))) for h in hits}) != 1:
        return None
    x = min(hits, key=lambda h: C.fnum(h.get("price")) or 9e9)
    if not (str(x["sku_id"]).isdigit() and C.fnum(x.get("price"))):
        return None
    lab = " · ".join(v for v in [x.get("value") if x.get("value") != x.get("size") else None, x.get("size")] if v) or it
    return {"sku_id": str(x["sku_id"]), "size": lab[:60], "price": C.fnum(x["price"])}


def collect(work):
    d = f"{work}/screen"
    want = jload(f"{work}/rest_pairs.json") or []
    got = {}
    for f in sorted(glob.glob(f"{d}/screen_*.json")):
        try:
            for r in json.load(open(f)):
                got[r["id"]] = r
        except Exception as e:  # noqa: BLE001
            print(f, "unreadable:", e)
    passed = [p for p in want if p in got and got[p]["call"] == "same"]
    flagged = [p for p in want if p not in passed]
    notes = {p: f"{got[p]['call']}: {got[p]['why']}" + (f" (item: {got[p]['item']})" if got[p].get("item") else "") for p in flagged if p in got}
    jsave(f"{work}/flagged.json", flagged)
    jsave(f"{work}/notes.json", notes)
    jsave(f"{work}/passed.json", passed)
    up = []
    for p in passed:
        a, pid = p.split("|")
        r = next((x for x in C.hub("GET", "/api/engine/sephora/catalog", params={"asin": a})["rows"] if x["product_id"] == pid), None)
        if r and r.get("match_ok") is None:
            up.append({"asin": a, "product_id": pid, "active": r["active"],
                       "extra": {**(r.get("extra") or {}), "screened": {"date": C.today().isoformat(), "call": "same", "why": got[p].get("why"),
                                                                   **({"item": got[p]["item"]} if got[p].get("item") else {})}}})
    if up:
        C.hub_upsert_catalog(up)
    # The screen's item becomes the pair's store size when it names exactly one Sephora item (Nov 23, 2026): kept only as
    # a note, the daily job didn't know the size and priced a 50 mL pair at the Sale page's 20 mL mini price.
    sized = 0
    lists = {r["product_id"]: r["skus"] for r in C.hub("GET", "/api/engine/store-skus", timeout=300)["rows"] if r.get("store") == "Sephora"}
    for u in up:
        it, L = u["extra"]["screened"].get("item"), lists.get(u["product_id"])
        x = screen_item_sku(it, L) if it and L else None
        if x:
            C.hub("POST", "/api/engine/sephora/catalog/mark", {"asin": u["asin"], "product_id": u["product_id"], "store_size": x})
            sized += 1
    print(len(want), "screened:", dict(collections.Counter(r["call"] for r in got.values())), "| no answer", sum(1 for p in want if p not in got),
          "| passed (recorded)", len(up), "| sized", sized, "| to research", len(flagged))


def _photos(m):
    """Keepa photo records with a file, MAIN first (large 'l' or medium 'm')."""
    ims = [i for i in (m.get("images") or []) if isinstance(i, dict) and (i.get("l") or i.get("m"))]
    return sorted(ims, key=lambda i: i.get("variant") != "MAIN")


def research(work):
    pairs = (jload(f"{work}/near_pairs.json") or []) + (jload(f"{work}/flagged.json") or [])
    notes = jload(f"{work}/notes.json") or {}
    d = f"{work}/research"
    os.makedirs(d, exist_ok=True)
    if not pairs:
        print("0 pairs to research")
        return
    bought = collections.defaultdict(list)
    for x in jload(f"{work}/bought.json") or []:
        bought[x["asin"]].append({"sephora_product_id": x["pid"], "what": x["note"]})
    brands = jload(f"{work}/brands.json")
    brand_of = {p["productId"]: b for b, ps in brands.items() for p in ps}
    skus = _skus()
    sk = lambda pid: [{k: x.get(k) for k in ("sku_id", "value", "size", "price", "desc", "oos")} for x in (skus.get(pid) or {}).get("skus", [])]
    by = collections.defaultdict(list)
    for p in pairs:
        a, pid = p.split("|")
        by[a].append(pid)
    meta, amz = _amazon(sorted(by), work)
    # Sephora item images saved in Nick's old-MacBook Chrome (Nov 24, 2026): download the swatch / product shot of every
    # item of the pairs to decide, so the research can compare shades without loading sephora.com
    imgdir = f"{d}/sephora_images"
    os.makedirs(imgdir, exist_ok=True)
    want = sorted({str(x.get("sku_id")) for pids in by.values() for pid in pids for x in (skus.get(pid) or {}).get("skus", [])})
    have = {}
    for i in range(0, len(want), 200):
        have.update(C.hub("GET", "/api/engine/sku-images", params={"skus": ",".join(want[i:i + 200])}) or {})
    files = {}
    for sku, kinds in have.items():
        for kind in kinds:
            fn = f"{imgdir}/{sku}_{kind}.jpg"
            if not os.path.exists(fn):
                r = subprocess.run(["curl", "-sf", "-m", "60", "-H", f"X-Engine-Key: {os.environ['ENGINE_KEY']}", "-o", fn,
                                    f"{C.HUB}/api/engine/sku-image/{sku}/{kind}"], capture_output=True)
                if r.returncode != 0:
                    continue
            files.setdefault(sku, {})[kind] = fn
    sk_img = lambda pid: [{**x, **({"image_files": files[str(x["sku_id"])]} if str(x["sku_id"]) in files else {})} for x in sk(pid)]
    cases = []
    for a, pids in by.items():
        rows = C.hub("GET", "/api/engine/sephora/catalog", params={"asin": a})["rows"]
        b = next((brand_of[p] for p in pids if p in brand_of), None)
        m = meta.get(a, {})
        cases.append({"asin": a, "amazon_url": f"https://www.amazon.com/dp/{a}", "amazon_page_read_in_chrome_today": amz.get(a),
                      "nick_bought_from_sephora": bought.get(a) or None,
                      "keepa": {k: m.get(k) for k in ("title", "brand", "color", "size", "scent", "style", "model", "partNumber", "upcList", "eanList", "parentAsin", "variations")},
                      "image_ids": [i.get("l") or i.get("m") for i in _photos(m)][:8] or [x for x in [(amz.get(a) or {}).get("image")] if x],
                      "pairs_to_decide": [{"product_id": pid, "sephora_name": next((r["sephora_name"] for r in rows if r["product_id"] == pid), None), "skus": sk_img(pid),
                                           "saved_item": next(({"sku_id": r.get("store_sku_id"), "label": r.get("store_size")} for r in rows if r["product_id"] == pid), None),
                                           "quick_screen_note": notes.get(f"{a}|{pid}")} for pid in pids],
                      "brand_sephora_products": [{"product_id": p["productId"], "name": p["name"], "price_lo": p.get("price_lo"), "price_hi": p.get("price_hi"),
                                                  "skus": sk(p["productId"])} for p in brands.get(b, [])]})
    for i in range(0, len(cases), 20):
        n = f"{i // 20 + 1:03d}"
        jsave(f"{d}/case_{n}.json", cases[i:i + 20])
        os.makedirs(f"{d}/work_{n}", exist_ok=True)
        _fill("research_prompt.md", f"{d}/prompt_{n}.txt", CASES=f"{d}/case_{n}.json", OUT=f"{d}/results_{n}.json", WORK=f"{d}/work_{n}")
        print(f"{d}/prompt_{n}.txt")
    print(len(cases), "ASINs,", len(pairs), "pairs;", sum(1 for c in cases if c["amazon_page_read_in_chrome_today"]), "with an Amazon page;",
          len(files), "Sephora items with images")


def finish(work, n):
    d = f"{work}/research"
    R = jload(f"{d}/results_{n}.json")
    C_ = {c["asin"]: c for c in jload(f"{d}/case_{n}.json")}
    bought = {(x["asin"], x["pid"]): x["note"] for x in jload(f"{work}/bought.json") or []}
    bad = []
    for r in R:
        c = C_.get(r.get("asin"))
        if not c:
            bad.append((r.get("asin"), "not in file")); r["confidence"] = "low"; continue
        allp = {p["product_id"]: p for p in c["brand_sephora_products"]}
        for p in c["pairs_to_decide"]:
            allp.setdefault(p["product_id"], p)
        cur = {p["product_id"] for p in c["pairs_to_decide"]}
        tgt = r.get("correct_product_id") if r["verdict"] == "other" else r["product_id"]
        prob = None
        if r["product_id"] not in cur:
            prob = "pair not in case"
        elif r.get("correct_product_id") and r["correct_product_id"] not in allp:
            prob = "unknown product " + r["correct_product_id"]
        elif r.get("sku_id") and tgt in allp and allp[tgt].get("skus") and r["sku_id"] not in {s["sku_id"] for s in allp[tgt]["skus"]}:
            prob = f"sku {r['sku_id']} not in {tgt}"
        if r.get("confidence") == "high" and not r.get("sources"):
            r["sources"] = [c["amazon_url"] + " (live page read in Nick's Chrome, photos)"]
        if not prob and r["verdict"] in ("other", "not_sold") and (r["asin"], r["product_id"]) in bought:
            prob = f"Nick bought this ASIN from this Sephora page ({bought[(r['asin'], r['product_id'])]}); a wrong mark needs his OK"
        if prob:
            bad.append((r["asin"], prob)); r["confidence"] = "medium"; r["check"] = prob
    jsave(f"{d}/results_{n}.checked.json", R)
    # undo: every touched ASIN's pairs as they were, kept in the Hub (a scheduled session's files don't survive it)
    before = [{"asin": a, "before": C.hub("GET", "/api/engine/sephora/catalog", params={"asin": a})["rows"]} for a in sorted({r["asin"] for r in R if r.get("asin")})]
    C.hub("PUT", f"/api/engine/docs/sephora_jobs/check_pairs_undo/{(jload(f'{work}/meta.json') or {}).get('started', C.today().isoformat())}_{n}", {"items": before})
    print(f"research {n}: {len(R)} answers", dict(collections.Counter((r["verdict"], r.get("confidence")) for r in R)), "| problems:", bad, flush=True)
    out = subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(HERE), "verify_apply.py"), "--results", f"{d}/results_{n}.checked.json"],
                         capture_output=True, text=True, timeout=1500)
    lines = [l for l in (out.stdout + out.stderr).splitlines() if re.search(r"applied|bundle|FAILED|not in this|Traceback|Error", l)]
    print("\n".join(lines))
    open(f"{d}/applied_{n}.txt", "w").write(out.stdout + out.stderr)


def summary(work):
    m = jload(f"{work}/meta.json") or {}
    R = []
    for f in sorted(glob.glob(f"{work}/research/results_*.checked.json")):
        R += json.load(open(f))
    c = collections.Counter(("applied" if r.get("confidence") == "high" and r.get("sources") and r["verdict"] != "cant_tell" else "for Nick", r["verdict"]) for r in R)
    opens = [f"https://www.amazon.com/dp/{r['asin']}: {r.get('evidence', '')[r.get('evidence', '').find('OPEN:'):][:200]}" for r in R
             if "OPEN:" in (r.get("evidence") or "") and not (r.get("confidence") == "high" and r.get("sources"))]
    print(json.dumps({"meta": m, "screen_passed": len(jload(f"{work}/passed.json") or []), "researched": len(R), "results": {f"{a} {v}": n for (a, v), n in c.items()},
                      "open_questions": opens[:15]}, indent=1))


if __name__ == "__main__":
    C.need_env("ENGINE_KEY")
    cmd, work = sys.argv[1], os.path.abspath(sys.argv[2])
    os.makedirs(work, exist_ok=True)
    if cmd == "prep":
        prep(work, int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 300, "--queue" in sys.argv)
    elif cmd == "wait":
        wait(work, float(sys.argv[3]) if len(sys.argv) > 3 else 240)
    elif cmd == "screen":
        screen(work)
    elif cmd == "collect":
        collect(work)
    elif cmd == "research":
        research(work)
    elif cmd == "finish":
        finish(work, sys.argv[3])
    elif cmd == "keepa":
        fill_keepa(work)
    elif cmd == "summary":
        summary(work)
    else:
        sys.exit(__doc__)
