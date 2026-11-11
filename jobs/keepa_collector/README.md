# Keepa collector (Nov 11, 2026)

Always-on Render background worker (`keepa-collector`, Starter, Oregon) that keeps Keepa data for every Lead DB ASIN, for Lead Score training. Code: `collector.py` (its docstring has the details).

- **What it stores (Hub database):**
  - `keepa_raw`: the latest full Keepa product per ASIN (history, stats, Buy Box, 20 offers with stock, rating), gzipped JSON, replaced on every pull. About 130–240 KB per ASIN.
  - `keepa_snapshots`: one row per pull with the day's Buy Box (and its seller), FBA/new/Amazon prices, offer counts, monthly sold, rank, rating, and the offers list (seller, FBA, price, stock) gzipped. Split by month (`keepa_snapshots_YYYY_MM`; the collector creates the next month's table itself).
  - `keepa_collector_status`: heartbeat and counters (System map shows the last beat).
- **What it pulls:** hot ASINs (pairs the daily finder watches, on-sale pairs, open candidates, Nick's leads from the last 30 days) every 22 h; every other active Lead DB ASIN every 72 h. Never-pulled ASINs first. About 6.5 tokens per ASIN.
- **Token sharing:** never takes the shared Keepa key below `KEEPA_RESERVE` (3000) and averages at most `KEEPA_MAX_PER_MIN` (90) tokens a minute of the plan's 125, so the daily Sephora job, Lead Score and the other jobs keep room.
- **Env:** `DATABASE_URL` (Hub database internal URL), `KEEPA_KEY`; optional `KEEPA_RESERVE`, `KEEPA_MAX_PER_MIN`, `HOT_HOURS`, `COLD_HOURS`, `BATCH`, `PAUSED=1` (idle without stopping the service).
- **Check it:** `GET /api/engine/training/summary` (engine key) or the System map. Local test: `python3 jobs/keepa_collector/collector.py --once`.

## Training export
`tools/export_training.py --out <folder>` (needs `ENGINE_KEY`, pandas, pyarrow) downloads everything for training, incrementally: Lead DB catalog, candidates, wrong-match feedback and runs (Parquet), `catalog_history` and `keepa_snapshots` (append-only Parquet parts) and the raw Keepa products (`keepa_raw/keepa_raw_<date>_cNNN.json.gz`, 80 per chunk, + `keepa_raw_index.json`, the same format as the Lead Score Keepa pulls). Read with DuckDB. Same script on Nick's computer or in a cloud session.

## Sizing (Nov 11, 2026)
Hub database: 0.5 CPU / 1 GB RAM, storage raised from 5 GB (Nick, Nov 11). ~6,200 Lead DB pairs ≈ 5,000 ASINs → ~1 GB of raw + ~5 MB/day of snapshots, plus catalog_history. Move to the 2 GB plan when a second store is added or exports get slow.
