"""Leads, stores and brands. Mirrors Airtable until cutover; afterwards this is the home."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date
from typing import Any, Iterable

import httpx

from . import db
from .orders import Actor, LedgerError, _audit, _date, _money, _text, jsonable

log = logging.getLogger("ledger.leads")

LEAD_FIELDS = (
    "asin", "lead_date", "product_name", "sourced_by", "stores", "brands", "store_link", "cost",
    "promo_code", "va_notes", "feedback", "buy_qty", "qty_ordered", "buyer_notes",
    "lead_score_band", "lead_score_roi", "lead_score_notes", "claude_buy_qty", "claude_buy_note", "replen",
)
INT_FIELDS = {"buy_qty", "qty_ordered", "lead_score_band", "claude_buy_qty"}
LIST_FIELDS = {"stores", "brands"}

KEEPA_KEY = os.environ.get("KEEPA_KEY", "")
LEAD_SCORE_URL = os.environ.get("LEAD_SCORE_URL", "").rstrip("/")
LEAD_SCORE_KEY = os.environ.get("LEAD_SCORE_KEY", "")

SORTS = {
    "date": "l.lead_date {d} NULLS LAST, l.created_at {d}",
    "cost": "l.cost {d} NULLS LAST", "buy_qty": "l.buy_qty {d} NULLS LAST", "qty_ordered": "l.qty_ordered {d} NULLS LAST",
    "left": "l.left_to_buy {d} NULLS LAST", "band": "l.lead_score_band {d} NULLS LAST", "roi": "l.lead_score_roi {d} NULLS LAST",
    "claude_qty": "l.claude_buy_qty {d} NULLS LAST", "spend": "l.actual_spend {d} NULLS LAST", "asin": "l.asin {d}",
    "updated": "l.updated_at {d}",
}

LIST_COLS = (
    "l.id, l.airtable_id, l.asin, l.lead_date, l.product_name, l.sourced_by, l.stores, l.brands, l.store_link, l.cost, "
    "l.promo_code, l.va_notes, l.feedback, l.buy_qty, l.qty_ordered, l.buyer_notes, l.lead_score_band, l.lead_score_roi, "
    "l.claude_buy_qty, l.replen, l.expected_spend, l.actual_spend, l.left_to_buy, l.updated_at, l.created_at"
)


async def keepa_title(asin: str) -> str | None:
    """Amazon product title for an ASIN from Keepa (about 1 token). None if unavailable."""
    if not KEEPA_KEY or not asin:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://api.keepa.com/product",
                            params={"key": KEEPA_KEY, "domain": 1, "asin": asin, "history": 0})
        prods = r.json().get("products") or []
        title = (prods[0].get("title") or "").strip() if prods else ""
        return title or None
    except Exception as e:
        log.warning("keepa title lookup failed for %s: %s", asin, e)
        return None


# ---------------------------------------------------------------- settings

async def get_setting(key: str, default: Any = None) -> Any:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT value FROM settings WHERE key=%s", (key,))
        r = await cur.fetchone()
    return r["value"] if r else default


async def mode() -> str:
    return await get_setting("leads_mode", "mirror")


async def require_home() -> None:
    if await mode() != "home":
        raise LedgerError(
            "Stores and brands are still copied from Airtable; change them there until leads are switched over."
        )


async def bonus_params() -> tuple[float, str]:
    return float(await get_setting("bonus_rate", 0.012)), str(await get_setting("bonus_sourcer", "Ariane"))


def _bonus_sql(rate: float, who: str) -> tuple[str, list]:
    return ("ROUND(COALESCE(l.expected_spend,0) * %s::numeric, 2) AS potential_bonus, "
            "CASE WHEN l.sourced_by = %s THEN ROUND(COALESCE(l.actual_spend,0) * %s::numeric, 2) ELSE 0 END AS bonus"), [rate, who, rate]


# ---------------------------------------------------------------- cleaning

def _int(v: Any, field: str) -> int | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return int(round(float(str(v).replace(",", ""))))
    except ValueError:
        raise LedgerError(f"{field} must be a whole number (got {v!r})")


def _names(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[;,]", v)]
    return sorted({str(x).strip() for x in v if str(x).strip()})


def clean_lead(data: dict) -> dict:
    out: dict[str, Any] = {}
    for k in LEAD_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "asin":
            s = _text(v)
            out[k] = s.upper() if s else None
        elif k == "lead_date":
            out[k] = _date(v) if v not in (None, "") else None
        elif k == "cost":
            out[k] = _money(v, "cost")
        elif k == "lead_score_roi":
            out[k] = None if v in (None, "") else round(float(v), 4)
        elif k in INT_FIELDS:
            out[k] = _int(v, k)
        elif k in LIST_FIELDS:
            out[k] = _names(v)
        elif k == "replen":
            out[k] = bool(v) and str(v).lower() not in ("false", "0", "no")
        else:
            out[k] = _text(v)
    return out


# ---------------------------------------------------------------- reads

def _where(f: dict) -> tuple[list[str], list]:
    w, a = [], []
    if f.get("q"):
        q = f"%{str(f['q']).strip()}%"
        w.append("(l.asin ILIKE %s OR l.product_name ILIKE %s OR array_to_string(l.stores,' ') ILIKE %s OR "
                 "array_to_string(l.brands,' ') ILIKE %s OR l.va_notes ILIKE %s OR l.buyer_notes ILIKE %s OR "
                 "l.promo_code ILIKE %s OR l.store_link ILIKE %s)")
        a += [q] * 8
    if f.get("asin"):
        w.append("upper(l.asin) = upper(%s)"); a.append(str(f["asin"]).strip())
    if f.get("store"):
        w.append("EXISTS (SELECT 1 FROM unnest(l.stores) s WHERE s ILIKE %s)"); a.append(f"%{f['store'].strip()}%")
    if f.get("brand"):
        w.append("EXISTS (SELECT 1 FROM unnest(l.brands) b WHERE b ILIKE %s)"); a.append(f"%{f['brand'].strip()}%")
    if f.get("date_from"):
        w.append("l.lead_date >= %s"); a.append(_date(f["date_from"]))
    if f.get("date_to"):
        w.append("l.lead_date <= %s"); a.append(_date(f["date_to"]))
    if f.get("sourced_by"):
        w.append("l.sourced_by = %s"); a.append(f["sourced_by"])
    if f.get("ordered") in (True, "1", "true", "yes"):
        w.append("COALESCE(l.qty_ordered,0) > 0")
    if f.get("left") in (True, "1", "true", "yes"):
        w.append("COALESCE(l.left_to_buy,0) > 0")
    if f.get("unscored") in (True, "1", "true", "yes"):
        w.append("l.lead_score_band IS NULL")
    if f.get("band_min") not in (None, ""):
        w.append("l.lead_score_band >= %s"); a.append(int(f["band_min"]))
    if f.get("replen") in (True, "1", "true", "yes"):
        w.append("l.replen")
    return w, a


async def list_leads(filters: dict, sort: str = "date", desc: bool = True, limit: int = 200, offset: int = 0) -> dict:
    w, a = _where(filters)
    where = ("WHERE " + " AND ".join(w)) if w else ""
    order = SORTS.get(sort, SORTS["date"]).format(d="DESC" if desc else "ASC")
    if sort != "date":
        # Leads are shown grouped by day: keep days together (newest first) and sort within each day.
        order = "l.lead_date DESC NULLS LAST, " + order
    rate, who = await bonus_params()
    bsql, bargs = _bonus_sql(rate, who)
    limit = max(1, min(int(limit), 1000))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {LIST_COLS}, {bsql} FROM leads l {where} ORDER BY {order} LIMIT %s OFFSET %s",
            bargs + a + [limit, int(offset)],
        )
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
        cur = await conn.execute(
            f"SELECT count(*) n, COALESCE(sum(l.expected_spend),0) expected, COALESCE(sum(l.actual_spend),0) actual, "
            f"COALESCE(sum(l.qty_ordered),0) units, "
            f"ROUND(COALESCE(sum(CASE WHEN l.sourced_by=%s THEN l.actual_spend ELSE 0 END),0) * %s::numeric, 2) bonus, "
            f"count(*) FILTER (WHERE COALESCE(l.qty_ordered,0)>0) bought "
            f"FROM leads l {where}",
            [who, rate] + a,
        )
        totals = jsonable(dict(await cur.fetchone()))
        days = sorted({r["lead_date"] for r in rows if r.get("lead_date")})
        day_totals = {}
        if days:
            w2 = w + ["l.lead_date = ANY(%s::date[])"]
            cur = await conn.execute(
                f"SELECT l.lead_date, count(*) n, count(*) FILTER (WHERE COALESCE(l.qty_ordered,0)>0) bought, "
                f"COALESCE(sum(l.qty_ordered),0) units, COALESCE(sum(l.actual_spend),0) actual, "
                f"COALESCE(sum(l.expected_spend),0) expected FROM leads l WHERE {' AND '.join(w2)} GROUP BY l.lead_date",
                a + [days],
            )
            day_totals = {str(r["lead_date"]): jsonable({k: v for k, v in r.items() if k != "lead_date"}) for r in await cur.fetchall()}
    return {"leads": rows, "totals": totals, "days": day_totals, "offset": offset, "limit": limit}


async def get_lead(lead_id: str | None = None, airtable_id: str | None = None) -> dict | None:
    rate, who = await bonus_params()
    bsql, bargs = _bonus_sql(rate, who)
    key, val = ("l.airtable_id", airtable_id) if airtable_id else ("l.id", lead_id)
    async with db.pool.connection() as conn:
        try:
            cur = await conn.execute(f"SELECT l.*, {bsql} FROM leads l WHERE {key}=%s", bargs + [val])
        except Exception as e:
            if "uuid" in str(e).lower():
                raise LedgerError(f"{val!r} isn't a valid lead id")
            raise
        r = await cur.fetchone()
        if not r:
            return None
        lead = jsonable(dict(r))
        # Other leads of the same ASIN (history), newest first.
        if lead.get("asin"):
            cur = await conn.execute(
                "SELECT id, lead_date, stores, cost, buy_qty, qty_ordered, sourced_by, buyer_notes FROM leads "
                "WHERE upper(asin)=upper(%s) AND id<>%s ORDER BY lead_date DESC NULLS LAST LIMIT 30",
                (lead["asin"], r["id"]),
            )
            lead["same_asin"] = [jsonable(dict(x)) for x in await cur.fetchall()]
    return lead


async def lead_choices() -> dict:
    async with db.pool.connection() as conn:
        out = {}
        cur = await conn.execute("SELECT name, can_source FROM stores ORDER BY name")
        out["stores"] = [jsonable(dict(r)) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT name, can_source FROM brands ORDER BY name")
        out["brands"] = [jsonable(dict(r)) for r in await cur.fetchall()]
    out["sourcers"] = await get_setting("sourcers", ["Ariane", "Nick"])
    out["mode"] = await mode()
    out["bonus_rate"], out["bonus_sourcer"] = await bonus_params()
    out["last_sync"] = await get_setting("leads_last_sync", None)
    return out


async def store_stats(kind: str = "stores", q: str | None = None) -> list[dict]:
    if kind not in ("stores", "brands"):
        raise LedgerError("kind must be stores or brands")
    col = kind
    args: list = []
    where = ""
    if q:
        where = "WHERE t.name ILIKE %s"; args.append(f"%{q}%")
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT t.name, t.can_source, COALESCE(s.leads,0) leads, COALESCE(s.spend,0) spend,
                       COALESCE(s.units,0) units, s.last_date,
                       CASE WHEN s.last_date IS NULL THEN NULL ELSE (current_date - s.last_date) END AS days_since
                FROM {kind} t
                LEFT JOIN (SELECT x AS name, count(*) leads, sum(actual_spend) spend, sum(qty_ordered) units,
                                  max(lead_date) last_date
                           FROM leads, unnest({col}) x GROUP BY x) s ON s.name = t.name
                {where} ORDER BY COALESCE(s.spend,0) DESC, t.name""",
            args,
        )
        return [jsonable(dict(r)) for r in await cur.fetchall()]


