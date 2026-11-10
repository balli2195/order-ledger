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
        disc = _money(r.get("discount_pct", r.get("disc")))
        face = _money(r.get("face_value", r.get("face")))
        if price is None and face is not None and disc is not None:
            price = (face * (1 - disc / 100)).quantize(Decimal("0.01"))
        if cost is None and price is not None:
            cost = price - cb
        clean.append((source, str(r.get("order_ref") or r.get("o") or "").strip() or None,
                      _date(r.get("order_date") or r.get("dt")), product, (str(r.get("store") or "").strip() or store_of(product)),
                      num, str(r.get("pin") or "").strip() or None, face,
                      price, cb, cost, disc, _money(r.get("source_balance")), (str(r.get("source_status") or "").strip() or None)))
    added = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for i in range(0, len(clean), 500):
                chunk = clean[i:i + 500]
                cur = await conn.execute(
                    """INSERT INTO gift_cards (source, order_ref, order_date, product, store, card_number, pin, face_value, price, cashback, cost,
                                               discount_pct, source_balance, source_status)
                       SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[], %s::text[], %s::text[], %s::text[],
                                            %s::numeric[], %s::numeric[], %s::numeric[], %s::numeric[], %s::numeric[], %s::numeric[], %s::text[])
                       ON CONFLICT (source, card_number) DO UPDATE SET
                         order_ref = COALESCE(EXCLUDED.order_ref, gift_cards.order_ref),
                         order_date = COALESCE(EXCLUDED.order_date, gift_cards.order_date),
                         product = COALESCE(NULLIF(EXCLUDED.product, ''), gift_cards.product),
                         store = COALESCE(NULLIF(EXCLUDED.store, ''), gift_cards.store),
                         discount_pct = COALESCE(EXCLUDED.discount_pct, gift_cards.discount_pct),
                         source_balance = EXCLUDED.source_balance, source_status = COALESCE(EXCLUDED.source_status, gift_cards.source_status), pin = COALESCE(EXCLUDED.pin, gift_cards.pin),
                         face_value = COALESCE(EXCLUDED.face_value, gift_cards.face_value),
                         price = COALESCE(EXCLUDED.price, gift_cards.price), cashback = GREATEST(EXCLUDED.cashback, gift_cards.cashback),
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
                      discount_pct, source_balance, source_status, balance, balance_checked_at, status, notes, updated_at,
                      used_total, used_count, last_used, used_match
               FROM gift_cards
               ORDER BY order_date DESC NULLS LAST, CASE WHEN order_ref ~ '^[0-9]+$' THEN order_ref::bigint END DESC NULLS LAST, id""")
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




# ---------------------------------------------------------------- what each card paid for (store order emails)

def _l4(num: str) -> str:
    d = re.sub(r"\D", "", num or "")
    return d[-4:] if len(d) >= 4 else ""


async def add_uses(rows: list[dict]) -> dict:
    """rows: [{store, o|order_ref, dt|order_date, l4|last4, a|amount, m|method?, mb|mailbox?}] -> gift_card_uses, then re-match."""
    clean, bad = [], 0
    for r in rows:
        store = str(r.get("store") or "").replace("’", "'").strip()
        o = str(r.get("o") or r.get("order_ref") or "").strip()
        l4 = re.sub(r"\D", "", str(r.get("l4") or r.get("last4") or ""))[-4:]
        a = _money(r.get("a", r.get("amount")))
        if not (store and o and len(l4) == 4 and a is not None):
            bad += 1
            continue
        clean.append((store, o, _date(r.get("dt") or r.get("order_date")), l4,
                      (str(r.get("m") or r.get("method") or "gift card").strip().lower()), a, (str(r.get("mb") or r.get("mailbox") or "").strip() or None)))
    new = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for i in range(0, len(clean), 500):
                cur = await conn.execute(
                    """INSERT INTO gift_card_uses (store, order_ref, order_date, last4, method, amount, mailbox)
                       SELECT * FROM unnest(%s::text[], %s::text[], %s::date[], %s::text[], %s::text[], %s::numeric[], %s::text[])
                       ON CONFLICT (store, order_ref, last4, amount) DO NOTHING RETURNING id""",
                    [list(col) for col in zip(*clean[i:i + 500])])
                new += len(await cur.fetchall())
    m = await rematch()
    return {"received": len(rows), "saved": len(clean), "new": new, "skipped": bad, **m}


async def rematch() -> dict:
    """Roll gift-card uses up onto cards with the same store + last 4 digits.
    used_match: 'exact' (one card has those digits), 'shared' (several cards share them — total shown on each),
    'over' (more was spent than the card's face value: likely another card with the same last 4 bought elsewhere)."""
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE gift_cards SET used_total = NULL, used_count = NULL, last_used = NULL, used_match = NULL WHERE used_total IS NOT NULL OR used_match IS NOT NULL")
            await conn.execute(
                r"""WITH c AS (SELECT id, store, face_value, right(regexp_replace(card_number, '\D', '', 'g'), 4) l4 FROM gift_cards),
                        n AS (SELECT store, l4, count(*) k FROM c GROUP BY 1, 2),
                        u AS (SELECT store, last4 l4, sum(amount) amt, count(*) k, max(order_date) last FROM gift_card_uses
                              WHERE method = 'gift card' GROUP BY 1, 2)
                   UPDATE gift_cards g SET used_total = u.amt, used_count = u.k, last_used = u.last,
                          used_match = CASE WHEN n.k > 1 THEN 'shared' WHEN u.amt > c.face_value + 0.005 THEN 'over' ELSE 'exact' END
                   FROM c JOIN n ON n.store = c.store AND n.l4 = c.l4 JOIN u ON u.store = c.store AND u.l4 = c.l4
                   WHERE g.id = c.id""")
            cur = await conn.execute(
                """SELECT count(*) FILTER (WHERE used_match = 'exact') exact, count(*) FILTER (WHERE used_match = 'shared') shared,
                          count(*) FILTER (WHERE used_match = 'over') over_face FROM gift_cards""")
            stats = dict(await cur.fetchone())
            cur = await conn.execute(
                r"""SELECT count(*) uses, count(DISTINCT u.store || u.last4) cards, coalesce(sum(amount), 0) amt FROM gift_card_uses u
                   WHERE method = 'gift card' AND NOT EXISTS (SELECT 1 FROM gift_cards g WHERE g.store = u.store
                         AND right(regexp_replace(g.card_number, '\D', '', 'g'), 4) = u.last4)""")
            stats["unmatched"] = jsonable(dict(await cur.fetchone()))
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "giftcards", "op": "uses"}),))
    return stats


