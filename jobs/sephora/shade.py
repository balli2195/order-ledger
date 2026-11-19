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
FIELD_W = {"own": 10, "color": 9, "title": 8, "feat": 3, "desc": 2}


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("’", "'")
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
    parts = re.split(r"\s+[-–|]\s+|\s*\(", v or "")
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
            "desc": norm(_strip_sizes((k.get("description") or "")[:1500])), "_own_raw": " | ".join(own)}


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
    return a[i + 1:] == b[i + 1:] or a[i:] == b[i + 1:] or a[i + 1:] == b[i:]


def shade_not_sold(k: dict, skus: list, asin: str | None = None) -> str | None:
    """The Amazon shade when it shares no word or code with any Sephora SKU (Sephora doesn't sell that shade), else None."""
    a = amazon_shade(k, asin)
    if not a:
        return None
    aw = set(norm(_strip_sizes(a)).replace("#", " ").split()) - GENERIC
    asq = _squash(a)
    for s in skus:
        code, name = split_value(s.get("value") or "")
        v = norm(_strip_sizes(s.get("value") or ""))
        if not v:
            return None   # size-only SKUs: no shade list to compare with
        sw = set(v.replace("#", " ").split()) - GENERIC
        if aw & sw or any(_near(x, y) for x in aw for y in sw) or (code and any(_code_eq(code, t) for t in aw)) or (_squash(name) and _squash(name) in asq) \
                or (asq and asq in _squash(s.get("value") or "")):
            return None
        d = norm(s.get("desc") or "")
        if d and len(d.split()) >= 2 and d in norm(a):
            return None
    return a


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
    refill = bool(re.search(r"\brefill", (k.get("title") or "").lower()))
    best = None
    for s in skus:
        lab = s.get("size") or s.get("value") or ""
        so = oz_amounts(lab)
        if not so:
            continue
        r = so[0] / a if a else 0
        d = abs(1 - r) + (0 if bool(re.search(r"\brefill", lab.lower())) == refill else 0.05)
        if len(so) > 1 and re.search(r"\d\s*x\s*\d|,", lab) and not re.search(r"\b(set|with|duo|gift|\+)\b", (k.get("title") or "").lower()):
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


def resolve_text(k: dict, skus: list, vt: str | None = None, asin: str | None = None, product_name: str | None = None) -> dict:
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
    shade_like = "color" in (vt or "").lower() or "scent" in (vt or "").lower() or not vt
    if not shade_like and "size" in (vt or "").lower():
        p = _size_pick(k, F, skus)
        if p:
            return p
    # codes are only trusted as whole tokens; names as whole phrases (longest wins), partial names count a little
    names = {}
    for s in skus:
        code, name = split_value(s.get("value") or s.get("size") or "")
        names[s["sku_id"]] = (code, name)
    all_names = [n for _, n in names.values() if n]
    pn = set(norm(product_name or "").split())
    descs = collections.Counter(norm(s.get("desc") or "") for s in skus)
    ranked = []
    for s in skus:
        code, name = names[s["sku_id"]]
        sc, why = 0.0, []
        if code:
            for f, w in FIELD_W.items():
                toks = F[f].replace("#", " ").split()
                if any(_code_eq(code, t) or _code_eq(code, t.lstrip("no")) for t in toks):
                    sc += w * (1.2 if re.search(r"[a-z]", code) or len(code) >= 3 else 0.9); why.append(f"code {code} in {f}")
                    break
        if name:
            # a name that is part of a longer SKU name found in the same field doesn't count ('Juicy Pink' in 'Juicy Pink Lady')
            for f, w in FIELD_W.items():
                if _has_phrase(F[f], name):
                    longer = [n for n in all_names if n != name and name in n and _has_phrase(F[f], n)]
                    if longer:
                        continue
                    ww = w * (1 + 0.15 * min(len(name.split()), 4))
                    if pn and set(name.split()) <= pn:
                        ww *= 0.3   # the shade has the product line's name
                    sc += ww; why.append(f"name '{name}' in {f}")
                    break
            else:
                if len(_squash(name)) >= 3 and any(_squash(name) == _squash(t) for t in F["_own_raw"].split(" | ") + [F["color"]] if t):
                    sc += FIELD_W["own"]; why.append(f"name '{name}' = selector (punctuation aside)")
                    ranked.append([round(sc, 2), s["sku_id"], "; ".join(why)])
                    continue
                words = [x for x in name.split() if len(x) >= 3 and x not in GENERIC]
                if words:
                    for f in ("own", "color", "title"):
                        hit = [x for x in words if _has_phrase(F[f], x)]
                        if hit and len(hit) == len(words):
                            sc += FIELD_W[f] * 0.6; why.append(f"name words {' '.join(hit)} in {f}")
                            break
        d = norm(s.get("desc") or "")
        if len(d.split()) >= 2 and descs[d] == 1 and any(_has_phrase(F[f], d) for f in ("own", "color", "title")):
            sc += 4; why.append(f"description '{d}' in the listing")
        ranked.append([round(sc, 2), s["sku_id"], "; ".join(why)])
    # a SKU whose code is missing while another SKU's code is in the selector / color / title is another shade
    # ('5.5 Tan' when the title says '5 Tan')
    found_code = {sid for sc_, sid, why in ranked if any(w.startswith("code") and w.split(" in ")[-1] in ("own", "color", "title")
                                                         for w in why.split("; "))}
    for r in ranked:
        code = names[r[1]][0]
        if code and found_code and r[1] not in found_code and r[0] > 0:
            r[0] = round(r[0] - 6, 2); r[2] += "; its code is missing, another SKU's code is there"
    ranked = [tuple(r) for r in ranked]
    ranked.sort(key=lambda x: -x[0])
    # the same shade in several sizes (full size + mini) is one shade: compare shades, then pick the size
    key = {s["sku_id"]: norm(_strip_sizes(re.split(r"\s+[-–|]\s+|\s*\(", s.get("value") or "")[0])) for s in skus}
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
            else:   # no size on Amazon: the full size, unless the listing says mini
                mini = bool(re.search(r"\b(mini|travel|deluxe sample)\b", (k.get("title") or "").lower()))
                sz = sorted(group, key=lambda g: (oz_amounts(g.get("size") or "") or [0])[0])
                pick = (sz[0] if mini else sz[-1])["sku_id"]; how += "; " + ("smallest" if mini else "largest") + " size of that shade"
        return {"sku_id": pick, "score": top[0], "margin": round(top[0] - second, 2), "how": how, "ranked": ranked[:5], "alts": alts}
    if "size" in (vt or "").lower():
        p = _size_pick(k, F, skus)
        if p:
            return p
    return {"sku_id": None, "score": top[0], "margin": round(top[0] - second, 2), "how": "text can't decide", "ranked": ranked[:5]}
