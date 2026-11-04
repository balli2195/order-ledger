"""Reimbursements page (Nick, Nov 4 2026): inbound shipments with units Amazon didn't receive, grouped by shipment plan.

Refreshed daily by the Hub itself from SP-API:
- shipments   GET /fba/inbound/v0/shipments (DATE_RANGE, last ~150 days) -> status, FC, name. The plan is the name's
              timestamp ("FBA STA (08/04/2026 15:55)-MCI4"): Nick splits a plan into shipments to several FCs and
              Amazon nets units across the plan's shipments.
- items       GET /fba/inbound/v0/shipments/{id}/items -> shipped / received per SKU (one call returns everything).
- receipts    GET_LEDGER_DETAIL_VIEW_DATA (Receipts, Reference ID = shipment) -> first / last check-in, product titles.
              Closed date = last receipt (or the day the Hub first saw the shipment CLOSED).
- reimbursed  GET_FBA_REIMBURSEMENTS_DATA -> Lost_Inbound reimbursements with case IDs.
Case status comes from Amazon's case emails, read daily by Claude (the "Nightly hub refresh" task) and posted to
POST /api/engine/inbound/cases in the same shape as claude/inbound-nightly/decisions.json.

Claim window (Nick, Nov 4 2026): lost inbound is claimable up to 9 months after the shipment was delivered (first check-in;
setting reimb_window_months). Zero-received shipments have no check-in, so delivery is estimated as plan date + 30 days.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from . import db, sellable
from .leads import get_setting
from .orders import jsonable

log = logging.getLogger("ledger.inbound")
status: dict = {"last_run": None, "last_error": None, "shipments": 0}
LOOKBACK_DAYS = 330   # 9-month window + time to deliver
ACTIVE = ("SHIPPED", "IN_TRANSIT", "DELIVERED", "CHECKED_IN", "RECEIVING", "CLOSED")


def plan_of(name: str | None) -> tuple[str, date | None]:
    """'FBA STA (08/04/2026 15:55)-MCI4' -> ('08/04/2026 15:55', 2026-08-04)."""
    n = name or ""
    m = re.search(r"(\d\d)/(\d\d)/(\d{4})(?:\s+(\d\d:\d\d))?", n)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}" + (f" {m.group(4)}" if m.group(4) else ""), date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{4})-(\d\d)-(\d\d)(?:\s+(\d\d:\d\d))?", n)
    if m:
        return m.group(0), date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return n.strip() or "?", None


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    import calendar
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _day(v) -> date | None:
    v = (v or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(v[:10], fmt).date()
        except ValueError:
            pass
    return None


async def _get(c, path, params):
    for i in range(8):
        r = await c.get(sellable.SP + path, headers={"x-amz-access-token": await sellable._token(c)}, params=params)
        if r.status_code == 429:
            await asyncio.sleep(2 * (i + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{path}: {r.status_code} {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"{path}: rate limited")


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _download(c, doc_id: str) -> list[dict]:
    d = await _get(c, f"/reports/2021-06-30/documents/{doc_id}", {})
    raw = (await c.get(d["url"])).content
    if d.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace")), delimiter="\t"))


async def _report(c, rtype: str, start: datetime, end: datetime | None = None) -> list[dict]:
    # A report covering the same range made in the last 20 hours is reused (createReport has a small quota).
    since = (datetime.now(timezone.utc) - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    j = await _get(c, "/reports/2021-06-30/reports", {"reportTypes": rtype, "processingStatuses": "DONE", "createdSince": since, "pageSize": 50})
    want_end = end or datetime.now(timezone.utc)
    for x in j.get("reports", []):
        a, b = _ts(x.get("dataStartTime")), _ts(x.get("dataEndTime"))
        if a and b and a <= start + timedelta(days=1) and b >= want_end - timedelta(days=1) and x.get("reportDocumentId"):
            return await _download(c, x["reportDocumentId"])
    body = {"reportType": rtype, "marketplaceIds": [sellable.MP], "dataStartTime": start.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if end:
        body["dataEndTime"] = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(6):   # createReport has a small quota per report type: wait and retry
        r = await c.post(sellable.SP + "/reports/2021-06-30/reports", headers={"x-amz-access-token": await sellable._token(c)}, json=body)
        if r.status_code != 429:
            break
        await asyncio.sleep(60 * (i + 1))
    if r.status_code >= 400:
        raise RuntimeError(f"report {rtype}: {r.status_code} {r.text[:200]}")
    rid = r.json()["reportId"]
    for _ in range(120):
        await asyncio.sleep(15)
        j = await _get(c, f"/reports/2021-06-30/reports/{rid}", {})
        st = j.get("processingStatus")
        if st == "DONE":
            return await _download(c, j["reportDocumentId"])
        if st in ("CANCELLED", "FATAL"):
            return []   # CANCELLED = no data in the range
    raise RuntimeError(f"report {rtype} timed out")


async def _ledger(c, start: datetime, now: datetime) -> list[dict]:
    """Inventory ledger rows from start to now. Ledger reports made in the last 24 hours (by this Hub or by Claude's
    nightly run) are reused when together they cover the range; only the gaps get new 90-day reports, because
    Amazon allows very few new ledger reports per hour."""
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    have, p = [], {"reportTypes": "GET_LEDGER_DETAIL_VIEW_DATA", "processingStatuses": "DONE", "createdSince": since, "pageSize": 100}
    for _ in range(5):
        j = await _get(c, "/reports/2021-06-30/reports", p)
        for x in j.get("reports", []):
            a, b = _ts(x.get("dataStartTime")), _ts(x.get("dataEndTime"))
            if a and b and b - a > timedelta(days=2) and x.get("reportDocumentId"):
                have.append((a, b, x["reportDocumentId"]))
        if not j.get("nextToken"):
            break
        p = {"nextToken": j["nextToken"]}
    rows, cur, stop = [], start, now - timedelta(days=1)
    for _ in range(12):
        if cur >= stop:
            break
        cands = [h for h in have if h[0] <= cur + timedelta(days=1) and h[1] > cur + timedelta(days=1)]
        if cands:
            a, b, doc = max(cands, key=lambda h: h[1])
            rows += await _download(c, doc)
            cur = b
        else:
            nxt = min([h[0] for h in have if h[0] > cur] or [now])
            end = min(cur + timedelta(days=89), nxt + timedelta(days=1), now - timedelta(hours=1))
            rows += await _report(c, "GET_LEDGER_DETAIL_VIEW_DATA", cur, end)
            cur = end
    return rows


async def sync() -> dict:
    """Pull shipments, items, receipts and reimbursements from Amazon into the Hub."""
    if not sellable.ENABLED:
        return {"skipped": "SP-API keys not set"}
    now = datetime.now(timezone.utc)
    out = {"shipments": 0, "items_fetched": 0, "receipts": 0, "reimb": 0}
    async with httpx.AsyncClient(timeout=60) as c:
        # 1. shipments
        ships = {}
        p = {"MarketplaceId": sellable.MP, "QueryType": "DATE_RANGE",
             "LastUpdatedAfter": (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "LastUpdatedBefore": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "ShipmentStatusList": ",".join(ACTIVE + ("WORKING", "CANCELLED", "DELETED", "ERROR"))}
        for _ in range(100):
            pl = (await _get(c, "/fba/inbound/v0/shipments", p)).get("payload", {})
            n0 = len(ships)
            for s in pl.get("ShipmentData", []):
                ships[s["ShipmentId"]] = s
            nt = pl.get("NextToken")
            if not nt or len(ships) == n0:
                break
            p = {"MarketplaceId": sellable.MP, "QueryType": "NEXT_TOKEN", "NextToken": nt}
            await asyncio.sleep(0.6)
        async with db.pool.connection() as conn:
            cur = await conn.execute("SELECT shipment_id, status, items_at, closed_seen_at FROM inbound_shipments")
            have = {r["shipment_id"]: r for r in await cur.fetchall()}
            for sid, s in ships.items():
                pk, pd = plan_of(s.get("ShipmentName"))
                st = s.get("ShipmentStatus")
                await conn.execute(
                    """INSERT INTO inbound_shipments (shipment_id, name, plan_key, plan_created, fc, status, closed_seen_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s, NULL, now())
                       ON CONFLICT (shipment_id) DO UPDATE SET name=EXCLUDED.name, plan_key=EXCLUDED.plan_key, plan_created=EXCLUDED.plan_created,
                         fc=EXCLUDED.fc, status=EXCLUDED.status, updated_at=now(),
                         closed_seen_at=COALESCE(inbound_shipments.closed_seen_at,
                           CASE WHEN EXCLUDED.status = 'CLOSED' AND inbound_shipments.status <> 'CLOSED' THEN now() END)""",
                    (sid, s.get("ShipmentName"), pk, pd, s.get("DestinationFulfillmentCenterId"), st))
            await conn.commit()
        out["shipments"] = len(ships)
        # 2. items: shipments still moving every day; closed ones once more after they close
        todo = []
        for sid, s in ships.items():
            st = s.get("ShipmentStatus")
            if st not in ACTIVE:
                continue
            h = have.get(sid)
            if not h or not h["items_at"] or st != "CLOSED" or h["status"] != "CLOSED":
                todo.append(sid)
        for sid in todo:
            items = {}
            pp = {"MarketplaceId": sellable.MP}
            for _ in range(40):
                pl = (await _get(c, f"/fba/inbound/v0/shipments/{sid}/items", pp)).get("payload", {})
                n0 = len(items)
                for it in pl.get("ItemData", []):
                    items[it["SellerSKU"]] = it
                nt = pl.get("NextToken")
                if not nt or len(items) == n0:
                    break
                pp = {"MarketplaceId": sellable.MP, "NextToken": nt}
                await asyncio.sleep(0.6)
            async with db.pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute("DELETE FROM inbound_items WHERE shipment_id=%s", (sid,))
                    for it in items.values():
                        await conn.execute(
                            "INSERT INTO inbound_items (shipment_id, sku, fnsku, shipped, received) VALUES (%s,%s,%s,%s,%s)",
                            (sid, it["SellerSKU"], it.get("FulfillmentNetworkSKU"), int(it.get("QuantityShipped") or 0), int(it.get("QuantityReceived") or 0)))
                    await conn.execute("UPDATE inbound_shipments SET items_at=now() WHERE shipment_id=%s", (sid,))
            out["items_fetched"] += 1
            await asyncio.sleep(0.6)
        # 3. receipts from the inventory ledger (90-day windows)
        rec: dict = {}
        titles: dict = {}
        for row in await _ledger(c, now - timedelta(days=LOOKBACK_DAYS), now):
            if (row.get("Event Type") or "").strip() != "Receipts":
                continue
            sid = (row.get("Reference ID") or "").strip()
            d = _day(row.get("Date") or row.get("Date and Time"))
            if not d:
                continue
            a, b = rec.get(sid, (d, d))
            rec[sid] = (min(a, d), max(b, d))
            if row.get("MSKU") and row.get("ASIN"):
                titles[row["MSKU"].strip()] = (row["ASIN"].strip(), (row.get("FNSKU") or "").strip(), (row.get("Title") or "").strip()[:300])
        # 4. reimbursements
        rb = await _report(c, "GET_FBA_REIMBURSEMENTS_DATA", now - timedelta(days=LOOKBACK_DAYS + 30))
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for sid, (a, b) in rec.items():
                await conn.execute("UPDATE inbound_shipments SET first_receipt=%s, last_receipt=%s WHERE shipment_id=%s", (a, b, sid))
            for sku, (asin, fnsku, t) in titles.items():
                await conn.execute("INSERT INTO inbound_skus (sku, asin, fnsku, title) VALUES (%s,%s,%s,%s) "
                                   "ON CONFLICT (sku) DO UPDATE SET asin=EXCLUDED.asin, fnsku=EXCLUDED.fnsku, title=COALESCE(NULLIF(EXCLUDED.title,''), inbound_skus.title)",
                                   (sku, asin, fnsku, t))
            for r in rb:
                rid, sku = (r.get("reimbursement-id") or "").strip(), (r.get("sku") or "").strip()
                if not rid:
                    continue
                def num(k):
                    try:
                        return float((r.get(k) or "0").strip() or 0)
                    except ValueError:
                        return 0.0
                await conn.execute(
                    """INSERT INTO inbound_reimb (reimbursement_id, sku, fnsku, asin, case_id, reason, approval_date, amount, qty_cash, qty_inv, title)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (reimbursement_id, sku) DO UPDATE SET amount=EXCLUDED.amount, qty_cash=EXCLUDED.qty_cash, qty_inv=EXCLUDED.qty_inv,
                         case_id=EXCLUDED.case_id, reason=EXCLUDED.reason""",
                    (rid, sku, (r.get("fnsku") or "").strip(), (r.get("asin") or "").strip(), (r.get("case-id") or "").strip() or None,
                     (r.get("reason") or "").strip(), (r.get("approval-date") or "")[:10] or None, num("amount-total"),
                     int(num("quantity-reimbursed-cash")), int(num("quantity-reimbursed-inventory")), (r.get("product-name") or "")[:300]))
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "inbound", "op": "sync"}),))
    out["receipts"], out["reimb"] = len(rec), len(rb)
    return out


async def loop() -> None:
    """Once a day (and at start if the last refresh is over 20 hours old)."""
    if not sellable.ENABLED:
        return
    await asyncio.sleep(120)
    while True:
        try:
            async with db.pool.connection() as conn:
                cur = await conn.execute("SELECT value FROM settings WHERE key='inbound_synced_at'")
                r = await cur.fetchone()
            last = datetime.fromisoformat(r["value"]) if r and r["value"] else None
            if not last or datetime.now(timezone.utc) - last > timedelta(hours=20):
                res = await sync()
                async with db.pool.connection() as conn:
                    await conn.execute("INSERT INTO settings (key, value) VALUES ('inbound_synced_at', %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                                       (json.dumps(datetime.now(timezone.utc).isoformat()),))
                status.update(last_run=time.strftime("%Y-%m-%d %H:%M:%S"), last_error=None, result=res)
        except Exception as e:
            status["last_error"] = str(e)[:300]
            log.exception("inbound sync failed")
        await asyncio.sleep(1800)


async def save_cases(data: dict) -> dict:
    """Engine: {SID: {case, st: pending|decided, at, opened, reimb: [ids], deny: {fnsku: [units, note]}, rereview?, note?, link?}}
    (the decisions.json shape). Replaces each shipment's case record."""
    n = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for sid, v in (data or {}).items():
                if not re.fullmatch(r"FBA[0-9A-Z]{6,12}", str(sid)) or not isinstance(v, dict):
                    continue
                await conn.execute(
                    """INSERT INTO inbound_cases (shipment_id, case_id, status, opened, decided, data, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s::jsonb, now())
                       ON CONFLICT (shipment_id) DO UPDATE SET case_id=EXCLUDED.case_id, status=EXCLUDED.status, opened=EXCLUDED.opened,
                         decided=EXCLUDED.decided, data=EXCLUDED.data, updated_at=now()""",
                    (sid, str(v.get("case") or "") or None, v.get("st") or "pending", (v.get("opened") or None),
                     (v.get("at") if v.get("st") == "decided" else None), json.dumps(v)))
                n += 1
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"t": "inbound", "op": "cases"}),))
    return {"cases": n}


