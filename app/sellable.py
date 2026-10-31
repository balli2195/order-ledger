"""Can Nick sell this ASIN through FBA? Asked of Amazon directly (Nick, Oct 30 2026).

SP-API FBA Inbound Eligibility: GET /fba/inbound/v1/eligibility/itemPreview?asin=&program=INBOUND
- works for ASINs not yet listed on his account; no Keepa tokens; ~1 request/second
- isEligibleForProgram false + ineligibilityReasonList codes (FBA_INB_xxxx) = can't send it to FBA right now
  (hazmat review, needs an SDS, no FC can receive it, ...). Checked Oct 30: B0050CZ1KW -> FBA_INB_0007 + 0050.
Results are cached on asin_flags (fba_eligible, fba_reasons, fba_checked_at), re-checked after 14 days, and the
last 60 days of leads are backfilled in the background.
Listing restrictions (Nick, Oct 31 2026): GET /listings/2021-08-01/restrictions?asin=&sellerId=&conditionType=new_new
says whether Nick's account may list the ASIN at all (brand gating "APPROVAL_REQUIRED" with an apply-to-sell link, or
"NOT_ELIGIBLE"). Needs the app's Product Listing role; while Amazon answers 403 the listing part is skipped and the
FBA part still runs. Cached on asin_flags.list_ok / list_reasons / list_checked_at.
Env: SPAPI_CLIENT_ID, SPAPI_CLIENT_SECRET, SPAPI_REFRESH_TOKEN.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import db

log = logging.getLogger("ledger.sellable")
CID, CS, RT = (os.environ.get(k, "") for k in ("SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN"))
ENABLED = bool(CID and CS and RT)
SP = "https://sellingpartnerapi-na.amazon.com"
MP = "ATVPDKIKX0DER"
SELLER_ID = os.environ.get("SPAPI_SELLER_ID", "A2UYRB3AYUMGAJ")
listing_access = {"ok": None}   # None = not asked yet, False = Amazon refuses (no Product Listing role), True = works
TZ = ZoneInfo("America/Chicago")
RECHECK_DAYS = 14
LOOKBACK_DAYS = 60
BATCH = 100
status: dict = {"last_batch": None, "last_error": None}

# Amazon's reasons, in plain words. Codes not listed here are shown as the code.
REASONS = {
    "FBA_INB_0004": "Amazon is missing the package dimensions for this product.",
    "FBA_INB_0006": "Amazon can't find this product (unknown SKU/ASIN).",
    "FBA_INB_0007": "Product Under Dangerous Goods (Hazmat) Review: Amazon doesn't have enough information to classify it. Until it's cleared it can't be sold or sent to FBA (usually needs a safety data sheet or exemption sheet).",
    "FBA_INB_0008": "Dangerous Goods (Hazmat) review: Amazon needs battery information (a battery exemption sheet).",
    "FBA_INB_0009": "Dangerous Goods (Hazmat) review: the hazmat information is insufficient. Amazon needs a safety data sheet (SDS).",
    "FBA_INB_0010": "Dangerous Goods (Hazmat) review: the hazmat information doesn't match. Amazon needs compliant documents.",
    "FBA_INB_0011": "Dangerous Goods (Hazmat) review: the hazmat information is incomplete, inaccurate or contradictory.",
    "FBA_INB_0012": "The listing's title, bullets, images or description conflict with the detail page or other offers.",
    "FBA_INB_0013": "Amazon needs more information to finish its hazmat review.",
    "FBA_INB_0014": "Flagged as possible dangerous goods: under hazmat review (usually 4–7 business days); can't be received at FBA until done.",
    "FBA_INB_0015": "Classified as an unfulfillable dangerous good: it can't be sold through FBA.",
    "FBA_INB_0016": "Classified as a fulfillable dangerous good: you may need to be in the FBA Dangerous Goods (Hazmat) program.",
    "FBA_INB_0050": "No Amazon fulfillment center in the US can currently receive this product.",
    "FBA_INB_0097": "Fully regulated dangerous good: it can't go to FBA.",
}
HAZMAT_CODES = {"FBA_INB_0007", "FBA_INB_0008", "FBA_INB_0009", "FBA_INB_0010", "FBA_INB_0011", "FBA_INB_0013",
                "FBA_INB_0014", "FBA_INB_0015", "FBA_INB_0016", "FBA_INB_0097"}

_tok: dict = {}
_lock = asyncio.Lock()
_last = [0.0]


async def _token(c: httpx.AsyncClient) -> str:
    if _tok.get("exp", 0) < time.time():
        r = (await c.post("https://api.amazon.com/auth/o2/token", data=dict(
            grant_type="refresh_token", refresh_token=RT, client_id=CID, client_secret=CS))).json()
        _tok.update(v=r["access_token"], exp=time.time() + 3000)
    return _tok["v"]


async def eligibility(asin: str) -> dict | None:
    """{eligible: bool, reasons: [codes]} from Amazon, or None if Amazon couldn't be asked."""
    if not ENABLED:
        return None
    async with _lock:   # one call at a time, about one a second (Amazon's limit)
        async with httpx.AsyncClient(timeout=30) as c:
            for i in range(6):
                wait = _last[0] + 1.05 - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                _last[0] = time.monotonic()
                r = await c.get(f"{SP}/fba/inbound/v1/eligibility/itemPreview", headers={"x-amz-access-token": await _token(c)},
                                params={"asin": asin, "program": "INBOUND", "marketplaceIds": MP})
                if r.status_code == 429:
                    await asyncio.sleep(2 * (i + 1))
                    continue
                if r.status_code != 200:
                    log.warning("eligibility %s: %s %s", asin, r.status_code, r.text[:200])
                    return None
                p = r.json().get("payload") or {}
                return {"eligible": bool(p.get("isEligibleForProgram")), "reasons": p.get("ineligibilityReasonList") or []}
    return None


