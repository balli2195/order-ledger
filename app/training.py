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


META_KEYS = ("asin", "title", "brand", "color", "size", "model", "partNumber", "features", "description", "images",
             "variations", "parentAsin", "itemTypeKeyword", "scent", "style", "upcList", "eanList")


async def keepa_meta(asins: list) -> dict:
    """Listing details from the collector's latest pull (no Keepa tokens): title, color, size, model / partNumber,
    bullets, description, photos and the variation selector, for up to 300 ASINs (Nov 19, 2026)."""
    asins = [a.strip().upper() for a in asins if a and a.strip()][:100]   # 100 max: memory (Nov 24, 2026)
    if not asins:
        raise LedgerError("asins is required")
    out = {}
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT asin, pulled_at, raw_gz FROM keepa_raw WHERE asin = ANY(%s) AND bytes > 0", (asins,))
        for r in await cur.fetchall():
            try:
                p = json.loads(gzip.decompress(bytes(r["raw_gz"])))
            except Exception:  # noqa: BLE001
                continue
            p = p.get("products", [p])[0] if isinstance(p, dict) and "products" in p else p
            d = {k: p.get(k) for k in META_KEYS}
            d["features"] = (d.get("features") or [])[:5]
            d["description"] = (d.get("description") or "")[:1500]
            d["pulled_at"] = r["pulled_at"].isoformat()
            out[r["asin"]] = d
    return {"products": out, "missing": [a for a in asins if a not in out]}


SLIM_KEYS = ("asin", "title", "brand", "parentAsin", "parentTitle", "color", "size", "style", "variations", "itemWeight",
             "packageQuantity", "numberOfItems", "unitCount", "itemForm", "productType", "monthlySold", "stats",
             "availabilityAmazon", "referralFeePercentage", "referralFeePercent", "fbaFees", "eanList", "upcList", "listedSince")


async def keepa_slim(asins: list, max_age_hours: float = 36) -> dict:
    """The collector's latest Keepa /product per ASIN (stats=90, Buy Box), without price history or offers, for up to
    300 ASINs pulled within max_age_hours. The daily Sephora job prices from these instead of spending its own tokens
    (Nick, Nov 23, 2026)."""
    # at most 100 per call: unpacking 300 full Keepa products at once ran the Hub out of memory (Nov 24, 2026)
    asins = list(dict.fromkeys(a.strip().upper() for a in asins if a and a.strip()))[:100]
    if not asins:
        raise LedgerError("asins is required")
    out = {}
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT asin, pulled_at, raw_gz FROM keepa_raw WHERE asin = ANY(%s) AND bytes > 0 "
                                 "AND pulled_at >= now() - make_interval(secs => %s)", (asins, float(max_age_hours) * 3600))
        for r in await cur.fetchall():
            try:
                p = json.loads(gzip.decompress(bytes(r["raw_gz"])))
            except Exception:  # noqa: BLE001
                continue
            p = p.get("products", [p])[0] if isinstance(p, dict) and "products" in p else p
            d = {k: p.get(k) for k in SLIM_KEYS if k in p}
            d["pulled_at"] = r["pulled_at"].isoformat()
            out[r["asin"]] = d
            del p
    return {"products": out, "missing": [a for a in asins if a not in out]}


async def keepa_raw_one(asin: str, max_age_hours: float = 24) -> dict:
    """The Keepa recorder's latest full pull of one ASIN (the same request Lead Score makes: history, Buy Box, 20 offers
    with stock, rating), if it is at most max_age_hours old. One product at a time keeps the Hub's memory small.
    Lead Score uses it instead of its own Keepa pull (Nick, Nov 24, 2026: faster scoring, fewer tokens)."""
    asin = (asin or "").strip().upper()
    if len(asin) != 10:
        return {"found": False, "why": "bad asin"}
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT pulled_at, raw_gz FROM keepa_raw WHERE asin = %s AND bytes > 0 "
                                 "AND pulled_at >= now() - make_interval(secs => %s)", (asin, float(max_age_hours) * 3600))
        r = await cur.fetchone()
    if not r:
        return {"found": False}
    return {"found": True, "pulled_at": r["pulled_at"].isoformat(), "product": json.loads(gzip.decompress(bytes(r["raw_gz"])))}


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


