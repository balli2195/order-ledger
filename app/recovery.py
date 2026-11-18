"""Reimbursements page, Recovery section (Nick, Nov 18 2026).

Per month of one year: how much inventory Amazon lost (at Nick's sourcing cost), how much Amazon paid back, and the
shortfall, for three kinds of loss, plus how the money came back (by method). Built only from tables the Hub already
refreshes every day, so it needs no job of its own:

- Lost inbound: units missing from closed shipment plans (profit.missing_inbound, the Reimbursements-page numbers),
  dated the day the plan's last shipment closed.
- Lost / damaged in warehouse: inv_writeoff kinds 'lost' (net of found) and 'damaged' (the Profit page's write-off lines,
  from the inventory ledger), dated by the ledger event.
- Paid back: inbound_reimb (GET_FBA_REIMBURSEMENTS_DATA) Lost_Inbound, Lost_Warehouse, Damaged_Warehouse cash, by
  approval date, net of Reimbursement_Reversal rows (clawbacks), which are matched to their kind by orig_type.
- Cost: the latest InventoryLab cost per SKU (sku_costs), else the Hub's average order cost for the ASIN.

Shortfall = lost at cost − paid. "Paid units at cost" = the same reimbursed units valued at Nick's cost, so
(paid units at cost − paid) is how far below cost Amazon paid on the units it did reimburse.
Losses and payments land in different months (Amazon pays weeks later), so one month's shortfall is not final until
the claim windows close; the year total is the meaningful number.
"""
from __future__ import annotations

from datetime import date

from . import db, profit

KINDS = {"Lost_Inbound": "inbound", "Lost_Warehouse": "wh_lost", "Damaged_Warehouse": "wh_damaged"}
METHODS = {
    "case": "Lost inbound cases (incl. contests on the same case)",
    "rereview": "Re-reviews (Seller Assistant)",
    "dispute": "Amount disputes & other inbound",
    "wh_auto": "Warehouse payouts (Amazon automatic)",
    "wh_claim": "Warehouse claims & amount disputes",
    "clawback": "Clawbacks (found units)",
}


async def _costs(conn) -> tuple[dict, dict, dict]:
    sku = {r["sku"]: float(r["cost"]) for r in await (await conn.execute(
        "SELECT DISTINCT ON (sku) sku, cost FROM sku_costs WHERE cost > 0 ORDER BY sku, as_of DESC")).fetchall()}
    sku_asin = {r["sku"]: r["asin"] for r in await (await conn.execute("SELECT sku, asin FROM sku_asin")).fetchall()}
    asin = {r["a"]: float(r["c"]) for r in await (await conn.execute(
        """SELECT upper(btrim(asin)) a, sum(qty_ordered * cost_per_unit) / NULLIF(sum(qty_ordered), 0) c
           FROM order_items WHERE cost_per_unit > 0 AND asin IS NOT NULL GROUP BY 1""")).fetchall() if r["c"]}
    return sku, sku_asin, asin


