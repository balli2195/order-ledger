You are screening Amazon ↔ Sephora product pairs. For each pair, decide quickly whether the saved Sephora product AND the saved Sephora item (shade/size) are clearly the same thing as the Amazon listing. Pairs you flag get full research later, so a false flag costs little; a wrong pair you pass as "same" costs real money. When in doubt, flag.

Read first: __PRINCIPLES__

Cases: __CASES__ (a JSON list). Each case has:
- amazon: title, selected_variation and details (from the live page read in Chrome, when present), bullets, keepa_color/size/scent, model, main_image.
- sephora: product name, brand, saved_item (the Sephora shade/size saved for this pair; null = none saved), items (every Sephora item of that product with price), price_range.

Look at the main photo for every case: curl -s -o <work>/<id>.jpg https://m.media-amazon.com/images/I/<main_image> and open it with Read. Never load amazon.com or sephora.com pages and don't use web search; decide from the case and the photo.

Calls:
- "same": the Amazon listing is this exact Sephora product (same line, same format) AND the saved item is the listing's shade and size (or, for a one-size product, the only size). Two Sephora items of the same size and shade are interchangeable.
  When NO item is saved (saved_item null): call "same" only if the product is clearly the same AND exactly one item in `items` (or several interchangeable ones) is clearly the listing's shade and size, shown in the title, details or photo; put its label in "item". If no item clearly fits, or two different items could fit, the call is "unsure".
- "wrong_product": a different product. Watch for: a set or kit vs a single; a single vs a set; sibling lines that share words (Sauvage vs Eau Sauvage, Poison vs Hypnotic Poison, Pencil vs Detailer vs Wax, Retro Matte vs other MAC lipsticks); a different format (rollerball vs spray, stick vs liquid, refill vs full, powder vs cream, travel spray vs bottle); a different concentration (EDT vs EDP vs Parfum); a Sephora Collection or other-brand product.
- "wrong_item": right product, but the saved item is the wrong shade or size; give the right item label from `items` in "item" if one fits.
- "not_sold": right product but Sephora doesn't sell the listing's size or version (e.g. a 1.2 oz bottle when Sephora has 1.7 and 3.4; a mini when Sephora only has full size; a pack of 3 when Sephora sells singles).
- Flag (don't call "same") whenever one name has a version word the other lacks (SPF, Mattifying, Mini, Refill, Intense, Extreme, Bubble, Deluxe, a different product name printed on the box), or the listing looks like an older version that may have been renamed or reformulated. If your own reason has any "confirm" or "if" in it, the call is "unsure", not "same".
- A free extra from another brand (a sheet mask or masque, acne patches) doesn't count against "same": judge the Sephora item alone and start "why" with "EXTRA: <the extra>;".
- "unsure": can't tell from the case and photo (shade or size not shown, title too vague). Say what's missing.

Output a JSON list to __OUT__, one object per case, in case order: {"id": "...", "call": "...", "item": "<label or null>", "why": "<one short sentence naming the deciding fact>"}. Write the file as you go (rewrite it after every 10 cases). Work in __WORK__. Don't use MCP or connector tools. When done, reply with only the counts per call.

Look at every case's main photo (contact sheets of 6-9 photos made with python PIL are fine as long as each photo is readable). At the end, say how many photos you viewed.
