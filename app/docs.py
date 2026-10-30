"""Documents written by the scheduled analysis runs, read by the analysis tabs (Inventory health, ...).

Paths are the same as the old claude.ai Inventory Hub database, e.g. "liquidation/latest", "doi/latest",
"liquidation_history/2026-10-29". Each path prefix belongs to one tab, which decides who may read it.
"""
from __future__ import annotations

import json
import re

from . import db

PREFIX_TAB = {
    "liquidation": "inventory_health", "liquidation_trend": "inventory_health", "liquidation_history": "inventory_health",
    "health_trend": "inventory_health", "doi": "inventory_health",
}
PATH_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,100}(/[A-Za-z0-9_\-.]{1,100}){1,4}$")


class DocError(Exception):
    pass


def tab_for(path: str) -> str | None:
    return PREFIX_TAB.get(path.split("/", 1)[0])


def check(path: str) -> str:
    if not PATH_RE.match(path or ""):
        raise DocError("Paths look like collection/doc, e.g. liquidation/latest")
    return path


async def get(path: str) -> dict | None:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT data, version, updated_at FROM hub_docs WHERE path=%s", (check(path),))
        r = await cur.fetchone()
    if not r:
        return None
    return {"path": path, "data": r["data"], "version": r["version"], "updated_at": r["updated_at"].isoformat()}


async def list_prefix(collection: str) -> list[dict]:
    """Documents directly inside a collection (collection/doc)."""
    if not re.match(r"^[A-Za-z0-9_\-.]{1,100}$", collection or ""):
        raise DocError("Bad collection name")
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT path, data, version, updated_at FROM hub_docs WHERE path LIKE %s AND path NOT LIKE %s ORDER BY path",
            (collection + "/%", collection + "/%/%"))
        rows = await cur.fetchall()
    return [{"path": r["path"], "id": r["path"].split("/", 1)[1], "data": r["data"], "version": r["version"],
             "updated_at": r["updated_at"].isoformat()} for r in rows]


async def put(path: str, data: dict, if_version: int | None = None) -> dict:
    if not isinstance(data, dict):
        raise DocError("The document must be a JSON object")
    check(path)
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT version FROM hub_docs WHERE path=%s FOR UPDATE", (path,))
            r = await cur.fetchone()
            if if_version is not None and (r["version"] if r else 0) != if_version:
                raise DocError(f"{path} is at version {r['version'] if r else 0}, not {if_version}; read it again")
            cur = await conn.execute(
                "INSERT INTO hub_docs (path, data) VALUES (%s, %s) ON CONFLICT (path) DO UPDATE "
                "SET data=EXCLUDED.data, version=hub_docs.version+1, updated_at=now() RETURNING version",
                (path, json.dumps(data)))
            v = (await cur.fetchone())["version"]
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "doc", "op": "upsert", "path": path}),))
    return {"path": path, "version": v}


async def delete(path: str) -> bool:
    async with db.pool.connection() as conn:
        cur = await conn.execute("DELETE FROM hub_docs WHERE path=%s RETURNING path", (check(path),))
        gone = await cur.fetchone() is not None
        await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "doc", "op": "delete", "path": path}),))
        await conn.commit()
    return gone
