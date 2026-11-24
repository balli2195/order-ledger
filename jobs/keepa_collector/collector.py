#!/usr/bin/env python3
"""Keepa collector: always-on Render background worker that keeps Keepa data for every Lead DB ASIN (Nov 11, 2026).

Why: Lead Score training needs what was true on each day: offers, sellers and stock. Keepa can't give those back
later, so they have to be recorded while they're current. Price history builds on itself, so only the latest full
pull per ASIN is kept.

What it does, forever:
  1. Every 10 minutes it reads the ASIN list from the Hub database:
     - hot: Lead DB pairs the daily finder watches (active, open to sell, not marked wrong), pairs on sale or with open candidates, and Nick's leads from the last 30 days -> pulled every HOT_HOURS
     - cold: every other active Lead DB ASIN -> pulled every COLD_HOURS
     Pairs Nick or Claude marked wrong are left out (Nov 19, 2026): they only matter for review, not for leads.
     Listings Nick is restricted from selling are left out too (Nov 20, 2026), unless they're one of his recent leads.
  2. Pulls the most overdue ASINs from Keepa, 20 per request (history, stats, Buy Box, 20 offers with stock, rating),
     at a steady pace spread over the whole day (Nick, Nov 23, 2026): the day's need (hot ASINs every HOT_HOURS, cold
     every COLD_HOURS, at the recent tokens per ASIN) divided by 1,440 minutes, plus HEADROOM. When nothing is overdue
     it keeps that pace by pulling the ASINs closest to due (at least PULL_AHEAD of their interval old), so pull times
     spread out instead of coming due together; a backlog of more than an hour catches up at up to KEEPA_MAX_PER_MIN.
  3. Writes keepa_raw (latest full pull, gzipped, replaced) and one keepa_snapshots row (today's offers/stock).
  4. Spends only spare tokens: it never takes the shared key below KEEPA_RESERVE and averages at most
     KEEPA_MAX_PER_MIN tokens a minute, so the daily Sephora job, Lead Score and the other jobs always have room.
  5. Writes a heartbeat to keepa_collector_status (shown on the System map).

Env: DATABASE_URL (the Hub database, internal URL), KEEPA_KEY.
Optional: KEEPA_RESERVE (3000), KEEPA_MAX_PER_MIN (90), HOT_HOURS (22), COLD_HOURS (72), BATCH (20), PAUSED (1 = idle),
HEADROOM (1.15), PULL_AHEAD (0.6).
Run: python3 -m jobs.keepa_collector.collector   (Render start command)    Test: add --once to do one batch and exit.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import psycopg
from psycopg.rows import dict_row

KEY = os.environ.get("KEEPA_KEY", "")
DB = os.environ.get("DATABASE_URL", "")
RESERVE = int(os.environ.get("KEEPA_RESERVE", "3000"))
MAX_PER_MIN = float(os.environ.get("KEEPA_MAX_PER_MIN", "90"))
HOT_H = float(os.environ.get("HOT_HOURS", "22"))
COLD_H = float(os.environ.get("COLD_HOURS", "72"))
BATCH = int(os.environ.get("BATCH", "20"))
HEADROOM = float(os.environ.get("HEADROOM", "1.15"))       # steady pace = the day's need x this
PULL_AHEAD = float(os.environ.get("PULL_AHEAD", "0.6"))    # nothing overdue: pull ASINs at least this share of their interval old
KEEPA_EPOCH = dt.datetime(2011, 1, 1, tzinfo=dt.timezone.utc)
STOP = False


def log(*a):
    print(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def _stop(*_):
    global STOP
    STOP = True
    log("stop requested")


def kt(m):
    """Keepa minutes -> ISO time."""
    return (KEEPA_EPOCH + dt.timedelta(minutes=int(m))).isoformat() if m is not None and m >= 0 else None


def keepa(path, **q):
    q["key"] = KEY
    url = f"https://api.keepa.com/{path}?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
    return json.loads(raw)


def cents(v):
    return round(v / 100, 2) if isinstance(v, (int, float)) and v > 0 else None


def snapshot(p: dict) -> dict:
    """The point-in-time part of a Keepa product: current prices, who sells, at what price, with how much stock."""
    st = p.get("stats") or {}
    cur = st.get("current") or []
    g = lambda i: cur[i] if len(cur) > i and cur[i] is not None and cur[i] >= 0 else None   # noqa: E731
    offers, live = [], set(p.get("liveOffersOrder") or [])
    for i, o in enumerate(p.get("offers") or []):
        oc, sc = o.get("offerCSV") or [], o.get("stockCSV") or []
        price = (oc[-2] + max(oc[-1], 0)) if len(oc) >= 3 and oc[-2] >= 0 else None
        offers.append({"seller": o.get("sellerId"), "fba": o.get("isFBA"), "amazon": o.get("isAmazon"), "prime": o.get("isPrime"),
                       "cond": o.get("condition"), "price": cents(price), "stock": sc[-1] if len(sc) >= 2 else None,
                       "stock_at": kt(sc[-2]) if len(sc) >= 2 else None, "last_seen": kt(o.get("lastSeen")),
                       "first_seen": kt(o.get("offerCSV", [None])[0] if oc else None), "live": i in live})
    live_new = [o for o in offers if o["live"] and o["cond"] == 1]
    bb = g(18)
    return {
        "buybox": cents(bb), "buybox_seller": st.get("buyBoxSellerId"), "buybox_fba": st.get("buyBoxIsFBA"),
        "fba_low": cents(g(10)), "new_low": cents(g(1)), "amazon": cents(g(0)),
        "offers_new": g(11), "offers_fba": sum(1 for o in live_new if o["fba"]) if offers else None,
        "monthly_sold": p.get("monthlySold"), "sales_rank": g(3),
        "rating": (g(16) / 10) if g(16) is not None else None, "reviews": g(17),
        "offers_gz": gzip.compress(json.dumps(offers, separators=(",", ":")).encode()) if offers else None,
    }


ASIN_SQL = """
WITH cat AS (
  SELECT asin,
         bool_or(active AND sellable = 'open' AND match_ok IS NOT FALSE) AS watched,   -- any confidence (Nick, Nov 19, 2026)
         bool_or(active AND on_sale) AS on_sale
    FROM sephora_catalog WHERE active AND match_ok IS NOT FALSE GROUP BY asin
  -- listings Nick can't sell (every pair restricted) aren't pulled (Nick, Nov 20, 2026); the monthly restriction check
  -- (jobs/sephora/brand_restrictions.py, SP-API, no Keepa) re-checks them and an opened listing is pulled again
  HAVING bool_or(sellable IS DISTINCT FROM 'restricted')),   -- pairs marked wrong aren't pulled (Nick, Nov 19, 2026)
