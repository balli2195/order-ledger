"""Sephora leads tab (Nov 2026): the Amazon ASIN <-> Sephora product catalog, the daily review queue of buy candidates,
and the scheduled job's run log.

A scheduled job (engine API, X-Engine-Key) keeps the catalog current and posts the day's candidates: Sephora products whose
price, sale or launch makes the matched ASIN worth buying. Nick reviews them on /sephora-leads: Approve creates a lead through
the same code path as the Leads page (Keepa title, FBA checks, Lead Score, audit, live update); Reject; Wrong match marks the
catalog pair match_ok=false so it never comes back. match_ok and size_confirmed are Nick's answers and the job never
overwrites them.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import db
from . import leads as L
from .orders import Actor, LedgerError, _audit, jsonable

CENTRAL = ZoneInfo("America/Chicago")
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# Catalog columns the job may write, by kind. match_ok / size_confirmed / marked_* are Nick's and never written here.
CAT_TEXT = ("store", "sku_id", "brand", "sephora_name", "sephora_url", "shade", "amazon_title", "match_reason")   # store: Lead DB, Nov 11 2026
CAT_MONEY = ("price_lo", "price_hi", "sale_price", "buybox", "fba_low", "bb_avg90", "fba_fee", "watch_price")
CAT_NUM = ("referral_pct", "roi_now", "roi_avg90")
CAT_INT = ("monthly_sold", "offers_fba")
CAT_BOOL = ("on_sale", "needs_size", "amazon_on", "active")
CAT_ENUM = {"size_guess": ("lo", "hi", "unknown"), "confidence": ("high", "medium", "low"),
            "sellable": ("open", "approval_required", "restricted")}
CAT_FIELDS = set(CAT_TEXT) | set(CAT_MONEY) | set(CAT_NUM) | set(CAT_INT) | set(CAT_BOOL) | set(CAT_ENUM) | {"extra"}
NICK_FIELDS = {"match_ok", "size_confirmed", "marked_by", "marked_at", "wrong_tags", "wrong_note", "correct_asin",
               "store_sku_id", "store_size", "store_size_price", "store_size_by", "store_size_at"}

# What can be wrong with a match (the Wrong match dialog's quick picks; static/wrongmatch.js has the labels).
# The jobs turn repeated tags into match rules (jobs/sephora/match_rules.py).
WRONG_TAGS = ("size", "shade", "concentration", "set", "bonus", "multipack", "version", "product", "other")
WRONG_LABELS = {"size": "wrong size", "shade": "wrong shade", "concentration": "wrong scent/type", "set": "set vs single",
                "bonus": "bundle/bonus item", "multipack": "pack count differs", "version": "old version", "product": "different product",
                "other": "other"}

CAND_FIELDS = {"asin", "product_id", "found_on", "reason", "cost", "price_basis", "buybox", "roi_now", "lead_score_band",
               "lead_score_pred_roi", "lead_score_notes", "note"}
REASONS = ("price_watch", "sale", "new_product")
STATUSES = ("new", "approved", "rejected", "expired")

ENGINE = Actor(None, "Claude engine", "engine")
_approve_lock = asyncio.Lock()   # one approval at a time, so a double click can't make two leads


def today() -> date:
    return datetime.now(CENTRAL).date()


# ---------------------------------------------------------------- cleaning

def _asin(v: Any) -> str:
    s = str(v or "").strip().upper()
    if not ASIN_RE.match(s):
        raise LedgerError(f"ASIN should be 10 letters/digits (got {v!r})")
    return s


def _pid(v: Any) -> str:
    s = str(v or "").strip()
    if not s or len(s) > 64:
        raise LedgerError(f"product_id is required (got {v!r})")
    return s


def _dec(v: Any, field: str, places: int | None = None) -> Decimal | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        d = Decimal(str(v).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        raise LedgerError(f"{field} must be a number (got {v!r})")
    if not d.is_finite():
        raise LedgerError(f"{field} must be a number (got {v!r})")
    return d.quantize(Decimal(1).scaleb(-places)) if places is not None else d


def _int(v: Any, field: str) -> int | None:
    d = _dec(v, field)
    return None if d is None else int(round(d))


def _bool(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


def _txt(v: Any, n: int = 2000) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] or None


def _day(v: Any, field: str) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        raise LedgerError(f"{field} should look like 2026-11-09 (got {v!r})")


def clean_catalog_row(r: dict) -> dict:
    if not isinstance(r, dict):
        raise LedgerError("Each catalog row must be an object")
    bad = set(r) - CAT_FIELDS - {"asin", "product_id"}
    if bad & NICK_FIELDS:
        raise LedgerError(f"{', '.join(sorted(bad & NICK_FIELDS))} are Nick's answers; the job can't set them")
    if bad:
        raise LedgerError(f"Unknown catalog fields: {', '.join(sorted(bad))} (put anything else in extra)")
    out: dict[str, Any] = {"asin": _asin(r.get("asin")), "product_id": _pid(r.get("product_id"))}
    for k, v in r.items():
        if k in ("asin", "product_id"):
            continue
        if k in CAT_TEXT:
            out[k] = _txt(v)
        elif k in CAT_MONEY:
            out[k] = _dec(v, k, 2)
        elif k in CAT_NUM:
            out[k] = _dec(v, k, 4)
        elif k in CAT_INT:
            out[k] = _int(v, k)
        elif k in CAT_BOOL:
            b = _bool(v)
            if k == "active" and b is None:
                continue   # leave active as it is
            out[k] = b
        elif k in CAT_ENUM:
            s = _txt(v)
            if s is not None:
                s = s.lower()
                if s not in CAT_ENUM[k]:
                    raise LedgerError(f"{k} must be one of {', '.join(CAT_ENUM[k])} (got {v!r})")
            out[k] = s
        elif k == "extra":
            if v is not None and not isinstance(v, dict):
                raise LedgerError("extra must be an object")
            out[k] = json.dumps(v or {})
    return out


def clean_candidate_row(r: dict) -> dict:
    if not isinstance(r, dict):
        raise LedgerError("Each candidate must be an object")
    bad = set(r) - CAND_FIELDS
    if bad:
        raise LedgerError(f"Unknown candidate fields: {', '.join(sorted(bad))}")
    reason = str(r.get("reason") or "").strip().lower()
    if reason not in REASONS:
        raise LedgerError(f"reason must be one of {', '.join(REASONS)} (got {r.get('reason')!r})")
    band = _int(r.get("lead_score_band"), "lead_score_band")
    return {
        "asin": _asin(r.get("asin")), "product_id": _pid(r.get("product_id")),
        "found_on": _day(r["found_on"], "found_on") if r.get("found_on") else today(),
        "reason": reason, "cost": _dec(r.get("cost"), "cost", 2), "price_basis": _txt(r.get("price_basis"), 200),
        "buybox": _dec(r.get("buybox"), "buybox", 2), "roi_now": _dec(r.get("roi_now"), "roi_now", 4),
        "lead_score_band": band, "lead_score_pred_roi": _dec(r.get("lead_score_pred_roi"), "lead_score_pred_roi", 4),
        "lead_score_notes": _txt(r.get("lead_score_notes"), 4000), "note": _txt(r.get("note"), 1000),
    }


async def _notify(conn, op: str = "reload") -> None:
    await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "sephora", "op": op}),))


# ---------------------------------------------------------------- engine: catalog

async def upsert_catalog(rows: list) -> dict:
    """Insert or update catalog pairs. Only the fields a row carries are changed; Nick's match_ok / size_confirmed stay."""
    if not isinstance(rows, list) or not rows:
        raise LedgerError("rows must be a non-empty list")
    if len(rows) > 1000:
        raise LedgerError("At most 1000 rows per call")
    clean = [clean_catalog_row(r) for r in rows]
    seen: dict[tuple, dict] = {}
    for c in clean:   # the same pair twice in one call: the later row wins
        seen[(c["asin"], c["product_id"])] = {**seen.get((c["asin"], c["product_id"]), {}), **c}
    clean = list(seen.values())
    blocked: list = []
    async with db.pool.connection() as conn:   # ASINs Nick never wants in the Lead DB (Nov 24, 2026)
        cur = await conn.execute("SELECT asin FROM sephora_blocked_asins WHERE asin = ANY(%s)", (list({c["asin"] for c in clean}),))
        bset = {r["asin"] for r in await cur.fetchall()}
    if bset:
        blocked = sorted(bset)
        clean = [c for c in clean if c["asin"] not in bset]
        if not clean:
            return {"upserted": 0, "inserted": 0, "updated": 0, "blocked": blocked}
    # Restricted listings stay out of the Lead DB (Nick, Nov 24, 2026): parked pairs are skipped (the monthly restriction
    # check restores them when they open up), a new pair whose listing is restricted is not added, and an existing pair
    # that turns restricted is parked after the update.
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT p.asin, p.product_id FROM sephora_parked_pairs p JOIN unnest(%s::text[], %s::text[]) k(asin, pid) "
            "ON p.asin = k.asin AND p.product_id = k.pid", ([c["asin"] for c in clean], [c["product_id"] for c in clean]))
        parked = {(r["asin"], r["product_id"]) for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT c.asin, c.product_id FROM sephora_catalog c JOIN unnest(%s::text[], %s::text[]) k(asin, pid) "
            "ON c.asin = k.asin AND c.product_id = k.pid", ([c["asin"] for c in clean], [c["product_id"] for c in clean]))
        have = {(r["asin"], r["product_id"]) for r in await cur.fetchall()}
    skipped_restricted = [c for c in clean if (c["asin"], c["product_id"]) in parked
                          or (c.get("sellable") == "restricted" and (c["asin"], c["product_id"]) not in have)]
    to_park = [{"asin": c["asin"], "product_id": c["product_id"]} for c in clean
               if c.get("sellable") == "restricted" and (c["asin"], c["product_id"]) in have]
    if skipped_restricted:
        sk = {(c["asin"], c["product_id"]) for c in skipped_restricted}
        clean = [c for c in clean if (c["asin"], c["product_id"]) not in sk]
        if not clean:
            return {"upserted": 0, "inserted": 0, "updated": 0, "blocked": blocked, "restricted_skipped": len(sk)}
    groups: dict[tuple, list] = {}
    for c in clean:
        groups.setdefault(tuple(sorted(c)), []).append(c)
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT count(*) n FROM sephora_catalog c JOIN unnest(%s::text[], %s::text[]) k(asin, pid) "
                "ON c.asin = k.asin AND c.product_id = k.pid",
                ([c["asin"] for c in clean], [c["product_id"] for c in clean]))
            existing = (await cur.fetchone())["n"]
            for cols, grp in groups.items():
                # New pairs default to active (table default). An update leaves active alone unless it is sent, so a
                # price refresh never revives a pair that was deactivated (Nov 9, 2026).
                ins_cols = list(cols)
                vals = ", ".join("%s::jsonb" if c == "extra" else "%s" for c in ins_cols)
                upd = [f"{c}=EXCLUDED.{c}" for c in ins_cols if c not in ("asin", "product_id")]
                sql = (f"INSERT INTO sephora_catalog ({', '.join(ins_cols)}) VALUES ({vals}) "
                       f"ON CONFLICT (asin, product_id) DO UPDATE SET {', '.join(upd + ['updated_at=now()'])}")
                params = [[g[c] for c in cols] for g in grp]
                async with conn.cursor() as cur:
                    await cur.executemany(sql, params)
            await _audit(conn, ENGINE, "upsert", "sephora", None, None, None,
                         {"catalog_rows": len(clean), "inserted": len(clean) - existing})
            await _notify(conn)
    out = {"upserted": len(clean), "inserted": len(clean) - existing, "updated": existing, "blocked": blocked,
           "restricted_skipped": len(skipped_restricted)}
    if to_park:
        out["parked"] = (await park_pairs(to_park, "Amazon listing restricted for Nick"))["parked"]
    return out


