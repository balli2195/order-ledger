"""Bank and card transactions (Nick, Nov 8 2026): every transaction from Nick's bank and card accounts, stored in the Hub.

Two sources, each row tagged with where it came from:
- statement: the bank's own monthly statement (PDFs Nick keeps in Dropbox /Amazon/Statements), parsed by
  claude/bank/parse_statements.py. A statement is only loaded whole, with its period and balances, and each
  parse is checked against the statement's own balance change (`ties`).
- quickbooks: QuickBooks' posted transactions on the same accounts (Intuit QuickBooks Labs connector), with the
  bookkeeper's category and payee, plus bank-feed rows still waiting for review. Loaded daily for recent days,
  so the days after the last statement are covered too.

Accounts are keyed bank_last4 (chase_2598, chase_8038, amex_1000, capone_1999, chase_5050, ...). Amounts are from
Nick's side: money out negative, money in positive. `bank_best` gives one row set per account and day: the
statement's rows where a loaded statement covers the day, else QuickBooks'.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import db

# QuickBooks account id -> Hub account key (QuickBooks names the closed Capital One card "8039"; its statements say 1999).
QB_ACCOUNTS = {
    "175": "chase_2598", "302": "chase_2826", "303": "chase_6304", "1150040004": "chase_8038", "369": "chase_2706",
    "177": "amex_1000", "176": "amex_1004", "149": "chase_5050", "157": "chase_859", "367": "capone_1999",
    "180": "capone_6382", "364": "paypal_usd", "384": "paypal_deposit", "151": "paypal_bank", "315": "paypal_mastercard",
    "371": "huntington_1948", "372": "huntington_loan", "296": "liveoak_6922", "297": "liveoak_5967",
    "209": "chase_5008", "181": "mesh", "1150040001": "paypal_eur", "336": "paypal_hkd",
}


def _d(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


async def save_statement(body: dict) -> dict:
    """{statement: {bank, last4, file, opened, closed, previous_balance, new_balance, rows_total, expected_total, ties},
        rows: [{seq, day, description, amount, card}], dropbox_path?, content_hash?} — replaces that statement."""
    st = body.get("statement") or {}
    rows = body.get("rows") or []
    bank, last4 = str(st.get("bank") or "").lower(), str(st.get("last4") or "")
    if not bank or not last4 or not st.get("closed"):
        raise ValueError("statement needs bank, last4 and closed")
    account = f"{bank}_{last4}"
    closed = _d(st["closed"])
    opened = _d(st["opened"]) if st.get("opened") else None
    sid = f"{account}:{closed.isoformat()}"
    keep = []
    for r in rows:
        keep.append((f"{sid}:{int(r['seq'])}", account, str(r.get("card") or last4), _d(r["day"]), round(float(r["amount"]), 2),
                     str(r.get("description") or "")[:500], sid))
    days = [k[3] for k in keep]
    async with db.pool.connection() as conn:
        async with conn.transaction():
            # a statement without an opening date starts the day after the account's previous statement closed
            if opened is None:
                p = await (await conn.execute("SELECT max(closed) d FROM bank_statements WHERE account=%s AND closed < %s",
                                              (account, closed))).fetchone()
                if p and p["d"]:
                    opened = p["d"] + timedelta(days=1)
                elif days:
                    opened = min(days)
            await conn.execute(
                """INSERT INTO bank_statements (id, account, bank, last4, opened, closed, previous_balance, new_balance, n_rows,
                       rows_total, expected_total, ties, file, dropbox_path, content_hash, loaded_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (id) DO UPDATE SET opened=EXCLUDED.opened, previous_balance=EXCLUDED.previous_balance,
                       new_balance=EXCLUDED.new_balance, n_rows=EXCLUDED.n_rows, rows_total=EXCLUDED.rows_total,
                       expected_total=EXCLUDED.expected_total, ties=EXCLUDED.ties, file=EXCLUDED.file,
                       dropbox_path=EXCLUDED.dropbox_path, content_hash=EXCLUDED.content_hash, loaded_at=now()""",
                (sid, account, bank, last4, opened, closed, st.get("previous_balance"), st.get("new_balance"), len(keep),
                 st.get("rows_total"), st.get("expected_total"), bool(st.get("ties")), st.get("file"),
                 body.get("dropbox_path"), body.get("content_hash")))
            await conn.execute("DELETE FROM bank_txns WHERE statement_id=%s", (sid,))
            async with conn.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO bank_txns (id, account, card, day, amount, description, source, statement_id)
                       VALUES (%s,%s,%s,%s,%s,%s,'statement',%s)""", keep)
    return {"statement": sid, "account": account, "opened": opened.isoformat() if opened else None, "closed": closed.isoformat(),
            "rows": len(keep), "total": round(sum(k[4] for k in keep), 2), "ties": bool(st.get("ties"))}


