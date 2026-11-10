"""System map page (Nick, Nov 10 2026): one page that explains every automation and part of the Hub, with diagrams.

It keeps itself current in two ways, so nobody has to remember to add things:
1. Read live from this code on every page load:
   - pages      = auth.TABS (a new tab shows up the moment it is added)
   - Hub jobs   = the asyncio.create_task(...) lines in main.lifespan, described by each module's docstring
   - engine API = every /api/engine/* route, with when it was last called and whether that call worked
                  (engine_calls table, written by the middleware in main.py). Scheduled tasks talk to the Hub
                  through these routes, so this is a health signal that doesn't depend on Claude.
   - reports    = hub_docs collections and when each was last written
2. The nightly "System map refresh" scheduled task lists every Claude scheduled task and writes plain-English
   descriptions to hub doc system_map/registry (jobs/system_map/refresh.py does the mechanical merge).

Anything found live but not yet described in the registry still appears, marked "new", with its docstring.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import auth, db
from . import docs as hubdocs

REPO = Path(__file__).resolve().parent.parent
_pending: dict[tuple[str, str], list] = {}   # (method, route) -> [n, n_err, last_at, last_status, last_ok_at, last_err_at]
_flusher: asyncio.Task | None = None


# ---------------------------------------------------------------- engine call stats

def record(method: str, path: str, status: int) -> None:
    """Called by the middleware for every /api/engine/* and /mcp request. Buffered; flushed every 20 s."""
    global _flusher
    now = datetime.now(timezone.utc)
    p = _pending.setdefault((method, path), [0, 0, None, None, None, None])
    p[0] += 1
    p[2], p[3] = now, status
    if status < 400:
        p[4] = now
    else:
        p[1] += 1
        p[5] = now
    if _flusher is None or _flusher.done():
        try:
            _flusher = asyncio.get_running_loop().create_task(_flush_soon())
        except RuntimeError:
            pass


async def _flush_soon() -> None:
    await asyncio.sleep(20)
    await flush()


async def flush() -> None:
    if not _pending or db.pool is None:
        return
    items = list(_pending.items())
    _pending.clear()
    try:
        async with db.pool.connection() as conn:
            for (m, p), (n, ne, at, st, ok_at, err_at) in items:
                await conn.execute(
                    "INSERT INTO engine_calls (method, path, n, n_err, last_at, last_status, last_ok_at, last_err_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (method, path) DO UPDATE SET "
                    "n = engine_calls.n + EXCLUDED.n, n_err = engine_calls.n_err + EXCLUDED.n_err, "
                    "last_at = EXCLUDED.last_at, last_status = EXCLUDED.last_status, "
                    "last_ok_at = COALESCE(EXCLUDED.last_ok_at, engine_calls.last_ok_at), "
                    "last_err_at = COALESCE(EXCLUDED.last_err_at, engine_calls.last_err_at)",
                    (m, p, n, ne, at, st, ok_at, err_at))
            await conn.commit()
    except Exception as e:   # stats must never break the site
        print("sysmap flush failed:", e, flush=True)


async def call_stats() -> list[dict]:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT method, path, n, n_err, last_at, last_status, last_ok_at, last_err_at, since "
                                 "FROM engine_calls ORDER BY path, method")
        rows = await cur.fetchall()
    return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()} for r in rows]


# ---------------------------------------------------------------- discovery from the code

def _doc_summary(mod_name: str, func: str | None = None) -> str:
    try:
        m = importlib.import_module(f"app.{mod_name}")
        d = inspect.getdoc(getattr(m, func)) if func and hasattr(m, func) else None
        d = d or inspect.getdoc(m) or ""
    except Exception:
        return ""
    para = d.strip().split("\n\n")[0]
    return re.sub(r"\s+", " ", para)[:600]


_TASK_RE = re.compile(r"asyncio\.create_task\(\s*(?:(\w+)\.)?(\w+)\(\)\s*\)\s*(?:#\s*(.*))?")


def hub_jobs() -> list[dict]:
    """Background jobs the Hub runs itself: every asyncio.create_task(module.func()) in main.lifespan."""
    src = (REPO / "app" / "main.py").read_text()
    m = re.search(r"async def lifespan\(.*?\n(.*?)\n    async with mcp", src, re.S)
    body = m.group(1) if m else ""
    aliases = {a.asname or a.name: a.name for node in ast.walk(ast.parse(src)) if isinstance(node, ast.ImportFrom)
               for a in node.names}
    out = []
    for line in body.splitlines():
        t = _TASK_RE.search(line)
        if not t:
            continue
        mod, func, comment = t.group(1), t.group(2), (t.group(3) or "").strip()
        real = aliases.get(mod, mod) if mod else "main"
        out.append({"id": f"job:{real}.{func}", "kind": "hub_job", "name": comment or f"{real}.{func}",
                    "module": real, "func": func, "code_doc": _doc_summary(real, func) if real != "main" else ""})
    return out


def pages() -> list[dict]:
    out = []
    for key, (label, path) in auth.TABS.items():
        mod = {"tasks": "tasks", "timeclock": "timeclock", "commissions": "commissions", "reimbursements": "inbound",
               "profit": "profit", "sephora_leads": "sephora", "leads": "leads", "orders": "orders", "prepos": "prepos"}.get(key)
        out.append({"id": f"page:{key}", "kind": "page", "name": label, "path": path,
                    "own_page": key in auth.PAGE_TABS or key == "system_map",
                    "code_doc": _doc_summary(mod) if mod else ""})
    return out


def engine_routes(app) -> list[dict]:
    out = []
    for r in app.routes:
        p = getattr(r, "path", "")
        if p.startswith("/api/engine/"):
            for m in sorted(getattr(r, "methods", None) or []):
                if m in ("HEAD", "OPTIONS"):
                    continue
                ep = getattr(r, "endpoint", None)
                doc = re.sub(r"\s+", " ", (inspect.getdoc(ep) or "").split("\n\n")[0])[:240] if ep else ""
                out.append({"method": m, "path": p, "doc": doc})
    return out


def repo_jobs() -> list[dict]:
    """Folders under jobs/: code the scheduled tasks clone and run (e.g. jobs/sephora)."""
    out = []
    base = REPO / "jobs"
    for d in sorted(base.iterdir()) if base.exists() else []:
        if d.is_dir():
            files = sorted(f.name for f in d.iterdir() if f.is_file())
            out.append({"folder": f"jobs/{d.name}", "files": files[:40]})
    return out


async def doc_collections() -> list[dict]:
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT split_part(path, '/', 1) c, count(*) n, max(updated_at) at FROM hub_docs GROUP BY 1 ORDER BY 1")
        rows = await cur.fetchall()
    return [{"collection": r["c"], "docs": r["n"], "updated_at": r["at"].isoformat()} for r in rows]


async def table_activity() -> dict:
    """Last-change time of the main tables, so a quiet automation is visible even if it never calls the engine API."""
    q = {
        "orders": "SELECT max(updated_at) FROM orders", "leads": "SELECT max(updated_at) FROM leads",
        "inbound_cases": "SELECT max(updated_at) FROM inbound_cases", "prepos_sends": "SELECT max(sent_at) FROM prepos_sends",
        "sephora_runs": "SELECT max(at) FROM sephora_runs", "amazon_digests": "SELECT max(updated_at) FROM amazon_digests",
        "bank_txns": "SELECT max(loaded_at) FROM bank_txns", "fin_days": "SELECT max(pulled_at) FROM fin_days",
        "transparency_notices": "SELECT max(added_at) FROM transparency_notices",
        "inbound_shipments": "SELECT max(updated_at) FROM inbound_shipments",
    }
    out = {}
    async with db.pool.connection() as conn:
        for k, sql in q.items():
            try:
                async with conn.transaction():
                    r = await (await conn.execute(sql)).fetchone()
                v = list(r.values())[0] if r else None
                out[k] = v.isoformat() if hasattr(v, "isoformat") else v
            except Exception:
                out[k] = None   # column names differ on some tables; fine to skip
    return out


async def build(app) -> dict:
    await flush()
    reg = await hubdocs.get("system_map/registry")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live": {
            "pages": pages(), "hub_jobs": hub_jobs(), "engine_routes": engine_routes(app),
            "repo_jobs": repo_jobs(), "doc_collections": await doc_collections(),
            "engine_calls": await call_stats(), "tables": await table_activity(),
        },
        "registry": (reg or {}).get("data") or {},
        "registry_version": (reg or {}).get("version", 0),
        "registry_updated_at": (reg or {}).get("updated_at"),
    }
