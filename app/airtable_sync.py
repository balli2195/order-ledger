"""Mirror the Airtable "VA Leads" base into Postgres until cutover.

- Every minute: leads changed since the last pass (by LAST_MODIFIED_TIME), plus all stores and brands.
- Every 6 hours (and on first run): a full pass that also removes leads deleted in Airtable.
Stops on its own once settings.leads_mode becomes "home".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .leads import get_setting

log = logging.getLogger("ledger.airtable")

TOKEN = os.environ.get("AIRTABLE_TOKEN", "")
BASE = os.environ.get("AIRTABLE_BASE", "appRZY2bE1iqg4woe")
AT_API = os.environ.get("AIRTABLE_API", "https://api.airtable.com/v0")
T_LEADS, T_STORES, T_BRANDS = "tblZ7YMTrHt4dzvbn", "tbl1dDAv7cIv8aapI", "tblUEjXRCbNRo7tIt"

F = dict(
    asin="fldRfoFcF0VgDn7Vb", lead_date="fldxDiSYLjoXpmsDa", product_name="fldAMptKtz27UxPLx",
    sourced_by="fldT3auzxYa4D1q1m", store_link="fldRkQPS5o6Fv3pmY", cost="fldG2srrM07bQ8pw0",
    promo_code="fldDblCko1dDoRp3p", va_notes="fldMP0SvA0RPO8VUM", feedback="fldq5qYsnr77IocJd",
    buy_qty="fldmF1offA5I42FV5", qty_ordered="fld7Rz91u31GHboeM", buyer_notes="fld2OwPirvpzkPfe7",
    lead_score_band="fldm6rQIFKyGbkhFx", lead_score_roi="fldAZ0Lv6j4hPUypa", lead_score_notes="fldMK8WrrR6D3MyCT",
    claude_buy_qty="fld0iMIHSJ1vRqr8y", claude_buy_note="fld11L9ftCMJcwB8L", replen="fldZdQaEKqJaZqtNY",
)
F_STORE_LINK = "fld6lGOUgTjlskTqB"
F_BRAND_LINKS = ("fld1rja5jxLo1WfVK", "fldJILFYdKmPQECWS")
# Computed / helper fields we don't copy (we compute our own).
SKIP = {
    "fldeotEwtW85GnBbW", "fldYaXkc4VLbn8KHE", "fldTDe5gBKaE3EECD", "fldJxuAUNldK5Z711", "fldpsNz6241KkjzM4",
    "fldTiAQLaYHJn9LlX", "fld6fOAX6tEvmUaCh", "fldVR4BO6ldRsacJx", "fldo34ur33kP3GVtz",
}
# Leftover columns (old imports, one-off lists) kept in leads.extra under their Airtable names.
EXTRA_NAMES = {
    "fldbsQelAqKxrc53y": "Imported table", "fldM1CWiIISqiNaqm": "Imported table 2", "fldbdcSzCpcWUInBc": "Imported table 2 (2)",
    "fldPf9RPeGcMBbVCZ": "Imported table 3", "fld5ksQ555rFMsEhI": "Imported table 3 (2)", "fldL3YqSXqRqS9oCd": "Imported table 4",
    "fldbaY2jvvhLjhtrj": "11/20", "fldEUuZOYmxc49qL1": "Imported table 2 (3)", "fldAXLVwt70I9rDkC": "12/18",
    "fld5puvLTmLDqDizd": "12/30", "fld9LyV8yW0X93Gdo": "12/30 (2)", "fldKO5Q4bg3XOpHM7": "Imported table 5",
    "fldBH79wEo2kkRJlI": "Imported table 2 (4)", "fldBqGwfJCJgstMLc": "Imported table 2 (5)", "fldbMXhXusiYx3i6s": "Imported table 2 (6)",
    "fldr8JIS2Wi6iolsV": "Most profitable leads 6/11", "fldlNm3zVtKstBip3": "Imported table 2 (7)", "fld5xMIUCh6d7UgUP": "units",
    "fldDV39cIReAO296w": "Most profitable leads last 30 days 8/1/25", "fldhQC69Va9B33RCj": "Most profitable last 30 days 9/3/25",
    "fldgWQEm6rI1SkVtb": "Most profitable last 30 days 10/16/25", "fldwVWaTGmPgoaU72": "Lost/Damaged FBA",
    "fldDK0gOjtQZr0cnz": "Most profitable last 90 days 1/29/26", "fldRfZiGHrwVQZb4c": "Imported table 6",
}

INCREMENTAL_EVERY = 60
FULL_EVERY = 6 * 3600

status: dict = {"running": False, "last_incremental": None, "last_full": None, "last_error": None, "last_counts": {}}


async def _pages(client: httpx.AsyncClient, table: str, params: dict) -> list[dict]:
    out, offset = [], None
    while True:
        p = dict(params, pageSize=100, returnFieldsByFieldId="true")
        if offset:
            p["offset"] = offset
        for attempt in range(5):
            r = await client.get(f"{AT_API}/{BASE}/{table}", params=p)
            if r.status_code == 429:
                await asyncio.sleep(30)
                continue
            r.raise_for_status()
            break
        j = r.json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return out
        await asyncio.sleep(0.22)  # stay under Airtable's 5 requests/second per base


async def _sync_named(conn, client, table: str, name_fld: str, can_fld: str, kind: str) -> dict[str, str]:
    recs = await _pages(client, table, {"fields[]": [name_fld, can_fld]})
    idmap = {}
    for r in recs:
        name = (r["fields"].get(name_fld) or "").strip()
        if not name:
            continue
        idmap[r["id"]] = name
        await conn.execute(
            f"INSERT INTO {kind} (name, can_source, airtable_id) VALUES (%s,%s,%s) "
            f"ON CONFLICT (name) DO UPDATE SET can_source=EXCLUDED.can_source, airtable_id=EXCLUDED.airtable_id, "
            f"updated_at=CASE WHEN {kind}.can_source IS DISTINCT FROM EXCLUDED.can_source THEN now() ELSE {kind}.updated_at END",
            (name, bool(r["fields"].get(can_fld)), r["id"]),
        )
    return idmap


def _row(rec: dict, stores: dict[str, str], brands: dict[str, str]) -> dict:
    f = rec["fields"]
    row = {}
    for k, fid in F.items():
        v = f.get(fid)
        if isinstance(v, dict) and "name" in v:   # single select
            v = v["name"]
        if isinstance(v, str):
            v = v.strip() or None
        row[k] = v
    if row["asin"]:
        row["asin"] = row["asin"].upper()
    row["lead_date"] = (row["lead_date"] or "")[:10] or None
    for k in ("buy_qty", "qty_ordered", "lead_score_band", "claude_buy_qty"):
        if row[k] is not None:
            try:
                row[k] = int(round(float(row[k])))
            except (TypeError, ValueError):
                row[k] = None
    row["replen"] = bool(row["replen"])
    row["stores"] = sorted({stores.get(x, x) for x in f.get(F_STORE_LINK, [])})
    row["brands"] = sorted({brands.get(x, x) for fl in F_BRAND_LINKS for x in f.get(fl, [])})
    extra = {}
    for fid, name in EXTRA_NAMES.items():
        v = f.get(fid)
        if v not in (None, "", []):
            extra[name] = v
    row["extra"] = json.dumps(extra)
    row["airtable_id"] = rec["id"]
    row["created_at"] = rec.get("createdTime")
    return row


_SYNC_COLS = ['asin', 'lead_date', 'product_name', 'sourced_by', 'stores', 'brands', 'store_link', 'cost', 'promo_code', 'va_notes', 'feedback', 'buy_qty', 'qty_ordered', 'buyer_notes', 'lead_score_band', 'lead_score_roi', 'lead_score_notes', 'claude_buy_qty', 'claude_buy_note', 'replen', 'extra']


def _keep(c: str) -> str:
    # A field edited on the site keeps the site's value; everything else follows Airtable.
    return f"CASE WHEN '{c}' = ANY(leads.local_edits) THEN leads.{c} ELSE EXCLUDED.{c} END"


UPSERT = (
    "INSERT INTO leads (airtable_id, " + ", ".join(_SYNC_COLS) + ", created_at) VALUES (%(airtable_id)s, "
    + ", ".join(f"%({c})s" for c in _SYNC_COLS) + ", COALESCE(%(created_at)s::timestamptz, now())) "
    "ON CONFLICT (airtable_id) DO UPDATE SET " + ", ".join(f"{c}={_keep(c)}" for c in _SYNC_COLS) + ", updated_at=now() "
    "WHERE (" + ", ".join(f"leads.{c}" for c in _SYNC_COLS) + ") IS DISTINCT FROM ("
    + ", ".join(_keep(c) for c in _SYNC_COLS) + ") RETURNING id"
)


async def sync_once(full: bool) -> dict:
    started = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=60, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        async with db.pool.connection() as conn:
            async with conn.transaction():
                stores = await _sync_named(conn, client, T_STORES, "fldA6H5koCfWhuIQ1", "fldBc0kqarofQPbZ5", "stores")
                brands = await _sync_named(conn, client, T_BRANDS, "fld5731MAIhxilIJr", "fldawk4W9pYFJrXYI", "brands")
        params: dict = {"fields[]": list(F.values()) + [F_STORE_LINK, *F_BRAND_LINKS, *EXTRA_NAMES]}
        since = None if full else await get_setting("leads_sync_cursor", None)
        if since:
            # 3-minute overlap so nothing slips between passes.
            ts = (datetime.fromisoformat(since) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            params["filterByFormula"] = f"IS_AFTER(LAST_MODIFIED_TIME(), '{ts}')"
        recs = await _pages(client, T_LEADS, params)

    changed, removed = [], 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT airtable_id FROM lead_tombstones")
            gone = {r["airtable_id"] for r in await cur.fetchall()}   # deleted on the site
            for rec in recs:
                if rec["id"] in gone:
                    continue
                cur = await conn.execute(UPSERT, _row(rec, stores, brands))
                hit = await cur.fetchone()
                if hit:
                    changed.append(hit["id"])
            if full:
                seen = [r["id"] for r in recs]
                cur = await conn.execute(
                    "DELETE FROM leads WHERE airtable_id IS NOT NULL AND NOT (airtable_id = ANY(%s)) RETURNING id", (seen,))
                removed = len(await cur.fetchall())
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('leads_sync_cursor', %s), ('leads_last_sync', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (json.dumps(started.isoformat()), json.dumps(started.isoformat())),
            )
            if changed or removed:
                payload = {"t": "lead", "op": "upsert", "ids": [str(i) for i in changed], "by": "Airtable", "src": "airtable"}
                if len(changed) > 50 or removed:
                    payload = {"t": "lead", "op": "reload", "by": "Airtable", "src": "airtable"}
                await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps(payload),))
    counts = {"fetched": len(recs), "changed": len(changed), "removed": removed, "stores": len(stores), "brands": len(brands)}
    status["last_counts"] = counts
    status["last_full" if full else "last_incremental"] = started.isoformat()
    return counts


async def loop() -> None:
    if not TOKEN:
        log.info("AIRTABLE_TOKEN not set; leads mirror off")
        return
    last_full = 0.0
    while True:
        try:
            if await get_setting("leads_mode", "mirror") != "mirror":
                status["running"] = False
                await asyncio.sleep(300)
                continue
            status["running"] = True
            now = asyncio.get_event_loop().time()
            never = await get_setting("leads_sync_cursor", None) is None
            full = never or (now - last_full) > FULL_EVERY
            counts = await sync_once(full)
            if full:
                last_full = now
            if counts["changed"] or counts["removed"]:
                log.info("airtable %s sync: %s", "full" if full else "incremental", counts)
            status["last_error"] = None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep going; show the error on the page
            status["last_error"] = f"{type(e).__name__}: {e}"
            log.warning("airtable sync failed: %s", e)
        await asyncio.sleep(INCREMENTAL_EVERY)

