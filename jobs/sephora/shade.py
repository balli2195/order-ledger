"""Which Sephora SKU (shade / size / scent) an Amazon listing is (Nov 19, 2026).

A catalog pair is product-level (ASIN <-> Sephora product). Products with several SKUs need the right one: the price,
the stock check and the lead link all depend on it. Amazon often hides the shade: not in the title, only in the
variation selector, only the code (Sephora has the name) or the name (Sephora has the code), or only in the photos.

resolve_text(k, skus, vt) uses the Amazon text fields, strongest first:
  1. model / partNumber equal to a Sephora SKU id (sellers often list the Sephora item number)
  2. the listing's own variation attributes (the selector: Color / Size / Scent / Style)
  3. Keepa's color field, the title, then the bullets and description
  and matches each SKU's code ("81", "3F", "#410", "N°5") and name ("Daydreaming", "Salty Tan") separately, so
  a listing with only one of the two still matches. Size SKUs are matched on ounces (within 12%).
resolve_image(...) (shade_image.py) compares the Amazon photos with Sephora's swatch and SKU photos when the text
  can't decide.
k = Keepa product record (title, color, size, model, partNumber, features, description, variations, images);
skus = store_skus list [{sku_id, size, value, desc, price, oos}]; vt = Sephora variation_type.
"""
from __future__ import annotations

import collections
import re
import unicodedata

NUM = r"(\d+(?:[.,]\d+)?|\.\d+)"
UNIT = r"(fl\.?\s*oz\.?|fluid\s*ounces?|oz\.?|ounces?|ml|millilit(?:er|re)s?|g|grams?|gr)\b"
SIZE_RX = re.compile(NUM + r"\s*-?\s*" + UNIT, re.I)
GENERIC = set("""shade color colour the and with for in of a an lip lipstick gloss liner pencil matte satin sheer shine
cream liquid powder foundation concealer blush bronzer highlighter oil balm tint stain mascara eyeshadow palette
spray brush nail polish finish long wear lasting waterproof full size mini travel new warm cool neutral deep light
medium fair tan rich dark soft pink nude red brown beige berry rose coral peach plum mauve shimmer glow""".split())
FIELD_W = {"own": 10, "color": 9, "title": 8, "ocr": 7, "feat": 3, "desc": 2}


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("’", "'").replace("$", "s")   # 'Fu$$y' = 'Fussy'
    s = re.sub(r"(\d)\(([a-z])\)", r"\1\2", s)                          # '1.3(N)' = '1.3N'
    s = re.sub(r"\bn\s*[°o]\s*\.?\s*(\d)", r"no\1", s)
    return re.sub(r"[^a-z0-9.]+", " ", s).strip()


def oz_amounts(text):
    m = re.fullmatch(r"\s*(\d*\.?\d+)\s*/\s*(\d+(?:\.\d+)?)\s*", text or "")   # Sephora's unitless '1.7/50' = oz / ml
    if m and float(m.group(1)) > 0 and 25 <= float(m.group(2)) / float(m.group(1)) <= 35:   # not '1/1' or '16.9/300'
        return [round(float(m.group(1)), 3)]
    if m:
        return []
    out = []
    for m in SIZE_RX.finditer(text or ""):
        v = float(m.group(1).replace(",", "."))
        u = m.group(2).lower().replace(" ", "").rstrip(".")
        if u.startswith("ml") or u.startswith("millil"):
            v /= 29.57
        elif u in ("g", "gram", "grams", "gr"):
            v /= 28.35
        out.append(round(v, 3))
    return out


PACK_RX = re.compile(r"\(?\bpack of \d+\)?|\b\d+\s*(?:count|ct|pcs?|pieces?)\b", re.I)


def _strip_sizes(t: str) -> str:
    return PACK_RX.sub(" ", SIZE_RX.sub(" ", t or ""))


def split_value(v: str) -> tuple[str | None, str]:
    """Sephora shade value -> (code, name). '81 Daydreaming' -> ('81', 'daydreaming'); 'Salty Tan - soft mauve' ->
    (None, 'salty tan'); '3F' -> ('3f', ''); '0.1 Translucent' -> ('0.1', 'translucent'); '#56.5' -> ('56.5', '')."""
    v = re.sub(r"(\d)\(([A-Za-z])\)", r"\1\2", v or "")
    parts = re.split(r"\s+[-–|]\s+|\s*\(", v)
    v = parts[0]
    if len(parts) > 1 and re.fullmatch(r"#?\s*(?:no\.?\s*)?\d+(?:\.\d+)?[A-Za-z]{0,2}", v.strip()) and parts[1].strip():
        v = v + " " + parts[1]   # '5 - Tan': code then name (not code then description)
    n = norm(_strip_sizes(v)).replace("#", "")
    m = re.match(r"^((?:no)?\d+(?:\.\d+)?[a-z]{0,3}|[a-z]{1,3}\d+(?:\.\d+)?[a-z]?)\b\s*(.*)$", n)
    if m:
        return m.group(1), m.group(2).strip()
    m = re.match(r"^(.*?)\s+((?:no)?\d+(?:\.\d+)?[a-z]?)$", n)   # 'Vanilla 28' style: name then code
    if m and m.group(1):
        return m.group(2), m.group(1).strip()
    return None, n


