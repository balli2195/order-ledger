"""Tasks tab: Nick's to-do list. People add tasks by hand; the Hub adds some itself (kind != 'manual'), keyed so each
is created once and updated in place, e.g. a canceled order whose units were already sent to PrepOS (Nick, Nov 3 2026)."""
from __future__ import annotations

import json
import re
from datetime import date

from fastapi import HTTPException

from . import db
from .orders import Actor, _audit, jsonable

COLS = ("id, title, details, status, kind, key, link, due, created_at, updated_at, done_at, template_id, for_date, position, "
        "(SELECT freq FROM task_templates x WHERE x.id=t.template_id) AS freq, "
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
    await ensure_recurring()
    async with db.pool.connection() as conn:
        cur = await conn.execute(f"SELECT {COLS} FROM tasks t ORDER BY (status='done'), "
                                 f"CASE WHEN status='open' THEN due END NULLS LAST, CASE WHEN status='open' THEN position END NULLS LAST, "
                                 f"COALESCE(done_at, created_at) DESC LIMIT 500")
        rows = [jsonable(dict(r)) for r in await cur.fetchall()]
    return {"tasks": rows, "open": sum(1 for r in rows if r["status"] == "open")}


async def open_count() -> int:
    await ensure_recurring()
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


async def upsert_auto(conn, key: str, kind: str, title: str, details: str, link: str | None = None, update: bool = True) -> None:
    """A task the Hub adds itself. Created once per key; if its text changes later it's updated and reopened
    (update=False: never touched again once created)."""
    cur = await conn.execute("SELECT id, title, details FROM tasks WHERE key=%s", (key,))
    r = await cur.fetchone()
    if r and not update:
        return
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


# ---------------------------------------------------------------- daily Amazon email summary

VIOLATION_KINDS = {"ip": "IP complaint", "authenticity": "Authenticity complaint"}


def _violation_text(v: dict) -> tuple[str, str, str]:
    kind = "authenticity" if str(v.get("kind", "")).lower().startswith("auth") else "ip"
    asin = str(v.get("asin") or "").strip().upper()[:10]
    product = " ".join(str(v.get("title") or "").split())[:90]
    title = f"{VIOLATION_KINDS[kind]}: {asin}" + (f" · {product}" if product else "")
    bits = [str(v.get(k)).strip() for k in ("reason",) if v.get(k)]
    if v.get("date"):
        bits.append(f"filed {v['date']}")
    if v.get("case_id"):
        bits.append(f"case {v['case_id']}")
    if v.get("appeal"):
        bits.append(f"appeal: {v['appeal']}")
    if v.get("due"):
        bits.append(f"respond by {v['due']}")
    lines = [" · ".join(bits)] if bits else []
    if v.get("sku"):
        lines.append(f"SKU {v['sku']}")
    if v.get("summary"):
        lines.append(str(v["summary"])[:600])
    if v.get("email_link"):
        lines.append(f"Email: {v['email_link']}")
    return f"viol:{kind}:{asin}", title[:300], "\n".join(lines)[:5000]


async def upsert_violations(items: list[dict], full: bool = False, update: bool = True) -> dict:
    """One task per specific violation (Nick, Nov 4 2026): IP complaints and product-authenticity customer complaints,
    keyed by kind + ASIN, linked to the chat where he works on them, listed under the daily tasks.
    full=True (a complete Account Health list): open violation tasks that are no longer on it are closed."""
    chat = None
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT value FROM settings WHERE key='violation_chat_url'")
        r = await cur.fetchone()
        chat = r["value"] if r else None
        keys, n = set(), 0
        async with conn.transaction():
            for v in reversed(items or []):   # first item ends up newest, so the list keeps the given order
                if not re.fullmatch(r"[A-Z0-9]{10}", str(v.get("asin") or "").strip().upper()):
                    continue
                key, title, details = _violation_text(v)
                if key in keys:
                    continue
                keys.add(key)
                await upsert_auto(conn, key, "violation", title, details, chat, update=update)
                await conn.execute("UPDATE tasks SET position = 6, created_at = CASE WHEN created_at = now() THEN clock_timestamp() ELSE created_at END "
                                   "WHERE key=%s", (key,))
                n += 1
            closed = 0
            if full:
                cur = await conn.execute(
                    "UPDATE tasks SET status='done', done_at=now(), updated_at=now() "
                    "WHERE kind='violation' AND status='open' AND NOT (key = ANY(%s)) RETURNING id", (list(keys),))
                closed = len(await cur.fetchall())
            await _notify(conn)
    return {"tasks": n, "closed": closed}


async def put_digest(day: date, body: dict) -> dict:
    """Engine: {period, overview, urgent:[...], routine:[...], violations:[...]} (Nick, Nov 4 2026: summary only, urgent and
    routine as bullets; older days may carry summary/headline/sections). Replaces that day's summary; tasks are added once each (key = email id)."""
    body.pop("tasks", None)
    tasks = []   # Nick, Nov 3 2026: don't add tasks from email; the summary alone shows what needs action
    violations = body.pop("violations", None) or []   # ...except one task per new IP / authenticity complaint (Nov 4 2026)
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO amazon_digests (day, data) VALUES (%s,%s) ON CONFLICT (day) DO UPDATE SET data=EXCLUDED.data, updated_at=now()",
                               (day, json.dumps(body)))
            n = 0
            for t in tasks:
                if not t.get("key") or not t.get("title"):
                    continue
                await upsert_auto(conn, "amz:" + str(t["key"])[:200], "amazon_email", str(t["title"])[:300],
                                  str(t.get("details") or "")[:5000], t.get("link"), update=False)
                n += 1
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "task", "op": "digest"}),))
    v = await upsert_violations(violations, full=False, update=False) if violations else {"tasks": 0}
    return {"day": day.isoformat(), "tasks": n, "violations": v["tasks"]}


