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
    "candle": {"candle", "votive"}, "diffuser": {"diffuser", "reed"},   # home scent (NEST soap is not the candle, Nick's marks)
    "fragrance": {"parfum", "perfume", "toilette", "cologne", "edp", "edt", "fragrance", "extrait", "eau", "leau"},
}
# pairs of types that are compatible (a product may carry both words)
TYPE_OK = {frozenset(x) for x in [("lipstick", "gloss"), ("lipstick", "lipbalm"), ("gloss", "lipbalm"), ("powder", "foundation"),
           ("powder", "blush"), ("powder", "bronzer"), ("powder", "highlighter"), ("eyeshadow", "palette"),
           ("blush", "palette"), ("highlighter", "palette"), ("contour", "palette"), ("bronzer", "palette"), ("bronzer", "contour"),
           ("liner", "brow"), ("powder", "brow"), ("moisturizer", "sunscreen"), ("moisturizer", "foundation"),
           ("cleanser", "bath"), ("moisturizer", "bodylotion"), ("moisturizer", "lipbalm"),
           ("moisturizer", "mask"), ("primer", "eyeshadow"), ("liner", "lipstick"),
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
           re.compile(r"\b(\d+)\s?(?:pcs|pieces)\b"), re.compile(r"\b(\d)\s?[x×]\s?\d*\.?\d+\s?(?:ml|oz|fl|g|ounces?)\b"), re.compile(r"\b(twice) as precise\b"),
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
spf daily all day over hour hr 24hr 12hr 24 16hr 16 36hr 36 48hr 48 72hr natural soft smooth radiant buildable lightweight full coverage
treatment repair daily clear brightening firming refillable new formula shampoo conditioner deodorant soap bath
shower women men pour homme femme vaporisateur vapo natural""".split())


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def base_text(s):
    s = strip_accents(s or "").lower().replace("’", "'")
    s = re.sub(r"\bn\s*[°ºo]\s*[._]?\s*(\d)", r"no\1", s)          # N°5, No.5, No_5 -> no5
    s = re.sub(r"\bchanel\s+(5|19|22)\b", r"chanel no\1", s)
    s = re.sub(r"\bafter[- ]?shave\b", "aftershave", s)   # Fahrenheit After Shave is not the EDT (round 5)
    s = re.sub(r"\beau sauvage\b", "eausauvage", s)   # Dior's Eau Sauvage is not Sauvage EDT (checker round 4)
    s = re.sub(r"\bl\s*'?\s*eau\b", "leau", s)                      # L'Eau, L Eau, LEau -> leau (N°5 L'Eau is not N°5 EDT)
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


def _small_size(text: str) -> bool:
    """A mini-sized listing that doesn't say 'mini' (15 ml, 0.5 oz, 4 g): ≤ 0.6 oz / 18 ml / 17 g."""
    t = (text or "").lower()
    for v, u in re.findall(r"(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|oz|ounce|ml|g)\b", t):
        v = float(v)
        if (u in ("ml",) and v <= 18) or (u == "g" and v <= 17) or (u not in ("ml", "g") and v <= 0.6):
            return True
    return False


def ptypes(text):
    bt = base_text(text).replace("dry shampoo", "dry-shampoo")
    t = {w.strip("-") for w in re.sub(r"[^a-z0-9-]+", " ", bt).split()}   # 'Concealer- 16 Deep' (checker round 3)
    t |= {w[:-1] for w in t if w.endswith("s")}
    found = {k for k, ws in TYPE_GROUPS.items() if t & ws}
    if "lip" in t and ("oil" in t or "tint" in t or "glaze" in t) and not found & {"lipstick", "liner"}:
        found.add("gloss")
    if "hair" in t and ({"mist", "perfume", "parfum"} & t):
        found.discard("fragrance"); found.add("hairmist")
    if ("mist" in t and ({"body", "moisture", "moisturizing"} & t)) or ("body" in t and "spray" in t and "fragrance" not in found):
        found.discard("fragrance"); found.add("bodymist")
    if not found and "cream" in t and not ({"eye", "body", "hand", "foot", "lip", "cleansing", "shave", "hair"} & t):
        found.add("moisturizer")   # a face cream with no other type word ('Black Tea Advanced Age Renewal Cream' is not a serum, Nick Nov 20)
    if "body" in t and ({"cream", "veil", "oil", "butter", "milk", "lotion", "balm", "gel", "scrub", "wash"} & t):
        found.discard("fragrance"); found.discard("moisturizer"); found.add("bodylotion")   # Coco Mademoiselle Body Cream is not the EDP
    if "fragrance" in found:   # "Bleu de Chanel Eau de Parfum" style names are fragrances whatever else they say
        found -= {"lipstick"}
    return found


FORMS = {"liquid": {"liquid", "fluid", "serum"}, "powder": {"powder", "pressed", "baked", "loose"}, "stick": {"stick", "balm"},
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
            n = 2 if m.group(1) in ("double", "twice") else int(m.group(1))
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


def match_brand(slug, cfg_b, keepa=None, exclude=None, shades=None):
    """keepa: the Amazon side ({"asins": [...]}) instead of raw/keepa/<slug>.json. exclude: {asin: {productId, ...}}
    products never proposed for that ASIN (pairs Nick marked wrong), so the next-best product is picked instead.
    shades: {productId: [SKU value, ...]} from the Hub's store_skus (Nov 19, 2026). Shade names / codes of a product
    that appear in the Amazon title ('#2 Cream Puff', 'C-Thru', '1.3N Nougat') are not counted against it, and a
    shade the product really sells adds a little: titles full of shade words used to fall below the match threshold."""
    import match_rules as MR   # rules learned from Nick's wrong-match marks (written by the weekly job before matching)
    rules = MR.load()
    seph = json.load(open(os.path.join(CAT, "raw", "sephora", slug + ".json")))
    if keepa is None:
        keepa = json.load(open(os.path.join(CAT, "raw", "keepa", slug + ".json")))
    exclude = exclude or {}
    shades = shades or {}
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
    for x in P:
        vals = shades.get(x["p"]["productId"]) or []
        names = set()
        for v in vals:
            v0 = re.split(r"\s+[-–|]\s+|\s*\(", v or "")[0]
            t = re.sub(r"[^a-z0-9.]+", " ", SIZE_RE.sub(" ", base_text(v0))).strip()
            if len(t) >= 3 and not re.fullmatch(r"[\d. ]+", t):
                names.add(t)
        x["shade_names"] = names
        x["shade_tok"] = {w for v in vals for w in tokens(v, drop)}
    N = max(len(P), 1)
    df = Counter(w for x in P for w in x["set"])
    for x in P:   # line name = first distinctive (rare, non-generic) word among the first three of the Sephora name
        x["lead"] = next((w for w in x["tok"][:2] if w not in GENERIC and df[w] <= 4), None)
    idf = {w: math.log(1 + N / c) * (0.35 if w in GENERIC else 1.0) for w, c in df.items()}
    vocab = set(df)
    # Words that tell siblings apart, from the brand's own Sephora catalog (MATCHING_PRINCIPLES §1, §5; Nov 20, 2026):
    # products sharing a line word (a non-generic word used by ≤ 6 products) are siblings; a sibling's word that this product
    # lacks names the sibling ('tendre', 'extreme', 'superleggera', 'luminous'). A title carrying one is about the sibling.
    line_w = {w for w, c in df.items() if w not in GENERIC and c <= 6}
    for x in P:
        sib = [y for y in P if y is not x and (x["set"] & y["set"] & line_w)]
        # each sibling's own words that this product lacks; it counts only when ALL of them are in the title
        x["sibs"] = [(d, y["flags"]["mini"]) for d, y in (({w for w in y["set"] - x["set"] if w not in GENERIC}, y) for y in sib) if d]

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
        sp = x.get("shade_tok") or set()
        shade = shade | (at & sp - x["set"])   # this product's own shade words in the title are shade, not mismatch
        av = (at & vocab) - (shade - x["set"])
        if not av or not x["set"]:
            return 0.0, "", (0.0, None)
        prec = sum(idf[w] for w in av & x["set"]) / sum(idf[w] for w in av)
        rest = at - shade
        prec_all = sum(aw[w] for w in rest & x["set"]) / max(sum(aw[w] for w in rest), 1e-9)
        rec = sum(idf[w] for w in at & x["set"]) / sum(idf[w] for w in x["set"])
        s = 0.30 * prec + 0.25 * min(1.0, prec_all * 1.5) + 0.45 * rec
        bt = base_text(title)
        hits = [n for n in (x.get("shade_names") or ()) if re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", bt)]
        if hits:
            s += 0.08   # the title names a shade this product sells
        why = []
        # product type from the title without the shade text ('Cream Puff' is a lip tint shade, not a brush)
        tt = bt
        for v in [a.get("color")] + list((a.get("variation_attrs") or {}).values()) + hits:
            v = base_text(str(v or "")).strip()
            if len(v) >= 3:
                tt = tt.replace(v, " ")
        at_types = ptypes(tt)
        if type_conflict(at_types, x["types"]):
            s -= 0.60; why.append("type differs (%s vs %s)" % ("/".join(sorted(at_types)), "/".join(sorted(x["types"]))))
        elif at_types and not x["types"] and not (at_types & {"fragrance"}):
            s -= 0.08
        if not flags(title)["set"]:   # (a set's title names its contents, i.e. siblings: not evidence against it)
            sh_ = shade | set(x.get("shade_tok") or ())
            am = flags(title + " " + (a.get("size") or ""))["mini"]
            # (only siblings of the listing's own kind: a Mini page's name is not evidence about a full-size listing)
            named_sib = next((d for d, ym in x.get("sibs", ()) if ym == am and d <= at and not d <= sh_), None)
            if named_sib:
                s -= 0.25; why.append("the title names a sibling product (%s)" % "/".join(sorted(named_sib)))
        fa, fx = forms(title), x["forms"]
        if (({"liquid", "powder"} <= (fa | fx)) or ({"liquid", "stick"} <= (fa | fx))) and not (fa & fx) and fa and fx:
            # liquid vs powder; liquid vs stick (MAC Studio Radiance serum foundation vs the skin tint stick, checker rounds 1-3)
            s -= 0.30; why.append("form differs (%s vs %s)" % ("/".join(sorted(fa)), "/".join(sorted(fx))))
        # brow pencil vs brow gel (Gimme Brow+ pencil listing vs the gel, checker round 2)
        if "brow" in at_types and "brow" in x["types"]:
            ga, gx = {"pencil", "gel", "pomade"} & set(re.sub(r"[^a-z0-9]+", " ", bt).split()), \
                {"pencil", "gel", "pomade"} & set(re.sub(r"[^a-z0-9]+", " ", base_text(x["p"]["name"])).split())
            if ga and gx and not (ga & gx):
                s -= 0.35; why.append("brow format differs (%s vs %s)" % ("/".join(sorted(ga)), "/".join(sorted(gx))))
        # one-sided variant words (Nov 19, 2026 dry run): a Sephora 'Waterproof' mascara for a regular listing, the
        # perfume for a deodorant stick, a body / hair version for the fragrance
        bta = set(re.sub(r"[^a-z0-9]+", " ", bt).split())
        bsx = set(re.sub(r"[^a-z0-9]+", " ", base_text(x["p"]["name"])).split())
        for wv, pen in (("waterproof", 0.30), ("deodorant", 0.45), ("stick", 0.12), ("refillable", 0.0), ("body", 0.2), ("hair", 0.2),
                        ("sharpener", 0.6), ("rollerball", 0.25), ("toenail", 0.6), ("toenails", 0.6), ("loose", 0.2), ("pressed", 0.2), ("aftershave", 0.5)):
            if (wv in bsx) != (wv in bta) and pen:
                s -= pen; why.append(f"'{wv}' on one side only")
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
                if f == "mini" and x["multi"] and af["mini"]:
                    continue   # a mini listing on a full-size page that sells several sizes: size_guess picks the price
                if f == "mini" and x["flags"]["mini"] and not af["mini"] and not _small_size(title + " " + (a.get("size") or "")):
                    # a Mini page never matches a listing whose size is the full size (Nick, Nov 20, 2026)
                    s -= 0.40; why.append("Sephora page is a mini, the listing isn't"); continue
                s -= pen; why.append(f"{f} mismatch")
        cap = None   # line-name check: the first words of the Sephora name must be in the Amazon title
        if x["lead"] and x["lead"] not in at:
            cap = "low"; why.append(f"line word '{x['lead']}' missing")
        elif x["head"] and not all(w in at for w in x["head"]):
            cap = "medium"
        return s, ", ".join(why), (rec, cap, bool(hits))

    VARIANT_WORDS = {"travel", "spray", "mini", "set", "duo", "trio", "refill", "refillable", "intense", "jumbo", "deluxe"}
    for x in P:
        x["core"] = frozenset(w for w in x["set"] if w not in GENERIC and w not in VARIANT_WORDS)

    own = {}
    for a in asins:
        skip = exclude.get(a["asin"]) or ()
        sc = sorted(((score(a, x), i) for i, x in enumerate(P) if x["p"]["productId"] not in skip), key=lambda t: -t[0][0])
        top = sc[0] if sc else ((0, "", (0, None)), None)
        if top[1] is not None:
            core = P[top[1]]["core"]
            # near-duplicate Sephora listings (same line, other size/format): prefer the one whose mini/set flags agree
            af = flags((a["title"] or "") + " " + (a.get("size") or ""))
            twins = [t for t in sc if P[t[1]]["core"] == core and t[0][0] >= top[0][0] - 0.12]
            agree = [t for t in twins if P[t[1]]["flags"]["mini"] == af["mini"] and P[t[1]]["flags"]["set"] == af["set"]]
            if agree:
                top = agree[0]
            # same line, one name has an extra variant word the title doesn't ('Overcurl Waterproof', 'Blush Stick',
            # 'Deodorant'): the one whose words are all in the title wins (Nov 19, 2026, from Nick's leads)
            pool = agree or twins
            if len(pool) > 1:
                at_all = A_tok[a["asin"]]
                miss = lambda t: len(P[t[1]]["set"] - at_all)
                best_t = min(pool, key=lambda t: (miss(t), -t[0][0]))
                if miss(best_t) < miss(top):
                    top = best_t
            others = [t for t in sc if P[t[1]]["core"] != core]
            second = others[0][0][0] if others else 0
        else:
            second = 0
        own[a["asin"]] = {"best": top[1], "s": top[0][0], "why": top[0][1], "rec": top[0][2], "margin": top[0][0] - second,
                          "scores": {i: v for v, i in sc[:8]}}

    def conf_of(s, margin, rc):
        rec, cap = rc[0], rc[1]
        shade_hit = len(rc) > 2 and rc[2]
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
        if c is None and shade_hit and s >= LOW_MIN:   # a shade this product sells is in the title: keep it as low
            c = "low"
        if cap and c and order.index(c) < order.index(cap):
            c = cap
        # Sephora's first name word missing (marketing words: 'Pro Filt'r', 'Ultra Suede', 'Perfectionist'): keep a
        # clear winner as low (Nov 19, 2026; Nick's leads showed these are mostly the right product)
        if cap == "low" and rec < 0.5 and not (shade_hit or (s >= 0.40 and margin >= 0.15)):
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
                              "best_guess": P[best]["p"]["name"] if best is not None else None, "best_score": round(o["s"], 3),
                              "best_pid": P[best]["p"]["productId"] if best is not None else None, "best_why": o["why"],
                              "best_rec": round(o["rec"][0], 3), "best_cap": o["rec"][1], "best_margin": round(o["margin"], 3)})
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
