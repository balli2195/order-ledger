"""Order and line-item data access. Every write is audited and broadcast live."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from . import db

ORDER_FIELDS = ("order_date", "store", "email", "order_total", "order_ref", "notes")
ITEM_FIELDS = ("asin", "product_title", "shade", "set_type", "qty_ordered", "cost_per_unit")


class LedgerError(ValueError):
    """A problem with the caller's input; safe to show to the user."""


# ---------------------------------------------------------------- helpers

def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if hasattr(v, "hex") and type(v).__name__ == "UUID":
        return str(v)
    return v


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _money(v: Any, field: str, places: int = 2) -> Decimal | None:
    if _blank(v):
        return None
    try:
        d = Decimal(str(v).replace("$", "").replace(",", "").strip())
    except Exception:
        raise LedgerError(f"{field} must be a number (got {v!r})")
    return d.quantize(Decimal(1).scaleb(-places))


def _date(v: Any) -> date:
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise LedgerError(f"order_date must be a date like 2026-10-29 (got {v!r})")


def _text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def clean_order(data: dict, partial: bool) -> dict:
    out: dict[str, Any] = {}
    for k in ORDER_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "order_date":
            out[k] = _date(v)
        elif k == "order_total":
            out[k] = _money(v, "order_total")
        elif k == "store":
            s = _text(v)
            if not s:
                raise LedgerError("store can't be blank")
            out[k] = s
        else:
            out[k] = _text(v)
    if not partial:
        if "order_date" not in out:
            raise LedgerError("order_date is required")
        if "store" not in out:
            raise LedgerError("store is required")
    return out


def clean_item(data: dict, partial: bool, strict: bool = True) -> dict:
    out: dict[str, Any] = {}
    for k in ITEM_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k == "asin":
            s = _text(v)
            if s:
                s = s.upper()
                if strict and not ASIN_RE.match(s):
                    raise LedgerError(f"ASIN should be 10 letters/digits (got {v!r})")
            out[k] = s
        elif k == "qty_ordered":
            try:
                q = int(Decimal(str(v))) if not _blank(v) else 1
            except Exception:
                raise LedgerError(f"qty_ordered must be a whole number (got {v!r})")
            if q < 0:
                raise LedgerError("qty_ordered can't be negative")
            out[k] = q
        elif k == "cost_per_unit":
            out[k] = _money(v, "cost_per_unit", places=4)
        else:
            out[k] = _text(v)
    if not partial and "qty_ordered" not in out:
        out["qty_ordered"] = 1
    return out


async def _notify(conn, op: str, order_id: Any, by: str | None, source: str) -> None:
    payload = json.dumps({"op": op, "id": str(order_id), "by": by, "src": source})
    await conn.execute("SELECT pg_notify('ledger', %s)", (payload,))


async def _audit(conn, actor: "Actor", action: str, entity: str, entity_id, order_id, before, after) -> None:
    await conn.execute(
        "INSERT INTO audit_log (user_id, source, action, entity, entity_id, order_id, before, after) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            actor.user_id,
            actor.source,
            action,
            entity,
            entity_id,
            order_id,
            json.dumps(jsonable(before)) if before is not None else None,
            json.dumps(jsonable(after)) if after is not None else None,
        ),
    )


class Actor:
    def __init__(self, user_id: int | None, label: str | None, source: str):
        self.user_id = user_id
        self.label = label
        self.source = source  # 'web' | 'chat' | 'import' | 'api'


# ---------------------------------------------------------------- reads

SUMMARY_SQL = """
SELECT o.id, o.order_date, o.store, o.email, o.order_total, o.order_ref, o.notes,
       o.created_at, o.updated_at,
       COALESCE(SUM(i.qty_ordered * COALESCE(i.cost_per_unit, 0)), 0)::numeric(14,2) AS items_total,
       COALESCE(SUM(i.qty_ordered), 0)::int AS units,
       COUNT(i.id)::int AS item_count,
       array_remove(ARRAY[
         CASE WHEN bool_or(i.asin IS NULL OR btrim(i.asin) = '') THEN 'ASIN' END,
         CASE WHEN bool_or(i.product_title IS NULL OR btrim(i.product_title) = '') THEN 'Product title' END,
         CASE WHEN bool_or(i.cost_per_unit IS NULL) THEN 'Cost per unit' END,
         CASE WHEN bool_or(i.set_type IS NULL OR btrim(i.set_type) = '') THEN 'Set type' END,
         CASE WHEN bool_or(i.shade IS NULL OR btrim(i.shade) = '') THEN 'Shade' END,
         CASE WHEN bool_or(i.qty_ordered < 1) THEN 'Qty ordered' END
       ], NULL) AS gaps,
       string_agg(DISTINCT upper(i.asin), ' ') AS asins
FROM orders o
LEFT JOIN order_items i ON i.order_id = o.id
{where}
GROUP BY o.id
ORDER BY o.order_date DESC, o.created_at DESC
{limit}
"""


