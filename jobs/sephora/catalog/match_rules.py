#!/usr/bin/env python3
"""Match rules learned from Nick's wrong-match marks (Nov 11, 2026).

Nick marks a wrong Amazon ASIN <-> Sephora product match on the Hub (Sephora leads tab or Sephora relist page) and says
why with tags: size, shade, concentration, set, bonus, multipack, version, product, other (+ a free-text note).
The Hub keeps them in match_feedback; GET /api/engine/sephora/feedback returns them as `wrong_matches`.

derive(wrong_matches, manual) -> rules
  - A tag with an automatic check (size, shade, concentration, set, bonus, multipack) becomes a STRICT check for that
    brand after 1 mark, and for every brand once the tag shows up in 3+ brands.
  - version / product / other have no automatic check: a Claude session reads their notes (weekly run, RUNBOOK step 4b)
    and, where a pattern is clear, adds a manual rule to Hub doc sephora_jobs/match_rules:
      {"manual": [{"brand": "Chanel" | "*", "kind": "block_term" | "require_term", "side": "amazon" | "sephora",
                   "term": "tester", "note": "why (from which marks)"}]}
violations(rules, brand, amazon_title, amazon_size, amazon_color, sephora_name, sephora_shade) -> [reason, ...]
  Used by catalog/match.py (a violating match is capped at confidence low, which the daily watch never proposes),
  by the weekly recheck of existing catalog pairs, and by the daily watch as a last filter.
No network; stdlib only.
"""
import json
import os
import re
from collections import defaultdict

from match import base_text, conc, flags, pack_qty, size_ml, tokens

AUTO_TAGS = ("size", "shade", "concentration", "set", "bonus", "multipack")
GLOBAL_AFTER_BRANDS = 3
BONUS_RE = re.compile(r"\b(bonus|complimentary|free gift|gift with|with free|plus free|\+\s*free|sheet mask|masque|acne patch)\b")
HERE = os.path.dirname(os.path.abspath(__file__))
CAT = os.environ.get("SEPH_DATA") or os.path.join(os.path.dirname(HERE), ".data")
RULES_FILE = os.path.join(CAT, "match_rules.json")


def _b(brand):
    return (brand or "").strip().lower()


def derive(wrong_matches, manual=None):
    """Rules from the Hub's wrong_matches rows (+ manual rules from the Hub doc)."""
    by_tag = defaultdict(lambda: defaultdict(list))   # tag -> brand -> [example]
    for w in wrong_matches or []:
        for t in w.get("tags") or []:
            if t in AUTO_TAGS:
                by_tag[t][_b(w.get("brand"))].append({"asin": w.get("asin"), "sephora": w.get("sephora_name"), "note": w.get("note")})
    auto = []
    for t, brands in by_tag.items():
        named = [b for b in brands if b]
        scope = ["*"] if len(named) >= GLOBAL_AFTER_BRANDS else (named or ["*"])
        for b in scope:
            ex = [e for bb, es in brands.items() if b == "*" or bb == b for e in es]
            auto.append({"brand": b, "kind": t, "marks": len(ex), "examples": ex[:3]})
    return {"auto": sorted(auto, key=lambda r: (r["kind"], r["brand"])), "manual": list(manual or [])}


def save(rules, path=RULES_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(rules, open(path, "w"), indent=1)


def load(path=RULES_FILE):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {"auto": [], "manual": []}


def _applies(rule, brand):
    rb = _b(rule.get("brand"))
    return rb in ("", "*") or rb == _b(brand)


def _conc(text):
    c = conc(text)
    if "edp" in c:   # "Eau de Parfum" also trips the bare "parfum" pattern
        c.discard("parfum")
    return c


def _shade_words(s):
    return set(tokens(s or "", set()))


def violations(rules, brand, amazon_title, amazon_size=None, amazon_color=None, sephora_name=None, sephora_shade=None,
               sephora_sizes=None, sephora_multi=False):
    """Reasons this pair breaks a learned rule ([] = fine). sephora_sizes: ml values Sephora sells, when known;
    sephora_multi: Sephora sells several sizes (Nick confirms which one on the tab), so mini vs full isn't judged here."""
    out = []
    at, st = amazon_title or "", sephora_name or ""
    a_full = at + " " + (amazon_size or "")
    for r in (rules or {}).get("auto", []):
        if not _applies(r, brand):
            continue
        k, why = r["kind"], None
        if k == "size":
            fa, fs = flags(a_full), flags(st)
            ml = size_ml(a_full)
            if fa["mini"] != fs["mini"] and not sephora_sizes and not sephora_multi:
                why = "mini vs full size differs"
            elif ml and sephora_sizes and all(abs(ml - s) / max(s, 1) > 0.12 for s in sephora_sizes):
                why = f"Amazon size {ml:g} ml isn't a size Sephora sells"
        elif k == "shade":
            aw, sw = _shade_words(amazon_color), _shade_words(sephora_shade)
            if aw and sw and not (aw & sw):
                why = f"shade differs ({amazon_color} vs {sephora_shade})"
        elif k == "concentration":
            ca, cs = _conc(at), _conc(st)
            if ca and cs and ca != cs:   # both say what they are, and they differ
                why = "scent concentration differs (%s vs %s)" % ("/".join(sorted(ca)) or "none", "/".join(sorted(cs)) or "none")
        elif k == "set":
            if flags(a_full)["set"] != flags(st)["set"]:
                why = "set vs single"
        elif k == "bonus":
            if BONUS_RE.search(base_text(at)) and not BONUS_RE.search(base_text(st)):
                why = "Amazon listing adds a bonus item"
        elif k == "multipack":
            if pack_qty(at, amazon_size) > 1:
                why = "Amazon multi-pack"
        if why:
            out.append(f"rule ({k}, {r['brand']}): {why}")
    for r in (rules or {}).get("manual", []):
        if not _applies(r, brand) or not r.get("term"):
            continue
        txt = base_text(st if r.get("side") == "sephora" else at)
        has = re.search(r"\b" + re.escape(base_text(r["term"])) + r"\b", txt) is not None
        if (r.get("kind") == "block_term" and has) or (r.get("kind") == "require_term" and not has):
            out.append(f"rule ({r.get('kind')} '{r['term']}', {r.get('brand') or '*'}): {r.get('note') or ''}".rstrip(": "))
    return out
