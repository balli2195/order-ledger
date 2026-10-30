"""Hazmat flags per ASIN (Nick, Oct 30 2026).

From one Keepa /product call (history=1, 1 token per ASIN, the same call that fills in the product title):
- hazmat     = Keepa `hazardousMaterials` is non-empty
- fba_ever   = csv[34] (COUNT_NEW_FBA: number of New FBA offers over time) was ever above 0, or csv[10] (NEW_FBA
               price) has a positive price. csv[10] is only filled when offers are requested (6+ tokens), so the
               free-with-history FBA offer count is the series that decides it (checked Oct 30 2026: B0050CZ1KW has
               no csv[34], only csv[35] FBM counts; ASINs Nick sells via FBA all have csv[34]).
- amazon_ever= csv[0]  (Amazon price history) has a positive price
level "strong" = hazmat and neither FBA nor Amazon has ever been on the listing: it will very likely sit in
                 Amazon's Dangerous Goods review and needs an SDS / exemption sheet before it can go to FBA.
level "soft"   = hazmat, but FBA or Amazon has sold it before (doable; may still need an SDS).
level "none"   = not hazmat.

Flags are cached per ASIN in asin_flags. A background loop checks the last ~60 days of leads in batches of 100 and
re-checks flags older than ~14 days. It pauses when Keepa tokens run low, because the Lead Score server and the
nightly runs use the same key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import db

log = logging.getLogger("ledger.hazmat")
KEEPA_KEY = os.environ.get("KEEPA_KEY", "")
TZ = ZoneInfo("America/Chicago")
BATCH = 100
RECHECK_DAYS = 14
LOOKBACK_DAYS = 60
TOKEN_FLOOR = 600          # don't start a batch below this many Keepa tokens (batch costs ~100)
QUIET_HOURS = (2, 5)       # skip the backfill 2–5 AM Central while the nightly liquidation run uses Keepa
status: dict = {"last_batch": None, "last_error": None, "tokens_left": None, "paused": None}


def _ever(series) -> bool:
    """Keepa price series [time, price, time, price, ...]: any positive price?"""
    if not series:
        return False
    return any(isinstance(v, (int, float)) and v > 0 for v in series[1::2])


def level_of(hazmat: bool, fba_ever: bool, amazon_ever: bool) -> str:
    if not hazmat:
        return "none"
    return "soft" if (fba_ever or amazon_ever) else "strong"


async def keepa_products(asins: list[str]) -> tuple[dict, int | None]:
    """ASIN -> info dict, plus Keepa tokensLeft. One request per call (up to 100 ASINs)."""
    out: dict[str, dict] = {}
    if not KEEPA_KEY or not asins:
        return out, None
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.get("https://api.keepa.com/product",
                        params={"key": KEEPA_KEY, "domain": 1, "asin": ",".join(asins), "history": 1})
    j = r.json()
    for p in j.get("products") or []:
        csv = p.get("csv") or []
        hz = p.get("hazardousMaterials") or []
        out[p.get("asin")] = {
            "title": (p.get("title") or "").strip() or None,
            "hazmat": bool(hz), "hazmat_info": hz,
            "fba_ever": _ever(csv[34] if len(csv) > 34 else None) or _ever(csv[10] if len(csv) > 10 else None),
            "amazon_ever": _ever(csv[0] if len(csv) > 0 else None),
        }
    tl = j.get("tokensLeft")
    status["tokens_left"] = tl
    return out, tl


async def save(infos: dict) -> None:
    if not infos:
        return
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for asin, x in infos.items():
                lv = level_of(x["hazmat"], x["fba_ever"], x["amazon_ever"])
                await conn.execute(
                    "INSERT INTO asin_flags (asin, hazmat, hazmat_info, fba_ever, amazon_ever, level, title, checked_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (asin) DO UPDATE SET hazmat=EXCLUDED.hazmat, "
                    "hazmat_info=EXCLUDED.hazmat_info, fba_ever=EXCLUDED.fba_ever, amazon_ever=EXCLUDED.amazon_ever, "
                    "level=EXCLUDED.level, title=COALESCE(EXCLUDED.title, asin_flags.title), checked_at=now()",
                    (asin, x["hazmat"], json.dumps(x["hazmat_info"]), x["fba_ever"], x["amazon_ever"], lv, x["title"]))
            ids = []
            cur = await conn.execute("SELECT id FROM leads WHERE upper(btrim(asin)) = ANY(%s) LIMIT 200", (list(infos),))
            ids = [str(r["id"]) for r in await cur.fetchall()]
            if ids:   # leads pages refresh those rows
                await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "lead", "op": "upsert", "ids": ids[:50], "src": "hazmat"}),))


async def check(asins: list[str]) -> dict:
    """Look up ASINs on Keepa now (1 token each) and cache the flags. Returns ASIN -> info."""
    asins = [a for a in dict.fromkeys((a or "").strip().upper() for a in asins) if len(a) == 10]
    if not asins:
        return {}
    try:
        infos, _ = await keepa_products(asins[:BATCH])
        await save(infos)
        return infos
    except Exception as e:
        log.warning("hazmat check failed for %s: %s", asins[:3], e)
        return {}


async def get(asin: str) -> dict | None:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM asin_flags WHERE asin=%s", ((asin or "").strip().upper(),))
        r = await cur.fetchone()
    if not r:
        return None
    d = dict(r)
    if d.get("checked_at"):
        d["checked_at"] = d["checked_at"].isoformat()
    return d


async def is_fresh(asin: str) -> bool:
    f = await get(asin)
    return bool(f) and bool(f.get("checked_at")) and datetime.fromisoformat(f["checked_at"]) > datetime.now(TZ) - timedelta(days=RECHECK_DAYS)


async def _tokens() -> int | None:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            j = (await c.get("https://api.keepa.com/token", params={"key": KEEPA_KEY})).json()   # free call
        return j.get("tokensLeft")
    except Exception:
        return None


async def backfill_once() -> int:
    """Check one batch of recent-lead ASINs that have no flag or a stale one. Returns how many were checked."""
    now = datetime.now(TZ)
    if QUIET_HOURS[0] <= now.hour < QUIET_HOURS[1]:
        status["paused"] = "quiet hours (nightly liquidation run)"
        return 0
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT a FROM (SELECT DISTINCT upper(btrim(l.asin)) a, max(l.lead_date) d FROM leads l "
            "WHERE l.lead_date >= %s AND length(btrim(l.asin)) = 10 GROUP BY 1) x "
            "LEFT JOIN asin_flags f ON f.asin = x.a WHERE f.asin IS NULL OR f.checked_at < now() - make_interval(days => %s) "
            "ORDER BY (f.asin IS NULL) DESC, x.d DESC LIMIT %s",
            (now.date() - timedelta(days=LOOKBACK_DAYS), RECHECK_DAYS, BATCH))
        asins = [r["a"] for r in await cur.fetchall()]
    if not asins:
        status["paused"] = None
        return 0
    tl = await _tokens()
    status["tokens_left"] = tl
    if tl is None or tl < TOKEN_FLOOR + len(asins):
        if status.get("paused") != "tokens":
            log.info("hazmat backfill waiting for Keepa tokens (%s left, %s ASINs to check)", tl, len(asins))
        status["paused"] = "tokens"
        return 0
    status["paused"] = None
    infos, tl = await keepa_products(asins)
    await save(infos)
    status["last_batch"] = {"at": now.isoformat(timespec="seconds"), "checked": len(infos),
                            "strong": sum(1 for x in infos.values() if level_of(x["hazmat"], x["fba_ever"], x["amazon_ever"]) == "strong"),
                            "tokens_left": tl}
    log.info("hazmat backfill: %s", status["last_batch"])
    return len(infos)


async def backfill_loop() -> None:
    if not KEEPA_KEY:
        log.info("KEEPA_KEY not set; hazmat checks off")
        return
    log.info("hazmat backfill starts in 60 s")
    await asyncio.sleep(60)
    while True:
        try:
            n = await backfill_once()
            status["last_error"] = None
        except Exception as e:
            n = 0
            status["last_error"] = str(e)
            log.exception("hazmat backfill failed")
        await asyncio.sleep(90 if n else 900)   # keep going batch by batch while there's work; else check every 15 min
