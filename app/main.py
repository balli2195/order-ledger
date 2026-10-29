"""Order Ledger web app: website + JSON API + live events + Claude connector."""
from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, db, live
from . import orders as o
from .mcp_server import BASE_URL, mcp, provider

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ledger")

STATIC = Path(__file__).resolve().parent.parent / "static"

mcp_app = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    listener = asyncio.create_task(live.listener())
    async with mcp.session_manager.run():
        yield
    listener.cancel()
    await db.close_pool()


app = FastAPI(title="Order Ledger", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


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


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not await auth.current_user(request):
        return RedirectResponse("/login", 302)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


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
    await auth.require(request)
    return {"orders": await o.list_orders()}


@app.get("/api/orders/{order_id}")
async def api_order(order_id: str, request: Request):
    await auth.require(request)
    found = await o.get_order(order_id)
    if not found:
        raise HTTPException(404, "Order not found")
    return found


@app.post("/api/orders")
async def api_create_order(request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor")
    items = body.pop("items", []) or []
    return await o.create_order(web_actor(user), body, items)


@app.patch("/api/orders/{order_id}")
async def api_update_order(order_id: str, request: Request, body: dict = Body(...)):
    user = await auth.require(request, "editor")
    return await o.update_order(web_actor(user), order_id, body)


@app.delete("/api/orders/{order_id}")
async def api_delete_order(order_id: str, request: Request):
    user = await auth.require(request, "editor")
    return await o.delete_order(web_actor(user), order_id)


@app.post("/api/orders/{order_id}/items")
async def api_add_items(order_id: str, request: Request, body: Any = Body(...)):
    user = await auth.require(request, "editor")
    items = body if isinstance(body, list) else [body]
    return await o.add_items(web_actor(user), order_id, items)


@app.patch("/api/items")
async def api_update_items(request: Request, body: Any = Body(...)):
    user = await auth.require(request, "editor")
    changes = body if isinstance(body, list) else [body]
    return {"orders": await o.update_items(web_actor(user), changes)}


@app.delete("/api/items/{item_id}")
async def api_delete_item(item_id: str, request: Request):
    user = await auth.require(request, "editor")
    return {"orders": await o.delete_items(web_actor(user), [item_id])}


@app.get("/api/meta")
async def api_meta(request: Request):
    await auth.require(request)
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
async def api_activity(request: Request, limit: int = 100, order_id: str | None = None):
    await auth.require(request)
    return {"changes": await o.recent_changes(min(limit, 500), order_id)}


@app.get("/api/events")
async def api_events(request: Request):
    await auth.require(request)
    q = live.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20)
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
            "SELECT id, email, name, role, active, (password_hash IS NOT NULL) AS has_password, created_at "
            "FROM users ORDER BY id"
        )
        return {"users": [o.jsonable(dict(r)) for r in await cur.fetchall()]}


@app.post("/api/users")
async def api_invite(request: Request, body: dict = Body(...)):
    await auth.require(request, "owner")
    token = await auth.invite_user(str(body.get("email", "")), str(body.get("name", "")), str(body.get("role", "viewer")))
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
        return o.jsonable(await cur.fetchone())


@app.post("/api/admin/import-raw")
async def admin_import_raw(request: Request):
    """TEMPORARY: same as /api/admin/import, but takes text/plain with the key in the body,
    so the old app's page can send its data without a CORS preflight. Remove after migration."""
    import hmac as _h
    data = json.loads(await request.body())
    if not auth.ADMIN_KEY or not _h.compare_digest(str(data.pop("admin_key", "")), auth.ADMIN_KEY):
        raise HTTPException(401, "Bad admin key")
    return await _do_import(data)


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
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Claude · Order Ledger</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body class="auth-body"><main class="auth-card"><h1 class="brand">Order Ledger</h1>{body}</main></body></html>""",
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
<p>Let <b>Claude</b> read and edit the ledger as <b>{html.escape(user.label)}</b> ({user.role})?</p>
<p class="muted">Changes it makes show up live and are logged as "{html.escape(user.label)} via Claude".</p>{err}
<form method="post" class="row"><input type="hidden" name="req" value="{r}">
<button name="action" value="deny" class="btn ghost">Cancel</button>
<button name="action" value="allow" class="btn primary">Allow</button></form>""")
    return _oauth_page(f"""
<p>Sign in to connect Claude to the ledger.</p>{err}
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