async def report(year: int) -> dict:
    a, b = date(year, 1, 1), date(year, 12, 31)
    async with db.pool.connection() as conn:
        sku_c, sku_asin, asin_c = await _costs(conn)

        def cost(sku: str | None, asin: str | None = None) -> float | None:
            if sku and sku in sku_c:
                return sku_c[sku]
            k = (asin or sku_asin.get(sku or "") or "").strip().upper()
            return asin_c.get(k)

        wo = await (await conn.execute(
            "SELECT day, sku, kind, units FROM inv_writeoff WHERE kind IN ('lost','damaged') AND day BETWEEN %s AND %s",
            (a, b))).fetchall()
        rb = await (await conn.execute(
            """SELECT reimbursement_id, sku, asin, case_id, reason, approval_date, amount, qty_cash, qty_inv, orig_type
               FROM inbound_reimb WHERE approval_date BETWEEN %s AND %s""", (a, b))).fetchall()
        cases = await (await conn.execute("SELECT data FROM inbound_cases")).fetchall()
    inbound_lost = await profit.missing_inbound(a, b)

    case_ids, rereview_ids = set(), set()
    for r in cases:
        d = r["data"] or {}
        if d.get("case"):
            case_ids.add(str(d["case"]))
        rr = d.get("rereview") or {}
        if isinstance(rr, dict) and rr.get("case"):
            rereview_ids.add(str(rr["case"]))

    months = {m: {"month": f"{year}-{m:02d}", **{k: {"lost_u": 0, "lost_v": 0.0, "paid": 0.0, "paid_u": 0, "paid_cost": 0.0, "inv_u": 0}
                                                 for k in KINDS.values()}, "methods": {k: 0.0 for k in METHODS}}
              for m in range(1, 13)}
    uncosted = 0

    def lost(day: date, kind: str, sku: str, units: int):
        nonlocal uncosted
        c = cost(sku)
        if c is None:
            uncosted += abs(units)
            c = 0.0
        g = months[day.month][kind]
        g["lost_u"] += units
        g["lost_v"] += units * c

    for day, sku, units in inbound_lost:
        lost(day, "inbound", sku, units)
    for r in wo:
        lost(r["day"], "wh_lost" if r["kind"] == "lost" else "wh_damaged", r["sku"], int(r["units"]))

    for r in rb:
        reason = (r["reason"] or "").strip()
        rev = reason == "Reimbursement_Reversal"
        kind = KINDS.get((r["orig_type"] or "").strip() if rev else reason)
        if not kind:
            continue
        d = r["approval_date"]
        d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
        m = months[d.month]
        g = m[kind]
        amt, qc, qi = float(r["amount"] or 0), int(r["qty_cash"] or 0), int(r["qty_inv"] or 0)
        g["paid"] += amt
        g["paid_u"] += qc
        g["inv_u"] += qi
        g["paid_cost"] += qc * (cost(r["sku"], r["asin"]) or 0)
        cid = str(r["case_id"] or "")
        if rev:
            meth = "clawback"
        elif kind == "inbound":
            meth = "rereview" if cid in rereview_ids else "case" if cid in case_ids else "dispute"
        else:
            meth = "wh_claim" if cid else "wh_auto"
        m["methods"][meth] += amt

    out, tot = [], {k: {"lost_u": 0, "lost_v": 0.0, "paid": 0.0, "paid_u": 0, "paid_cost": 0.0, "inv_u": 0} for k in KINDS.values()}
    tot_m = {k: 0.0 for k in METHODS}
    last = date.today().month if year == date.today().year else 12
    for mi in range(1, last + 1):
        m = months[mi]
        row = {"month": m["month"], "methods": {k: round(v, 2) for k, v in m["methods"].items()}}
        for k in KINDS.values():
            g = m[k]
            row[k] = {**{x: round(g[x], 2) for x in g}, "shortfall": round(g["lost_v"] - g["paid"], 2)}
            for x in g:
                tot[k][x] += g[x]
        for k, v in m["methods"].items():
            tot_m[k] += v
        row["all"] = {x: round(sum(row[k][x] for k in KINDS.values()), 2) for x in ("lost_u", "lost_v", "paid", "paid_u", "paid_cost", "shortfall")}
        out.append(row)
    total = {k: {**{x: round(v, 2) for x, v in tot[k].items()}, "shortfall": round(tot[k]["lost_v"] - tot[k]["paid"], 2)} for k in KINDS.values()}
    total["all"] = {x: round(sum(total[k][x] for k in KINDS.values()), 2) for x in ("lost_u", "lost_v", "paid", "paid_u", "paid_cost", "shortfall")}
    total["methods"] = {k: round(v, 2) for k, v in tot_m.items()}
    pc = total["all"]["paid_cost"]
    total["payout_rate"] = round(total["all"]["paid"] / pc, 4) if pc else None
    for row in out:
        c = row["all"]["paid_cost"]
        row["payout_rate"] = round(row["all"]["paid"] / c, 4) if c else None
    return {"year": year, "months": out, "total": total, "method_labels": METHODS, "uncosted_units": uncosted,
            "kinds": {"inbound": "Lost inbound", "wh_lost": "Lost in warehouse (net of found)", "wh_damaged": "Damaged in warehouse"}}