def bullet_key(text: str) -> str:
    """A digest bullet's identity for its tick box: the hash of its text (re-posting a day keeps unchanged bullets ticked)."""
    import hashlib
    return hashlib.sha1(" ".join(str(text or "").split()).encode()).hexdigest()[:16]


async def get_digest(day: date | None, last: int = 0) -> dict:
    """One day's summary, or with last=N the N most recent days (newest first) for the stacked Inbox view.
    Each urgent/routine bullet gets a key; marks[key] = {done, task_id, task_status} (Nick, Nov 5 2026)."""
    async with db.pool.connection() as conn:
        if last:
            cur = await conn.execute("SELECT day, data, updated_at FROM amazon_digests ORDER BY day DESC LIMIT %s", (max(1, min(int(last), 14)),))
            rows = [{"day": r["day"], "data": r["data"], "updated_at": r["updated_at"]} for r in await cur.fetchall()]
            days = [r["day"] for r in rows]
            cur = await conn.execute(
                "SELECT m.day, m.key, m.done, m.task_id, t.status AS task_status FROM digest_marks m "
                "LEFT JOIN tasks t ON t.id = m.task_id WHERE m.day = ANY(%s)", (days,))
            marks: dict = {}
            for m in await cur.fetchall():
                marks.setdefault(m["day"], {})[m["key"]] = {"done": m["done"] or m["task_status"] == "done",
                                                           "task_id": m["task_id"], "task_status": m["task_status"]}
            for r in rows:
                g = r["data"] or {}
                r["keys"] = {k: [bullet_key(x) for x in (g.get(k) or [])] for k in ("urgent", "routine")}
                r["marks"] = marks.get(r["day"], {})
            return jsonable({"digests": rows})
        if day:
            cur = await conn.execute("SELECT day, data, updated_at FROM amazon_digests WHERE day=%s", (day,))
        else:
            cur = await conn.execute("SELECT day, data, updated_at FROM amazon_digests ORDER BY day DESC LIMIT 1")
        r = await cur.fetchone()
        cur = await conn.execute("SELECT day FROM amazon_digests ORDER BY day DESC LIMIT 60")
        days = [x["day"].isoformat() for x in await cur.fetchall()]
    return jsonable({"day": r["day"] if r else None, "data": r["data"] if r else None, "updated_at": r["updated_at"] if r else None, "days": days})


