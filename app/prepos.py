"""Send order items to PrepOS (Network Prep's software) as one line per ASIN.

Rules (Nick, Oct 29, 2026):
- Orders dated on/after settings.prepos_start (Oct 29, 2026) are sent.
- Nightly at 11:45 PM Central, with a 2-day lag (settings.prepos_lag_days): the run on day D sends unsent items from
  orders dated up to D-2, so orders cancelled (deleted here) in the meantime never reach PrepOS.
- Every unsent item of the same ASIN is combined into one line: qty summed, cost = weighted average cost per unit.
- PrepOS lines can't be edited through the API, so an ASIN gets at most one line per day: if an ASIN was already sent
  today (button or nightly), its new items wait for the next run.
- Sell = Buy Box (or the last Buy Box Keepa saw) + 10%.
- Multi-Pack comes from the item's set type: "2-pack" -> 2, "3-pack" -> 3, ... (Single, Split set, Bundle -> blank).
  Items are combined per ASIN and pack size, so a 2-pack and singles of one ASIN are separate lines.
- A "Send now" button sends early, through any date up to today.

PrepOS API: POST {PREPOS_URL}/items/import, multipart field `products` = the same CSV as the manual import.
Env: PREPOS_TOKEN (Bearer), optional PREPOS_URL.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import db
from .leads import KEEPA_KEY, get_setting

log = logging.getLogger("ledger.prepos")
URL = os.environ.get("PREPOS_URL", "https://prepos-production-c65e.up.railway.app/api/v1").rstrip("/")
TOKEN = os.environ.get("PREPOS_TOKEN", "")
TZ = ZoneInfo("America/Chicago")
RUN_AT = (23, 45)
HEADER = ["Platform", "Title", "ASIN or ID", "Inbound", "Cost", "Sell", "Supplier", "Multi-Pack", "Comments", "Link", "Order #"]
_lock = asyncio.Lock()
status: dict = {"last_error": None}


class PreposError(Exception):
    pass


def today() -> date:
    return datetime.now(TZ).date()


async def config() -> dict:
    start = date.fromisoformat(await get_setting("prepos_start", "2026-10-29"))
    lag = int(await get_setting("prepos_lag_days", 2))
    return {"start": start, "lag": lag, "default_through": today() - timedelta(days=lag), "enabled": bool(TOKEN)}


async def pending(through: date) -> dict:
    """Unsent items from orders dated start..through, combined into one line per ASIN."""
    cfg = await config()
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT i.id, upper(trim(i.asin)) AS asin, i.product_title, i.qty_ordered, i.cost_per_unit, i.set_type,
                      o.id AS order_id, o.store, o.order_ref, o.order_date
               FROM order_items i JOIN orders o ON o.id = i.order_id
               LEFT JOIN prepos_sent_items s ON s.item_id = i.id
               WHERE s.item_id IS NULL AND o.order_date >= %s AND o.order_date <= %s AND i.qty_ordered > 0
               ORDER BY o.order_date, o.created_at, i.created_at""",
            (cfg["start"], through),
        )
        rows = await cur.fetchall()
        cur = await conn.execute("SELECT DISTINCT asin, multipack FROM prepos_sends WHERE send_day=%s AND status='sent'", (today(),))
        sent_today = {(r["asin"], r["multipack"]) for r in await cur.fetchall()}
    groups: dict[tuple, list] = defaultdict(list)
    no_asin = []
    for r in rows:
        if r["asin"] and len(r["asin"]) == 10:
            groups[(r["asin"], pack_size(r["set_type"]))].append(r)
        else:
            no_asin.append({"order_id": str(r["order_id"]), "store": r["store"], "order_ref": r["order_ref"],
                            "order_date": r["order_date"].isoformat(), "title": r["product_title"], "qty": r["qty_ordered"]})
    lines, held = [], []
    for (asin, pack), items in groups.items():
        qty = sum(i["qty_ordered"] for i in items)
        costed = [i for i in items if i["cost_per_unit"] is not None]
        cu = sum(i["qty_ordered"] for i in costed)
        cost = round(float(sum(i["qty_ordered"] * i["cost_per_unit"] for i in costed)) / cu, 2) if cu else None
        stores = Counter()
        for i in items:
            stores[i["store"]] += i["qty_ordered"]
        titles = Counter(i["product_title"].strip() for i in items if (i["product_title"] or "").strip())
        refs = list(dict.fromkeys(i["order_ref"] for i in items if i["order_ref"]))
        line = {
            "asin": asin, "multipack": pack, "title": titles.most_common(1)[0][0] if titles else None, "qty": qty, "unit_cost": cost,
            "cost_missing_units": qty - cu, "supplier": " + ".join(s for s, _ in stores.most_common()),
            "order_refs": refs, "orders": len({str(i["order_id"]) for i in items}),
            "first_date": items[0]["order_date"].isoformat(), "last_date": items[-1]["order_date"].isoformat(),
            "item_ids": [str(i["id"]) for i in items],
        }
        (held if (asin, pack) in sent_today else lines).append(line)
    lines.sort(key=lambda x: (x["first_date"], x["supplier"], x["asin"]))
    return {"through": through.isoformat(), "lines": lines, "held": held, "no_asin": no_asin}