def _code_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    if re.fullmatch(r"\d+", a) and re.fullmatch(r"\d+", b):
        return int(a) == int(b)
    return False


def _fields(k: dict, asin: str | None) -> dict:
    own = []
    for v in k.get("variations") or []:
        if v.get("asin") == (asin or k.get("asin")):
            own = [a.get("value") or "" for a in v.get("attributes") or []]
    feats = " ".join((k.get("features") or [])[:5])
    return {"own": norm(_strip_sizes(" | ".join(own))), "color": norm(_strip_sizes(k.get("color") or "")),
            "title": norm(_strip_sizes(k.get("title") or "")), "feat": norm(_strip_sizes(feats)),
            "desc": norm(_strip_sizes((k.get("description") or "")[:1500])), "_own_raw": " | ".join(own),
            "ocr": norm(_strip_sizes(" | ".join(k.get("_ocr") or [])))}


def ocr_photos(paths: list) -> list:
    """Text printed on the listing photos (box fronts / bottoms, stickers: the checkers found the shade there in about
    half the cases where the text didn't name it). Needs tesseract; returns one string per photo."""
    try:
        import pytesseract
        from PIL import Image
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in paths:
        try:
            im = Image.open(p).convert("L")
            if im.width < 1200:
                im = im.resize((im.width * 2, im.height * 2))
            out.append(" ".join(pytesseract.image_to_string(im, timeout=20).split())[:1500])
        except Exception:  # noqa: BLE001
            out.append("")
    return out


def keep_ocr(texts: list, skus: list) -> list:
    """Photos that name at most two of the product's shades: a shade chart naming many of them says nothing."""
    names = [split_value(s.get("value") or "")[1] for s in skus]
    keep = []
    for t in texts:
        nt = norm(t)
        hits = sum(1 for n in names if n and len(n) >= 3 and _has_phrase(nt, n))   # (names under 3 letters are ignored)
        if hits <= 2:
            keep.append(t)
    return keep


def _squash(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(t).replace(" and ", " "))


COLOUR_WORDS = GENERIC | set("black white grey gray blue green yellow orange purple gold silver bronze multi multicolor "
                             "clear transparent universal original regular one none cranberry multicolored multicolour "
                             "standard translucent assorted various".split())


def amazon_shade(k: dict, asin: str | None = None) -> str | None:
    """The shade / style the listing names in its selector (or Keepa's color field when it isn't a generic colour)."""
    for v in k.get("variations") or []:
        if v.get("asin") == (asin or k.get("asin")):
            for at in v.get("attributes") or []:
                if (at.get("dimension") or "").lower() in ("color", "colour", "style", "scent", "style name", "pattern"):
                    val = (at.get("value") or "").strip()
                    if val and set(norm(_strip_sizes(val)).split()) - COLOUR_WORDS:
                        return val
    c = (k.get("color") or "").strip()   # Keepa's color field is often filler ('Cranberry'): only when the title says it too
    if c and set(norm(_strip_sizes(c)).split()) - COLOUR_WORDS and norm(c) in norm(k.get("title") or ""):
        return c
    return None


