#!/usr/bin/env python3
"""Full catalog build for brands that have no matcher rows in the Hub yet (lead-found, auto-added or hand-added brands).
Runs ONCE within a Keepa token budget, then exits; the weekly job calls run() each week until the backlog is empty.

  python3 -I build_backlog.py --max-tokens 3000 [--dry-run] [--brands "NARS,Givenchy"]

Per brand (highest 2026 lead spend first): Sephora listing, Keepa brand finder + /product for up to 420 ASINs
(about 3 tokens each, capped by what's left of the budget), catalog/match.py, SP-API restrictions + offers, build.py,
then the new ASINs are upserted to the Hub. The shared Keepa key never goes below KEEPA_RESERVE (1500): calls wait for
refill instead, which also paces the run. Built brands are recorded in job state 'brands_built' (so a brand with no
Amazon matches isn't rebuilt every week).
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

MIN_BRAND_TOKENS = 200   # don't start a brand with less than this left in the budget


def backlog(cfg, cat):
    built_state = C.state_get("brands_built", {}) or {}
    excluded = {b.lower() for b in cfg.get("exclude_brands", [])}
    built = {x["brand"] for x in cat if (x.get("extra") or {}).get("source") != "nick_lead"}
    todo = [b for b in cfg["brands"] if b["name"] not in built and b["name"] not in built_state and not b.get("catalog_built")
            and b["name"].lower() not in excluded]
    spend = collections.Counter()
    for x in cat:
        spend[x["brand"]] += C.fnum((x.get("extra") or {}).get("lead_spend_2026")) or 0
    todo.sort(key=lambda b: -spend[b["name"]])
    return todo, built_state


def run(budget: int, dry_run: bool = False, only: set | None = None, cfg=None, cat=None, log=print) -> dict:
    """Build as many backlog brands as fit in `budget` Keepa tokens. Returns {built, left, tokens, rows, problems}."""
    import keepa_pull as kp
    from weekly_new_products import build_brand   # same match/build pipeline as the weekly job
    cfg = cfg or C.load_brands()
    cat = cat if cat is not None else C.hub_catalog()
    hub_asins = {x["asin"] for x in cat}
    excluded = {b.lower() for b in cfg.get("exclude_brands", [])}
    todo, built_state = backlog(cfg, cat)
    if only:
        todo = [b for b in todo if b["name"] in only]
    out = {"built": [], "left": [], "tokens": 0, "rows": 0, "problems": []}
    for b in todo:
        left = budget - out["tokens"]
        if left < MIN_BRAND_TOKENS:
            out["left"].append(b["name"])
            continue
        try:
            if not C.sephora_brand(b)["ok"]:
                out["problems"].append(f"{b['name']}: Sephora listing unreadable, skipped this run")
                out["left"].append(b["name"])
                continue
            used0 = kp.state["used"]
            kp.pull_brand(b, max_asins=max(30, min(420, (left - 40) // 3)))
            spent = kp.state["used"] - used0
            out["tokens"] += spent
            rows = [x for x in build_brand(b["slug"], b, excluded) if x["asin"] not in hub_asins]
            if rows and not dry_run:
                C.hub_upsert_catalog(rows)
            hub_asins |= {x["asin"] for x in rows}
            out["rows"] += len(rows)
            out["built"].append(f"{b['name']} ({len(rows)} pairs, {spent} tokens)")
            built_state[b["name"]] = {"date": C.today().isoformat(), "pairs": len(rows), "tokens": spent}
            if not dry_run:
                C.state_put("brands_built", built_state)
            log(f"built {b['name']}: {len(rows)} new pairs, {spent} tokens (run total {out['tokens']}/{budget})")
        except Exception as e:  # noqa: BLE001
            out["problems"].append(f"build of {b['name']} failed: {e!r}"[:300])
            out["left"].append(b["name"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=3000, help="Keepa tokens for this run (it stops, the rest waits)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--brands", help="comma-separated brand names to build (default: whole backlog by lead spend)")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY", "KEEPA_KEY", "SPAPI_CLIENT_ID", "SPAPI_CLIENT_SECRET", "SPAPI_REFRESH_TOKEN")
    C.DRY_RUN = a.dry_run
    t0 = time.time()
    r = run(a.max_tokens, a.dry_run, set(x.strip() for x in a.brands.split(",")) if a.brands else None, log=C.log)
    txt = (f"# Brand backlog build, {C.today().isoformat()}{' (DRY RUN)' if a.dry_run else ''}\n\n"
           f"- Built: {', '.join(r['built']) or 'none'}\n- New catalog pairs: {r['rows']}\n"
           f"- Keepa tokens: {r['tokens']} of {a.max_tokens}; runtime {(time.time() - t0) / 60:.0f} min\n"
           f"- Still to build ({len(r['left'])}): {', '.join(r['left']) or 'none'}\n"
           + ("\n## Problems\n" + "\n".join(f"- {p}" for p in r["problems"]) + "\n" if r["problems"] else ""))
    open(os.path.join(C.DATA, "backlog_summary.md"), "w").write(txt)
    print(txt)
    if not a.dry_run:
        try:
            C.hub("POST", "/api/engine/sephora/run", {"kind": "backlog", "counts": {"built": len(r["built"]), "pairs": r["rows"],
                  "keepa_tokens": r["tokens"], "left": len(r["left"])}})
        except Exception as e:  # noqa: BLE001
            print("run record failed:", e)


if __name__ == "__main__":
    main()
