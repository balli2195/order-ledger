"""Add a lead's product to Nick's Amazon catalog as soon as a Buy qty is entered (Nick, Nov 3 2026).

Some restrictions (e.g. Transparency codes required) only show up once the ASIN is on his account, so the Hub creates
an offer-only FBA listing (condition New, no stock) whenever a lead's Buy qty is changed to more than 0 and its ASIN
isn't already in his catalog (Nick, Nov 4 2026: only then, not for every recent lead). Amazon's listing issues are read back a few minutes later and a day later and shown on the lead card;
a Transparency issue also becomes a Transparency notice, so the usual warnings and same-brand warnings appear.

SP-API Listings Items 2021-08-01 (needs the app's Product Listing role):
- in the catalog?  GET /listings/2021-08-01/items/{seller}?identifiers=<ASIN>&identifiersType=ASIN
- create           PUT /listings/2021-08-01/items/{seller}/{sku}, requirements LISTING_OFFER_ONLY, productType and the
                   required compliance answers (batteries_required, supplier_declared_dg_hz_regulation) copied from the
                   ASIN's own catalog record (GET /catalog/2022-04-01/items/{asin}), never made up.
- issues           GET /listings/2021-08-01/items/{seller}/{sku}?includedData=summaries,issues
SKUs look like Amazon's own ("AB-1C2D-3E4F"), like the ones Nick makes in Seller Central.
Price (Nick, Nov 3 2026): Buy Box + 10% (Keepa's current Buy Box, or the last one it saw), the same rule as PrepOS's
Sell price. No Buy Box known -> no price.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import string
import time
from datetime import timedelta

import httpx

from . import db, sellable

log = logging.getLogger("ledger.catalog")
RECHECK = [timedelta(minutes=10), timedelta(hours=24)]
COMPLIANCE = ("batteries_required", "supplier_declared_dg_hz_regulation")
status: dict = {"last_run": None, "last_error": None, "added": 0}
_last = [0.0]


def _sku() -> str:
    a = string.ascii_uppercase + string.digits
    p = lambda n: "".join(random.choice(a) for _ in range(n))
    return f"{p(2)}-{p(4)}-{p(4)}"


async def _call(c: httpx.AsyncClient, method: str, path: str, **kw) -> tuple[int, dict]:
    for i in range(6):
        wait = _last[0] + 0.6 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last[0] = time.monotonic()
        r = await c.request(method, sellable.SP + path, headers={"x-amz-access-token": await sellable._token(c),
                                                                 "content-type": "application/json"}, **kw)
        if r.status_code == 429:
            await asyncio.sleep(2 * (i + 1))
            continue
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"text": r.text[:300]}
    return 429, {}


PRICE_UP = 1.10


async def _price(asin: str) -> float | None:
    from .prepos import keepa_prices
    bb = ((await keepa_prices([asin])).get(asin) or {}).get("bb")
    return round(bb * PRICE_UP, 2) if bb else None


def _offer(price: float) -> list:
    return [{"currency": "USD", "marketplace_id": sellable.MP, "audience": "ALL",
             "our_price": [{"schedule": [{"value_with_tax": price}]}]}]


def _issues(items: list) -> list[dict]:
    return [{"code": x.get("code"), "message": x.get("message"), "severity": x.get("severity")} for x in items or []]


async def process(asin: str, preview: bool = False) -> dict:
    """Make sure the ASIN is on Nick's account; returns the stored row (preview=True: Amazon only validates)."""
    asin = asin.strip().upper()
    async with httpx.AsyncClient(timeout=40) as c:
        s, j = await _call(c, "GET", f"/listings/2021-08-01/items/{sellable.SELLER_ID}",
                           params={"marketplaceIds": sellable.MP, "identifiers": asin, "identifiersType": "ASIN", "includedData": "summaries,issues"})
        if s == 403:
            raise PermissionError("Amazon refused the listings call (Product Listing role missing?)")
        if s != 200:
            raise RuntimeError(f"search {asin}: {s} {str(j)[:200]}")
        items = j.get("items") or []
        if items:
            it = items[0]
            return await _save(asin, status_="listed", sku=it.get("sku"), added=False,
                               issues=_issues(it.get("issues")), summary=(it.get("summaries") or [{}])[0])
        s, cat = await _call(c, "GET", f"/catalog/2022-04-01/items/{asin}",
                             params={"marketplaceIds": sellable.MP, "includedData": "attributes,productTypes"})
        if s != 200:
            return await _save(asin, status_="failed", error=f"Amazon has no catalog record for this ASIN ({s})")
        pt = next((p.get("productType") for p in cat.get("productTypes") or [] if p.get("productType")), None)
        attrs = {"condition_type": [{"value": "new_new", "marketplace_id": sellable.MP}],
                 "merchant_suggested_asin": [{"value": asin, "marketplace_id": sellable.MP}],
                 "fulfillment_availability": [{"fulfillment_channel_code": "AMAZON_NA"}]}
        for k in COMPLIANCE:
            if (cat.get("attributes") or {}).get(k):
                attrs[k] = cat["attributes"][k]
        price = await _price(asin)
        if price:
            attrs["purchasable_offer"] = _offer(price)
        sku = _sku()
        s, j = await _call(c, "PUT", f"/listings/2021-08-01/items/{sellable.SELLER_ID}/{sku}",
                           params={"marketplaceIds": sellable.MP, "includedData": "issues", **({"mode": "VALIDATION_PREVIEW"} if preview else {})},
                           json={"productType": pt or "PRODUCT", "requirements": "LISTING_OFFER_ONLY", "attributes": attrs})
        iss = _issues(j.get("issues"))
        if s != 200 or j.get("status") not in ("ACCEPTED", "VALID"):
            return await _save(asin, status_="failed", sku=None, issues=iss,
                               error=f"Amazon didn't accept the listing ({j.get('status') or s})")
        log.info("added %s to the catalog as %s", asin, sku)
        status["added"] += 1
        row = await _save(asin, status_="added", sku=sku, added=True, issues=iss)
        if not preview:
            await _set_price_row(asin, price)
        return row


