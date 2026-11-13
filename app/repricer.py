"""Repricer page: a control panel for Nick's own repricer, which runs inside the offer recorder service
(https://offer-recorder.onrender.com, repo balli2195/offer-recorder), not on the Hub.

The recorder is the only place repricer settings live; this module just relays reads and owner changes to it
server-to-server (the recorder key never reaches the browser) and writes every change to the Hub activity log.
Recorder key: Render env RECORDER_KEY, or saved once from the page by an owner (settings key 'recorder_key').
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx

from . import db

URL = os.environ.get("RECORDER_URL", "https://offer-recorder.onrender.com").rstrip("/")
SETTABLE = ("enabled", "skus", "max_step_pct", "min_interval_s", "max_per_day", "chase_window_min", "chase_cuts", "freeze_h", "max_drop_pct",
            "decay_per_day", "decay_auto", "min_gain_abs", "min_gain_rel")


async def _key() -> str:
    k = os.environ.get("RECORDER_KEY", "")
    if k:
        return k
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT value #>> '{}' AS v FROM settings WHERE key = 'recorder_key'")
        row = await cur.fetchone()
    return (row or {}).get("v") or ""


async def key_source() -> str:
    if os.environ.get("RECORDER_KEY"):
        return "env"
    return "saved" if await _key() else "missing"


async def save_key(key: str) -> None:
    async with db.pool.connection() as conn:
        await conn.execute("INSERT INTO settings (key, value) VALUES ('recorder_key', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                           (json.dumps(key.strip()),))


class RecorderError(Exception):
    pass


async def call(method: str, path: str, params: dict | None = None, body=None, timeout: float = 30):
    key = await _key()
    if not key:
        raise RecorderError("The Hub doesn't have the recorder key yet")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.request(method, URL + path, params=params, json=body, headers={"X-Recorder-Key": key})
    if r.status_code == 401:
        raise RecorderError("The recorder rejected the key")
    if r.status_code >= 400:
        raise RecorderError(f"Recorder error {r.status_code}: {r.text[:200]}")
    return r.json()


async def overview() -> dict:
    s = await call("GET", "/status")
    lv = await call("GET", "/live")
    return {"recorder_started": s.get("started"), "shadow": s.get("shadow"), "live": lv, "inputs": {
        k: s.get(k) for k in ("offers", "foep", "inventory", "keepa", "listings")}, "disk": s.get("disk"), "retrain": s.get("retrain"), "abtest": s.get("abtest")}


async def components() -> dict | None:
    """The recorder's own list of streams and repricer parts (GET /components), for the system map. None if unreachable."""
    try:
        return await call("GET", "/components", timeout=8)
    except Exception:
        return None


async def set_config(upd: dict) -> dict:
    body = {k: upd[k] for k in SETTABLE if k in upd}
    return await call("POST", "/live", body=body)


async def audit(user, action: str, before, after) -> None:
    async with db.pool.connection() as conn:
        await conn.execute("INSERT INTO audit_log (user_id, source, action, entity, entity_id, order_id, before, after) VALUES (%s,'web',%s,'repricer',NULL,NULL,%s,%s)",
                           (user.id, action, json.dumps(before) if before is not None else None, json.dumps(after) if after is not None else None))


# ---------------------------------------------------------------- price limits and costs for the repricer (Nick, Nov 12 2026)
# Everything the repricer needs that used to come from Seller Snap, from the Hub's own data. Sent to the recorder hourly
# (POST /limits with the recorder key) and on every change, so the recorder never needs Seller Snap or a second key.
log = logging.getLogger("repricer")
LIMIT_DEFAULTS = {"min_roi": 0.12, "prep": 1.40, "max_floor_mult": 1.25, "max_new_mult": 2.0, "max_history_days": 90}
push_status = {"last": None, "ok": None, "error": None, "costs": 0, "overrides": 0}


async def limit_settings() -> dict:
    async with db.pool.connection() as conn:
        row = await (await conn.execute("SELECT value FROM settings WHERE key = 'repricer_limits'")).fetchone()
    v = (row or {}).get("value") or {}
    if isinstance(v, str):
        v = json.loads(v)
    return {**LIMIT_DEFAULTS, **{k: v[k] for k in LIMIT_DEFAULTS if k in v}}