# ---------------------------------------------------------------- writes (home mode only)

async def _notify(conn, op: str, ids: Iterable, by: str | None, src: str) -> None:
    ids = [str(i) for i in ids]
    payload = {"t": "lead", "op": op, "by": by, "src": src}
    if len(ids) <= 50:
        payload["ids"] = ids
    else:
        payload["op"] = "reload"
    await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps(payload),))


async def _ensure_names(conn, kind: str, names: list[str]) -> None:
    for n in names:
        await conn.execute(f"INSERT INTO {kind} (name) VALUES (%s) ON CONFLICT DO NOTHING", (n,))


async def create_lead(actor: Actor, data: dict) -> dict:
    f = clean_lead(data)
    if not f.get("asin") and not f.get("store_link"):
        raise LedgerError("A lead needs at least an ASIN or a store link")
    if f.get("asin") and not re.fullmatch(r"[A-Z0-9]{10}", f["asin"]):
        raise LedgerError(f"ASIN should be 10 letters/digits (got {f['asin']!r})")
    f.setdefault("lead_date", date.today())
    if f.get("asin") and not f.get("product_name"):
        f["product_name"] = await keepa_title(f["asin"])
        if not f["product_name"]:
            f.pop("product_name")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await _ensure_names(conn, "stores", f.get("stores", []))
            await _ensure_names(conn, "brands", f.get("brands", []))
            cols = list(f) + ["created_by", "updated_by"]
            cur = await conn.execute(
                f"INSERT INTO leads ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
                list(f.values()) + [actor.user_id, actor.user_id],
            )
            row = await cur.fetchone()
            await _audit(conn, actor, "create", "lead", row["id"], None, None, {k: row[k] for k in f})
            await _notify(conn, "upsert", [row["id"]], actor.label, actor.source)
    maybe_score(row["id"], row)
    return await get_lead(str(row["id"]))