async def park_pairs(keys: list | None, reason: str, by: str = "engine") -> dict:
    """Move catalog pairs out of the Lead DB into sephora_parked_pairs (whole row, Nick's marks included) (Nick, Nov 24,
    2026: listings he can't sell don't belong in the Lead DB). keys = [{asin, product_id}], or None = every pair whose
    listing is restricted. Their open suggestions expire with the reason. Returns the parked rows (keep a backup)."""
    reason = _txt(reason, 300)
    if not reason:
        raise LedgerError("reason is required")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            if keys is None:
                cur = await conn.execute("SELECT * FROM sephora_catalog WHERE sellable = 'restricted'")
            else:
                if not isinstance(keys, list) or len(keys) > 5000:
                    raise LedgerError("keys must be a list of at most 5000 {asin, product_id}")
                cur = await conn.execute(
                    "SELECT c.* FROM sephora_catalog c JOIN unnest(%s::text[], %s::text[]) k(asin, pid) "
                    "ON c.asin = k.asin AND c.product_id = k.pid",
                    ([_asin(k.get("asin")) for k in keys], [_pid(k.get("product_id")) for k in keys]))
            rows = [jsonable(dict(r)) for r in await cur.fetchall()]
            if not rows:
                return {"ok": True, "parked": 0, "suggestions_expired": 0, "pairs": []}
            A, P = [r["asin"] for r in rows], [r["product_id"] for r in rows]
            await conn.execute(
                "INSERT INTO sephora_parked_pairs (asin, product_id, pair, reason) "
                "SELECT k.asin, k.pid, k.pair::jsonb, %s FROM unnest(%s::text[], %s::text[], %s::text[]) k(asin, pid, pair) "
                "ON CONFLICT (asin, product_id) DO UPDATE SET pair = EXCLUDED.pair, reason = EXCLUDED.reason, parked_at = now()",
                (reason, A, P, [json.dumps(r) for r in rows]))
            cur = await conn.execute(
                "UPDATE sephora_candidates s SET status='expired', expired_reason=%s, updated_at=now() "
                "FROM unnest(%s::text[], %s::text[]) k(asin, pid) WHERE s.asin = k.asin AND s.product_id = k.pid AND s.status='new'",
                (f"Removed from the Lead DB: {reason}", A, P))
            expired = cur.rowcount
            await conn.execute(
                "DELETE FROM sephora_catalog c USING unnest(%s::text[], %s::text[]) k(asin, pid) "
                "WHERE c.asin = k.asin AND c.product_id = k.pid", (A, P))
            await _audit(conn, ENGINE, "park_pairs", "sephora", None, None, None,
                         {"pairs": len(rows), "expired": expired, "reason": reason, "by": by})
            await _notify(conn)
    return {"ok": True, "parked": len(rows), "suggestions_expired": expired, "pairs": rows}


async def parked_pairs(offset: int = 0, limit: int = 2000) -> dict:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) n FROM sephora_parked_pairs")
        n = (await cur.fetchone())["n"]
        cur = await conn.execute("SELECT asin, product_id, reason, parked_at, checked_at, pair->>'brand' AS brand, "
                                 "pair->>'sellable' AS sellable FROM sephora_parked_pairs ORDER BY asin, product_id "
                                 "OFFSET %s LIMIT %s", (max(0, int(offset)), max(1, min(int(limit), 5000))))
        return {"total": n, "rows": [jsonable(dict(r)) for r in await cur.fetchall()]}


async def unpark_pairs(items: list) -> dict:
    """The monthly restriction check found these parked listings open (or needing one-click approval):
    items = [{asin, sellable}] -> each ASIN's parked pairs go back into the Lead DB as they were, with the new sellable.
    items = [{asin, checked: true}] (still restricted) only stamps checked_at."""
    if not isinstance(items, list) or len(items) > 5000:
        raise LedgerError("items must be a list of at most 5000 {asin, sellable}")
    back = {_asin(i.get("asin")): i.get("sellable") for i in items if i.get("sellable") in ("open", "approval_required")}
    seen = [_asin(i.get("asin")) for i in items]
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE sephora_parked_pairs SET checked_at = now() WHERE asin = ANY(%s)", (seen,))
            if not back:
                return {"ok": True, "restored": 0, "checked": len(seen)}
            # the stored row goes back as it was (jsonb_populate_record maps it onto today's columns), with the new sellable
            cur = await conn.execute(
                "INSERT INTO sephora_catalog SELECT (jsonb_populate_record(NULL::sephora_catalog, "
                "p.pair || jsonb_build_object('sellable', k.sellable, 'updated_at', now()))).* "
                "FROM sephora_parked_pairs p JOIN unnest(%s::text[], %s::text[]) k(asin, sellable) ON p.asin = k.asin "
                "ON CONFLICT (asin, product_id) DO NOTHING", (list(back), list(back.values())))
            pairs = cur.rowcount
            await conn.execute("DELETE FROM sephora_parked_pairs WHERE asin = ANY(%s)", (list(back),))
            await _audit(conn, ENGINE, "unpark_pairs", "sephora", None, None, None, {"asins": len(back), "pairs": pairs})
            await _notify(conn)
    return {"ok": True, "restored": pairs, "checked": len(seen)}


async def delete_asins(asins: list, reason: str, by: str = "engine") -> dict:
    """Remove ASINs from the Lead DB for good (Nick, Nov 24, 2026: generic-brand listings he can't sell): their catalog
    pairs, open suggestions (expired with the reason), the collector's stored Keepa data, and adds them to
    sephora_blocked_asins so no job adds them back. Returns the deleted pairs so the caller can keep a backup.
    Approved / rejected suggestions and wrong-match feedback stay as history."""
    asins = sorted({str(a).strip().upper() for a in (asins or []) if str(a).strip()})
    reason = _txt(reason, 300)
    if not asins or not reason:
        raise LedgerError("asins and reason are required")
    if len(asins) > 2000:
        raise LedgerError("At most 2000 ASINs per call")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT * FROM sephora_catalog WHERE asin = ANY(%s)", (asins,))
            pairs = [jsonable(dict(r)) for r in await cur.fetchall()]
            await conn.execute("DELETE FROM sephora_catalog WHERE asin = ANY(%s)", (asins,))
            cur = await conn.execute("UPDATE sephora_candidates SET status='expired', expired_reason=%s, updated_at=now() "
                                     "WHERE asin = ANY(%s) AND status='new'", (f"Removed from the Lead DB: {reason}", asins))
            expired = cur.rowcount
            await conn.execute("DELETE FROM keepa_raw WHERE asin = ANY(%s)", (asins,))
            await conn.execute("DELETE FROM keepa_snapshots WHERE asin = ANY(%s)", (asins,))
            await conn.execute("INSERT INTO sephora_blocked_asins (asin, reason, added_by) SELECT unnest(%s::text[]), %s, %s "
                               "ON CONFLICT (asin) DO NOTHING", (asins, reason, by))
            await _audit(conn, ENGINE, "delete_asins", "sephora", None, None, None,
                         {"asins": len(asins), "pairs": len(pairs), "expired": expired, "reason": reason})
            await _notify(conn)
    return {"ok": True, "asins": len(asins), "pairs_deleted": len(pairs), "suggestions_expired": expired, "pairs": pairs}


async def deactivate_catalog(keys: list) -> dict:
    if not isinstance(keys, list) or not keys:
        raise LedgerError("keys must be a non-empty list of {asin, product_id}")
    if len(keys) > 5000:
        raise LedgerError("At most 5000 keys per call")
    asins, pids = [], []
    for k in keys:
        if not isinstance(k, dict):
            raise LedgerError("Each key must be {asin, product_id}")
        asins.append(_asin(k.get("asin"))); pids.append(_pid(k.get("product_id")))
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "UPDATE sephora_catalog c SET active=false, updated_at=now() FROM unnest(%s::text[], %s::text[]) k(asin, pid) "
                "WHERE c.asin = k.asin AND c.product_id = k.pid AND c.active RETURNING c.asin", (asins, pids))
            n = len(await cur.fetchall())
            cur = await conn.execute(
                "UPDATE sephora_candidates s SET status='expired', updated_at=now() FROM unnest(%s::text[], %s::text[]) k(asin, pid) "
                "WHERE s.asin = k.asin AND s.product_id = k.pid AND s.status = 'new' RETURNING s.id", (asins, pids))
            exp = len(await cur.fetchall())
            await _notify(conn)
    return {"deactivated": n, "candidates_expired": exp}


CAT_COLS = ("asin, product_id, store, sku_id, brand, sephora_name, sephora_url, price_lo, price_hi, sale_price, on_sale, needs_size, "
            "size_guess, size_confirmed, confidence, match_reason, sellable, shade, amazon_title, buybox, fba_low, bb_avg90, "
            "referral_pct, fba_fee, monthly_sold, offers_fba, amazon_on, roi_now, roi_avg90, watch_price, match_ok, marked_by, "
            "marked_at, wrong_tags, wrong_note, correct_asin, store_sku_id, store_size, store_size_price, store_size_by, store_size_at, "
            "active, extra, created_at, updated_at")


async def engine_catalog(brand: str | None, active: str | None, limit: int, offset: int, asin: str | None = None) -> dict:
    w, a = [], []
    if brand:
        w.append("lower(brand) = lower(%s)"); a.append(brand.strip())
    if asin:
        w.append("asin = %s"); a.append(asin.strip().upper())
    if active not in (None, "", "all"):
        w.append("active = %s"); a.append(_bool(active))
    where = ("WHERE " + " AND ".join(w)) if w else ""
    limit = max(1, min(int(limit), 5000)); offset = max(0, int(offset))
    async with db.pool.connection() as conn:
        cur = await conn.execute(f"SELECT {CAT_COLS} FROM sephora_catalog {where} ORDER BY lower(brand), asin, product_id "
                                 f"LIMIT %s OFFSET %s", a + [limit + 1, offset])
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
        total = (await (await conn.execute(f"SELECT count(*) n FROM sephora_catalog {where}", a)).fetchone())["n"]
    more = len(rows) > limit
    return {"rows": rows[:limit], "total": total, "offset": offset, "limit": limit, "next_offset": offset + limit if more else None}


# ---------------------------------------------------------------- engine: candidates, runs, feedback

