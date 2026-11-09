# Myrddin Hub

Nick's Amazon purchase ledger: one row per store checkout, with line items, live on the web and editable from Claude chat.

- **Website** — Ledger table, order editor with line items, Discrepancy / Missing-info flags, monthly Spent vs Need-to-spend tiles, activity log, user logins.
- **Live** — every change (site or chat) goes through Postgres `NOTIFY` → Server-Sent Events, so open pages update within a second.
- **Claude connector** — MCP endpoint at `/mcp` with OAuth sign-in against the ledger's own logins. Add it in Claude → Customize → Connectors → Add custom connector.

## Stack
FastAPI + Postgres (psycopg 3) on Render. No front-end build: `static/` is plain HTML/CSS/JS.

| Path | What |
| --- | --- |
| `app/main.py` | routes, pages, SSE, admin import, OAuth login page |
| `app/orders.py` | all order/item reads and writes (audited + broadcast) |
| `app/mcp_server.py` | Claude connector tools |
| `app/oauth.py` | OAuth 2.1 server (DCR, PKCE, refresh) for the connector |
| `app/auth.py` | passwords, session cookies, invites, roles |
| `app/db.py` | pool + append-only migrations |
| `app/live.py` | LISTEN/NOTIFY → SSE fan-out |

## Rules
- **Discrepancy**: |Σ qty × cost_per_unit − order_total| > $0.01 × units.
- **Missing info**: any item missing ASIN, title, cost, set type, shade or qty ≥ 1, or the order has no items.
- **Need to spend**: daily target (default $6,800, editable by owner) × days elapsed in the month − spent.
- Roles: `owner` (everything + users + daily target), `editor` (add/edit/delete orders), `viewer` (read only). The connector acts with the signed-in person's role.

