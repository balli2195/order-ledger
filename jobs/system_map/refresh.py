"""Nightly System map refresh: keeps the Hub's /system page in step with every scheduled task and Hub part.

The page reads pages and Hub jobs live from the code. This script handles what the Hub can't see by itself: Claude's
scheduled tasks (from list_triggers). It does the mechanical part; Claude only writes plain-English explanations for
what is new or changed.

  python3 -I refresh.py plan  <triggers.json> <workdir>
      triggers.json = every trigger from list_triggers (all pages; a list, or {"data": [...]} objects, or a list of those).
      Per trigger only these fields are needed: id, name, enabled, cron_expression, run_once_at, next_run_at,
      updated_at, last_fired_at, last_run {status, fired_at, finished_at}. Copy updated_at exactly: it is how a
      changed prompt or schedule is noticed.
      Reads the live map from the Hub (GET /api/engine/system-map), updates schedules, last/next run, on/off for every
      task, and writes:
        <workdir>/draft.json   the registry with all mechanical updates applied
        <workdir>/todo.json    items that need a description from Claude: new tasks, tasks whose prompt or schedule
                               changed, new Hub pages / jobs, plus removed items (already handled, listed for the log)
  python3 -I refresh.py apply <workdir> [descriptions.json] [--dry]
      descriptions.json = {"components": [{id, name?, area, what, reads, writes, endpoints?, tables?, doc?, warn?, asks?,
                           when?}], "nodes": {id: {name, what, side}}, "areas": [...], "changes": ["text", ...]}
      Merges them into draft.json, checks every area/node reference, and PUTs system_map/registry (with if_version).

Env: ENGINE_KEY (the Hub's X-Engine-Key). HUB_URL defaults to https://order-ledger-3mt6.onrender.com.
Writes only the one Hub doc system_map/registry.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HUB = os.environ.get("HUB_URL", "https://order-ledger-3mt6.onrender.com").rstrip("/")
CT = ZoneInfo("America/Chicago")
DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
DOW_LONG = {"Sun": "Sundays", "Mon": "Mondays", "Tue": "Tuesdays", "Wed": "Wednesdays", "Thu": "Thursdays", "Fri": "Fridays", "Sat": "Saturdays"}


def http(method: str, path: str, body=None):
    key = os.environ["ENGINE_KEY"]
    data = json.dumps(body).encode() if body is not None else None
    for i in range(4):
        req = urllib.request.Request(HUB + path, data=data, method=method,
                                     headers={"X-Engine-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) and i < 3:
                time.sleep(30); continue     # Render waking up
            raise SystemExit(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")
        except urllib.error.URLError:
            if i < 3:
                time.sleep(30); continue
            raise


# ---------------------------------------------------------------- cron -> plain schedule

def _field(f: str, lo: int, hi: int) -> list[int]:
    out = set()
    for part in f.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/"); step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = map(int, part.split("-"))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return sorted(out)


def _fmt(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def _ord(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def schedule(cron: str | None, once: str | None) -> tuple[dict, str]:
    """-> (sched, when text), times in Central."""
    if once:
        t = datetime.fromisoformat(once.replace("Z", "+00:00")).astimezone(CT)
        return {"type": "once", "date": t.date().isoformat(), "times": [t.strftime("%H:%M")]}, f"Once, {t.strftime('%b')} {t.day}, {t.year} {_fmt(t)}"
    if not cron:
        return {"type": "manual"}, "When started (no schedule)"
    tz = ZoneInfo("UTC")
    parts = cron.split()
    if parts[0].startswith("CRON_TZ="):
        tz = ZoneInfo(parts[0].split("=", 1)[1]); parts = parts[1:]
    mi, hr, dom, mon, dow = parts
    mins, hrs = _field(mi, 0, 59), _field(hr, 0, 23)
    ref = datetime.now(tz).date()
    # Convert each run time to Central on a reference day (handles UTC schedules and DST).
    def ct(d: date, h: int, m: int) -> datetime:
        return datetime(d.year, d.month, d.day, h, m, tzinfo=tz).astimezone(CT)
    if dom == "*" and dow == "*":
        ts = sorted((ct(ref, h, m) for h in hrs for m in mins), key=lambda t: (t.hour, t.minute))
        times = sorted({t.strftime("%H:%M") for t in ts})
        if len(ts) == 1:
            when = f"Daily {_fmt(ts[0])}"
        else:
            gaps = {(ts[i + 1] - ts[i]).seconds // 60 for i in range(len(ts) - 1)}
            when = (f"Every {gaps.pop() // 60} h, {_fmt(ts[0])}–{_fmt(ts[-1])}" if len(gaps) == 1 and list(gaps)[0] % 60 == 0
                    else "Daily " + ", ".join(_fmt(t) for t in ts))
        return {"type": "daily", "times": times}, when
    if dom == "*":
        days = _field(dow.replace("7", "0"), 0, 6)
        out, names = [], []
        for d in days:
            dd = ref + timedelta(days=(d - (ref.isoweekday() % 7)) % 7)
            for h in hrs:
                for m in mins:
                    t = ct(dd, h, m); out.append(t.strftime("%H:%M")); names.append(DOW[t.isoweekday() % 7])
        times = sorted(set(out)); dn = sorted(set(names), key=DOW.index)
        t0 = ct(ref, hrs[0], mins[0])
        when = (DOW_LONG[dn[0]] if len(dn) == 1 else "/".join(dn)) + f" {_fmt(t0)}"
        if dn == ["Mon", "Tue", "Wed", "Thu", "Fri"]:
            when = f"Weekdays {_fmt(t0)}"
        return {"type": "weekly", "dows": dn, "times": times}, when
    days = _field(dom, 1, 31)
    t0 = ct(ref, hrs[0], mins[0])
    dtxt = _ord(days[0]) if len(days) == 1 else (f"{_ord(days[0])}–{_ord(days[-1])}" if days == list(range(days[0], days[-1] + 1)) else ", ".join(map(_ord, days)))
    return {"type": "monthly", "dom": days, "times": [t0.strftime("%H:%M")]}, f"{dtxt} of each month, {_fmt(t0)}"


# ---------------------------------------------------------------- plan / apply

def load_triggers(p: str) -> list[dict]:
    j = json.load(open(p))
    out = []
    def walk(x):
        if isinstance(x, list):
            for y in x: walk(y)
        elif isinstance(x, dict):
            if "data" in x and isinstance(x["data"], list): walk(x["data"])
            elif "id" in x and str(x["id"]).startswith("trig_"): out.append(x)
    walk(j)
    seen, uniq = set(), []
    for t in out:
        if t["id"] not in seen:
            seen.add(t["id"]); uniq.append(t)
    return uniq


def prompt_of(t: dict) -> str:
    return (t.get("derived_state") or {}).get("prompt") or t.get("prompt") or ""


def c_hash(c):
    return (c or {}).get("prompt_hash")


def plan(trig_path: str, work: str) -> None:
    os.makedirs(work, exist_ok=True)
    trig = load_triggers(trig_path)
    if not trig:
        raise SystemExit("No triggers found in " + trig_path + " — refusing to mark every task removed")
    m = http("GET", "/api/engine/system-map")
    reg = m.get("registry") or {}
    reg.setdefault("components", []); reg.setdefault("changes", []); reg.setdefault("removed", [])
    by = {c["id"]: c for c in reg["components"]}
    today = datetime.now(CT).date().isoformat()
    todo = {"new": [], "changed": [], "removed": [], "new_hub_parts": [], "gone_hub_parts": []}
    live_ids = set()
    for t in trig:
        cid = "task:" + t["id"]; live_ids.add(cid)
        sched, when = schedule(t.get("cron_expression") or None, t.get("run_once_at"))
        pr = prompt_of(t)
        basis = t.get("updated_at") or pr   # a task's updated_at changes whenever its prompt or schedule is edited
        ph = hashlib.sha1((basis + "|" + (t.get("cron_expression") or "") + "|" + (t.get("run_once_at") or "")).encode()).hexdigest()[:12] if basis else None
        lr = t.get("last_run") or {}
        st = {"ROUTINE_RUN_STATUS_SUCCEEDED": "ok", "ROUTINE_RUN_STATUS_FAILED": "failed"}.get(lr.get("status", ""), "running" if lr.get("status") else None)
        mech = {"kind": "scheduled", "trigger_id": t["id"], "enabled": bool(t.get("enabled", True)), "cron": t.get("cron_expression") or "",
                "sched": sched, "when_auto": when, "next_run": t.get("next_run_at") or t.get("run_once_at"),
                "last_run": {"at": lr.get("finished_at") or lr.get("fired_at") or t.get("last_fired_at"), "status": st} if (lr or t.get("last_fired_at")) else None,
                "prompt_hash": ph or c_hash(by.get(cid))}
        c = by.get(cid)
        ctx = {"id": cid, "name": t.get("name"), "when_auto": when, "enabled": mech["enabled"], "prompt": pr[:6000],
               "connectors": [x.get("name") for x in t.get("mcp_connections") or []]}
        if not c:
            c = {"id": cid, "name": t.get("name"), "area": "other", "what": "", "reads": [], "writes": [], "added": today}
            reg["components"].append(c); by[cid] = c
            todo["new"].append(ctx)
            reg["changes"].append({"date": today, "text": f"New scheduled task: {t.get('name')}"})
        elif c.get("prompt_hash") and ph and c.get("prompt_hash") != ph:
            todo["changed"].append({**ctx, "current": {k: c.get(k) for k in ("name", "area", "what", "reads", "writes", "endpoints", "doc", "warn", "when")}})
        elif c.get("enabled") is not None and c.get("enabled") != mech["enabled"]:
            reg["changes"].append({"date": today, "text": f"{t.get('name')} turned {'on' if mech['enabled'] else 'off'}"})
        if c.get("name") != t.get("name") and c.get("name"):
            reg["changes"].append({"date": today, "text": f"Renamed: {c['name']} → {t.get('name')}"})
            c["name"] = t.get("name")
        # keep a hand-written "when" only while the schedule is unchanged
        if c.get("cron", None) is not None and c.get("cron") != mech["cron"]:
            c.pop("when", None)
        c.update(mech)
        c.setdefault("when", when)
    for c in list(reg["components"]):
        if c.get("kind") == "scheduled" and c["id"] not in live_ids:
            reg["components"].remove(c)
            reg["removed"].append({"id": c["id"], "name": c.get("name"), "removed": today})
            reg["changes"].append({"date": today, "text": f"Scheduled task removed: {c.get('name')}"})
            todo["removed"].append(c.get("name"))
    # Hub pages / jobs read live from the code
    live = m.get("live") or {}
    hub_live = {p["id"]: p for p in live.get("pages", []) if p.get("own_page")} | {j["id"]: j for j in live.get("hub_jobs", [])}
    for hid, h in hub_live.items():
        if hid not in by:
            todo["new_hub_parts"].append({"id": hid, "kind": "page" if hid.startswith("page:") else "hub_job", "name": h.get("name"), "path": h.get("path"), "code_doc": h.get("code_doc")})
    for c in list(reg["components"]):
        if c.get("kind") in ("page", "hub_job") and c["id"] not in hub_live:
            reg["components"].remove(c)
            reg["removed"].append({"id": c["id"], "name": c.get("name"), "removed": today})
            reg["changes"].append({"date": today, "text": f"Removed from the Hub: {c.get('name')}"})
            todo["gone_hub_parts"].append(c.get("name"))
    todo["engine_routes_unused_by_any_component"] = sorted(
        {f"{r['method']} {r['path']}" for r in live.get("engine_routes", [])}
        - {e for c in reg["components"] for e in c.get("endpoints", [])}
        - {f"{r['method']} {e}" for r in live.get("engine_routes", []) for c in reg["components"] for e in c.get("endpoints", []) if e == r["path"]})
    todo["areas"] = [a["id"] for a in reg.get("areas", [])]
    todo["nodes"] = sorted((reg.get("nodes") or {}).keys())
    reg["changes"] = reg["changes"][-200:]
    reg["removed"] = reg["removed"][-100:]
    json.dump({"registry": reg, "version": m.get("registry_version", 0)}, open(os.path.join(work, "draft.json"), "w"), indent=1)
    json.dump(todo, open(os.path.join(work, "todo.json"), "w"), indent=1)
    print(f"tasks {len(trig)} | new {len(todo['new'])} | changed {len(todo['changed'])} | removed {len(todo['removed'])} | "
          f"new hub parts {len(todo['new_hub_parts'])} | gone hub parts {len(todo['gone_hub_parts'])} | "
          f"engine routes no component lists: {len(todo['engine_routes_unused_by_any_component'])}")


FIELDS = ("name", "area", "what", "reads", "writes", "endpoints", "tables", "doc", "warn", "asks", "when", "kind", "path", "url", "sched")


def apply(work: str, desc_path: str | None, dry: bool) -> None:
    d = json.load(open(os.path.join(work, "draft.json")))
    reg, ver = d["registry"], d["version"]
    today = datetime.now(CT).date().isoformat()
    if desc_path:
        ds = json.load(open(desc_path))
        for a in ds.get("areas", []):   # new areas go before "other" (kept last), in the order given
            areas = reg.setdefault("areas", [])
            if not any(x["id"] == a["id"] for x in areas):
                i = next((k for k, x in enumerate(areas) if x["id"] == "other"), len(areas))
                areas.insert(i, a)
        reg.setdefault("nodes", {}).update(ds.get("nodes", {}))
        by = {c["id"]: c for c in reg["components"]}
        for c in ds.get("components", []):
            cur = by.get(c["id"])
            if not cur:
                cur = {"id": c["id"], "added": today}; reg["components"].append(cur); by[c["id"]] = cur
                reg["changes"].append({"date": today, "text": f"Added to the map: {c.get('name', c['id'])}"})
            for k in FIELDS:
                if k in c:
                    cur[k] = c[k]
        for t in ds.get("changes", []):
            reg["changes"].append({"date": today, "text": t})
    # checks
    areas = {a["id"] for a in reg.get("areas", [])} | {"other"}
    nodes = set((reg.get("nodes") or {}).keys())
    pages = {c["id"] for c in reg["components"] if c.get("kind") == "page"}
    bad = []
    for c in reg["components"]:
        if c.get("area") not in areas: bad.append(f"{c['id']}: unknown area {c.get('area')}")
        for r in c.get("reads", []) + c.get("writes", []):
            if r not in nodes and r not in pages: bad.append(f"{c['id']}: unknown node {r}")
        if not c.get("what"): bad.append(f"{c['id']}: no description yet")
    reg["updated_at"] = datetime.now(timezone.utc).isoformat()
    reg["refreshed_by"] = "System map refresh (nightly scheduled task)"
    print(f"{len(reg['components'])} components; {len(bad)} problems")
    for b in bad: print("  -", b)
    if [b for b in bad if "unknown" in b]:
        raise SystemExit("Fix the unknown areas/nodes (add them under 'nodes'/'areas' in descriptions.json) and re-run apply")
    json.dump(reg, open(os.path.join(work, "registry.json"), "w"), indent=1)
    if dry:
        print("dry run: not sent"); return
    r = http("PUT", f"/api/engine/docs/system_map/registry?if_version={ver}" if ver else "/api/engine/docs/system_map/registry", reg)
    print("PUT system_map/registry ->", r)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "plan" and len(sys.argv) >= 4:
        plan(sys.argv[2], sys.argv[3])
    elif cmd == "apply" and len(sys.argv) >= 3:
        args = [a for a in sys.argv[3:] if not a.startswith("--")]
        apply(sys.argv[2], args[0] if args else None, "--dry" in sys.argv)
    else:
        print(__doc__); sys.exit(2)