def decorate(row: dict) -> dict:
    r = jsonable(dict(row))
    items_total = r.get("items_total") or 0.0
    total = r.get("order_total") or 0.0
    tolerance = (r.get("units") or 0) * 0.01
    r["discrepancy"] = abs(items_total - total) > tolerance + 1e-9
    if not r.get("item_count"):
        r["missing"] = ["No items"]
    else:
        r["missing"] = r.get("gaps") or []
    r.pop("gaps", None)
    return r


async def list_orders(
    *,
    query: str | None = None,
    store: str | None = None,
    date_from: Any = None,
    date_to: Any = None,
    asin: str | None = None,
    order_ref: str | None = None,
    problem: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    where, args = [], []
    if store:
        where.append("o.store ILIKE %s")
        args.append(f"%{store.strip()}%")
    if date_from:
        where.append("o.order_date >= %s")
        args.append(_date(date_from))
    if date_to:
        where.append("o.order_date <= %s")
        args.append(_date(date_to))
    if order_ref:
        where.append("o.order_ref ILIKE %s")
        args.append(f"%{order_ref.strip()}%")
    if asin:
        where.append("EXISTS (SELECT 1 FROM order_items x WHERE x.order_id=o.id AND upper(x.asin)=upper(%s))")
        args.append(asin.strip())
    if query:
        q = f"%{query.strip()}%"
        where.append(
            "(o.store ILIKE %s OR o.email ILIKE %s OR o.order_ref ILIKE %s OR o.notes ILIKE %s OR "
            "EXISTS (SELECT 1 FROM order_items x WHERE x.order_id=o.id AND "
            "(x.asin ILIKE %s OR x.product_title ILIKE %s OR x.shade ILIKE %s)))"
        )
        args += [q] * 7
    sql = SUMMARY_SQL.format(
        where=("WHERE " + " AND ".join(where)) if where else "",
        limit="",
    )
    async with db.pool.connection() as conn:
        cur = await conn.execute(sql, args)
        rows = [decorate(r) for r in await cur.fetchall()]
    if problem == "discrepancy":
        rows = [r for r in rows if r["discrepancy"]]
    elif problem == "missing":
        rows = [r for r in rows if r["missing"]]
    elif problem == "any":
        rows = [r for r in rows if r["discrepancy"] or r["missing"]]
    if limit:
        rows = rows[:limit]
    return rows


async def order_summary(conn, order_id) -> dict | None:
    cur = await conn.execute(SUMMARY_SQL.format(where="WHERE o.id = %s", limit=""), (order_id,))
    r = await cur.fetchone()
    return decorate(r) if r else None


async def get_order(order_id=None, order_ref: str | None = None) -> dict | None:
    async with db.pool.connection() as conn:
        if order_id is None and order_ref:
            cur = await conn.execute(
                "SELECT id FROM orders WHERE lower(order_ref)=lower(%s) ORDER BY order_date DESC LIMIT 2",
                (order_ref.strip(),),
            )
            found = await cur.fetchall()
            if not found:
                return None
            if len(found) > 1:
                raise LedgerError(f"More than one order has order ID {order_ref!r}; use the internal id")
            order_id = found[0]["id"]
        try:
            o = await order_summary(conn, order_id)
        except Exception as e:  # bad uuid
            if "uuid" in str(e).lower():
                raise LedgerError(f"{order_id!r} isn't a valid internal order id")
            raise
        if not o:
            return None
        cur = await conn.execute(
            "SELECT id, asin, product_title, shade, set_type, qty_ordered, cost_per_unit, created_at, updated_at "
            "FROM order_items WHERE order_id=%s ORDER BY created_at, id",
            (order_id,),
        )
        o["items"] = [jsonable(dict(r)) for r in await cur.fetchall()]
    return o


async def suggestions() -> dict:
    async with db.pool.connection() as conn:
        out = {}
        for key, sql in {
            "stores": "SELECT store v, count(*) c FROM orders GROUP BY store ORDER BY c DESC LIMIT 300",
            "emails": "SELECT email v, count(*) c FROM orders WHERE email IS NOT NULL GROUP BY email ORDER BY c DESC LIMIT 50",
            "set_types": "SELECT name v FROM set_types UNION SELECT DISTINCT set_type FROM order_items WHERE set_type IS NOT NULL ORDER BY 1",
        }.items():
            cur = await conn.execute(sql)
            out[key] = [r["v"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT value FROM settings WHERE key='daily_target'")
        r = await cur.fetchone()
        out["daily_target"] = float(r["value"]) if r else 6800.0
    return out


async def monthly_spend(months: int = 6) -> list[dict]:
    import calendar

    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT value FROM settings WHERE key='daily_target'")
        r = await cur.fetchone()
        target = float(r["value"]) if r else 6800.0
        cur = await conn.execute(
            "SELECT to_char(order_date,'YYYY-MM') m, COALESCE(SUM(order_total),0) s, COUNT(*) n "
            "FROM orders GROUP BY 1 ORDER BY 1 DESC LIMIT %s",
            (months,),
        )
        rows = await cur.fetchall()
    today = date.today()
    out = []
    for row in rows:
        y, m = map(int, row["m"].split("-"))
        dim = calendar.monthrange(y, m)[1]
        if (y, m) == (today.year, today.month):
            days = today.day
        elif (y, m) > (today.year, today.month):
            days = 0
        else:
            days = dim
        spent = float(row["s"])
        out.append(
            {
                "month": row["m"],
                "orders": row["n"],
                "spent": round(spent, 2),
                "days_counted": days,
                "target": round(target * days, 2),
                "need_to_spend": round(target * days - spent, 2),
            }
        )
    return out


# ---------------------------------------------------------------- writes

async def create_order(actor: Actor, data: dict, items: Iterable[dict] = ()) -> dict:
    fields = clean_order(data, partial=False)
    clean_items = [clean_item(i, partial=False) for i in items]
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cols = list(fields) + ["created_by", "updated_by"]
            vals = list(fields.values()) + [actor.user_id, actor.user_id]
            cur = await conn.execute(
                f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
                vals,
            )
            order = await cur.fetchone()
            oid = order["id"]
            await _audit(conn, actor, "create", "order", oid, oid, None, order)
            for it in clean_items:
                await _insert_item(conn, actor, oid, it)
            await _notify(conn, "upsert", oid, actor.label, actor.source)
    return await get_order(oid)


async def update_order(actor: Actor, order_id, data: dict) -> dict:
    fields = clean_order(data, partial=True)
    if not fields:
        raise LedgerError("Nothing to change")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,))
            before = await cur.fetchone()
            if not before:
                raise LedgerError("Order not found")
            sets = ", ".join(f"{k}=%s" for k in fields)
            cur = await conn.execute(
                f"UPDATE orders SET {sets}, updated_at=now(), updated_by=%s WHERE id=%s RETURNING *",
                list(fields.values()) + [actor.user_id, order_id],
            )
            after = await cur.fetchone()
            await _audit(conn, actor, "update", "order", order_id, order_id,
                         {k: before[k] for k in fields}, {k: after[k] for k in fields})
            await _notify(conn, "upsert", order_id, actor.label, actor.source)
    return await get_order(order_id)


async def delete_order(actor: Actor, order_id) -> dict:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,))
            before = await cur.fetchone()
            if not before:
                raise LedgerError("Order not found")
            cur = await conn.execute("SELECT * FROM order_items WHERE order_id=%s", (order_id,))
            items = await cur.fetchall()
            await conn.execute("DELETE FROM orders WHERE id=%s", (order_id,))
            await _audit(conn, actor, "delete", "order", order_id, order_id,
                         {"order": before, "items": items}, None)
            await _notify(conn, "delete", order_id, actor.label, actor.source)
    return jsonable({"deleted": str(order_id), "store": before["store"], "order_ref": before["order_ref"],
                     "items_removed": len(items)})