async def _set_price_row(asin: str, price) -> None:
    async with db.pool.connection() as conn:
        await conn.execute("UPDATE catalog_adds SET price=%s, price_tried_at=now() WHERE asin=%s", (price, asin))
        await conn.commit()


async def set_price(asin: str, sku: str) -> float | None:
    """Give a listing the Hub added (without a price) Buy Box + 10%."""
    price = await _price(asin)
    if price:
        async with httpx.AsyncClient(timeout=40) as c:
            s, cat = await _call(c, "GET", f"/catalog/2022-04-01/items/{asin}", params={"marketplaceIds": sellable.MP, "includedData": "productTypes"})
            pt = next((p.get("productType") for p in cat.get("productTypes") or [] if p.get("productType")), "PRODUCT")
            s, j = await _call(c, "PATCH", f"/listings/2021-08-01/items/{sellable.SELLER_ID}/{sku}",
                               params={"marketplaceIds": sellable.MP, "includedData": "issues"},
                               json={"productType": pt, "patches": [{"op": "replace", "path": "/attributes/purchasable_offer", "value": _offer(price)}]})
        if s != 200 or j.get("status") not in ("ACCEPTED", "VALID"):
            log.warning("price %s %s: %s %s", asin, sku, s, str(j)[:300])
            price = None
        else:
            log.info("priced %s (%s) at %s", asin, sku, price)
    await _set_price_row(asin, price)
    return price


async def recheck(asin: str, sku: str) -> None:
    async with httpx.AsyncClient(timeout=40) as c:
        s, j = await _call(c, "GET", f"/listings/2021-08-01/items/{sellable.SELLER_ID}/{sku}",
                           params={"marketplaceIds": sellable.MP, "includedData": "summaries,issues"})
    if s == 200:
        await _save(asin, status_=None, sku=sku, issues=_issues(j.get("issues")), summary=(j.get("summaries") or [{}])[0], recheck=True)
    elif s == 404:
        await _save(asin, status_=None, sku=sku, recheck=True, error="Amazon doesn't show this listing yet")