async def upsert_candidates(rows: list) -> dict:
    """The day's buy candidates, keyed by (asin, found_on). Pairs Nick marked wrong (or that are inactive / unknown) are
    skipped. A decided candidate keeps its decision. Older 'new' candidates of the same ASIN are expired."""
    if not isinstance(rows, list) or not rows:
        raise LedgerError("rows must be a non-empty list")
    if len(rows) > 1000:
        raise LedgerError("At most 1000 rows per call")
    clean = [clean_candidate_row(r) for r in rows]
    inserted = updated = expired = 0
    skipped = []
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for c in clean:
                cur = await conn.execute("SELECT match_ok, active FROM sephora_catalog WHERE asin=%s AND product_id=%s",
                                         (c["asin"], c["product_id"]))
                cat = await cur.fetchone()
                if not cat:
                    skipped.append({"asin": c["asin"], "product_id": c["product_id"], "why": "not in the catalog"}); continue
                if cat["match_ok"] is False:
                    skipped.append({"asin": c["asin"], "product_id": c["product_id"], "why": "Nick marked this a wrong match"}); continue
                if not cat["active"]:
                    skipped.append({"asin": c["asin"], "product_id": c["product_id"], "why": "catalog row is inactive"}); continue
                cols = list(c)
                upd = ", ".join(f"{k}=EXCLUDED.{k}" for k in cols if k not in ("asin", "found_on"))
                cur = await conn.execute(
                    f"INSERT INTO sephora_candidates ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                    f"ON CONFLICT (asin, found_on) DO UPDATE SET {upd}, updated_at=now() "
                    f"WHERE sephora_candidates.status = 'new' RETURNING (xmax = 0) AS ins",
                    [c[k] for k in cols])
                r = await cur.fetchone()
                if r is None:
                    skipped.append({"asin": c["asin"], "product_id": c["product_id"], "why": "already decided today"})
                elif r["ins"]:
                    inserted += 1
                else:
                    updated += 1
                cur = await conn.execute(
                    "UPDATE sephora_candidates SET status='expired', updated_at=now() WHERE asin=%s AND status='new' AND found_on < %s "
                    "RETURNING id", (c["asin"], c["found_on"]))
                expired += len(await cur.fetchall())
            await _audit(conn, ENGINE, "upsert", "sephora", None, None, None,
                         {"candidates": len(clean), "inserted": inserted, "skipped": len(skipped), "expired": expired})
            await _notify(conn)
    return {"inserted": inserted, "updated": updated, "expired": expired, "skipped": skipped}


async def recheck_candidates(rows: list) -> dict:
    """Daily re-price of open candidates (Nick, Nov 17, 2026): [{id, cost, price_basis, buybox, roi_now, expire, why}].
    expire -> status expired with expired_reason; else today's cost / Buy Box / ROI replace the old ones. Only 'new' rows change."""
    if not isinstance(rows, list) or len(rows) > 2000:
        raise LedgerError("rows must be a list (max 2000)")
    out = {"expired": 0, "refreshed": 0, "reopened": 0}
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for r in rows:
                cid = str((r or {}).get("id") or "")
                if not re.fullmatch(r"[0-9a-f-]{36}", cid):
                    continue
                if r.get("reopen"):   # undo an automatic expiry after a rule change (Nov 24, 2026); never a decided row
                    cur = await conn.execute(
                        "UPDATE sephora_candidates SET status='new', expired_reason=NULL, rechecked_at=now(), updated_at=now() "
                        "WHERE id=%s::uuid AND status='expired' AND decided_by IS NULL RETURNING id", (cid,))
                    out["reopened"] += len(await cur.fetchall())
                    continue
                if r.get("expire"):
                    cur = await conn.execute(
                        "UPDATE sephora_candidates SET status='expired', expired_reason=%s, rechecked_at=now(), updated_at=now() "
                        "WHERE id=%s::uuid AND status='new' RETURNING id", (_txt(r.get("why"), 500), cid))
                    out["expired"] += len(await cur.fetchall())
                    continue
                vals = {}
                for k in ("cost", "buybox", "roi_now"):
                    if r.get(k) is not None:
                        try:
                            vals[k] = float(r[k])
                        except (TypeError, ValueError):
                            raise LedgerError(f"{k} must be a number")
                if r.get("price_basis"):
                    vals["price_basis"] = _txt(r["price_basis"], 200)
                sets = ", ".join(f"{k}=%s" for k in vals)
                cur = await conn.execute(
                    f"UPDATE sephora_candidates SET {sets + ', ' if sets else ''}rechecked_at=now(), updated_at=now() "
                    f"WHERE id=%s::uuid AND status='new' RETURNING id", list(vals.values()) + [cid])
                out["refreshed"] += len(await cur.fetchall())
            await _audit(conn, ENGINE, "recheck", "sephora", None, None, None, out)
            await _notify(conn)
    return out


CAND_SQL = """
SELECT s.id, s.asin, s.product_id, s.found_on, s.reason, s.cost, s.price_basis, s.buybox, s.roi_now, s.lead_score_band,
       s.rechecked_at, s.expired_reason,
       s.lead_score_pred_roi, s.lead_score_notes, s.note, s.status, s.reject_reason, s.decided_by, s.decided_at, s.lead_id,
       s.created_at, s.updated_at,
       c.store, c.brand, c.sephora_name, c.sephora_url, c.shade, c.amazon_title, c.price_lo, c.price_hi, c.sale_price, c.on_sale,
       c.needs_size, c.size_guess, c.size_confirmed, c.confidence, c.match_reason, c.sellable, c.fba_low, c.bb_avg90,
       c.monthly_sold, c.offers_fba, c.amazon_on, c.roi_avg90, c.watch_price, c.match_ok, c.wrong_tags, c.wrong_note, c.correct_asin,
       c.store_sku_id, c.store_size, c.store_size_price,
       (SELECT w.status FROM sephora_stock_watch w WHERE w.candidate_id = s.id) AS stock_watch,
       (SELECT w.last_checked_at FROM sephora_stock_watch w WHERE w.candidate_id = s.id) AS stock_checked_at,
       (SELECT w.restocked_at FROM sephora_stock_watch w WHERE w.candidate_id = s.id) AS stock_restocked_at,
       c.active AS catalog_active,
       l.qty_ordered AS lead_qty_ordered, l.buy_qty AS lead_buy_qty
  FROM sephora_candidates s
  LEFT JOIN sephora_catalog c ON c.asin = s.asin AND c.product_id = s.product_id
  LEFT JOIN leads l ON l.id = s.lead_id
"""


async def list_candidates(status: str | None = None, since: str | None = None, limit: int = 1000, asin: str | None = None) -> dict:
    w, a = [], []
    if status and status != "all":
        sts = [x.strip() for x in status.split(",") if x.strip()]
        if set(sts) - set(STATUSES):
            raise LedgerError(f"status must be one of {', '.join(STATUSES)}")
        w.append("s.status = ANY(%s)"); a.append(sts)
    if since:
        w.append("s.found_on >= %s"); a.append(_day(since, "since"))
    if asin:
        w.append("s.asin = %s"); a.append(asin.strip().upper())
    where = ("WHERE " + " AND ".join(w)) if w else ""
    limit = max(1, min(int(limit), 5000))
    async with db.pool.connection() as conn:
        cur = await conn.execute(f"{CAND_SQL} {where} ORDER BY s.found_on DESC, s.roi_now DESC NULLS LAST, s.created_at DESC LIMIT %s",
                                 a + [limit])
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
    return {"candidates": rows, "count": len(rows)}


MISMATCH_KINDS = ("wrong", "held", "rejected")


async def mismatches(kind: str, f: dict, limit: int = 500, offset: int = 0) -> dict:
    """Lead DB -> Mismatches page (Nick, Nov 17, 2026: one place to look for false mismatches).
    wrong     catalog pairs marked wrong (by people or Claude), with the reason and the correct ASIN
    held      active pairs nobody has checked that the learned match rules hold back (match_reason has "rule (")
    rejected  candidates rejected from To review, with the reason
    Filters: brand, q (text), by (claude | people), days (marked / decided within N days)."""
    if kind not in MISMATCH_KINDS:
        raise LedgerError(f"kind must be one of {', '.join(MISMATCH_KINDS)}")
    w, a = [], []
    who, when = ("s.decided_by", "s.decided_at") if kind == "rejected" else ("c.marked_by", "c.marked_at")
    if kind == "wrong":
        w.append("c.match_ok IS FALSE")
    elif kind == "held":
        w += ["c.match_ok IS NULL", "c.active", "c.match_reason ILIKE %s"]; a.append("%rule (%")
    else:
        w.append("s.status = 'rejected'")
    if f.get("brand"):
        w.append("lower(c.brand) = lower(%s)"); a.append(f["brand"].strip())
    if f.get("q"):
        q = f"%{f['q'].strip()}%"
        w.append("(c.asin ILIKE %s OR c.sephora_name ILIKE %s OR c.amazon_title ILIKE %s OR c.brand ILIKE %s OR c.correct_asin ILIKE %s)")
        a += [q] * 5
    if kind != "held":
        if f.get("by") == "claude":
            w.append(f"{who} ILIKE %s"); a.append("claude%")
        elif f.get("by") == "people":
            w.append(f"{who} IS NOT NULL AND {who} NOT ILIKE %s"); a.append("claude%")
        if f.get("days"):
            try:
                d = float(f["days"])
            except ValueError:
                raise LedgerError("days must be a number")
            w.append(f"{when} >= now() - make_interval(secs => %s)"); a.append(d * 86400)
    where = "WHERE " + " AND ".join(w)
    limit = max(1, min(int(limit), 2000)); offset = max(0, int(offset))
    if kind == "rejected":
        base = f"{CAND_SQL.replace('SELECT s.id', 'SELECT c.extra, s.id', 1)} {where}"
        order = "ORDER BY s.decided_at DESC NULLS LAST, s.found_on DESC"
        count_sql = f"SELECT count(*) n FROM sephora_candidates s LEFT JOIN sephora_catalog c ON c.asin = s.asin AND c.product_id = s.product_id {where}"
    else:
        base = (f"SELECT {', '.join('c.' + x.strip() for x in CAT_COLS.split(','))}, "
                f"(SELECT k.amazon_title FROM sephora_catalog k WHERE k.asin = c.correct_asin ORDER BY (k.product_id = c.product_id) DESC LIMIT 1) AS correct_title "
                f"FROM sephora_catalog c {where}")
        order = "ORDER BY c.marked_at DESC NULLS LAST, lower(c.brand), c.asin" if kind == "wrong" else "ORDER BY c.updated_at DESC, lower(c.brand), c.asin"
        count_sql = f"SELECT count(*) n FROM sephora_catalog c {where}"
    async with db.pool.connection() as conn:
        cur = await conn.execute(f"{base} {order} LIMIT %s OFFSET %s", a + [limit, offset])
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
        total = (await (await conn.execute(count_sql, a)).fetchone())["n"]
        cur = await conn.execute(
            "SELECT (SELECT count(*) FROM sephora_catalog WHERE match_ok IS FALSE) wrong, "
            "(SELECT count(*) FROM sephora_catalog WHERE match_ok IS NULL AND active AND match_reason ILIKE %s) held, "
            "(SELECT count(*) FROM sephora_candidates WHERE status = 'rejected') rejected", ("%rule (%",))
        counts = dict(await cur.fetchone())
        cur = await conn.execute("SELECT DISTINCT brand FROM sephora_catalog WHERE brand IS NOT NULL AND (match_ok IS FALSE OR "
                                 "(match_ok IS NULL AND active AND match_reason ILIKE %s) OR (asin, product_id) IN "
                                 "(SELECT asin, product_id FROM sephora_candidates WHERE status = 'rejected')) ORDER BY brand", ("%rule (%",))
        brands = [r["brand"] for r in await cur.fetchall()]
    return {"kind": kind, "rows": rows, "total": total, "counts": counts, "brands": brands}


