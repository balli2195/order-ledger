#!/usr/bin/env python3
"""Download Lead DB + Keepa data from Myrddin Hub for Lead Score training (Nov 11, 2026).

Works the same on Nick's computer and in a cloud session. Incremental: re-running only fetches what changed since
the last run (state in <out>/_export_state.json), so a local copy stays current cheaply.

  ENGINE_KEY=... python3 tools/export_training.py --out "~/Dropbox/Keepa data/lead_db"      # everything
  ENGINE_KEY=... python3 tools/export_training.py --out ./lead_db --only snapshots,history     # some kinds

Writes:
  catalog.parquet, candidates.parquet, feedback.parquet, runs.parquet      (replaced each run; small)
  history/part-<n>.parquet, snapshots/part-<n>.parquet                     (append-only; new parts each run)
  keepa_raw/keepa_raw_<date>_cNNN.json.gz + keepa_raw_index.json           (80 full Keepa products per chunk, the
                                                                            same format as the Lead Score Keepa pulls;
                                                                            the index maps ASIN -> newest chunk)
Read with DuckDB:  SELECT * FROM 'lead_db/snapshots/*.parquet'
Needs: pandas + pyarrow (pip install pandas pyarrow). HUB_URL overrides the Hub address.
"""
import argparse
import base64
import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HUB = (os.environ.get("HUB_URL") or "https://order-ledger-3mt6.onrender.com").rstrip("/")
SMALL = ("catalog", "candidates", "feedback", "runs")
APPEND = ("history", "snapshots")


def get(kind, **q):
    q = {k: v for k, v in q.items() if v not in (None, "")}
    url = f"{HUB}/api/engine/training/{kind}?" + urllib.parse.urlencode(q)
    for i in range(6):
        try:
            req = urllib.request.Request(url, headers={"X-Engine-Key": os.environ["ENGINE_KEY"]})
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001  (Render cold start, network)
            if i == 5:
                raise
            print(f"  retry {kind} after {e!r}", flush=True)
            time.sleep(10 * (i + 1))


def frame(rows):
    """DataFrame with dict / list values (extra, tags, counts...) stored as JSON text, which Parquet always accepts."""
    import pandas as pd
    return pd.DataFrame([{k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()} for r in rows])


def pages(kind, since=None, after=None, limit=None):
    while True:
        r = get(kind, after=after, since=since, limit=limit)
        yield r["rows"]
        after = r.get("next")
        if not after:
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help="comma list of kinds (catalog,candidates,feedback,runs,history,snapshots,raw)")
    ap.add_argument("--full", action="store_true", help="ignore the saved state and fetch everything again")
    a = ap.parse_args()
    if not os.environ.get("ENGINE_KEY"):
        sys.exit("Set ENGINE_KEY (Myrddin Hub engine key)")
    import pandas  # noqa: F401  (fails early if pandas / pyarrow are missing)
    import pyarrow  # noqa: F401
    out = os.path.expanduser(a.out)
    os.makedirs(out, exist_ok=True)
    sfile = os.path.join(out, "_export_state.json")
    state = {} if a.full or not os.path.exists(sfile) else json.load(open(sfile))
    kinds = [k for k in (a.only.split(",") if a.only else SMALL + APPEND + ("raw",)) if k]
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    print("Hub summary:", json.dumps(get("summary"), default=str)[:600], flush=True)

    for k in kinds:
        t0 = time.time()
        if k in SMALL:
            rows = [r for p in pages(k) for r in p]
            frame(rows).to_parquet(os.path.join(out, f"{k}.parquet"), index=False)
            print(f"{k}: {len(rows)} rows ({time.time() - t0:.0f}s)", flush=True)
        elif k in APPEND:
            d = os.path.join(out, k); os.makedirs(d, exist_ok=True)
            cur = state.get(f"{k}_after")
            n_part = len([f for f in os.listdir(d) if f.endswith(".parquet")])
            buf, total = [], 0
            r = None
            while True:
                r = get(k, after=cur, limit=5000)
                buf += r["rows"]; total += len(r["rows"])
                if r["rows"]:
                    cur = (str(r["rows"][-1]["id"]) if k == "history"
                           else f"{r['rows'][-1]['pulled_at']}|{r['rows'][-1]['asin']}")
                if len(buf) >= 100000 or not r.get("next"):
                    if buf:
                        frame(buf).to_parquet(os.path.join(d, f"part-{n_part:05d}.parquet"), index=False)
                        n_part += 1; buf = []
                if not r.get("next"):
                    break
            state[f"{k}_after"] = cur
            print(f"{k}: {total} new rows ({time.time() - t0:.0f}s)", flush=True)
        elif k == "raw":
            d = os.path.join(out, "keepa_raw"); os.makedirs(d, exist_ok=True)
            ifile = os.path.join(d, "keepa_raw_index.json")
            idx = json.load(open(ifile)) if os.path.exists(ifile) and not a.full else {}
            tag = "keepa_raw_" + dt.date.today().isoformat()
            chunk = len([f for f in os.listdir(d) if f.startswith(tag)])
            buf, total = [], 0

            def flush():
                nonlocal chunk, buf
                if not buf:
                    return
                name = f"{tag}_c{chunk:03d}.json.gz"
                with gzip.open(os.path.join(d, name), "wt") as fh:
                    json.dump([p for _, p in buf], fh)
                for asin, _ in buf:
                    idx[asin] = name
                chunk += 1; buf = []
                json.dump(idx, open(ifile, "w"))

            for page in pages("raw", since=state.get("raw_since"), limit=200):
                for r in page:
                    buf.append((r["asin"], json.loads(gzip.decompress(base64.b64decode(r["raw_gz_b64"])))))
                    total += 1
                    if len(buf) >= 80:
                        flush()
            flush()
            state["raw_since"] = started
            print(f"raw: {total} products updated, {len(idx)} ASINs in the index ({time.time() - t0:.0f}s)", flush=True)
        json.dump(state, open(sfile, "w"), indent=1)
    print("done ->", out)


if __name__ == "__main__":
    main()
