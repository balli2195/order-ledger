"""Profit page (Nick, Nov 8 2026): Amazon P&L that updates through the day instead of waiting for the bookkeeper's close.

Amazon side
- Source: SP-API Finances v0 listFinancialEvents, the same posted transactions Link My Books books into QuickBooks.
  September 2026 rebuilt this way tied to QuickBooks to the cent on every Amazon.com line.
- Days are Pacific calendar days of the posting date (Amazon's marketplace time). Each day is pulled as its own
  PostedAfter/PostedBefore window, so fee events that carry no date (storage, removal, inbound) land on the window's day.
- The Hub keeps per-day totals by currency, P&L line and charge type (fin_agg) and units shipped / refunded per SKU
  (fin_units). Re-pulling a day replaces it, so late postings are picked up by the rolling refresh.
- Refresh: the last 3 days every 3 hours, the last 14 days once a day, and history back to profit_start (default
  2026-01-01), newest day first, until every day is loaded.
- Mexico (MXN) and Canada (CAD) amounts are converted to USD with that day's ECB rate (frankfurter.dev).

Product cost
- Not in Amazon's data. COGS = units shipped × unit cost for that SKU on that day, from sku_costs: InventoryLab's
  active cost per unit (FBA Inventory export, uploaded by the daily InventoryLab task) or InventoryLab's monthly SKU
  costs for older months. A sale uses the latest cost dated on or before its day, else the earliest one after.
  SKUs InventoryLab has no cost for fall back to the order ledger's average cost for the ASIN; anything left is
  counted as uncosted units on the page.
- COGS is charged on units shipped only, like Link My Books (returns don't credit cost back).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from . import db, sellable

log = logging.getLogger("ledger.profit")
PT = ZoneInfo("America/Los_Angeles")
SP = sellable.SP
status: dict = {"last_pull": None, "last_error": None, "pulled_days": 0, "phase": None, "throttled": 0, "last_day": None}
_lock = asyncio.Lock()

# ---------------------------------------------------------------- mapping SP-API amounts to P&L lines
MAP = {
    "Commission": "Referral fees", "FixedClosingFee": "Referral fees", "VariableClosingFee": "Referral fees",
    "DigitalServicesFee": "Referral fees", "SalesTaxCollectionFee": "Referral fees",
    "RefundCommission": "Refund administration fee",
    "FBAPerUnitFulfillmentFee": "FBA fulfillment fees", "FBAPerOrderFulfillmentFee": "FBA fulfillment fees",
    "FBAWeightBasedFee": "FBA fulfillment fees", "RemoteFulfillmentCredit": "FBA fulfillment fees",
    "ShippingChargeback": "Shipping & gift wrap chargebacks", "GiftwrapChargeback": "Shipping & gift wrap chargebacks",
    "FBAStorageFee": "Storage fees", "FBALongTermStorageFee": "Storage fees", "StorageRenewalBilling": "Storage fees",
    "FBAStorageUtilizationSurcharge": "Storage fees", "FBAInventoryStorageUtilizationSurcharge": "Storage fees",
    "FBARemovalFee": "Removal & disposal fees", "FBADisposalFee": "Removal & disposal fees",
    "FBAInboundConvenienceFee": "Inbound placement fees", "FBAInboundPlacementServiceFee": "Inbound placement fees",
    "FBAInboundDefectFee": "Inbound placement fees", "FBALowInventoryLevelFee": "Storage fees",
    "FBAInboundTransportationFee": "Inbound shipping", "FBAInboundTransportationProgramFee": "Inbound shipping",
    "CustomerReturnHRRUnitFee": "Returns processing fees", "FBACustomerReturnPerUnitFee": "Returns processing fees",
    "Subscription": "Subscription fees",
    "RestockingFee": "Restocking fees", "ReturnShipping": "Refunds",
    "ShippingCharge": "Shipping credits", "GiftWrap": "Gift wrap credits", "ExportCharge": "Sales",
    "PromotionMetaDataDefinitionValue": "Promotions",
}
TAX_PREFIX = ("Tax", "ShippingTax", "GiftWrapTax", "MarketplaceFacilitator", "Combined TCS", "BaseTax", "LowValueGoods")
REIMB = {"WAREHOUSE_DAMAGE", "WAREHOUSE_LOST", "WAREHOUSE_LOST_MANUAL", "REVERSAL_REIMBURSEMENT", "FREE_REPLACEMENT_REFUND_ITEMS",
         "COMPENSATED_CLAWBACK", "MISSING_FROM_INBOUND", "MISSING_FROM_INBOUND_CLAWBACK", "INBOUND_CARRIER_DAMAGE", "CS_ERROR_ITEMS",
         "WAREHOUSE_DAMAGE_EXCEPTION", "REMOVAL_ORDER_LOST", "REMOVAL_ORDER_DAMAGED", "DESTROY_REIMBURSEMENT",
         "FBA_INVENTORY_REIMBURSEMENT", "INCORRECT_FEES_ITEMS", "CUSTOMER_RETURN_DAMAGED", "RE_EVALUATION", "DISPOSAL_ERROR"}

# How the page groups the lines (QuickBooks / Link My Books account names), in order.
GROUPS = [
    ("income", "Amazon Sales", ["Sales", "Gift wrap credits"]),
    ("income", "Amazon Refunds", ["Refunds", "Restocking fees"]),
    ("income", "Amazon Shipping Income", ["Shipping credits"]),
    ("income", "Amazon Discounts & Promotions", ["Promotions"]),
    ("income", "Amazon FBA Inventory Reimbursement", ["Inventory reimbursements"]),
    ("income", "Other Amazon adjustments", ["Other adjustments"]),
    ("cogs", "Product cost", ["COGS"]),
    ("cogs", "Amazon Inbound Shipping", ["Inbound shipping"]),
    ("expense", "Amazon Seller Fees & Charges", ["Referral fees", "Refund administration fee"]),
    ("expense", "Amazon FBA Warehousing & Fulfillment", ["FBA fulfillment fees", "Shipping & gift wrap chargebacks",
                                                         "Removal & disposal fees", "Returns processing fees"]),
    ("expense", "Amazon Storage Fees", ["Storage fees", "Inbound placement fees"]),
    ("expense", "Amazon Subscription Fees", ["Subscription fees"]),
    ("expense", "Amazon Advertising", ["Advertising"]),
    ("expense", "Other Amazon fees", ["Other fees"]),
]


def line_for(ev: str, kind: str, label: str, reason: str) -> str | None:
    """P&L line for one amount; None = not P&L (pass-through tax, duplicate ad fields, balance recovery)."""
    leaf = label.split("|")[-1] if label else ""
    if ev == "DebtRecoveryEventList":
        return None
    if ev == "ProductAdsPaymentEventList":
        return "Advertising" if kind == "transactionValue" else None
    if ev == "RetrochargeEventList" or kind in ("BaseTax", "ShippingTax") or any(leaf.startswith(p) or label.startswith(p) for p in TAX_PREFIX):
        return None   # sales tax Amazon collects and remits
    if ev == "AdjustmentEventList":
        if reason in REIMB or "REIMBURSEMENT" in reason or "CLAWBACK" in reason:
            return "Inventory reimbursements"
        return "Other adjustments"
    if leaf == "Principal":
        return "Sales" if ev == "ShipmentEventList" else "Refunds"
    if leaf in MAP:
        return MAP[leaf]
    if ev in ("ShipmentEventList", "RefundEventList") and "Charge" in kind:
        return "Other adjustments"
    return "Other fees"


def _money(d) -> bool:
    return isinstance(d, dict) and "CurrencyAmount" in d


def _walk(obj, path, label, out):
    if _money(obj):
        out.append(("/".join(path), label, float(obj["CurrencyAmount"] or 0), obj.get("CurrencyCode") or "USD"))
        return
    if isinstance(obj, dict):
        lab = label
        for k in ("ChargeType", "FeeType", "PromotionType", "AdjustmentType", "TaxCollectionModel"):
            if isinstance(obj.get(k), str):
                lab = (label + "|" if label else "") + obj[k]
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _walk(v, path + [k], lab, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk(x, path, label, out)


def flatten(pages: list[dict]):
    """-> (agg {(cur, line, detail): amount}, units {(mp, sku): [shipped, refunded]}, n_events)"""
    agg: dict = {}
    units: dict = {}
    n = 0

    def add(cur, ln, detail, a):
        if ln is None or not a:
            return
        k = (cur, ln, detail[:80])
        agg[k] = agg.get(k, 0.0) + a

    for fe in pages:
        for ev, lst in (fe or {}).items():
            for e in lst or []:
                n += 1
                reason = e.get("FeeReason") or e.get("RetrochargeEventType") or e.get("AdjustmentType") or ""
                mp = e.get("MarketplaceName") or ""
                items = e.get("ShipmentItemList") or e.get("ShipmentItemAdjustmentList")
                if ev == "AdjustmentEventList":
                    amt = e.get("AdjustmentAmount") or {}
                    if _money(amt):
                        add(amt.get("CurrencyCode") or "USD", line_for(ev, "AdjustmentAmount", reason, reason), reason, float(amt["CurrencyAmount"] or 0))
                    continue
                if items:
                    for it in items:
                        rows: list = []
                        _walk(it, [], "", rows)
                        for kind, lab, a, cur in rows:
                            leaf = lab.split("|")[-1] if lab else kind
                            add(cur, line_for(ev, kind, lab, reason), ev.replace("EventList", "") + " · " + leaf, a)
                        q = int(it.get("QuantityShipped") or 0)
                        if q and it.get("SellerSKU"):
                            u = units.setdefault((mp, it["SellerSKU"]), [0, 0])
                            u[0 if ev == "ShipmentEventList" else 1] += q
                    rest = {k: v for k, v in e.items() if k not in ("ShipmentItemList", "ShipmentItemAdjustmentList")}
                else:
                    rest = e
                rows = []
                _walk(rest, [], "", rows)
                for kind, lab, a, cur in rows:
                    leaf = lab.split("|")[-1] if lab else kind
                    add(cur, line_for(ev, kind, lab, reason), ev.replace("EventList", "") + " · " + leaf, a)
    return agg, units, n


# ---------------------------------------------------------------- SP-API pull
def window(day: date) -> tuple[str, str]:
    a = datetime(day.year, day.month, day.day, tzinfo=PT)
    b = a + timedelta(days=1)
    cap = datetime.now(timezone.utc) - timedelta(minutes=3)
    b = min(b.astimezone(timezone.utc), cap)
    z = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return z(a), z(b)


async def _get(c: httpx.AsyncClient, params: dict) -> dict:
    for attempt in range(8):
        r = await c.get(SP + "/finances/v0/financialEvents", params=params,
                        headers={"x-amz-access-token": await sellable._token(c)})
        if r.status_code == 429 or r.status_code >= 500:
            status["throttled"] += 1
            await asyncio.sleep(min(60, 4 * (attempt + 1)))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"financialEvents {r.status_code}: {r.text[:200]}")
        return r.json().get("payload") or {}
    raise RuntimeError("financialEvents: too many retries")


async def pull_day(day: date) -> dict:
    a, b = window(day)
    if a >= b:
        return {"day": day.isoformat(), "skipped": True}
    t0 = time.monotonic()
    pages = []
    async with httpx.AsyncClient(timeout=60) as c:
        params = {"PostedAfter": a, "PostedBefore": b, "MaxResultsPerPage": 100}
        while True:
            p = await _get(c, params)
            pages.append(p.get("FinancialEvents") or {})
            nt = p.get("NextToken")
            if not nt:
                break
            params = {"NextToken": nt}
            await asyncio.sleep(2.1)
    agg, units, n = flatten(pages)
    complete = datetime.now(timezone.utc) > datetime(day.year, day.month, day.day, tzinfo=PT) + timedelta(days=1, hours=1)
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM fin_agg WHERE day=%s", (day,))
            await conn.execute("DELETE FROM fin_units WHERE day=%s", (day,))
            if agg:
                async with conn.cursor() as cur:
                    await cur.executemany("INSERT INTO fin_agg (day, currency, line, detail, amount) VALUES (%s,%s,%s,%s,%s)",
                                          [(day, k[0], k[1], k[2], round(v, 2)) for k, v in agg.items() if round(v, 2)])
            if units:
                async with conn.cursor() as cur:
                    await cur.executemany("INSERT INTO fin_units (day, marketplace, sku, shipped, refunded) VALUES (%s,%s,%s,%s,%s)",
                                          [(day, k[0], k[1], v[0], v[1]) for k, v in units.items()])
            await conn.execute(
                """INSERT INTO fin_days (day, pulled_at, pages, events, complete) VALUES (%s, now(), %s, %s, %s)
                   ON CONFLICT (day) DO UPDATE SET pulled_at=now(), pages=EXCLUDED.pages, events=EXCLUDED.events, complete=EXCLUDED.complete""",
                (day, len(pages), n, complete))
    status["pulled_days"] += 1
    status["last_day"] = {"day": day.isoformat(), "pages": len(pages), "events": n, "secs": round(time.monotonic() - t0, 1)}
    return status["last_day"]


async def fx_refresh(start: date, end: date) -> int:
    """ECB daily rates (frankfurter.dev) for MXN and CAD -> USD per unit."""
    n = 0
    async with httpx.AsyncClient(timeout=30) as c:
        for cur in ("MXN", "CAD"):
            r = await c.get(f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}", params={"from": cur, "to": "USD"})
            if r.status_code != 200:
                continue
            rates = (r.json() or {}).get("rates") or {}
            async with db.pool.connection() as conn:
                async with conn.cursor() as cu:
                    await cu.executemany(
                        "INSERT INTO fx_rates (day, currency, usd) VALUES (%s,%s,%s) ON CONFLICT (day, currency) DO UPDATE SET usd=EXCLUDED.usd",
                        [(date.fromisoformat(d), cur, v.get("USD")) for d, v in rates.items() if v.get("USD")])
            n += len(rates)
    return n


async def _setting(key: str, default=None):
    async with db.pool.connection() as conn:
        r = await (await conn.execute("SELECT value FROM settings WHERE key=%s", (key,))).fetchone()
    return r["value"] if r else default


async def _set(key: str, value) -> None:
    async with db.pool.connection() as conn:
        await conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                           (key, json.dumps(value)))


def today_pt() -> date:
    return datetime.now(PT).date()


async def missing_days(limit: int) -> list[date]:
    start = date.fromisoformat(await _setting("profit_start", "2026-01-01"))
    async with db.pool.connection() as conn:
        rows = await (await conn.execute(
            """SELECT d::date AS day FROM generate_series(%s::date, %s::date, interval '1 day') d
               LEFT JOIN fin_days f ON f.day = d::date WHERE f.day IS NULL ORDER BY d DESC LIMIT %s""",
            (start, today_pt(), limit))).fetchall()
    return [r["day"] for r in rows]


def _due(stamp, hours: float) -> bool:
    if not stamp:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(stamp) > timedelta(hours=hours)


async def step() -> str:
    """One unit of work. Returns what it did ('' = nothing to do)."""
    t = today_pt()
    if _due(await _setting("profit_recent_at"), 3):
        for d in (t - timedelta(days=i) for i in range(3)):
            await pull_day(d)
        await fx_refresh(t - timedelta(days=20), t)
        await _set("profit_recent_at", datetime.now(timezone.utc).isoformat())
        return "recent"
    if _due(await _setting("profit_daily_at"), 20):
        for d in (t - timedelta(days=i) for i in range(3, 15)):
            await pull_day(d)
        await _set("profit_daily_at", datetime.now(timezone.utc).isoformat())
        return "daily"
    miss = await missing_days(7)
    if miss:
        if not await _setting("profit_fx_backfill"):
            start = date.fromisoformat(await _setting("profit_start", "2026-01-01"))
            await fx_refresh(start - timedelta(days=7), t)
            await _set("profit_fx_backfill", True)
        for d in miss:
            await pull_day(d)
        return "backfill"
    return ""


async def loop() -> None:
    if not sellable.ENABLED:
        return
    await asyncio.sleep(90)
    while True:
        did = ""
        try:
            async with _lock:
                status["phase"] = "working"
                did = await step()
                status.update(last_pull=time.strftime("%Y-%m-%d %H:%M:%S"), last_error=None, phase=did or "idle")
        except Exception as e:
            status.update(last_error=str(e)[:300], phase="error")
            log.exception("profit pull failed")
        await asyncio.sleep(5 if did == "backfill" else 600)


async def repull(start: date, end: date) -> dict:
    """Engine: re-pull a date range now (in the background)."""
    async with _lock:
        d = end
        n = 0
        while d >= start:
            await pull_day(d)
            d -= timedelta(days=1)
            n += 1
    return {"days": n}


# ---------------------------------------------------------------- costs
async def save_costs(source: str, as_of: date, rows: list[dict]) -> dict:
    """rows: [{sku, asin?, cost}] — a cost of 0 or missing (InventoryLab: no active batch) is skipped."""
    keep = []
    for r in rows or []:
        sku = str(r.get("sku") or "").strip()
        try:
            c = float(r.get("cost") or 0)
        except (TypeError, ValueError):
            c = 0
        if sku and c > 0:
            keep.append((sku, as_of, source, round(c, 4), (str(r.get("asin") or "").strip().upper() or None)))
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM sku_costs WHERE source=%s AND as_of=%s", (source, as_of))
            async with conn.cursor() as cur:
                await cur.executemany("INSERT INTO sku_costs (sku, as_of, source, cost, asin) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", keep)
            await conn.execute(
                """INSERT INTO sku_asin (sku, asin) SELECT DISTINCT ON (sku) sku, asin FROM sku_costs
                   WHERE source=%s AND as_of=%s AND asin IS NOT NULL
                   ON CONFLICT (sku) DO UPDATE SET asin=EXCLUDED.asin""", (source, as_of))
    return {"saved": len(keep), "skipped": len(rows or []) - len(keep), "as_of": as_of.isoformat(), "source": source}


# ---------------------------------------------------------------- report
COGS_SQL = """
WITH u AS (
  SELECT fu.day, fu.marketplace, fu.sku, fu.shipped FROM fin_units fu
  WHERE fu.day BETWEEN %(a)s AND %(b)s AND fu.shipped > 0 AND (%(us)s = false OR fu.marketplace = 'Amazon.com')
), c AS (
  SELECT u.*,
    COALESCE(
      (SELECT sc.cost FROM sku_costs sc WHERE sc.sku = u.sku AND sc.as_of <= u.day ORDER BY sc.as_of DESC LIMIT 1),
      (SELECT sc.cost FROM sku_costs sc WHERE sc.sku = u.sku AND sc.as_of > u.day ORDER BY sc.as_of ASC LIMIT 1)) AS il_cost,
    (SELECT asin FROM sku_asin sa WHERE sa.sku = u.sku) AS asin
  FROM u
)
SELECT c.day, c.marketplace, c.sku, c.asin, c.shipped, c.il_cost,
  CASE WHEN c.il_cost IS NULL AND c.asin IS NOT NULL THEN
    (SELECT sum(oi.qty_ordered * oi.cost_per_unit) / NULLIF(sum(oi.qty_ordered), 0) FROM order_items oi WHERE upper(btrim(oi.asin)) = c.asin AND oi.cost_per_unit > 0)
  END AS ledger_cost