cand AS (SELECT DISTINCT asin FROM sephora_candidates WHERE status = 'new'),
lead AS (SELECT DISTINCT upper(btrim(asin)) AS asin FROM leads
          WHERE asin IS NOT NULL AND btrim(asin) <> '' AND COALESCE(lead_date, created_at::date) >= current_date - 30),
allx AS (
  SELECT asin, (watched OR on_sale) AS hot FROM cat
  UNION ALL SELECT asin, true FROM cand
  UNION ALL SELECT asin, true FROM lead)
SELECT a.asin, bool_or(a.hot) AS hot, k.pulled_at
  FROM allx a LEFT JOIN keepa_raw k ON k.asin = a.asin
 WHERE a.asin ~ '^[A-Z0-9]{10}$'
 GROUP BY a.asin, k.pulled_at
"""


def due_list(conn) -> tuple[list[str], dict]:
    """ASINs to pull, in order: never pulled, then overdue (hot first, most overdue first), then the pull-ahead queue
    (closest to due first). Also the day's need: pulls a day to keep every ASIN inside its interval."""
    now = dt.datetime.now(dt.timezone.utc)
    rows = conn.execute(ASIN_SQL).fetchall()
    due, n_hot, n_over, per_day = [], 0, 0, 0.0
    # one row per ASIN (a missing keepa_raw row gives pulled_at NULL)
    best = {}
    for r in rows:
        b = best.get(r["asin"])
        best[r["asin"]] = {"hot": bool(r["hot"]) or bool(b and b["hot"]), "pulled_at": r["pulled_at"] or (b and b["pulled_at"])}
    for a, r in best.items():
        n_hot += r["hot"]
        limit = HOT_H if r["hot"] else COLD_H
        per_day += 24 / limit
        age = (now - r["pulled_at"]).total_seconds() / 3600 if r["pulled_at"] else None
        ratio = None if age is None else age / limit
        if ratio is None:
            due.append((0, 0, 0, a))
        elif ratio >= 1:
            n_over += 1
            due.append((1, 0 if r["hot"] else 1, -ratio, a))
        elif ratio >= PULL_AHEAD:
            due.append((2, 0, -ratio, a))
    due.sort()
    return [d[3] for d in due], {"asins": len(best), "hot": n_hot, "due": n_over + sum(1 for d in due if d[0] == 0),
                                 "never_pulled": sum(1 for d in due if d[0] == 0), "ahead_ok": sum(1 for d in due if d[0] == 2),
                                 "need_per_day": round(per_day)}


