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
- **Prep:** the actual prep-center bills (Rayven and Network Prep payments out of Chase checking, `bank.RECURRING` line "Prep center bills"), in cost of goods sold on the day paid. Replaced the $0.75/unit estimate and its toggle (Nick, Nov 8 2026); the `prep` query parameter is ignored.
- **Cash back (other income):** table `profit_other`, loaded by a daily scheduled task from QuickBooks via `POST /api/engine/profit/other {source, start, end, rows:[{id, day, kind, amount, memo}]}` (replaces that source's rows in start..end). Kinds: `chase_cashback` (Chase Premier …2598 "STATEMENT CREDIT"), `amex_cashback` (Amex Plum …1000 "EARLY PAY DISCOUNT"), dated the day the credit posted. Shown as "Other income" below Amazon profit; the page's Profit includes it.

## Bank and card transactions (`app/bank.py`)
Every transaction from Nick's bank and card accounts (Nick, Nov 8 2026). Tables `bank_statements` (one row per monthly statement: period, balances, `ties` = rows sum to the balance change) and `bank_txns` (source `statement` or `quickbooks`; money out negative). View `bank_best`: statement rows, plus QuickBooks rows on days no loaded statement covers. Loaders: `POST /api/engine/bank/statement` (one parsed statement, replaces it), `POST /api/engine/bank/txns` (QuickBooks rows for start..end, replaces that range), `GET /api/engine/bank` (loaded statements with Dropbox content hashes, counts by account). Statements are PDFs in Dropbox `/Amazon/Statements`, parsed by project doc `claude/bank/parse_statements.py`; a daily scheduled task loads new ones and the last few days from QuickBooks (runbook: project doc `claude/bank/RUNBOOK.md`). `upsert_only: true` on `/api/engine/bank/txns` adds rows without clearing the range; `full_month: true` records the range as complete in `qb_full_from` (history backfill).
- **Software & insurance on the Profit page** (`bank.RECURRING`): merchant patterns (from QuickBooks Software & Apps / Business Insurance, 2026) matched against `bank_best` descriptions on every account; detail by vendor. Add a merchant there when a new subscription starts.
- **Contractors on the Profit page** (`bank.CONTRACTORS`): Ariane Armada (Wise) and Leen Rodriguez (Chase RealTime / Same-Day / Basic Online ACH payroll payments) out of Chase checking, from `bank_best`, matched by QuickBooks payee or bank text; section "Business expenses" between Amazon profit and other income.
- **Missing inbound (toggle):** `?inbound=1` adds units missing from inbound plans whose shipments are all CLOSED (per SKU across the plan, Seller Central counts when present — same as the Reimbursements page), dated the day the plan's last shipment closed, under Inventory written off. Page checkbox "Missing inbound", default on.
- **Inventory written off:** lost (M less found F, sellable) + damaged into WAREHOUSE_DAMAGED (P) + unrecoverable (5) from the inventory ledger, at the same unit cost, table `inv_writeoff`. Also "Restocked customer returns" (kind `returned`, negative units = cost credited back, shown under Product cost): ledger CustomerReturns rows with disposition SELLABLE, on the return day (Nick, Nov 8, 2026; Link My Books never reverses COGS on refunds). Plus "Destroyed by Amazon" (kind `disposed`): disposal orders' disposed quantity from GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA (orders requested since 90 days before `profit_start`), dated the order's last-updated date, replaced on every daily sync (`profit.disposal_sync`). Removals shipped to Nick aren't written off. Also under "Destroyed by Amazon" (kind `destroyed`, Nick, Nov 9, 2026: expense units Amazon surely destroyed): `profit.destroyed_rows` follows each SKU's unsellable stock through the ledger rows kept in `inv_ledger` (FIFO per disposition, each unit marked charged or not: unsellable customer returns and warehouse-damaged units are charged; units Amazon moved out of sellable into defective/expired/distributor-damaged are not) and counts reason D on any unsellable state and reason O on DEFECTIVE (authenticity disposals) for uncharged units only. `inv_ledger` keeps history from 180 days before `profit_start`. Missing inbound is not written off. Synced daily (last 60 days; history once from `profit_start`).

## Sephora relist page (Nov 9, 2026)
- `/relist` (tab "Sephora relist", tab key `relist`): past Sephora sale leads re-priced at today's Amazon price and today's Sephora price, 15%+ ROI only, tiered Ready / Check first.
- Data lives in hub docs under `relist/<name>`; the page lists every doc in that collection (picker appears when there are 2+).
- Load a list from chat with the connector tool `save_relist` (owner only), or bundle a `{"path","data"}` JSON in `app/seed/` — seeds load at startup only if the path doesn't exist yet (never overwrite).

## Sephora leads (Nov 2026)
`/sephora-leads` (tab "Sephora leads", key `sephora_leads`; owners see it, tick it per person in Users & logins). Code: `app/sephora.py`, `static/sephora-leads.html`. A scheduled job keeps an Amazon ASIN ↔ Sephora product catalog and posts the day's buy candidates; Nick reviews them here.
- **Tables (migration 53):** `sephora_catalog` (PK asin + product_id: Sephora ids/name/URL, list prices `price_lo`/`price_hi`, `sale_price`/`on_sale`, `needs_size` + `size_guess` lo/hi/unknown, `confidence` high/medium/low, `sellable` open/approval_required/restricted, shade, Amazon title and numbers — buybox, fba_low, bb_avg90, referral_pct, fba_fee, monthly_sold, offers_fba, amazon_on, roi_now, roi_avg90, watch_price — `extra` jsonb, `active`; plus Nick's answers `match_ok` (true right / false wrong / null unchecked) and `size_confirmed` lo/hi, with `marked_by`/`marked_at`). `sephora_candidates` (uuid id, unique asin + found_on: reason price_watch/sale/new_product, cost = Sephora price Nick would pay before uplift, price_basis, buybox, roi_now, Lead Score band / predicted ROI / notes, note, status new/approved/rejected/expired, reject_reason, decided_by/at, lead_id). `sephora_runs` (kind, at, counts jsonb, `sale_checked`, sale {text, code, ends}).
- **Rules:** the job never writes `match_ok` / `size_confirmed` (posting them is refused). A candidate whose pair is `match_ok=false`, inactive or not in the catalog is skipped. Posting a candidate expires older `new` candidates of the same ASIN; a candidate already decided today keeps its decision. Deactivating a pair expires its open candidates. The active-sale banner is the newest run that checked the sale (sent `sale`), until its `ends` date (Central).
- **Approve** (editor with the Leads tab too): creates the lead with `leads.create_lead` — the Leads page's own path, so Keepa title, hazmat / FBA eligibility checks, Lead Score scoring, audit log and live update all happen — with lead_date today (Central), the ASIN, stores ["Sephora"], brands [brand], store_link = Sephora URL, cost = candidate cost, promo_code = the active sale code, sourced_by "Claude", va_notes = reason, ROI now, Lead Score, shade, size. Refused if a lead for that ASIN is already dated today. Needs-size pairs need a size first ("Mini / smaller size" = lo, "Full size" = hi, saved on the catalog pair); if Nick picks the size the job didn't price, the cost is scaled by the price ratio of the two sizes (keeps the discount) and the note says so. Approving an unchecked pair marks it `match_ok=true`. **Reject** takes an optional reason. **Wrong match** rejects and sets `match_ok=false` (and rejects that pair's other open candidates).
- **Page:** last run per kind with counts, sale banner, counts; "To review" (status new, newest day first, grouped by day), "Decided" (last 30 days, with the lead and its Ordered), "Catalog" (search, brand chips with counts, filters for confidence / sellable / size / match / active, Match OK / Wrong / Mini / Full buttons; click a chosen answer again to clear it). Live updates through SSE event `{"t": "sephora"}`.
- **Engine API (X-Engine-Key):**
  - `POST /api/engine/sephora/catalog {rows:[{asin, product_id, ...catalog fields}]}` ≤1000 → `{upserted, inserted, updated}`. Only the fields a row carries change; a re-sent pair is active again.
  - `POST /api/engine/sephora/catalog/deactivate {keys:[{asin, product_id}]}` → `{deactivated, candidates_expired}`.
  - `GET /api/engine/sephora/catalog?brand=&asin=&active=true|false|all&limit=1000&offset=0` → `{rows, total, offset, limit, next_offset}`.
  - `POST /api/engine/sephora/candidates {rows:[{asin, product_id, found_on?, reason, cost, price_basis, buybox, roi_now, lead_score_band, lead_score_pred_roi, lead_score_notes, note}]}` ≤1000 (found_on defaults to today, Central) → `{inserted, updated, expired, skipped:[{asin, product_id, why}]}`.
  - `GET /api/engine/sephora/candidates?status=new|approved|rejected|expired|a,b|all&since=YYYY-MM-DD&asin=&limit=2000` → `{candidates (joined with the catalog pair and the lead's buy/ordered qty), count}`.
  - `POST /api/engine/sephora/run {kind, counts:{...}, sale:{text, code, ends} | null}` — leave `sale` out when the sale wasn't checked; `null` = checked, no sale → `{id, at, sale_checked, sale}`.
  - `GET /api/engine/sephora/feedback?since=` → `{decisions:[approved/rejected with dates, reason, brand, confidence, lead buy_qty / qty_ordered / actual_spend], brands:[{brand, approved, rejected, wrong_match, units_ordered, spend}]}`.
  - `POST /api/engine/sephora/score {rows:[{asin, cost, url, date?}]}` (≤25) → Lead Score `/preview` one row at a time, no lead_id, nothing written. Returns `{rows:[{asin, ok, band, pred_roi, notes}]}`. Added Nov 9, 2026 so the daily job never holds the Lead Score key and never runs parallel requests.
  - Catalog upserts leave `active` untouched unless it is sent; new pairs default to active (Nov 9, 2026). Sephora job code lives in `jobs/sephora/` (not imported by the app; see its RUNBOOK.md).
- **Web API (session):** `GET /api/sephora/overview`, `GET /api/sephora/candidates?status=&since=`, `GET /api/sephora/catalog?q&brand&confidence&sellable&needs_size=1|open&match=ok|wrong|unchecked&active=1|0|all`, `POST /api/sephora/candidates/{id}/approve {size?}`, `POST /api/sephora/candidates/{id}/reject {reason?, wrong_match?}`, `POST /api/sephora/catalog/mark {asin, product_id, match_ok?, size_confirmed?}`. Activity log entity `sephora`.
- **Connector:** `list_sephora_candidates`, `search_sephora_catalog` (read only).