async def guide_stats() -> dict:
    """Live counts for the Lead DB guide page (/lead-db/guide)."""
    async with db.pool.connection() as conn:
        r = await (await conn.execute(
            """SELECT (SELECT count(*) FROM sephora_catalog) pairs,
                      (SELECT count(*) FROM sephora_catalog WHERE active) active_pairs,
                      (SELECT count(DISTINCT asin) FROM sephora_catalog WHERE active) asins,
                      (SELECT count(DISTINCT lower(brand)) FROM sephora_catalog WHERE active) brands,
                      (SELECT count(DISTINCT store) FROM sephora_catalog) stores,
                      (SELECT count(*) FROM sephora_catalog WHERE active AND sellable = 'open' AND match_ok IS NOT FALSE) watched,
                      (SELECT count(*) FROM sephora_parked_pairs) held,
                      (SELECT count(DISTINCT asin) FROM sephora_parked_pairs) held_asins,
                      (SELECT count(*) FROM sephora_parked_pairs WHERE reason LIKE 'Amazon%%') held_amazon,
                      (SELECT count(*) FROM sephora_parked_pairs WHERE reason LIKE 'Nick:%%') held_yours,
                      (SELECT count(*) FROM sephora_parked_pairs WHERE reason LIKE 'No sales rank%%') held_norank,
                      (SELECT count(*) FROM sephora_catalog WHERE match_ok IS TRUE) marked_ok,
                      (SELECT count(*) FROM match_feedback) wrong_marks,
                      (SELECT count(*) FROM sephora_candidates) candidates,
                      (SELECT count(*) FROM sephora_candidates WHERE status = 'approved') approved,
                      (SELECT count(*) FROM sephora_candidates WHERE status = 'rejected') rejected,
                      (SELECT count(*) FROM catalog_history) history_rows,
                      (SELECT count(*) FROM keepa_raw WHERE bytes > 0) raw_asins,
                      (SELECT COALESCE(sum(octet_length(raw_gz)), 0) FROM keepa_raw) raw_gz_bytes,
                      (SELECT count(*) FROM keepa_snapshots) snapshots,
                      (SELECT count(DISTINCT asin) FROM keepa_snapshots) snapshot_asins,
                      (SELECT min(pulled_at) FROM keepa_snapshots) snapshots_since,
                      (SELECT count(*) FROM leads) leads,
                      (SELECT count(*) FROM leads WHERE COALESCE(lead_date, created_at::date) < current_date - 80) leads_gradable""")).fetchone()
        st = await (await conn.execute("SELECT beat_at, state, detail FROM keepa_collector_status WHERE id = 1")).fetchone()
        db_bytes = (await (await conn.execute("SELECT pg_database_size(current_database()) b")).fetchone())["b"]
    return jsonable({**dict(r), "db_bytes": db_bytes, "collector": dict(st) if st else None})


# Sample rows for the Lead DB guide's data-flow diagram (Nick, Nov 25, 2026: "when i click on a table i would like for the
# table to be displayed under the diagram with a short description... it can be a sample table"). One entry per diagram
# node: a few readable columns, newest rows first. Column kinds tell the page how to format: asin, money, pct, date, ts,
# int, bool, text.
_SAMPLE_N = 10
_CAT_NAME = ("LEFT JOIN LATERAL (SELECT COALESCE((SELECT sephora_name FROM sephora_catalog c WHERE c.asin = t.asin "
             "AND c.product_id = t.product_id LIMIT 1), (SELECT pair->>'sephora_name' FROM sephora_parked_pairs p "
             "WHERE p.asin = t.asin AND p.product_id = t.product_id LIMIT 1)) AS sephora_name) c ON true")   # held pairs too