async def update_leads(actor: Actor, changes: list[dict]) -> list[str]:
    mirror = await mode() != "home"
    touched = []
    rescore = []
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for ch in changes:
                lid = ch.get("id")
                f = clean_lead({k: v for k, v in ch.items() if k != "id"})
                if not lid or not f:
                    raise LedgerError("Each change needs a lead id and at least one field")
                cur = await conn.execute("SELECT * FROM leads WHERE id=%s FOR UPDATE", (lid,))
                before = await cur.fetchone()
                if not before:
                    raise LedgerError(f"Lead {lid} not found")
                await _ensure_names(conn, "stores", f.get("stores", []))
                await _ensure_names(conn, "brands", f.get("brands", []))
                sets = ", ".join(f"{k}=%s" for k in f)
                extra_set = ""
                extra_args: list = []
                if mirror and before["airtable_id"]:
                    extra_set = ", local_edits=(SELECT array_agg(DISTINCT x) FROM unnest(local_edits || %s::text[]) x)"
                    extra_args = [list(f)]
                cur = await conn.execute(
                    f"UPDATE leads SET {sets}, updated_at=now(), updated_by=%s{extra_set} WHERE id=%s RETURNING *",
                    list(f.values()) + [actor.user_id] + extra_args + [lid],
                )
                after = await cur.fetchone()
                await _audit(conn, actor, "update", "lead", lid, None,
                             {k: before[k] for k in f}, {k: after[k] for k in f})
                touched.append(str(lid))
                if {"asin", "cost", "store_link"} & set(f) and "lead_score_band" not in f:
                    rescore.append(after)
            await _notify(conn, "upsert", touched, actor.label, actor.source)
    for r in rescore:
        maybe_score(r["id"], r, force=True)
    return touched


