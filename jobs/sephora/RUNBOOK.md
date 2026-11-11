# Sephora → Amazon lead jobs: runbook

This runbook is for a fresh Claude session with no memory of earlier runs. The jobs find Sephora products worth buying for Nick's Amazon FBA account and post them to Myrddin Hub (https://order-ledger-3mt6.onrender.com, page /lead-db, "Lead DB"; it was "Sephora leads"). Nick approves or rejects them there.

The jobs are read-only against Sephora, Keepa and SP-API. They write only to the Hub's Sephora engine endpoints. Never touch Seller Central, Seller Snap, Airtable or the Hub's leads table.

## 1. Get the code

1. `add_repo` balli2195/order-ledger, clone it, then `cd order-ledger/jobs/sephora`.
2. Python 3.10+ and `curl` are all you need; there is nothing to pip install.
3. Nothing here is imported by the web app, and the Render build ignores this folder.
4. The catalog's source of truth is the Hub table (`GET /api/engine/sephora/catalog`).
5. Local data (pulls, caches, summaries) goes to `jobs/sephora/.data/`, which git ignores. Override it with `SEPH_DATA`.

## 2. Environment variables

The values are in the scheduled task's prompt or the project instructions. Never write them into files.

| Variable | What |
|---|---|
| `ENGINE_KEY` | Hub engine key (header `X-Engine-Key`) |
| `KEEPA_KEY` | Keepa API key. Shared with other jobs, so stay inside the budgets below |
| `SPAPI_CLIENT_ID`, `SPAPI_CLIENT_SECRET`, `SPAPI_REFRESH_TOKEN` | SP-API, read-only calls (seller A2UYRB3AYUMGAJ, marketplace ATVPDKIKX0DER) |
| `SEPH_STATE` | Optional. Default `hub`: job state (new_products, brands_added, brands_screened, brands_built, brand_snapshot, weekly_tried) lives in the Hub doc store `/api/engine/docs/sephora_jobs/*`, so a fresh session carries on. `local` keeps it in files only (tests) |
| `KEEPA_RESERVE` | Optional, default 1500. Every Keepa call waits for refill rather than take the shared key below this |

## 3. Daily: `python3 -I daily_watch.py`

What a run does:

1. Reads the Sephora Sale page (search "sale", about 2 requests) and the promo pages (Savings Event / Beauty Offers) for site-wide or brand-wide codes.
   - The sale is recorded as `{text, code, ends}`.
   - The discount comes from `brands.json` → `promo`: `savings_event_pct` (Nick: 25%), or a higher brand banner such as "30% off Sephora Collection".
2. Refreshes watched ASINs with Keepa (Buy Box, 90-day average): sale and promo rows first, then the best ROI.
3. Applies the ROI ≥ 15% prefilter, then an SP-API live offers check.
4. Scores the top ≤60 with Lead Score (through the Hub, ≤25 per call, one call at a time).
5. Posts as candidates the rows with predicted ROI ≥ 21%.
   - Multi-size products are never posted until Nick confirms the size (`needs_size` with no `size_confirmed`). They are listed under "Needs your size check" with the Amazon size.
   - A cost that comes from an event code rather than the item's own sale price gets the note prefix "Event code — confirm brand qualifies at checkout".
6. Updates the catalog's prices and ROI and records the run.

Details:

- **Budget and runtime:** ≤2,500 Keepa tokens (`--max-tokens`), about 3 tokens per ASIN; about 670 watch ASINs ≈ 2,000 tokens. Runtime is 4–6 minutes, longer if Keepa has to refill.
- **Re-running the same day is safe.** Candidates are keyed on (asin, found_on). Today's Keepa pulls and Lead Scores are cached and reused.
- **Flags:** `--dry-run` writes nothing to the Hub and skips Lead Score unless you add `--score`. `--no-keepa` skips the Keepa refresh.
- **Output:** `.data/daily_summary.md`, always written. Sale-page items that aren't in the catalog go to state `new_products`.
- **Exit codes:** `0` ran (problems listed in the summary); `1` crashed, missing env or Hub unreadable; `2` ran but couldn't post the candidates.

## 4. Weekly: `python3 -I weekly_new_products.py`

Run it once a week (for example Sunday).