def _near(a: str, b: str) -> bool:
    """Same word allowing one typo ('rasperry' / 'raspberry')."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    if len(a) == len(b) and i + 1 < len(a) and a[i] == b[i + 1] and a[i + 1] == b[i] and a[i + 2:] == b[i + 2:]:
        return True   # two letters swapped: Sephora's 'Siene' = 'Seine' (checker round 3)
    return a[i + 1:] == b[i + 1:] or a[i:] == b[i + 1:] or a[i + 1:] == b[i:]


# colour words that mean the same thing (French names on Dior / Chanel / Givenchy listings, US vs UK spelling)
SYN = {"noir": "black", "nero": "black", "negro": "black", "blanc": "white", "bianco": "white", "rouge": "red",
       "rosso": "red", "rose": "pink", "rosa": "pink", "brun": "brown", "marron": "brown", "gris": "grey", "gray": "grey",
       "bleu": "blue", "vert": "green", "or": "gold", "dore": "gold", "argent": "silver", "beige": "nude", "naked": "nude",
       "chocolat": "chocolate", "prune": "plum", "framboise": "raspberry", "cerise": "cherry", "corail": "coral",
       "peche": "peach", "pêche": "peach", "colour": "color", "transparent": "clear"}


def _syn(words):
    return {SYN.get(w, w) for w in words}


def close_skus(k: dict, skus: list, asin: str | None = None) -> list:
    """Sephora SKUs close to the Amazon shade without being it for sure (Nick, Nov 19, 2026: 'close but different should
    still count, I can review manually'): same colour after synonyms ('Noir' / 'Black'), or the Amazon shade words are
    Sephora's description of the SKU ('Cherry Red Chrome' vs a 'cherry red' SKU)."""
    a = amazon_shade(k, asin) or (k.get("color") or "")
    aw = _syn(set(norm(_strip_sizes(a)).replace("#", " ").split()) - {"shade", "color", "colour", "the", "and", "with"})
    if not aw:
        return []
    acode, _an = split_value(a)
    only_colour = not (aw - _syn(COLOUR_WORDS))   # 'Red', 'Black': means something on a 3-shade mascara, not a 40-shade lipstick
    if only_colour and len(skus) > 4:
        return []
    out = []
    for s in skus:
        code, name = split_value(s.get("value") or "")
        if acode and code and not _code_eq(acode, code):
            continue   # '051 Nude Bloom' is not '013 Beige'
        nw = _syn(set(name.split()))
        dw = _syn(set(norm(s.get("desc") or "").split()) - {"undertone", "undertones", "skin", "tones", "for", "with", "and"})
        if (nw and (nw <= aw or aw <= nw)) or (len(dw) >= 2 and dw <= aw) or (len(aw) >= 2 and aw <= dw):
            out.append(s)
    return out if len(out) <= 3 else []   # 'close' to half the list says nothing


TITLE_SHADE = re.compile(r"(?:^|[\s,\-–(#])(\d{1,3}(?:\.\d{1,2})?[A-Za-z]{0,2})\s+([A-Z][\w'&.]*(?:\s+[A-Z][\w'&.]*){0,2})"
                         r"(?!\s*(?:oz|ml|fl|g\b|gram|ounce|count|pack|pc|piece|spf))")


def title_shade(k: dict) -> str | None:
    """A 'code Name' shade written in the title ('202 Loud Lollipop', '50 Stiletto', '100B Milkshake'), the last one."""
    t = _strip_sizes(k.get("title") or "")
    hits = [m for m in TITLE_SHADE.finditer(t) if not re.fullmatch(r"(?i)spf|pa|hr|hour|in", m.group(2).split()[0])
            and not re.search(r"(?i)(hr|h|x|pc|pk|mm|cm|d)$", m.group(1))]   # '12HR', '3D', '2X' aren't shades
    return f"{hits[-1].group(1)} {hits[-1].group(2)}" if hits else None


def shade_not_sold(k: dict, skus: list, asin: str | None = None) -> str | None:
    """The Amazon shade when it shares no word or code with any Sephora SKU (Sephora doesn't sell that shade), else None.
    The shade comes from the selector / color field, else a 'code Name' shade in the title, but only when the product's
    Sephora shades carry codes too (so a title code that isn't among them means another shade)."""
    a = amazon_shade(k, asin)
    if not a:
        coded = sum(1 for s_ in skus if split_value(s_.get("value") or "")[0]) >= max(2, len(skus) // 2)
        a = title_shade(k) if coded else None
    if not a or close_skus(k, skus, asin):   # close shades are kept for Nick to review, not dropped
        return None
    # the shade's name part only ('Fizzy Frosé - rose gold' -> 'Fizzy Frosé'), and without the line words most Sephora
    # shades share ('Intense' in Dior's '026 Intense Mauve'): round 1 of the checker loop, Nov 19, 2026
    a_name = re.split(r"\s+[-–|]\s+|\s*\(", a)[0] or a
    df = collections.Counter(w for s_ in skus for w in set(norm(_strip_sizes(s_.get("value") or "")).split()))
    line = {w for w, c in df.items() if len(skus) >= 4 and c >= max(3, 0.3 * len(skus))}
    aw = set(norm(_strip_sizes(a_name)).replace("#", " ").split()) - GENERIC - line
    acode = split_value(a_name)[0]
    m_ = re.fullmatch(r"(?:shade|color|colour|no\.?|#)\s*([\d.]+[a-z]{0,2})", norm(a_name).strip())
    if not acode and m_:
        acode = m_.group(1)   # 'Shade 4' (Benefit brow) is code 4
    asq = _squash(a_name)
    for s in skus:
        code, name = split_value(s.get("value") or "")
        v = norm(_strip_sizes(s.get("value") or ""))
        if not v:
            if not any((x.get("value") or "").strip() for x in skus):
                return None   # size-only SKUs: no shade list to compare with
            continue          # one nameless SKU (a mini) among named shades
        sw = set(v.replace("#", " ").split()) - GENERIC - line
        if acode and code and _code_eq(acode, code):
            if _tone_conflict(code, name, norm(a_name)):
                continue   # '16 Cool' is not '16 Neutral'
            return None
        if aw & sw or any(_near(x, y) for x in aw for y in sw) or (code and not acode and any(_code_eq(code, t) for t in aw)) \
                or (_squash(name) and len(_squash(name)) >= 3 and set(name.split()) - GENERIC and _squash(name) in asq) \
                or (asq and asq in _squash(s.get("value") or "")):
            return None
        d = norm(s.get("desc") or "")
        if d and len(d.split()) >= 2 and d in norm(a) and not (acode and code and not _code_eq(acode, code)):
            return None   # (a sibling with another code can share the description: '15 Cool' / '16 Cool', round 5)
    return a


TONES = {"warm", "cool", "neutral", "olive", "golden", "rosy", "peach", "pink", "yellow", "red"}


def _tone_conflict(code: str, name: str, text: str) -> bool:
    """'16 Cool' on Amazon vs Sephora's '16 Neutral': the code matches, the undertone right after it doesn't (round 5)."""
    m = re.search(r"(?<![a-z0-9.])" + re.escape(code) + r"\s+([a-z]+)", text or "")
    nt = set((name or "").split()) & TONES
    return bool(m and nt and m.group(1) in TONES and m.group(1) not in nt)


def _has_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and re.search(r"(?<![a-z0-9.])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def same_label(a: dict, b: dict) -> bool:
    """Two SKUs that read the same (value and size, punctuation and 'fl' aside): '1.7 fl oz /50 ml' = '1.7 oz / 50 ml'.
    A refill, another scent or another formulation reads differently, so it is never an alternate."""
    f = lambda s: re.sub(r"fl|[^a-z0-9]", "", norm((s.get("value") or "") + " " + (s.get("size") or "")))
    return f(a) == f(b) and bool(f(a))


def _size_pick(k: dict, F: dict, skus: list) -> dict | None:
    # the title first: Keepa's size field is often another variation's (3.4 oz on a 10 ml travel spray)
    amz = oz_amounts(k.get("title") or "") or oz_amounts(F["_own_raw"]) or oz_amounts(k.get("size") or "")
    if not amz:
        return None
    a = amz[0]
    refill = bool(re.search(r"\brefill(?!able)", (k.get("title") or "").lower()))
    best = None
    for s in skus:
        lab = s.get("size") or s.get("value") or ""
        so = oz_amounts(lab)
        if not so:
            continue
        r = so[0] / a if a else 0
        d = abs(1 - r) + (0 if bool(re.search(r"\brefill(?!able)", lab.lower())) == refill else 0.05)
        if len(so) > 1 and re.search(r"\d\s*x\s*\d|,", lab) and not re.search(r"\b(set|with|duo|gift|\+)\b|\d\s*x\s*\d", (k.get("title") or "").lower()):
            d += 0.2   # '1 x 3.4 oz , 1 x 0.34 oz' is a gift set, not the single bottle
        if best is None or d < best[0]:
            best = (d, s, so[0])
    if best and best[0] <= 0.12:
        # two SKUs of the same size (Kayali sells two 10 ml bottles, $29 and $32): Nick takes either (Nov 19, 2026), so
        # the cheaper one is the store size and the others are alternates (the stock check accepts any of them)
        same = [s for s in skus if same_label(s, best[1])]
        same.sort(key=lambda s: s.get("price") or 9e9)
        if same:   # an alternate costing much more is something else (a bundle, a bigger supply): not interchangeable
            same = [s for s in same if (s.get("price") or 0) <= (same[0].get("price") or 0) * 1.25]
        pick = same[0] if same else best[1]
        return {"sku_id": pick["sku_id"], "how": f"size {a:.2f} oz = Sephora {pick.get('size') or pick.get('value')}"
                + (f" (cheapest of {len(same)} SKUs that size)" if len(same) > 1 else ""),
                "score": 20, "margin": 20, "alts": [s["sku_id"] for s in same[1:]]}
    return None


ACCESSORY = re.compile(r"\b(puff|brush|sponge|pouch|bag|sharpener|applicator|case)\b")


def resolve_text(k: dict, skus: list, vt: str | None = None, asin: str | None = None, product_name: str | None = None) -> dict:
    """_resolve_text, plus product + accessory sets: 'Translucent Loose Setting Powder & Velour Puff' picks the puff SKU
    by name, but the listing is the powder and the puff together (Nick, Nov 20, 2026: a bundle lead at the real cost)."""
    r = _resolve_text(k, skus, vt, asin, product_name)
    by = {s["sku_id"]: s for s in skus}
    x = by.get(r.get("sku_id"))
    title = norm(k.get("title") or "")
    if x and ACCESSORY.search(norm(x.get("value") or "")) and re.search(r"\s(&|\+|and|with)\s", (k.get("title") or "").lower()):
        main = [s for s in skus if s is not x and (s.get("value") or "").strip() and not ACCESSORY.search(norm(s.get("value") or ""))]
        # the main item: a SKU named in the title / color field, at the title's size, else the largest
        named = [s for s in main if _has_phrase(title + " " + norm(k.get("color") or ""), norm(split_value(s.get("value") or "")[1] or "~"))]
        cand = named or main
        t_oz = oz_amounts(k.get("title") or "") or oz_amounts(str(k.get("size") or ""))
        if cand:
            sz = lambda s: (oz_amounts(s.get("size") or s.get("value") or "") or [0])[0]
            m = min(cand, key=lambda s: abs(sz(s) - t_oz[0])) if t_oz else max(cand, key=sz)
            return {**r, "sku_id": m["sku_id"], "alts": [x["sku_id"]], "bundle": True,
                    "how": r["how"] + f"; a set: {m.get('value')} {m.get('size') or ''} + {x.get('value')}"}
    # a duo / 2-pack of one Sephora single ('BADgal Bang! Double The Volume Mascara Duo'): two of that SKU (Nick, Nov 20)
    m2 = re.search(r"\b(duo|twin|pack of ([2-4])|([2-4]) ?(pack|pk|count|ct|pc|pcs|piece|pieces)|set of ([2-4]))\b", title)
    if x and m2 and not r.get("bundle") and not r.get("two_shades") and not re.search(r"\b(duo|set|pack|kit)\b", norm(product_name or "")) \
            and not re.search(r"\d\s*x\s*\d|\bduo\b|\bset\b", ((x.get("value") or "") + " " + (x.get("size") or "")).lower()):
        # (not when Sephora's SKU is itself the pack: Huda Legit Lashes '2 x 0.28 oz')
        n = int(next((g for g in m2.groups()[1:] if g and g.isdigit()), 2))
        return {**r, "alts": [x["sku_id"]] * (n - 1), "bundle": True, "how": r["how"] + f"; {n} of this SKU in one listing"}
    return r


def _resolve_text(k: dict, skus: list, vt: str | None = None, asin: str | None = None, product_name: str | None = None) -> dict:
    """{sku_id | None, score, margin, how, ranked: [(score, sku_id, why)]}. product_name = the Sephora product's name
    (a SKU called 'Benetint' on the Benetint page is weak evidence: every listing of the line says Benetint)."""
    if not skus:
        return {"sku_id": None, "score": 0, "margin": 0, "how": "no SKUs"}
    if len(skus) == 1:
        return {"sku_id": skus[0]["sku_id"], "score": 99, "margin": 99, "how": "only one SKU"}
    ids = {s["sku_id"] for s in skus}
    t_oz = oz_amounts(k.get("title") or "")
    for f in ("model", "partNumber"):
        v = str(k.get(f) or "").strip()
        if v in ids:
            so = oz_amounts(next((x.get("size") or x.get("value") or "") for x in skus if x["sku_id"] == v))
            if t_oz and so and abs(1 - so[0] / t_oz[0]) > 0.12:
                continue   # the title's size wins over a reused item number
            return {"sku_id": v, "score": 100, "margin": 100, "how": f"Amazon {f} {v} is the Sephora item number"}
    F = _fields(k, asin)
    # a color field that lists many of the product's shades ('Worthy, Strength, Courage, Harmony, ...') names none of them
    if F["color"] and len(re.split(r"[,/;|]", str(k.get("color") or ""))) >= 3 and sum(1 for x in skus if (x.get("value") or "").strip() and _has_phrase(F["color"], norm(split_value(x.get("value") or "")[1] or "~"))) >= 3:
        F["color"] = ""
    shade_like = "color" in (vt or "").lower() or "scent" in (vt or "").lower() or not vt
    if not shade_like and "size" in (vt or "").lower():
        p = _size_pick(k, F, skus)
        if p:
            return p
        if all(oz_amounts(x.get("value") or x.get("size") or "") for x in skus):
            # the values are only sizes: their numbers aren't shade codes ('1 x 5 oz' is not code 1, checker round 3)
            return {"sku_id": None, "score": 0.0, "margin": 0.0, "how": "no Sephora size matches the listing", "ranked": []}
    # codes are only trusted as whole tokens; names as whole phrases (longest wins), partial names count a little
    names = {}
    for s in skus:
        code, name = split_value(s.get("value") or s.get("size") or "")
        names[s["sku_id"]] = (code, name)
    # line words shared by most shade names ('Atelier' in Westman's 'Atelier X', 'Pillow Talk' in Charlotte Tilbury's,
    # 'Shade' in Benefit's 'Shade 04') say nothing about the shade: drop them (round 1 of the checker loop, Nov 19, 2026)
    df = collections.Counter(w for _, n in names.values() for w in set(n.split()))
    line = {w for w, c in df.items() if len(skus) >= 4 and c >= max(4, 0.7 * len(skus))}
    if line:
        for sid, (code, name) in list(names.items()):
            rest = " ".join(w for w in name.split() if w not in line)
            names[sid] = (code, rest)
    # a 'name' that is only a generic word ('Shade' in Benefit's 'Shade 3') is no name
    for sid, (code, name) in list(names.items()):
        if name and set(name.split()) <= {"shade", "color", "colour", "no", "number", "tone"}:
            names[sid] = (code, "")
    all_names = [n for _, n in names.values() if n]
    all_codes = {c for c, _ in names.values() if c}
    pn = set(norm(product_name or "").split())
    def desc_of(s):
        # Sephora's description, else the value's tail ('toasted teddy + espresso - a toasty brown pair', checker round 3)
        d = norm(s.get("desc") or "")
        tail = re.split(r"\s+[-–|]\s+", s.get("value") or "")[1:]
        if not d and tail:
            d = " ".join(w for w in norm(tail[0]).split() if w not in ("a", "an", "the", "pair", "duo", "set"))
        return d
    descs = collections.Counter(desc_of(s) for s in skus)
    ranked = []
    for s in skus:
        code, name = names[s["sku_id"]]
        sc, why = 0.0, []
        if code:
            for f, w in FIELD_W.items():
                toks = F[f].replace("#", " ").split()
                # '10 N' / '27 P' written apart on Amazon = Sephora's '10N' / '27P'
                toks += [toks[i] + toks[i + 1] for i in range(len(toks) - 1)
                         if re.fullmatch(r"[\d.]+", toks[i]) and re.fullmatch(r"[a-z]{1,2}", toks[i + 1])]
                if any(_code_eq(code, t) or _code_eq(code, re.sub(r"^no\.?(?=\d)", "", t)) for t in toks):
                    if _tone_conflict(code, name, F[f]):
                        why.append(f"code {code} in {f} but another undertone"); break
                    sc += w * (1.2 if re.search(r"[a-z]", code) or len(code) >= 3 else 0.9); why.append(f"code {code} in {f}")
                    break
                if "." in code and f in ("own", "color", "title") and re.search(r"[a-z]", code) and any(
                        t == code.replace(".", "") and t not in all_codes for t in toks):
                    sc += w * 0.9; why.append(f"code {code} in {f} (decimal point dropped)")
                    break
        # '1 Neutral' / 'Lightest 2': a name made only of tone words is part of the code, it counts only with the code
        name0 = name
        # (tone words only: '011 Pink' / '017 Nude' on Dior's highlighter are real names)
        weak = bool(code) and set(name.split()) <= {"warm", "cool", "neutral", "deep", "light", "medium", "fair", "tan", "rich",
                                                    "dark", "lightest", "lighter", "darkest", "deepest", "olive", "golden"}
        if weak and not any(w.startswith("code") and w.split(" in ")[-1].split(" (")[0] in ("own", "color", "title") for w in why):
            name = ""
        if not name.replace(".", "").strip():
            name = ""
        if name:
            # a name that is part of a longer SKU name found in the same field doesn't count ('Juicy Pink' in 'Juicy Pink Lady')
            for f, w in FIELD_W.items():
                if f in ("feat", "desc") and any(len(x) >= 5 and any(_near(x, y) for y in F[g].split())
                                                 for g in ("own", "color", "title") for x in name.split()) \
                        and all(any(_near(x, y) for g in ("own", "color", "title") for y in F[g].split()) for x in name.split() if len(x) >= 3):
                    g = next(g for g in ("own", "color", "title") if any(_near(x, y) for x in name.split() for y in F[g].split()))
                    sc += FIELD_W[g] * 0.9; why.append(f"name '{name}' in {g} (typo / word order)")
                    break
                if _has_phrase(F[f], name):
                    longer = [n for n in all_names if n != name and name in n and _has_phrase(F[f], n)]
                    if longer:
                        continue
                    ww = w * (1 + 0.15 * min(len(name.split()), 4))
                    if pn and set(name.split()) <= pn and f not in ("own", "color"):
                        ww *= 0.3   # the shade has the product line's name (in the title that's the line; the selector names the shade)
                    sc += ww; why.append(f"name '{name}' in {f}")
                    break
            else:
                if len(_squash(name)) >= 3 and any(_squash(name) == _squash(t) for t in F["_own_raw"].split(" | ") + [F["color"]] if t):
                    sc += FIELD_W["own"]; why.append(f"name '{name}' in own (punctuation aside)")
                    ranked.append([round(sc, 2), s["sku_id"], "; ".join(why)])
                    continue
                nw = name.split()
                hitf = None
                for f in ("own", "color", "title"):
                    fw = F[f].split()
                    if len(_squash(name)) >= 6 and _squash(name) in re.sub(r"[^a-z0-9]", "", F[f]):
                        hitf = (f, "run together")      # 'LOVECHILD' = 'Love Child', 'BananaBread'
                    elif len(nw) >= 1 and all(any(_near(x, y) for y in fw) for x in nw if len(x) >= 3) and any(len(x) >= 5 for x in nw):
                        hitf = (f, "typo / word order")  # 'RASPERRY' = 'Raspberry', 'Black Brown' = 'Brown Black'
                    if hitf:
                        break
                if hitf:
                    sc += FIELD_W[hitf[0]] * 0.9; why.append(f"name '{name}' in {hitf[0]} ({hitf[1]})")
                    ranked.append([round(sc, 2), s["sku_id"], "; ".join(why)])
                    continue
                words = [x for x in name.split() if len(x) >= 3 and x not in GENERIC]
                if words:
                    for f in ("own", "color", "title"):
                        hit = [x for x in words if _has_phrase(F[f], x)]
                        if hit and len(hit) == len(words):
                            sc += FIELD_W[f] * 0.6; why.append(f"name words {' '.join(hit)} in {f}")
                            break
        # tone name + undertone from the description ('Light 2 | neutral undertone' = Amazon 'light neutral', Glossier)
        STOP = {"undertone", "undertones", "skin", "tones", "tone", "for", "with", "and", "to", "the"}
        nd = {w for w in name0.split() if not re.fullmatch(r"[\d.]+", w)} | (set(norm(s.get("desc") or "").split()) - STOP)
        for f in ("color", "own"):
            fw = set(F[f].split()) - STOP
            if nd and len(nd) >= 2 and fw and nd == fw:
                sc += FIELD_W[f]; why.append(f"name + description = {f} ('{' '.join(sorted(nd))}')")
                break
        d = desc_of(s)
        if len(d.split()) >= 2 and descs[d] == 1 and any(_has_phrase(F[f], d) for f in ("own", "color", "title")):
            whole = d in (F["color"], F["own"])   # the color field is exactly Sephora's description ('Light Warm' = 140)
            sc += 8 if whole else 4; why.append(f"description '{d}' {'is the color field' if whole else 'in the listing'}")
        ranked.append([round(sc, 2), s["sku_id"], "; ".join(why)])
    # a SKU whose code is missing while another SKU's code is in the selector / color / title is another shade
    # ('5.5 Tan' when the title says '5 Tan')
    found_code = {sid for sc_, sid, why in ranked if any(w.startswith("code") and w.split(" in ")[-1] in ("own", "color", "title")
                                                         for w in why.split("; "))}
    # the name is the stable key: Sephora renumbers lines ('206 Secret Boudoir' is now '109 Secret Boudoir' and 206 is
    # Golden Bronze), so a name hit beats a code-only hit (Nick, Nov 19, 2026); a code alone still counts when no
    # SKU's name is in the listing (a renamed shade is the same shade)
    def _hit(why, kind):
        return any(w.startswith(kind) and w.split(" in ")[-1] in ("own", "color", "title") for w in why.split("; "))
    found_name = {sid for sc_, sid, why in ranked if _hit(why, "name '") or "= selector" in why}
    key0 = lambda sid: " ".join(x for x in names[sid] if x)
    for r in ranked:
        code = names[r[1]][0]
        if r[1] in found_name:
            continue
        if found_name and _hit(r[2], "code") and r[0] > 0:
            r[0] = round(r[0] - 6, 2); r[2] += "; only its code matches, another SKU's name is there"
        elif code and found_code and r[1] not in found_code and r[0] > 0:
            r[0] = round(r[0] - 6, 2); r[2] += "; its code is missing, another SKU's code is there"
    # two one-word names side by side in one field are one compound shade ('04 medium deep' is not Medium + Deep, round 5)
    one = {}
    for r in ranked:
        for w in r[2].split("; "):
            m = re.fullmatch(r"name '([a-z]+)' in (own|color|title)", w)
            if m:
                one.setdefault(m.group(2), []).append((m.group(1), r))
    for f, lst in one.items():
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                (a_, ra), (b_, rb) = lst[i], lst[j]
                tone_w = {"light", "medium", "deep", "fair", "tan", "dark", "rich", "lightest", "deepest"}
                if ra is not rb and a_ in tone_w and b_ in tone_w and (_has_phrase(F[f], a_ + " " + b_) or _has_phrase(F[f], b_ + " " + a_)):
                    for rr in (ra, rb):
                        if "compound" not in rr[2]:
                            rr[0] = round(rr[0] - 6, 2); rr[2] += f"; part of the compound '{a_} {b_}', not its own shade"
    # a shade named like the product line ('Translucent' on Laura Mercier's Translucent powder) next to another named
    # shade ('translucent honey'): the other one is the shade (round 5)
    if pn:
        nm = [r for r in ranked if r[1] in found_name and r[0] >= 7]
        line_hit = [r for r in nm if names[r[1]][1] and set(names[r[1]][1].split()) <= pn]
        if line_hit and len({key0(r[1]) for r in line_hit}) < len({key0(r[1]) for r in nm}):
            for r in line_hit:
                r[0] = round(r[0] - 5, 2); r[2] += "; its name is the product line's"
    refill_l = bool(re.search(r"\brefill(?!able)", (k.get("title") or "").lower() + " " + F["own"]))
    is_refill = {x["sku_id"]: bool(re.search(r"\brefill(?!able)", (x.get("value") or "").lower())) for x in skus}
    if any(is_refill.values()) and not all(is_refill.values()):
        for r in ranked:
            if r[0] > 0 and is_refill[r[1]] != refill_l:
                r[0] = round(r[0] * 0.4, 2); r[2] += "; refill / not refill differs from the listing"
    for r in ranked:
        if found_name and r[1] not in found_name and "is the color field" in r[2] and not _hit(r[2], "code"):
            r[0] = round(r[0] - 4, 2); r[2] += "; another SKU's name is in the listing (the color field is a sibling's description)"
    ranked = [tuple(r) for r in ranked]
    ranked.sort(key=lambda x: -x[0])
    # the same shade in several sizes (full size + mini) is one shade: compare shades, then pick the size
    # ('3 - Fair' and '3 Fair' are one shade: compare code + name)
    key = {s["sku_id"]: " ".join(x for x in split_value(s.get("value") or "") if x) or norm(_strip_sizes(s.get("value") or ""))
           for s in skus}
    # a mini whose size text names another SKU's shade ('Black' · 'Mini Intense Pitch Black – 0.14 oz') is that shade's
    # mini: group them, so the title's size picks between them (BADgal BANG! 'Regular Size, 0.3 fl oz', Nick Nov 20)
    for s_ in skus:
        sz_ = norm(s_.get("size") or "")
        for o in skus:
            if o is not s_ and key[o["sku_id"]] and len(key[o["sku_id"]]) >= 5 and key[o["sku_id"]] in sz_:
                key[s_["sku_id"]] = key[o["sku_id"]]
    top = ranked[0]
    second = next((r[0] for r in ranked[1:] if key[r[1]] != key[top[1]]), 0)
    if top[0] >= 7 and top[0] - second >= 3:
        group = [s for s in skus if key[s["sku_id"]] == key[top[1]]]
        pick, how, alts = top[1], top[2], []
        if len(group) > 1:
            p = _size_pick(k, F, group)
            if p:
                pick, how = p["sku_id"], how + "; " + p["how"]
                alts = p.get("alts") or []
            else:   # no size on Amazon: the full size, unless the listing says mini or sells at the mini's price
                mini = bool(re.search(r"\b(mini|travel|deluxe sample)\b", (k.get("title") or "").lower()))
                bb = k.get("_buybox")
                prices = sorted(g.get("price") for g in group if g.get("price"))
                if not mini and bb and len(prices) >= 2 and bb < 0.75 * prices[-1] and bb <= 1.6 * prices[0]:
                    mini = True   # Amazon's price is the mini's, not the full size's (checker round 2: Laura Mercier mini)
                sz = sorted(group, key=lambda g: (oz_amounts(g.get("size") or "") or [0])[0])
                pick = (sz[0] if mini else sz[-1])["sku_id"]; how += "; " + ("smallest" if mini else "largest") + " size of that shade"
        # the title's size disagrees with the pick and a SKU with no shade name has that size (Sephora lists some minis
        # without the shade, e.g. Size Up mascara 'Black' full size + a nameless 0.17 oz mini): take the mini
        po = (oz_amounts(next((x.get("size") or "") for x in skus if x["sku_id"] == pick)) or [None])[0]
        if t_oz and po and abs(1 - po / t_oz[0]) > 0.25:
            bare = [x for x in skus if not (x.get("value") or "").strip()
                    and (oz_amounts(x.get("size") or "") or [0])[0] and abs(1 - oz_amounts(x["size"])[0] / t_oz[0]) <= 0.12]
            if len(bare) == 1:
                pick, how = bare[0]["sku_id"], how + f"; the title's size {t_oz[0]:.2f} oz is the unnamed {bare[0].get('size')} SKU"
            else:
                g_oz = [oz_amounts(g.get("size") or g.get("value") or "") for g in group]
                tl = (k.get("title") or "").lower()
                # only for a travel / trial size smaller than every Sephora size: title sizes are often a gram weight or
                # another sibling's (a lead test on Nick's leads, Nov 19, 2026), a 'travel' word plus a smaller size is not
                if re.search(r"\b(travel|trial|mini|deluxe|sample)\b", tl) and all(o and o[0] / t_oz[0] > 1.43 for o in g_oz) \
                        and not re.search(r"\b(set|kit|duo|with|bundle|pack of [2-9])\b|\d\s*x\s*\d", tl):
                    # the title's size isn't one Sephora sells for that shade (Benetint 4 mL travel vs Sephora's 6 / 10 mL,
                    # Fresh Soy 20 mL vs 50 mL+): not this listing's SKU (checker rounds 3-4)
                    return {"sku_id": None, "score": top[0], "margin": round(top[0] - second, 2), "ranked": ranked[:5],
                            "how": how.split("; ")[0] + f"; but the title's size {t_oz[0]:.2f} oz isn't a Sephora size of that shade",
                            "size_not_sold": f"{top[2].split(';')[0]} · {t_oz[0]:.2f} oz"}
        return {"sku_id": pick, "score": top[0], "margin": round(top[0] - second, 2), "how": how, "ranked": ranked[:5], "alts": alts}
    # the listing names two Sephora shades (title 'Say Less', color field 'Superficial'): Nick counts it as both and
    # reviews it later (Nov 19, 2026) -> the stronger one is the store size, the other an alternate, flagged
    named = [r for r in ranked if r[0] >= 7 and r[1] in found_name]
    if len(named) >= 2 and key[named[0][1]] != key[named[1][1]] and named[0][0] - named[1][0] < 3:
        return {"sku_id": named[0][1], "score": named[0][0], "margin": round(named[0][0] - named[1][0], 2),
                "how": f"listing names two Sephora shades ({named[0][2]} / {named[1][2]}): both kept, check the photo",
                "ranked": ranked[:5], "alts": [named[1][1]], "two_shades": True,
                # 'The Lip Drip Set ... Watermelon Rind ... Mango Pulp', '2 Count': both shades in one listing (checker round 3)
                "bundle": bool(re.search(r"\b(set|duo|trio|kit|bundle|[2-9] ?(pc|pcs|piece|pieces|count|pack)|pack of [2-9])\b",
                                         norm((k.get("title") or "") + " " + str(k.get("size") or ""))))}
    if "size" in (vt or "").lower():
        p = _size_pick(k, F, skus)
        if p:
            return p
    return {"sku_id": None, "score": top[0], "margin": round(top[0] - second, 2), "how": "text can't decide", "ranked": ranked[:5]}
