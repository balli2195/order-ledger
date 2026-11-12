"""Myrddin Hub web app: website + JSON API + live events + Claude connector."""
from __future__ import annotations

import asyncio
import httpx
import contextlib
import html
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import airtable_sync, auth, db, live
from . import leads as L
from . import prepos
from . import docs as hubdocs
from . import hazmat
from . import transparency
from . import sellable
from . import profit
from . import bank
from . import catalog
from . import timeclock as tc
from . import tasks as T
from . import orders as o
from . import commissions as comm
from . import order_bonus
from . import inbound
from . import sephora
from . import sysmap
from . import giftcards as gc
from . import repricer as rp
from .mcp_server import BASE_URL, mcp, provider

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ledger")

STATIC = Path(__file__).resolve().parent.parent / "static"

mcp_app = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    await hubdocs.seed_missing(Path(__file__).resolve().parent / "seed")   # bundled docs, e.g. Sephora relist lists
    listener = asyncio.create_task(live.listener())
    prepos_task = asyncio.create_task(prepos.nightly_loop())
    brand_task = asyncio.create_task(hazmat.brand_backfill_loop())   # Keepa brands for the Transparency brand warning
    catalog_task = asyncio.create_task(catalog.loop())   # Buy qty entered -> product added to Nick's Amazon catalog
    hazmat_task = asyncio.create_task(sellable.backfill_loop())   # Amazon FBA eligibility (Keepa hazmat comes with title lookups)
    inbound_task = asyncio.create_task(inbound.loop())   # Reimbursements page: inbound shipments from SP-API, daily
    profit_task = asyncio.create_task(profit.loop())   # Profit page: Amazon finance events, rolling refresh + history backfill
    async with mcp.session_manager.run():
        yield
    listener.cancel()
    prepos_task.cancel()
    hazmat_task.cancel()
    catalog_task.cancel()
    inbound_task.cancel()
    profit_task.cancel()
    brand_task.cancel()
    await sysmap.flush()
    await db.close_pool()


app = FastAPI(title="Myrddin Hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

# The daily InventoryLab cost run posts costs straight from the InventoryLab page in Nick's Chrome (Nov 8 2026).
# Only that endpoint answers cross-origin, only to InventoryLab's own sites, and it still needs the engine key.
_IL_ORIGINS = {"https://inventorylab.threecolts.com", "https://inventory-inventorylab.threecolts.com", "https://app.inventorylab.com"}
_CORS_PATHS = ("/api/engine/profit/costs", "/api/engine/giftcards", "/api/engine/giftcards/uses",
               "/api/engine/giftcards/to-check", "/api/engine/giftcards/check-results", "/api/engine/giftcards/order-dates")
# The gift card load posts straight from Nick's signed-in Arbitrage Card tab (Nov 10 2026), so card numbers never pass through chat.
_GC_ORIGINS = {"https://arbitragecard.com", "https://www.arbitragecard.com"}
# Gift card uses are read from the stores' order emails in Nick's signed-in Gmail / Outlook tabs (Nov 10 2026).
_MAIL_ORIGINS = {"https://mail.google.com", "https://outlook.office.com", "https://outlook.cloud.microsoft", "https://outlook.live.com"}
# The Macy's balance script runs in Nick's own macys.com tab (Nov 10 2026).
_STORE_ORIGINS = {"https://www.macys.com", "https://macys.com"}


@app.middleware("http")
async def il_cors(request: Request, call_next):
    origin = request.headers.get("origin", "")
    allowed = {"/api/engine/giftcards": _GC_ORIGINS, "/api/engine/giftcards/uses": _MAIL_ORIGINS, "/api/engine/giftcards/order-dates": _MAIL_ORIGINS,
               "/api/engine/giftcards/to-check": _STORE_ORIGINS, "/api/engine/giftcards/check-results": _STORE_ORIGINS}.get(request.url.path, _IL_ORIGINS)
    if request.url.path in _CORS_PATHS and origin in allowed:
        h = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
             "Access-Control-Allow-Headers": "Content-Type, X-Engine-Key, X-GC-Key", "Access-Control-Max-Age": "600", "Vary": "Origin"}
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=h)
        resp = await call_next(request)
        for k, v in h.items():
            resp.headers[k] = v
        return resp
    return await call_next(request)


@app.middleware("http")
async def engine_call_stats(request: Request, call_next):
    """System map: remember when each /api/engine/* route and the Claude connector were last called (app/sysmap.py)."""
    p = request.url.path
    if not (p.startswith("/api/engine/") or p.startswith("/mcp")):
        return await call_next(request)
    try:
        resp = await call_next(request)
    except Exception:
        route = request.scope.get("route")
        sysmap.record(request.method, getattr(route, "path", p), 500)
        raise
    route = request.scope.get("route")
    sysmap.record(request.method, "/mcp" if p.startswith("/mcp") else getattr(route, "path", p), resp.status_code)
    return resp


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    """Pages and their scripts/styles: browsers re-check on every load (cheap 304 via ETag), so updates show at once."""
    resp = await call_next(request)
    p = request.url.path
    if p.startswith("/static/") or p in ("/", "/leads", "/stores", "/login", "/invite"):
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


@app.exception_handler(o.LedgerError)
async def ledger_error(_: Request, exc: o.LedgerError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def web_actor(user: auth.User) -> o.Actor:
    return o.Actor(user.id, user.label, "web")


def set_session(resp, user_id: int, ver: int) -> None:
    resp.set_cookie(
        auth.COOKIE, auth.make_cookie(user_id, ver), max_age=auth.SESSION_DAYS * 86400,
        httponly=True, secure=auth.SECURE_COOKIES, samesite="lax", path="/",
    )


# ---------------------------------------------------------------- pages

@app.get("/healthz")
async def healthz():
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) n FROM orders")
        n = (await cur.fetchone())["n"]
    return {"ok": True, "orders": n}