async def page(include_all: bool = False) -> dict:
    months = int(await get_setting("reimb_window_months", 9) or 9)
    chat = await get_setting("reimb_chat_url", None)
    today = datetime.now().astimezone().date()
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT * FROM inbound_shipments WHERE status = ANY(%s) AND (plan_created IS NULL OR plan_created >= current_date - %s)",
            (list(ACTIVE), LOOKBACK_DAYS + 30))
        ships = {r["shipment_id"]: dict(r) for r in await cur.fetchall()}
        cur = await conn.execute("SELECT * FROM inbound_items WHERE shipment_id = ANY(%s)", (list(ships),))
        items = await cur.fetchall()
        cur = await conn.execute("SELECT * FROM inbound_cases")
        cases = {r["shipment_id"]: dict(r) for r in await cur.fetchall()}
        cur = await conn.execute("SELECT * FROM inbound_reimb WHERE reason ILIKE 'Lost_Inbound%%'")
        reimb = [dict(r) for r in await cur.fetchall()]
        skus = list({i["sku"] for i in items})
        cur = await conn.execute("SELECT sku, asin, title FROM inbound_skus WHERE sku = ANY(%s)", (skus,))
        info = {r["sku"]: r for r in await cur.fetchall()}
        sku_asin = {k: v["asin"] for k, v in info.items() if v["asin"]}
        titles = {k: v["title"] for k, v in info.items() if v["title"]}
        for r in reimb:   # reimbursements also name the SKU's ASIN
            if r["sku"] and r["asin"]:
                sku_asin.setdefault(r["sku"], r["asin"])
                if r.get("title"):
                    titles.setdefault(r["sku"], r["title"])
        asins = list(set(sku_asin.values()))
        cur = await conn.execute(
            """SELECT upper(btrim(i.asin)) asin, avg(i.cost_per_unit) c FROM order_items i JOIN orders o ON o.id=i.order_id
                WHERE upper(btrim(i.asin)) = ANY(%s) AND i.cost_per_unit > 0 AND o.order_date > current_date - 400 GROUP BY 1""", (asins,))
        cost = {r["asin"]: float(r["c"]) for r in await cur.fetchall()}
        cur = await conn.execute(
            """SELECT DISTINCT ON (upper(btrim(asin))) upper(btrim(asin)) asin, cost FROM leads
                WHERE upper(btrim(asin)) = ANY(%s) AND cost > 0 ORDER BY upper(btrim(asin)), lead_date DESC""", (asins,))
        for r in await cur.fetchall():
            cost.setdefault(r["asin"], float(r["cost"]))
    by_ship: dict = {}
    for i in items:
        by_ship.setdefault(i["shipment_id"], []).append(i)
    rb_by_id: dict = {}
    for r in reimb:
        rb_by_id.setdefault(r["reimbursement_id"], []).append(r)
        if r["case_id"]:
            rb_by_id.setdefault("case:" + r["case_id"], []).append(r)
    plans: dict = {}
    for sid, s in ships.items():
        plans.setdefault(s["plan_key"], []).append(s)
    out = []
    for pk, ss in plans.items():
        agg: dict = {}
        shp = []
        for s in sorted(ss, key=lambda x: x["shipment_id"]):
            sid = s["shipment_id"]
            rows = by_ship.get(sid, [])
            shipped, received = sum(r["shipped"] for r in rows), sum(r["received"] for r in rows)
            short = sum(max(0, r["shipped"] - r["received"]) for r in rows)
            closed = None
            if s["status"] == "CLOSED":
                closed = s["last_receipt"] or (s["closed_seen_at"].date() if s["closed_seen_at"] else None)
            delivered, est = s["first_receipt"], False
            if not delivered and s["status"] in ("CLOSED", "RECEIVING", "CHECKED_IN", "DELIVERED") and s["plan_created"]:
                delivered, est = s["plan_created"] + timedelta(days=30), True   # nothing checked in: estimate
            deadline = _add_months(delivered, months) if delivered else None
            cs = cases.get(sid)
            cdata = (cs or {}).get("data") or {}
            ids = list(cdata.get("reimb") or [])
            if cdata.get("rereview"):
                ids += list(cdata["rereview"].get("reimb") or [])
            paid_rows = []
            seen = set()
            for k in ids + (["case:" + cs["case_id"]] if cs and cs.get("case_id") else []):
                for r in rb_by_id.get(k, []):
                    key = (r["reimbursement_id"], r["sku"])
                    if key not in seen:
                        seen.add(key)
                        paid_rows.append(r)
            denied = sum(int(v[0]) for v in (cdata.get("deny") or {}).values() if isinstance(v, list) and v)
            case = None
            if cs:
                case = {"id": cs["case_id"], "status": cs["status"], "opened": cs["opened"], "decided": cs["decided"],
                        "paid": round(sum(float(r["amount"] or 0) for r in paid_rows), 2),
                        "paid_units": sum((r["qty_cash"] or 0) + (r["qty_inv"] or 0) for r in paid_rows),
                        "denied_units": denied, "rereview": (cdata.get("rereview") or {}).get("case"),
                        "deny": [{"fnsku": k, "units": v[0], "note": v[1] if len(v) > 1 else ""} for k, v in (cdata.get("deny") or {}).items()]}
            shp.append({"id": sid, "fc": s["fc"], "status": s["status"], "shipped": shipped, "received": received, "short": short,
                        "delivered": delivered, "delivered_est": est, "closed": closed, "deadline": deadline,
                        "days_left": (deadline - today).days if deadline else None, "case": case})
            for r in rows:
                g = agg.setdefault(r["sku"], {"sku": r["sku"], "fnsku": r["fnsku"], "shipped": 0, "received": 0, "by": {}})
                g["shipped"] += r["shipped"]
                g["received"] += r["received"]
                g["by"][sid] = [r["shipped"], r["received"]]
        paid_by_sku: dict = {}
        for s in ss:
            cs = cases.get(s["shipment_id"])
            if not cs:
                continue
            cdata = cs.get("data") or {}
            for k in list(cdata.get("reimb") or []) + (["case:" + cs["case_id"]] if cs.get("case_id") else []):
                for r in rb_by_id.get(k, []):
                    paid_by_sku[(r["sku"], r["reimbursement_id"])] = (r["sku"], (r["qty_cash"] or 0) + (r["qty_inv"] or 0))
        paid_units_sku: dict = {}
        for sku, q in paid_by_sku.values():
            paid_units_sku[sku] = paid_units_sku.get(sku, 0) + q
        sku_rows = []
        missing = missing_v = extra = 0
        for g in agg.values():
            miss = max(0, g["shipped"] - g["received"])
            ex = max(0, g["received"] - g["shipped"])
            asin = sku_asin.get(g["sku"])
            c = cost.get(asin) if asin else None
            missing += miss
            extra += ex
            missing_v += miss * (c or 0)
            if miss or ex:
                sku_rows.append({"sku": g["sku"], "fnsku": g["fnsku"], "asin": asin, "title": titles.get(g["sku"]),
                                 "shipped": g["shipped"], "received": g["received"], "missing": miss, "extra": ex, "cost": c,
                                 "paid_units": paid_units_sku.get(g["sku"], 0), "by": g["by"]})
        sku_rows.sort(key=lambda r: (-(r["missing"] * (r["cost"] or 0)), -r["missing"]))
        all_closed = all(s["status"] == "CLOSED" for s in shp)
        unfiled = [s for s in shp if not s["case"] and s["short"] > 0]
        open_deadlines = [s["deadline"] for s in unfiled if s["status"] == "CLOSED" and s["deadline"] and s["deadline"] >= today]
        pending = [s for s in shp if s["case"] and s["case"]["status"] != "decided"]
        decided = [s for s in shp if s["case"] and s["case"]["status"] == "decided"]
        if missing == 0 and not any(s["case"] for s in shp):
            continue
        if pending:
            state = "case_open"
        elif open_deadlines:
            state = "eligible"
        elif unfiled and not all_closed and any(s["status"] != "CLOSED" for s in unfiled):
            state = "receiving"
        elif decided and not unfiled:
            state = "decided"
        elif unfiled and not open_deadlines:
            state = "expired" if all_closed else "receiving"
        else:
            state = "decided" if decided else "none"
        out.append({
            "plan": pk, "created": ss[0]["plan_created"], "shipments": shp, "shipped": sum(s["shipped"] for s in shp),
            "received": sum(s["received"] for s in shp), "missing": missing, "missing_value": round(missing_v, 2), "extra": extra,
            "paid": round(sum((s["case"] or {}).get("paid", 0) for s in shp), 2),
            "paid_units": sum((s["case"] or {}).get("paid_units", 0) for s in shp),
            "denied_units": sum((s["case"] or {}).get("denied_units", 0) for s in shp),
            "deadline": min(open_deadlines) if open_deadlines else None,
            "days_left": (min(open_deadlines) - today).days if open_deadlines else None,
            "state": state, "skus": sku_rows, "unfiled": len(unfiled),
            "to_file": sum(1 for x in unfiled if x["status"] == "CLOSED" and x["deadline"] and x["deadline"] >= today)})
    order = {"eligible": 0, "case_open": 1, "receiving": 2, "decided": 3, "expired": 4, "none": 5}
    out.sort(key=lambda p: (order.get(p["state"], 9), p["deadline"] or date(2100, 1, 1), -(p["missing_value"] or 0)))
    if not include_all:
        out = [p for p in out if p["state"] in ("eligible", "case_open", "receiving")]
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT value FROM settings WHERE key='inbound_synced_at'")
        r = await cur.fetchone()
    return jsonable({"plans": out, "chat": chat, "window_months": months, "synced_at": r["value"] if r else None,
                     "status": {k: v for k, v in status.items() if k != "result"}})
