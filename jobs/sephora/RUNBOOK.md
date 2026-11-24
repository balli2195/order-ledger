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
2. Prices watched ASINs from the Keepa collector's latest pull (`POST /api/engine/keepa/latest`, ≤36 h old, no tokens; Nick, Nov 23, 2026). The collector pulls all ~5,300 watched ASINs within 24 h. Only ASINs it hasn't pulled get a Keepa refresh (Buy Box, 90-day average): sale and promo rows first, then the best ROI.
   - Every watchable pair is priced, not one per ASIN (Nov 19, 2026). An ASIN with several pairs (Nick's lead and the matcher's pick, a mini and a full size) is posted once, from: the pair of its open candidate, else the most trusted profitable pair (marked OK → Nick's lead → confidence → match score), else the best ROI. Keepa and SP-API are still called once per ASIN.
1c. Deal banners (Nick, Nov 24, 2026: "make sure we don't miss anything"). Codes are only part of Sephora's deals; Cyber Week also runs brand deals ("30% off Kiehl's—Today Only"), category deals ("Up to 30% off Select Fragrances") and multi-buys ("Buy 2 Minis, Get Both 50% Off"). Each run reads the deal content pages (home, Beauty Offers, Savings Event, Cyber Week, plus up to 6 deal pages they link to; the hidden `seo` link list is skipped because it keeps ended offers) and:
   - saves every promo-looking text to `.data/promo_banners.md` and Hub doc `sephora_jobs/promo_banners/<date>`;
   - prices minis at a live "Buy N minis, get X% off" (Nick always buys 2+), with the note "Promo price — confirm it applies at checkout" and "confirm this mini qualifies";
   - reports brand and category deals. They show on sephora.com as markdowns (checked Nov 24: Laura Mercier's today-only 30% was exactly 70% of list on all 288 pairs), so the Sale page prices them; the summary flags a brand deal with no markdowns seen, or a brand that's not in the Lead DB or restricted.
   - The summary has "Promo codes", "Markdowns today by brand" and "Deals on Sephora's banners" (it used to say only "Sale: none", which read as no sale during Cyber Week).
   - **Claude review (daily task):** read `promo_banners.md` and report any deal the summary doesn't account for (a code, gift with purchase, multi-buy, app-only or checkout-only discount, a brand deal with no markdowns), with what it would change.
3. Applies the ROI ≥ 15% prefilter, then an SP-API live offers check.
   - No demand or thin-price gate (Nick, Nov 23, 2026). Low-demand listings and listings with no Buy Box or one FBA seller are posted too, with the note prefix "No competition / restock: …" (no Buy Box can mean no competition, and sales can come back when restocked). With no offer at all, the 90-day average Buy Box prices the pair.
3b. Rank rule (Nick, Nov 24, 2026): a listing under 50% ROI needs a 30-day average sales rank under 300,000 (Keepa `stats.avg30`, rank index 3; no rank in 30 days fails). At 50% ROI and up, ROI alone decides. Applied before Lead Score, so held rows aren't scored, and to open suggestions every day (they expire with "Rank rule on <date>: …"). Settings: `RANK_RULE_ROI`, `RANK30_MAX` in `daily_watch.py`.
3c. Match-check gate (Nick, Nov 24, 2026): nothing reaches To review until its pair has been checked. Checked = Nick marked it OK, the weekly check passed it (`extra.screened` / `extra.verified`), or its research verdict on the for-Nick list (`sephora_jobs/verify_for_nick`) says the same product (medium confidence adds "Match check: same product (medium confidence)" to the note; cant_tell adds "Match check couldn't tell"). A research verdict of another product / not sold holds the pair (open suggestions expire "Match check says …"); Nick decides those on the for-Nick list. An unchecked profitable pair is held and listed in Hub doc `sephora_jobs/check_queue`; the daily task checks the queue (`check_pairs/pairs.py prep <work> --queue`, then the usual screen / research / finish steps) and re-runs `daily_watch.py --no-keepa` to post the ones that pass. First run (Nov 24): 0 unchecked, 16 held as wrong, 15 open suggestions expired.
4. Scores every row that passed with Lead Score (through the Hub, ≤25 per call, one call at a time). No daily cap (Nick, Nov 23, 2026).
1b. Sale-page prices belong to one item (Nov 23, 2026). The Sale page lists each product once, with the item that's on sale (its skuId) and that item's price. For a product sold in several sizes (from the Sephora item lists, `/api/engine/store-skus`):
   - the product's price range comes from the item list, not the Sale page;
   - a pair with a known store size gets the markdown only when its own item is the one on sale;
   - a pair with an unknown size is priced at the full size, and the markdown is used only if the item on sale is the full size.
   Before this, the Ultra Violette 50 mL and Kosas 100 mL listings were queued at the 20 mL and 30 mL prices.
4a. Out of stock at Sephora (Nick, Nov 23, 2026): a pair whose own item (its store size, else the product's only item) is out of stock on its Sephora item list read in the last 10 days isn't posted, and an open suggestion in that state expires ("Out of stock at Sephora …"). Those products go in Hub doc `reader/sephora_todo` for the daily stock check (section 4c step 6) to re-read.
4b. Multi-size rows without a known size are no longer held (Nick, Nov 19, 2026): they're priced at the size guess, or the full size when unknown, and posted with "Size not checked" at the front of the note.
5. Posts as candidates the rows with predicted ROI ≥ 10% (Nick, Nov 19, 2026; was 21%). Every pair not marked wrong is watched: learned match rules, confidence and the Sephora-listing check no longer hold a pair back (Nick judges the match on To review).
   - A cost that comes from an event code rather than the item's own sale price gets the note prefix "Event code — confirm brand qualifies at checkout".
6. Updates the catalog's prices and ROI and records the run.

Details:

- **Budget and runtime:** ≤2,500 Keepa tokens (`--max-tokens`) for ASINs the collector hasn't pulled in 36 h, about 3 tokens per ASIN; usually few or none. Lead Score takes about 2 s per row with no daily cap, so a run with ~500 rows to score takes about 20 minutes.
- **Re-running the same day is safe.** Candidates are keyed on (asin, found_on). Today's Keepa pulls and Lead Scores are cached and reused.
- **Flags:** `--dry-run` writes nothing to the Hub and skips Lead Score unless you add `--score`. `--no-keepa` skips the job's own Keepa pulls (the collector's pulls are still used).
- **Output:** `.data/daily_summary.md`, always written. Sale-page items that aren't in the catalog go to state `new_products`.
- **Exit codes:** `0` ran (problems listed in the summary); `1` crashed, missing env or Hub unreadable; `2` ran but couldn't post the candidates.

## 4. Weekly: `python3 -I weekly_new_products.py`

Run it once a week (for example Sunday).

1. Refreshes every brand's Sephora listing: prices, sale flags, and products that have gone.
2. Reads Sephora New arrivals and the full Sephora brand list.
3. Seeds Nick's new Sephora leads (`seed_from_leads.py --since` 14 days ago).
4. Builds brands with no matcher rows yet by calling `build_backlog.py` (highest lead spend first, within what's left of the budget).
5. Screens unseen brands (≤500 tokens). Nick's rule (Nov 22, 2026): match every brand he can sell; only sellability decides, never demand or seller counts. Restricted (monthly check, or most of the top 5 listings) → screened out; mostly open → auto-added and built; approval required → for Nick (one-click approval: keep; manufacturer invoices or Transparency: skip); no listings found under the brand's Amazon spellings → for Nick. Only Beauty & Personal Care listings decide (Donna Karan passed on open handbags while its fragrances are restricted). Brands Nick says he can't sell go in job state `brands_nick_cant_sell` and are always excluded (Bobbi Brown, Donna Karan). A brand counts as built only with a `brands_built` record, not because it has a few pairs. `add_brands.py --eligible --rescreen` adds every Nov 2 sellable brand not yet built and re-screens brands left out for other reasons. The Keepa finder takes listings with a sales rank, including ones with no seller right now.
6. Matches new products (Keepa title search, `catalog/match.py`, restrictions, offers, `build.py`).
6b. Re-matches ASINs whose every pair is marked wrong (`rematch.py`, no Keepa). The matcher picks one product per ASIN and never tried the runner-up, so one wrong mark used to end the ASIN (CHANEL No.5 L'Eau → N°5 EDT, Chance Eau Fraîche → the EDP). The ASIN is scored again against the brand's listing without the wrong products. A high / medium match is added only when the names agree both ways: every word of the Sephora name is in the Amazon title, and every Amazon word Sephora uses for this brand is in the Sephora name. Body cream / deodorant / tester / sample words and the men's line (Allure vs Allure Homme) count; EDT / EDP words only when the Amazon title names a concentration. Everything else is listed as a suggestion in the weekly summary. A test on Chanel, Dior, Make Up For Ever and Benefit (188 all-wrong ASINs) added 4, 3 of them right.
6c. Brand best-seller sweep (`rematch.py`, ≤800 tokens, `--sweep-tokens`). The step 6 search only finds Amazon titles that contain a Sephora product's first name words, so "CHANEL Bleu De Paris Cologne" was never found. A few brands per run (oldest swept first): Keepa finder sorted by sales rank, one page of 50, a deeper page each rotation (4 pages, then back to page 0). ASINs not in the catalog go through the usual match/build. State: `brand_sweep`.
6c2. Dead Amazon listings, or "dog pages" (`listing_check.py`, Nov 19, 2026). Keepa keeps the last title, offers and rank for a while after Amazon takes a listing down, so a dead ASIN can still look profitable. SP-API's catalog search (20 ASINs a call) finds them, and they get `extra.listing_dead`; the daily job doesn't watch them, and the flag is cleared when the ASIN comes back. The daily job also flags an ASIN when the SP-API offers call says "invalid ASIN". The first sweep (Nov 19) checked 6,335 ASINs and found 64 dead, e.g. B0D6YKCPW4 Merit Clean Lash.
6d. Shades and sizes: `python3 -I resolve_skus.py` (run it after the weekly script, and after new SKU lists are read in Chrome). For pairs whose Sephora product has 2+ SKUs and no saved store size, it picks the SKU from the Amazon listing (Nov 19, 2026):
   - **Text first** (`shade.py`): the Sephora item number in Amazon's model / partNumber, then the variation selector, the color field, the title and the bullets. The shade code ("81", "3F", "NW20") and the name ("Daydreaming") are matched separately, so a listing with only one still matches. The same shade in several sizes is one shade, and the size comes from the title. Size SKUs are matched on ounces, with the title beating Keepa's size field and a reused item number.
   - **Then photos** (`shade_image.py`), for shade pairs near profit: the Amazon photos are compared with Sephora's photo of each SKU. A pick needs a near-identical render. Swatch colours are only a hint.
   - **Results:** a decided pair gets `store_size`. A listing that names a shade Sephora's list doesn't have gets `extra.shade_hint.not_sold`, and the daily job doesn't watch it. Anything else gets `extra.shade_hint.guess`, and the candidate note says "Shade not confirmed: looks like …".
   - **Amazon data source:** the Keepa collector's latest pulls (`GET /api/engine/keepa/meta?asins=`, no tokens); Keepa only for ASINs the collector lacks (`--max-tokens`).
   - **Test on Nick's leads** (Nov 19). 316 lead ASINs had one clear lead shade; leads where one product link was reused for many shades were left out. Text decided 84%, and 96% of those agreed with the lead link; photos added 1%. Only 4 of the 316 were flagged "not sold", and those look like lead-link mistakes.
   - **First catalog run (Nov 19):** 1,733 pairs had no SKU. 739 got a store size (734 from text, 5 from photos). 460 were flagged as a shade Sephora doesn't sell. 502 got guesses only, and 32 had no Amazon data.
   - **Nick's rules (Nov 19):**
     - A lead's Sephora link is right about the product page but not always about the shade (the skuId), so the job never takes a lead link's skuId as the shade.
     - When the title and the shade picker disagree, the title wins if the picker names a shade Sephora doesn't sell.
     - Two SKUs that read the same (same size, different bottle, e.g. Kayali's $29 and $32 10 ml) are interchangeable. The cheaper one is the store size, and the other is saved in `extra.store_sku_alts`, so the stock check counts either one in stock. A refill, another scent or formulation, or a price more than 25% higher is never an alternate.
     - A shade Sephora's list doesn't have is skipped. Stores sometimes hide out-of-stock shades, and Nick still skips them; the weekly run re-checks them.
     - A *close* shade is kept as a lead with "Close shade, check it: …" in the note, and Nick reviews it. Close means the same colour after synonyms (Noir = Black, Rouge = Red, Rose = Pink), or the Amazon shade words are the SKU's description. Clearly different shades, like another code ("051 Nude Bloom" vs "013 Beige"), are skipped.
     - When the title and the colour field name two Sephora shades, both are kept: the second is an alternate, and the note says "Two shades named … (check the photo)". The photos decide (Nick sold B0D1NRK56Z as Say Less from the picture).
     - Sephora renumbers lines, so a name match beats a code-only match. A code match alone counts as the same shade renamed.
   - **Review by eye:** for a profitable lead the text and photos can't settle, make a contact sheet (Amazon photos + every Sephora SKU photo, labelled) and judge it. Box photos sometimes print the shade.
   - **Product matching:** `catalog/match.py` now takes `shades` (store_skus values). A product's own shade words in the Amazon title no longer count against it, and "Cream Puff" no longer reads as a brush. Clear winners whose Sephora name starts with a marketing word ("Pro Filt'r", "Ultra Suede") are kept as low instead of dropped. Nick's leads whose product Sephora still sells: right product 712 → 737 of 981. Against pairs already marked OK or wrong, the change added no wrong matches.
7. Writes a `brand_weights` table from Nick's approvals and rejections. It only reports; it never removes brands.

- **Budget and runtime:** ≤4,000 Keepa tokens; leftovers carry over to next week. Runtime 15–40 minutes.
- **Output:** `.data/weekly_summary.md`.
- **Adding a brand by hand:** copy an entry in `brands.json`. The next weekly run builds it.
- **Brand restrictions come only from Amazon.** The brand skip list (`exclude_brands`, built by `common.load_brands()`) is the brands the monthly restriction check (section 11) found restricted. The Hub's old brand "can source" switches were removed on Nov 20, 2026 (Nick: they conflicted with his restriction check). Never keep a hand list of brands.

**A wrong mark switches on the ASIN's fallback pairs (Nov 19, 2026).** When a pair is marked wrong and the ASIN has no other active, not-wrong pair, the Hub reactivates its inactive not-wrong pairs (`mark_catalog`, returned as `reactivated`). `seed_from_leads.py` keeps every lead pair active (it used to store extra lead pairs inactive). The one-time sweep on Nov 19 reactivated 23 such pairs (e.g. B0DZV1RBGX's glitter spray, hidden after its regular spray was marked wrong).

**Pairs marked wrong are never updated (Nick, Nov 19, 2026).** Only matches and possible matches matter for leads; a
wrong pair is kept as it was, for review in the Lead DB catalog (Match filter "Wrong match only"; the Mismatches page was retired Nov 20, 2026). The daily job, the weekly listing refresh,
recompute_roi, the Keepa collector and the Sephora page reader all skip `match_ok = false`. Clearing the mark or
marking it a match puts the pair back in every update.

## 4b. Weekly: learn from Nick's wrong-match marks (after the weekly script)

Nick (or his team) marks wrong Amazon ↔ Sephora matches on the Hub — the Sephora leads tab (candidate card or catalog) and the Sephora relist page — and says why: tags `size`, `shade`, `concentration`, `set`, `bonus`, `multipack`, `version`, `product`, `other`, plus an optional note and the correct ASIN. They are stored in the Hub table `match_feedback` and come back as `wrong_matches` from `GET /api/engine/sephora/feedback`.

- **Automatic (no action needed):** every run turns tags into strict checks (`catalog/match_rules.py`): `size`, `shade`, `concentration`, `set`, `bonus`, `multipack` apply to that brand after 1 mark and to every brand once a tag shows up in 3+ brands. New matches that break one are capped at confidence low; the weekly job downgrades existing unconfirmed pairs that break one; the daily watch holds them back. A pair Nick marks OK is never held back.
- **Your part:** the weekly summary lists "Notes for Claude to review". Read them (`python3 -I rules.py show` prints them with the Amazon titles). When several marks show the same clear, repeatable pattern that the automatic checks miss (a word like "tester", "refill", "travel" on one side), add a manual rule:
  `python3 -I rules.py add --brand "<Brand or *>" --kind block_term|require_term --side amazon|sephora --term "<word>" --note "<why, from N marks>"`
  Don't add a rule from a single one-off mark. Then run `python3 -I rules.py reviewed`. A `same_term` rule (`--kind same_term --term cakeless`) says a word marks a separate line or form and must be on both sides or neither.
- **Find the correct ASIN for every wrong match (Nick, Nov 17, 2026), and put it in the Lead DB.** For each mark without a correct ASIN:
  1. Work out what Sephora actually sells (the note says, or the size list on the Sephora page; Sephora blocks product pages from the cloud, so only read them in Nick's Chrome when he allows it, a handful at a time, ~15 s apart, reading `#linkStore` → `page.product.currentSku` plus every `*ChildSkus` list: `regularChildSkus` AND `onSaleChildSkus` (on-sale shades are only in the second list)).
  2. Look for the right ASIN among the product's other catalog pairs first (`GET /api/engine/sephora/catalog?asin=` or SQL on `sephora_catalog` by `product_id`), then Keepa search. Right means same product, same line and scent/concentration, and an Amazon size equal to a size Sephora sells (Nick: a slightly different volume is acceptable; a different bottle size, bigger or smaller, is not — Nov 20, 2026, Coco Noir 1.2 oz vs Sephora 1.7 / 3.4 oz).
  3. Write it with `POST /api/engine/sephora/catalog/mark`: `{asin, product_id, correct_asin_only: true, correct_asin}` on the wrong pair (keeps Nick's mark), `{asin, product_id, match_ok: true}` on the right pair, and add the right pair with `POST /api/engine/sephora/catalog` if it isn't in the catalog yet. Mark other pairs of the same product that you can show are wrong (`match_ok: false` with tags, note and correct_asin). Marks you write show as "Claude".
  4. If no right ASIN exists on Amazon, leave correct_asin empty and say so in the message.
- **Judging a match (Nick, Nov 19, 2026).**
  - A newer or older version of the same product is a match (Brow Harmony Pencil & Gel ↔ Precision Pencil, MAC Matte Lipstick ↔ M·A·Cximal Silky Matte, MUFE HD Microfinish ↔ HD Skin powder). For shaded products the Amazon shade must still be sold under the new line (Velvet Teddy yes; MUFE Artist Rouge C403 no, the new line renumbered its shades).
  - When the title and Keepa fields can't settle it (EDT or EDP, which scents are in a duo, the size), read the Amazon listing photos. Keepa `/product` gives `imagesCSV`; download `https://m.media-amazon.com/images/I/<id>` with curl (it works from the cloud) and view it with Read. Read the bottle or box: concentration, contents, ml. Nov 19 examples: B0050CZ1KW's photo shows the EDP, B07MW3ZJBW's shows the EDT, and B0DK22H247's box is Vanilla 28 + Vanilla Candy Rock Sugar 42, not Sephora's Musk Santal + Vanilla 28 duo.
- **Store size on a pair (Nick, Nov 17, 2026).** A multi-size store product (1.2 / 1.7 / 3.4 oz) pairs with one Amazon size. When the Sephora page sizes are known, write the matching SKU with `POST /api/engine/sephora/catalog/mark` `{asin, product_id, store_size: {sku_id, size, price}}` (doesn't change who checked the match). The page then links to that size and shows its price, and the daily watch prices the pair at that size (no size check needed). Sizes within 12% count as the same; a refill or 3 x 20 ml set maps to the store's travel set SKU only.
- In the message to Nick, add one line: wrong-match marks this week, automatic checks now on, any manual rule you added (and why), pairs downgraded.

## 4c. Daily: Sephora stock check (Claude in Chrome on Nick's MacBook, before the daily run)
The daily run never posts an item that Sephora's item list shows out of stock (step 6 re-reads those daily). And when Nick rejects a lead as **Not in stock at Sephora**, the Hub starts a stock watch instead of the 21-day block (Nick, Nov 17, 2026:
"I don't need it snoozed"). The daily watch holds that ASIN back, at any price and for any time, until a check finds it back in stock;
then the next daily run may propose it again with fresh prices.
1. `GET /api/engine/sephora/stock-watch` (X-Engine-Key). Also `GET /api/engine/docs/reader/sephora_todo`. If both are empty (no watch rows, no items), stop: nothing to check, no message. If only the watch is empty, skip steps 3–4.
2. Load the Chrome tools in one ToolSearch, call tabs_context_mcp, open a new tab at https://www.sephora.com/ and wait for it to load.
   Read `jobs/sephora/stock_check.js` (project doc `claude/sephora-catalog/stock_check.js`) and run it with javascript_tool.
3. For the watched rows, 3 at a time: `await sephStock([{id, url: sephora_url}, ...])`. Keep the 8-second gap; never more than
   60 pages a day. If a result says blocked / no product data twice in a row, stop and report it.
4. POST all results (from the session, not the page) to `/api/engine/sephora/stock-watch/results` as `{results: [...]}`. The Hub picks
   the watched SKU (the pair's store size, else the only SKU, else the shade that matches the Amazon title) and marks the watch
   restocked when that SKU is in stock. Close your tab.
5. Only message Nick when something came back in stock (name, Amazon ASIN link, Sephora link) or a step failed. Reads only: never
   add to bag, sign in, or change anything on Sephora.
6. Out-of-stock re-read (Nick, Nov 23, 2026). The daily run holds back any pair whose own Sephora item was out of stock on its
   item list (read in the last 10 days) and puts those products in Hub doc `reader/sephora_todo` (`GET /api/engine/docs/reader/sephora_todo`).
   If it has items: `POST /api/engine/upload-token {"purpose":"store_skus","hours":3}` (engine key, in the session), then in the same
   Sephora tab run `check_pairs/sephora_reader.js` (project doc `claude/sephora-catalog/sephora_reader.js`) with `__TOKEN__` = that
   token, `__SOURCE__` = `recheck` and `__LIMIT__` = 60 minus the pages read in step 3. It reads one page every 7 s, uploads the fresh
   item lists itself and sets `window.sk.finished`; poll `window.sk` every couple of minutes. A restocked item is posted again by
   the 4:47 run. The 60-page daily cap covers steps 3 and 6 together.

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
  - If a batch fails, rows with ROI ≥ 10% are posted with "not scored" in the note.
  - The Hub proxy takes about 2 s per row.
  - No daily row cap (Nick, Nov 23, 2026), but never run batches in parallel: one call of ≤25 rows at a time.
- **SP-API:** if the offers check fails, Keepa prices are used. If the restrictions check fails, the row stays `sellable=null` and is not watched.

## 7. What to send Nick after the daily run

A short message built from `daily_summary.md`, for example:

> Sephora leads today: **N new** on the Lead DB page (Hub tab "Lead DB").
> Top 3: <Sephora name (shade)>, $cost → Buy Box $X, ROI Y%, Lead Score band B (pred P%) · …
> Sale: <text, code, ends> (or "no site-wide sale").
> Problems: <one line each, or "none">.

After the weekly run, add: new products matched (flag ROI ≥ 21%), brands auto-added, brands for Nick to decide (with the reason), and brands Nick keeps rejecting (`brand_weights`). Keep it under 10 lines.

## 8. Rules the jobs keep

- **Generic-brand listings never go in the Lead DB (Nick, Nov 24, 2026):** he can't sell an Amazon listing whose brand is "Generic". `catalog/build.py` and `seed_from_leads.py` skip Keepa brand Generic and titles starting "Generic " (B0CQQPVF8L only said so in the title). On Nov 24 the 248 existing generic ASINs were deleted with `POST /api/engine/sephora/catalog/delete-asins {asins, reason}` (deletes their pairs, expires open suggestions, drops the collector's Keepa data, and adds them to `sephora_blocked_asins`; the catalog upsert refuses blocked ASINs). Backup: Hub doc `sephora_jobs/deleted/2026-11-24_generic`. Nick then had the two he had marked OK (B00F642WEW, B0GQH13Y48) and the title-only generic B0CQQPVF8L removed too: 251 in all. List blocked ASINs: `GET /api/engine/sephora/blocked-asins`.

- Nick's answers in the catalog (`match_ok`, `size_confirmed`, `wrong_tags`, `wrong_note`, `correct_asin`) are never sent.
  - `match_ok=false` pairs are never proposed.
  - `size_confirmed` decides which Sephora price is used.
- ASINs Nick approved or rejected in the last 21 days are not proposed again unless the cost is now lower.
- Costs:
  - `cost` = Sephora's own price × pack quantity, with no store uplift (Nick, Nov 23, 2026). The 5% Sephora uplift on Lead Score exists only to correct costs a VA entered net of Nick's outside cash back; the job reads Sephora's real price, so it sends `store_cost=0` to Lead Score (also for leads made from approved suggestions, `sourced_by = Claude`).
  - Prep is $1.40 per unit everywhere (Nick's Seller Snap figure).
  - The Hub stores ROI and `referral_pct` as fractions.
- When updating existing catalog pairs, always send their current `active`: the Hub re-activates any pair sent without it.
- Sizes: the search API gives only Sephora's price range, not sizes. A multi-size product without a saved size is posted priced at the full size with "Size not checked" (Nov 19, 2026); section 4d's SKU reader saves the exact size.

## 4d. Reading Sephora's SKU lists (sizes, shades, prices, stock) — Nov 17, 2026

Sephora product pages only load in Nick's Chrome. The reader posts what it reads straight to the Hub, so nothing goes through chat:

1. Mint a short-lived upload token from the cloud: `POST /api/engine/upload-token {"purpose":"store_skus","hours":12}` (engine key). The token can only write `store_skus`.
2. In a sephora.com tab: `GET /api/store-skus/todo?limit=60` (header `X-Upload-Token`) lists unread product pages, closest-to-profitable first (Chanel excluded; it was read separately).
3. Read each page (`#linkStore` → `currentSku` + every `*ChildSkus` list), one page every ~7 s, never two at once, and `POST /api/store-skus/upload {products:{P123:{vt, skus:[[sku_id,size,value,desc,price,oos],…]}}}`.
4. In the cloud, `GET /api/engine/store-skus` returns everything read; match each pair's Amazon size/shade to a SKU and write `store_size` (and marks) through `/api/engine/sephora/catalog/mark`.

**Item names and photos (Nov 24, 2026).** Each SKU row carries a 7th element `{names, alt, sw, img}`: Sephora's other name
fields (digit-only values and the item number are dropped; `displayName` is the item number, `skuImages.altText` is only
"product name + size", so neither is used as a shade name). For products sold in colours or shades, the reader also fetches
each item's swatch (and the product shot when there are at most 8 items) during the same page visit and posts them to
`/api/store-skus/images` (table `store_sku_images`). `SOURCE='images'` re-reads the list on Hub doc
`reader/sephora_images_todo` (colour products with unnamed items, and those in Nick's match decisions). In the cloud:
`GET /api/engine/sku-images?skus=…` lists what is stored, `GET /api/engine/sku-image/{sku}/{sw|main}` returns one image.
Some products have no shade names anywhere on Sephora (REFY duo P526371): the swatch photo is then the only evidence.

Status Nov 17: all 1,536 active Sephora products read (11 pages empty or 404, likely discontinued). Profitable pairs and every pair within reach of profit (≥5% now or ≥10% on the 90-day Buy Box) were reviewed by hand; the rest only got their exact size/price set automatically.

## 9. Checker loop (Nov 19, 2026)

Five rounds of blind checks: fresh subagents read every Amazon photo and the live page and named the Sephora SKU, with no access to the matcher's answer. Every fix was regression-tested on Nick's leads (scratchpad `shade/eval_text.py`, `eval_match.py`, `eval_neg.py`) and on all checker labels (`loop/regress.py`) before committing. The summary is the project doc `claude/sephora-catalog/loop/SUMMARY.md`.
- Shade hints the daily job skips (unless the pair is marked OK): `not_sold` (now also "size not sold: …" for travel sizes smaller than any Sephora size) and `bundle` (the listing is a set of two Sephora singles; one single's price isn't the cost).
- The checker prompts must fetch Amazon one page at a time with desktop Safari headers, `--compressed`, and a fresh cookie jar. They must never fetch sephora.com.
- Product re-matches found by the loop are listed, never applied, until Nick says so.
- **Nick's calls from the one-by-one review (Nov 20, 2026; log in the scratchpad `review/decisions.json`):**
  - One difference between two products (finish, packaging, name) may still be the same product; three together (waterproof vs not, glass bottle vs tube, "blur balm" vs "foundation") are a mismatch (ONE/SIZE BBB Cream ≠ Full Beat foundation).
  - A trio is not a duo (LYS mini cream blush trio ≠ the cream + powder duo).
  - A finish word alone can be the same product, and the shade goes by its description: Merit Signature Lip *Matte* Antibes (soft peach) = Signature Lip in Ginger (soft peach). Merit sells no separate matte line.
  - An older version of a line maps to the new line's shades by name. When the old shade has no exact counterpart, the nearest name is acceptable only if the colour agrees too (Huda Easy Bake and Snatch Blondie = Easy Bake Airbrush Matte Kunafa Blondie; plain Blondie doesn't exist in the new line). Old Cupcake (pink undertone) ≠ Peach Cupcake: no counterpart, no match (Nick, Nov 20).
  - A different bottle size is not sold, even when Sephora's is bigger (Coco Noir 1.2 oz listing vs Sephora 1.7 / 3.4 oz; Nick, Nov 20).
  - The original SurrealSkin liquid foundation is Sephora's SurrealSkin *Luminous Hydrating* Foundation, not Natural Finish.
  - A set of Sephora singles is a lead at the real cost, one of each (`pack_qty`), with the note "Bundle: buy one each of …" (Fresh Lip Drip Set).
  - Press-on nails: the design must match; a design Sephora doesn't sell is no match.
  - After-shave is not the fragrance.
  - "If a product doesn't match it doesn't match; if you can find the right pair, match it." A pair marked wrong only for size / shade / pack had the right product, so `rematch.py` only re-matches it to the same product on another Sephora page (mini page, another size), never a sibling scent or line. On Nov 20 this blocked 54 sibling proposals (Allure Homme Sport → Allure, Chance → Chance Eau Fraîche, …).
  - A face cream is never a serum. "Regular Size" with a size means the full size. "36-Hour" / "48hr" are marketing words; waterproof on one side only counts against a match. A duo / 2-pack of one Sephora single is a bundle of 2. "Refillable" is the jar, not a refill. A Mini page never matches a full-size listing.

## 10. Verify step: settle conflicting matches with outside evidence (Nov 20, 2026)

The principles are in `MATCHING_PRINCIPLES.md` (project doc `claude/sephora-catalog/MATCHING_PRINCIPLES.md`). When a pair's evidence conflicts, the scripts don't decide it by score; a research pass does, the way Nick does (web search, brand page, photos, model number), and it records its sources.
1. `python3 -I verify_queue.py --limit 40` lists the conflicts: sibling named in the title, concentration, form / type, line word missing, mini page vs full-size listing, set vs single, low close calls, shade guesses, unsized multi-size pairs. Only undecided, active, live, unverified pairs are listed, and none Nick is restricted from selling (`sellable = restricted`). Near-profit pairs come first, then each kind in turn. Output: Hub doc `sephora_jobs/verify_queue`.
2. Research pass: a Claude session follows `verify_prompt.md` for the queued cases and writes `.data/verify_results.json`. It never loads sephora.com; the Sephora side is the SKU lists (`/api/engine/store-skus`) and the brand listings.
3. `python3 -I verify_apply.py` applies only high-confidence results with a source. Everything is logged in `.data/verify_undo.json`.
   - **same:** sets `extra.verified` and the store size.
   - **other:** marks the pair wrong and adds the right pair.
   - **not sold:** marks the pair wrong.
   - **Otherwise:** the case goes to Nick (`sephora_jobs/verify_for_nick`).

   Pairs Nick decided are never touched. Every result is added to `sephora_jobs/verify_labels`, the test set every matcher change must not get worse on.
4. Each result's `lesson` line is read before changing the matcher. A change is made only when it follows a principle and passes the tests: Nick's leads, his OK and wrong marks, the checker labels, and the verify labels.

## 11. Monthly Amazon restriction check (Nov 20, 2026)

Nick: "the once a month sephora run should update this data". `brand_restrictions.py` re-checks every active Lead DB listing with Amazon's Listing Restrictions API on his seller account (new condition), the method of the Nov 2, 2026 brand eligibility run:
- changed `sellable` values are written back to the catalog (the daily job watches only `open` rows);
- the per-brand counts go to Hub doc `sephora_jobs/brand_restrictions`; a brand is `restricted` when ≥90% of at least 3 checked listings are restricted, and the jobs skip those brands;
- `.data/brand_restrictions_summary.md` is the message: brand status changes, listings that opened up or became restricted.
- Restricted listings are kept but not tracked (Nick, Nov 20, 2026): the Keepa collector skips ASINs whose every pair is restricted, the Lead DB catalog hides them unless "Restricted" or "Any" is picked (a search still finds them), and the daily run and verify step already skip them. This run is what brings an opened listing back.
Run: `python3 -I brand_restrictions.py` (about 30 minutes for ~6,700 ASINs). It writes nothing if more than 20% of checks fail. Scheduled task: "Monthly Sephora restriction check", the 1st of each month.

## 12. Weekly check of new pairs: `check_pairs/pairs.py` (Nick, Nov 20, 2026)

The screen's item is saved as the pair's store size (`collect`, Nov 23, 2026) when its label names exactly one Sephora item (shade name = the item's value, size = its size); otherwise it stays a note. 915 earlier screened pairs were backfilled the same way (undo: Hub doc `sephora_jobs/check_pairs_undo/2026-11-23_screen_items`).
Every Sunday after the weekly Sephora run, every new pair (active, sellable `open`, not decided by Nick, no `extra.verified`
or `extra.screened`, not already on his To review list) gets the same check as the Nov 20 check-all run
(claude/sephora-catalog/CHECK_ALL_2026-11-20.md). At most 300 pairs per run, best ROI first; the rest wait a week.
1. `python3 -I check_pairs/pairs.py prep $W` picks the pairs, writes Sephora product lists from the Lead DB, Nick's Sephora
   purchases, and puts the ASINs whose Amazon page is older than 7 days on Hub doc `reader/amazon_todo`.
2. Amazon pages: Nick's OLD MacBook Chrome, signed-out "Lead search" profile (deviceId fe93b29a-4cf7-498f-ab74-23b11ad69fe4).
   Get a token: `POST /api/engine/upload-token {"purpose":"amazon_pages","hours":12}`; open a new amazon.com tab of your own;
   run `check_pairs/reader.js` with `__TOKEN__` replaced (javascript_tool), then poll `window.az`. Never click, sign in, or
   get around a "continue shopping" / captcha page: the reader pauses itself and stops after 8 pauses. If that Chrome isn't
   connected, skip the reader: the screen and research still run from Keepa data and photos.
   `python3 -I check_pairs/pairs.py wait $W 240` waits for the pages.
3. `pairs.py screen $W` → one Agent (model sonnet) per `screen/prompt_NNN.txt` ("Follow the instructions in <file> exactly"),
   up to 5 at a time. Then `pairs.py collect $W` records the passes on the pairs (`extra.screened`).
4. `pairs.py research $W` → one general-purpose Agent per `research/prompt_NNN.txt`, up to 5 at a time; as each finishes,
   `pairs.py finish $W NNN` cross-checks it (SKUs and products must be in the case file; a pair Nick bought can't be marked
   wrong automatically) and applies it with verify_apply.py: only high confidence with a source; the rest go to
   `sephora_jobs/verify_for_nick`. Undo: Hub doc `sephora_jobs/check_pairs_undo/<date>_<NNN>` (every touched ASIN's pairs before).
   Case files include every Amazon listing photo, main photo first, large or medium size (Nov 24: medium-only photos were
   being dropped, so B0CFM41RR9's research saw only a secondary photo of another product), and the stored Sephora
   swatch/product shot of each item (`image_files`). A printed shade name on the photo settles it (high confidence); a
   colour match alone is medium at best.
   Undecided cases appear on the Hub page `/lead-db/decisions` (Match decisions): research verdict, Amazon photos, Sephora
   items with swatches; "Same product" asks which item when there are several and saves it as the pair's store size.
5. `pairs.py summary $W` → Nick's message.
Writes: Lead DB pairs (catalog upsert / mark), Hub docs `reader/amazon_todo`, `sephora_jobs/verify_for_nick`,
`sephora_jobs/verify_labels`, `sephora_jobs/check_pairs_undo/*`. Nothing else.