async def save_qb(body: dict) -> dict:
    """{start, end, rows: [{id, qb_account_id, day, amount, description, payee, category, type, state}]}.
    Replaces QuickBooks rows dated start..end on the accounts the rows name (plus `accounts`, QuickBooks ids, if given).
    id: the QuickBooks transaction id, or 'feed:<feed row id>' for a bank-feed row not yet reviewed."""
    a, b = _d(body["start"]), _d(body["end"])
    rows = body.get("rows") or []
    accts = {QB_ACCOUNTS.get(str(x), f"qb_{x}") for x in (body.get("accounts") or [])}
    keep, bad = [], 0
    for r in rows:
        try:
            d = _d(r["day"])
            amt = round(float(r["amount"]), 2)
        except (KeyError, TypeError, ValueError):
            bad += 1
            continue
        qa = str(r.get("qb_account_id") or "")
        acct = QB_ACCOUNTS.get(qa, f"qb_{qa}")
        if not (a <= d <= b) or not r.get("id"):
            bad += 1
            continue
        accts.add(acct)
        rid = str(r["id"])
        keep.append((f"qb:{qa}:{rid}", acct, d, amt, str(r.get("description") or "")[:500], rid, qa,
                     (str(r.get("category") or "")[:200] or None), (str(r.get("payee") or "")[:200] or None),
                     (str(r.get("type") or "")[:60] or None), (str(r.get("state") or "posted")[:20])))
    async with db.pool.connection() as conn:
        async with conn.transaction():
            # upsert_only: add/refresh just these rows (e.g. one payee's history) without clearing the range
            if not body.get("upsert_only"):
                await conn.execute("DELETE FROM bank_txns WHERE source='quickbooks' AND account = ANY(%s) AND day BETWEEN %s AND %s",
                                   (sorted(accts), a, b))
            async with conn.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO bank_txns (id, account, day, amount, description, source, qb_id, qb_account_id, category, payee,
                           qb_type, qb_state) VALUES (%s,%s,%s,%s,%s,'quickbooks',%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (id) DO UPDATE SET account=EXCLUDED.account, day=EXCLUDED.day, amount=EXCLUDED.amount,
                           description=EXCLUDED.description, category=EXCLUDED.category, payee=EXCLUDED.payee,
                           qb_type=EXCLUDED.qb_type, qb_state=EXCLUDED.qb_state, loaded_at=now()""", keep)
            await conn.execute("INSERT INTO settings (key, value) VALUES ('bank_qb_loaded_at', %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                               (json.dumps({"at": date.today().isoformat(), "start": a.isoformat(), "end": b.isoformat(), "rows": len(keep)}),))
            # full_month: this POST carried every QuickBooks feed row for these accounts in start..end (history backfill)
            if body.get("full_month"):
                r = await (await conn.execute("SELECT value FROM settings WHERE key='bank_full_from'")).fetchone()
                full = (r["value"] if r and isinstance(r["value"], dict) else json.loads(r["value"]) if r else {}) or {}
                for acct in accts:
                    if not full.get(acct) or a.isoformat() < full[acct]:
                        full[acct] = a.isoformat()
                await conn.execute("INSERT INTO settings (key, value) VALUES ('bank_full_from', %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                                   (json.dumps(full),))
    return {"saved": len(keep), "skipped": bad, "accounts": sorted(accts), "start": a.isoformat(), "end": b.isoformat()}


async def summary() -> dict:
    async with db.pool.connection() as conn:
        st = await (await conn.execute(
            """SELECT id, account, opened, closed, n_rows, rows_total, expected_total, ties, file, dropbox_path, content_hash
               FROM bank_statements ORDER BY account, closed""")).fetchall()
        tx = await (await conn.execute(
            """SELECT account, source, count(*) n, min(day) first, max(day) last, round(sum(amount),2) net
               FROM bank_txns GROUP BY 1,2 ORDER BY 1,2""")).fetchall()
        r = await (await conn.execute("SELECT value FROM settings WHERE key='bank_full_from'")).fetchone()
    full = (r["value"] if r and isinstance(r["value"], dict) else json.loads(r["value"]) if r else {}) or {}
    return {"statements": [dict(x) for x in st], "txns": [dict(x) for x in tx],
            "qb_full_from": full}   # first day from which every QuickBooks feed row is loaded, per account


# Contractor payments on the Profit page (Nick, Nov 8 2026): (page line, QuickBooks payee, bank-text pattern).
CONTRACTORS = [
    ("Ariane Armada (Wise)", "Ariane Armada", r"wise.*ariane"),
    ("Leen Rodriguez (ACH)", "Leen Rodriguez", r"(realtime|same-day ach|basic online) payroll payment"),
]


async def contractor_rows(conn, a: date, b: date) -> list[dict]:
    out = []
    for line, payee, pat in CONTRACTORS:
        for r in await (await conn.execute(
                """SELECT day, amount, description, qb_state FROM bank_best
                   WHERE account='chase_5050' AND day BETWEEN %s AND %s AND (payee=%s OR description ~* %s)""",
                (a, b, payee, pat))).fetchall():
            out.append({"line": line, "day": r["day"], "amount": float(r["amount"]), "description": r["description"] or "",
                        "pending": r["qb_state"] == "pending"})
    return out

# Recurring business expenses on the Profit page (Nick, Nov 8 2026): software and insurance, recognised by merchant
# in the stored bank/card transactions. The merchant list is what the bookkeeper files under QuickBooks'
# "Office Expenses:Software & Apps" and "General Business Expenses:Business Insurance" in 2026.
# (page line, vendor shown in the detail, bank-text pattern) — first match wins.
RECURRING = [
    ("Business insurance", "The Hartford", r"hartford"),
    ("Software & apps", "Seller Snap", r"seller ?snap"),
    ("Software & apps", "Keepa", r"keepa"),
    ("Software & apps", "Airtable", r"airtable"),
    ("Software & apps", "InventoryLab (Threecolts)", r"threecolts"),
    ("Software & apps", "QuickBooks", r"intuit.*qbooks|qbooks online"),
    ("Software & apps", "Asinzen", r"asinzen"),
    ("Software & apps", "Datadoe", r"datadoe"),
    ("Software & apps", "Retool", r"retool"),
    ("Software & apps", "Notion", r"notion"),
    ("Software & apps", "Asana", r"asana\.com"),
    ("Software & apps", "Google Workspace", r"google.{0,3}workspace"),
    ("Software & apps", "Dropbox", r"dropbox"),
    ("Software & apps", "JetBrains", r"jetbrains"),
    ("Software & apps", "DigitalOcean", r"digitalocean"),
    ("Software & apps", "Anthropic / Claude", r"anthropic|claude\.ai"),
    ("Software & apps", "OpenAI / ChatGPT", r"openai|chatgpt"),
    ("Software & apps", "Adobe", r"adobe"),
    ("Software & apps", "Lovable", r"lovable"),
    ("Software & apps", "Reventure", r"reventure"),
    ("Software & apps", "PDF tools (Smallpdf, PDF Guru, PDF Expert)", r"smallpdf|pdfguru|pdf expert"),
    ("Software & apps", "Ahrefs", r"ahrefs"),
    ("Software & apps", "GMB Everywhere", r"gmbeverywhere"),
    ("Software & apps", "Local Falcon", r"local ?falcon"),
    ("Software & apps", "Chatbase", r"chatbase"),
    ("Software & apps", "n8n (Paddle)", r"paddle\.net"),
    ("Software & apps", "Twilio", r"twilio"),
    ("Software & apps", "Tello", r"tello us"),
    ("Software & apps", "Porkbun (domains)", r"porkbun"),
    ("Software & apps", "Niimbot", r"niimbot"),
    ("Software & apps", "Kindle / Audible", r"kindle (svcs|unltd)|audible"),
]
_RECUR_ANY = "|".join(f"({p})" for _, _, p in RECURRING)


async def recurring_rows(conn, a: date, b: date) -> list[dict]:
    import re as _re
    pats = [(line, vendor, _re.compile(p, _re.I)) for line, vendor, p in RECURRING]
    out = []
    for r in await (await conn.execute(
            """SELECT day, account, amount, description, qb_state FROM bank_best
               WHERE day BETWEEN %s AND %s AND description ~* %s""", (a, b, _RECUR_ANY))).fetchall():
        d = r["description"] or ""
        for line, vendor, rx in pats:
            if rx.search(d):
                out.append({"line": line, "vendor": vendor, "day": r["day"], "account": r["account"], "amount": float(r["amount"]),
                            "pending": r["qb_state"] == "pending"})
                break
    return out


async def coverage() -> dict:
    """Per account: last statement closing date and last QuickBooks feed day — how current the bank side is."""
    async with db.pool.connection() as conn:
        st = await (await conn.execute("SELECT account, max(closed) d FROM bank_statements GROUP BY 1")).fetchall()
        qb = await (await conn.execute("SELECT account, max(day) d FROM bank_txns WHERE source='quickbooks' GROUP BY 1")).fetchall()
    out: dict = {}
    for r in st:
        out.setdefault(r["account"], {})["statement"] = r["d"].isoformat()
    for r in qb:
        out.setdefault(r["account"], {})["quickbooks"] = r["d"].isoformat()
    return out
