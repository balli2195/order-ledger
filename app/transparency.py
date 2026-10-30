"""Amazon Transparency enrollment per ASIN (Nick, Oct 30 2026).

Brands that enroll an ASIN in Transparency require a Transparency (or serial number) code on every unit. Nick buys
retail, so his units don't have them: units sent in are marked unfulfillable at the warehouse. Don't send them in.

SP-API has no call that tells a reseller whether an ASIN is enrolled, so the list comes from Amazon's emails to Nick:
- "Important: Serial numbers required for certain items" (TPNCY_3P_NOTIFICATION_FBA): ASINs the brand just enrolled,
  with the date after which units without codes can't be sold.
- "Action Required: Transparency Code Requirements - Unsellable Inventory MM/DD - MM/DD" (TRANSPARENCY_FC_FAILURES_3P):
  units already rejected at fulfillment centers that week.
- "Action Required: Listing Removed from Amazon" / "Important message about your Amazon Seller account" (kind removed):
  Amazon removed the listing or set the inventory aside because units lacked Transparency codes.
A scheduled Claude run reads those emails and posts them to /api/engine/transparency. One row per ASIN per email
(transparency_notices), so re-posting the same email changes nothing.
"""
from __future__ import annotations

import json
import re
from datetime import date

from . import db

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


class TransparencyError(ValueError):
    pass


def _d(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise TransparencyError(f"bad date {v!r}")


async def add_notices(notices: list[dict], source: str = "email") -> dict:
    rows = []
    for n in notices or []:
        a = (n.get("asin") or "").strip().upper()
        if not ASIN_RE.match(a):
            raise TransparencyError(f"bad ASIN {n.get('asin')!r}")
        kind = n.get("kind") or "enrolled"
        if kind not in ("enrolled", "rejected", "removed"):
            raise TransparencyError(f"kind must be enrolled, rejected or removed, not {kind!r}")
        ref = (n.get("thread_id") or n.get("ref") or "").strip()
        if not ref:
            raise TransparencyError(f"{a}: thread_id is required")
        units = n.get("units_rejected")
        rows.append((a, ref, kind, _d(n.get("email_date")), _d(n.get("effective_date")),
                     int(units) if units not in (None, "") else None, (n.get("title") or "").strip()[:300] or None, source))
    new_asins = set()
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for r in rows:
                cur = await conn.execute(
                    "INSERT INTO transparency_notices (asin, ref, kind, email_date, effective_date, units_rejected, title, source) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (asin, ref) DO UPDATE SET kind=EXCLUDED.kind, "
                    "email_date=COALESCE(EXCLUDED.email_date, transparency_notices.email_date), "
                    "effective_date=COALESCE(EXCLUDED.effective_date, transparency_notices.effective_date), "
                    "units_rejected=COALESCE(EXCLUDED.units_rejected, transparency_notices.units_rejected), "
                    "title=COALESCE(EXCLUDED.title, transparency_notices.title) RETURNING (xmax = 0) AS inserted", r)
                if (await cur.fetchone())["inserted"]:
                    new_asins.add(r[0])
            asins = sorted({r[0] for r in rows})
            if asins:
                cur = await conn.execute("SELECT id FROM leads WHERE upper(btrim(asin)) = ANY(%s) ORDER BY lead_date DESC NULLS LAST LIMIT 50", (asins,))
                ids = [str(x["id"]) for x in await cur.fetchall()]
                if ids:
                    await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "lead", "op": "upsert", "ids": ids, "src": "transparency"}),))
    return {"notices": len(rows), "asins": len({r[0] for r in rows}), "new_notices_for": sorted(new_asins)}


SUMMARY_SQL = (
    "SELECT asin, (array_agg(title ORDER BY email_date DESC NULLS LAST) FILTER (WHERE title IS NOT NULL))[1] AS title, "
    "min(email_date) AS first_email, max(email_date) AS last_email, min(effective_date) AS effective_date, "
    "bool_or(kind='enrolled') AS enrolled_notice, COALESCE(sum(units_rejected) FILTER (WHERE kind='rejected'),0) AS units_rejected, "
    "count(*) AS notices FROM transparency_notices"
)


def _j(r):
    d = dict(r)
    for k, v in d.items():
        if isinstance(v, date):
            d[k] = v.isoformat()
    return d


async def get(asin: str) -> dict | None:
    a = (asin or "").strip().upper()
    async with db.pool.connection() as conn:
        cur = await conn.execute(SUMMARY_SQL + " WHERE asin=%s GROUP BY asin", (a,))
        r = await cur.fetchone()
        if not r:
            return None
        out = _j(r)
        cur = await conn.execute("SELECT kind, email_date, effective_date, units_rejected, ref FROM transparency_notices "
                                 "WHERE asin=%s ORDER BY email_date DESC NULLS LAST", (a,))
        out["emails"] = [_j(x) for x in await cur.fetchall()]
    return out


async def list_all() -> dict:
    async with db.pool.connection() as conn:
        cur = await conn.execute(SUMMARY_SQL + " GROUP BY asin ORDER BY max(email_date) DESC NULLS LAST, asin")
        rows = [_j(r) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT max(email_date) d, count(DISTINCT ref) n FROM transparency_notices WHERE source='email'")
        m = await cur.fetchone()
    return {"asins": rows, "latest_email": m["d"].isoformat() if m["d"] else None, "emails": m["n"]}