# ---------------------------------------------------------------- store balance checks (Macy's script, Nov 10 2026)
# A script Nick pastes into his own macys.com balance tab asks the Hub for cards to check (number + PIN),
# fills Macy's own form one card at a time, and posts each balance back. Its key (settings 'gc_check_key')
# only opens these two endpoints, so the script never carries the full engine key.

CHECK_STORES = {"macys": ("Macy's", "Macy's Online Store")}


async def check_key() -> str:
    import secrets
    async with db.pool.connection() as conn:
        r = await (await conn.execute("SELECT value #>> '{}' AS v FROM settings WHERE key = 'gc_check_key'")).fetchone()
        if r and r["v"]:
            return r["v"]
        k = secrets.token_urlsafe(24)
        await conn.execute("INSERT INTO settings (key, value) VALUES ('gc_check_key', to_jsonb(%s::text)) ON CONFLICT (key) DO NOTHING", (k,))
        r = await (await conn.execute("SELECT value #>> '{}' AS v FROM settings WHERE key = 'gc_check_key'")).fetchone()
        return r["v"]


async def to_check(store: str, limit: int = 50, recheck_days: int = 7, include_zero: bool = False) -> list[dict]:
    """Cards to check next: never checked (or checked more than recheck_days ago), not marked used/invalid,
    skipping cards Arbitrage Card already shows at $0 unless include_zero. Oldest card first (purchase date, then
    Arbitrage Card order number, which rises over time — most export cards have no date)."""
    names = CHECK_STORES.get(store)
    if not names:
        raise ValueError("Unknown store")
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            r"""SELECT id, regexp_replace(card_number, '\D', '', 'g') AS n, pin, face_value AS face, source_balance AS ac
               FROM gift_cards
               WHERE store = ANY(%s) AND pin IS NOT NULL
                 AND status NOT IN ('used', 'invalid')
                 AND (balance_checked_at IS NULL OR balance_checked_at < now() - make_interval(days => %s))
                 AND (%s OR source_balance IS NULL OR source_balance > 0)
               ORDER BY order_date NULLS LAST, CASE WHEN order_ref ~ '^[0-9]+$' THEN order_ref::bigint END NULLS LAST, id
               LIMIT %s""",
            (list(names), recheck_days, include_zero, max(1, min(limit, 500))))
        return [jsonable(dict(r)) for r in await cur.fetchall()]


async def check_results(rows: list[dict], who: str = "balance check") -> dict:
    """[{id, balance} | {id, error}] -> balance + checked time + status (0 used, below face partial, else active);
    an error (card refused by the store) marks the card invalid with the store's message in notes."""
    n = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for r in rows:
                try:
                    cid = int(r.get("id"))
                except (TypeError, ValueError):
                    continue
                if r.get("error"):
                    cur = await conn.execute(
                        """UPDATE gift_cards SET status = 'invalid', updated_at = now(), updated_by = %s,
                                  notes = left(concat_ws(' | ', nullif(notes, ''), %s), 500)
                           WHERE id = %s""", (who, "Balance check: " + str(r["error"])[:200], cid))
                    n += cur.rowcount
                    continue
                b = _money(r.get("balance"))
                if b is None:
                    continue
                cur = await conn.execute(
                    """UPDATE gift_cards SET balance = %s, balance_checked_at = now(), updated_at = now(), updated_by = %s,
                              status = CASE WHEN %s <= 0 THEN 'used' WHEN %s < face_value THEN 'partial' ELSE 'active' END
                       WHERE id = %s""", (b, who, b, b, cid))
                n += cur.rowcount
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "giftcards", "op": "balances"}),))
    return {"updated": n}


async def set_order_dates(source: str, rows: list[dict]) -> dict:
    """[{o: order number, dt: 'November 10, 2026' | ISO}] -> order_date on every card from that order (Nov 10 2026:
    dates read from Arbitrage Card's "order is now complete" emails; the card export has no dates)."""
    pairs = {}
    for r in rows:
        o, d = str(r.get("o") or r.get("order_ref") or "").strip(), _date(r.get("dt") or r.get("order_date"))
        if o and d:
            pairs[o] = d
    if not pairs:
        return {"received": len(rows), "orders": 0, "cards": 0}
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """UPDATE gift_cards g SET order_date = x.d, updated_at = now()
                   FROM unnest(%s::text[], %s::date[]) AS x(o, d)
                   WHERE g.source = %s AND g.order_ref = x.o AND g.order_date IS DISTINCT FROM x.d""",
                (list(pairs), list(pairs.values()), (source or "arbitragecard").lower()))
            n = cur.rowcount
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "giftcards", "op": "dates"}),))
    return {"received": len(rows), "orders": len(pairs), "cards": n}