TABLE_SAMPLES = {
    "catalog": [("sephora_catalog", "Pairs, most recently updated first",
        "SELECT asin, brand, sephora_name, left(amazon_title, 70), COALESCE(sale_price, price_lo), buybox, roi_now, confidence, "
        "sellable, CASE WHEN match_ok THEN 'OK' WHEN match_ok IS FALSE THEN 'wrong' ELSE '' END, updated_at "
        "FROM sephora_catalog WHERE active ORDER BY updated_at DESC LIMIT %s",
        "SELECT count(*) FROM sephora_catalog WHERE active",
        [("ASIN", "asin"), ("Brand", "text"), ("Sephora item", "text"), ("Amazon title", "text"), ("Sephora price", "money"),
         ("Buy Box", "money"), ("ROI now", "pct"), ("Confidence", "text"), ("Can sell", "text"), ("Your mark", "text"),
         ("Updated", "ts")])],
    "history": [("catalog_history", "Latest changes to pairs",
        "SELECT t.at, t.asin, c.sephora_name, t.price_lo, t.sale_price, t.on_sale, t.buybox, t.offers_fba, t.roi_now "
        "FROM (SELECT * FROM catalog_history ORDER BY at DESC LIMIT %s) t " + _CAT_NAME + " ORDER BY t.at DESC",
        "SELECT count(*) FROM catalog_history",
        [("When", "ts"), ("ASIN", "asin"), ("Sephora item", "text"), ("List price", "money"), ("Sale price", "money"),
         ("On sale", "bool"), ("Buy Box", "money"), ("FBA offers", "int"), ("ROI now", "pct")])],
    "held": [("sephora_parked_pairs", "Held pairs, most recently held first",
        "SELECT asin, pair->>'brand', pair->>'sephora_name', reason, parked_at, checked_at FROM sephora_parked_pairs "
        "ORDER BY parked_at DESC, asin LIMIT %s",
        "SELECT count(*) FROM sephora_parked_pairs",
        [("ASIN", "asin"), ("Brand", "text"), ("Sephora item", "text"), ("Why held", "text"), ("Held on", "date"),
         ("Last checked", "date")])],
    "items": [("store_skus", "Sephora item lists, most recently read first",
        "SELECT s.product_id, c.brand, c.sephora_name, s.variation_type, jsonb_array_length(s.skus), "
        "(SELECT count(*) FROM jsonb_array_elements(s.skus) e WHERE NOT COALESCE((e->>5) IN ('true', '1'), false)), s.read_at "
        "FROM (SELECT * FROM store_skus ORDER BY read_at DESC LIMIT %s) s LEFT JOIN LATERAL (SELECT brand, sephora_name "
        "FROM sephora_catalog c WHERE c.product_id = s.product_id LIMIT 1) c ON true ORDER BY s.read_at DESC",
        "SELECT count(*) FROM store_skus",
        [("Sephora product", "text"), ("Brand", "text"), ("Name", "text"), ("Sold by", "text"), ("Items", "int"),
         ("In stock", "int"), ("Read", "ts")]),
              ("sephora_stock_watch", "Stock watch: listings you rejected as out of stock",
        "SELECT t.asin, c.sephora_name, t.status, t.checks, t.last_in_stock, t.last_checked_at, t.added_at "
        "FROM (SELECT * FROM sephora_stock_watch ORDER BY added_at DESC LIMIT %s) t " + _CAT_NAME + " ORDER BY t.added_at DESC",
        "SELECT count(*) FROM sephora_stock_watch",
        [("ASIN", "asin"), ("Sephora item", "text"), ("Status", "text"), ("Checks", "int"), ("In stock at last check", "bool"),
         ("Last checked", "ts"), ("Added", "date")])],
    "cands": [("sephora_candidates", "Suggestions, newest first",
        "SELECT t.found_on, t.asin, c.sephora_name, t.cost, t.buybox, t.roi_now, t.lead_score_band, t.lead_score_pred_roi, "
        "t.status, COALESCE(t.reject_reason, t.expired_reason), t.decided_by "
        "FROM (SELECT * FROM sephora_candidates ORDER BY found_on DESC, updated_at DESC LIMIT %s) t " + _CAT_NAME +
        " ORDER BY t.found_on DESC",
        "SELECT count(*) FROM sephora_candidates",
        [("Found", "date"), ("ASIN", "asin"), ("Sephora item", "text"), ("Cost", "money"), ("Buy Box", "money"),
         ("ROI", "pct"), ("Band", "int"), ("Pred ROI", "pct"), ("Status", "text"), ("Why", "text"), ("Decided by", "text")])],
    "feedback": [("match_feedback", "Wrong-match marks, newest first",
        "SELECT created_at, asin, brand, sephora_name, array_to_string(tags, ', '), note, correct_asin, marked_by "
        "FROM match_feedback ORDER BY created_at DESC LIMIT %s",
        "SELECT count(*) FROM match_feedback",
        [("Marked", "date"), ("ASIN", "asin"), ("Brand", "text"), ("Sephora item", "text"), ("Why", "text"), ("Note", "text"),
         ("Correct ASIN", "asin"), ("By", "text")])],
    "leads": [("leads", "Leads, newest first",
        "SELECT lead_date, asin, left(product_name, 60), sourced_by, cost, buy_qty, qty_ordered, lead_score_band "
        "FROM leads ORDER BY lead_date DESC NULLS LAST, created_at DESC LIMIT %s",
        "SELECT count(*) FROM leads",
        [("Date", "date"), ("ASIN", "asin"), ("Product", "text"), ("Sourced by", "text"), ("Cost", "money"),
         ("Buy qty", "int"), ("Ordered", "int"), ("Band", "int")])],
    "raw": [("keepa_raw", "Latest Keepa recorder pulls (the full product is stored compressed)",
        "SELECT t.pulled_at, t.asin, c.sephora_name, t.tokens, round(t.bytes / 1024.0) "
        "FROM (SELECT asin, pulled_at, tokens, bytes FROM keepa_raw ORDER BY pulled_at DESC LIMIT %s) t "
        "LEFT JOIN LATERAL (SELECT sephora_name FROM sephora_catalog c WHERE c.asin = t.asin LIMIT 1) c ON true "
        "ORDER BY t.pulled_at DESC",
        "SELECT count(*) FROM keepa_raw",
        [("Pulled", "ts"), ("ASIN", "asin"), ("Sephora item", "text"), ("Tokens", "int"), ("Size (KB)", "int")])],
    "snaps": [("keepa_snapshots", "Latest offer/stock snapshots (each seller's stock is stored compressed)",
        "SELECT pulled_at, asin, buybox, buybox_seller, buybox_fba, fba_low, amazon, offers_fba, monthly_sold, sales_rank "
        "FROM keepa_snapshots ORDER BY pulled_at DESC LIMIT %s",
        "SELECT count(*) FROM keepa_snapshots",
        [("Pulled", "ts"), ("ASIN", "asin"), ("Buy Box", "money"), ("Buy Box seller", "text"), ("FBA", "bool"),
         ("Lowest FBA", "money"), ("Amazon", "money"), ("FBA offers", "int"), ("Monthly sold", "int"), ("Rank", "int")])],
    "runs": [("sephora_runs", "Latest job runs",
        "SELECT at, kind, (counts->>'watched')::int, (counts->>'keepa_from_collector')::int, (counts->>'posted')::int, "
        "(counts->>'held_rank')::int, (counts->>'held_out_of_stock')::int, (counts->>'needs_size_check')::int, "
        "(counts->>'minutes')::numeric, sale->>'text' FROM sephora_runs ORDER BY at DESC LIMIT %s",
        "SELECT count(*) FROM sephora_runs",
        [("When", "ts"), ("Job", "text"), ("Watched", "int"), ("Priced by Keepa recorder", "int"), ("Posted", "int"),
         ("Not posted: rank", "int"), ("Not posted: out of stock", "int"), ("Not posted: size", "int"), ("Minutes", "int"),
         ("Sale", "text")])],
}


async def table_sample(node: str) -> dict:
    """A few newest rows of the table(s) behind one diagram node, for the guide page."""
    specs = TABLE_SAMPLES.get(node)
    if not specs:
        raise LedgerError("No table for that box")
    out = []
    from psycopg.rows import tuple_row   # positional rows: unnamed expressions would collide in a dict row
    async with db.pool.connection() as conn:
        for name, caption, sql, count_sql, cols in specs:
            async with conn.cursor(row_factory=tuple_row) as cur:
                await cur.execute(sql, (_SAMPLE_N,))
                rows = await cur.fetchall()
                await cur.execute(count_sql)
                total = (await cur.fetchone())[0]
            out.append({"table": name, "caption": caption, "total": total,
                        "columns": [{"label": l, "kind": k} for l, k in cols],
                        "rows": [jsonable(list(r)) for r in rows]})
    return {"node": node, "tables": out}
