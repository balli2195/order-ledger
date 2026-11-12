"""Repricer page: a control panel for Nick's own repricer, which runs inside the offer recorder service
(https://offer-recorder.onrender.com, repo balli2195/offer-recorder), not on the Hub.

The recorder is the only place repricer settings live; this module just relays reads and owner changes to it
server-to-server (the recorder key never reaches the browser) and writes every change to the Hub activity log.
Recorder key: Render env RECORDER_KEY, or saved once from the page by an owner (settings key 'recorder_key').
"""
import json
import os

import httpx

from . import db

URL = os.environ.get("RECORDER_URL", "https://offer-recorder.onrender.com").rstrip("/")
SETTABLE = ("enabled", "skus", "max_step_pct", "min_interval_s", "max_per_day", "decay_per_day", "min_gain_abs", "min_gain_rel")


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
        k: s.get(k) for k in ("offers", "foep", "inventory", "keepa", "listings")}, "disk": s.get("disk")}


async def set_config(upd: dict) -> dict:
    body = {k: upd[k] for k in SETTABLE if k in upd}
    return await call("POST", "/live", body=body)


async def audit(user, action: str, before, after) -> None:
    async with db.pool.connection() as conn:
        await conn.execute("INSERT INTO audit_log (user_id, source, action, entity, entity_id, order_id, before, after) VALUES (%s,'web',%s,'repricer',NULL,NULL,%s,%s)",
                           (user.id, action, json.dumps(before) if before is not None else None, json.dumps(after) if after is not None else None))
