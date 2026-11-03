"""Time clock: people clock in and out with their Hub login; the owner sees hours and pay per person and week.

Weeks run Monday to Sunday in Central time (as in the old Google Sheets timesheets). An entry belongs to the day it
started on, so a shift past midnight counts on the day it began. Pay for a week = hours x the person's hourly rate +
that week's adjustments (bonus, laundry, deductions...)."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException

from . import db
from .orders import Actor, _audit, jsonable

TZ = "America/Chicago"
HOURS = "EXTRACT(EPOCH FROM (COALESCE(e.clock_out, now()) - e.clock_in)) / 3600.0"
DAY = f"(e.clock_in AT TIME ZONE '{TZ}')::date"


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parse_week(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return monday(date.fromisoformat(s[:10]))
    except ValueError:
        raise HTTPException(400, "Week must be a date like 2026-11-02")


async def _notify(conn) -> None:
    await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "timeclock", "op": "reload"}),))


def _n(v):
    return float(v) if isinstance(v, Decimal) else v


# ---------------------------------------------------------------- people

async def person_for_user(conn, user_id: int) -> dict | None:
    cur = await conn.execute("SELECT * FROM tc_people WHERE user_id=%s", (user_id,))
    return await cur.fetchone()


async def _link_by_name(conn, user) -> dict | None:
    """Join a login to an unlinked time-clock person with its name: the full name, its first word, or the email name
    (e.g. the imported "Abby" timesheet and the login "Abby Smith")."""
    first = (user.name or "").strip().split(" ")[0]
    local = (user.email or "").split("@")[0]
    cur = await conn.execute(
        "UPDATE tc_people SET user_id=%s WHERE id = (SELECT id FROM tc_people WHERE user_id IS NULL AND active "
        "AND lower(name) IN (lower(%s), lower(%s), lower(%s)) ORDER BY (lower(name)=lower(%s)) DESC, id LIMIT 1) RETURNING *",
        (user.id, user.label, first or user.label, local, user.label))
    return await cur.fetchone()


async def _ensure_person(conn, user) -> dict:
    """The time-clock person for this login: linked already, else an unlinked person with the same name, else a new one."""
    p = await person_for_user(conn, user.id)
    if p:
        return p
    p = await _link_by_name(conn, user)
    if p:
        return p
    cur = await conn.execute("INSERT INTO tc_people (name, user_id) VALUES (%s,%s) "
                             "ON CONFLICT (lower(name)) DO UPDATE SET name=tc_people.name RETURNING *", (user.label, user.id))
    p = await cur.fetchone()
    if p["user_id"] != user.id:   # same name already taken by someone linked to another login
        cur = await conn.execute("INSERT INTO tc_people (name, user_id) VALUES (%s,%s) RETURNING *", (f"{user.label} ({user.email})", user.id))
        p = await cur.fetchone()
    return p


async def add_person(actor: Actor, name: str, rate) -> dict:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Enter a name")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("INSERT INTO tc_people (name, rate) VALUES (%s,%s) ON CONFLICT (lower(name)) DO NOTHING RETURNING *",
                                     (name, _money(rate)))
            p = await cur.fetchone()
            if not p:
                raise HTTPException(400, f"{name} is already on the time clock")
            await _audit(conn, actor, "create", "tc_person", None, None, None, {"name": name, "rate": _money(rate)})
            await _notify(conn)
    return jsonable(dict(p))


async def update_person(actor: Actor, pid: int, body: dict) -> dict:
    sets, args = [], []
    if "name" in body:
        n = str(body["name"] or "").strip()
        if not n:
            raise HTTPException(400, "Enter a name")
        sets.append("name=%s"); args.append(n)
    if "rate" in body:
        sets.append("rate=%s"); args.append(_money(body["rate"]))
    if "active" in body:
        sets.append("active=%s"); args.append(bool(body["active"]))
    if "user_id" in body:
        sets.append("user_id=%s"); args.append(int(body["user_id"]) if body["user_id"] not in (None, "", 0, "0") else None)
    if not sets:
        raise HTTPException(400, "Nothing to change")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT * FROM tc_people WHERE id=%s FOR UPDATE", (pid,))
            before = await cur.fetchone()
            if not before:
                raise HTTPException(404, "No such person")
            if "user_id" in body and args[-1] is not None:
                await conn.execute("UPDATE tc_people SET user_id=NULL WHERE user_id=%s AND id<>%s", (args[-1], pid))
            try:
                cur = await conn.execute(f"UPDATE tc_people SET {', '.join(sets)} WHERE id=%s RETURNING *", args + [pid])
            except Exception as e:
                if "tc_people_name_idx" in str(e):
                    raise HTTPException(400, "Someone else already has that name")
                raise
            after = await cur.fetchone()
            await _audit(conn, actor, "update", "tc_person", None, None, {k: before[k] for k in body if k in before},
                         {k: after[k] for k in body if k in after})
            await _notify(conn)
    return jsonable(dict(after))


def _money(v):
    if v in (None, ""):
        return None
    try:
        return round(float(str(v).replace("$", "").replace(",", "")), 2)
    except ValueError:
        raise HTTPException(400, "Enter an amount like 15.00")


# ---------------------------------------------------------------- clocking in and out

async def clock(actor: Actor, user, direction: str, note: str = "") -> dict:
    note = (note or "").strip()[:500]
    async with db.pool.connection() as conn:
        async with conn.transaction():
            p = await _ensure_person(conn, user)
            if not p["active"]:
                raise HTTPException(403, "Your time clock is turned off; ask the owner")
            cur = await conn.execute("SELECT * FROM time_entries WHERE person_id=%s AND clock_out IS NULL FOR UPDATE", (p["id"],))
            open_e = await cur.fetchone()
            if direction == "in":
                if open_e:
                    raise HTTPException(400, "You're already clocked in")
                cur = await conn.execute("INSERT INTO time_entries (person_id, clock_in, note, source) VALUES (%s, now(), %s, 'clock') RETURNING *",
                                         (p["id"], note))
                e = await cur.fetchone()
                await _audit(conn, actor, "create", "time_entry", e["id"], None, None, {"person": p["name"], "clock_in": e["clock_in"]})
            else:
                if not open_e:
                    raise HTTPException(400, "You're not clocked in")
                cur = await conn.execute(
                    "UPDATE time_entries SET clock_out=now(), note=CASE WHEN %s='' THEN note WHEN note='' THEN %s ELSE note || ' · ' || %s END, "
                    "updated_at=now() WHERE id=%s RETURNING *", (note, note, note, open_e["id"]))
                e = await cur.fetchone()
                await _audit(conn, actor, "update", "time_entry", e["id"], None, {"clock_out": None},
                             {"person": p["name"], "clock_out": e["clock_out"]})
            await _notify(conn)
    return jsonable(dict(e))


# ---------------------------------------------------------------- owner edits

_HM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def _times(body: dict) -> tuple[str, str | None]:
    """{date: 'YYYY-MM-DD', in: 'HH:MM', out: 'HH:MM' | ''} as Central wall-clock strings; an out at or before the in is the next day."""
    try:
        d = date.fromisoformat(str(body.get("date", ""))[:10])
    except ValueError:
        raise HTTPException(400, "Pick a date")
    m_in = _HM.match(str(body.get("in") or ""))
    if not m_in:
        raise HTTPException(400, "Enter a clock-in time")
    t_in = (int(m_in[1]), int(m_in[2]))
    start = f"{d.isoformat()} {t_in[0]:02d}:{t_in[1]:02d}"
    out = str(body.get("out") or "").strip()
    if not out:
        return start, None
    m_out = _HM.match(out)
    if not m_out:
        raise HTTPException(400, "Clock-out time should look like 17:30")
    t_out = (int(m_out[1]), int(m_out[2]))
    d_out = d + timedelta(days=1) if t_out <= t_in else d
    return start, f"{d_out.isoformat()} {t_out[0]:02d}:{t_out[1]:02d}"


async def add_entry(actor: Actor, body: dict) -> dict:
    start, end = _times(body)
    note = str(body.get("note") or "").strip()[:500]
    async with db.pool.connection() as conn:
        async with conn.transaction():
            try:
                cur = await conn.execute(
                    f"INSERT INTO time_entries (person_id, clock_in, clock_out, note, source) VALUES "
                    f"(%s, %s::timestamp AT TIME ZONE '{TZ}', %s::timestamp AT TIME ZONE '{TZ}', %s, 'manual') RETURNING *",
                    (int(body.get("person_id") or 0), start, end, note))
            except Exception as e:
                raise HTTPException(400, _friendly(e))
            e = await cur.fetchone()
            await _audit(conn, actor, "create", "time_entry", e["id"], None, None, jsonable(dict(e)))
            await _notify(conn)
    return jsonable(dict(e))


async def update_entry(actor: Actor, eid: str, body: dict) -> dict:
    start, end = _times(body)
    note = str(body.get("note") or "").strip()[:500]
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT * FROM time_entries WHERE id=%s FOR UPDATE", (eid,))
            before = await cur.fetchone()
            if not before:
                raise HTTPException(404, "That entry is gone")
            try:
                cur = await conn.execute(
                    f"UPDATE time_entries SET clock_in=%s::timestamp AT TIME ZONE '{TZ}', clock_out=%s::timestamp AT TIME ZONE '{TZ}', "
                    f"note=%s, updated_at=now() WHERE id=%s RETURNING *", (start, end, note, eid))
            except Exception as e:
                raise HTTPException(400, _friendly(e))
            after = await cur.fetchone()
            await _audit(conn, actor, "update", "time_entry", eid, None,
                         jsonable({k: before[k] for k in ("clock_in", "clock_out", "note")}),
                         jsonable({k: after[k] for k in ("clock_in", "clock_out", "note")}))
            await _notify(conn)
    return jsonable(dict(after))


async def delete_entry(actor: Actor, eid: str) -> None:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("DELETE FROM time_entries WHERE id=%s RETURNING *", (eid,))
            before = await cur.fetchone()
            if not before:
                raise HTTPException(404, "That entry is gone")
            await _audit(conn, actor, "delete", "time_entry", eid, None, jsonable(dict(before)), None)
            await _notify(conn)


def _friendly(e: Exception) -> str:
    s = str(e)
    if "time_entries_open_idx" in s:
        return "This person is already clocked in; give that entry a clock-out first"
    if "time_entries_check" in s or "clock_out" in s and "check" in s:
        return "Clock-out must be after clock-in"
    if "foreign key" in s or "person_id" in s:
        return "Pick a person"
    return "Couldn't save that entry"


async def add_adjustment(actor: Actor, body: dict) -> dict:
    wk = parse_week(str(body.get("week_start") or ""))
    label = str(body.get("label") or "").strip()[:80]
    amount = _money(body.get("amount"))
    if not wk or not label or amount is None:
        raise HTTPException(400, "Enter a label and an amount (use a minus sign for a deduction)")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("INSERT INTO tc_adjustments (person_id, week_start, label, amount) VALUES (%s,%s,%s,%s) RETURNING *",
                                     (int(body.get("person_id") or 0), wk, label, amount))
            a = await cur.fetchone()
            await _audit(conn, actor, "create", "tc_adjustment", a["id"], None, None, jsonable(dict(a)))
            await _notify(conn)
    return jsonable(dict(a))


async def delete_adjustment(actor: Actor, aid: str) -> None:
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("DELETE FROM tc_adjustments WHERE id=%s RETURNING *", (aid,))
            before = await cur.fetchone()
            if not before:
                raise HTTPException(404, "That line is gone")
            await _audit(conn, actor, "delete", "tc_adjustment", aid, None, jsonable(dict(before)), None)
            await _notify(conn)


# ---------------------------------------------------------------- the page's data

async def overview(user, week: date | None, history_weeks: int = 16) -> dict:
    owner = user.role == "owner"
    async with db.pool.connection() as conn:
        cur = await conn.execute(f"SELECT (now() AT TIME ZONE '{TZ}')::date AS d")
        today = (await cur.fetchone())["d"]
        wk = week or monday(today)
        me = await person_for_user(conn, user.id)
        if not me and not owner:
            me = await _link_by_name(conn, user)   # their imported timesheet shows up the first time they open the page
            if me:
                await conn.commit()
        my_open = None
        if me:
            cur = await conn.execute("SELECT id, clock_in, note FROM time_entries WHERE person_id=%s AND clock_out IS NULL", (me["id"],))
            my_open = await cur.fetchone()
        if owner:
            cur = await conn.execute(
                f"SELECT * FROM tc_people p WHERE p.active OR EXISTS (SELECT 1 FROM time_entries e WHERE e.person_id=p.id AND {DAY} BETWEEN %s AND %s) "
                f"ORDER BY lower(p.name)", (wk, wk + timedelta(days=6)))
            people = await cur.fetchall()
        else:
            people = [me] if me else []
        ids = [p["id"] for p in people]
        cur = await conn.execute(
            f"SELECT e.id, e.person_id, e.clock_in, e.clock_out, e.note, e.source, {DAY} AS day, {HOURS} AS hours "
            f"FROM time_entries e WHERE e.person_id = ANY(%s) AND {DAY} BETWEEN %s AND %s ORDER BY e.clock_in",
            (ids, wk, wk + timedelta(days=6)))
        entries = await cur.fetchall()
        cur = await conn.execute("SELECT id, person_id, label, amount FROM tc_adjustments WHERE person_id = ANY(%s) AND week_start=%s ORDER BY created_at",
                                 (ids, wk))
        adjs = await cur.fetchall()
        h0 = wk - timedelta(weeks=history_weeks - 1)
        cur = await conn.execute(
            f"SELECT e.person_id, date_trunc('week', {DAY})::date AS wk, sum({HOURS}) AS hours FROM time_entries e "
            f"WHERE e.person_id = ANY(%s) AND {DAY} BETWEEN %s AND %s GROUP BY 1, 2",
            (ids, h0, wk + timedelta(days=6)))
        hist = await cur.fetchall()
        users = []
        if owner:
            cur = await conn.execute("SELECT id, COALESCE(NULLIF(name,''), email) AS label, email FROM users WHERE active ORDER BY 2")
            users = await cur.fetchall()

    rows = []
    for p in people:
        days = [0.0] * 7
        for e in entries:
            if e["person_id"] == p["id"]:
                days[(e["day"] - wk).days] += float(e["hours"])
        raw = sum(days)
        hours = round(raw, 2)
        rate = _n(p["rate"])
        p_adj = [dict(id=a["id"], label=a["label"], amount=_n(a["amount"])) for a in adjs if a["person_id"] == p["id"]]
        base = round(raw * rate, 2) if rate is not None else None   # exact minutes, as the old sheets did
        extra = round(sum(a["amount"] for a in p_adj), 2)
        rows.append(dict(id=p["id"], name=p["name"], user_id=p["user_id"], rate=rate, active=p["active"],
                         days=[round(x, 2) for x in days], hours=hours, base_pay=base, adjustments=p_adj,
                         pay=round((base or 0) + extra, 2) if base is not None or p_adj else None,
                         open=any(e["clock_out"] is None for e in entries if e["person_id"] == p["id"])))
    weeks = [(h0 + timedelta(weeks=i)).isoformat() for i in range(history_weeks)]
    history = {p["id"]: [0.0] * history_weeks for p in people}
    for h in hist:
        i = (h["wk"] - h0).days // 7
        if 0 <= i < history_weeks:
            history[h["person_id"]][i] = round(float(h["hours"]), 2)
    return jsonable(dict(
        owner=owner, today=today, week_start=wk, prev_week=wk - timedelta(weeks=1), next_week=wk + timedelta(weeks=1),
        me=dict(person_id=me["id"], name=me["name"], active=me["active"], open=dict(my_open) if my_open else None) if me else None,
        people=rows, entries=[dict(e, hours=round(float(e["hours"]), 2)) for e in entries],
        history=dict(weeks=weeks, series=[dict(id=p["id"], name=p["name"], hours=history[p["id"]]) for p in people]),
        users=[dict(u) for u in users]))


async def export_rows(start: date, end: date) -> list[dict]:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT p.name, {DAY} AS day, to_char(e.clock_in AT TIME ZONE '{TZ}', 'HH24:MI') AS clock_in, "
            f"to_char(e.clock_out AT TIME ZONE '{TZ}', 'HH24:MI') AS clock_out, round(({HOURS})::numeric, 2) AS hours, e.note, e.source "
            f"FROM time_entries e JOIN tc_people p ON p.id=e.person_id WHERE {DAY} BETWEEN %s AND %s ORDER BY lower(p.name), e.clock_in",
            (start, end))
        return [jsonable(dict(r)) for r in await cur.fetchall()]


# ---------------------------------------------------------------- one-off import from the old Google Sheets timesheets

async def import_sheet(body: dict) -> dict:
    """Engine only. {person, rate, entries: [{clock_in, clock_out, note}] (ISO strings, Central wall clock),
    adjustments: [{week_start, label, amount}]}. Replaces this person's earlier imported rows, so it can be re-run."""
    name = str(body.get("person") or "").strip()
    if not name:
        raise HTTPException(400, "person is required")
    actor = Actor(None, "Import", "import")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("INSERT INTO tc_people (name, rate) VALUES (%s,%s) ON CONFLICT (lower(name)) DO UPDATE "
                                     "SET rate=COALESCE(EXCLUDED.rate, tc_people.rate) RETURNING *", (name, _money(body.get("rate"))))
            p = await cur.fetchone()
            cur = await conn.execute("DELETE FROM time_entries WHERE person_id=%s AND source='import'", (p["id"],))
            removed = cur.rowcount
            await conn.execute("DELETE FROM tc_adjustments WHERE person_id=%s AND source='import'", (p["id"],))
            n = 0
            for e in body.get("entries") or []:
                await conn.execute(
                    f"INSERT INTO time_entries (person_id, clock_in, clock_out, note, source) VALUES "
                    f"(%s, %s::timestamp AT TIME ZONE '{TZ}', %s::timestamp AT TIME ZONE '{TZ}', %s, 'import')",
                    (p["id"], e["clock_in"], e["clock_out"], str(e.get("note") or "")))
                n += 1
            a = 0
            for x in body.get("adjustments") or []:
                await conn.execute("INSERT INTO tc_adjustments (person_id, week_start, label, amount, source) VALUES (%s,%s,%s,%s,'import')",
                                   (p["id"], parse_week(x["week_start"]), str(x["label"])[:80], _money(x["amount"])))
                a += 1
            await _audit(conn, actor, "import", "tc_person", None, None, None, {"name": name, "entries": n, "adjustments": a, "replaced": removed})
            await _notify(conn)
    return {"person_id": p["id"], "entries": n, "adjustments": a, "replaced": removed}