async def _insert_item(conn, actor: Actor, order_id, it: dict) -> dict:
    cols = ["order_id"] + list(it)
    cur = await conn.execute(
        f"INSERT INTO order_items ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
        [order_id] + list(it.values()),
    )
    row = await cur.fetchone()
    await _audit(conn, actor, "create", "item", row["id"], order_id, None, row)
    return row


async def _touch(conn, actor: Actor, order_id) -> None:
    await conn.execute("UPDATE orders SET updated_at=now(), updated_by=%s WHERE id=%s", (actor.user_id, order_id))


async def add_items(actor: Actor, order_id, items: Iterable[dict]) -> dict:
    clean_items = [clean_item(i, partial=False) for i in items]
    if not clean_items:
        raise LedgerError("No items given")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT id FROM orders WHERE id=%s FOR UPDATE", (order_id,))
            if not await cur.fetchone():
                raise LedgerError("Order not found")
            for it in clean_items:
                await _insert_item(conn, actor, order_id, it)
            await _touch(conn, actor, order_id)
            await _notify(conn, "upsert", order_id, actor.label, actor.source)
    return await get_order(order_id)


async def update_items(actor: Actor, changes: list[dict]) -> list[str]:
    """changes: [{"id": item_id, ...fields}]. Returns the affected order ids."""
    touched: set = set()
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for ch in changes:
                item_id = ch.get("id")
                fields = clean_item({k: v for k, v in ch.items() if k != "id"}, partial=True)
                if not item_id or not fields:
                    raise LedgerError("Each change needs an item id and at least one field")
                cur = await conn.execute("SELECT * FROM order_items WHERE id=%s FOR UPDATE", (item_id,))
                before = await cur.fetchone()
                if not before:
                    raise LedgerError(f"Item {item_id} not found")
                sets = ", ".join(f"{k}=%s" for k in fields)
                cur = await conn.execute(
                    f"UPDATE order_items SET {sets}, updated_at=now() WHERE id=%s RETURNING *",
                    list(fields.values()) + [item_id],
                )
                after = await cur.fetchone()
                await _audit(conn, actor, "update", "item", item_id, before["order_id"],
                             {k: before[k] for k in fields}, {k: after[k] for k in fields})
                touched.add(before["order_id"])
            for oid in touched:
                await _touch(conn, actor, oid)
                await _notify(conn, "upsert", oid, actor.label, actor.source)
    return [str(o) for o in touched]