def ensure_partitions(conn, now: dt.datetime):
    for k in (0, 1):
        y, m = now.year + (now.month - 1 + k) // 12, (now.month - 1 + k) % 12 + 1
        y2, m2 = y + m // 12, m % 12 + 1
        conn.execute(f"CREATE TABLE IF NOT EXISTS keepa_snapshots_{y}_{m:02d} PARTITION OF keepa_snapshots "
                     f"FOR VALUES FROM ('{y}-{m:02d}-01') TO ('{y2}-{m2:02d}-01')")


def beat(conn, state: str, detail: dict, started: dt.datetime):
    conn.execute("INSERT INTO keepa_collector_status (id, beat_at, started_at, state, detail) VALUES (1, now(), %s, %s, %s) "
                 "ON CONFLICT (id) DO UPDATE SET beat_at = now(), started_at = EXCLUDED.started_at, state = EXCLUDED.state, "
                 "detail = EXCLUDED.detail", (started, state, json.dumps(detail, default=str)))


def save(conn, products: list[dict], asked: list[str], tokens: int, pulled: dt.datetime) -> int:
    got = set()
    per = round(tokens / max(len(asked), 1))
    with conn.transaction():
        for p in products:
            a = p.get("asin")
            if not a:
                continue
            got.add(a)
            body = json.dumps(p, separators=(",", ":")).encode()
            gz = gzip.compress(body, 6)
            conn.execute("INSERT INTO keepa_raw (asin, pulled_at, tokens, bytes, raw_gz) VALUES (%s,%s,%s,%s,%s) "
                         "ON CONFLICT (asin) DO UPDATE SET pulled_at=EXCLUDED.pulled_at, tokens=EXCLUDED.tokens, "
                         "bytes=EXCLUDED.bytes, raw_gz=EXCLUDED.raw_gz", (a, pulled, per, len(body), gz))
            s = snapshot(p)
            conn.execute(
                "INSERT INTO keepa_snapshots (asin, pulled_at, buybox, buybox_seller, buybox_fba, fba_low, new_low, amazon, offers_new, "
                "offers_fba, monthly_sold, sales_rank, rating, reviews, offers_gz) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (a, pulled, s["buybox"], s["buybox_seller"], s["buybox_fba"], s["fba_low"], s["new_low"], s["amazon"], s["offers_new"],
                 s["offers_fba"], s["monthly_sold"], s["sales_rank"], s["rating"], s["reviews"], s["offers_gz"]))
        for a in set(asked) - got:   # Keepa doesn't know it: remember that, so it isn't asked again every loop
            conn.execute("INSERT INTO keepa_raw (asin, pulled_at, tokens, bytes, raw_gz) VALUES (%s,%s,0,0,%s) "
                         "ON CONFLICT (asin) DO UPDATE SET pulled_at=EXCLUDED.pulled_at, raw_gz=EXCLUDED.raw_gz",
                         (a, pulled, gzip.compress(b'{"missing":true}')))
    return len(got)


