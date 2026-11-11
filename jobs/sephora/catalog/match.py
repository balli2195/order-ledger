#!/usr/bin/env python3
"""Match a brand's Amazon ASINs (raw/keepa/<slug>.json) to its Sephora products (raw/sephora/<slug>.json).

Usage: python3 -I match.py [slug ...]   -> writes raw/match/<slug>.json (no network)

Method (per brand):
- Normalize titles: lowercase, strip accents, drop brand words / sizes / units / filler; light plural stemming.
- Vocabulary = words used in the brand's Sephora product names, weighted by IDF across those names.
- Score(ASIN, product) = 0.55 * precision (IDF share of the ASIN's brand-vocabulary words that the Sephora name has;
  shade words from Keepa's color field are ignored) + 0.45 * recall (IDF share of the Sephora name found in the title),
  minus penalties when the product type (blush vs bronzer, EDP vs EDT, ...) disagrees or when one side is a
  mini/travel, set/kit, or refill and the other is not.
- Variation children (same parentAsin) inherit the family's best product unless they have a strong own match.
- Confidence: high / medium / low by score and margin over the runner-up; below LOW_MIN -> unmatched.
- Learned rules (match_rules.py, from Nick's wrong-match marks): a match that breaks one is capped at low.
"""
import json, math, os, re, sys, unicodedata
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CAT = os.environ.get("SEPH_DATA") or os.path.join(os.path.dirname(HERE), ".data")   # data lives outside git (see ../.gitignore)

FILLER = set("""a an and the with for of by in on to from new authentic original genuine beauty cosmetics makeup
skincare skin care women woman men unisex fl oz ml g gram grams ounce ounces count ct pack pcs pc piece pieces size
full travel mini deluxe sample usa us edition free shipping sealed box boxed nib item each x one inch
de la le les du des pour l d""".split())
# 'mini', 'travel', 'full', 'size' handled through flags, not tokens.
TYPE_GROUPS = {
    "blush": {"blush"}, "bronzer": {"bronzer"}, "highlighter": {"highlighter", "luminizer", "illuminator"},
    "contour": {"contour"},
    "lipstick": {"lipstick", "rouge"}, "gloss": {"gloss", "lacquer"}, "lipbalm": {"balm", "baume"},
    "liner": {"liner", "eyeliner", "pencil", "crayon", "khol", "kohl", "kajal", "fluidline"},
    "mascara": {"mascara"}, "foundation": {"foundation"}, "concealer": {"concealer"},
    "powder": {"powder"}, "primer": {"primer"},
    "eyeshadow": {"eyeshadow", "shadow"}, "palette": {"palette"}, "brow": {"brow", "eyebrow"},
    "brush": {"brush", "sponge", "puff", "applicator"},
    "cleanser": {"cleanser", "cleansing", "wash"}, "toner": {"toner"}, "serum": {"serum"},
    "moisturizer": {"moisturizer", "moisturiser"}, "mask": {"mask"}, "sunscreen": {"sunscreen"},
    "shampoo": {"shampoo"}, "conditioner": {"conditioner"}, "dryshampoo": {"dry-shampoo"},
    "deodorant": {"deodorant"}, "bodylotion": {"lotion", "emulsion"}, "bath": {"shower", "soap", "bath"},
    "nail": {"nail", "vernis", "polish"}, "patch": {"patch", "patches"},
    "fragrance": {"parfum", "perfume", "toilette", "cologne", "edp", "edt", "fragrance", "extrait", "eau"},
}
# pairs of types that are compatible (a product may carry both words)
TYPE_OK = {frozenset(x) for x in [("lipstick", "gloss"), ("lipstick", "lipbalm"), ("gloss", "lipbalm"), ("powder", "foundation"),
           ("powder", "blush"), ("powder", "bronzer"), ("powder", "highlighter"), ("eyeshadow", "palette"),
           ("blush", "palette"), ("highlighter", "palette"), ("contour", "palette"), ("bronzer", "palette"), ("bronzer", "contour"),
           ("liner", "brow"), ("powder", "brow"), ("moisturizer", "sunscreen"), ("moisturizer", "foundation"),
           ("cleanser", "bath"), ("moisturizer", "bodylotion"), ("moisturizer", "lipbalm"),
           ("moisturizer", "mask"), ("concealer", "foundation"), ("primer", "eyeshadow"), ("liner", "lipstick"),
           ("cleanser", "mask"), ("primer", "moisturizer"),("blush", "lipstick"), ("blush", "bronzer"), ("highlighter", "bronzer"),
           ("moisturizer", "blush"), ("moisturizer", "bronzer"), ("moisturizer", "highlighter"), ("serum", "foundation"),
           ]}