async def keepa_prices(asins: list[str]) -> dict[str, dict]:
    """ASIN -> {bb: Buy Box now or last seen (dollars), title}. One Keepa call per 100 ASINs."""
    out: dict[str, dict] = {}
    if not KEEPA_KEY or not asins:
        return out
    async with httpx.AsyncClient(timeout=120) as c:
        for k in range(0, len(asins), 100):
            chunk = asins[k:k + 100]
            try:
                r = await c.get("https://api.keepa.com/product", params={
                    "key": KEEPA_KEY, "domain": 1, "asin": ",".join(chunk), "buybox": 1, "history": 1, "days": 180, "stats": 1})
                prods = r.json().get("products") or []
            except Exception as e:
                log.warning("keepa prices failed: %s", e)
                continue
            for p in prods:
                st = p.get("stats") or {}
                bb = None
                now = st.get("buyBoxPrice")
                if now and now > 0:
                    bb = now + max(0, st.get("buyBoxShipping") or 0)
                else:
                    h = (p.get("csv") or [None] * 19)[18] if len(p.get("csv") or []) > 18 else None
                    if h:
                        for j in range(len(h) - 3, -1, -3):   # [time, price, shipping] triples, newest last
                            if h[j + 1] is not None and h[j + 1] > 0:
                                bb = h[j + 1] + max(0, h[j + 2] or 0)
                                break
                out[p.get("asin")] = {"bb": round(bb / 100, 2) if bb else None, "title": p.get("title")}
    return out


def pack_size(set_type: str | None) -> int | None:
    """'2-pack' / '2 pack' / '2pk' -> 2; anything else (Single, Split set, Bundle, blank) -> None."""
    m = re.match(r"^\s*(\d{1,2})\s*-?\s*(pack|pk)\s*$", set_type or "", re.I)
    n = int(m.group(1)) if m else None
    return n if n and n >= 2 else None


def to_csv(lines: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    for x in lines:
        w.writerow(["AMZ FBA", x["title"] or "", x["asin"], x["qty"],
                    "" if x["unit_cost"] is None else f"{x['unit_cost']:.2f}",
                    "" if x.get("sell") is None else f"{x['sell']:.2f}",
                    x["supplier"], x.get("multipack") or "", "", "", ", ".join(x["order_refs"])])
    return buf.getvalue().encode("utf-8-sig")


async def upload(data: bytes) -> tuple[int, object]:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{URL}/items/import", headers={"Authorization": f"Bearer {TOKEN}"},
                         files={"products": ("myrddin-hub.csv", data, "text/csv")})
    try:
        body = r.json()
    except Exception:
        body = r.text[:2000]
    return r.status_code, body