async def mark_bullet(actor: Actor, day: date, key: str, done: bool) -> dict:
    async with db.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO digest_marks (day, key, done, by_user, at) VALUES (%s,%s,%s,%s, now()) "
            "ON CONFLICT (day, key) DO UPDATE SET done=EXCLUDED.done, by_user=EXCLUDED.by_user, at=now()",
            (day, key[:40], done, actor.user_id))
        cur = await conn.execute("SELECT task_id FROM digest_marks WHERE day=%s AND key=%s", (day, key[:40]))
        tid = (await cur.fetchone())["task_id"]
        if tid:   # ticking the bullet also ticks the task it was moved to (and back)
            await conn.execute("UPDATE tasks SET status=%s, done_at=CASE WHEN %s THEN now() END, done_by=CASE WHEN %s THEN %s::int END "
                               "WHERE id=%s", ("done" if done else "open", done, done, actor.user_id, tid))
        await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "task", "op": "digest"}),))
    return {"ok": True}


async def bullet_to_task(actor: Actor, day: date, key: str, text: str) -> dict:
    """Moves an Amazon email bullet to the task list: title = its bold lead-in (or the start of the text), details = the
    whole bullet. Moving the same bullet twice returns the task it already made."""
    import re as _re
    text = str(text or "").strip()
    if bullet_key(text) != key:
        raise HTTPException(400, "That summary changed; reload the page")
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT m.task_id FROM digest_marks m JOIN tasks t ON t.id = m.task_id WHERE m.day=%s AND m.key=%s", (day, key))
        r = await cur.fetchone()
    if r:
        return {"task_id": str(r["task_id"]), "existing": True}
    m = _re.match(r"\*\*(.+?)\*\*\s*(.*)", text, _re.S)
    plain = _re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    lead = (m.group(1).strip().rstrip(":").strip() if m else "") or plain
    title = (lead if len(lead) <= 120 else lead[:117] + "…")
    details = f"{plain}\n\nFrom the Amazon email summary, {day.strftime('%b')} {day.day}."
    t = await create(actor, {"title": title, "details": details})
    async with db.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO digest_marks (day, key, task_id, by_user, at) VALUES (%s,%s,%s,%s, now()) "
            "ON CONFLICT (day, key) DO UPDATE SET task_id=EXCLUDED.task_id, by_user=EXCLUDED.by_user, at=now()",
            (day, key, t["id"], actor.user_id))
        await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "task", "op": "digest"}),))
    return {"task_id": t["id"], "task": t}


# ---------------------------------------------------------------- recurring tasks

TZ = "America/Chicago"
FREQS = ("daily", "weekdays", "weekly", "monthly")
_last_ensure: dict = {"day": None, "at": 0.0}


def _due_on(t: dict, d: date) -> bool:
    f = t["freq"]
    if f == "daily":
        return True
    if f == "weekdays":
        return d.weekday() < 5
    if f == "weekly":
        return d.weekday() in (t["days"] or [])
    if f == "monthly":
        import calendar
        md = min(int(t["monthday"] or 1), calendar.monthrange(d.year, d.month)[1])
        return d.day == md
    return False


async def ensure_recurring(force: bool = False) -> int:
    """Add today's recurring tasks (Central time). A template whose last one is still open isn't added again:
    the open one carries over (it shows as overdue). Runs at most once a minute."""
    import time
    if not force and time.monotonic() - _last_ensure["at"] < 60:
        return 0
    _last_ensure["at"] = time.monotonic()
    n = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(f"SELECT (now() AT TIME ZONE '{TZ}')::date AS d")
            today = (await cur.fetchone())["d"]
            cur = await conn.execute(
                "SELECT t.* FROM task_templates t WHERE t.active "
                "AND NOT EXISTS (SELECT 1 FROM tasks x WHERE x.template_id=t.id AND (x.for_date=%s OR x.status='open')) ORDER BY position",
                (today,))
            for t in await cur.fetchall():
                if not _due_on(t, today):
                    continue
                await conn.execute(
                    "INSERT INTO tasks (title, details, kind, template_id, for_date, due, position, link) VALUES (%s,%s,'recurring',%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING", (t["title"], t["details"], t["id"], today, today, t["position"], t.get("link")))
                n += 1
            if n:
                await _notify(conn)
    return n