FRAG_CONC = [("extrait", r"\bextrait\b"), ("edp", r"eau de parfum|\bedp\b"), ("edt", r"eau de toilette|\bedt\b"),
             ("cologne", r"eau de cologne|\bcologne\b"), ("parfum", r"\bparfum\b(?! spray)(?!.*eau de)"),
             ("mist", r"body mist|hair mist|hair perfume|perfume mist|body spray|fragrance mist")]
MINI_RE = re.compile(r"\b(mini|minis|miniature|minature|travel[- ]size|travel|deluxe sample|sample|samples|trial|vial|vials)\b")
SET_RE = re.compile(r"\b(set|kit|trio|duo|bundle|coffret|gift set|vault|quad|calendar|advent|\d+\s?pc|\d+\s?piece)\b")
REFILL_RE = re.compile(r"\brefills?\b(?!able)")
PACK_RE = [re.compile(r"\bpack of (\d+)\b"), re.compile(r"\b(\d+)[- ]?(?:pack|pk)\b"), re.compile(r"\bquantity of (\d+)\b"),
           re.compile(r"\b(\d+)\s?(?:pcs|pieces)\b"), re.compile(r"\b(\d)\s?x\s?\d*\.?\d+\s?(?:ml|oz|fl|g)\b"),
           re.compile(r"\b(\d+) full[- ]size\b"), re.compile(r"\b(double) deal\b")]
SIZE_RE = re.compile(r"(\d*\.?\d+)\s*(fl\.? ?oz|fluid ounces?|oz|ounces?|ml|mls|milliliters?|g|grams?|gr|mg|l|liters?)\b")
HIGH_MIN, MED_MIN, LOW_MIN = 0.70, 0.52, 0.38
REC_MIN = {"high": 0.40, "medium": 0.28, "low": 0.18}
# descriptive / product-type words: weigh 0.35 so that line names (Allure, Hoola, Gimme Brow) decide the match
GENERIC = set("""lip lips eye eyes face facial body hair cheek brow nail skin gel cream creme liquid matte satin shine
sheer dewy glow glossy hydrating moisturizing nourishing longwear long wear wearing lasting waterproof spray stick oil
mist tint tinted balm after shave lotion wash color colour finish plumping volumizing lengthening curling setting
blush bronzer highlighter powder foundation concealer primer mascara liner eyeliner pencil lipstick gloss serum
toner cleanser mask moisturizer eyeshadow shadow palette brush eau de parfum toilette perfume fragrance cologne
spf daily all day over hour hr 24hr 12hr 24 natural soft smooth radiant buildable lightweight full coverage
treatment repair daily clear brightening firming refillable new formula shampoo conditioner deodorant soap bath
shower women men pour homme femme vaporisateur vapo natural""".split())


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def base_text(s):
    s = strip_accents(s or "").lower().replace("’", "'")
    s = re.sub(r"\bn\s*[°ºo]\s*[._]?\s*(\d)", r"no\1", s)          # N°5, No.5, No_5 -> no5
    s = re.sub(r"\bchanel\s+(5|19|22)\b", r"chanel no\1", s)
    s = re.sub(r"m\s*[·.]\s*a\s*[·.]\s*c\s*\.?\s*", "mac", s)          # M·A·C / M.A.C. -> mac
    s = re.sub(r"\bedp\b", " eau de parfum ", s)
    s = re.sub(r"\bedt\b", " eau de toilette ", s)
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)
    return s


