"""Commissions page (Nick, Nov 4 2026).

Ariane isn't paid hourly: he earns a commission (settings bonus_rate, 1%) of the actual spend (cost x units bought) on
leads he sourced. A lead counts only if he also added it himself: if Nick (or anyone else) added the lead, it doesn't
count, even when Ariane sourced that product at an earlier date. Leads copied from Airtable carry who added them in
sourced_by; leads added on the Hub also need created_by to be his login (leads.COMMISSION_COND).

Weeks (Mon-Sun, Nick Nov 4 2026: shown weekly) go by the lead's date. Only weeks from settings commission_start (Oct 19, 2026) on are reported. The owner sees the bonus sourcer's commissions; anyone else sees only their own.
"""
from __future__ import annotations

from . import db
from .auth import User
from .leads import COMMISSION_COND, bonus_params, get_setting
from .orders import jsonable

# Leads with his name that were added by someone else: shown so it's clear why they don't count.
OTHER_COND = ("(l.sourced_by = %s AND l.created_by IS NOT NULL AND l.created_by NOT IN "
              "(SELECT u.id FROM users u WHERE lower(u.name) = lower(%s)))")


async def _who(user: User) -> tuple[float, str]:
    rate, sourcer = await bonus_params()
    return rate, (sourcer if user.role == "owner" else (user.name or user.email))


async def _start() -> str:
    return str(await get_setting("commission_start", "2026-10-19"))


async def summary(user: User) -> dict:
    rate, who = await _who(user)
    start = await _start()
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT to_char(date_trunc('week', l.lead_date), 'YYYY-MM-DD') AS week,
                       count(*) FILTER (WHERE {COMMISSION_COND}) AS leads,
                       count(*) FILTER (WHERE {COMMISSION_COND} AND COALESCE(l.qty_ordered, 0) > 0) AS bought,
                       COALESCE(sum(l.qty_ordered) FILTER (WHERE {COMMISSION_COND}), 0) AS units,
                       COALESCE(sum(l.actual_spend) FILTER (WHERE {COMMISSION_COND}), 0) AS spend,
                       count(*) FILTER (WHERE {OTHER_COND} AND COALESCE(l.qty_ordered, 0) > 0) AS other_n,
                       COALESCE(sum(l.actual_spend) FILTER (WHERE {OTHER_COND}), 0) AS other_spend
                  FROM leads l
                 WHERE l.sourced_by = %s AND l.lead_date >= %s::date
                 GROUP BY 1 ORDER BY 1 DESC""",
            [who, who] * 6 + [who, start])
        weeks = []
        for r in await cur.fetchall():
            d = jsonable(dict(r))
            d["commission"] = round(float(r["spend"]) * rate, 2)
            weeks.append(d)
        cur = await conn.execute(
            "SELECT to_char(date_trunc('week', (now() AT TIME ZONE 'America/Chicago')::date), 'YYYY-MM-DD') AS w")
        this_week = (await cur.fetchone())["w"]
    total = round(sum(w["commission"] for w in weeks), 2)
    return {"who": who, "rate": rate, "owner": user.role == "owner", "this_week": this_week, "start": start,
            "weeks": weeks, "total": total, "total_spend": round(sum(float(w["spend"]) for w in weeks), 2)}


async def week(user: User, start: str) -> dict:
    """One week's bought leads; start is the Monday (YYYY-MM-DD)."""
    rate, who = await _who(user)
    first = await _start()
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT l.id, l.lead_date, l.asin, l.product_name, l.stores, l.cost, l.buy_qty, l.qty_ordered, l.actual_spend,
                       {COMMISSION_COND} AS counts, cu.name AS added_by
                  FROM leads l LEFT JOIN users cu ON cu.id = l.created_by
                 WHERE l.sourced_by = %s AND l.lead_date >= %s::date AND l.lead_date < (%s::date + 7) AND l.lead_date >= %s::date
                   AND COALESCE(l.qty_ordered, 0) > 0
                 ORDER BY {COMMISSION_COND} DESC, l.lead_date DESC, l.actual_spend DESC NULLS LAST""",
            [who, who, who, start, start, first, who, who])
        rows = []
        for r in await cur.fetchall():
            d = jsonable(dict(r))
            d["commission"] = round(float(r["actual_spend"] or 0) * rate, 2) if r["counts"] else 0
            rows.append(d)
    return {"week": start, "who": who, "rate": rate, "leads": rows}