async def restrictions(asin: str) -> dict | None:
    """{ok: bool, reasons: [{code, message, link}]} for listing this ASIN new on Nick's account, or None if Amazon can't be asked."""
    if not ENABLED or listing_access["ok"] is False and time.time() < listing_access.get("retry_at", 0):
        return None
    async with _lock:
        async with httpx.AsyncClient(timeout=30) as c:
            for i in range(6):
                wait = _last[0] + 0.25 - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                _last[0] = time.monotonic()
                r = await c.get(f"{SP}/listings/2021-08-01/restrictions", headers={"x-amz-access-token": await _token(c)},
                                params={"asin": asin, "sellerId": SELLER_ID, "marketplaceIds": MP, "conditionType": "new_new"})
                if r.status_code == 429:
                    await asyncio.sleep(2 * (i + 1))
                    continue
                if r.status_code == 403:   # app/token without the Product Listing role: skip, ask again in an hour
                    if listing_access["ok"] is not False:
                        log.warning("listing restrictions: Amazon refuses (403); the app needs the Product Listing role and a new refresh token")
                    listing_access.update(ok=False, retry_at=time.time() + 3600)
                    return None
                if r.status_code != 200:
                    log.warning("restrictions %s: %s %s", asin, r.status_code, r.text[:200])
                    return None
                listing_access["ok"] = True
                reasons = []
                for x in r.json().get("restrictions") or []:
                    for rr in x.get("reasons") or []:
                        link = next((l.get("resource") for l in rr.get("links") or [] if l.get("resource")), None)
                        reasons.append({"code": rr.get("reasonCode"), "message": rr.get("message"), "link": link})
                return {"ok": not reasons, "reasons": reasons}
    return None


