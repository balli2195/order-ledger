"""Training export (Nov 11, 2026): pages of Lead DB and Keepa data for Lead Score training, read through the engine API.

GET /api/engine/training/<kind>?after=<cursor>&since=<iso time>&limit=<n> -> {"rows": [...], "next": cursor | null}
kinds:
  catalog     Lead DB pairs (store, ASIN, product, prices, match answers)          cursor: "asin|product_id"
  history     catalog_history: every change to a pair's store price / Amazon numbers   cursor: id
  candidates  daily buy candidates and Nick's decisions                            cursor: offset
  feedback    match_feedback: wrong-match marks and reasons                        cursor: offset
  runs        sephora_runs: daily/weekly runs and the sale seen                    cursor: offset
  snapshots   keepa_snapshots: dated offers/stock per ASIN (offers as JSON text)   cursor: "pulled_at|asin"
  raw         keepa_raw: latest full Keepa product per ASIN (gzipped JSON, base64) cursor: asin; since= filters pulled_at
tools/export_training.py turns these into Parquet files (and Keepa chunk files) for local or cloud training.
"""
from __future__ import annotations

import base64
import gzip
import json
from datetime import datetime

from . import db
from .orders import LedgerError, jsonable

KINDS = ("catalog", "history", "candidates", "feedback", "runs", "snapshots", "raw")
MAX = {"raw": 200, "snapshots": 5000}


def _since(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise LedgerError("since must be an ISO date/time")


async def export(kind: str, after: str | None, since: str | None, limit: int) -> dict:
    if kind not in KINDS:
        raise LedgerError(f"kind must be one of {', '.join(KINDS)}")
    limit = max(1, min(int(limit or 1000), MAX.get(kind, 10000)))
    s = _since(since)
    async with db.pool.connection() as conn:
        if kind == "catalog":
            a, p = (after.split("|", 1) + [""])[:2] if after else ("", "")
            cur = await conn.execute(
                "SELECT * FROM sephora_catalog WHERE (asin, product_id) > (%s, %s) AND (%s::timestamptz IS NULL OR updated_at >= %s) "
                "ORDER BY asin, product_id LIMIT %s", (a, p, s, s, limit))
            rows = [jsonable(dict(r)) for r in await cur.fetchall()]
            nxt = f"{rows[-1]['asin']}|{rows[-1]['product_id']}" if len(rows) == limit else None
        elif kind == "history":
            cur = await conn.execute("SELECT * FROM catalog_history WHERE id > %s AND (%s::timestamptz IS NULL OR at >= %s) "
                                     "ORDER BY id LIMIT %s", (int(after or 0), s, s, limit))
            rows = [jsonable(dict(r)) for r in await cur.fetchall()]
            nxt = str(rows[-1]["id"]) if len(rows) == limit else None
        elif kind in ("candidates", "feedback", "runs"):
            table, ts = {"candidates": ("sephora_candidates", "updated_at"), "feedback": ("match_feedback", "created_at"),
                         "runs": ("sephora_runs", "at")}[kind]
            off = int(after or 0)
            cur = await conn.execute(f"SELECT * FROM {table} WHERE (%s::timestamptz IS NULL OR {ts} >= %s) ORDER BY {ts}, 1 "
                                     f"LIMIT %s OFFSET %s", (s, s, limit, off))
            rows = [jsonable(dict(r)) for r in await cur.fetchall()]
            nxt = str(off + limit) if len(rows) == limit else None
        elif kind == "snapshots":
            t, a = (after.split("|", 1) + [""])[:2] if after else ("1970-01-01T00:00:00+00:00", "")
            cur = await conn.execute(
                "SELECT * FROM keepa_snapshots WHERE (pulled_at, asin) > (%s::timestamptz, %s) AND (%s::timestamptz IS NULL OR pulled_at >= %s) "
                "ORDER BY pulled_at, asin LIMIT %s", (t, a, s, s, limit))
            rows = []
            for r in await cur.fetchall():
                d = dict(r)
                gz = d.pop("offers_gz", None)
                d["offers"] = gzip.decompress(gz).decode() if gz else None
                rows.append(jsonable(d))
            nxt = f"{rows[-1]['pulled_at']}|{rows[-1]['asin']}" if len(rows) == limit else None
        else:   # raw
            cur = await conn.execute(
                "SELECT asin, pulled_at, tokens, bytes, raw_gz FROM keepa_raw WHERE asin > %s AND bytes > 0 "
                "AND (%s::timestamptz IS NULL OR pulled_at >= %s) ORDER BY asin LIMIT %s", (after or "", s, s, limit))
            rows = []
            for r in await cur.fetchall():
                d = dict(r)
                d["raw_gz_b64"] = base64.b64encode(bytes(d.pop("raw_gz"))).decode()
                rows.append(jsonable(d))
            nxt = rows[-1]["asin"] if len(rows) == limit else None
    return {"kind": kind, "rows": rows, "next": nxt}


async def summary() -> dict:
    """Counts and sizes, for the export script and the System map."""
    async with db.pool.connection() as conn:
        r = await (await conn.execute(
            "SELECT (SELECT count(*) FROM keepa_raw WHERE bytes > 0) raw_asins, (SELECT count(*) FROM keepa_raw WHERE bytes = 0) raw_missing, "
            "(SELECT COALESCE(sum(octet_length(raw_gz)), 0) FROM keepa_raw) raw_gz_bytes, (SELECT min(pulled_at) FROM keepa_raw) raw_oldest, "
            "(SELECT count(*) FROM keepa_snapshots) snapshots, (SELECT count(*) FROM catalog_history) history_rows, "
            "(SELECT count(*) FROM sephora_catalog) catalog_pairs")).fetchone()
        st = await (await conn.execute("SELECT beat_at, started_at, state, detail FROM keepa_collector_status WHERE id = 1")).fetchone()
    return jsonable({**dict(r), "collector": dict(st) if st else None})
