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
  - A Sephora size bigger than the listing is acceptable; a smaller one is not.
  - A **Mini page never matches a full-size listing.**
  - A full-size page that also sells the mini is fine for a mini listing.
- **Refill vs refillable:** a refill is a refill. "Refillable" describes the jar, not a refill.

**Versions.** An older or newer version of the same product is a match: renamed, repackaged or reformulated (Easy Bake and Snatch → Easy Bake Airbrush Matte, SurrealSkin → SurrealSkin Luminous Hydrating). The listing's shade has to exist in the current line.
- When a line renames its shades, map them by name and description ("Antibes, soft peach" = Ginger, soft peach). If there's no exact counterpart, the nearest name is acceptable (Blondie → Kunafa Blondie).
- When a line renumbers its shades, the old numbers don't carry over (MUFE Artist Rouge C206).
- A reformulation that made the product a little smaller is still a match at today's size: MAC Studio Fix Powder Plus, old 15 g compact = today's 12 g. The "smaller Sephora size isn't acceptable" rule is for sizes Sephora sells alongside each other, not for a version change.

**How many differences.** One difference (finish, packaging or name) can still be the same product. Several together make it a different product. Waterproof vs not, glass bottle vs tube, and "blur balm" vs "foundation" all at once = different.

**Sets**
- A set is not its contents, and its contents are not the set.
- When Sephora sells the single, the single's page is the match, not a Sephora set that contains it.
- When the Amazon listing is a set of Sephora items (two lip creams, powder + puff, a duo of one mascara), it's a **bundle lead at the real cost**: one of each item, prices added up.
- If any product in the set isn't sold at Sephora, the set can't be built there and it's no lead (Ole Henriksen Party of 4: Blood Orange isn't at Sephora).

**No near-misses.** If a product doesn't match, it doesn't match. When a pair is wrong, the only acceptable replacement is the right pair: the same product on another Sephora page. A sibling scent or the "next closest" product never replaces it.

## 2. Which evidence to trust, strongest first

1. **Amazon's model / part number or barcode equal to a Sephora item number.** This settles shade and size outright.
2. **Text printed on the product in the photos:** box ends, stickers, the bottle band, shade codes and sizes on the label.
3. **The brand's own site or Sephora's page:** what the product is called, which strengths and sizes it comes in, which shades exist.
4. **The live Amazon page:** the selected variation, the per-ounce price and the Buy Box price. Price helps pick mini vs full size, not identity.
5. **The Amazon title and bullets.** Often right, sometimes wrong (an "EDT" that only exists as an EDP), and sometimes copied from a sibling listing.
6. **Keepa's color, size and model fields.** Often filler, a sibling's value or stale.

When a weaker source contradicts a stronger one, the stronger one wins.

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
