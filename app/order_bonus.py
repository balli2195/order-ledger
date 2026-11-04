"""Order bonus days (Nick, Nov 4 2026): "pay Abby a bonus today for every $1000 ordered, $5 per $1000".

settings order_bonus = [{user, date, per, pay}, ...]. On that day (Central time) the person earns `pay` for every full
`per` dollars of orders they placed (orders.created_by = their login, order_date = that day, canceled orders left out).
The person and the owner see a big bonus figure that celebrates each new milestone (static/bonus.js).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from . import db
from .auth import User
from .leads import get_setting


async def today(user: User) -> dict | None:
    day = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    cfg = [c for c in (await get_setting("order_bonus", []) or []) if str(c.get("date")) == day]
    me = (user.name or "").lower()
    cfg = [c for c in cfg if user.role == "owner" or str(c.get("user", "")).lower() == me]
    if not cfg:
        return None
    c = cfg[0]
    per, pay = float(c.get("per") or 1000), float(c.get("pay") or 0)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT COALESCE(sum(o.order_total), 0) AS total, count(*) AS n FROM orders o
                WHERE o.order_date = %s::date AND NOT COALESCE(o.confirm_info ? 'canceled', false)
                  AND o.created_by IN (SELECT id FROM users WHERE lower(name) = lower(%s))""",
            (day, c.get("user")))
        r = await cur.fetchone()
    total = float(r["total"])
    steps = int(total // per)
    return {"user": c.get("user"), "date": day, "per": per, "pay": pay, "total": round(total, 2), "orders": r["n"],
            "milestones": steps, "bonus": round(steps * pay, 2), "next_at": (steps + 1) * per,
            "to_next": round((steps + 1) * per - total, 2), "self": user.role != "owner"}