async def delete_leads(actor: Actor, ids: list[str]) -> int:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            n = 0
            for lid in ids:
                cur = await conn.execute("DELETE FROM leads WHERE id=%s RETURNING *", (lid,))
                before = await cur.fetchone()
                if not before:
                    raise LedgerError(f"Lead {lid} not found")
                if before["airtable_id"]:
                    # Airtable still has it; remember the deletion so the copy doesn't bring it back.
                    await conn.execute(
                        "INSERT INTO lead_tombstones (airtable_id, user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (before["airtable_id"], actor.user_id))
                await _audit(conn, actor, "delete", "lead", lid, None, dict(before), None)
                n += 1
            await _notify(conn, "delete", ids, actor.label, actor.source)
    return n


async def set_can_source(actor: Actor, kind: str, name: str, value: bool) -> None:
    await require_home()
    if kind not in ("stores", "brands"):
        raise LedgerError("kind must be stores or brands")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                f"UPDATE {kind} SET can_source=%s, updated_at=now() WHERE name=%s RETURNING name", (bool(value), name))
            if not await cur.fetchone():
                raise LedgerError(f"No {kind[:-1]} named {name!r}")
            await _audit(conn, actor, "update", kind[:-1], None, None, None, {"name": name, "can_source": bool(value)})
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "store", "op": "reload"}),))


async def make_home(actor: Actor) -> None:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE settings SET value='\"home\"' WHERE key='leads_mode'")
            await _audit(conn, actor, "update", "setting", None, None, {"leads_mode": "mirror"}, {"leads_mode": "home"})
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "lead", "op": "mode"}),))


# ---------------------------------------------------------------- Lead Score

_score_tasks: set[asyncio.Task] = set()


def maybe_score(lead_id, row: dict, force: bool = False) -> None:
    """Score in the background when the lead has an ASIN, cost and store link (and no band yet, unless forced)."""
    if not LEAD_SCORE_URL or not LEAD_SCORE_KEY:
        return
    if not (row.get("asin") and row.get("cost") and row.get("store_link")):
        return
    if row.get("lead_score_band") is not None and not force:
        return
    t = asyncio.create_task(score_lead(str(lead_id)))
    _score_tasks.add(t)
    t.add_done_callback(_score_tasks.discard)