async def check(asins: list[str]) -> dict:
    """Ask Amazon about each ASIN and cache it. Returns ASIN -> {eligible, reasons}."""
    out = {}
    for a in dict.fromkeys((x or "").strip().upper() for x in asins):
        if len(a) != 10:
            continue
        try:
            e = await eligibility(a)
        except Exception as ex:
            log.warning("eligibility %s failed: %s", a, ex)
            e = None
        try:
            lr = await restrictions(a)
        except Exception as ex:
            log.warning("restrictions %s failed: %s", a, ex)
            lr = None
        if e is None and lr is None:
            continue
        out[a] = {**(e or {}), "listing": lr}
    if out:
        async with db.pool.connection() as conn:
            async with conn.transaction():
                for a, e in out.items():
                    if "eligible" in e:
                        await conn.execute(
                            "INSERT INTO asin_flags (asin, fba_eligible, fba_reasons, fba_checked_at) VALUES (%s,%s,%s,now()) "
                            "ON CONFLICT (asin) DO UPDATE SET fba_eligible=EXCLUDED.fba_eligible, fba_reasons=EXCLUDED.fba_reasons, fba_checked_at=now()",
                            (a, e["eligible"], e["reasons"]))
                    if e.get("listing") is not None:
                        await conn.execute(
                            "INSERT INTO asin_flags (asin, list_ok, list_reasons, list_checked_at) VALUES (%s,%s,%s,now()) "
                            "ON CONFLICT (asin) DO UPDATE SET list_ok=EXCLUDED.list_ok, list_reasons=EXCLUDED.list_reasons, list_checked_at=now()",
                            (a, e["listing"]["ok"], json.dumps(e["listing"]["reasons"])))
                cur = await conn.execute("SELECT id FROM leads WHERE upper(btrim(asin)) = ANY(%s) LIMIT 50", (list(out),))
                ids = [str(r["id"]) for r in await cur.fetchall()]
                if ids:
                    await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "lead", "op": "upsert", "ids": ids, "src": "sellable"}),))
    return out


async def is_fresh(asin: str) -> bool:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT fba_checked_at, list_checked_at FROM asin_flags WHERE asin=%s", ((asin or "").strip().upper(),))
        r = await cur.fetchone()
    old = datetime.now(TZ) - timedelta(days=RECHECK_DAYS)
    fresh_fba = bool(r and r["fba_checked_at"] and r["fba_checked_at"] > old)
    fresh_list = bool(r and r["list_checked_at"] and r["list_checked_at"] > old) or listing_access["ok"] is False
    return fresh_fba and fresh_list


async def ensure(asin: str) -> None:
    if asin and not await is_fresh(asin):
        await check([asin])


async def backfill_once() -> int:
    now = datetime.now(TZ)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT a FROM (SELECT upper(btrim(l.asin)) a, max(l.lead_date) d FROM leads l "
            "WHERE l.lead_date >= %s AND length(btrim(l.asin)) = 10 GROUP BY 1) x "
            "LEFT JOIN asin_flags f ON f.asin = x.a "
            "WHERE f.fba_checked_at IS NULL OR f.fba_checked_at < now() - make_interval(days => %s) "
            "   OR (%s AND (f.list_checked_at IS NULL OR f.list_checked_at < now() - make_interval(days => %s))) "
            "ORDER BY (f.fba_checked_at IS NULL) DESC, (f.list_checked_at IS NULL) DESC, x.d DESC LIMIT %s",
            (now.date() - timedelta(days=LOOKBACK_DAYS), RECHECK_DAYS, listing_access["ok"] is not False, RECHECK_DAYS, BATCH))
        asins = [r["a"] for r in await cur.fetchall()]
    if not asins:
        return 0
    res = await check(asins)
    status["last_batch"] = {"at": now.isoformat(timespec="seconds"), "asked": len(asins), "answered": len(res),
                            "not_eligible": sum(1 for e in res.values() if e.get("eligible") is False),
                            "cant_list": sum(1 for e in res.values() if (e.get("listing") or {}).get("ok") is False),
                            "listing_access": listing_access["ok"]}
    log.info("sellable backfill: %s", status["last_batch"])
    return len(res)


async def backfill_loop() -> None:
    if not ENABLED:
        log.info("SP-API keys not set; FBA eligibility checks off")
        return
    log.info("FBA eligibility backfill starts in 45 s")
    await asyncio.sleep(45)
    while True:
        try:
            n = await backfill_once()
            status["last_error"] = None
        except Exception as e:
            n = 0
            status["last_error"] = str(e)
            log.exception("eligibility backfill failed")
        await asyncio.sleep(30 if n else 900)