def tokens(s, drop):
    s = SIZE_RE.sub(" ", base_text(s))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    out = []
    for w in s.split():
        if w in FILLER or w in drop or w.isdigit() or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("es") and w[:-2] + "e" not in ("patche",):
            w = w[:-1] if w.endswith("ses") else w
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def ptypes(text):
    bt = base_text(text).replace("dry shampoo", "dry-shampoo")
    t = set(re.sub(r"[^a-z0-9-]+", " ", bt).split())
    t |= {w[:-1] for w in t if w.endswith("s")}
    found = {k for k, ws in TYPE_GROUPS.items() if t & ws}
    if "lip" in t and ("oil" in t or "tint" in t or "glaze" in t) and not found & {"lipstick", "liner"}:
        found.add("gloss")
    if "hair" in t and ({"mist", "perfume", "parfum"} & t):
        found.discard("fragrance"); found.add("hairmist")
    if ("mist" in t and ({"body", "moisture", "moisturizing"} & t)) or ("body" in t and "spray" in t and "fragrance" not in found):
        found.discard("fragrance"); found.add("bodymist")
    if "fragrance" in found:   # "Bleu de Chanel Eau de Parfum" style names are fragrances whatever else they say
        found -= {"lipstick"}
    return found


FORMS = {"liquid": {"liquid", "fluid"}, "powder": {"powder", "pressed", "baked", "loose"}, "stick": {"stick", "balm"},
         "cream": {"cream", "creme"}}


def forms(text):
    t = set(re.sub(r"[^a-z0-9]+", " ", base_text(text)).split())
    return {k for k, ws in FORMS.items() if t & ws}


def type_conflict(a, b):
    """True when every type on one side clashes with every type on the other."""
    if not a or not b or a & b:
        return False
    return not any(frozenset((x, y)) in TYPE_OK for x in a for y in b)


def conc(text):
    t = base_text(text)
    return {k for k, rx in FRAG_CONC if re.search(rx, t)}


def flags(text):
    t = base_text(text)
    t = re.sub(r"sephora collection|sephora favorites|\bcollection (?=\d|for)", " ", t)
    t = re.sub(r"\(pack of 1\)|pack of 1\b", " ", t)
    return {"mini": bool(MINI_RE.search(t)), "set": bool(SET_RE.search(t)), "refill": bool(REFILL_RE.search(t))}


def pack_qty(title, size):
    t = base_text((title or "") + " | " + (size or ""))
    if re.search(r"double[- ]ended|dual[- ]ended|2[- ]in[- ]1", t):
        t = re.sub(r"\b2\s?x\s?", " ", t)
    best = 1
    for rx in PACK_RE:
        for m in rx.finditer(t):
            n = 2 if m.group(1) == "double" else int(m.group(1))
            if 1 < n <= 12:
                best = max(best, n)
    return best


def size_ml(text):
    """Largest size mentioned, in ml (or g as ml-equivalent); None if none."""
    vals = []
    for m in SIZE_RE.finditer(base_text(text)):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        u = m.group(2)
        if u.startswith("fl") or u.startswith("fluid") or u.startswith("oz") or u.startswith("ounce"):
            v *= 29.57
        elif u in ("l", "liter", "liters"):
            v *= 1000
        elif u == "mg":
            v /= 1000
        if 0.2 <= v <= 3000:
            vals.append(round(v, 1))
    return max(vals) if vals else None