async def _page(request: Request, tab: str, file: str):
    user = await auth.current_user(request)
    if not user:
        return RedirectResponse("/login", 302)
    if not user.can_tab(tab):
        home = user.home()
        return RedirectResponse(home if home != request.url.path else "/no-access", 302)
    return FileResponse(STATIC / file, headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
async def index(request: Request):
    return await _page(request, "orders", "index.html")


@app.get("/leads", include_in_schema=False)
async def leads_page(request: Request):
    return await _page(request, "leads", "leads.html")


@app.get("/stores", include_in_schema=False)
async def stores_page(request: Request):
    return await _page(request, "stores", "stores.html")


@app.get("/inventory-health", include_in_schema=False)
async def inventory_health_page(request: Request):
    return await _page(request, "inventory_health", "inventory-health.html")


@app.get("/inventory-health/frame", include_in_schema=False)
async def inventory_health_frame(request: Request):
    return await _page(request, "inventory_health", "inventory-health-frame.html")


@app.get("/brand-sellers", include_in_schema=False)
async def brand_sellers_page(request: Request):
    """The Sephora brands tab was removed (Nick, Nov 11, 2026); its Nov 2 snapshot stays in hub doc brand_sellers/latest."""
    return RedirectResponse("/lead-db", 301)


@app.get("/tasks", include_in_schema=False)
async def tasks_page(request: Request):
    return await _page(request, "tasks", "tasks.html")


@app.get("/timeclock", include_in_schema=False)
async def timeclock_page(request: Request):
    return await _page(request, "timeclock", "timeclock.html")


@app.get("/relist", include_in_schema=False)
async def relist_page(request: Request):
    """Lead DB -> Archived lists: the one-off re-priced lists (relist/* docs). Part of the Lead DB tab since Nov 11, 2026."""
    return await _page(request, "sephora_leads", "relist.html")


@app.get("/lead-db", include_in_schema=False)
async def lead_db_page(request: Request):
    """Lead DB (formerly Sephora leads): store products matched to ASINs, the daily buy queue, archived lists."""
    return await _page(request, "sephora_leads", "sephora-leads.html")


@app.get("/lead-db/guide", include_in_schema=False)
async def lead_db_guide_page(request: Request):
    """How the Lead DB works: tables, jobs, the Keepa collector and the Lead Score training plan (Nov 11, 2026)."""
    return await _page(request, "sephora_leads", "lead-db-guide.html")


@app.get("/api/lead-db/stats")
async def api_lead_db_stats(request: Request):
    """Live counts for the Lead DB guide page."""
    await auth.require(request, tab="sephora_leads")
    from . import training
    return await training.guide_stats()


@app.get("/sephora-leads", include_in_schema=False)
async def sephora_leads_old(request: Request):
    return RedirectResponse("/lead-db", 301)


@app.get("/reimbursements", include_in_schema=False)
async def reimbursements_page(request: Request):
    return await _page(request, "reimbursements", "reimbursements.html")


@app.get("/profit", include_in_schema=False)
async def profit_page(request: Request):
    return await _page(request, "profit", "profit.html")


@app.get("/system", include_in_schema=False)
async def system_page(request: Request):
    return await _page(request, "system_map", "system.html")


@app.get("/api/system-map")
async def api_system_map(request: Request):
    """System map page data: live parts read from the code + the nightly refresh's registry (app/sysmap.py)."""
    await auth.require(request, tab="system_map")
    return await sysmap.build(app)


@app.get("/api/engine/system-map")
async def engine_system_map(request: Request):
    """For the nightly System map refresh: the same data, so it can see new pages, Hub jobs and engine routes."""
    auth.require_engine_key(request)
    return await sysmap.build(app)


def _range(start: str | None, end: str | None) -> tuple[date, date]:
    t = profit.today_pt()
    try:
        a = date.fromisoformat(start) if start else t.replace(day=1)
        b = date.fromisoformat(end) if end else t
    except ValueError:
        raise HTTPException(400, "Dates look like 2026-09-30")
    if b < a:
        a, b = b, a
    if (b - a).days > 400:
        raise HTTPException(400, "Pick 400 days or fewer")
    return a, b


@app.get("/api/profit")
async def api_profit(request: Request, start: str | None = None, end: str | None = None, us: int = 0, inbound: int = 0, prep: int = 0):
    await auth.require(request, tab="profit")
    a, b = _range(start, end)
    return profit_json(await profit.report(a, b, bool(us), bool(inbound), bool(prep)))


def profit_json(r: dict) -> dict:
    from .orders import jsonable
    return jsonable(r)


@app.get("/api/engine/profit")
async def engine_profit(request: Request, start: str | None = None, end: str | None = None, us: int = 0, inbound: int = 0, prep: int = 0):
    """The Profit page's numbers for a date range (Pacific days), for checks against QuickBooks."""
    auth.require_engine_key(request)
    a, b = _range(start, end)
    return profit_json(await profit.report(a, b, bool(us), bool(inbound), bool(prep)))


@app.post("/api/engine/profit/costs")
async def engine_profit_costs(request: Request, body: dict = Body(...)):
    """Unit costs: {source: 'inventorylab'|'il_month'|..., as_of: 'YYYY-MM-DD', rows: [{sku, asin, cost}]}. Replaces that source+day."""
    auth.require_engine_key(request)
    try:
        as_of = date.fromisoformat(str(body.get("as_of")))
    except ValueError:
        raise HTTPException(400, "as_of looks like 2026-11-08")
    src = str(body.get("source") or "inventorylab")[:40]
    return await profit.save_costs(src, as_of, body.get("rows") or [])


@app.post("/api/engine/profit/other")
async def engine_profit_other(request: Request, body: dict = Body(...)):
    """Other income from QuickBooks (card cash back): {source, start, end, rows: [{id, day, kind, amount, memo}]}.
    Replaces that source's rows dated start..end."""
    auth.require_engine_key(request)
    try:
        a, b = date.fromisoformat(str(body.get("start"))), date.fromisoformat(str(body.get("end")))
    except ValueError:
        raise HTTPException(400, "start/end look like 2026-11-08")
    src = str(body.get("source") or "quickbooks")[:40]
    return await profit.save_other(src, a, b, body.get("rows") or [])


@app.post("/api/engine/bank/statement")
async def engine_bank_statement(request: Request, body: dict = Body(...)):
    """One parsed bank/card statement: {statement: {...}, rows: [...], dropbox_path?, content_hash?}. Replaces it if loaded before."""
    auth.require_engine_key(request)
    try:
        return await bank.save_statement(body)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/engine/bank/txns")
async def engine_bank_txns(request: Request, body: dict = Body(...)):
    """QuickBooks bank/card transactions: {start, end, accounts?, rows: [{id, qb_account_id, day, amount, description, payee,
    category, type, state}]}. Replaces QuickBooks rows in start..end on those accounts."""
    auth.require_engine_key(request)
    try:
        return await bank.save_qb(body)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/engine/bank")
async def engine_bank(request: Request):
    """Loaded statements (with content hashes, to skip files already loaded) and transaction counts by account and source."""
    auth.require_engine_key(request)
    return profit_json(await bank.summary())


_profit_run: dict = {}


@app.post("/api/engine/profit/repull")
async def engine_profit_repull(request: Request, body: dict = Body(...)):
    """Re-pull Amazon finance events for {start, end} now (background). GET /api/engine/profit/status to follow it."""
    auth.require_engine_key(request)
    a, b = _range(body.get("start"), body.get("end"))
    t = _profit_run.get("t")
    if t and not t.done():
        return {"running": True}
    async def run():
        try:
            _profit_run["result"] = await profit.repull(a, b)
        except Exception as e:
            _profit_run["result"] = {"error": str(e)[:300]}
    _profit_run["t"] = asyncio.create_task(run())
    return {"started": True, "start": a.isoformat(), "end": b.isoformat()}


@app.post("/api/engine/profit/ledger")
async def engine_profit_ledger(request: Request, body: dict = Body(default={})):
    """Re-read inventory write-offs (lost / damaged in warehouse) from Amazon's inventory ledger from {start} (background)."""
    auth.require_engine_key(request)
    try:
        start = date.fromisoformat(str(body.get("start") or (profit.today_pt() - timedelta(days=60)).isoformat()))
    except ValueError:
        raise HTTPException(400, "start looks like 2026-01-01")
    t = _profit_run.get("ledger")
    if t and not t.done():
        return {"running": True}
    async def run():
        try:
            async with profit._lock:
                _profit_run["ledger_result"] = await profit.ledger_sync(start)
                _profit_run["ledger_result"]["disposals"] = await profit.disposal_sync()
        except Exception as e:
            _profit_run["ledger_result"] = {"error": str(e)[:300]}
    _profit_run["ledger"] = asyncio.create_task(run())
    return {"started": True, "start": start.isoformat()}


@app.get("/api/engine/sku-costs")
async def engine_sku_costs(request: Request):
    """Current InventoryLab unit cost per SKU, for scheduled tasks (replaces the old claude.ai Inventory Hub `costs` export, Nick Nov 11 2026).
    Per SKU: the latest `inventorylab` snapshot cost, else the latest `il_month` cost. $0.01 placeholders are skipped.
    rows = [sku, asin, title(null), cost, source, as_of] — the same column order the old hub's costs rows used."""
    auth.require_engine_key(request)
    async with db.pool.connection() as conn:
        rows = await (await conn.execute("""
            SELECT DISTINCT ON (c.sku) c.sku, coalesce(c.asin, m.asin) asin, c.cost, c.source, c.as_of
            FROM sku_costs c LEFT JOIN sku_asin m ON m.sku = c.sku
            WHERE c.source IN ('inventorylab', 'il_month') AND c.cost > 0.01
            ORDER BY c.sku, (c.source = 'inventorylab') DESC, c.as_of DESC""")).fetchall()
        snap = await (await conn.execute("SELECT max(as_of) d FROM sku_costs WHERE source = 'inventorylab'")).fetchone()
    return {"snapshot_as_of": snap["d"].isoformat() if snap and snap["d"] else None, "count": len(rows),
            "rows": [[r["sku"], r["asin"], None, float(r["cost"]), r["source"], r["as_of"].isoformat()] for r in rows]}


@app.get("/api/engine/profit/status")
async def engine_profit_status(request: Request):
    auth.require_engine_key(request)
    async with db.pool.connection() as conn:
        d = await (await conn.execute("SELECT count(*) n, min(day) first, max(day) last, max(pulled_at) newest FROM fin_days")).fetchone()
        c = await (await conn.execute("SELECT source, as_of, count(*) n FROM sku_costs GROUP BY 1,2 ORDER BY 2 DESC LIMIT 20")).fetchall()
    t = _profit_run.get("t")
    lt = _profit_run.get("ledger")
    return profit_json({"loop": profit.status, "days": d, "costs": c, "repull": {"running": bool(t and not t.done()), "result": _profit_run.get("result")},
                        "ledger": {"running": bool(lt and not lt.done()), "result": _profit_run.get("ledger_result")}})


@app.get("/api/reimbursements")
async def api_reimbursements(request: Request, all: int = 0):
    await auth.require(request, tab="reimbursements")
    return await inbound.page(include_all=bool(all))


@app.post("/api/engine/inbound/seller-central")
async def engine_inbound_sc(request: Request, body: dict = Body(...)):
    """Seller Central's discrepancy table per shipment: {shipments: [{sid, msg, total, rows: [{sku, fnsku, expected, located, disc, status}]}]}."""
    auth.require_engine_key(request)
    return await inbound.save_sc(body.get("shipments") or [])


@app.post("/api/engine/inbound/cases")
async def engine_inbound_cases(request: Request, body: dict = Body(...)):
    """Case status from Amazon's emails: {SID: {case, st, at, opened, reimb, deny, rereview}} (decisions.json shape)."""
    auth.require_engine_key(request)
    return await inbound.save_cases(body.get("cases") if isinstance(body.get("cases"), dict) else body)


_inbound_run: dict = {}


@app.post("/api/engine/inbound/sync")
async def engine_inbound_sync(request: Request):
    """Start a refresh from Amazon now (runs in the background; see diag 'inbound')."""
    auth.require_engine_key(request)
    t = _inbound_run.get("t")
    if t and not t.done():
        return {"running": True}
    async def run():
        try:
            inbound.status["result"] = await inbound.sync()
            inbound.status.update(last_run=_t.strftime("%Y-%m-%d %H:%M:%S"), last_error=None, rss_mb_after=inbound.mem_trim())
            async with db.pool.connection() as conn:
                await conn.execute("INSERT INTO settings (key, value) VALUES ('inbound_synced_at', %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                                   (json.dumps(datetime.now(timezone.utc).isoformat()),))
        except Exception as e:
            inbound.status["last_error"] = str(e)[:300]
    import time as _t
    _inbound_run["t"] = asyncio.create_task(run())
    return {"started": True}


@app.get("/commissions", include_in_schema=False)
async def commissions_page(request: Request):
    return await _page(request, "commissions", "commissions.html")


@app.get("/api/commissions")
async def api_commissions(request: Request):
    user = await auth.require(request, tab="commissions")
    return await comm.summary(user)


@app.post("/api/commissions/paid")
async def api_commissions_paid(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "owner", tab="commissions")
    try:
        d = date.fromisoformat(str(body.get("week")))
    except ValueError:
        raise HTTPException(400, "Week should be its Monday, like 2026-11-02")
    return await comm.set_paid(user, (d - timedelta(days=d.weekday())).isoformat(), bool(body.get("paid")))


@app.get("/api/commissions/{wk}")
async def api_commissions_week(wk: str, request: Request):
    user = await auth.require(request, tab="commissions")
    try:
        d = date.fromisoformat(wk)
    except ValueError:
        raise HTTPException(400, "Week should be its Monday, like 2026-11-02")
    return await comm.week(user, (d - timedelta(days=d.weekday())).isoformat())


@app.get("/api/relist/current")
async def api_relist_current(request: Request, since: str = "2026-10-30"):
    """ASINs with a lead on or after `since` (any store), for the Sephora relist page's "In current sale" column."""
    await auth.require(request, tab="sephora_leads")
    try:
        d = date.fromisoformat(since[:10])
    except ValueError:
        raise HTTPException(400, "since must be a date")
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT upper(btrim(asin)) AS asin, count(*) AS leads, COALESCE(sum(qty_ordered), 0) AS ordered,
                      max(COALESCE(lead_date, created_at::date)) AS last
                 FROM leads WHERE asin IS NOT NULL AND btrim(asin) <> '' AND COALESCE(lead_date, created_at::date) >= %s
                GROUP BY 1""", (d,))
        rows = await cur.fetchall()
    return {"since": d.isoformat(), "asins": {r["asin"]: {"leads": r["leads"], "ordered": int(r["ordered"]), "last": r["last"].isoformat()} for r in rows}}


@app.get("/api/relist/wrong")
async def api_relist_wrong_list(request: Request, path: str):
    """Wrong-match marks made on one relist list."""
    await auth.require(request, tab="sephora_leads")
    return {"marks": await sephora.wrong_matches(list_path=path)}


@app.post("/api/relist/wrong")
async def api_relist_wrong(request: Request, body: dict = Body(...)):
    """{path, asin, sephora_url, product, brand, tags, note, correct_asin} -> match_feedback (+ Sephora catalog when the pair is there)."""
    user = await auth.require(request, "editor", tab="sephora_leads")
    return await sephora.relist_wrong(web_actor(user), body)


@app.delete("/api/relist/wrong/{fid}")
async def api_relist_unwrong(fid: str, request: Request):
    user = await auth.require(request, "editor", tab="sephora_leads")
    return await sephora.relist_unwrong(web_actor(user), fid)


@app.get("/no-access", include_in_schema=False)
async def no_access(request: Request):
    user = await auth.current_user(request)
    if not user:
        return RedirectResponse("/login", 302)
    if user.home() != "/no-access":
        return RedirectResponse(user.home(), 302)
    back = ("""<p><b>You're viewing the Hub as this login.</b> <a href="#" onclick="fetch('/api/view-as',{method:'DELETE',"""
            """headers:{'X-Requested-With':'ledger'}}).then(()=>location.href='/')">Back to my view</a></p>""") if user.viewed_by else ""
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Myrddin Hub</title><link rel="stylesheet" href="/static/style.css"><body><div class="wrap" style="padding-top:60px">
<h1 style="font-family:var(--serif);font-weight:400">Myrddin Hub</h1><p>Hi {html.escape(user.label)}: your login doesn't have any tabs yet.
Ask the owner to give you access.</p>{back}<p><a href="#" onclick="fetch('/api/logout',{{method:'POST',headers:{{'X-Requested-With':'ledger'}}}}).then(()=>location.href='/login')">Sign out</a></p></div></body>""")


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})


@app.get("/invite/{token}", include_in_schema=False)
async def invite_page(token: str):
    return FileResponse(STATIC / "invite.html", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- session API

@app.post("/api/login")
async def api_login(body: dict = Body(...)):
    found = await auth.authenticate(str(body.get("email", "")), str(body.get("password", "")))
    if not found:
        await asyncio.sleep(0.8)
        raise HTTPException(401, "Wrong email or password")
    user, ver = found
    resp = JSONResponse({"user": user.public()})
    set_session(resp, user.id, ver)
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    resp.delete_cookie(auth.VIEW_AS_COOKIE, path="/")
    return resp


@app.get("/api/invite/{token}")
async def api_invite_info(token: str):
    info = await auth.invite_valid(token)
    if not info:
        raise HTTPException(404, "This link has expired or was already used")
    return info


@app.post("/api/invite/{token}")
async def api_invite_redeem(token: str, body: dict = Body(...)):
    user, ver = await auth.redeem_invite(token, str(body.get("password", "")))
    resp = JSONResponse({"user": user.public()})
    set_session(resp, user.id, ver)
    return resp


@app.post("/api/view-as")
async def api_view_as(request: Request, body: dict = Body(...)):
    """Owner: see the whole Hub as another login would (read-only), until Back to my view."""
    owner = await auth.current_user(request, real=True)
    if not owner or owner.role != "owner" or request.headers.get("x-requested-with") != "ledger":
        raise HTTPException(403, "Only an owner can do that")
    uid = int(body.get("user_id") or 0)
    found = await auth.user_by_id(uid)
    if not found or uid == owner.id:
        raise HTTPException(400, "Pick someone else's login")
    target = found[0]
    resp = JSONResponse({"user": target.public(), "home": target.home()})
    resp.set_cookie(auth.VIEW_AS_COOKIE, auth.make_view_cookie(owner.id, uid), max_age=12 * 3600,
                    httponly=True, secure=auth.SECURE_COOKIES, samesite="lax", path="/")
    return resp


@app.get("/api/view-as")
async def api_view_as_list(request: Request):
    owner = await auth.current_user(request, real=True)
    if not owner or owner.role != "owner":
        raise HTTPException(403, "Only an owner can do that")
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT id, email, name, role, tabs FROM users WHERE active AND id<>%s ORDER BY role DESC, lower(COALESCE(NULLIF(name,''), email))", (owner.id,))
        rows = await cur.fetchall()
    return {"users": [{"id": r["id"], "label": r["name"] or r["email"], "email": r["email"], "role": r["role"],
                       "tabs": [auth.TABS[t][0] for t in auth.clean_tabs(r["tabs"])] if r["role"] != "owner" else ["all tabs"]} for r in rows]}


@app.delete("/api/view-as")
async def api_view_as_end():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.VIEW_AS_COOKIE, path="/")
    return resp


@app.get("/api/order-bonus")
async def api_order_bonus(request: Request):
    user = await auth.require(request)
    return {"bonus": await order_bonus.today(user)}


@app.get("/api/order-bonus/switch")
async def api_order_bonus_switch(request: Request):
    user = await auth.require(request)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can turn the bonus on")
    return await order_bonus.switch_status()


@app.post("/api/order-bonus/switch")
async def api_order_bonus_switch_set(request: Request, body: dict = Body(...)):
    user = await auth.require(request)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can turn the bonus on")
    return await order_bonus.switch(bool(body.get("on")))


@app.get("/api/me")
async def api_me(request: Request):
    user = await auth.require(request)
    return {"user": user.public(), "connector_url": f"{BASE_URL}/mcp"}


# ---------------------------------------------------------------- orders API

@app.get("/api/orders")
async def api_orders(request: Request):
    await auth.require(request, tab="orders")
    return {"orders": await o.list_orders()}


@app.get("/api/orders/{order_id}")
async def api_order(order_id: str, request: Request):
    await auth.require(request, tab="orders")
    found = await o.get_order(order_id)
    if not found:
        raise HTTPException(404, "Order not found")
    return found


@app.post("/api/orders")
async def api_create_order(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="orders")
    items = body.pop("items", []) or []
    return await o.create_order(web_actor(user), body, items)


@app.get("/api/prepos")
async def api_prepos(request: Request, through: str | None = None):
    await auth.require(request, tab="prepos")
    cfg = await prepos.config()
    try:
        t = date.fromisoformat(through) if through else cfg["default_through"]
    except ValueError:
        raise HTTPException(400, "through should be a date like 2026-10-29")
    p = await prepos.pending(t)
    for x in p["lines"] + p["held"]:
        x.pop("item_ids", None)
    return {"enabled": cfg["enabled"], "start": cfg["start"].isoformat(), "lag_days": cfg["lag"],
            "default_through": cfg["default_through"].isoformat(), "today": prepos.today().isoformat(),
            "last_nightly": await L.get_setting("prepos_last_nightly", None), "last_error": prepos.status.get("last_error"),
            "pending": p, "history": await prepos.history(200)}


@app.post("/api/orders/{order_id}/prepos")
async def api_order_prepos(order_id: str, request: Request):
    """Send one order's unsent items to PrepOS now; the nightly run then skips them."""
    user = await auth.require(request, "editor", tab="prepos")
    try:
        return await prepos.send(prepos.today(), "order", user.id, order_id)
    except prepos.PreposError as e:
        raise HTTPException(400, str(e))


@app.post("/api/engine/orders/prepos")
async def engine_orders_prepos(request: Request, body: dict = Body(...)):
    """Claude: send several orders' unsent items to PrepOS in one upload (one line per ASIN), only when Nick asks."""
    auth.require_engine_key(request)
    ids = [str(x) for x in (body.get("order_ids") or [])]
    if not ids:
        raise HTTPException(400, "order_ids is required")
    try:
        return await prepos.send(prepos.today(), "order", None, ids)
    except prepos.PreposError as e:
        raise HTTPException(400, str(e))


@app.post("/api/engine/orders/{order_id}/prepos")
async def engine_order_prepos(order_id: str, request: Request):
    """Claude: send one order's unsent items to PrepOS (only when Nick asks); the nightly run then skips them."""
    auth.require_engine_key(request)
    try:
        return await prepos.send(prepos.today(), "order", None, order_id)
    except prepos.PreposError as e:
        raise HTTPException(400, str(e))


@app.post("/api/prepos/send")
async def api_prepos_send(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="prepos")
    try:
        t = date.fromisoformat(str(body.get("through")))
    except ValueError:
        raise HTTPException(400, "through should be a date like 2026-10-29")
    try:
        return await prepos.send(t, "button", user.id)
    except prepos.PreposError as e:
        raise HTTPException(400, str(e))


@app.post("/api/orders/{order_id}/duplicate")
async def api_duplicate_order(order_id: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="orders")
    return {"ids": await o.duplicate_order(web_actor(user), order_id, body.get("count", 1))}


@app.patch("/api/orders/{order_id}")
async def api_update_order(order_id: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="orders")
    return await o.update_order(web_actor(user), order_id, body)


@app.delete("/api/orders/{order_id}")
async def api_delete_order(order_id: str, request: Request):
    user = await auth.require(request, "editor", tab="orders")
    return await o.delete_order(web_actor(user), order_id)


@app.post("/api/orders/{order_id}/items")
async def api_add_items(order_id: str, request: Request, body: Any = Body(...)):
    user = await auth.require(request, "editor", tab="orders")
    items = body if isinstance(body, list) else [body]
    for it in items:   # fill a blank title from Keepa
        if isinstance(it, dict) and it.get("asin") and not str(it.get("product_title") or "").strip():
            t = await L.keepa_title(str(it["asin"]).strip().upper())
            if t: it["product_title"] = t
    return await o.add_items(web_actor(user), order_id, items)


@app.patch("/api/items")
async def api_update_items(request: Request, body: Any = Body(...)):
    user = await auth.require(request, "editor", tab="orders")
    changes = body if isinstance(body, list) else [body]
    return {"orders": await o.update_items(web_actor(user), changes)}


@app.delete("/api/items/{item_id}")
async def api_delete_item(item_id: str, request: Request):
    user = await auth.require(request, "editor", tab="orders")
    return {"orders": await o.delete_items(web_actor(user), [item_id])}


@app.get("/api/meta")
async def api_meta(request: Request):
    await auth.require(request, tab="orders")
    meta = await o.suggestions()
    meta["months"] = await o.monthly_spend(24)
    return meta


@app.put("/api/settings/daily_target")
async def api_daily_target(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "owner")
    try:
        amount = float(body.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Enter a number")
    await o.set_daily_target(web_actor(user), amount)
    return {"daily_target": amount}


@app.get("/api/activity")
async def api_activity(request: Request, limit: int = 100, order_id: str | None = None, entity_id: str | None = None):
    user = await auth.require(request)
    rows = await o.recent_changes(min(limit, 500), order_id, entity_id)
    return {"changes": [c for c in rows if user.can_tab(ENTITY_TAB.get(c.get("entity"), "_owner"))]}


ENTITY_TAB = {"order": "orders", "item": "orders", "batch": "orders", "lead": "leads", "lead_set": "leads", "task": "tasks", "store": "stores", "brand": "stores",
              "prepos": "prepos", "sephora": "sephora_leads"}   # anything else (settings, users) is owner-only

# ---------------------------------------------------------------- leads API

LEAD_FILTERS = ("q", "asin", "store", "brand", "date_from", "date_to", "sourced_by", "ordered", "left", "unscored", "band_min", "roi_min", "replen")


@app.get("/api/leads")
async def api_leads(request: Request, sort: str = "date", desc: bool = True, limit: int = 200, offset: int = 0):
    await auth.require(request, tab="leads")
    f = {k: request.query_params.get(k) for k in LEAD_FILTERS if request.query_params.get(k) not in (None, "")}
    dl = []
    for d in (request.query_params.get("day_totals") or "").split(","):
        try: dl.append(date.fromisoformat(d.strip()))
        except ValueError: pass
    return await L.list_leads(f, sort, desc, limit, offset, dl[:60])


@app.get("/api/leads-meta")
async def api_leads_meta(request: Request):
    await auth.require(request, tab="leads")
    meta = await L.lead_choices()
    meta["sync"] = airtable_sync.status
    return meta


@app.get("/api/hazmat/{asin}")
async def api_hazmat(asin: str, request: Request):
    await auth.require(request, tab=("leads", "orders"))
    f = await hazmat.get(asin)
    if f:
        for k in ("fba_checked_at", "list_checked_at", "amz_brand_checked_at"):
            if f.get(k) and hasattr(f[k], "isoformat"):
                f[k] = f[k].isoformat()
        f["reason_text"] = [{"code": c, "text": sellable.REASONS.get(c, "")} for c in (f.get("fba_reasons") or [])]
        f["hazmat_reason"] = bool(set(f.get("fba_reasons") or []) & sellable.HAZMAT_CODES)
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT status, sku, added_by_hub, issues, error, created_at, checked_at FROM catalog_adds WHERE asin=upper(btrim(%s))", (asin,))
        cat = await cur.fetchone()
    return {"flag": f, "status": sellable.status, "listing_access": sellable.listing_access.get("ok"), "transparency": await transparency.get(asin),
            "tp_brand": await transparency.brand_matches(asin), "catalog": o.jsonable(dict(cat)) if cat else None}


@app.get("/api/transparency")
async def api_transparency(request: Request):
    """ASINs Amazon has emailed Nick about as enrolled in Transparency (units need Transparency codes)."""
    await auth.require(request, tab=("leads", "orders"))
    return await transparency.list_all()


@app.get("/api/engine/diag")
async def engine_diag(request: Request):
    """Server-side timings of the Leads page requests (to check live speed without a login)."""
    auth.require_engine_key(request)
    import time as _t
    out = {}
    t = _t.perf_counter(); lst = await L.list_leads({"date_from": (date.today() - timedelta(days=30)).isoformat()}, "date", True, 200, 0, [])
    out["list_30d_ms"] = round((_t.perf_counter() - t) * 1000)
    rows = lst["leads"]
    if rows:
        t = _t.perf_counter(); await L.get_lead(rows[0]["id"]); out["get_lead_ms"] = round((_t.perf_counter() - t) * 1000)
        a = rows[0].get("asin") or ""
        t = _t.perf_counter(); await transparency.brand_matches(a); await transparency.get(a); await hazmat.get(a)
        out["popup_lookups_ms"] = round((_t.perf_counter() - t) * 1000)
        t = _t.perf_counter(); await L.list_leads({"asin": a, "date_from": rows[0]["lead_date"], "date_to": rows[0]["lead_date"]}, "date", False, 50, 0, [])
        out["same_day_ms"] = round((_t.perf_counter() - t) * 1000)
    st = db.pool.get_stats()
    out["pool"] = {k: st.get(k) for k in ("pool_size", "pool_available", "requests_waiting", "requests_num", "requests_wait_ms", "requests_errors", "connections_lost")}
    async with db.pool.connection() as conn:
        out["schema_version"] = (await (await conn.execute("SELECT max(version) v FROM schema_version")).fetchone())["v"]
        out["brands_filled"] = (await (await conn.execute("SELECT count(*) n FROM asin_flags WHERE brand IS NOT NULL")).fetchone())["n"]
        out["brand_pending"] = (await (await conn.execute(
            "SELECT count(DISTINCT upper(btrim(l.asin))) n FROM leads l LEFT JOIN asin_flags f ON f.asin = upper(btrim(l.asin)) "
            "WHERE l.lead_date >= current_date - 60 AND length(btrim(l.asin)) = 10 AND f.brand_checked_at IS NULL")).fetchone())["n"]
        out["catalog_counts"] = {r["status"]: r["n"] for r in await (await conn.execute("SELECT status, count(*) n FROM catalog_adds GROUP BY 1")).fetchall()}
    out["catalog"] = catalog.status
    out["inbound"] = {k: v for k, v in inbound.status.items()}
    out["memory_mb"] = inbound.mem_mb()
    return out


@app.get("/api/engine/transparency")
async def engine_transparency(request: Request):
    auth.require_engine_key(request)
    return await transparency.list_all()


@app.post("/api/engine/transparency")
async def engine_transparency_add(request: Request, body: dict = Body(...)):
    """For the scheduled email check: {notices: [{asin, title, email_date, kind: enrolled|rejected, effective_date, units_rejected, thread_id}]}."""
    auth.require_engine_key(request)
    try:
        return await transparency.add_notices(body.get("notices") or [])
    except transparency.TransparencyError as e:
        raise HTTPException(400, str(e))


@app.get("/api/leads/{lead_id}")
async def api_lead(lead_id: str, request: Request):
    await auth.require(request, tab="leads")
    found = await L.get_lead(lead_id)
    if not found:
        raise HTTPException(404, "Lead not found")
    return found


@app.post("/api/leads")
async def api_create_lead(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="leads")
    return await L.create_lead(web_actor(user), body)


@app.patch("/api/leads")
async def api_update_leads(request: Request, body: Any = Body(...)):
    user = await auth.require(request, "editor", tab="leads")
    return {"leads": await L.update_leads(web_actor(user), body if isinstance(body, list) else [body])}


@app.post("/api/lead-sets")
async def api_group_leads(request: Request, body: dict = Body(...)):
    """Group leads as one set: {lead_ids: [...], name?}."""
    user = await auth.require(request, "editor", tab="leads")
    return await L.group_leads(web_actor(user), body.get("lead_ids") or [], body.get("name"))


@app.post("/api/lead-sets/ungroup")
async def api_ungroup_leads(request: Request, body: dict = Body(...)):
    """Take leads out of their sets: {lead_ids: [...]}."""
    user = await auth.require(request, "editor", tab="leads")
    return await L.ungroup_leads(web_actor(user), body.get("lead_ids") or [])


@app.delete("/api/lead-sets/{set_id}")
async def api_ungroup_set(set_id: str, request: Request):
    user = await auth.require(request, "editor", tab="leads")
    return await L.ungroup_set(web_actor(user), set_id)


@app.patch("/api/lead-sets/{set_id}")
async def api_rename_set(set_id: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="leads")
    return await L.rename_set(web_actor(user), set_id, body.get("name"))


@app.delete("/api/leads/{lead_id}")
async def api_delete_lead(lead_id: str, request: Request):
    user = await auth.require(request, "editor", tab="leads")
    return {"deleted": await L.delete_leads(web_actor(user), [lead_id])}


@app.post("/api/leads/{lead_id}/score")
async def api_score_lead(lead_id: str, request: Request):
    await auth.require(request, "editor", tab="leads")
    return await L.score_lead(lead_id)


# Rescore every lead on one day (button on the day row of the Leads page; Nick, Nov 8 2026).
# Runs in the background, 3 at a time; each lead's row updates live as its score lands.
_rescore: dict = {}


@app.post("/api/leads-day/rescore")
async def api_rescore_day(request: Request, day: str):
    await auth.require(request, "editor", tab="leads")
    d = date.fromisoformat(day)
    job = _rescore.get(d.isoformat())
    if job and not job["task"].done():
        return {k: v for k, v in job.items() if k != "task"}
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM leads WHERE lead_date=%s AND btrim(coalesce(asin,'')) <> '' AND cost > 0 "
                                 "AND btrim(coalesce(store_link,'')) <> '' ORDER BY created_at", (d,))
        ids = [str(r["id"]) for r in await cur.fetchall()]
    job = {"day": d.isoformat(), "total": len(ids), "done": 0, "ok": 0, "failed": 0, "running": True}
    sem = asyncio.Semaphore(3)

    async def one(i):
        async with sem:
            try:
                r = await L.score_lead(i)
                job["ok" if r.get("ok") else "failed"] += 1
            except Exception:
                job["failed"] += 1
            job["done"] += 1

    async def run():
        try:
            await asyncio.gather(*(one(i) for i in ids))
        finally:
            job["running"] = False
    job["task"] = asyncio.create_task(run())
    _rescore[d.isoformat()] = job
    return {k: v for k, v in job.items() if k != "task"}


@app.get("/api/leads-day/rescore")
async def api_rescore_day_status(request: Request, day: str):
    await auth.require(request, "viewer", tab="leads")
    job = _rescore.get(date.fromisoformat(day).isoformat())
    return {k: v for k, v in job.items() if k != "task"} if job else {"day": day, "running": False, "total": 0, "done": 0}


@app.post("/api/leads-admin/make-home")
async def api_make_home(request: Request):
    user = await auth.require(request, "owner")
    if await L.mode() == "home":
        return {"mode": "home"}
    await airtable_sync.sync_once(full=True)   # final catch-up copy first
    await L.make_home(web_actor(user))
    return {"mode": "home", "final_sync": airtable_sync.status.get("last_counts")}


@app.post("/api/leads-admin/sync-now")
async def api_sync_now(request: Request, full: bool = False):
    await auth.require(request, "owner")
    if await L.mode() != "mirror":
        raise HTTPException(400, "Leads already live here; Airtable sync is off")
    return await airtable_sync.sync_once(full=full)


@app.get("/api/stores")
async def api_stores(request: Request, kind: str = "stores", q: str | None = None):
    await auth.require(request, tab=("stores", "leads"))
    return {"rows": await L.store_stats(kind, q), "mode": await L.mode()}


@app.put("/api/stores/{kind}/{name}")
async def api_set_can_source(kind: str, name: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="stores")
    await L.set_can_source(web_actor(user), kind, name, bool(body.get("can_source")))
    return {"ok": True}


@app.get("/api/events")
async def api_events(request: Request):
    user = await auth.require(request)
    q = live.subscribe()
    kinds = {"order": "orders", "lead": "leads", "store": ("stores", "leads"), "doc": "inventory_health", "timeclock": "timeclock", "task": "tasks", "inbound": "reimbursements", "sephora": "sephora_leads", "giftcards": "gift_cards"}

    def allowed(payload: str) -> bool:
        try:
            t = json.loads(payload).get("t") or "order"
        except Exception:
            return True
        need = kinds.get(t)
        if need is None:
            return True
        return any(user.can_tab(x) for x in ((need,) if isinstance(need, str) else need))

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20)
                    if allowed(payload):
                        yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            live.unsubscribe(q)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- users (owner)