async def record_run(body: dict) -> dict:
    kind = str(body.get("kind") or "").strip()[:40]
    if not kind:
        raise LedgerError("kind is required (e.g. catalog, daily)")
    counts = body.get("counts") or {}
    if not isinstance(counts, dict):
        raise LedgerError("counts must be an object")
    sale_checked = "sale" in body
    sale = body.get("sale")
    if sale is not None:
        if not isinstance(sale, dict):
            raise LedgerError("sale must be an object {text, code, ends} or null (no sale on)")
        ends = sale.get("ends")
        sale = {"text": _txt(sale.get("text"), 300), "code": _txt(sale.get("code"), 60),
                "ends": _day(ends, "sale.ends").isoformat() if ends else None}
        if not (sale["text"] or sale["code"]):
            sale = None
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO sephora_runs (kind, counts, sale_checked, sale) VALUES (%s, %s::jsonb, %s, %s::jsonb) RETURNING id, at",
                (kind, json.dumps(counts), sale_checked, json.dumps(sale) if sale else None))
            r = await cur.fetchone()
            await _notify(conn)
    return {"id": r["id"], "at": r["at"].isoformat(), "sale_checked": sale_checked, "sale": sale}


async def active_sale(conn=None) -> dict | None:
    """The newest sale-watch result, if it found a sale that hasn't ended (Central time)."""
    async def q(c):
        cur = await c.execute("SELECT at, sale FROM sephora_runs WHERE sale_checked ORDER BY at DESC LIMIT 1")
        return await cur.fetchone()
    if conn is None:
        async with db.pool.connection() as c:
            r = await q(c)
    else:
        r = await q(conn)
    if not r or not r["sale"]:
        return None
    s = dict(r["sale"])
    if s.get("ends"):
        try:
            if date.fromisoformat(s["ends"]) < today():
                return None
        except ValueError:
            pass
    s["checked_at"] = r["at"].isoformat()
    return s