async def delete_items(actor: Actor, item_ids: list) -> list[str]:
    touched: set = set()
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for item_id in item_ids:
                cur = await conn.execute("DELETE FROM order_items WHERE id=%s RETURNING *", (item_id,))
                before = await cur.fetchone()
                if not before:
                    raise LedgerError(f"Item {item_id} not found")
                await _audit(conn, actor, "delete", "item", item_id, before["order_id"], before, None)
                touched.add(before["order_id"])
            for oid in touched:
                await _touch(conn, actor, oid)
                await _notify(conn, "upsert", oid, actor.label, actor.source)
    return [str(o) for o in touched]


async def set_daily_target(actor: Actor, amount: float) -> None:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('daily_target', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (json.dumps(float(amount)),),
            )
            await _audit(conn, actor, "update", "setting", None, None, None, {"daily_target": amount})
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"op": "settings"}),))


async def recent_changes(limit: int = 50, order_id=None, entity_id=None) -> list[dict]:
    where, args = "", []
    if order_id:
        where, args = "WHERE a.order_id=%s", [order_id]
    elif entity_id:
        where, args = "WHERE a.entity_id=%s", [entity_id]
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT a.id, a.at, a.source, a.action, a.entity, a.entity_id, a.order_id, a.before, a.after, "
            f"(SELECT COALESCE(ld.asin, '') FROM leads ld WHERE a.entity='lead' AND ld.id=a.entity_id) AS lead_asin, "
            f"COALESCE(NULLIF(u.name,''), u.email, CASE WHEN a.source='engine' THEN 'Claude engine' END) AS who, o.store, o.order_ref "
            f"FROM audit_log a LEFT JOIN users u ON u.id=a.user_id LEFT JOIN orders o ON o.id=a.order_id "
            f"{where} ORDER BY a.id DESC LIMIT %s",
            args + [limit],
        )
        return [jsonable(dict(r)) for r in await cur.fetchall()]


async def run_readonly_sql(sql: str, max_rows: int = 500) -> dict:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise LedgerError("One statement at a time")
    if not re.match(r"^\s*(select|with|explain)\b", stripped, re.I):
        raise LedgerError("Only SELECT / WITH queries are allowed here")
    if re.search(r"\b(oauth_\w+|invites|password_hash|session_ver|pg_\w+|set_config|dblink|lo_\w+)\b", stripped, re.I):
        raise LedgerError("That table or function isn't available to queries")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SET TRANSACTION READ ONLY")
            await conn.execute("SET LOCAL statement_timeout = '15s'")
            cur = await conn.execute(stripped)
            rows = await cur.fetchmany(max_rows + 1)
            cols = [d.name for d in cur.description] if cur.description else []
    truncated = len(rows) > max_rows
    return {"columns": cols, "rows": [jsonable(dict(r)) for r in rows[:max_rows]], "truncated": truncated}
