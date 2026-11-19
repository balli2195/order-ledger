"""Reimbursements page, Recovery section (Nick, Nov 18 2026).

Per month of one year: how much inventory Amazon lost (at Nick's sourcing cost), how much Amazon paid back, and the
shortfall, for three kinds of loss, plus how the money came back (by method). Built only from tables the Hub already
refreshes every day, so it needs no job of its own:

- Lost inbound: units missing from closed shipment plans (profit.missing_inbound, the Reimbursements-page numbers),
  dated the day the plan's last shipment closed.
- Lost / damaged in warehouse: inv_writeoff kinds 'lost' (net of found) and 'damaged' (the Profit page's write-off lines,
  from the inventory ledger), dated by the ledger event.
- Not Amazon's fault (Nick, Nov 18 2026): damaged units Amazon moved on to DISTRIBUTOR_DAMAGED (ledger P +n into
  DISTRIBUTOR_DAMAGED within a few days of the damage) = Amazon says they arrived damaged, so it doesn't reimburse them.
  They are a real loss but not an unpaid claim, so they are taken out of "Damaged in warehouse" into their own line and
  are not part of the claimable shortfall.
- Paid back: inbound_reimb (GET_FBA_REIMBURSEMENTS_DATA) Lost_Inbound, Lost_Warehouse, Damaged_Warehouse cash, net of
  Reimbursement_Reversal rows (clawbacks), which are matched to their kind by orig_type.
- Cost: the latest InventoryLab cost per SKU (sku_costs), else the Hub's average order cost for the ASIN.

Two ways to date a payment (by=):
- "paid": the month Amazon approved it.
- "loss" (Nick, Nov 18 2026): the month of the loss it pays for, so lost and paid line up. Warehouse payments are matched
  first-in-first-out to earlier losses of the same SKU and kind; lost-inbound payments go to the month their shipment's plan
  closed (case ID -> shipment from inbound_cases), else FIFO by SKU; a clawback goes where its original payment went.
  Payments for losses from before the year go to an "Earlier losses" line.

Shortfall = lost at cost − paid. "Paid units at cost" = the same reimbursed units valued at Nick's cost, so
(paid units at cost − paid) is how far below cost Amazon paid on the units it did reimburse.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from . import db, profit

KINDS = {"Lost_Inbound": "inbound", "Lost_Warehouse": "wh_lost", "Damaged_Warehouse": "wh_damaged"}
KIND_LABELS = {"inbound": "Lost inbound", "wh_lost": "Lost in warehouse (net of found)", "wh_damaged": "Damaged in warehouse",
               "not_amazon": "Damaged, Amazon says not its fault"}
METHODS = {
    "case": "Lost inbound cases (incl. contests on the same case)",
    "rereview": "Re-reviews (Seller Assistant)",
    "dispute": "Amount disputes & other inbound",
    "wh_auto": "Warehouse payouts (Amazon automatic)",
    "wh_claim": "Warehouse claims & amount disputes",
    "clawback": "Clawbacks (found units)",
}
CLAIMABLE = ("inbound", "wh_lost", "wh_damaged")
RECLASS_DAYS = 10      # damage -> DISTRIBUTOR_DAMAGED move counted if it happens within this many days
LOOKBACK = 300         # losses from before the year are loaded this far back so early payments can match them


async def _costs(conn) -> tuple[dict, dict, dict]:
    sku = {r["sku"]: float(r["cost"]) for r in await (await conn.execute(
        "SELECT DISTINCT ON (sku) sku, cost FROM sku_costs WHERE cost > 0 ORDER BY sku, as_of DESC")).fetchall()}
    sku_asin = {r["sku"]: r["asin"] for r in await (await conn.execute("SELECT sku, asin FROM sku_asin")).fetchall()}
    asin = {r["a"]: float(r["c"]) for r in await (await conn.execute(
        """SELECT upper(btrim(asin)) a, sum(qty_ordered * cost_per_unit) / NULLIF(sum(qty_ordered), 0) c
           FROM order_items WHERE cost_per_unit > 0 AND asin IS NOT NULL GROUP BY 1""")).fetchall() if r["c"]}
    return sku, sku_asin, asin


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


async def report(year: int, by: str = "paid") -> dict:
    by = "loss" if by == "loss" else "paid"
    a, b = date(year, 1, 1), date(year, 12, 31)
    a0 = a - timedelta(days=LOOKBACK)
    async with db.pool.connection() as conn:
        sku_c, sku_asin, asin_c = await _costs(conn)
        wo = await (await conn.execute(
            "SELECT day, sku, kind, units FROM inv_writeoff WHERE kind IN ('lost','damaged') AND day BETWEEN %s AND %s ORDER BY day",
            (a0, b))).fetchall()
        dist = await (await conn.execute(
            """SELECT day, sku, qty FROM inv_ledger WHERE event = 'Adjustments' AND reason = 'P' AND disp = 'DISTRIBUTOR_DAMAGED'
               AND qty > 0 AND day BETWEEN %s AND %s ORDER BY day""", (a0, b + timedelta(days=RECLASS_DAYS)))).fetchall()
        rb = await (await conn.execute(
            """SELECT reimbursement_id, sku, asin, case_id, reason, approval_date, amount, qty_cash, qty_inv, orig_id, orig_type
               FROM inbound_reimb WHERE approval_date BETWEEN %s AND %s ORDER BY approval_date""",
            (a if by == "paid" else a0, b))).fetchall()
        cases = await (await conn.execute("SELECT shipment_id, data FROM inbound_cases")).fetchall()
        ships = await (await conn.execute(
            "SELECT shipment_id, plan_key, last_receipt, closed_seen_at FROM inbound_shipments")).fetchall()
    inbound_lost = await profit.missing_inbound(a0, b)

    def cost(sku: str | None, asin: str | None = None) -> float | None:
        if sku and sku in sku_c:
            return sku_c[sku]
        k = (asin or sku_asin.get(sku or "") or "").strip().upper()
        return asin_c.get(k)

    # case IDs -> method, and -> shipment -> the day its plan closed
    case_ids, rereview_ids, case_ship = set(), set(), {}
    for r in cases:
        d = r["data"] or {}
        if d.get("case"):
            case_ids.add(str(d["case"]))
            case_ship[str(d["case"])] = r["shipment_id"]
        rr = d.get("rereview") or {}
        if isinstance(rr, dict) and rr.get("case"):
            rereview_ids.add(str(rr["case"]))
            case_ship[str(rr["case"])] = r["shipment_id"]
    plan_close: dict = {}
    ship_plan: dict = {}
    for s in ships:
        c = s["last_receipt"] or (s["closed_seen_at"].date() if s["closed_seen_at"] else None)
        ship_plan[s["shipment_id"]] = s["plan_key"]
        if c and (s["plan_key"] not in plan_close or c > plan_close[s["plan_key"]]):
            plan_close[s["plan_key"]] = c

    # losses (incl. the lookback, for matching) -> list of [day, kind, sku, units]
    losses: list = []
    for day, sku, units in inbound_lost:
        losses.append([day, "inbound", sku, int(units)])
    pool = defaultdict(list)      # sku -> [[day, units left]] of moves into DISTRIBUTOR_DAMAGED
    for r in dist:
        pool[r["sku"]].append([r["day"], int(r["qty"])])
    for r in wo:
        units = int(r["units"])
        if r["kind"] == "lost":
            losses.append([r["day"], "wh_lost", r["sku"], units])
            continue
        moved = 0
        for p in pool.get(r["sku"], []):
            if units - moved <= 0:
                break
            if p[1] > 0 and r["day"] - timedelta(days=2) <= p[0] <= r["day"] + timedelta(days=RECLASS_DAYS):
                t = min(p[1], units - moved)
                p[1] -= t
                moved += t
        if moved:
            losses.append([r["day"], "not_amazon", r["sku"], moved])
        if units - moved:
            losses.append([r["day"], "wh_damaged", r["sku"], units - moved])

    def mkey(d: date):
        return d.month if d >= a else 0          # 0 = earlier losses (before the year)

    blank = lambda: {"lost_u": 0, "lost_v": 0.0, "paid": 0.0, "paid_u": 0, "paid_cost": 0.0, "inv_u": 0}
    months = {m: {**{k: blank() for k in KIND_LABELS}, "methods": {k: 0.0 for k in METHODS}} for m in range(0, 13)}
    uncosted = 0
    queue = defaultdict(list)     # (kind, sku) -> [[day, units left]] for FIFO matching of payments to losses
    for day, kind, sku, units in sorted(losses, key=lambda x: x[0]):
        if units > 0:
            queue[(kind, sku)].append([day, units])
        if day < a:
            continue
        c = cost(sku)
        if c is None:
            uncosted += abs(units)
            c = 0.0
        g = months[day.month][kind]
        g["lost_u"] += units
        g["lost_v"] += units * c

    placed: dict = {}             # reimbursement_id|sku -> [(month, share)] where a payment landed (for its clawbacks)

    def place_fifo(kind: str, sku: str, n: int, when: date) -> list:
        """Split n paid units over the earliest unmatched losses of the same kind and SKU dated on or before `when`."""
        out, left = [], n
        for q in queue.get((kind, sku), []):
            if left <= 0:
                break
            if q[1] <= 0 or q[0] > when:
                continue
            t = min(q[1], left)
            q[1] -= t
            left -= t
            out.append((mkey(q[0]), t))
        if left > 0:
            out.append((mkey(when), left))      # no matching loss on file: keep it in the month it was paid
        tot = sum(t for _, t in out) or 1
        return [(m, t / tot) for m, t in out]

    for r in rb:
        reason = (r["reason"] or "").strip()
        rev = reason == "Reimbursement_Reversal"
        kind = KINDS.get((r["orig_type"] or "").strip() if rev else reason)
        if not kind:
            continue
        paid_day = _d(r["approval_date"])
        sku = r["sku"]
        amt, qc, qi = float(r["amount"] or 0), int(r["qty_cash"] or 0), int(r["qty_inv"] or 0)
        cid = str(r["case_id"] or "")
        if rev:
            meth = "clawback"
        elif kind == "inbound":
            meth = "rereview" if cid in rereview_ids else "case" if cid in case_ids else "dispute"
        else:
            meth = "wh_claim" if cid else "wh_auto"
        if by == "paid":
            where = [(paid_day.month, 1.0)] if paid_day >= a else []
        elif rev:
            where = placed.get(f"{r['orig_id']}|{sku}") or [(mkey(paid_day), 1.0)]
        elif kind == "inbound" and cid in case_ship and plan_close.get(ship_plan.get(case_ship[cid])):
            where = [(mkey(plan_close[ship_plan[case_ship[cid]]]), 1.0)]
            place_fifo("inbound", sku, qc + qi, paid_day)    # use up matching losses so FIFO doesn't reuse them
        else:
            q_kind = kind
            if kind == "wh_damaged" and not any(q[1] > 0 for q in queue.get(("wh_damaged", sku), [])):
                q_kind = "not_amazon" if queue.get(("not_amazon", sku)) else kind   # Amazon paid one after all
            where = place_fifo(q_kind, sku, max(1, qc + qi), paid_day)
        placed[f"{r['reimbursement_id']}|{sku}"] = where
        if paid_day < a:
            continue          # paid before the year: only used to match losses and clawbacks, not counted
        uc = cost(sku, r["asin"]) or 0
        for m, share in where:
            g = months[m][kind]
            g["paid"] += amt * share
            g["paid_u"] += qc * share
            g["inv_u"] += qi * share
            g["paid_cost"] += qc * uc * share
            months[m]["methods"][meth] += amt * share

    def finish(src: dict) -> dict:
        row = {"methods": {k: round(v, 2) for k, v in src["methods"].items()}}
        for k in KIND_LABELS:
            g = src[k]
            row[k] = {x: round(v, 2) for x, v in g.items()}
            row[k]["shortfall"] = round(g["lost_v"] - g["paid"], 2)
        row["all"] = {x: round(sum(row[k][x] for k in CLAIMABLE), 2)
                      for x in ("lost_u", "lost_v", "paid", "paid_u", "paid_cost", "shortfall")}
        c = row["all"]["paid_cost"]
        row["payout_rate"] = round(row["all"]["paid"] / c, 4) if c else None
        return row

    last = date.today().month if year == date.today().year else 12
    out = []
    if by == "loss" and any(months[0][k]["paid"] for k in KIND_LABELS):
        out.append({"month": f"{year - 1}", "earlier": True, **finish(months[0])})
    for mi in range(1, last + 1):
        out.append({"month": f"{year}-{mi:02d}", **finish(months[mi])})
    tot = {**{k: blank() for k in KIND_LABELS}, "methods": {k: 0.0 for k in METHODS}}
    for r in out:
        src = months[0] if r.get("earlier") else months[int(r["month"][5:])]
        for k in KIND_LABELS:
            for x in tot[k]:
                tot[k][x] += src[k][x]
        for k in METHODS:
            tot["methods"][k] += src["methods"][k]
    total = finish(tot)
    return {"year": year, "by": by, "months": out, "total": {**total, **{k: total[k] for k in KIND_LABELS}},
            "method_labels": METHODS, "uncosted_units": uncosted, "kinds": KIND_LABELS, "claimable": list(CLAIMABLE)}


async def pipeline(year: int) -> dict:
    """"Where it stands" diagram on the Reimbursements page (Nick, Nov 18 2026): for lost inbound and for warehouse
    lost/damaged, the year's loss at Nick's cost split into paid / in process / claimable now / underpaid (needs an
    invoice to dispute) / unpaid.

    - Paid: Amazon's net payouts (report(), by month paid).
    - In process, inbound: missing value of this year's plans with a case open; warehouse: the shortfall on losses from the
      last 60 days (by month of loss), which Amazon is normally still settling.
    - Claimable now, inbound: plans ready to file (closed, unfiled, window open).
    - Reimbursement-amount disputes (payouts below Nick's approved cost) come from the settings key reimb_disputes,
      which Claude posts from the dispute list: {"inbound"|"warehouse": {"filed", "claimable", "needs_invoice"}, "as_of"}.
      Filed counts as in process, claimable (invoice on file) as claimable now, needs_invoice as its own segment.
    - Unpaid = loss − everything above (closed, denied, or paid without a dispute path), never below 0.
    """
    from . import inbound, leads
    paid = await report(year, "paid")
    loss = await report(year, "loss")
    pg = await inbound.page(include_all=True)
    yp = [p for p in pg["plans"] if str(p.get("plan", ""))[6:10] == str(year)]
    disp = await leads.get_setting("reimb_disputes", {}) or {}
    T = paid["total"]
    cutoff = date.today() - timedelta(days=60)
    wh_recent = 0.0
    for r in loss["months"]:
        if r.get("earlier"):
            continue
        y, m = int(r["month"][:4]), int(r["month"][5:7])
        end = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        if end >= cutoff:
            wh_recent += max(0.0, r["wh_lost"]["shortfall"]) + max(0.0, r["wh_damaged"]["shortfall"])

    def seg(kind: str, lost: float, pd: float, ip: float, cl: float) -> dict:
        """Split the unpaid part (lost − paid) so the pieces never add up to more than the loss: disputes first
        (filed, with invoice, needs invoice), then open cases / recent unsettled losses, then ready to file; the rest is unpaid.
        Recent warehouse shortfalls already contain recent underpayments, so taking disputes first avoids counting them twice."""
        d = disp.get(kind) or {}
        left = max(0.0, lost - pd)

        def take(v: float) -> float:
            nonlocal left
            t = max(0.0, min(float(v or 0), left))
            left -= t
            return t
        d_ip, d_cl, ni = take(d.get("filed", 0)), take(d.get("claimable", 0)), take(d.get("needs_invoice", 0))
        ip2 = d_ip + take(ip)
        cl2 = d_cl + take(cl)
        return {k: round(v, 2) for k, v in {"lost": lost, "paid": pd, "in_process": ip2, "claimable": cl2,
                                             "needs_invoice": ni, "unpaid": left}.items()}

    inb = seg("inbound", T["inbound"]["lost_v"], T["inbound"]["paid"],
              sum(p["missing_value"] for p in yp if p["state"] == "case_open"),
              sum(p["missing_value"] for p in yp if p["state"] == "eligible"))
    wh = seg("warehouse", T["wh_lost"]["lost_v"] + T["wh_damaged"]["lost_v"], T["wh_lost"]["paid"] + T["wh_damaged"]["paid"],
             wh_recent, 0.0)
    return {"year": year, "inbound": inb, "warehouse": wh, "disputes_as_of": disp.get("as_of"),
            "receiving_plans": sum(1 for p in yp if p["state"] == "receiving")}
