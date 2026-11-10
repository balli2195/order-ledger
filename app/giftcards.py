"""Gift cards page (Nov 10 2026): every gift card Nick bought (first source: Arbitrage Card orders), one row per card.

Loaded through the engine API (POST /api/engine/giftcards, X-Engine-Key) — upsert keyed by (source, card_number), so
re-loading the same orders is safe and never touches the balance / status / notes Nick or a balance check wrote.
Balances are filled in later (manually on the page or by a future balance-check job via the same API).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import db
from .orders import jsonable

STATUSES = ("unknown", "active", "used", "partial", "invalid")
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"), 1)}


def _money(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace("$", "").replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _date(v) -> date | None:
    if not v:
        return None
    s = str(v).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", s)   # "November 9, 2026"
    if m and m.group(1).lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


def store_of(product: str) -> str:
    """'Sephora Gift Card - Cashback - $243.80' -> 'Sephora'; "Kohl's Gift Card" -> "Kohl's"."""
    p = (product or "").replace("’", "'").strip()
    m = re.match(r"(.+?)\s+(e-?gift|gift)\s*card", p, re.I)
    return (m.group(1) if m else p.split(" - ")[0]).strip()


async def upsert(source: str, rows: list[dict]) -> dict:
    source = (source or "arbitragecard").strip().lower()
    clean, bad = [], 0
    for r in rows:
        num = re.sub(r"\s+", "", str(r.get("card_number") or r.get("n") or ""))
        if not num:
            bad += 1
            continue
        product = str(r.get("product") or r.get("p") or "").strip()
        price = _money(r.get("price"))
        cb = _money(r.get("cashback", r.get("cb"))) or Decimal("0.00")
        cost = _money(r.get("cost"))
        if cost is None and price is not None:
            cost = price - cb
        clean.append((source, str(r.get("order_ref") or r.get("o") or "").strip() or None,
                      _date(r.get("order_date") or r.get("dt")), product, r.get("store") or store_of(product),
                      num, str(r.get("pin") or "").strip() or None, _money(r.get("face_value", r.get("face"))),
                      price, cb, cost))
    added = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for i in range(0, len(clean), 500):
                chunk = clean[i:i + 500]
                cur = await conn.execute(
                    """INSERT INTO gift_cards (source, order_ref, order_date, product, store, card_number, pin, face_value, price, cashback, cost)
                       SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[], %s::text[], %s::text[], %s::text[],
                                            %s::numeric[], %s::numeric[], %s::numeric[], %s::numeric[])
                       ON CONFLICT (source, card_number) DO UPDATE SET
                         order_ref = EXCLUDED.order_ref, order_date = EXCLUDED.order_date, product = EXCLUDED.product,
                         store = EXCLUDED.store, pin = COALESCE(EXCLUDED.pin, gift_cards.pin),
                         face_value = COALESCE(EXCLUDED.face_value, gift_cards.face_value),
                         price = COALESCE(EXCLUDED.price, gift_cards.price), cashback = EXCLUDED.cashback,
                         cost = COALESCE(EXCLUDED.cost, gift_cards.cost), updated_at = now()
                       RETURNING (xmax = 0) AS inserted""",
                    [list(col) for col in zip(*chunk)])
                added += sum(1 for x in await cur.fetchall() if x["inserted"])
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "giftcards", "op": "load"}),))
    return {"received": len(rows), "saved": len(clean), "new": added, "updated": len(clean) - added, "skipped": bad}


async def list_all() -> dict:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT id, source, order_ref, order_date, product, store, card_number, pin, face_value, price, cashback, cost,
                      balance, balance_checked_at, status, notes, updated_at
               FROM gift_cards ORDER BY order_date DESC NULLS LAST, order_ref DESC, id""")
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
    return {"cards": rows, "statuses": STATUSES}


async def update(card_id: int, body: dict, who: str) -> dict:
    sets, vals = [], []
    if "balance" in body:
        b = _money(body["balance"])
        sets += ["balance = %s", "balance_checked_at = %s"]
        vals += [b, datetime.now().astimezone() if b is not None else None]
        if "status" not in body and b is not None:
            body["status"] = "used" if b == 0 else "active"
    if "status" in body:
        if body["status"] not in STATUSES:
            raise ValueError("Unknown status")
        sets.append("status = %s"); vals.append(body["status"])
    if "notes" in body:
        sets.append("notes = %s"); vals.append((body["notes"] or "").strip() or None)
    if not sets:
        raise ValueError("Nothing to change")
    sets += ["updated_at = now()", "updated_by = %s"]; vals.append(who)
    async with db.pool.connection() as conn:
        cur = await conn.execute(f"UPDATE gift_cards SET {', '.join(sets)} WHERE id = %s RETURNING *", (*vals, card_id))
        row = await cur.fetchone()
        if not row:
            raise LookupError("No such card")
        await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "giftcards", "op": "update", "id": card_id}),))
    return jsonable(dict(row))


async def engine_balances(source: str, rows: list[dict]) -> dict:
    """For a future balance-check job: [{card_number, balance}] -> sets balance, checked time and status."""
    n = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for r in rows:
                b = _money(r.get("balance"))
                if b is None:
                    continue
                cur = await conn.execute(
                    """UPDATE gift_cards SET balance = %s, balance_checked_at = now(), updated_at = now(), updated_by = 'balance check',
                              status = CASE WHEN %s = 0 THEN 'used' ELSE 'active' END
                       WHERE source = %s AND card_number = %s""",
                    (b, b, (source or "arbitragecard").lower(), re.sub(r"\s+", "", str(r.get("card_number") or ""))))
                n += cur.rowcount
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "giftcards", "op": "balances"}),))
    return {"updated": n}


def jsonable_any(v: Any) -> Any:
    return jsonable(v)