async def list_templates() -> list[dict]:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT id, title, details, link, freq, days, monthday, position, active FROM task_templates ORDER BY position, created_at")
        return [jsonable(dict(r)) for r in await cur.fetchall()]


def _tpl_fields(body: dict, partial: bool) -> dict:
    out = {}
    if "title" in body or not partial:
        t = str(body.get("title") or "").strip()[:300]
        if not t:
            raise HTTPException(400, "Give the task a title")
        out["title"] = t
    if "details" in body:
        out["details"] = str(body.get("details") or "").strip()[:5000]
    if "freq" in body or not partial:
        f = body.get("freq") or "daily"
        if f not in FREQS:
            raise HTTPException(400, "Repeat must be daily, weekdays, weekly or monthly")
        out["freq"] = f
    if "days" in body:
        out["days"] = sorted({int(x) for x in (body.get("days") or []) if str(x).strip() != "" and 0 <= int(x) <= 6})
    if "monthday" in body:
        md = body.get("monthday")
        out["monthday"] = max(1, min(31, int(md))) if md not in (None, "") else None
    if "link" in body:
        lk = str(body.get("link") or "").strip()[:1000]
        if lk and not lk.startswith(("https://", "http://")):
            raise HTTPException(400, "The link should start with https://")
        out["link"] = lk or None
    if "active" in body:
        out["active"] = bool(body["active"])
    if "position" in body:
        out["position"] = int(body["position"] or 0)
    if out.get("freq") == "weekly" and not out.get("days", [1]) and not partial:
        raise HTTPException(400, "Pick the day(s) of the week")
    return out


async def save_template(actor: Actor, tid: str | None, body: dict) -> dict:
    f = _tpl_fields(body, partial=bool(tid))
    is_new = not tid
    async with db.pool.connection() as conn:
        async with conn.transaction():
            if tid:
                if not f:
                    raise HTTPException(400, "Nothing to change")
                cur = await conn.execute(f"UPDATE task_templates SET {', '.join(k + '=%s' for k in f)} WHERE id=%s RETURNING id",
                                         list(f.values()) + [tid])
                if not await cur.fetchone():
                    raise HTTPException(404, "That recurring task is gone")
                # keep today's open copy in step with a renamed template
                if "title" in f or "details" in f:
                    await conn.execute("UPDATE tasks SET title=COALESCE(%s, title), details=COALESCE(%s, details) WHERE template_id=%s AND status='open'",
                                       (f.get("title"), f.get("details"), tid))
                if "link" in f:
                    await conn.execute("UPDATE tasks SET link=%s WHERE template_id=%s AND status='open'", (f["link"], tid))
                if f.get("active") is False:   # paused: drop its unfinished copy
                    await conn.execute("DELETE FROM tasks WHERE template_id=%s AND status='open'", (tid,))
            else:
                cur = await conn.execute("SELECT COALESCE(max(position), 0) + 1 AS p FROM task_templates")
                f.setdefault("position", (await cur.fetchone())["p"])
                f["created_by"] = actor.user_id
                cur = await conn.execute(f"INSERT INTO task_templates ({', '.join(f)}) VALUES ({', '.join(['%s'] * len(f))}) RETURNING id",
                                         list(f.values()))
                tid = str((await cur.fetchone())["id"])
            await _audit(conn, actor, "create" if is_new else "update", "task_template", tid, None, None, {"title": f.get("title")})
            await _notify(conn)
    await ensure_recurring(force=True)
    return {"id": tid}


async def delete_template(actor: Actor, tid: str) -> None:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM tasks WHERE template_id=%s AND status='open'", (tid,))
            cur = await conn.execute("DELETE FROM task_templates WHERE id=%s RETURNING title", (tid,))
            r = await cur.fetchone()
            if not r:
                raise HTTPException(404, "That recurring task is gone")
            await _audit(conn, actor, "delete", "task_template", tid, None, {"title": r["title"]}, None)
            await _notify(conn)
