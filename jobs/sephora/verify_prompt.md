# Verify pass: settle conflicting matches with outside evidence

You are checking Amazon ↔ Sephora matches whose evidence conflicts. Follow `MATCHING_PRINCIPLES.md` (project doc `claude/sephora-catalog/MATCHING_PRINCIPLES.md`); read it first. Work like Nick does when a listing doesn't add up: look the product up instead of guessing.

## Input

The verify queue (`GET /api/engine/docs/sephora_jobs/verify_queue`, or `.data/verify_queue.json`). Each case has:
- `asin`, `amazon_title`, `amazon_url`
- `product_id`, `sephora_name`, `sephora_url`, `store_size`
- `kinds` (why it's in the queue), the `question` to answer, and the matcher's `matcher_reason`

The Sephora side: the product's SKU list (sizes, shades, prices) from `GET /api/engine/store-skus`, and the brand's Sephora listing (all product names) from the catalog. **Never load sephora.com pages or its API.**

## For each case

1. **Read the live Amazon page** with curl: desktop Safari user agent, `--compressed`, a fresh cookie jar, no Accept header, one page at a time with a few seconds between, and retry once after a pause if blocked. Note:
   - the full title
   - the selected variation (shade / size)
   - the model or part number
   - the item form and the bullets
   - the Buy Box price and the per-ounce price
2. **Look at the listing photos** (download from `m.media-amazon.com`, view them). Read what's printed on the product: the name, the size, the shade, "mini", "refill", the bottle band, box stickers.
3. **Check against what exists.** Web-search the exact product (brand + line + name + strength / size). Read the brand's own page where possible, plus retailers that list the variants. Answer: does the product exist as the Amazon listing describes it? Which Sephora product and SKU is it? Does Sephora sell that size or shade?
4. **Decide, using the evidence order** in the principles: model number = Sephora item number, then printed packaging, then brand / retailer pages, then the Amazon page, then the title, then Keepa fields.

## Output (one JSON object per case, appended as you go)

```json
{"asin": "...", "product_id": "...",
 "verdict": "same | other | not_sold | cant_tell",
 "correct_product_id": "P… (for 'other', a product of the same brand from the Sephora listing; null if Sephora doesn't sell it)",
 "sku_id": "the Sephora SKU (shade / size) when known",
 "bundle": [{"sku_id": "...", "qty": 1}],
 "confidence": "high | medium | low",
 "principle": "which principle decided it (e.g. 'a Mini page never matches a full-size listing')",
 "evidence": "what you saw, briefly: model number, box text, brand page",
 "sources": ["URLs you used"],
 "lesson": "if the matcher got it wrong: the general reason, in one line"}
```

- `bundle` is for listings that are a set of Sephora items. Use `{"product_id", "sku_id" (or null), "qty", "note"}` per item. A set that also holds things Sephora doesn't sell (a pouch, another brand's sheet mask) is still a bundle when the Sephora items are the point of the set. Say what's extra in `evidence`, and use medium confidence.
- `verdict`:
  - `other`: the listing is a different product. Give `correct_product_id`, or null when Sephora doesn't sell it at all.
  - `not_sold`: the product is right, but Sephora doesn't sell the listing's shade or size.
- **Sephora products with no SKUs in the data** haven't been read yet (or were discontinued). Don't treat that as "not sold". Settle the size from other sources if you can, else medium confidence.
- A page that sells only a travel size counts as a mini page, whatever its name.
- Write the results where the run tells you to (default `.data/verify_results.json`). Running `verify_apply.py` is the session's job, not yours, if you were given only the cases file.
- `high` confidence needs a source that settles it (a model number match, printed packaging, or the brand's page). Without one, say medium and explain what's missing.
- Never invent a URL; cite only pages you actually loaded.

Write the results to `.data/verify_results.json` (a JSON list). Then run `python3 -I verify_apply.py`.
