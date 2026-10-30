"""Myrddin Hub web app: website + JSON API + live events + Claude connector."""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
from datetime import date, timedelta
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
from . import orders as o
from .mcp_server import BASE_URL, mcp, provider

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ledger")

STATIC = Path(__file__).resolve().parent.parent / "static"

mcp_app = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    listener = asyncio.create_task(live.listener())
    syncer = asyncio.create_task(airtable_sync.loop())
    prepos_task = asyncio.create_task(prepos.nightly_loop())
    brand_task = asyncio.create_task(hazmat.brand_backfill_loop())   # Keepa brands for the Transparency brand warning
    hazmat_task = asyncio.create_task(sellable.backfill_loop())   # Amazon FBA eligibility (Keepa hazmat comes with title lookups)
    async with mcp.session_manager.run():
        yield
    listener.cancel()
    syncer.cancel()
    prepos_task.cancel()
    hazmat_task.cancel()
    brand_task.cancel()
    await db.close_pool()


app = FastAPI(title="Myrddin Hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


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


@app.get("/no-access", include_in_schema=False)
async def no_access(request: Request):
    user = await auth.current_user(request)
    if not user:
        return RedirectResponse("/login", 302)
    if user.home() != "/no-access":
        return RedirectResponse(user.home(), 302)
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Myrddin Hub</title><link rel="stylesheet" href="/static/style.css"><body><div class="wrap" style="padding-top:60px">
<h1 style="font-family:var(--serif);font-weight:400">Myrddin Hub</h1><p>Hi {html.escape(user.label)}: your login doesn't have any tabs yet.
Ask the owner to give you access.</p><p><a href="#" onclick="fetch('/api/logout',{{method:'POST',headers:{{'X-Requested-With':'ledger'}}}}).then(()=>location.href='/login')">Sign out</a></p></div></body>""")


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


ENTITY_TAB = {"order": "orders", "item": "orders", "batch": "orders", "lead": "leads", "store": "stores", "brand": "stores",
              "prepos": "prepos"}   # anything else (settings, users) is owner-only

# ---------------------------------------------------------------- leads API

LEAD_FILTERS = ("q", "asin", "store", "brand", "date_from", "date_to", "sourced_by", "ordered", "left", "unscored", "band_min", "replen")


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
        for k in ("fba_checked_at",):
            if f.get(k) and hasattr(f[k], "isoformat"):
                f[k] = f[k].isoformat()
        f["reason_text"] = [{"code": c, "text": sellable.REASONS.get(c, "")} for c in (f.get("fba_reasons") or [])]
        f["hazmat_reason"] = bool(set(f.get("fba_reasons") or []) & sellable.HAZMAT_CODES)
    return {"flag": f, "status": sellable.status, "transparency": await transparency.get(asin),
            "tp_brand": await transparency.brand_matches(asin)}


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


@app.delete("/api/leads/{lead_id}")
async def api_delete_lead(lead_id: str, request: Request):
    user = await auth.require(request, "editor", tab="leads")
    return {"deleted": await L.delete_leads(web_actor(user), [lead_id])}


@app.post("/api/leads/{lead_id}/score")
async def api_score_lead(lead_id: str, request: Request):
    await auth.require(request, "editor", tab="leads")
    return await L.score_lead(lead_id)


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
    kinds = {"order": "orders", "lead": "leads", "store": ("stores", "leads"), "doc": "inventory_health"}

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
    user = await auth.current_user(request)
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
        user = await auth.current_user(request)
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


app.mount("/static", StaticFiles(directory=STATIC), name="static")
# The connector (/mcp) and its OAuth endpoints (/authorize, /token, /register, /.well-known/...).
app.mount("/", mcp_app)