@app.get("/api/users")
async def api_users(request: Request):
    await auth.require(request, "owner")
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, email, name, role, tabs, active, (password_hash IS NOT NULL) AS has_password, created_at "
            "FROM users ORDER BY id"
        )
        return {"users": [o.jsonable(dict(r)) for r in await cur.fetchall()],
                "tabs": [{"key": k, "label": v[0]} for k, v in auth.TABS.items()]}


@app.post("/api/users")
async def api_invite(request: Request, body: dict = Body(...)):
    await auth.require(request, "owner")
    token = await auth.invite_user(str(body.get("email", "")), str(body.get("name", "")), str(body.get("role", "viewer")),
                                   body.get("tabs") or [])
    return {"link": f"{BASE_URL}/invite/{token}"}


@app.patch("/api/users/{user_id}")
async def api_update_user(user_id: int, request: Request, body: dict = Body(...)):
    me = await auth.require(request, "owner")
    sets, args = [], []
    if "role" in body:
        if body["role"] not in auth.ROLES:
            raise HTTPException(400, "Bad role")
        if user_id == me.id and body["role"] != "owner":
            raise HTTPException(400, "You can't remove your own owner access")
        sets.append("role=%s"); args.append(body["role"])
    if "active" in body:
        if user_id == me.id and not body["active"]:
            raise HTTPException(400, "You can't disable yourself")
        sets.append("active=%s"); args.append(bool(body["active"]))
        sets.append("session_ver=session_ver+1")
    if "name" in body:
        sets.append("name=%s"); args.append(str(body["name"]).strip())
    if "tabs" in body:
        sets.append("tabs=%s"); args.append(auth.clean_tabs(body["tabs"]))
    if not sets:
        raise HTTPException(400, "Nothing to change")
    async with db.pool.connection() as conn:
        await conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=%s", args + [user_id])
        if body.get("active") is False:
            await conn.execute("DELETE FROM oauth_tokens WHERE user_id=%s", (user_id,))
        await conn.commit()
    return {"ok": True}