FROM c
"""


async def report(a: date, b: date, us_only: bool = False) -> dict:
    async with db.pool.connection() as conn:
        fx = {}
        for r in await (await conn.execute("SELECT day, currency, usd FROM fx_rates WHERE day BETWEEN %s AND %s ORDER BY day",
                                             (a - timedelta(days=10), b))).fetchall():
            fx.setdefault(r["currency"], []).append((r["day"], float(r["usd"])))
        agg = await (await conn.execute(
            "SELECT day, currency, line, detail, amount FROM fin_agg WHERE day BETWEEN %s AND %s" + (" AND currency='USD'" if us_only else ""),
            (a, b))).fetchall()
        cogs_rows = await (await conn.execute(COGS_SQL, {"a": a, "b": b, "us": us_only})).fetchall()
        days = await (await conn.execute(
            "SELECT count(*) n, min(pulled_at) oldest, max(pulled_at) newest FROM fin_days WHERE day BETWEEN %s AND %s", (a, b))).fetchone()
        span = (b - a).days + 1
        cost_asof = await (await conn.execute("SELECT max(as_of) d, count(*) FILTER (WHERE as_of=(SELECT max(as_of) FROM sku_costs)) n FROM sku_costs")).fetchone()

    def rate(cur: str, d: date) -> float | None:
        if cur == "USD":
            return 1.0
        best = None
        for dd, v in fx.get(cur, []):
            if dd <= d:
                best = v
        return best if best is not None else (fx.get(cur) or [(None, None)])[-1][1]

    lines: dict = {}
    detail: dict = {}
    daily: dict = {}
    other_cur: dict = {}
    for r in agg:
        v = float(r["amount"])
        k = rate(r["currency"], r["day"])
        usd = v * k if k else 0.0
        lines[r["line"]] = lines.get(r["line"], 0.0) + usd
        dk = (r["line"], r["detail"], r["currency"])
        detail[dk] = detail.get(dk, 0.0) + usd
        dd = daily.setdefault(r["day"].isoformat(), {"income": 0.0, "fees": 0.0, "cogs": 0.0, "sales": 0.0, "units": 0})
        grp = next((g for g, _, ls in GROUPS if r["line"] in ls), "expense")
        if grp == "income":
            dd["income"] += usd
        elif grp == "cogs":
            dd["cogs"] += usd
        else:
            dd["fees"] += usd
        if r["line"] == "Sales":
            dd["sales"] += usd
        if r["currency"] != "USD":
            other_cur[r["currency"]] = other_cur.get(r["currency"], 0.0) + v

    cogs = 0.0
    units = costed_il = costed_ledger = 0
    uncosted: dict = {}
    for r in cogs_rows:
        q = int(r["shipped"])
        units += q
        c = r["il_cost"] if r["il_cost"] is not None else r["ledger_cost"]
        dd = daily.setdefault(r["day"].isoformat(), {"income": 0.0, "fees": 0.0, "cogs": 0.0, "sales": 0.0, "units": 0})
        dd["units"] += q
        if c is None:
            u = uncosted.setdefault(r["sku"], {"sku": r["sku"], "asin": r["asin"], "units": 0})
            u["units"] += q
            continue
        if r["il_cost"] is not None:
            costed_il += q
        else:
            costed_ledger += q
        cogs -= q * float(c)
        dd["cogs"] -= q * float(c)
    lines["COGS"] = cogs

    groups = []
    tot = {"income": 0.0, "cogs": 0.0, "expense": 0.0}
    for sec, name, ls in GROUPS:
        amt = sum(lines.get(x, 0.0) for x in ls)
        if not amt and name in ("Other Amazon adjustments", "Other Amazon fees"):
            continue
        parts = []
        for x in ls:
            subs = sorted(((d, cu, v) for (ln, d, cu), v in detail.items() if ln == x and round(v, 2)), key=lambda z: -abs(z[2]))
            parts.append({"line": x, "amount": round(lines.get(x, 0.0), 2),
                          "detail": [{"what": d + ("" if cu == "USD" else f" ({cu})"), "amount": round(v, 2)} for d, cu, v in subs[:25]]})
        tot[sec] += amt
        groups.append({"section": sec, "name": name, "amount": round(amt, 2), "parts": parts})
    profit = tot["income"] + tot["cogs"] + tot["expense"]
    series = []
    d = a
    while d <= b:
        x = daily.get(d.isoformat(), {"income": 0.0, "fees": 0.0, "cogs": 0.0, "sales": 0.0, "units": 0})
        series.append({"day": d.isoformat(), "sales": round(x["sales"], 2), "units": x["units"],
                       "profit": round(x["income"] + x["fees"] + x["cogs"], 2)})
        d += timedelta(days=1)
    return {
        "start": a.isoformat(), "end": b.isoformat(), "us_only": us_only,
        "groups": groups,
        "totals": {"income": round(tot["income"], 2), "cogs": round(tot["cogs"], 2), "expense": round(tot["expense"], 2),
                   "profit": round(profit, 2), "sales": round(lines.get("Sales", 0.0), 2),
                   "margin": round(profit / tot["income"], 4) if tot["income"] else None},
        "daily": series,
        "coverage": {"days_loaded": days["n"], "days": span, "oldest_pull": days["oldest"], "newest_pull": days["newest"],
                     "units": units, "costed_inventorylab": costed_il, "costed_ledger": costed_ledger,
                     "uncosted_units": units - costed_il - costed_ledger,
                     "uncosted": sorted(uncosted.values(), key=lambda z: -z["units"])[:30],
                     "cost_snapshot": cost_asof["d"], "cost_snapshot_skus": cost_asof["n"],
                     "other_currency": {k: round(v, 2) for k, v in other_cur.items()}},
        "status": {k: v for k, v in status.items()},
    }