async def score_lead(lead_id: str) -> dict:
    """Ask the Lead Score server (read-only /preview) and save band, predicted ROI and notes on the lead."""
    if not LEAD_SCORE_URL or not LEAD_SCORE_KEY:
        raise LedgerError("Lead Score server isn't configured")
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT asin, cost, store_link, lead_date FROM leads WHERE id=%s", (lead_id,))
        r = await cur.fetchone()
    if not r:
        raise LedgerError("Lead not found")
    if not (r["asin"] and r["cost"] and r["store_link"]):
        raise LedgerError("Scoring needs an ASIN, a cost and a store link")
    params = {"asin": r["asin"], "cost": str(r["cost"]), "url": r["store_link"],
              "date": (r["lead_date"] or date.today()).isoformat(), "lead_id": str(lead_id)}
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            resp = await c.get(f"{LEAD_SCORE_URL}/preview", params=params, headers={"X-Key": LEAD_SCORE_KEY})
        s = resp.json()
    except Exception as e:
        log.warning("lead score failed for %s: %s", lead_id, e)
        return {"ok": False, "why": str(e)}
    if not s.get("ok"):
        return {"ok": False, "why": s.get("why") or s.get("error") or "not scored"}
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE leads SET lead_score_band=%s, lead_score_roi=%s, lead_score_notes=%s, updated_at=now(), "
                "local_edits=CASE WHEN airtable_id IS NULL THEN local_edits ELSE (SELECT array_agg(DISTINCT x) FROM "
                "unnest(local_edits || ARRAY['lead_score_band','lead_score_roi','lead_score_notes']) x) END WHERE id=%s",
                (s.get("band"), round(float(s.get("pred_roi") or 0), 4), s.get("notes"), lead_id),
            )
            await _notify(conn, "upsert", [lead_id], "Lead Score", "api")
    return {"ok": True, "band": s.get("band"), "pred_roi": s.get("pred_roi")}


# ---------------------------------------------------------------- engine API (lead scoring / buy qty jobs)

ENGINE_FIELDS = {"claude_buy_qty": "claude_buy_qty", "claude_buy_note": "claude_buy_note",
                 "lead_score_band": "lead_score_band", "lead_score_pred_roi": "lead_score_roi",
                 "lead_score_roi": "lead_score_roi", "lead_score_notes": "lead_score_notes"}
ENGINE_ACTOR = Actor(None, "Claude engine", "engine")


async def engine_leads(since: str | None = None, asin: str | None = None, limit: int = 2000, offset: int = 0) -> dict:
    """Leads for the scoring / buy qty jobs, oldest first, paginated."""
    w, a = [], []
    if since:
        w.append("l.lead_date >= %s"); a.append(_date(since))
    if asin:
        w.append("upper(l.asin) = upper(%s)"); a.append(asin.strip())
    where = ("WHERE " + " AND ".join(w)) if w else ""
    limit = max(1, min(int(limit), 5000)); offset = max(0, int(offset))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT l.id, l.lead_date AS date, l.created_at, l.asin, l.cost,
                       array_to_string(l.stores, '; ') AS store, l.stores, l.store_link,
                       array_to_string(l.brands, '; ') AS brand, l.brands, l.qty_ordered, l.buy_qty,
                       l.lead_score_band, l.lead_score_roi AS lead_score_pred_roi, l.claude_buy_qty
                FROM leads l {where}
                ORDER BY l.lead_date ASC NULLS FIRST, l.created_at ASC, l.id
                LIMIT %s OFFSET %s""",
            a + [limit + 1, offset],
        )
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
        cur = await conn.execute(f"SELECT count(*) n FROM leads l {where}", a)
        total = (await cur.fetchone())["n"]
    more = len(rows) > limit
    rows = rows[:limit]
    return {"leads": rows, "total": total, "offset": offset, "limit": limit,
            "next_offset": offset + limit if more else None}


async def engine_update(updates: list[dict]) -> dict:
    """Write scoring / buy qty results. Only the engine fields are accepted; audited as source 'engine'."""
    if not isinstance(updates, list) or not updates:
        raise LedgerError("updates must be a non-empty list")
    if len(updates) > 200:
        raise LedgerError("At most 200 updates per call")
    changes = []
    for u in updates:
        if not isinstance(u, dict) or not u.get("id"):
            raise LedgerError("Each update needs an id")
        bad = set(u) - set(ENGINE_FIELDS) - {"id"}
        if bad:
            raise LedgerError(f"Fields not allowed here: {', '.join(sorted(bad))}")
        ch = {"id": u["id"]}
        for k, col in ENGINE_FIELDS.items():
            if k in u:
                ch[col] = u[k]
        if len(ch) == 1:
            raise LedgerError(f"Update for {u['id']} has no fields")
        changes.append(ch)
    touched = await update_leads(ENGINE_ACTOR, changes)
    return {"updated": len(touched), "ids": touched}