async def feedback(since: str | None = None) -> dict:
    """Decisions for the manager job: what Nick approved / rejected, and what the approved leads turned into."""
    w, a = ["s.status IN ('approved', 'rejected')"], []
    if since:
        w.append("s.decided_at >= %s"); a.append(_day(since, "since"))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT s.id, s.asin, s.product_id, s.found_on, s.reason, s.cost, s.buybox, s.roi_now, s.lead_score_band,
                       s.lead_score_pred_roi, s.status, s.reject_reason, s.decided_by, s.decided_at, s.lead_id,
                       c.brand, c.confidence, c.sellable, c.needs_size, c.size_confirmed, c.match_ok,
                       l.lead_date, l.buy_qty, l.qty_ordered, l.actual_spend, l.lead_score_band AS lead_band_now,
                       l.lead_score_roi AS lead_pred_roi_now
                  FROM sephora_candidates s
                  LEFT JOIN sephora_catalog c ON c.asin = s.asin AND c.product_id = s.product_id
                  LEFT JOIN leads l ON l.id = s.lead_id
                 WHERE {' AND '.join(w)} ORDER BY s.decided_at DESC""", a)
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
        cur = await conn.execute(
            f"""SELECT c.brand, count(*) FILTER (WHERE s.status='approved') approved, count(*) FILTER (WHERE s.status='rejected') rejected,
                       count(*) FILTER (WHERE s.status='rejected' AND (c.match_ok IS FALSE OR s.reject_reason = 'Wrong match')) wrong_match,
                       COALESCE(sum(l.qty_ordered), 0) units_ordered, COALESCE(sum(l.actual_spend), 0) spend
                  FROM sephora_candidates s
                  LEFT JOIN sephora_catalog c ON c.asin = s.asin AND c.product_id = s.product_id
                  LEFT JOIN leads l ON l.id = s.lead_id
                 WHERE {' AND '.join(w)} GROUP BY c.brand ORDER BY approved DESC, c.brand""", a)
        brands = [jsonable(dict(r)) for r in await cur.fetchall()]
        wrong = await wrong_matches(conn)
    return {"decisions": rows, "brands": brands, "wrong_matches": wrong}


def clean_wrong(body: dict) -> dict:
    """tags / note / correct_asin from a Wrong match dialog."""
    tags = body.get("tags") or body.get("wrong_tags") or []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\s]+", tags) if t]
    tags = [str(t).strip().lower() for t in tags if str(t).strip()]
    bad = [t for t in tags if t not in WRONG_TAGS]
    if bad:
        raise LedgerError(f"Unknown wrong-match reason {bad[0]!r}; use one of {', '.join(WRONG_TAGS)}")
    ca = body.get("correct_asin")
    ca = _asin(ca) if ca not in (None, "") else None
    return {"tags": list(dict.fromkeys(tags)), "note": _txt(body.get("note") or body.get("wrong_note"), 1000), "correct_asin": ca}


def wrong_text(w: dict) -> str:
    """'Wrong match: size, shade — note' for reject_reason."""
    t = ", ".join(WRONG_LABELS.get(x, x) for x in (w.get("tags") or []))
    return "Wrong match" + (f": {t}" if t else "") + (f" — {w['note']}" if w.get("note") else "")


async def log_wrong(conn, actor: Actor, source: str, asin: str, product_id: str | None, w: dict, list_path: str | None = None,
                    extra: dict | None = None) -> dict:
    """One match_feedback row (names and titles copied from the catalog when the pair is there)."""
    cat = {}
    if product_id:
        cur = await conn.execute("SELECT brand, sephora_name, sephora_url, amazon_title FROM sephora_catalog WHERE asin=%s AND product_id=%s",
                                 (asin, product_id))
        cat = dict(await cur.fetchone() or {})
    e = extra or {}
    cur = await conn.execute(
        "INSERT INTO match_feedback (source, asin, product_id, brand, sephora_name, sephora_url, amazon_title, tags, note, correct_asin, "
        "list_path, marked_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, created_at",
        (source, asin, product_id, e.get("brand") or cat.get("brand"), e.get("sephora_name") or cat.get("sephora_name"),
         e.get("sephora_url") or cat.get("sephora_url"), e.get("amazon_title") or cat.get("amazon_title"),
         w.get("tags") or [], w.get("note"), w.get("correct_asin"), list_path, actor.label))
    return jsonable(dict(await cur.fetchone()))


async def wrong_matches(conn=None, list_path: str | None = None) -> list:
    async def q(c):
        w, a = "", []
        if list_path:
            w, a = "WHERE list_path = %s", [list_path]
        cur = await c.execute(f"SELECT id, source, asin, product_id, brand, sephora_name, sephora_url, amazon_title, tags, note, correct_asin, "
                              f"list_path, marked_by, created_at FROM match_feedback {w} ORDER BY created_at DESC", a)
        return [jsonable(dict(r)) for r in await cur.fetchall()]
    if conn is not None:
        return await q(conn)
    async with db.pool.connection() as c:
        return await q(c)


# ---------------------------------------------------------------- page reads

async def overview() -> dict:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT DISTINCT ON (kind) kind, at, counts FROM sephora_runs ORDER BY kind, at DESC")
        runs = [jsonable(dict(r)) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT count(*) total, count(*) FILTER (WHERE active) active, count(*) FILTER (WHERE active AND match_ok IS NULL) unchecked, "
            "count(*) FILTER (WHERE match_ok) ok, count(*) FILTER (WHERE match_ok IS FALSE) wrong, "
            "count(*) FILTER (WHERE active AND needs_size AND size_confirmed IS NULL) size_open, "
            "count(DISTINCT lower(brand)) FILTER (WHERE active) brands FROM sephora_catalog")
        cat = jsonable(dict(await cur.fetchone()))
        cur = await conn.execute("SELECT status, count(*) n FROM sephora_candidates GROUP BY 1")
        cand = {r["status"]: r["n"] for r in await cur.fetchall()}
        sale = await active_sale(conn)
    return {"runs": runs, "catalog": cat, "candidates": cand, "sale": sale, "today": today().isoformat()}


CAT_SORTS = {   # Lead DB catalog sort options (whitelisted ORDER BY clauses)
    "": "lower(brand), sephora_name, asin",
    "roi": "roi_now DESC NULLS LAST, lower(brand), asin",
    "roi90": "roi_avg90 DESC NULLS LAST, lower(brand), asin",
    "sold": "monthly_sold DESC NULLS LAST, lower(brand), asin",
    "roi_asc": "roi_now ASC NULLS LAST, lower(brand), asin",
    "roi90_asc": "roi_avg90 ASC NULLS LAST, lower(brand), asin",
    "sold_asc": "monthly_sold ASC NULLS LAST, lower(brand), asin",
    "checked": "GREATEST(marked_at, NULLIF(extra->'verified'->>'date', '')::date::timestamptz) DESC NULLS LAST, lower(brand), asin",
}


# "Last checked" = the later of a person's/Claude's OK-wrong mark and a research verification (extra.verified, Nov 20, 2026:
# the verify step confirms right pairs without marking them, so they showed as never checked).
VER_AT = "NULLIF(extra->'verified'->>'date', '')::date::timestamptz"
CHK_AT = f"GREATEST(marked_at, {VER_AT})"
CHK_BY = f"CASE WHEN marked_at IS NOT NULL AND ({VER_AT} IS NULL OR marked_at >= {VER_AT}) THEN marked_by WHEN {VER_AT} IS NOT NULL THEN 'Claude (research)' END"


async def page_catalog(f: dict, limit: int = 300, offset: int = 0) -> dict:
    w, a = [], []
    if f.get("q"):
        q = f"%{f['q'].strip()}%"
        w.append("(asin ILIKE %s OR sephora_name ILIKE %s OR amazon_title ILIKE %s OR brand ILIKE %s OR shade ILIKE %s OR product_id ILIKE %s)")
        a += [q] * 6
    if f.get("brand"):
        w.append("lower(brand) = lower(%s)"); a.append(f["brand"].strip())
    if f.get("store"):
        w.append("store = %s"); a.append(f["store"].strip())
    if f.get("confidence"):
        w.append("confidence = %s"); a.append(f["confidence"])
    if f.get("sellable") and f["sellable"] != "any":
        w.append("sellable = %s"); a.append(f["sellable"])
    elif not f.get("sellable") and not f.get("q"):
        # Nick, Nov 20, 2026: listings he can't sell are kept (the monthly restriction check re-checks them) but hidden
        # by default; pick Restricted or Any to see them. A search still finds them.
        w.append("sellable IS DISTINCT FROM 'restricted'")
    if f.get("needs_size") in ("1", "true"):
        w.append("needs_size")
    if f.get("needs_size") == "open":
        w.append("needs_size AND size_confirmed IS NULL")
    m = f.get("match")
    held = "(match_ok IS NULL AND COALESCE(match_reason, '') ILIKE %s)"   # held back by a learned match rule (Mismatches page)
    if m == "ok":
        w.append("match_ok")
    elif m == "wrong":
        w.append("match_ok IS FALSE")
    elif m == "unchecked":
        w.append(f"match_ok IS NULL AND NOT {held}"); a.append("%rule (%")
    elif m == "held":
        w.append(held); a.append("%rule (%")
    elif m != "all":   # default (Nick, Nov 17, 2026): mismatches live on the Mismatches page, not in the catalog
        w.append(f"match_ok IS NOT FALSE AND NOT {held}"); a.append("%rule (%")
    if f.get("on_sale") in ("1", "0"):   # Sephora's own sale price on the product (Nick, Nov 17, 2026)
        w.append("COALESCE(on_sale, false)" if f["on_sale"] == "1" else "NOT COALESCE(on_sale, false)")
    by = f.get("checked_by")   # who last marked the pair OK / wrong / size (Nick, Nov 17, 2026: review Claude's recent work)
    if by == "claude":
        w.append(f"({CHK_BY}) ILIKE 'claude%%'")
    elif by == "people":
        w.append(f"({CHK_BY}) IS NOT NULL AND ({CHK_BY}) NOT ILIKE 'claude%%'")
    elif by == "any":
        w.append(f"({CHK_BY}) IS NOT NULL")
    if f.get("checked_days"):
        try:
            days = float(f["checked_days"])
        except ValueError:
            raise LedgerError("checked_days must be a number of days")
        w.append(f"{CHK_AT} >= now() - make_interval(secs => %s)"); a.append(days * 86400)
    for key, col in (("min_roi", "roi_now"), ("min_roi90", "roi_avg90")):
        if f.get(key):
            try:
                w.append(f"{col} >= %s"); a.append(float(f[key]) / 100)   # percent in, fraction stored
            except ValueError:
                raise LedgerError(f"{key} must be a number (percent)")
    act = f.get("active", "1")
    if act in ("1", "true"):
        w.append("active")
    elif act in ("0", "false"):
        w.append("NOT active")
    where = ("WHERE " + " AND ".join(w)) if w else ""
    limit = max(1, min(int(limit), 1000)); offset = max(0, int(offset))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {CAT_COLS}, {CHK_AT} AS checked_at, {CHK_BY} AS checked_by, (SELECT count(*) FROM sephora_candidates s WHERE s.asin = c.asin AND s.product_id = c.product_id "
            f"AND s.status = 'new') AS open_candidates FROM sephora_catalog c {where} "
            f"ORDER BY {CAT_SORTS.get(f.get('sort') or '', CAT_SORTS[''])} LIMIT %s OFFSET %s", a + [limit, offset])
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
        total = (await (await conn.execute(f"SELECT count(*) n FROM sephora_catalog c {where}", a)).fetchone())["n"]
        # per-brand counts over everything active (not just the filtered rows), for the brand list
        cur = await conn.execute(
            # n = pairs the catalog shows by default: mismatches (marked wrong, or held back by a rule) are counted apart
            "SELECT brand, count(*) FILTER (WHERE match_ok IS NOT FALSE AND NOT (match_ok IS NULL AND COALESCE(match_reason, '') ILIKE %s)) n, "
            "count(*) FILTER (WHERE match_ok IS NULL AND COALESCE(match_reason, '') NOT ILIKE %s) unchecked, count(*) FILTER (WHERE match_ok) ok, "
            "count(*) FILTER (WHERE match_ok IS FALSE OR (match_ok IS NULL AND COALESCE(match_reason, '') ILIKE %s)) wrong "
            "FROM sephora_catalog WHERE active GROUP BY brand ORDER BY count(*) DESC, brand", ("%rule (%",) * 3)
        brands = [jsonable(dict(r)) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT store, count(*) n FROM sephora_catalog WHERE active GROUP BY store ORDER BY count(*) DESC")
        stores = [jsonable(dict(r)) for r in await cur.fetchall()]
    return {"rows": rows, "total": total, "offset": offset, "limit": limit, "brands": brands, "stores": stores}


# ---------------------------------------------------------------- Nick's actions

async def set_correct_asin(actor: Actor, asin: str, product_id: str, correct_asin: str | None) -> dict:
    """Add (or clear) the right ASIN on a pair already marked wrong, without changing who marked it or why.
    Updates the catalog row and its match_feedback rows (Nick, Nov 17, 2026: find the correct ASIN for every mismatch)."""
    asin, product_id = _asin(asin), _pid(product_id)
    ca = _asin(correct_asin) if correct_asin else None
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT match_ok, correct_asin FROM sephora_catalog WHERE asin=%s AND product_id=%s FOR UPDATE",
                                     (asin, product_id))
            before = await cur.fetchone()
            if not before:
                raise LedgerError("That catalog pair doesn't exist")
            if before["match_ok"] is not False:
                raise LedgerError("Only a pair marked wrong can carry a correct ASIN")
            await conn.execute("UPDATE sephora_catalog SET correct_asin=%s, updated_at=now() WHERE asin=%s AND product_id=%s",
                               (ca, asin, product_id))
            cur = await conn.execute("UPDATE match_feedback SET correct_asin=%s WHERE asin=%s AND product_id=%s RETURNING id",
                                     (ca, asin, product_id))
            n = len(await cur.fetchall())
            await _audit(conn, actor, "mark", "sephora", None, None,
                         {"asin": asin, "product_id": product_id, "correct_asin": before["correct_asin"]},
                         {"asin": asin, "product_id": product_id, "correct_asin": ca})
            await _notify(conn)
    return {"asin": asin, "product_id": product_id, "correct_asin": ca, "feedback_rows": n}


async def mark_catalog(actor: Actor, asin: str, product_id: str, body: dict) -> dict:
    """Nick's answers on a catalog pair: match_ok (true / false / null to clear), size_confirmed ('lo' / 'hi' / null)."""
    asin, product_id = _asin(asin), _pid(product_id)
    sets, args, after = [], [], {}
    if "match_ok" in body:
        v = body["match_ok"]
        v = None if v is None or v == "" else _bool(v)
        sets.append("match_ok=%s"); args.append(v); after["match_ok"] = v
    if "size_confirmed" in body:
        v = _txt(body["size_confirmed"])
        if v is not None and v not in ("lo", "hi"):
            raise LedgerError("size_confirmed must be lo (mini / smaller size), hi (full size) or empty")
        sets.append("size_confirmed=%s"); args.append(v); after["size_confirmed"] = v
    if "store_size" in body:   # {"sku_id", "size", "price"} = the store SKU this ASIN matches; null clears it
        v = body["store_size"]
        if v in (None, "", {}):
            sets += ["store_sku_id=NULL", "store_size=NULL", "store_size_price=NULL", "store_size_by=NULL", "store_size_at=NULL"]
            after["store_size"] = None
        else:
            if not isinstance(v, dict):
                raise LedgerError("store_size must be {sku_id, size, price}")
            sku, label = _txt(v.get("sku_id")), _txt(v.get("size"))
            try:
                price = round(float(str(v.get("price")).replace("$", "").replace(",", "")), 2)
            except (TypeError, ValueError):
                raise LedgerError("store_size.price must be a number")
            if not (sku and sku.isdigit() and label and 0 < price < 100000):
                raise LedgerError("store_size needs a numeric sku_id, a size and a price")
            sets += ["store_sku_id=%s", "store_size=%s", "store_size_price=%s", "store_size_by=%s", "store_size_at=now()"]
            args += [sku, label[:60], price, actor.label]
            after["store_size"] = {"sku_id": sku, "size": label[:60], "price": price}
    wrong = None
    if "match_ok" in after:
        if after["match_ok"] is False:
            wrong = clean_wrong(body)
            sets += ["wrong_tags=%s", "wrong_note=%s", "correct_asin=%s"]; args += [wrong["tags"], wrong["note"], wrong["correct_asin"]]
            after.update(wrong_tags=wrong["tags"], wrong_note=wrong["note"], correct_asin=wrong["correct_asin"])
        else:   # OK or cleared: the old wrong-match reason no longer applies
            sets.append("wrong_tags=NULL, wrong_note=NULL, correct_asin=NULL")
    if not sets:
        raise LedgerError("Nothing to change")
    source = body.get("_source") or "catalog"
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT match_ok, size_confirmed, store_size FROM sephora_catalog WHERE asin=%s AND product_id=%s "
                                     "FOR UPDATE", (asin, product_id))
            before = await cur.fetchone()
            if not before:
                raise LedgerError("That catalog pair doesn't exist")
            stamp = "marked_by=%s, marked_at=now(), " if ("match_ok" in after or "size_confirmed" in after) else ""
            await conn.execute(f"UPDATE sephora_catalog SET {', '.join(sets)}, {stamp}updated_at=now() "
                               f"WHERE asin=%s AND product_id=%s", args + ([actor.label] if stamp else []) + [asin, product_id])
            expired = 0
            if "match_ok" in after and after["match_ok"] is not False:   # changed his mind: stop learning from the old mark
                await conn.execute("DELETE FROM match_feedback WHERE asin=%s AND product_id=%s", (asin, product_id))
            if after.get("match_ok") is False:   # a wrong match: its open candidates go too
                cur = await conn.execute(
                    "UPDATE sephora_candidates SET status='rejected', reject_reason=COALESCE(reject_reason, %s), "
                    "decided_by=%s, decided_at=now(), updated_at=now() WHERE asin=%s AND product_id=%s AND status='new' RETURNING id",
                    (wrong_text(wrong), actor.label, asin, product_id))
                expired = len(await cur.fetchall())
                # Fallback: if no other active, not-wrong pair is left for this ASIN, switch its inactive
                # not-wrong pairs back on (e.g. Nick's lead pair stored inactive behind the matcher's pick).
                cur = await conn.execute(
                    "UPDATE sephora_catalog c SET active=true, updated_at=now() WHERE c.asin=%s AND c.product_id<>%s "
                    "AND c.active IS NOT TRUE AND c.match_ok IS NOT FALSE AND NOT EXISTS (SELECT 1 FROM sephora_catalog o "
                    "WHERE o.asin=c.asin AND o.product_id<>%s AND o.active AND o.match_ok IS NOT FALSE) RETURNING product_id",
                    (asin, product_id, product_id))
                after["reactivated"] = [r["product_id"] for r in await cur.fetchall()]
                if not body.get("_no_log"):
                    await log_wrong(conn, actor, source, asin, product_id, wrong, body.get("_list_path"))
            await _audit(conn, actor, "mark", "sephora", None, None,
                         {"asin": asin, "product_id": product_id, **{k: before[k] for k in after if k in before}},
                         {"asin": asin, "product_id": product_id, **after})
            await _notify(conn)
    return {"asin": asin, "product_id": product_id, **after, "candidates_rejected": expired}


async def _candidate(conn, cid: str, lock: bool = False) -> dict:
    try:
        cur = await conn.execute(f"{CAND_SQL} WHERE s.id = %s" + (" FOR UPDATE OF s" if lock else ""), (cid,))
    except Exception as e:
        if "uuid" in str(e).lower():
            raise LedgerError(f"{cid!r} isn't a valid candidate id")
        raise
    r = await cur.fetchone()
    if not r:
        raise LedgerError("That candidate no longer exists")
    return dict(r)


def _size_cost(c: dict) -> tuple[Decimal | None, str | None]:
    """The candidate's cost, moved to the size Nick confirmed when the job priced the other size.
    Keeps any discount: new cost = cost x (confirmed size price / guessed size price)."""
    cost = c.get("cost")
    if cost is None or not c.get("needs_size") or c.get("size_confirmed") not in ("lo", "hi"):
        return cost, None
    price = {"lo": c.get("price_lo"), "hi": c.get("price_hi")}
    basis = c.get("size_guess") if c.get("size_guess") in ("lo", "hi") else None
    if basis is None:   # unknown guess: the candidate cost was one of the two list prices
        for k in ("lo", "hi"):
            if price[k] is not None and abs(Decimal(price[k]) - Decimal(cost)) < Decimal("0.01"):
                basis = k
    want = c["size_confirmed"]
    if basis is None or basis == want or not price[basis] or not price[want]:
        return cost, None
    new = (Decimal(cost) * Decimal(price[want]) / Decimal(price[basis])).quantize(Decimal("0.01"))
    label = "full size" if want == "hi" else "mini / smaller size"
    return new, f"cost moved to the {label} price (${new})"