## Environment
`DATABASE_URL`, `SECRET_KEY` (session signing), `ADMIN_KEY` (header `X-Admin-Key` for `/api/admin/*`: invite, stats, import), optional `BASE_URL` (defaults to Render's `RENDER_EXTERNAL_URL`).

## Admin API (X-Admin-Key)
- `POST /api/admin/invite {email, name, role}` → one-time link to set a password.
- `GET /api/admin/stats`
- `POST /api/admin/import {orders:[...], items:[...], set_types:[...]}` — upsert keyed by the old app's ids; safe to re-run.

## Local dev
```
export DATABASE_URL=postgresql://postgres@127.0.0.1:5433/orders ADMIN_KEY=dev SECRET_KEY=dev SECURE_COOKIES=0
uvicorn app.main:app --reload
```

## Engine API (added Oct 29, 2026)
For the lead scoring / Claude Buy QTY jobs. Header `X-Engine-Key: <ENGINE_KEY>` (Render env var `ENGINE_KEY`).
- `GET /api/engine/leads?since=YYYY-MM-DD&asin=&limit=2000&offset=0` returns `{leads, total, offset, limit, next_offset}`, oldest first. Each lead has: id, date, created_at, asin, cost, store, stores, store_link, brand, brands, qty_ordered, buy_qty, lead_score_band, lead_score_pred_roi, claude_buy_qty. `limit` goes up to 5000; page until `next_offset` is null.
- `POST /api/engine/leads/update {updates:[{id, claude_buy_qty, claude_buy_note, lead_score_band, lead_score_pred_roi, lead_score_notes}]}` accepts any subset of those fields, up to 200 per call, in one transaction. Edits are audited with source `engine` (shown as "Claude engine") and broadcast live. Any other field is rejected. Fields the engine writes are kept by the site (the Airtable copy won't overwrite them).

## Brand sellers tab (Nov 2, 2026)
- Page /brand-sellers (static/brand-sellers.html), tab key `brand_sellers`: Sephora brands Nick can sell, ranked by distinct third-party sellers. Owners see it; tick it per person in Users & logins.
- Data: hub doc `brand_sellers/latest` = {label, builtAt, notes[], rows:[{rank, brand, sellers, fba, avg, asins, live, me, top_asin, top_n, verdict, new, thin, hist}]}. Refresh with `PUT /api/engine/docs/brand_sellers/latest` (X-Engine-Key); no redeploy needed. Method: project doc claude/sephora-brand-eligibility-2026-11-02.md.

## Profit page (Nov 8, 2026)
`/profit` (tab `profit`): Amazon P&L for any date range, updated through the day. Code: `app/profit.py`, `static/profit.html`.
- **Amazon lines:** SP-API Finances v0 `listFinancialEvents`, one Pacific day per pull (the same posted transactions Link My Books books into QuickBooks; Sep 2026 tied to QuickBooks to the cent on every Amazon.com line). Tables `fin_days`, `fin_agg` (day × currency × P&L line × charge type), `fin_units` (units shipped/refunded per SKU per day). Loop: last 3 days every 3 h, last 14 days daily, history from setting `profit_start` (default 2026-01-01), newest first.
- **FX:** MXN/CAD → USD at the day's ECB rate (frankfurter.dev), table `fx_rates`.
- **COGS:** units shipped × `sku_costs` (latest cost dated on/before the day, else the earliest after). Sources: `il_month` (InventoryLab SKU Profitability cost/unit for that month, as_of = 1st) and `inventorylab` (FBA Inventory active cost/unit snapshot). Fallback: order ledger average cost for the ASIN (`sku_asin`). Returns don't credit cost back (as Link My Books).
- **Engine API (X-Engine-Key):** `GET /api/engine/profit?start&end&us=1`, `POST /api/engine/profit/costs {source, as_of, rows:[{sku, asin, cost}]}` (also accepts cross-origin POSTs from InventoryLab's own pages, for the daily cost run), `POST /api/engine/profit/repull {start, end}`, `GET /api/engine/profit/status`, `POST /api/engine/profit/ledger {start}` (re-read write-offs).
- **Prep (toggle):** `?prep=1` adds setting `profit_prep_per_unit` (default $0.75) × units shipped to Amazon (inbound_items.shipped for shipments that left the prep center), on the plan date, as a COGS line "Prep". Page checkbox, default on.
- **Cash back (other income):** table `profit_other`, loaded by a daily scheduled task from QuickBooks via `POST /api/engine/profit/other {source, start, end, rows:[{id, day, kind, amount, memo}]}` (replaces that source's rows in start..end). Kinds: `chase_cashback` (Chase Premier …2598 "STATEMENT CREDIT"), `amex_cashback` (Amex Plum …1000 "EARLY PAY DISCOUNT"), dated the day the credit posted. Shown as "Other income" below Amazon profit; the page's Profit includes it.

## Bank and card transactions (`app/bank.py`)
Every transaction from Nick's bank and card accounts (Nick, Nov 8 2026). Tables `bank_statements` (one row per monthly statement: period, balances, `ties` = rows sum to the balance change) and `bank_txns` (source `statement` or `quickbooks`; money out negative). View `bank_best`: statement rows, plus QuickBooks rows on days no loaded statement covers. Loaders: `POST /api/engine/bank/statement` (one parsed statement, replaces it), `POST /api/engine/bank/txns` (QuickBooks rows for start..end, replaces that range), `GET /api/engine/bank` (loaded statements with Dropbox content hashes, counts by account). Statements are PDFs in Dropbox `/Amazon/Statements`, parsed by project doc `claude/bank/parse_statements.py`; a daily scheduled task loads new ones and the last few days from QuickBooks (runbook: project doc `claude/bank/RUNBOOK.md`).
- **Missing inbound (toggle):** `?inbound=1` adds units missing from inbound plans whose shipments are all CLOSED (per SKU across the plan, Seller Central counts when present — same as the Reimbursements page), dated the day the plan's last shipment closed, under Inventory written off. Page checkbox "Missing inbound", default on.
- **Inventory written off:** lost (M less found F, sellable) + damaged into WAREHOUSE_DAMAGED (P) + unrecoverable (5) from the inventory ledger, at the same unit cost, table `inv_writeoff`. Missing inbound is not written off. Synced daily (last 60 days; history once from `profit_start`).