@app.post("/api/users/{user_id}/reset")
async def api_reset_user(user_id: int, request: Request):
    await auth.require(request, "owner")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            token = await auth.create_invite(conn, user_id)
    return {"link": f"{BASE_URL}/invite/{token}"}


# ---------------------------------------------------------------- analysis docs (Inventory health, ...)

@app.get("/api/docs")
async def api_docs_list(request: Request, collection: str):
    user = await auth.require(request)
    if not user.can_tab(hubdocs.tab_for(collection + "/x") or "_owner"):
        raise HTTPException(403, "You don't have access to this tab; ask the owner")
    try:
        return {"docs": await hubdocs.list_prefix(collection)}
    except hubdocs.DocError as e:
        raise HTTPException(400, str(e))


@app.get("/api/docs/{path:path}")
async def api_doc(path: str, request: Request):
    user = await auth.require(request)
    if not user.can_tab(hubdocs.tab_for(path) or "_owner"):
        raise HTTPException(403, "You don't have access to this tab; ask the owner")
    try:
        d = await hubdocs.get(path)
    except hubdocs.DocError as e:
        raise HTTPException(400, str(e))
    return d or {"path": path, "data": None, "version": 0}


@app.get("/api/engine/docs/{path:path}")
async def api_engine_doc(path: str, request: Request):
    """For the scheduled runs: read a document (or, with a path ending in '/', list a collection)."""
    auth.require_engine_key(request)
    try:
        if path.endswith("/"):
            return {"docs": await hubdocs.list_prefix(path.rstrip("/"))}
        return (await hubdocs.get(path)) or {"path": path, "data": None, "version": 0}
    except hubdocs.DocError as e:
        raise HTTPException(400, str(e))