def _pct(v) -> str:
    return "?" if v is None else f"{float(v) * 100:.0f}%"


REASON_TEXT = {"price_watch": "Sephora price drop", "sale": "Sephora sale", "new_product": "New at Sephora"}


async def approve(actor: Actor, cid: str, size: str | None = None) -> dict:
    """Make a lead from the candidate through the Leads page's own code path (leads.create_lead)."""
    if size not in (None, "", "lo", "hi"):
        raise LedgerError("size must be lo (mini / smaller size) or hi (full size)")
    async with _approve_lock:
        async with db.pool.connection() as conn:
            c = await _candidate(conn, cid)
        if c["status"] != "new":
            raise LedgerError(f"This candidate was already {c['status']}")
        if c.get("match_ok") is False:
            raise LedgerError("This pair was marked a wrong match")
        if size:
            await mark_catalog(actor, c["asin"], c["product_id"], {"size_confirmed": size})
            c["size_confirmed"] = size
        # No size gate (Nick, Nov 24, 2026): the pair's own Sephora item (store_size) settles the size, and an unknown
        # size keeps the job's full-size price ("size not checked" goes in the lead note).
        if c.get("store_size"):
            c["needs_size"] = False
        day = today()
        async with db.pool.connection() as conn:
            cur = await conn.execute("SELECT id FROM leads WHERE upper(btrim(asin)) = %s AND lead_date = %s LIMIT 1", (c["asin"], day))
            dup = await cur.fetchone()
        if dup:
            raise LedgerError(f"There's already a lead for {c['asin']} today; not adding another (open it on the Leads page)")
        cost, cost_note = _size_cost(c)
        sale = await active_sale()
        bits = [REASON_TEXT.get(c["reason"], c["reason"])]
        bits.append(f"ROI now {_pct(c.get('roi_now'))}" + (" at the other size" if cost_note else ""))
        if c.get("lead_score_band") is not None:
            bits.append(f"Lead Score {c['lead_score_band']} ({_pct(c.get('lead_score_pred_roi'))})")
        if c.get("shade"):
            bits.append(f"shade {c['shade']}")
        if c.get("store_size"):
            bits.append(f"size {c['store_size']}")
        elif c.get("needs_size") and c.get("size_confirmed") in ("lo", "hi"):
            bits.append("full size" if c["size_confirmed"] == "hi" else "mini / smaller size")
        elif c.get("needs_size"):
            bits.append("size not checked (priced at full size)")
        if cost_note:
            bits.append(cost_note)
        data = {"lead_date": day, "asin": c["asin"], "stores": [c.get("store") or "Sephora"], "brands": [c["brand"]] if c.get("brand") else [],
                "store_link": c.get("sephora_url"), "cost": cost, "promo_code": (sale or {}).get("code"),
                "sourced_by": "Claude", "va_notes": " · ".join(bits)}
        lead = await L.create_lead(actor, {k: v for k, v in data.items() if v not in (None, "", [])})
        async with db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE sephora_candidates SET status='approved', decided_by=%s, decided_at=now(), lead_id=%s, "
                                   "updated_at=now() WHERE id=%s", (actor.label, lead["id"], cid))
                if c.get("match_ok") is None:   # approving it says the match is right
                    await conn.execute("UPDATE sephora_catalog SET match_ok=true, marked_by=%s, marked_at=now(), updated_at=now() "
                                       "WHERE asin=%s AND product_id=%s AND match_ok IS NULL", (actor.label, c["asin"], c["product_id"]))
                await _audit(conn, actor, "approve", "sephora", cid, None, {"status": "new"},
                             {"status": "approved", "asin": c["asin"], "lead_id": lead["id"], "cost": cost})
                await _notify(conn)
    return {"ok": True, "lead_id": lead["id"], "asin": c["asin"], "cost": jsonable(cost), "promo_code": data["promo_code"],
            "cost_note": cost_note}


async def new_upload_token(purpose: str, hours: float) -> dict:
    """A short-lived token that only allows posting SKU lists (store_skus), for scripts running inside a store's page."""
    import secrets
    if purpose not in ("store_skus", "amazon_pages"):
        raise LedgerError("purpose must be store_skus or amazon_pages")
    hours = max(0.25, min(float(hours or 8), 24))
    tok = secrets.token_urlsafe(24)
    async with db.pool.connection() as conn:
        await conn.execute("DELETE FROM upload_tokens WHERE expires_at < now()")
        await conn.execute("INSERT INTO upload_tokens (token, purpose, expires_at) VALUES (%s, %s, now() + make_interval(secs => %s))",
                           (tok, purpose, hours * 3600))
    return {"token": tok, "hours": hours}


async def upload_store_skus(token: str | None, body: dict) -> dict:
    """{products: {P123: {vt, skus: [[sku_id, size, variation_value, variation_desc, price, out_of_stock], ...]}}} from the
    product pages in Nick's Chrome. Needs a live store_skus upload token. Only writes store_skus."""
    if not token:
        raise LedgerError("Missing upload token")
    prods = (body or {}).get("products")
    if not isinstance(prods, dict) or not prods or len(prods) > 1000:
        raise LedgerError("products must be an object of up to 1000 products")
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM upload_tokens WHERE token=%s AND purpose='store_skus' AND expires_at > now()", (token,))
        if not await cur.fetchone():
            raise LedgerError("Upload token is invalid or expired")
        n = 0
        async with conn.transaction():
            for pid, v in prods.items():
                pid = _pid(pid)
                if not isinstance(v, dict) or not isinstance(v.get("skus"), list) or len(v["skus"]) > 400:
                    continue
                skus = []
                for x in v["skus"]:
                    if not isinstance(x, list) or len(x) < 6 or not str(x[0]).isdigit():
                        continue
                    price = None
                    try:
                        price = round(float(str(x[4]).replace("$", "").replace(",", "")), 2) if x[4] not in (None, "") else None
                    except ValueError:
                        pass
                    ex = x[6] if len(x) > 6 and isinstance(x[6], dict) else {}
                    value = _txt(x[2], 120)
                    if not value:   # shade name from Sephora's other name fields or the image alt text (Nick, Nov 24, 2026)
                        for cand in list(ex.get("names") or []):   # not the alt text: it's "product name + size"
                            cand = _txt(cand, 120)
                            if cand and not cand.isdigit() and cand != str(x[0]):
                                value = cand
                                break
                    sku = {"sku_id": str(x[0]), "size": _txt(x[1], 80), "value": value, "desc": _txt(x[3], 160),
                           "price": price, "oos": bool(x[5])}
                    for k in ("sw", "img"):
                        u = _txt(ex.get(k), 400)
                        if u and u.startswith("/productimages/"):
                            sku[k] = u
                    skus.append(sku)
                await conn.execute(
                    "INSERT INTO store_skus (store, product_id, variation_type, skus, read_at) VALUES ('Sephora', %s, %s, %s, now()) "
                    "ON CONFLICT (store, product_id) DO UPDATE SET variation_type=EXCLUDED.variation_type, skus=EXCLUDED.skus, read_at=now()",
                    (pid, _txt(v.get("vt"), 40), json.dumps(skus)))
                n += 1
    return {"saved": n}


async def upload_sku_images(token: str | None, body: dict) -> dict:
    """{images: [{sku_id, kind: 'sw'|'main', ct: 'image/jpeg', b64}]} from Sephora pages in Nick's Chrome (Nov 24, 2026).
    Needs a live store_skus upload token. Only writes store_sku_images."""
    import base64
    if not token:
        raise LedgerError("Missing upload token")
    imgs = (body or {}).get("images")
    if not isinstance(imgs, list) or not imgs or len(imgs) > 80:
        raise LedgerError("images must be a list of up to 80")
    n = 0
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM upload_tokens WHERE token=%s AND purpose='store_skus' AND expires_at > now()", (token,))
        if not await cur.fetchone():
            raise LedgerError("Upload token is invalid or expired")
        async with conn.transaction():
            for x in imgs:
                if not isinstance(x, dict) or not str(x.get("sku_id") or "").isdigit() or x.get("kind") not in ("sw", "main"):
                    continue
                ct = str(x.get("ct") or "image/jpeg")
                if not ct.startswith("image/"):
                    continue
                try:
                    b = base64.b64decode(str(x.get("b64") or ""), validate=True)
                except Exception:  # noqa: BLE001
                    continue
                if not b or len(b) > 400_000:
                    continue
                await conn.execute(
                    "INSERT INTO store_sku_images (store, sku_id, kind, content_type, bytes, read_at) VALUES ('Sephora', %s, %s, %s, %s, now()) "
                    "ON CONFLICT (store, sku_id, kind) DO UPDATE SET content_type=EXCLUDED.content_type, bytes=EXCLUDED.bytes, read_at=now()",
                    (str(x["sku_id"]), x["kind"], ct[:40], b))
                n += 1
    return {"saved": n}


async def sku_image(sku_id: str, kind: str) -> tuple[bytes, str] | None:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT content_type, bytes FROM store_sku_images WHERE store='Sephora' AND sku_id=%s AND kind=%s",
                                 (str(sku_id), kind))
        r = await cur.fetchone()
    return (bytes(r["bytes"]), r["content_type"]) if r else None


async def sku_images_have(sku_ids: list[str]) -> dict:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT sku_id, array_agg(kind) kinds FROM store_sku_images WHERE store='Sephora' AND sku_id = ANY(%s) "
                                 "GROUP BY sku_id", ([str(s) for s in sku_ids],))
        return {r["sku_id"]: r["kinds"] for r in await cur.fetchall()}


async def upload_store_pages(token: str | None, body: dict) -> dict:
    """{pages: {P123: {long, highlights, ingredients, oos, alt}}} from product pages in Nick's Chrome (same upload token
    as the SKU lists). Only writes store_pages."""
    pages = (body or {}).get("pages")
    if not token or not isinstance(pages, dict) or not pages or len(pages) > 500:
        raise LedgerError("Need an upload token and pages (up to 500)")
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM upload_tokens WHERE token=%s AND purpose='store_skus' AND expires_at > now()", (token,))
        if not await cur.fetchone():
            raise LedgerError("Upload token is invalid or expired")
        n = 0
        async with conn.transaction():
            for pid, v in pages.items():
                if not isinstance(v, dict):
                    continue
                d = {"long": _txt(v.get("long"), 3000), "short": _txt(v.get("short"), 600), "usage": _txt(v.get("usage"), 600),
                     "highlights": [_txt(h, 80) for h in (v.get("highlights") or [])[:12] if h],
                     "ingredients": _txt(v.get("ingredients"), 600), "oos": [_txt(x, 80) for x in (v.get("oos") or [])[:80]],
                     "alt_images": int(v.get("alt") or 0) if str(v.get("alt") or "0").isdigit() else 0}
                await conn.execute("INSERT INTO store_pages (store, product_id, data, read_at) VALUES ('Sephora', %s, %s, now()) "
                                   "ON CONFLICT (store, product_id) DO UPDATE SET data=EXCLUDED.data, read_at=now()", (_pid(pid), json.dumps(d)))
                n += 1
    return {"saved": n}


