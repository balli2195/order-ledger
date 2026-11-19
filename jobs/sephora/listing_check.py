"""Dead Amazon listings ("dog pages", Nov 19, 2026): ASINs Amazon no longer has a detail page for.

Keepa keeps showing the last title, offers and rank for a while after a listing is taken down, so the daily job could
post a lead nobody can sell into. SP-API knows: getCatalogItem answers NOT_FOUND and the offers call says "invalid
ASIN". sweep(asins) asks searchCatalogItems for 20 ASINs a call (~2 calls a second) and returns the ones not found.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog"))
import sp_restrictions as spr  # noqa: E402  (token(), SP host)


def sweep(asins, log=print) -> set:
    """ASINs (of those given) that Amazon's catalog doesn't have in the US marketplace. Errors -> not counted dead."""
    asins = list(dict.fromkeys(a for a in asins if a))
    dead, checked = set(), 0
    for i in range(0, len(asins), 20):
        ch = asins[i:i + 20]
        q = urllib.parse.urlencode({"identifiers": ",".join(ch), "identifiersType": "ASIN", "marketplaceIds": "ATVPDKIKX0DER",
                                    "includedData": "summaries", "pageSize": 20})
        for attempt in range(6):
            w = spr._last[0] + 0.55 - time.time()
            if w > 0:
                time.sleep(w)
            spr._last[0] = time.time()
            try:
                req = urllib.request.Request(spr.SP + "/catalog/2022-04-01/items?" + q, headers={"x-amz-access-token": spr.token()})
                d = json.loads(urllib.request.urlopen(req, timeout=60).read())
                found = {it.get("asin") for it in d.get("items") or []}
                dead |= {a for a in ch if a not in found}
                checked += len(ch)
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                log(f"catalog search HTTP {e.code}: {e.read()[:150]!r}")
                break
            except Exception as e:  # noqa: BLE001
                log(f"catalog search failed: {e!r}"[:200])
                time.sleep(3)
    log(f"listing check: {checked} ASINs checked, {len(dead)} not on Amazon any more")
    return dead