1. Refreshes every brand's Sephora listing: prices, sale flags, and products that have gone.
2. Reads Sephora New arrivals and the full Sephora brand list.
3. Seeds Nick's new Sephora leads (`seed_from_leads.py --since` 14 days ago).
4. Builds brands with no matcher rows yet by calling `build_backlog.py` (highest lead spend first, within what's left of the budget).
5. Screens unseen brands (≤500 tokens): can_source, Amazon demand and restrictions decide between auto-added, for Nick, or screened out.
6. Matches new products (Keepa title search, `catalog/match.py`, restrictions, offers, `build.py`).
7. Writes a `brand_weights` table from Nick's approvals and rejections. It only reports; it never removes brands.

- **Budget and runtime:** ≤4,000 Keepa tokens; leftovers carry over to next week. Runtime 15–40 minutes.
- **Output:** `.data/weekly_summary.md`.
- **Adding a brand by hand:** copy an entry in `brands.json`. The next weekly run builds it.
- **Keep the brand lists in sync.** `exclude_brands` mirrors the Hub `brands.can_source=false` list; refresh it with the Myrddin Hub `get_lead_choices` tool if Nick changes it.

## 4b. Weekly: learn from Nick's wrong-match marks (after the weekly script)

Nick (or his team) marks wrong Amazon ↔ Sephora matches on the Hub — the Sephora leads tab (candidate card or catalog) and the Sephora relist page — and says why: tags `size`, `shade`, `concentration`, `set`, `bonus`, `multipack`, `version`, `product`, `other`, plus an optional note and the correct ASIN. They are stored in the Hub table `match_feedback` and come back as `wrong_matches` from `GET /api/engine/sephora/feedback`.

- **Automatic (no action needed):** every run turns tags into strict checks (`catalog/match_rules.py`): `size`, `shade`, `concentration`, `set`, `bonus`, `multipack` apply to that brand after 1 mark and to every brand once a tag shows up in 3+ brands. New matches that break one are capped at confidence low; the weekly job downgrades existing unconfirmed pairs that break one; the daily watch holds them back. A pair Nick marks OK is never held back.
- **Your part:** the weekly summary lists "Notes for Claude to review". Read them (`python3 -I rules.py show` prints them with the Amazon titles). When several marks show the same clear, repeatable pattern that the automatic checks miss (a word like "tester", "refill", "travel" on one side), add a manual rule:
  `python3 -I rules.py add --brand "<Brand or *>" --kind block_term|require_term --side amazon|sephora --term "<word>" --note "<why, from N marks>"`
  Don't add a rule from a single one-off mark. If a note gives the correct ASIN, mention it to Nick so he can add that lead. Then run `python3 -I rules.py reviewed`.
- In the message to Nick, add one line: wrong-match marks this week, automatic checks now on, any manual rule you added (and why), pairs downgraded.

## 5. Other scripts

- `build_backlog.py --max-tokens N` runs once within N Keepa tokens, then exits.
  - It builds brands with no matcher rows (lead-found, auto-added or hand-added), recording each in state `brands_built`.
  - The weekly job calls it each week until the backlog is empty.
  - Run it by hand with e.g. `--max-tokens 3000` to speed up the backlog.
- `rules.py show|add|remove|reviewed` shows the learned match rules and Nick's new wrong-match notes, and edits the manual rules (Hub doc `sephora_jobs/match_rules`). See step 4b.
- `recompute_roi.py` recomputes every pair's ROI and watch price from stored values (no API calls). Run it after changing the economics rules in `common.py`.
- `seed_from_leads.py [--since DATE] [--dry-run] [--write-brands-json]` adds Nick's own past Sephora lead pairs.
  - Live products keep confidence high.
  - A product Sephora no longer lists is relinked by name within the same brand (medium confidence) or skipped as discontinued.
- `load_catalog.py --catalog-dir DIR` reloads a full catalog build (`all_matches.csv` plus per-brand JSON). Use it for disaster recovery only.
- `catalog/*.py` are the original catalog pipeline scripts. They read `SEPH_DATA` and `../brands.json`.

## 6. When something fails

Every stage logs into the summary's **Problems** list and the run carries on.

- **Sephora** (403, timeouts, empty pages): the sale is recorded as not checked (no `sale` key) and the Keepa watch still runs. Don't fetch product pages or old product URLs; they are blocked and not needed. If the Sale page shape changes, open `/api/v2/catalog/search?type=keyword&q=sale` by hand.
- **Keepa:**
  - Out of tokens: the run waits for refill up to `--keepa-wait` (30 minutes), then uses the stored prices.
  - A 429 or a "would wait" note in Problems means another job drained the key. Re-run later; the cached pulls are reused.
  - Never raise `--max-tokens` above the budget.
- **Hub** (5xx while Render cold-starts): requests retry 4 times with backoff. If the Hub is down, the run exits 1 and the summary says FAILED. Re-run once it's up.
- **Lead Score:**
  - If a batch fails, rows with ROI ≥ 21% are posted with "not scored" in the note.
  - The Hub proxy takes about 2 s per row.
  - Never send more than 60 rows a day or run batches in parallel; Nick doesn't want his Lead Score server loaded.
- **SP-API:** if the offers check fails, Keepa prices are used. If the restrictions check fails, the row stays `sellable=null` and is not watched.

## 7. What to send Nick after the daily run

A short message built from `daily_summary.md`, for example:

> Sephora leads today: **N new** on the Lead DB page (Hub tab "Lead DB").
> Top 3: <Sephora name (shade)>, $cost → Buy Box $X, ROI Y%, Lead Score band B (pred P%) · …
> Sale: <text, code, ends> (or "no site-wide sale").
> Problems: <one line each, or "none">.

After the weekly run, add: new products matched (flag ROI ≥ 21%), brands auto-added, brands for Nick to decide (with the reason), and brands Nick keeps rejecting (`brand_weights`). Keep it under 10 lines.

## 8. Rules the jobs keep

- Nick's answers in the catalog (`match_ok`, `size_confirmed`, `wrong_tags`, `wrong_note`, `correct_asin`) are never sent.
  - `match_ok=false` pairs are never proposed.
  - `size_confirmed` decides which Sephora price is used.
- ASINs Nick approved or rejected in the last 21 days are not proposed again unless the cost is now lower.
- Costs:
  - `cost` = entered Sephora price × pack quantity, before the ×1.05 store uplift (Lead Score and the ROI maths add the 5%).
  - Prep is $1.40 per unit everywhere (Nick's Seller Snap figure).
  - The Hub stores ROI and `referral_pct` as fractions.
- When updating existing catalog pairs, always send their current `active`: the Hub re-activates any pair sent without it.
- Sizes: the search API gives only Sephora's price range, not sizes. A multi-size product is therefore only proposed after Nick confirms the size on the Sephora leads tab.