async def send(through: date, kind: str, user_id: int | None = None) -> dict:
    """Combine and upload everything pending through `through`. Returns what was sent."""
    if not TOKEN:
        raise PreposError("PrepOS isn't connected (PREPOS_TOKEN is not set)")
    if through > today():
        raise PreposError("Can't send orders dated after today")
    async with _lock:
        async with db.pool.connection() as conn:   # one sender at a time, even across restarts overlapping
            cur = await conn.execute("SELECT pg_try_advisory_lock(424242) AS ok")
            if not (await cur.fetchone())["ok"]:
                raise PreposError("A PrepOS send is already running")
            try:
                p = await pending(through)
                lines = p["lines"]
                if not lines:
                    return {"sent": 0, "units": 0, "lines": [], "held": p["held"], "no_asin": p["no_asin"]}
                info = await keepa_prices([x["asin"] for x in lines])
                for x in lines:
                    k = info.get(x["asin"]) or {}
                    x["sell"] = round(k["bb"] * 1.10, 2) if k.get("bb") else None
                    x["bb"] = k.get("bb")
                    if not x["title"]:
                        x["title"] = k.get("title")
                try:
                    code, body = await upload(to_csv(lines))
                except httpx.HTTPError as e:   # nothing reached PrepOS; the items stay pending
                    status["last_error"] = f"Couldn't reach PrepOS: {e}"
                    raise PreposError(f"Couldn't reach PrepOS ({e.__class__.__name__}); nothing was sent")
                ok = 200 <= code < 300
                batch, day = uuid.uuid4(), today()
                async with conn.transaction():
                    for x in lines:
                        cur = await conn.execute(
                            """INSERT INTO prepos_sends (batch, send_day, kind, user_id, asin, multipack, title, qty, unit_cost, sell, supplier,
                                   order_refs, first_date, last_date, status, response)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                            (batch, day, kind, user_id, x["asin"], x["multipack"], x["title"], x["qty"], x["unit_cost"], x["sell"], x["supplier"],
                             ", ".join(x["order_refs"]), x["first_date"], x["last_date"], "sent" if ok else "failed",
                             json.dumps({"code": code, "body": body})),
                        )
                        sid = (await cur.fetchone())["id"]
                        if ok:
                            for iid in x["item_ids"]:
                                await conn.execute("INSERT INTO prepos_sent_items (item_id, send_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (iid, sid))
                    await conn.execute(
                        "INSERT INTO audit_log (user_id, source, action, entity, after) VALUES (%s,%s,'create','prepos',%s)",
                        (user_id, "web" if kind == "button" else "system",
                         json.dumps({"lines": len(lines), "units": sum(x["qty"] for x in lines), "through": through.isoformat(),
                                     "kind": kind, "ok": ok, "code": code})),
                    )
                await conn.commit()   # keep the record even when the upload was refused
                if not ok:
                    status["last_error"] = f"PrepOS answered {code}: {str(body)[:300]}"
                    raise PreposError(f"PrepOS didn't accept the upload ({code}): {str(body)[:300]}")
                status["last_error"] = None
                log.info("prepos %s: %d lines, %d units through %s", kind, len(lines), sum(x["qty"] for x in lines), through)
                for x in lines:
                    x.pop("item_ids", None)
                return {"sent": len(lines), "units": sum(x["qty"] for x in lines), "lines": lines, "held": p["held"],
                        "no_asin": p["no_asin"], "response": body}
            finally:
                await conn.execute("SELECT pg_advisory_unlock(424242)")


async def history(limit: int = 300) -> list[dict]:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT s.*, COALESCE(NULLIF(u.name,''), u.email) AS who FROM prepos_sends s
               LEFT JOIN users u ON u.id = s.user_id ORDER BY s.id DESC LIMIT %s""", (limit,))
        rows = await cur.fetchall()
    out = []
    for r in rows:
        d = {k: (v.isoformat() if hasattr(v, "isoformat") else float(v) if k in ("unit_cost", "sell") and v is not None else v)
             for k, v in dict(r).items() if k != "response"}
        d["batch"] = str(r["batch"])
        out.append(d)
    return out


async def nightly_loop() -> None:
    if not TOKEN:
        log.info("PREPOS_TOKEN not set; PrepOS sending off")
        return
    while True:
        try:
            now = datetime.now(TZ)
            day = now.date().isoformat()
            if (now.hour, now.minute) >= RUN_AT and await get_setting("prepos_last_nightly", None) != day:
                cfg = await config()
                tries = status.setdefault("tries", {}); tries[day] = tries.get(day, 0) + 1
                done = False
                try:
                    res = await send(cfg["default_through"], "nightly")
                    log.info("prepos nightly: %s lines", res["sent"])
                    done = True
                finally:
                    if done or tries[day] >= 3:   # a refused upload is retried twice, a few minutes apart
                      async with db.pool.connection() as conn:
                        await conn.execute(
                            "INSERT INTO settings (key, value) VALUES ('prepos_last_nightly', %s) "
                            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (json.dumps(day),))
                        await conn.commit()
        except Exception as e:
            status["last_error"] = str(e)
            log.exception("prepos nightly failed")
        await asyncio.sleep(120)