async def _save(asin, status_=None, sku=None, added=None, issues=None, summary=None, error=None, recheck=False) -> dict:
    st = (summary or {}).get("status")
    iss = json.dumps(issues) if issues is not None else None
    st = json.dumps(st) if st is not None else None
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """INSERT INTO catalog_adds AS c (asin, status, sku, added_by_hub, issues, listing_status, error, checked_at, rechecks)
                   VALUES (%(a)s, COALESCE(%(st)s::text, 'listed'), %(sku)s, COALESCE(%(add)s::boolean, false), COALESCE(%(iss)s::jsonb, '[]'::jsonb),
                           %(ls)s::jsonb, %(err)s, now(), %(rc)s)
                   ON CONFLICT (asin) DO UPDATE SET status=COALESCE(%(st)s::text, c.status), sku=COALESCE(%(sku)s, c.sku),
                     added_by_hub=COALESCE(%(add)s::boolean, c.added_by_hub), issues=COALESCE(%(iss)s::jsonb, c.issues),
                     listing_status=COALESCE(%(ls)s::jsonb, c.listing_status), error=%(err)s, checked_at=now(), rechecks=c.rechecks + %(rc)s
                   RETURNING *""",
                {"a": asin, "st": status_, "sku": sku, "add": added, "iss": iss, "ls": st, "err": error, "rc": 1 if recheck else 0})
            row = dict(await cur.fetchone())
            tp = [i for i in (issues or []) if re.search(r"transparency", i.get("message") or "", re.I)]
            if tp and sku:
                await conn.execute(
                    "INSERT INTO transparency_notices (asin, ref, kind, title, source) VALUES (%s,%s,'enrolled',%s,'listing') ON CONFLICT DO NOTHING",
                    (asin, f"listing:{sku}", (tp[0].get("message") or "")[:300]))
            cur = await conn.execute("SELECT id FROM leads WHERE upper(btrim(asin))=%s ORDER BY lead_date DESC NULLS LAST LIMIT 20", (asin,))
            ids = [str(r["id"]) for r in await cur.fetchall()]
            if ids:
                await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "lead", "op": "upsert", "ids": ids, "src": "catalog"}),))
    return row


async def run_once() -> int:
    """New ASINs: ones queued because a lead's Buy qty was just changed to more than 0 (Nick, Nov 4 2026). Each is
    checked against his Amazon catalog and added only if it isn't there. Already known to be in the catalog (listed or
    added before) -> dropped without asking Amazon; Amazon says he can't list it -> dropped; a failure is retried a day
    later, at most 3 times. Rechecks: listings the Hub added, 10 minutes and a day after."""
    async with db.pool.connection() as conn:
        await conn.execute(
            """DELETE FROM catalog_queue q USING catalog_adds c
                WHERE c.asin = q.asin AND c.status IN ('listed', 'added')""")
        await conn.execute(
            """DELETE FROM catalog_queue q USING asin_flags f
                WHERE f.asin = q.asin AND f.list_ok IS FALSE""")
        cur = await conn.execute("SELECT asin FROM catalog_queue WHERE next_try <= now() ORDER BY queued_at LIMIT 25")
        new = [r["asin"] for r in await cur.fetchall()]
        cur = await conn.execute(
            """SELECT asin, sku FROM catalog_adds WHERE added_by_hub AND sku IS NOT NULL AND
                 ((rechecks = 0 AND created_at < now() - %s) OR (rechecks = 1 AND created_at < now() - %s)) LIMIT 25""",
            RECHECK)
        later = [(r["asin"], r["sku"]) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT asin, sku FROM catalog_adds WHERE added_by_hub AND sku IS NOT NULL AND price IS NULL AND price_tried_at IS NULL LIMIT 25")
        unpriced = [(r["asin"], r["sku"]) for r in await cur.fetchall()]
    for a in new:
        try:
            row = await process(a)
            done = (row or {}).get("status") != "failed"
        except PermissionError:
            raise
        except Exception as e:
            log.warning("catalog %s failed: %s", a, e)
            await _save(a, status_="failed", error=str(e)[:300])
            done = False
        async with db.pool.connection() as conn:
            if done:
                await conn.execute("DELETE FROM catalog_queue WHERE asin=%s", (a,))
            else:
                await conn.execute("UPDATE catalog_queue SET attempts = attempts + 1, next_try = now() + interval '1 day' WHERE asin=%s", (a,))
                await conn.execute("DELETE FROM catalog_queue WHERE asin=%s AND attempts >= 3", (a,))
    for a, sku in unpriced:
        try:
            await set_price(a, sku)
        except Exception as e:
            log.warning("catalog price %s failed: %s", a, e)
            await _set_price_row(a, None)
    for a, sku in later:
        try:
            await recheck(a, sku)
        except Exception as e:
            log.warning("catalog recheck %s failed: %s", a, e)
    return len(new) + len(later) + len(unpriced)


async def loop() -> None:
    if not sellable.ENABLED:
        return
    await asyncio.sleep(60)
    while True:
        try:
            n = await run_once()
            status.update(last_run=time.strftime("%Y-%m-%d %H:%M:%S"), last_error=None)
        except PermissionError as e:
            n = 0
            status["last_error"] = str(e)
            await asyncio.sleep(3600)
        except Exception as e:
            n = 0
            status["last_error"] = str(e)
            log.exception("catalog run failed")
        await asyncio.sleep(20 if n else 90)
