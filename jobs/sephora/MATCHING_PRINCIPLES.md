# How to tell if an Amazon listing is a Sephora product

Draft for Nick, Nov 20, 2026. Everything that matches listings follows these principles: the matcher's scoring, the verify step, the checkers and Claude. They come from Nick's calls in the Nov 19–20 reviews. A new rule is only added when the same reasoning works across brands, not to fix one listing.

## 1. What makes two listings the same product

**Identity is what the product is.** All of these must agree:
- **Brand**
- **Line**: Airbrush Flawless, Easy Bake, Signature Lip, Allure Homme Sport
- **Name inside the line**: the scent or flanker (Eau Extrême, Eau Tendre, Sport, Fahrenheit), or the variant (Waterproof, Blur, Luminous vs Natural Finish)
- **Format**: liquid, stick, powder, pencil, gel, cream, serum, balm, spray; fragrance vs after-shave vs body cream; nails vs toenails; product vs its accessory (sharpener, puff)
- **Single or set**: a single, a duo, a trio, a kit. A trio is not a duo.

**Attributes are checked against what exists.** Size, strength (EDT / EDP), finish, packaging and shade don't decide identity alone. Each has to be one Sephora sells for that product:
- **Shade:** a shade Sephora doesn't sell is skipped. A close shade (Noir = Black, same description) is kept and flagged for review.
- **Size:** the Sephora size must equal the Amazon size. Nearly the same volume is fine.
  - A different bottle size, bigger or smaller, is not the listing's product: Sephora doesn't sell that size (Nick, Nov 20: Coco Noir 1.2 oz listing, Sephora only 1.7 / 3.4 oz → not sold).
  - **One-size products (Nick, Nov 20):** when the brand makes the product in only one size, a different size on the label is the same item (markets round grams and ounces differently: MUFE Artist Blush 4 g on the brand site = Sephora's 5 g). A real second size, such as a mini, still doesn't count.
  - **Two Sephora SKUs of the same size are interchangeable** (Kayali's $29 and $32 10 mL): the cheaper is the store size, the other an alternate. Not knowing which of them it is never makes a match unsure.
  - A **Mini page never matches a full-size listing.**
  - A full-size page that also sells the mini is fine for a mini listing.
- **Refill vs refillable:** a refill is a refill. "Refillable" describes the jar, not a refill.

**Versions.** An older or newer version of the same product is a match: renamed, repackaged or reformulated (Easy Bake and Snatch → Easy Bake Airbrush Matte, SurrealSkin → SurrealSkin Luminous Hydrating). The listing's shade has to exist in the current line.
- When a line renames its shades, map them by name and description ("Antibes, soft peach" = Ginger, soft peach). If there's no exact counterpart, the nearest name is acceptable only when the colour agrees too (Blondie → Kunafa Blondie). A shared word with a different colour is no match (Nick, Nov 20: old Cupcake, pink undertone ≠ Peach Cupcake; no counterpart, so not sold).
- When a line renumbers its shades, the old numbers don't carry over (MUFE Artist Rouge C206).
- **Discontinued items (Nick, Nov 20):** when Amazon's model or part number is an old Sephora item number that isn't in today's SKU lists, that exact item is discontinued: not sold, unless the brand names a successor and the shade carries over by name and colour (Sephora Matte Perfection Powder Foundation → not sold).
- **Past-year sets (Nick, Nov 20):** a past-year or holiday edition is the same set as today's when it has the same products, sizes and applicators (a rollerball is not a spray). Brands often re-release a set with the same products under a new name or packaging, and Amazon keeps the old listing, so a different set name or box alone is not a difference.
- A reformulation that made the product a little smaller is still a match at today's size: MAC Studio Fix Powder Plus, old 15 g compact = today's 12 g. The size rule is for sizes Sephora sells alongside each other, not for a version change.

**How many differences.** One difference (finish, packaging or name) can still be the same product. Several together make it a different product. Waterproof vs not, glass bottle vs tube, and "blur balm" vs "foundation" all at once = different.

**Sets**
- A set is not its contents, and its contents are not the set.
- When Sephora sells the single, the single's page is the match, not a Sephora set that contains it.
- When the Amazon listing is a set of Sephora items (two lip creams, powder + puff, a duo of one mascara), it's a **bundle lead at the real cost**: one of each item, prices added up.
- If any product in the set isn't sold at Sephora, the set can't be built there and it's no lead (Ole Henriksen Party of 4: Blood Orange isn't at Sephora).

**No near-misses.** If a product doesn't match, it doesn't match. When a pair is wrong, the only acceptable replacement is the right pair: the same product on another Sephora page. A sibling scent or the "next closest" product never replaces it.

## 2. Which evidence to trust, strongest first

1. **Amazon's model / part number equal to a Sephora item number.** This settles shade and size outright.
2. **Text printed on the product in the photos:** box ends, stickers, the bottle band, shade codes and sizes on the label.
3. **The brand's own site or Sephora's page:** what the product is called, which strengths and sizes it comes in, which shades exist.
4. **The live Amazon page:** the selected variation, the per-ounce price and the Buy Box price. Price helps pick mini vs full size, not identity.
5. **The Amazon title and bullets.** Often right, sometimes wrong (an "EDT" that only exists as an EDP), and sometimes copied from a sibling listing.
6. **Keepa's color, size and model fields.** Often filler, a sibling's value or stale.

**Variation family.** Amazon lists a product's shades as sibling listings. When every sibling is clearly another shade, the one left over is this listing's shade; that beats judging a photo's colour.

**Missing data is fetched, not guessed.** When the right Sephora page has no SKU list in the data, or a "not sold" call rests on a list that may be stale, read that page (Nick's old MacBook Chrome, signed out) before deciding.

When a weaker source contradicts a stronger one, the stronger one wins.

**Barcodes are a later step (Nick, Nov 20).** Amazon UPCs / EANs are often seller-assigned or regional, so they can differ even when the product is the same. Check a barcode against the brand's own list (brand sites publish one per shade and size: Westman, Makeup by Mario, Haus Labs, Natasha Denona, Merit, ONE/SIZE, Josie Maran, LYS, REFY; Chanel and Dior barcodes map to their product references) only when the title and photos don't settle the shade or size, for example when no shade is named or printed. When the title and photos agree on the product, ignore a barcode that disagrees. Nick, Nov 24, 2026: always go by the title, photos, bullets, selected variation or product description; the UPC is
only a clue. A shade named in any of them is not put in doubt by a barcode for another shade (B0F2RL8RXM, REFY concealer
shade 5 with shade 21N's barcode, is shade 5), and on its own a barcode never makes a match high confidence.

## 3. When the evidence conflicts, verify instead of guessing

A conflict is any case where two signals point to different answers. Examples:
- The line name points to one product, but the strength or format word points to another (Allure Homme Sport *Eau Extrême* "EDT").
- The title's shade differs from the photo or the selector.
- The listing names something Sephora's list doesn't have.
- The title's size isn't a size Sephora sells.
- The match only works by ignoring a word that names a sibling product.

To verify, do what Nick does:
1. Search the exact product (brand + line + name + strength or size) and check whether it exists as the listing describes.
2. Read the brand's page.
3. Look at the photos and the model number.
4. Record the source used.

The answer decides the match. An Amazon claim that turns out not to exist (Eau Extrême EDT) is a title error, not a different product.

## 4. When it can't be settled

Don't guess. Keep the pair with a note saying exactly what's unsure ("Shade not confirmed: looks like …", "Close shade, check it", "Two shades named", "Size not checked"), or leave it unmatched. Nick reviews those. A wrong match costs more than a missing one.

## 5. How changes are made

- A change has to follow from a principle above and be tested before it's used: on Nick's leads, on every pair Nick marked OK or wrong, and on all the checker answers so far. It's only kept if none of those get worse.
- A word list (penalty words, marketing words) is a stopgap. Where a brand's own catalog can show which words separate its sibling products, the catalog decides, not a hand list.
- Nick's calls are recorded with the reason (decision log and RUNBOOK). Each one is turned into the principle behind it, not a one-listing rule.

## Splitting a Sephora set into Amazon singles (Nick, Nov 20, 2026)

Nick buys Sephora sets and sells the pieces as Amazon singles (Beneville set → BADgal Bang, Hoola Mini, Benetint; Oil Sucker duo → primer + spray; Crown Affair duo → shampoo + conditioner). A single Amazon listing paired with a Sephora set that contains exactly that item (same product, shade and size as the piece in the set) is a valid source: verdict `same`, with `split_of`:
`{"set_sku_id", "set_label", "set_price", "piece_value", "total_value", "pieces": [{"label", "product_id", "sku_id", "price"}]}`.
- The set's price is shared across its pieces by each piece's own Sephora price: cost = set_price × piece_value / total_value (a $40 set of a $30 and a $20 item → $24 and $16).
- piece_value and total_value use each piece's own Sephora price (its single page, same size). If a piece isn't sold on its own at Sephora, use the brand's own price for that size and say so in `evidence` (medium).
- The piece in the set must be the listing's exact item: a mini in the set does not fill a full-size listing, and a different shade doesn't count.
- A set that Nick has bought for this ASIN (his leads' store link) is strong evidence the split works.
