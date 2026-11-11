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
NICK_FIELDS = {"match_ok", "size_confirmed", "marked_by", "marked_at", "wrong_tags", "wrong_note", "correct_asin"}

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
    return {"upserted": len(clean), "inserted": len(clean) - existing, "updated": existing}


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
            "marked_at, wrong_tags, wrong_note, correct_asin, active, extra, created_at, updated_at")


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


CAND_SQL = """
SELECT s.id, s.asin, s.product_id, s.found_on, s.reason, s.cost, s.price_basis, s.buybox, s.roi_now, s.lead_score_band,
       s.lead_score_pred_roi, s.lead_score_notes, s.note, s.status, s.reject_reason, s.decided_by, s.decided_at, s.lead_id,
       s.created_at, s.updated_at,
       c.store, c.brand, c.sephora_name, c.sephora_url, c.shade, c.amazon_title, c.price_lo, c.price_hi, c.sale_price, c.on_sale,
       c.needs_size, c.size_guess, c.size_confirmed, c.confidence, c.match_reason, c.sellable, c.fba_low, c.bb_avg90,
       c.monthly_sold, c.offers_fba, c.amazon_on, c.roi_avg90, c.watch_price, c.match_ok, c.wrong_tags, c.wrong_note, c.correct_asin,
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
    if f.get("sellable"):
        w.append("sellable = %s"); a.append(f["sellable"])
    if f.get("needs_size") in ("1", "true"):
        w.append("needs_size")
    if f.get("needs_size") == "open":
        w.append("needs_size AND size_confirmed IS NULL")
    m = f.get("match")
    if m == "ok":
        w.append("match_ok")
    elif m == "wrong":
        w.append("match_ok IS FALSE")
    elif m == "unchecked":
        w.append("match_ok IS NULL")
    act = f.get("active", "1")
    if act in ("1", "true"):
        w.append("active")
    elif act in ("0", "false"):
        w.append("NOT active")
    where = ("WHERE " + " AND ".join(w)) if w else ""
    limit = max(1, min(int(limit), 1000)); offset = max(0, int(offset))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {CAT_COLS}, (SELECT count(*) FROM sephora_candidates s WHERE s.asin = c.asin AND s.product_id = c.product_id "
            f"AND s.status = 'new') AS open_candidates FROM sephora_catalog c {where} "
            f"ORDER BY lower(brand), sephora_name, asin LIMIT %s OFFSET %s", a + [limit, offset])
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
        total = (await (await conn.execute(f"SELECT count(*) n FROM sephora_catalog c {where}", a)).fetchone())["n"]
        # per-brand counts over everything active (not just the filtered rows), for the brand list
        cur = await conn.execute(
            "SELECT brand, count(*) n, count(*) FILTER (WHERE match_ok IS NULL) unchecked, count(*) FILTER (WHERE match_ok) ok, "
            "count(*) FILTER (WHERE match_ok IS FALSE) wrong FROM sephora_catalog WHERE active GROUP BY brand ORDER BY count(*) DESC, brand")
        brands = [jsonable(dict(r)) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT store, count(*) n FROM sephora_catalog WHERE active GROUP BY store ORDER BY count(*) DESC")
        stores = [jsonable(dict(r)) for r in await cur.fetchall()]
    return {"rows": rows, "total": total, "offset": offset, "limit": limit, "brands": brands, "stores": stores}


# ---------------------------------------------------------------- Nick's actions

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
            cur = await conn.execute("SELECT match_ok, size_confirmed FROM sephora_catalog WHERE asin=%s AND product_id=%s FOR UPDATE",
                                     (asin, product_id))
            before = await cur.fetchone()
            if not before:
                raise LedgerError("That catalog pair doesn't exist")
            await conn.execute(f"UPDATE sephora_catalog SET {', '.join(sets)}, marked_by=%s, marked_at=now(), updated_at=now() "
                               f"WHERE asin=%s AND product_id=%s", args + [actor.label, asin, product_id])
            expired = 0
            if "match_ok" in after and after["match_ok"] is not False:   # changed his mind: stop learning from the old mark
                await conn.execute("DELETE FROM match_feedback WHERE asin=%s AND product_id=%s", (asin, product_id))
            if after.get("match_ok") is False:   # a wrong match: its open candidates go too
                cur = await conn.execute(
                    "UPDATE sephora_candidates SET status='rejected', reject_reason=COALESCE(reject_reason, %s), "
                    "decided_by=%s, decided_at=now(), updated_at=now() WHERE asin=%s AND product_id=%s AND status='new' RETURNING id",
                    (wrong_text(wrong), actor.label, asin, product_id))
                expired = len(await cur.fetchall())
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
        if c.get("needs_size") and c.get("size_confirmed") not in ("lo", "hi"):
            raise LedgerError("Pick the size first (Mini / smaller size or Full size)")
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
        if c.get("needs_size"):
            bits.append("full size" if c["size_confirmed"] == "hi" else "mini / smaller size")
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
                params = {"asin": asin, "cost": str(r["cost"]), "url": str(r["url"]),
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
