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