@app.put("/api/engine/docs/{path:path}")
async def api_engine_put_doc(path: str, request: Request, if_version: int | None = None):
    """For the scheduled runs: replace a document with the JSON body (optional ?if_version= guard)."""
    auth.require_engine_key(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be JSON")
    if isinstance(body, dict) and set(body) == {"data"} and isinstance(body["data"], dict):
        body = body["data"]   # also accept the {"data": {...}} shape ArtifactData files use
    try:
        return await hubdocs.put(path, body, if_version)
    except hubdocs.DocError as e:
        raise HTTPException(409 if "version" in str(e) else 400, str(e))


# ---------------------------------------------------------------- tasks (app/tasks.py)

@app.get("/api/tasks")
async def api_tasks(request: Request):
    await auth.require(request, tab="tasks")
    return await T.list_tasks()


@app.get("/api/tasks/count")
async def api_tasks_count(request: Request):
    user = await auth.require(request)
    return {"open": await T.open_count() if user.can_tab("tasks") else 0}


@app.post("/api/tasks")
async def api_task_add(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="tasks")
    return await T.create(web_actor(user), body)


@app.patch("/api/tasks/{tid}")
async def api_task_edit(tid: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="tasks")
    return await T.update(web_actor(user), tid, body)


@app.delete("/api/tasks/{tid}")
async def api_task_delete(tid: str, request: Request):
    user = await auth.require(request, "editor", tab="tasks")
    await T.delete(web_actor(user), tid)
    return {"ok": True}


@app.get("/api/task-templates")
async def api_task_templates(request: Request):
    await auth.require(request, tab="tasks")
    return {"templates": await T.list_templates()}


@app.post("/api/task-templates")
async def api_task_template_add(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="tasks")
    return await T.save_template(web_actor(user), None, body)


@app.patch("/api/task-templates/{tid}")
async def api_task_template_edit(tid: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="tasks")
    return await T.save_template(web_actor(user), tid, body)


@app.delete("/api/task-templates/{tid}")
async def api_task_template_delete(tid: str, request: Request):
    user = await auth.require(request, "editor", tab="tasks")
    await T.delete_template(web_actor(user), tid)
    return {"ok": True}


@app.get("/api/amazon-digest")
async def api_amazon_digest(request: Request, day: str | None = None, last: int = 0):
    await auth.require(request, tab="tasks")
    if last:
        return await T.get_digest(None, last=last)
    try:
        d = date.fromisoformat(day[:10]) if day else None
    except ValueError:
        raise HTTPException(400, "day must be a date")
    return await T.get_digest(d)


@app.put("/api/amazon-digest/{day}/mark")
async def api_digest_mark(day: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="tasks")
    try:
        d = date.fromisoformat(day[:10])
    except ValueError:
        raise HTTPException(400, "day must be a date")
    return await T.mark_bullet(web_actor(user), d, str(body.get("key") or ""), bool(body.get("done")))


@app.post("/api/amazon-digest/{day}/to-task")
async def api_digest_to_task(day: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor", tab="tasks")
    try:
        d = date.fromisoformat(day[:10])
    except ValueError:
        raise HTTPException(400, "day must be a date")
    return await T.bullet_to_task(web_actor(user), d, str(body.get("key") or ""), str(body.get("text") or ""))


@app.post("/api/engine/violations")
async def engine_violations(request: Request, body: dict = Body(...)):
    """One Inbox task per IP / authenticity complaint: {violations:[{kind: ip|authenticity, asin, title, reason, date,
    case_id, appeal, due, sku, summary, email_link}], full: true when it's the complete open list from Account Health}."""
    auth.require_engine_key(request)
    return await T.upsert_violations(body.get("violations") or [], full=bool(body.get("full")))


@app.put("/api/engine/amazon-digest/{day}")
async def engine_amazon_digest(day: str, request: Request, body: dict = Body(...)):
    """For the daily Amazon email run: that day's summary, plus action-item tasks."""
    auth.require_engine_key(request)
    try:
        d = date.fromisoformat(day[:10])
    except ValueError:
        raise HTTPException(400, "day must be a date")
    return await T.put_digest(d, body)


@app.post("/api/engine/tasks/prepos-cancel")
async def engine_prepos_cancel_tasks(request: Request):
    """Recheck: a task for every PrepOS line holding units of canceled orders."""
    auth.require_engine_key(request)
    async with db.pool.connection() as conn:
        async with conn.transaction():
            return {"lines": await T.prepos_cancel_tasks(conn)}


# ---------------------------------------------------------------- time clock (app/timeclock.py)

@app.get("/api/timeclock")
async def api_timeclock(request: Request, week: str | None = None):
    user = await auth.require(request, tab="timeclock")
    return await tc.overview(user, tc.parse_week(week))


@app.get("/api/timeclock/weeks")
async def api_timeclock_weeks(request: Request):
    user = await auth.require(request, tab="timeclock")
    return await tc.weeks(user)


@app.get("/api/timeclock/status")
async def api_timeclock_status(request: Request):
    user = await auth.require(request)
    return await tc.status(user)


@app.post("/api/timeclock/{direction}")
async def api_timeclock_clock(direction: str, request: Request, body: dict = Body(default={})):
    if direction not in ("in", "out"):
        raise HTTPException(404, "Not found")
    user = await auth.require(request, tab="timeclock")
    return await tc.clock(web_actor(user), user, direction, str(body.get("note") or ""))


@app.post("/api/timeclock-admin/entries")
async def api_tc_add_entry(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "owner")
    return await tc.add_entry(web_actor(user), body)


@app.patch("/api/timeclock-admin/entries/{eid}")
async def api_tc_edit_entry(eid: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "owner")
    return await tc.update_entry(web_actor(user), eid, body)


@app.delete("/api/timeclock-admin/entries/{eid}")
async def api_tc_delete_entry(eid: str, request: Request):
    user = await auth.require(request, "owner")
    await tc.delete_entry(web_actor(user), eid)
    return {"ok": True}


@app.post("/api/timeclock-admin/people")
async def api_tc_add_person(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "owner")
    return await tc.add_person(web_actor(user), str(body.get("name") or ""), body.get("rate"))


@app.patch("/api/timeclock-admin/people/{pid}")
async def api_tc_edit_person(pid: int, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "owner")
    return await tc.update_person(web_actor(user), pid, body)


@app.post("/api/timeclock-admin/adjustments")
async def api_tc_add_adj(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "owner")
    return await tc.add_adjustment(web_actor(user), body)


@app.delete("/api/timeclock-admin/adjustments/{aid}")
async def api_tc_delete_adj(aid: str, request: Request):
    user = await auth.require(request, "owner")
    await tc.delete_adjustment(web_actor(user), aid)
    return {"ok": True}


@app.get("/api/timeclock-admin/export.csv")
async def api_tc_export(request: Request, start: str, end: str):
    await auth.require(request, "owner")
    import csv, io
    try:
        a, b = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
    except ValueError:
        raise HTTPException(400, "start and end must be dates")
    rows = await tc.export_rows(a, b)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Person", "Date", "Clock in", "Clock out", "Hours", "Note", "Source"])
    for r in rows:
        w.writerow([r["name"], r["day"], r["clock_in"], r["clock_out"] or "", r["hours"], r["note"], r["source"]])
    return HTMLResponse(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="time-clock-{a}-to-{b}.csv"'})


@app.get("/api/engine/orders/confirm-queue")
async def engine_confirm_queue(request: Request, limit: int = 200):
    """For the scheduled confirmation-email check: orders (from Oct 1, 2026) still waiting for a confirmation email."""
    auth.require_engine_key(request)
    return {"orders": await o.confirm_queue(min(limit, 500))}


@app.post("/api/engine/orders/confirmations")
async def engine_confirmations(request: Request, body: dict = Body(...)):
    auth.require_engine_key(request)
    return await o.save_confirmations(body.get("results") or [])


@app.post("/api/engine/timeclock/import")
async def engine_tc_import(request: Request, body: dict = Body(...)):
    auth.require_engine_key(request)
    return await tc.import_sheet(body)


# ---------------------------------------------------------------- engine API (X-Engine-Key): lead scoring / buy qty jobs

@app.get("/api/engine/leads")
async def engine_leads(request: Request, since: str | None = None, asin: str | None = None,
                       limit: int = 2000, offset: int = 0):
    auth.require_engine_key(request)
    return await L.engine_leads(since, asin, limit, offset)


@app.post("/api/engine/leads/update")
async def engine_leads_update(request: Request, body: dict = Body(...)):
    auth.require_engine_key(request)
    return await L.engine_update(body.get("updates"))


# ---------------------------------------------------------------- Sephora leads (app/sephora.py; Nov 2026)

@app.get("/api/sephora/overview")
async def api_sephora_overview(request: Request):
    await auth.require(request, tab="sephora_leads")
    return await sephora.overview()


@app.get("/api/sephora/candidates")
async def api_sephora_candidates(request: Request, status: str = "new", since: str | None = None, limit: int = 1000):
    await auth.require(request, tab="sephora_leads")
    return await sephora.list_candidates(status, since, limit)


@app.get("/api/sephora/catalog")
async def api_sephora_catalog(request: Request, limit: int = 300, offset: int = 0):
    await auth.require(request, tab="sephora_leads")
    f = {k: request.query_params.get(k) for k in ("q", "store", "brand", "confidence", "sellable", "needs_size", "match", "active")
         if request.query_params.get(k) not in (None, "")}
    return await sephora.page_catalog(f, limit, offset)


@app.post("/api/sephora/candidates/{cid}/approve")
async def api_sephora_approve(cid: str, request: Request, body: dict = Body(default={})):
    """Make a lead from the candidate (same path as the Leads page). {size: 'lo'|'hi'} first for needs-size rows."""
    user = await auth.require(request, "editor", tab=("sephora_leads",))
    if not user.can_tab("leads"):
        raise HTTPException(403, "Approving makes a lead, so it needs the Leads tab too; ask the owner")
    return await sephora.approve(web_actor(user), cid, body.get("size"))


@app.post("/api/sephora/candidates/{cid}/reject")
async def api_sephora_reject(cid: str, request: Request, body: dict = Body(default={})):
    """{reason?, wrong_match?: true} — wrong_match also marks the catalog pair match_ok=false."""
    user = await auth.require(request, "editor", tab="sephora_leads")
    return await sephora.reject(web_actor(user), cid, body.get("reason"), bool(body.get("wrong_match")),
                                {k: body.get(k) for k in ("tags", "note", "correct_asin")})


@app.post("/api/sephora/catalog/mark")
async def api_sephora_mark(request: Request, body: dict = Body(...)):
    """{asin, product_id, match_ok?: true|false|null, size_confirmed?: 'lo'|'hi'|null, tags?, note?, correct_asin?}
    (tags / note / correct_asin say what was wrong when match_ok is false)."""
    user = await auth.require(request, "editor", tab="sephora_leads")
    return await sephora.mark_catalog(web_actor(user), body.get("asin"), body.get("product_id"),
                                      {k: body[k] for k in ("match_ok", "size_confirmed", "tags", "note", "correct_asin") if k in body})


@app.get("/api/engine/training/summary")
async def engine_training_summary(request: Request):
    """Keepa store + Lead DB sizes and the Keepa collector's heartbeat."""
    from . import training
    auth.require_engine_key(request)
    return await training.summary()


@app.get("/api/engine/training/{kind}")
async def engine_training_export(kind: str, request: Request, after: str | None = None, since: str | None = None, limit: int = 1000):
    """Pages of Lead DB / Keepa data for Lead Score training (app/training.py; tools/export_training.py)."""
    from . import training
    auth.require_engine_key(request)
    return await training.export(kind, after, since, limit)


@app.post("/api/engine/sephora/catalog")
async def engine_sephora_catalog(request: Request, body: dict = Body(...)):
    """Upsert catalog pairs: {rows: [{asin, product_id, ...}]} (up to 1000). Never touches match_ok / size_confirmed."""
    auth.require_engine_key(request)
    return await sephora.upsert_catalog(body.get("rows"))


@app.post("/api/engine/sephora/catalog/deactivate")
async def engine_sephora_deactivate(request: Request, body: dict = Body(...)):
    """{keys: [{asin, product_id}]} -> active=false (their open candidates expire)."""
    auth.require_engine_key(request)
    return await sephora.deactivate_catalog(body.get("keys"))


@app.get("/api/engine/sephora/catalog")
async def engine_sephora_catalog_get(request: Request, brand: str | None = None, active: str | None = None,
                                     asin: str | None = None, limit: int = 1000, offset: int = 0):
    auth.require_engine_key(request)
    return await sephora.engine_catalog(brand, active, limit, offset, asin)


@app.post("/api/engine/sephora/candidates")
async def engine_sephora_candidates(request: Request, body: dict = Body(...)):
    """The day's candidates: {rows: [{asin, product_id, found_on, reason, cost, ...}]}, upserted by (asin, found_on)."""
    auth.require_engine_key(request)
    return await sephora.upsert_candidates(body.get("rows"))


@app.get("/api/engine/sephora/candidates")
async def engine_sephora_candidates_get(request: Request, status: str | None = None, since: str | None = None,
                                        asin: str | None = None, limit: int = 2000):
    auth.require_engine_key(request)
    return await sephora.list_candidates(status, since, limit, asin)


@app.post("/api/engine/sephora/run")
async def engine_sephora_run(request: Request, body: dict = Body(...)):
    """{kind: 'catalog'|'daily'|..., counts: {...}, sale: {text, code, ends} | null}. Leave sale out if it wasn't checked."""
    auth.require_engine_key(request)
    return await sephora.record_run(body)


@app.post("/api/engine/sephora/score")
async def engine_sephora_score(request: Request, body: dict = Body(...)):
    """{rows: [{asin, cost, url, date?}]} (max 25). Lead Score /preview one at a time, no lead_id (so no Buy QTY is queued).
    Keeps the Lead Score key on the server and the load on it to one request at a time."""
    auth.require_engine_key(request)
    return await sephora.score_rows(body.get("rows") or [])


@app.get("/api/engine/sephora/feedback")
async def engine_sephora_feedback(request: Request, since: str | None = None):
    auth.require_engine_key(request)
    return await sephora.feedback(since)


# ---------------------------------------------------------------- admin key API (setup, import)

@app.post("/api/admin/invite")
async def admin_invite(request: Request, body: dict = Body(...)):
    auth.require_admin_key(request)
    token = await auth.invite_user(str(body["email"]), str(body.get("name", "")), str(body.get("role", "owner")))
    return {"link": f"{BASE_URL}/invite/{token}"}


@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    auth.require_admin_key(request)
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT (SELECT count(*) FROM orders) orders, (SELECT count(*) FROM order_items) items, "
            "(SELECT COALESCE(sum(order_total),0) FROM orders) order_total_sum, "
            "(SELECT count(*) FROM users) users, (SELECT min(order_date) FROM orders) first_date, "
            "(SELECT max(order_date) FROM orders) last_date"
        )
        stats = o.jsonable(await cur.fetchone())
    rows = await o.list_orders()
    stats["discrepancy_orders"] = sum(1 for r in rows if r["discrepancy"])
    stats["missing_info_orders"] = sum(1 for r in rows if r["missing"])
    return stats


@app.post("/api/admin/import")
async def admin_import(request: Request, body: dict = Body(...)):
    """Upsert orders/items from the old app, keyed by their old ids (safe to re-run)."""
    auth.require_admin_key(request)
    return await _do_import(body)


async def _do_import(body: dict):
    orders_in = body.get("orders") or []
    items_in = body.get("items") or []
    set_types = body.get("set_types") or []
    n_orders = n_items = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            for name in set_types:
                if name and str(name).strip():
                    await conn.execute("INSERT INTO set_types (name) VALUES (%s) ON CONFLICT DO NOTHING", (str(name).strip(),))
            idmap: dict[str, Any] = {}
            for r in orders_in:
                r = dict(r)
                if not str(r.get("store") or "").strip():
                    r["store"] = "(no store)"
                if not r.get("order_date"):
                    r["order_date"] = str(r.get("created_at") or "")[:10]
                f = o.clean_order(r, partial=False)
                cur = await conn.execute(
                    "INSERT INTO orders (legacy_id, order_date, store, email, order_total, order_ref, notes, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,COALESCE(%s::timestamptz, now()),COALESCE(%s::timestamptz, now())) "
                    "ON CONFLICT (legacy_id) DO UPDATE SET order_date=EXCLUDED.order_date, store=EXCLUDED.store, "
                    "email=EXCLUDED.email, order_total=EXCLUDED.order_total, order_ref=EXCLUDED.order_ref, "
                    "notes=EXCLUDED.notes, updated_at=EXCLUDED.updated_at RETURNING id",
                    (r["id"], f["order_date"], f["store"], f.get("email"), f.get("order_total"), f.get("order_ref"),
                     f.get("notes"), r.get("created_at"), r.get("updated_at")),
                )
                idmap[str(r["id"])] = (await cur.fetchone())["id"]
                n_orders += 1
            missing_parent = 0
            for r in items_in:
                parent = idmap.get(str(r["order_id"]))
                if parent is None:
                    cur = await conn.execute("SELECT id FROM orders WHERE legacy_id=%s", (r["order_id"],))
                    row = await cur.fetchone()
                    if not row:
                        missing_parent += 1
                        continue
                    parent = row["id"]
                f = o.clean_item({k: r.get(k) for k in o.ITEM_FIELDS if k in r}, partial=False, strict=False)
                await conn.execute(
                    "INSERT INTO order_items (legacy_id, order_id, asin, product_title, shade, set_type, qty_ordered, cost_per_unit, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s::timestamptz, now()),COALESCE(%s::timestamptz, now())) "
                    "ON CONFLICT (legacy_id) DO UPDATE SET order_id=EXCLUDED.order_id, asin=EXCLUDED.asin, "
                    "product_title=EXCLUDED.product_title, shade=EXCLUDED.shade, set_type=EXCLUDED.set_type, "
                    "qty_ordered=EXCLUDED.qty_ordered, cost_per_unit=EXCLUDED.cost_per_unit, updated_at=EXCLUDED.updated_at",
                    (r["id"], parent, f.get("asin"), f.get("product_title"), f.get("shade"), f.get("set_type"),
                     f.get("qty_ordered", 1), f.get("cost_per_unit"), r.get("created_at"), r.get("updated_at")),
                )
                n_items += 1
            await conn.execute(
                "INSERT INTO audit_log (source, action, entity, after) VALUES ('import','import','batch',%s)",
                (json.dumps({"orders": n_orders, "items": n_items, "skipped_items": missing_parent}),),
            )
            await conn.execute("SELECT pg_notify('ledger', %s)", (json.dumps({"op": "reload"}),))
    return {"orders": n_orders, "items": n_items, "skipped_items_without_order": missing_parent}


# ---------------------------------------------------------------- OAuth login page for the connector

def _oauth_page(body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Claude · Myrddin Hub</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body class="auth-body"><main class="auth-card"><h1 class="brand">Myrddin Hub</h1>{body}</main></body></html>""",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/oauth/login", include_in_schema=False)
async def oauth_login_page(request: Request, req: str = "", error: str = ""):
    found = await provider.pending(req) if req else None
    if not found:
        return _oauth_page("<p>This sign-in request expired. Go back to Claude and click <b>Connect</b> again.</p>")
    user = await auth.current_user(request, real=True)
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    r = html.escape(req)
    if user:
        return _oauth_page(f"""
<p>Let <b>Claude</b> read and edit Myrddin Hub as <b>{html.escape(user.label)}</b> ({user.role})?</p>
<p class="muted">Changes it makes show up live and are logged as "{html.escape(user.label)} via Claude".</p>{err}
<form method="post" class="row"><input type="hidden" name="req" value="{r}">
<button name="action" value="deny" class="btn ghost">Cancel</button>
<button name="action" value="allow" class="btn primary">Allow</button></form>""")
    return _oauth_page(f"""
<p>Sign in to connect Claude to Myrddin Hub.</p>{err}
<form method="post" class="stack"><input type="hidden" name="req" value="{r}">
<label>Email<input name="email" type="email" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button name="action" value="login" class="btn primary">Sign in and allow</button></form>""")


@app.post("/oauth/login", include_in_schema=False)
async def oauth_login_submit(
    request: Request, req: str = Form(""), action: str = Form(""),
    email: str = Form(""), password: str = Form(""),
):
    if action == "deny":
        back = await provider.deny(req)
        return RedirectResponse(back or "/", 302)
    if action == "allow":
        user = await auth.current_user(request, real=True)
        if not user:
            return RedirectResponse(f"/oauth/login?req={req}", 302)
        try:
            return RedirectResponse(await provider.approve(req, user.id), 302)
        except ValueError as e:
            return _oauth_page(f"<p>{html.escape(str(e))}</p>")
    found = await auth.authenticate(email, password)
    if not found:
        await asyncio.sleep(0.8)
        return RedirectResponse(f"/oauth/login?req={req}&error=Wrong+email+or+password", 302)
    user, ver = found
    try:
        resp = RedirectResponse(await provider.approve(req, user.id), 302)
    except ValueError as e:
        return _oauth_page(f"<p>{html.escape(str(e))}</p>")
    set_session(resp, user.id, ver)
    return resp


# ---------------------------------------------------------------- gift cards (app/giftcards.py)

@app.get("/gift-cards", include_in_schema=False)
async def gift_cards_page(request: Request):
    return await _page(request, "gift_cards", "gift-cards.html")


@app.get("/api/gift-cards")
async def api_gift_cards(request: Request):
    """~10k cards is ~4 MB of JSON; gzip it (about 8x smaller) when the browser accepts it."""
    await auth.require(request, tab="gift_cards")
    import gzip
    from fastapi import Response
    body = json.dumps(await gc.list_all(), separators=(",", ":")).encode()
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(gzip.compress(body, 6), media_type="application/json", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    return Response(body, media_type="application/json")


@app.patch("/api/gift-cards/{cid}")
async def api_gift_card_update(cid: int, request: Request, body: dict = Body(...)):
    user = await auth.require(request, role="editor", tab="gift_cards")
    try:
        return await gc.update(cid, body, user.label)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/engine/giftcards")
async def engine_giftcards(request: Request, body: dict = Body(...)):
    """{source, rows:[{o|order_ref, dt|order_date, p|product, n|card_number, pin, face, price, cb}]} -> upsert by (source, card_number)."""
    auth.require_engine_key(request)
    rows = body.get("rows") or []
    if not isinstance(rows, list) or len(rows) > 20000:
        raise HTTPException(400, "rows must be a list of at most 20000")
    return await gc.upsert(body.get("source") or "arbitragecard", rows)


@app.post("/api/engine/giftcards/uses")
async def engine_giftcard_uses(request: Request, body: dict = Body(...)):
    """{rows:[{store, o, dt, l4, a, m?, mb?}]} — gift card payments read from store order emails; re-matches cards by last 4."""
    auth.require_engine_key(request)
    rows = body.get("rows") or []
    if not isinstance(rows, list) or len(rows) > 20000:
        raise HTTPException(400, "rows must be a list of at most 20000")
    return await gc.add_uses(rows)


async def _gc_check_auth(request: Request) -> None:
    """The balance-check script's own key (X-GC-Key) or the engine key."""
    import hmac as _h
    given = request.headers.get("x-gc-key", "")
    if given and _h.compare_digest(given, await gc.check_key()):
        return
    auth.require_engine_key(request)


@app.post("/api/gift-cards/order-dates")
async def api_gift_card_order_dates(request: Request, body: dict = Body(...)):
    """Same as the engine call, signed in as an editor (used by static/inbox-bridge.html)."""
    await auth.require(request, role="editor", tab="gift_cards")
    rows = body.get("rows") or []
    if not isinstance(rows, list) or len(rows) > 20000:
        raise HTTPException(400, "rows must be a list of at most 20000")
    return await gc.set_order_dates("arbitragecard", rows)


@app.post("/api/engine/giftcards/order-dates")
async def engine_giftcard_order_dates(request: Request, body: dict = Body(...)):
    """{source, rows:[{o, dt}]} — Arbitrage Card order number -> order date, read from the order emails in Outlook."""
    auth.require_engine_key(request)
    rows = body.get("rows") or []
    if not isinstance(rows, list) or len(rows) > 20000:
        raise HTTPException(400, "rows must be a list of at most 20000")
    return await gc.set_order_dates(body.get("source") or "arbitragecard", rows)


@app.get("/api/engine/giftcards/to-check")
async def engine_giftcards_to_check(request: Request, store: str = "macys", limit: int = 50, recheck_days: int = 7, include_zero: bool = False):
    await _gc_check_auth(request)
    try:
        return {"cards": await gc.to_check(store, limit, recheck_days, include_zero)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/engine/giftcards/check-results")
async def engine_giftcards_check_results(request: Request, body: dict = Body(...)):
    """{rows:[{id, balance} | {id, error}]} from the store balance script."""
    await _gc_check_auth(request)
    rows = body.get("rows") or []
    if not isinstance(rows, list) or len(rows) > 1000:
        raise HTTPException(400, "rows must be a list of at most 1000")
    return await gc.check_results(rows, str(body.get("who") or "Macy's balance check")[:60])


@app.get("/api/gift-cards/check-script/{store}")
async def api_gift_card_check_script(store: str, request: Request):
    """The paste-in balance script for a store, with the Hub address and the check key filled in (editors only)."""
    await auth.require(request, role="editor", tab="gift_cards")
    from fastapi import Response
    f = STATIC / f"{store}-gc-check.js"
    if store not in gc.CHECK_STORES or not f.exists():
        raise HTTPException(404, "No balance script for that store")
    js = f.read_text().replace("__HUB__", str(request.base_url).rstrip("/").replace("http://", "https://")).replace("__KEY__", await gc.check_key())
    return Response(js, media_type="text/plain", headers={"Cache-Control": "no-store"})


@app.post("/api/engine/giftcards/balances")
async def engine_giftcard_balances(request: Request, body: dict = Body(...)):
    """{source, rows:[{card_number, balance}]} — for a balance-check job."""
    auth.require_engine_key(request)
    return await gc.engine_balances(body.get("source") or "arbitragecard", body.get("rows") or [])


# ---------------------------------------------------------------- repricer (app/repricer.py; the repricer itself runs in the offer recorder)
@app.get("/repricer", include_in_schema=False)
async def repricer_page(request: Request):
    return await _page(request, "repricer", "repricer.html")


async def _rp(fn, *a, **kw):
    try:
        return await fn(*a, **kw)
    except rp.RecorderError as e:
        raise HTTPException(502, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Couldn't reach the recorder: {type(e).__name__}")


@app.get("/api/repricer/overview")
async def api_repricer_overview(request: Request):
    await auth.require(request, tab="repricer")
    src = await rp.key_source()
    if src == "missing":
        return {"connected": False, "key_source": src}
    return dict(await _rp(rp.overview), connected=True, key_source=src)


@app.get("/api/repricer/decisions")
async def api_repricer_decisions(request: Request, n: int = 300, action: str | None = None, asin: str | None = None, sku: str | None = None):
    await auth.require(request, tab="repricer")
    params = {k: v for k, v in dict(n=min(n, 2000), action=action, asin=asin, sku=sku).items() if v}
    return await _rp(rp.call, "GET", "/shadow", params=params)


@app.get("/api/repricer/skus")
async def api_repricer_skus(request: Request):
    await auth.require(request, tab="repricer")
    return await _rp(rp.call, "GET", "/repricer/skus", timeout=60)


@app.get("/api/repricer/suppressed")
async def api_repricer_suppressed(request: Request):
    """In-stock listings with the Buy Box suppressed, with Amazon's competitive price vs min, break-even and cost."""
    await auth.require(request, tab="repricer")
    return await _rp(rp.call, "GET", "/repricer/suppressed", timeout=60)


@app.get("/api/repricer/log")
async def api_repricer_log(request: Request, days: int = 7):
    await auth.require(request, tab="repricer")
    return await _rp(rp.call, "GET", "/live/log", params={"days": min(days, 30)})


@app.post("/api/repricer/settings")
async def api_repricer_settings(request: Request, body: dict = Body(...)):
    user = await auth.require(request, role="owner", tab="repricer")
    before = (await _rp(rp.call, "GET", "/live"))["config"]
    r = await _rp(rp.set_config, body)
    await rp.audit(user, "update", before, r.get("config"))
    return r


@app.post("/api/repricer/stop")
async def api_repricer_stop(request: Request):
    user = await auth.require(request, role="owner", tab="repricer")
    r = await _rp(rp.call, "POST", "/live/stop")
    await rp.audit(user, "stop", None, r.get("config"))
    return r


@app.post("/api/repricer/unfreeze")
async def api_repricer_unfreeze(request: Request, body: dict = Body(...)):
    """Lets a SKU the chase brake paused take price cuts again (owner only)."""
    user = await auth.require(request, role="owner", tab="repricer")
    sku = str(body.get("sku") or "")
    r = await _rp(rp.call, "POST", "/live/unfreeze", body={"sku": sku})
    await rp.audit(user, "unfreeze", {"sku": sku}, r.get("frozen"))
    return r


@app.post("/api/repricer/key")
async def api_repricer_key(request: Request, body: dict = Body(...)):
    user = await auth.require(request, role="owner", tab="repricer")
    key = str(body.get("key") or "").strip()
    if len(key) < 20:
        raise HTTPException(400, "That doesn't look like the recorder key")
    await rp.save_key(key)
    try:
        await rp.call("GET", "/live")
    except Exception as e:
        raise HTTPException(400, f"Saved, but the recorder didn't accept it: {e}")
    await rp.audit(user, "connect", None, {"recorder": rp.URL})
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
# The connector (/mcp) and its OAuth endpoints (/authorize, /token, /register, /.well-known/...).
app.mount("/", mcp_app)

