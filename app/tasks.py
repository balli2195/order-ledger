"""Tasks tab: Nick's to-do list. People add tasks by hand; the Hub adds some itself (kind != 'manual'), keyed so each
is created once and updated in place, e.g. a canceled order whose units were already sent to PrepOS (Nick, Nov 3 2026)."""
from __future__ import annotations

import json
from datetime import date

from fastapi import HTTPException

from . import db
from .orders import Actor, _audit, jsonable

COLS = ("id, title, details, status, kind, key, link, due, created_at, updated_at, done_at, "
        "(SELECT COALESCE(NULLIF(u.name,''), u.email) FROM users u WHERE u.id=t.created_by) AS created_by_name, "
        "(SELECT COALESCE(NULLIF(u.name,''), u.email) FROM users u WHERE u.id=t.done_by) AS done_by_name")


async def _notify(conn) -> None:
    await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "task", "op": "reload"}),))


def _due(v):
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise HTTPException(400, "Due date should look like 2026-11-05")


async def list_tasks() -> dict:
    async with db.pool.connection() as conn:
        cur = await conn.execute(f"SELECT {COLS} FROM tasks t ORDER BY (status='done'), "
                                 f"CASE WHEN status='open' THEN due END NULLS LAST, created_at DESC LIMIT 500")
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
    return {"tasks": rows, "open": sum(1 for r in rows if r["status"] == "open")}


async def open_count() -> int:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) n FROM tasks WHERE status='open'")
        return (await cur.fetchone())["n"]


async def create(actor: Actor, body: dict) -> dict:
    title = str(body.get("title") or "").strip()[:300]
    if not title:
        raise HTTPException(400, "Give the task a title")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                f"INSERT INTO tasks (title, details, due, created_by) VALUES (%s,%s,%s,%s) RETURNING id",
                (title, str(body.get("details") or "").strip()[:5000], _due(body.get("due")), actor.user_id))
            tid = (await cur.fetchone())["id"]
            await _audit(conn, actor, "create", "task", tid, None, None, {"title": title})
            await _notify(conn)
            cur = await conn.execute(f"SELECT {COLS} FROM tasks t WHERE id=%s", (tid,))
            return jsonable(dict(await cur.fetchone()))


async def update(actor: Actor, tid: str, body: dict) -> dict:
    sets, args = [], []
    if "title" in body:
        t = str(body["title"] or "").strip()[:300]
        if not t:
            raise HTTPException(400, "Give the task a title")
        sets.append("title=%s"); args.append(t)
    if "details" in body:
        sets.append("details=%s"); args.append(str(body["details"] or "").strip()[:5000])
    if "due" in body:
        sets.append("due=%s"); args.append(_due(body["due"]))
    if "status" in body:
        st = "done" if body["status"] == "done" else "open"
        sets.append("status=%s"); args.append(st)
        if st == "done":
            sets.append("done_at=now(), done_by=%s"); args.append(actor.user_id)
        else:
            sets.append("done_at=NULL, done_by=NULL")
    if not sets:
        raise HTTPException(400, "Nothing to change")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(f"UPDATE tasks SET {', '.join(sets)}, updated_at=now() WHERE id=%s RETURNING title, status", args + [tid])
            r = await cur.fetchone()
            if not r:
                raise HTTPException(404, "That task is gone")
            await _audit(conn, actor, "update", "task", tid, None, None, {k: body[k] for k in ("title", "status", "due") if k in body} | {"title": r["title"]})
            await _notify(conn)
            cur = await conn.execute(f"SELECT {COLS} FROM tasks t WHERE id=%s", (tid,))
            return jsonable(dict(await cur.fetchone()))


async def delete(actor: Actor, tid: str) -> None:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("DELETE FROM tasks WHERE id=%s RETURNING title", (tid,))
            r = await cur.fetchone()
            if not r:
                raise HTTPException(404, "That task is gone")
            await _audit(conn, actor, "delete", "task", tid, None, {"title": r["title"]}, None)
            await _notify(conn)


async def upsert_auto(conn, key: str, kind: str, title: str, details: str, link: str | None = None) -> None:
    """A task the Hub adds itself. Created once per key; if its text changes later it's updated and reopened."""
    cur = await conn.execute("SELECT id, title, details FROM tasks WHERE key=%s", (key,))
    r = await cur.fetchone()
    if not r:
        await conn.execute("INSERT INTO tasks (title, details, kind, key, link) VALUES (%s,%s,%s,%s,%s)", (title, details, kind, key, link))
    elif (r["title"], r["details"]) != (title, details):
        await conn.execute("UPDATE tasks SET title=%s, details=%s, link=%s, status='open', done_at=NULL, done_by=NULL, updated_at=now() "
                           "WHERE id=%s", (title, details, link, r["id"]))
    else:
        return
    await _notify(conn)


async def prepos_cancel_tasks(conn) -> int:
    """One task per PrepOS line that has units from canceled orders: PrepOS lines can't be changed through its API,
    so Nick fixes them by hand."""
    cur = await conn.execute(
        """SELECT s.id AS send_id, s.send_day, s.asin, s.title, s.qty AS sent_qty, s.multipack, s.supplier,
                  sum(i.qty_ordered)::int AS canceled_qty,
                  string_agg(DISTINCT o.store || ' #' || COALESCE(o.order_ref, '?') || ' (' || to_char(o.order_date, 'Mon FMDD') || ')', ', ') AS orders
             FROM prepos_sends s
             JOIN prepos_sent_items x ON x.send_id = s.id
             JOIN order_items i ON i.id = x.item_id
             JOIN orders o ON o.id = i.order_id
            WHERE COALESCE(o.confirm_info ? 'canceled', false) AND s.status = 'sent'
            GROUP BY s.id ORDER BY s.id""")
    n = 0
    for r in await cur.fetchall():
        keep = max((r["sent_qty"] or 0) - r["canceled_qty"], 0)
        pack = f" ({r['multipack']}-pack)" if r["multipack"] else ""
        title = f"PrepOS: remove {r['canceled_qty']} canceled unit{'s' if r['canceled_qty'] != 1 else ''} of {r['asin']}{pack}"
        details = (f"{r['title'] or r['asin']}\n"
                   f"Sent to PrepOS on {r['send_day']:%b %-d} ({r['supplier'] or 'store'}): {r['sent_qty']} units. "
                   f"{r['canceled_qty']} of them are from canceled orders: {r['orders']}.\n"
                   + (f"Delete this PrepOS line." if keep == 0 else f"Change the PrepOS line to {keep} units."))
        await upsert_auto(conn, f"prepos-cancel:{r['send_id']}", "prepos_cancel", title, details,
                          f"https://www.amazon.com/dp/{r['asin']}")
        n += 1
    return n