async def save_limit_settings(upd: dict) -> dict:
    cur = await limit_settings()
    for k in LIMIT_DEFAULTS:
        if k in upd and upd[k] is not None:
            cur[k] = float(upd[k])
    cur["min_roi"] = min(max(cur["min_roi"], 0.0), 2.0)
    cur["prep"] = min(max(cur["prep"], 0.0), 20.0)
    cur["max_floor_mult"] = min(max(cur["max_floor_mult"], 1.0), 5.0)
    cur["max_new_mult"] = min(max(cur["max_new_mult"], 1.0), 10.0)
    cur["max_history_days"] = min(max(cur["max_history_days"], 0.0), 365.0)
    async with db.pool.connection() as conn:
        await conn.execute("INSERT INTO settings (key, value) VALUES ('repricer_limits', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                           (json.dumps(cur),))
    return cur


async def unit_costs() -> dict:
    """Unit cost per SKU and per ASIN, in the order the Hub uses everywhere: InventoryLab snapshot -> InventoryLab month ->
    order-ledger average (last 12 months, by ASIN) -> latest lead cost (by ASIN). $0.01 placeholders are skipped."""
    async with db.pool.connection() as conn:
        il = await (await conn.execute("""
            SELECT DISTINCT ON (c.sku) c.sku, coalesce(c.asin, m.asin) asin, c.cost, c.source
            FROM sku_costs c LEFT JOIN sku_asin m ON m.sku = c.sku
            WHERE c.source IN ('inventorylab', 'il_month') AND c.cost > 0.01
            ORDER BY c.sku, (c.source = 'inventorylab') DESC, c.as_of DESC""")).fetchall()
        ledger = await (await conn.execute("""
            SELECT upper(trim(i.asin)) asin, sum(i.qty_ordered * i.cost_per_unit) / nullif(sum(i.qty_ordered), 0) cost
            FROM order_items i JOIN orders o ON o.id = i.order_id
            WHERE i.asin IS NOT NULL AND i.cost_per_unit > 0.01 AND i.qty_ordered > 0 AND o.order_date >= now() - interval '365 days'
            GROUP BY 1""")).fetchall()
        leads = await (await conn.execute("""
            SELECT DISTINCT ON (upper(trim(asin))) upper(trim(asin)) asin, cost FROM leads
            WHERE asin IS NOT NULL AND cost > 0.01 ORDER BY upper(trim(asin)), lead_date DESC NULLS LAST""")).fetchall()
    return {"sku": {r["sku"]: [round(float(r["cost"]), 4), r["source"]] for r in il},
            "asin_ledger": {r["asin"]: round(float(r["cost"]), 4) for r in ledger if r["cost"]},
            "asin_lead": {r["asin"]: round(float(r["cost"]), 4) for r in leads}}


async def overrides() -> dict:
    async with db.pool.connection() as conn:
        rows = await (await conn.execute("SELECT sku, min_price, max_price, source, note, approved_by, updated_at FROM price_limits")).fetchall()
    return {r["sku"]: {"min": float(r["min_price"]) if r["min_price"] is not None else None, "max": float(r["max_price"]) if r["max_price"] is not None else None,
                       "source": r["source"], "note": r["note"], "approved_by": r["approved_by"], "updated_at": r["updated_at"].isoformat()} for r in rows}


async def set_overrides(rows: list, source: str, who: str | None) -> int:
    """Upsert min/max overrides. A row with neither min nor max removes the override (the normal rule applies again)."""
    n = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for r in rows:
                sku = str(r.get("sku") or "").strip()
                if not sku:
                    continue
                mn, mx = r.get("min"), r.get("max")
                if mn is None and mx is None:
                    await conn.execute("DELETE FROM price_limits WHERE sku = %s", (sku,))
                else:
                    if mn is not None and mx is not None and float(mn) > float(mx):
                        raise ValueError(f"{sku}: min above max")
                    await conn.execute("""INSERT INTO price_limits (sku, min_price, max_price, source, note, approved_by, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (sku) DO UPDATE SET min_price = EXCLUDED.min_price,
                        max_price = EXCLUDED.max_price, source = EXCLUDED.source, note = EXCLUDED.note, approved_by = EXCLUDED.approved_by, updated_at = now()""",
                                       (sku, mn, mx, source, r.get("note"), who))
                n += 1
    return n


async def push_limits() -> dict:
    """Send costs, overrides and the limit settings to the recorder."""
    body = {"sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "settings": await limit_settings(),
            "costs": await unit_costs(), "overrides": await overrides()}
    try:
        r = await call("POST", "/limits", body=body, timeout=60)
        push_status.update(last=body["sent_at"], ok=True, error=None, costs=len(body["costs"]["sku"]), overrides=len(body["overrides"]))
        return r
    except Exception as e:
        push_status.update(last=body["sent_at"], ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")
        raise


async def limits_loop() -> None:
    """Repricer price limits: sends the Hub's unit costs, min/max overrides and limit settings to the offer recorder every hour."""
    await asyncio.sleep(90)
    while True:
        try:
            if await _key():
                await push_limits()
        except Exception as e:
            log.warning("repricer limits push failed: %s", e)
        await asyncio.sleep(3600)