async def upload_amazon_pages(token: str | None, body: dict) -> dict:
    """{pages: {ASIN: {title, selected, price, bullets, details, image}}} read from Amazon product pages in Nick's Chrome for the
    verify step (Nov 20, 2026: the cloud's Amazon fetches were mostly blocked). Token purpose 'amazon_pages'; only writes
    store_pages rows with store 'Amazon'."""
    pages = (body or {}).get("pages")
    if not token or not isinstance(pages, dict) or not pages or len(pages) > 500:
        raise LedgerError("Need an upload token and pages (up to 500)")
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM upload_tokens WHERE token=%s AND purpose='amazon_pages' AND expires_at > now()", (token,))
        if not await cur.fetchone():
            raise LedgerError("Upload token is invalid or expired")
        n = 0
        async with conn.transaction():
            for asin, v in pages.items():
                a = str(asin or "").strip().upper()
                if not re.fullmatch(r"[A-Z0-9]{10}", a) or not isinstance(v, dict):
                    continue
                det = v.get("details") if isinstance(v.get("details"), dict) else {}
                d = {"title": _txt(v.get("title"), 300), "price": _txt(v.get("price"), 20), "blocked": bool(v.get("blocked")),
                     "selected": [_txt(x, 120) for x in (v.get("selected") or [])[:6] if x],
                     "bullets": [_txt(x, 300) for x in (v.get("bullets") or [])[:8] if x],
                     "details": {_txt(k, 60): _txt(x, 200) for k, x in list(det.items())[:40] if k and x}}
                img = str(v.get("image") or "")
                if re.fullmatch(r"[A-Za-z0-9+%._-]{5,80}\.(jpg|jpeg|png|webp)", img):   # main photo file name on m.media-amazon.com/images/I/
                    d["image"] = img
                await conn.execute("INSERT INTO store_pages (store, product_id, data, read_at) VALUES ('Amazon', %s, %s, now()) "
                                   "ON CONFLICT (store, product_id) DO UPDATE SET data=EXCLUDED.data, read_at=now()", (a, json.dumps(d)))
                n += 1
    return {"saved": n}


async def amazon_pages(asins: list | None) -> dict:
    async with db.pool.connection() as conn:
        if asins:
            cur = await conn.execute("SELECT product_id, data, read_at FROM store_pages WHERE store='Amazon' AND product_id = ANY(%s)",
                                     ([str(a).strip().upper() for a in asins],))
        else:
            cur = await conn.execute("SELECT product_id, data, read_at FROM store_pages WHERE store='Amazon'")
        return {"rows": [{"asin": r["product_id"], "data": r["data"], "read_at": r["read_at"].isoformat()} for r in await cur.fetchall()]}


async def store_pages(product_ids: list | None) -> dict:
    async with db.pool.connection() as conn:
        if product_ids:
            cur = await conn.execute("SELECT product_id, data, read_at FROM store_pages WHERE product_id = ANY(%s)", (product_ids,))
        else:
            cur = await conn.execute("SELECT product_id, data, read_at FROM store_pages")
        return {"rows": [{"product_id": r["product_id"], "data": r["data"], "read_at": r["read_at"].isoformat()} for r in await cur.fetchall()]}


async def store_skus_todo(token: str | None, limit: int) -> dict:
    """Sephora product pages not read yet (no store_skus row), most profitable pairs first. Upload-token gated, for the
    reader script inside Nick's Chrome."""
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM upload_tokens WHERE token=%s AND purpose='store_skus' AND expires_at > now()", (token or "",))
        if not await cur.fetchone():
            raise LedgerError("Upload token is invalid or expired")
        cur = await conn.execute(
            "SELECT c.product_id, min(split_part(c.sephora_url, '?', 1)) AS url, count(*) FILTER (WHERE c.roi_now >= 0.15) AS prof, count(*) AS n "
            "FROM sephora_catalog c WHERE c.active IS NOT FALSE AND c.match_ok IS NOT FALSE AND lower(c.brand) <> 'chanel' AND c.sephora_url LIKE 'https://www.sephora.com/product/%%' "
            "AND NOT EXISTS (SELECT 1 FROM store_skus s WHERE s.store='Sephora' AND s.product_id=c.product_id) "
            "GROUP BY c.product_id ORDER BY prof DESC, max(GREATEST(COALESCE(c.roi_now, -9), COALESCE(c.roi_avg90, -9))) DESC, n DESC, c.product_id LIMIT %s", (max(1, min(int(limit or 100), 2000)),))
        rows = await cur.fetchall()
        cur = await conn.execute("SELECT count(DISTINCT product_id) AS left FROM sephora_catalog c WHERE c.active IS NOT FALSE AND c.match_ok IS NOT FALSE AND lower(c.brand) <> 'chanel' "
                                 "AND NOT EXISTS (SELECT 1 FROM store_skus s WHERE s.store='Sephora' AND s.product_id=c.product_id)")
        left = (await cur.fetchone())["left"]
    return {"left": left, "items": [r["product_id"] + "|" + r["url"].replace("https://www.sephora.com", "") for r in rows]}


async def store_skus(product_ids: list[str] | None) -> dict:
    async with db.pool.connection() as conn:
        if product_ids:
            cur = await conn.execute("SELECT * FROM store_skus WHERE product_id = ANY(%s)", ([_pid(p) for p in product_ids],))
        else:
            cur = await conn.execute("SELECT * FROM store_skus")
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
    return {"rows": rows}


async def match_decisions(scope: str = "profitable", offset: int = 0, limit: int = 40) -> dict:
    """The match check's undecided cases for Nick (Hub doc sephora_jobs/verify_for_nick), with what he needs to decide
    each: the research verdict and evidence, the Amazon photos and title (Keepa collector), the Sephora product, its
    item list and a photo of the saved item, and the lead's numbers (Nick, Nov 24, 2026: "Match decisions" page).
    scope: profitable (an open suggestion, or ROI now / 90-day 15%+) | all. Decided pairs (match_ok set) drop out."""
    from . import docs as hubdocs, training
    doc = await hubdocs.get("sephora_jobs/verify_for_nick") or {}
    data = doc.get("data") or {}
    items = (data.get("items") if isinstance(data, dict) else data) or []
    keys = [(str(x.get("asin") or ""), str(x.get("product_id") or "")) for x in items]
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT c.asin, c.product_id, c.brand, c.sephora_name, c.sephora_url, c.shade, c.amazon_title, c.price_lo, c.price_hi,
                      c.sale_price, c.roi_now, c.roi_avg90, c.buybox, c.sku_id, c.store_sku_id, c.store_size, c.store_size_price, c.match_ok,
                      c.sellable, c.active, c.match_reason,
                      s.id AS cand_id, s.status AS cand_status, s.cost AS cand_cost, s.roi_now AS cand_roi, s.lead_score_pred_roi,
                      s.expired_reason
                 FROM sephora_catalog c
                 JOIN unnest(%s::text[], %s::text[]) k(asin, pid) ON c.asin = k.asin AND c.product_id = k.pid
                 LEFT JOIN LATERAL (SELECT * FROM sephora_candidates x WHERE x.asin = c.asin AND x.product_id = c.product_id
                                    ORDER BY x.found_on DESC LIMIT 1) s ON true""",
            ([k[0] for k in keys], [k[1] for k in keys]))
        cat = {(r["asin"], r["product_id"]): dict(r) for r in await cur.fetchall()}
    rows = []
    for x, k in zip(items, keys):
        c = cat.get(k)
        if not c or c["match_ok"] is not None or not c["active"]:
            continue
        roi = max([v for v in (c["roi_now"], c["roi_avg90"], c["cand_roi"]) if v is not None] or [-9])
        held = c["cand_status"] == "new" or (c["cand_status"] == "expired" and str(c["expired_reason"] or "").startswith("Match check"))
        profitable = c["sellable"] == "open" and (held or roi >= 0.15)
        if scope != "all" and not profitable:
            continue
        rows.append({"research": {kk: x.get(kk) for kk in ("verdict", "confidence", "evidence", "lesson", "principle", "sources",
                                                            "sku_id", "correct_product_id", "bundle")},
                     **{kk: c[kk] for kk in ("asin", "product_id", "brand", "sephora_name", "sephora_url", "shade", "amazon_title",
                                             "price_lo", "price_hi", "sale_price", "buybox", "sku_id", "store_sku_id", "store_size",
                                             "store_size_price", "sellable", "match_reason", "cand_status", "cand_cost",
                                             "lead_score_pred_roi")},
                     "roi": roi if roi > -9 else None, "profitable": profitable})
    order = {"other": 0, "not_sold": 1, "cant_tell": 2, "same": 3}
    rows.sort(key=lambda r: (not r["profitable"], order.get(r["research"]["verdict"], 4), -(r["roi"] or -9)))
    total = len(rows)
    page = rows[offset:offset + max(1, min(limit, 60))]
    asins = list(dict.fromkeys(r["asin"] for r in page))
    meta = (await training.keepa_meta(asins))["products"] if asins else {}
    skus = {r["product_id"]: r for r in (await store_skus(list({r["product_id"] for r in page})))["rows"]} if page else {}
    for r in page:
        m = meta.get(r["asin"]) or {}
        r["amazon"] = {"title": m.get("title") or r["amazon_title"], "brand": m.get("brand"), "size": m.get("size"),
                       "color": m.get("color"),
                       "images": [i.get("l") or i.get("m") for i in (m.get("images") or []) if isinstance(i, dict) and (i.get("l") or i.get("m"))][:4]}
        r["sephora_items"] = (skus.get(r["product_id"]) or {}).get("skus") or []
    return {"total": total, "offset": offset, "rows": jsonable(page)}


OOS_RE = re.compile(r"\b(not in stock|out of stock|sold out|oos)\b", re.I)


async def stock_watch(status: str = "watching") -> dict:
    """Stock watches (Nick, Nov 17, 2026). status: watching (what the Chrome check should read) | all."""
    w = "w.status = 'watching' AND c.active AND c.match_ok IS NOT FALSE" if status == "watching" else "TRUE"
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT w.id, w.candidate_id, w.asin, w.product_id, w.sku_id, w.status, w.added_by, w.added_at, w.checks,
                       w.last_checked_at, w.last_in_stock, w.last_detail, w.restocked_at, s.decided_at AS rejected_at,
                       c.sephora_name, c.sephora_url, c.shade, c.amazon_title, c.store_size
                  FROM sephora_stock_watch w
                  JOIN sephora_candidates s ON s.id = w.candidate_id
                  LEFT JOIN sephora_catalog c ON c.asin = w.asin AND c.product_id = w.product_id
                 WHERE {w} ORDER BY w.added_at""")
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
    return {"rows": rows}


async def add_stock_watch(actor: Actor, cid: str) -> dict:
    """Watch the stock of an already-rejected candidate (for rejects made before watches existed)."""
    async with db.pool.connection() as conn:
        async with conn.transaction():
            c = await _candidate(conn, cid)
            if c["status"] != "rejected":
                raise LedgerError("Only a rejected candidate can be watched")
            cur = await conn.execute(
                "INSERT INTO sephora_stock_watch (candidate_id, asin, product_id, sku_id, added_by) "
                "SELECT %s::uuid, %s::text, %s::text, COALESCE(store_sku_id, substring(sephora_url from 'skuId=([0-9]+)')), %s::text "
                "FROM sephora_catalog WHERE asin=%s AND product_id=%s ON CONFLICT (candidate_id) DO NOTHING RETURNING id",
                (cid, c["asin"], c["product_id"], actor.label, c["asin"], c["product_id"]))
            row = await cur.fetchone()
            await _notify(conn)
    return {"added": bool(row), "candidate_id": cid}


