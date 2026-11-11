#!/usr/bin/env python3
"""Manual match rules and the wrong-match notes review (RUNBOOK step 4b). Hub doc: sephora_jobs/match_rules.

  python3 -I rules.py show                          # automatic + manual rules, and notes not yet reviewed
  python3 -I rules.py add --brand Chanel --kind block_term --side amazon --term tester --note "testers aren't the retail item (2 marks)"
  python3 -I rules.py remove --term tester [--brand Chanel]
  python3 -I rules.py reviewed                      # mark every current note as reviewed

kind: block_term (the term must NOT be in that side's title/name) or require_term (it MUST be).
brand: a brand name, or * for every brand. side: amazon (Amazon title) or sephora (Sephora name).
Add a manual rule only when the marks show a clear, repeatable pattern; quote how many marks it came from in --note.
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["show", "add", "remove", "reviewed"])
    ap.add_argument("--brand", default="*")
    ap.add_argument("--kind", choices=["block_term", "require_term"])
    ap.add_argument("--side", choices=["amazon", "sephora"], default="amazon")
    ap.add_argument("--term")
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    C.need_env("ENGINE_KEY")
    st = C.state_get("match_rules", {}) or {}
    st.setdefault("manual", [])
    if a.cmd == "show":
        wrong = C.hub("GET", "/api/engine/sephora/feedback").get("wrong_matches") or []
        rules = C.learned_rules(wrong)
        print(json.dumps({"auto": rules["auto"], "manual": rules["manual"],
                          "notes_reviewed_through": st.get("notes_reviewed_through")}, indent=1))
        thru = st.get("notes_reviewed_through") or ""
        for w in wrong:
            if C.after_ts(w.get("created_at") or "", thru):
                print(f"NEW  {w.get('created_at', '')[:16]} {w.get('brand') or ''} | {w.get('asin')} <-> {w.get('sephora_name') or w.get('sephora_url')} "
                      f"| amazon: {(w.get('amazon_title') or '')[:90]} | tags {w.get('tags')} | note: {w.get('note') or '-'} | correct {w.get('correct_asin') or '-'}")
        return
    if a.cmd == "add":
        if not (a.kind and a.term):
            sys.exit("add needs --kind and --term")
        st["manual"] = [m for m in st["manual"] if not (m.get("term", "").lower() == a.term.lower() and (m.get("brand") or "*").lower() == a.brand.lower())]
        st["manual"].append({"brand": a.brand, "kind": a.kind, "side": a.side, "term": a.term, "note": a.note,
                             "added": C.today().isoformat()})
    elif a.cmd == "remove":
        if not a.term:
            sys.exit("remove needs --term")
        st["manual"] = [m for m in st["manual"] if not (m.get("term", "").lower() == a.term.lower()
                                                         and (a.brand == "*" or (m.get("brand") or "*").lower() == a.brand.lower()))]
    elif a.cmd == "reviewed":
        st["notes_reviewed_through"] = dt.datetime.now(dt.timezone.utc).isoformat()
    C.state_put("match_rules", st)
    print(json.dumps(st, indent=1))


if __name__ == "__main__":
    main()