def main(once: bool = False):
    if not KEY or not DB:
        sys.exit("KEEPA_KEY and DATABASE_URL are required")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    started = dt.datetime.now(dt.timezone.utc)
    spent = []            # (time, tokens) over the last hour, for the average cap
    stats = {"pulled_today": 0, "tokens_today": 0, "day": started.date().isoformat(), "errors": 0, "last_error": None}
    due, info, listed_at = [], {}, 0.0
    tpa = 8.0             # recent tokens per ASIN (moving average)
    next_at = 0.0         # steady pace: the next batch may start at this time
    conn = None
    while not STOP:
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(DB, autocommit=True, row_factory=dict_row)
                ensure_partitions(conn, dt.datetime.now(dt.timezone.utc))
            now = dt.datetime.now(dt.timezone.utc)
            if now.date().isoformat() != stats["day"]:
                stats.update(pulled_today=0, tokens_today=0, day=now.date().isoformat())
                ensure_partitions(conn, now)
            if os.environ.get("PAUSED") == "1":
                beat(conn, "paused", {**info, **stats}, started); time.sleep(60); continue
            if not due or time.time() - listed_at > 600:
                due, info = due_list(conn)
                listed_at = time.time()
                log("ASINs", info)
            # steady pace: the day's need spread over 1,440 minutes; catch up faster only on a backlog > 1 hour
            target = min(MAX_PER_MIN, info.get("need_per_day", 0) * tpa / 1440 * HEADROOM) or MAX_PER_MIN
            overdue = info.get("due", 0)
            rate = MAX_PER_MIN if overdue * tpa > target * 60 else target
            stats.update(target_per_min=round(target, 1), rate_per_min=round(rate, 1), tokens_per_asin=round(tpa, 2))
            if time.time() < next_at:
                beat(conn, f"steady pace ({rate:.0f} tokens/min)", {**info, **stats}, started)
                time.sleep(min(next_at - time.time(), 60)); continue
            if not due:
                beat(conn, "idle (everything fresh)", {**info, **stats}, started)
                if once:
                    return
                time.sleep(120); listed_at = 0; continue
            # stay under the hourly average and above the shared reserve
            cutoff = time.time() - 3600
            spent[:] = [x for x in spent if x[0] > cutoff]
            used_hour = sum(t for _, t in spent)
            if used_hour >= MAX_PER_MIN * 60:
                wait = max(30, spent[0][0] + 3600 - time.time())
                beat(conn, f"pacing ({used_hour} tokens in the last hour)", {**info, **stats}, started)
                time.sleep(min(wait, 300)); continue
            tk = keepa("token")
            left, rate = tk.get("tokensLeft", 0), tk.get("refillRate", 125) or 125
            need = BATCH * 7
            if left - need < RESERVE:
                wait = (RESERVE + need - left) / rate * 60
                beat(conn, f"waiting for tokens ({left} left, reserve {RESERVE})", {**info, **stats}, started)
                time.sleep(min(max(wait, 20), 300)); continue
            batch, due = due[:BATCH], due[BATCH:]
            pulled = dt.datetime.now(dt.timezone.utc)
            r = keepa("product", domain=1, asin=",".join(batch), history=1, stats=90, buybox=1, offers=20, stock=1, rating=1,
                      only_live_offers=0)
            used = int(r.get("tokensConsumed") or 0)
            spent.append((time.time(), used))
            if batch and used:
                tpa = 0.9 * tpa + 0.1 * (used / len(batch))
            next_at = max(next_at, time.time() - 60) + used / max(rate, 1) * 60
            n = save(conn, r.get("products") or [], batch, used, pulled)
            stats["pulled_today"] += n; stats["tokens_today"] += used
            stats["last_batch"] = {"at": pulled.isoformat(), "asins": len(batch), "saved": n, "tokens": used, "left": r.get("tokensLeft")}
            beat(conn, "collecting", {**info, **stats, "due_left": len(due)}, started)
            log(f"saved {n}/{len(batch)}, {used} tokens, {r.get('tokensLeft')} left, {len(due)} due")
            if once:
                return
        except urllib.error.HTTPError as e:
            stats["errors"] += 1; stats["last_error"] = f"Keepa HTTP {e.code}"
            log("keepa error", e.code); time.sleep(60 if e.code == 429 else 120)
        except (psycopg.Error, OSError) as e:
            stats["errors"] += 1; stats["last_error"] = repr(e)[:300]
            log("error", repr(e)[:300])
            try:
                conn and conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
            if once:
                raise
            time.sleep(60)
    log("stopped")


if __name__ == "__main__":
    main(once="--once" in sys.argv)