def _pick_sku(watch: dict, skus: list[dict]) -> tuple[dict | None, str]:
    """The Sephora SKU a watch is about: its own sku_id, else the one SKU, else a shade match on the Amazon title."""
    if not skus:
        return None, "no SKUs on the page"
    text0 = " ".join(x for x in (watch.get("shade"), watch.get("amazon_title")) if x).lower()
    if any(s.get("shade") for s in skus):   # shade product: the Amazon shade must be one Sephora lists
        want = {int(n) for n in re.findall(r"(?:#|shade\s*|\b0)(\d{1,2}(?:\.5)?)\b", text0) if "." not in n}
        want |= {int(n) for n in re.findall(r"\b0?(\d{1,2})\b", watch.get("shade") or "")}
        if want:
            hits = [s for s in skus if s.get("shade") and {int(n) for n in re.findall(r"\b0?(\d{1,2})\b", s["shade"])} & want]
            if len(hits) == 1:
                return hits[0], f"shade '{hits[0].get('shade')}'"
            if not hits:
                return None, f"Sephora's page doesn't list the Amazon shade ({'/'.join(str(x) for x in sorted(want))}); it lists " + \
                    ", ".join(str(s.get("shade")) for s in skus[:8])
    by_id = {str(s.get("sku_id")): s for s in skus}
    if watch.get("sku_id") and str(watch["sku_id"]) in by_id and len(skus) == 1:
        return by_id[str(watch["sku_id"])], "only SKU"
    if len(skus) == 1:
        return skus[0], "only SKU"
    text = " ".join(x for x in (watch.get("shade"), watch.get("amazon_title"), watch.get("store_size")) if x).lower()
    words = set(re.findall(r"[a-z]{3,}|\d+(?:\.\d+)?", text))
    best, score, tie = None, 0, False
    for s in skus:
        sw = set(re.findall(r"[a-z]{3,}|\d+(?:\.\d+)?", str(s.get("name") or "").lower()))
        n = len(sw & words)
        if n > score:
            best, score, tie = s, n, False
        elif n == score and n:
            tie = True
    if best and not tie:
        return best, f"matched '{best.get('name')}'"
    if watch.get("sku_id") and str(watch["sku_id"]) in by_id:
        return by_id[str(watch["sku_id"])], "the pair's SKU"
    return None, "can't tell which SKU/shade"


async def stock_results(actor: Actor, results: list) -> dict:
    """Results of the Chrome stock check: [{id, skus: [{sku_id, name, in_stock}], error?}].
    Picks the watch's SKU (see _pick_sku); in stock -> status restocked (the daily job may propose the ASIN again)."""
    if not isinstance(results, list) or len(results) > 500:
        raise LedgerError("results must be a list (max 500)")
    done = {"checked": 0, "restocked": 0, "still_out": 0, "unclear": 0}
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for r in results:
                wid = str((r or {}).get("id") or "")
                cur = await conn.execute(
                    "SELECT w.*, c.shade, c.amazon_title, c.store_size, c.extra->'store_sku_alts' AS alts FROM sephora_stock_watch w LEFT JOIN sephora_catalog c "
                    "ON c.asin = w.asin AND c.product_id = w.product_id WHERE w.id::text = %s FOR UPDATE OF w", (wid,))
                w = await cur.fetchone()
                if not w or w["status"] != "watching":
                    continue
                skus = [{"sku_id": str(x.get("sku_id")), "name": _txt(x.get("name"), 120), "shade": _txt(x.get("shade"), 120),
                         "in_stock": x.get("in_stock")}
                        for x in (r.get("skus") or []) if isinstance(x, dict) and x.get("sku_id")]
                if r.get("error"):
                    sku, why, ins = None, _txt(r["error"], 200), None
                else:
                    sku, why = _pick_sku(dict(w), skus)
                    ins = None if sku is None or sku.get("in_stock") is None else bool(sku["in_stock"])
                    alts = {str(x) for x in (w.get("alts") or [])}
                    if ins is not True and alts:   # a same-size alternate SKU in stock counts (Nick, Nov 19, 2026: "use both")
                        alt = next((x for x in skus if x["sku_id"] in alts and x.get("in_stock")), None)
                        if alt:
                            sku, ins, why = alt, True, why + f"; alternate SKU {alt['sku_id']} (same size) in stock"
                detail = f"{why}; SKU {sku['sku_id']} {'in stock' if ins else 'out of stock'}" if sku and ins is not None else why
                await conn.execute(
                    "UPDATE sephora_stock_watch SET checks = checks + 1, last_checked_at = now(), last_in_stock = %s, last_detail = %s, "
                    "sku_id = COALESCE(%s, sku_id), status = CASE WHEN %s THEN 'restocked' ELSE status END, "
                    "restocked_at = CASE WHEN %s THEN now() ELSE restocked_at END WHERE id = %s",
                    (ins, detail[:300], sku["sku_id"] if sku else None, ins is True, ins is True, w["id"]))
                done["checked"] += 1
                done["restocked" if ins else ("still_out" if ins is False else "unclear")] += 1
            await _audit(conn, actor, "stock_check", "sephora", None, None, None, done)
            await _notify(conn)
    return done


async def reject(actor: Actor, cid: str, reason: str | None = None, wrong_match: bool = False, wrong: dict | None = None) -> dict:
    reason = _txt(reason, 300)
    w = clean_wrong(wrong or {}) if wrong_match else None
    async with db.pool.connection() as conn:
        async with conn.transaction():
            c = await _candidate(conn, cid, lock=True)
            if c["status"] != "new":
                raise LedgerError(f"This candidate was already {c['status']}")
            if wrong_match:
                reason = wrong_text(w)
            await conn.execute("UPDATE sephora_candidates SET status='rejected', reject_reason=%s, decided_by=%s, decided_at=now(), "
                               "updated_at=now() WHERE id=%s", (reason, actor.label, cid))
            if not wrong_match and OOS_RE.search(reason or ""):   # watch Sephora stock instead of the 21-day block
                await conn.execute(
                    "INSERT INTO sephora_stock_watch (candidate_id, asin, product_id, sku_id, added_by) "
                    "SELECT %s::uuid, %s::text, %s::text, COALESCE(store_sku_id, substring(sephora_url from 'skuId=([0-9]+)')), %s::text "
                    "FROM sephora_catalog WHERE asin=%s AND product_id=%s ON CONFLICT (candidate_id) DO NOTHING",
                    (cid, c["asin"], c["product_id"], actor.label, c["asin"], c["product_id"]))
            await _audit(conn, actor, "reject", "sephora", cid, None, {"status": "new"},
                         {"status": "rejected", "asin": c["asin"], "reason": reason, "wrong_match": wrong_match})
            await _notify(conn)
    out = {"ok": True, "status": "rejected", "reason": reason}
    if wrong_match:
        out.update(await mark_catalog(actor, c["asin"], c["product_id"], {"match_ok": False, **w, "_source": "candidate"}))
    return out


async def relist_wrong(actor: Actor, body: dict) -> dict:
    """Wrong match from the Sephora relist page: {path, asin, sephora_url, product?, tags, note, correct_asin}.
    Logged in match_feedback; when the pair is in the Sephora catalog it is marked wrong there too (so it is never proposed)."""
    path = _txt(body.get("path"), 200)
    if not path or not path.startswith("relist/"):
        raise LedgerError("path must be a relist list (relist/...)")
    asin = _asin(body.get("asin"))
    w = clean_wrong(body)
    url = _txt(body.get("sephora_url"), 1000)
    m = re.search(r"\b(P\d{4,8})\b", url or "")
    pid = m.group(1) if m else None
    async with db.pool.connection() as conn:
        async with conn.transaction():
            in_cat = False
            if pid:
                cur = await conn.execute("SELECT 1 FROM sephora_catalog WHERE asin=%s AND product_id=%s", (asin, pid))
                in_cat = bool(await cur.fetchone())
            row = await log_wrong(conn, actor, "relist", asin, pid, w, path,
                                  {"sephora_url": url, "sephora_name": _txt(body.get("product"), 300), "brand": _txt(body.get("brand"), 100),
                                   "amazon_title": _txt(body.get("amazon_title"), 500)})
            await _audit(conn, actor, "wrong_match", "relist", None, None, None, {"path": path, "asin": asin, **w})
            await _notify(conn)
    out = {"ok": True, "id": row["id"], "catalog_marked": False}
    if in_cat:
        await mark_catalog(actor, asin, pid, {"match_ok": False, **w, "_no_log": True})
        out["catalog_marked"] = True
    return out


async def relist_unwrong(actor: Actor, fid: str) -> dict:
    """Undo a relist-page wrong match; when that mark also flagged the pair in the Sephora catalog, the catalog flag is cleared."""
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("DELETE FROM match_feedback WHERE id=%s AND source='relist' RETURNING asin, product_id, list_path", (fid,))
            r = await cur.fetchone()
            if not r:
                raise LedgerError("That relist mark doesn't exist")
            await _audit(conn, actor, "wrong_match_undo", "relist", None, None, {"asin": r["asin"], "path": r["list_path"]}, None)
            await _notify(conn)
            cat = None
            if r["product_id"]:
                cur = await conn.execute("SELECT match_ok FROM sephora_catalog WHERE asin=%s AND product_id=%s", (r["asin"], r["product_id"]))
                cat = await cur.fetchone()
    if cat and cat["match_ok"] is False:
        await mark_catalog(actor, r["asin"], r["product_id"], {"match_ok": None})
    return {"ok": True}


_score_lock = asyncio.Lock()


async def score_rows(rows: list) -> dict:
    """Lead Score /preview for up to 25 candidate rows, sequentially. Read-only: nothing is written anywhere."""
    if not L.LEAD_SCORE_URL or not L.LEAD_SCORE_KEY:
        raise LedgerError("Lead Score server isn't configured")
    if not isinstance(rows, list) or len(rows) > 25:
        raise LedgerError("rows must be a list of at most 25")
    out = []
    async with _score_lock:
        async with httpx.AsyncClient(timeout=180) as c:
            for r in rows:
                asin = str(r.get("asin") or "").strip().upper()
                if not ASIN_RE.match(asin) or not r.get("cost") or not r.get("url"):
                    out.append({"asin": asin, "ok": False, "why": "needs asin, cost and url"})
                    continue
                # store_cost=0: the daily job's cost is Sephora's own price, so Lead Score adds no store uplift (that 5% only
                # corrects VA-entered costs that net out Nick's outside cash back; Nick, Nov 23, 2026)
                params = {"asin": asin, "cost": str(r["cost"]), "url": str(r["url"]), "store_cost": "0",
                          "date": str(r.get("date") or datetime.now(CENTRAL).date().isoformat())}
                try:
                    resp = await c.get(f"{L.LEAD_SCORE_URL}/preview", params=params, headers={"X-Key": L.LEAD_SCORE_KEY})
                    s = resp.json()
                except Exception as e:
                    out.append({"asin": asin, "ok": False, "why": str(e)[:200]})
                    continue
                if not s.get("ok"):
                    out.append({"asin": asin, "ok": False, "why": s.get("why") or s.get("error") or "not scored"})
                    continue
                out.append({"asin": asin, "ok": True, "band": s.get("band"), "pred_roi": s.get("pred_roi"),
                            "notes": s.get("notes")})
    return {"rows": out}