def match_brand(slug, cfg_b):
    import match_rules as MR   # rules learned from Nick's wrong-match marks (written by the weekly job before matching)
    rules = MR.load()
    seph = json.load(open(os.path.join(CAT, "raw", "sephora", slug + ".json")))
    keepa = json.load(open(os.path.join(CAT, "raw", "keepa", slug + ".json")))
    drop = set()
    for n in [cfg_b["name"]] + cfg_b.get("sephora_names", []) + cfg_b.get("keepa_brands", []) + cfg_b.get("strip", []):
        drop |= set(re.sub(r"[^a-z0-9]+", " ", base_text(n)).split())
    drop -= {"rare", "fresh", "merit", "one", "size"} if False else set()
    products = seph["products"]
    P = []
    for p in products:
        tk = tokens(p["name"], drop)
        head = tk[:2]
        P.append({"p": p, "tok": tk, "set": set(tk), "types": ptypes(p["name"]), "conc": conc(p["name"]), "flags": flags(p["name"]),
                  "head": head, "forms": forms(p["name"]), "multi": bool(p.get("price_lo") and p.get("price_hi") and p["price_hi"] > p["price_lo"])})
    N = max(len(P), 1)
    df = Counter(w for x in P for w in x["set"])
    for x in P:   # line name = first distinctive (rare, non-generic) word among the first three of the Sephora name
        x["lead"] = next((w for w in x["tok"][:2] if w not in GENERIC and df[w] <= 4), None)
    idf = {w: math.log(1 + N / c) * (0.35 if w in GENERIC else 1.0) for w, c in df.items()}
    vocab = set(df)

    asins = keepa["asins"]
    # Amazon-side word weights: words common across this brand's Amazon titles (marketing filler) weigh little
    def with_joins(tk):
        return set(tk) | {tk[i] + tk[i + 1] for i in range(len(tk) - 1)}
    A_tok = {a["asin"]: with_joins(tokens(a["title"] or "", drop)) for a in asins}
    NA = max(len(asins), 1)
    adf = Counter(w for ws in A_tok.values() for w in ws)
    aw = {w: math.log(1 + NA / c) * (0.35 if w in GENERIC else 1.0) for w, c in adf.items()}

    def score(a, x):
        title = a["title"] or ""
        shade = set(tokens(" ".join(str(v) for v in [a.get("color")] + list((a.get("variation_attrs") or {}).values()) if v), drop))
        at = A_tok[a["asin"]]
        av = (at & vocab) - (shade - x["set"])
        if not av or not x["set"]:
            return 0.0, "", (0.0, None)
        prec = sum(idf[w] for w in av & x["set"]) / sum(idf[w] for w in av)
        rest = at - shade
        prec_all = sum(aw[w] for w in rest & x["set"]) / max(sum(aw[w] for w in rest), 1e-9)
        rec = sum(idf[w] for w in at & x["set"]) / sum(idf[w] for w in x["set"])
        s = 0.30 * prec + 0.25 * min(1.0, prec_all * 1.5) + 0.45 * rec
        why = []
        at_types = ptypes(title)
        if type_conflict(at_types, x["types"]):
            s -= 0.60; why.append("type differs (%s vs %s)" % ("/".join(sorted(at_types)), "/".join(sorted(x["types"]))))
        elif at_types and not x["types"] and not (at_types & {"fragrance"}):
            s -= 0.08
        fa, fx = forms(title), x["forms"]
        if ({"liquid", "powder"} <= (fa | fx)) and not (fa & fx) and fa and fx:
            s -= 0.30; why.append("form differs (%s vs %s)" % ("/".join(sorted(fa)), "/".join(sorted(fx))))
        for ex, pen in (("brush", 0.45), ("palette", 0.2)):
            if (ex in at_types) != (ex in x["types"]):
                s -= pen; why.append(f"{ex} mismatch")
        ac = conc(title)
        if ac and x["conc"] and not (ac & x["conc"]):
            s -= 0.35; why.append("concentration differs")
        af = flags(title + " " + (a.get("size") or ""))
        for f, pen in (("mini", 0.15), ("set", 0.25), ("refill", 0.3)):
            if af[f] != x["flags"][f]:
                if f == "set" and af[f] and not x["flags"]["set"] and pack_qty(title, a.get("size")) > 1 \
                        and not re.search(r"\b(duo|trio|quad|kit|gift|coffret|collection|vault)\b", base_text(title)):
                    continue
                if f == "mini" and x["multi"]:
                    continue   # Sephora sells several sizes; size_guess picks the price
                s -= pen; why.append(f"{f} mismatch")
        cap = None   # line-name check: the first words of the Sephora name must be in the Amazon title
        if x["lead"] and x["lead"] not in at:
            cap = "low"; why.append(f"line word '{x['lead']}' missing")
        elif x["head"] and not all(w in at for w in x["head"]):
            cap = "medium"
        return s, ", ".join(why), (rec, cap)

    VARIANT_WORDS = {"travel", "spray", "mini", "set", "duo", "trio", "refill", "refillable", "intense", "jumbo", "deluxe"}
    for x in P:
        x["core"] = frozenset(w for w in x["set"] if w not in GENERIC and w not in VARIANT_WORDS)

    own = {}
    for a in asins:
        sc = sorted(((score(a, x), i) for i, x in enumerate(P)), key=lambda t: -t[0][0])
        top = sc[0] if sc else ((0, "", (0, None)), None)
        if top[1] is not None:
            core = P[top[1]]["core"]
            # near-duplicate Sephora listings (same line, other size/format): prefer the one whose mini/set flags agree
            af = flags((a["title"] or "") + " " + (a.get("size") or ""))
            twins = [t for t in sc if P[t[1]]["core"] == core and t[0][0] >= top[0][0] - 0.12]
            agree = [t for t in twins if P[t[1]]["flags"]["mini"] == af["mini"] and P[t[1]]["flags"]["set"] == af["set"]]
            if agree:
                top = agree[0]
            others = [t for t in sc if P[t[1]]["core"] != core]
            second = others[0][0][0] if others else 0
        else:
            second = 0
        own[a["asin"]] = {"best": top[1], "s": top[0][0], "why": top[0][1], "rec": top[0][2], "margin": top[0][0] - second,
                          "scores": {i: v for v, i in sc[:8]}}

    def conf_of(s, margin, rc):
        rec, cap = rc
        c = None
        if s >= HIGH_MIN and margin >= 0.12:
            c = "high"
        elif s >= MED_MIN and margin >= 0.06:
            c = "medium"
        elif s >= LOW_MIN:
            c = "low"
        order = ["high", "medium", "low", None]
        while c is not None and rec < REC_MIN[c]:      # the Sephora name's distinctive words must be in the title
            c = order[order.index(c) + 1]
        if cap and c and order.index(c) < order.index(cap):
            c = cap
        if cap == "low" and rec < 0.5:
            c = None
        return c

    for o in own.values():
        o["conf"] = conf_of(o["s"], o["margin"], o["rec"])
        # clear winner: line name fully present, no type/size/set conflicts, far ahead of every other product line
        if o["best"] is not None and o["rec"][1] is None and not o["why"] and o["rec"][0] >= 0.25:
            if o["s"] >= 0.45 and o["margin"] >= 0.25 and o["conf"] in (None, "low"):
                o["conf"] = "medium"; o["promoted"] = True
            if o["s"] >= 0.55 and o["margin"] >= 0.35 and o["conf"] == "medium":
                o["conf"] = "high"; o["promoted"] = True
    # variation families: children vote for one product
    fam = defaultdict(list)
    for a in asins:
        if a.get("parentAsin"):
            fam[a["parentAsin"]].append(a["asin"])
    fam_best = {}
    for par, kids in fam.items():
        votes = Counter()
        for k in kids:
            o = own[k]
            if o["conf"] in ("high", "medium"):
                votes[o["best"]] += o["s"]
        if votes:
            b_, v = votes.most_common(1)[0]
            n_hi = sum(1 for k in kids if own[k]["best"] == b_ and own[k]["conf"] == "high")
            fam_best[par] = (b_, n_hi, v / sum(votes.values()))

    matches, unmatched = [], []
    for a in asins:
        o = own[a["asin"]]
        best, s, margin, why, conf = o["best"], o["s"], o["margin"], o["why"], o["conf"]
        reason = []
        par = a.get("parentAsin")
        if par in fam_best and len(fam[par]) > 1:
            fb, n_hi, share = fam_best[par]
            if fb != best and conf in (None, "low"):
                fs, fwhy, frec = o["scores"].get(fb, (0, "", 0))
                # inherit only when the child's own title is at least loosely about that product and types agree
                if fs >= 0.30 and "type differs" not in fwhy and "concentration" not in fwhy and share >= 0.6:
                    best, why = fb, fwhy
                    conf = "medium" if (n_hi >= 2 and fs >= 0.45) else "low"
                    reason.append(f"inherits variation family {par} (own score {fs:.2f})")
                    s = fs
            elif fb == best and conf == "low" and n_hi >= 2 and share >= 0.6:
                conf = "medium"
                reason.append(f"variation family {par} agrees")
        if conf is None or best is None:
            unmatched.append({**{k: a.get(k) for k in ("asin", "title", "color", "size", "parentAsin", "buybox", "monthlySold", "offers_fba")},
                              "best_guess": P[best]["p"]["name"] if best is not None else None, "best_score": round(o["s"], 3)})
            continue
        x = P[best]
        if conf and ("set mismatch" in why or "refill mismatch" in why):
            conf = "low"
        elif conf == "high" and "mini mismatch" in why and x["p"].get("price_lo") == x["p"].get("price_hi"):
            conf = "medium"
        elif conf == "high" and x["flags"]["set"]:
            conf = "medium"   # sets/kits change contents every season under similar names: verify before buying
            reason.append("set: verify contents")
        broken = MR.violations(rules, cfg_b.get("name"), a.get("title"), a.get("size"), a.get("color"), x["p"]["name"],
                               sephora_multi=x["multi"])
        if broken:   # same kind of mistake Nick has marked before: never proposed until he says OK
            conf = "low"
            reason.extend(broken)
        common = sorted(set(tokens(a["title"] or "", drop)) & x["set"], key=lambda w: -idf[w])
        reason.insert(0, f"score {s:.2f} (margin {margin:.2f}); shared words: {' '.join(common[:6])}"
                         + ("; clear winner by margin" if o.get("promoted") else ""))
        if why:
            reason.append(why)
        pq = pack_qty(a["title"], a.get("size"))
        if pq == 1 and not x["flags"]["set"]:
            mm = re.search(r"\b(duo|trio)\b", base_text(a["title"] or ""))
            if mm:   # Amazon duo/trio of a single Sephora item
                pq = 2 if mm.group(1) == "duo" else 3
        if x["flags"]["set"]:
            pq = 1
        if pq > 1:
            reason.append(f"Amazon multi-pack x{pq}: cost = {pq} x Sephora price")
        matches.append({"asin": a["asin"], "productId": x["p"]["productId"], "skuId": x["p"]["skuId"],
                        "sephora_name": x["p"]["name"], "confidence": conf, "score": round(s, 3),
                        "reason": "; ".join(reason), "pack_qty": pq, "keepa": a})
    matched_pids = {m["productId"] for m in matches}
    unmatched_products = [{k: p[k] for k in ("productId", "skuId", "name", "url", "price_lo", "price_hi")}
                          for p in products if p["productId"] not in matched_pids]
    return {"matches": matches, "unmatched_asins": unmatched, "unmatched_products": unmatched_products,
            "sephora": seph, "keepa_meta": {k: v for k, v in keepa.items() if k != "asins"}}


def main():
    cfg = json.load(open(os.path.join(os.path.dirname(HERE), "brands.json")))
    want = set(sys.argv[1:])
    os.makedirs(os.path.join(CAT, "raw", "match"), exist_ok=True)
    for b in cfg["brands"]:
        if want and b["slug"] not in want:
            continue
        if not os.path.exists(os.path.join(CAT, "raw", "keepa", b["slug"] + ".json")):
            print(f"{b['name']}: no Keepa file yet, skipped")
            continue
        r = match_brand(b["slug"], b)
        json.dump(r, open(os.path.join(CAT, "raw", "match", b["slug"] + ".json"), "w"), indent=1)
        c = Counter(m["confidence"] for m in r["matches"])
        print(f"{b['name']}: {len(r['matches'])} matched {dict(c)}, {len(r['unmatched_asins'])} unmatched ASINs, "
              f"{len(r['unmatched_products'])} unmatched products")


if __name__ == "__main__":
    main()
