/**
 * Generated product artwork.
 *
 * No photography: we have no rights to stock imagery, and these are invented
 * products no photograph exists of. Generated line drawings beat grey boxes and
 * beat borrowing images we should not use.
 *
 * A real client supplies photographs. Product carries image_url for exactly that,
 * and this renders only when it is absent, which makes it a fallback rather than
 * the intended state.
 *
 * Two things were wrong with the first version. Every product in a category got the
 * same drawing, so three coffees in a row were three identical bags. And the tints
 * came from one fixed palette, so both shops looked the same beneath different
 * products. Glyphs are now matched on the product itself, and tints come from the
 * active theme.
 */

type GlyphName =
  | "roadshoe"
  | "trailshoe"
  | "slide"
  | "top"
  | "shorts"
  | "jacket"
  | "cap"
  | "socks"
  | "bottle"
  | "gel"
  | "watch"
  | "headphones"
  | "roller"
  | "band"
  | "beans"
  | "beanbag"
  | "cup"
  | "dripper"
  | "grinder"
  | "kettle"
  | "scale"
  | "gift";

/**
 * Match on the product's own name first, its category second.
 *
 * Name first because it is more specific: a gooseneck kettle and a burr grinder are
 * both Equipment, and drawing them the same wastes the only visual the card has.
 */
function glyphFor(title: string, category: string | undefined): GlyphName {
  const t = title.toLowerCase();
  const c = (category ?? "").toLowerCase();

  // Coffee
  if (/kettle/.test(t)) return "kettle";
  if (/grinder/.test(t)) return "grinder";
  if (/scale/.test(t)) return "scale";
  if (/dripper|pour|filter paper/.test(t)) return "dripper";
  if (/subscription/.test(t)) return "cup";
  if (/gift|taster|brew kit/.test(t)) return "gift";
  if (/decaf|blend|roast|espresso/.test(t)) return "beanbag";
  if (/ethiopia|colombia|kenya|brazil|guji|huila|nyeri|cerrado/.test(t))
    return "beans";

  // Footwear
  if (/trail|fell/.test(t)) return "trailshoe";
  if (/slide|recovery slide/.test(t)) return "slide";
  if (/shoe|sneaker|flat/.test(t)) return "roadshoe";

  // Apparel
  if (/jacket|gilet|half zip|thermal/.test(t)) return "jacket";
  if (/shorts|tights/.test(t)) return "shorts";
  if (/cap/.test(t)) return "cap";
  if (/sock|calf sleeve/.test(t)) return "socks";
  if (/tee|singlet|base layer|sleeve/.test(t)) return "top";

  // Accessories and tech
  if (/bottle|flask|hydration/.test(t)) return "bottle";
  if (/gel|tablet|protein|capsule|chew|bar/.test(t)) return "gel";
  if (/watch/.test(t)) return "watch";
  if (/headphone|torch/.test(t)) return "headphones";
  if (/roller|massage|boots/.test(t)) return "roller";
  if (/band|belt|strap|pod/.test(t)) return "band";

  // Category fallbacks
  if (/footwear/.test(c)) return "roadshoe";
  if (/apparel/.test(c)) return "top";
  if (/nutrition/.test(c)) return "gel";
  if (/tech/.test(c)) return "watch";
  if (/recovery/.test(c)) return "roller";
  if (/origin/.test(c)) return "beans";
  if (/blend/.test(c)) return "beanbag";
  if (/equipment/.test(c)) return "dripper";
  if (/subscription/.test(c)) return "cup";
  if (/accessor/.test(c)) return "bottle";
  return "gift";
}

const PATHS: Record<GlyphName, React.ReactElement> = {
  roadshoe: (
    <>
      <path d="M6 33c0-4 2-7 6-9l7-4 4 4h9c5 0 9 3 11 7l1 2H8a2 2 0 01-2-2z" />
      <path d="M17 21l4 4M21 27l4-3" />
      <path d="M5 36h38" />
    </>
  ),
  trailshoe: (
    <>
      <path d="M6 31c0-4 2-7 6-9l7-4 4 4h9c5 0 9 3 11 7l1 2H8a2 2 0 01-2-2z" />
      <path d="M17 19l4 4M21 25l4-3" />
      <path d="M5 34h38M10 34v4M17 34v4M24 34v4M31 34v4M38 34v4" />
    </>
  ),
  slide: (
    <>
      <path d="M9 30c0-3 2-5 5-5h20c3 0 5 2 5 5v3H9z" />
      <path d="M14 25c2-5 6-7 10-7s8 2 10 7" />
      <path d="M8 36h32" />
    </>
  ),
  top: (
    <>
      <path d="M18 9l-8 5 3 7 3-2v22h16V19l3 2 3-7-8-5" />
      <path d="M18 9c0 3 2.7 5 6 5s6-2 6-5" />
    </>
  ),
  shorts: (
    <>
      <path d="M13 12h22l-2 12-2 16h-7l-2-13-2 13h-7l-2-16z" />
      <path d="M13 18h22" />
    </>
  ),
  jacket: (
    <>
      <path d="M17 8l-7 5 2 9 3-1v20h18V21l3 1 2-9-7-5" />
      <path d="M24 8v33" />
      <path d="M17 8l7 5 7-5" />
    </>
  ),
  cap: (
    <>
      <path d="M11 28c0-8 6-14 13-14s13 6 13 14" />
      <path d="M11 28h32c1 0 2 1 2 2s-1 2-2 2H11z" />
      <path d="M24 14v14" />
    </>
  ),
  socks: (
    <>
      <path d="M17 8h11v16l7 7a5 5 0 01-7 7l-9-9a6 6 0 01-2-4z" />
      <path d="M17 14h11" />
    </>
  ),
  bottle: (
    <>
      <rect x="19" y="6" width="10" height="5" rx="1" />
      <path d="M20 11v3l-3 4v20a2 2 0 002 2h10a2 2 0 002-2V18l-3-4v-3" />
      <path d="M17 25h14" />
    </>
  ),
  gel: (
    <>
      <path d="M15 12h18v25a3 3 0 01-3 3H18a3 3 0 01-3-3z" />
      <path d="M15 12l2-4h14l2 4" />
      <path d="M20 20h8M20 26h8" />
    </>
  ),
  watch: (
    <>
      <rect x="14" y="14" width="20" height="20" rx="5" />
      <path d="M18 14l1-6h10l1 6M18 34l1 6h10l1-6" />
      <path d="M24 20v5l3 2" />
    </>
  ),
  headphones: (
    <>
      <path d="M11 28v-4a13 13 0 0126 0v4" />
      <rect x="7" y="26" width="7" height="11" rx="3" />
      <rect x="34" y="26" width="7" height="11" rx="3" />
    </>
  ),
  roller: (
    <>
      <rect x="8" y="18" width="32" height="12" rx="6" />
      <path d="M16 18v12M32 18v12" />
      <path d="M12 24H9M39 24h-3" />
    </>
  ),
  band: (
    <>
      <path d="M12 18c8-6 16-6 24 0s4 14-4 14-16-2-20-6" />
      <path d="M12 18l-4-3M12 18l-3 4" />
    </>
  ),
  beans: (
    <>
      <ellipse cx="19" cy="21" rx="8" ry="6" transform="rotate(-30 19 21)" />
      <path d="M16 25c2-3 4-5 7-6" />
      <ellipse cx="30" cy="31" rx="8" ry="6" transform="rotate(-30 30 31)" />
      <path d="M27 35c2-3 4-5 7-6" />
    </>
  ),
  beanbag: (
    <>
      <path d="M14 15h20v23a3 3 0 01-3 3H17a3 3 0 01-3-3z" />
      <path d="M14 15l4-6h12l4 6" />
      <circle cx="24" cy="24" r="3" />
      <path d="M18 32h12" />
    </>
  ),
  cup: (
    <>
      <path d="M12 17h21l-3 15a4 4 0 01-4 3h-7a4 4 0 01-4-3z" />
      <path d="M33 21h3a4 4 0 010 8h-4" />
      <path d="M13 40h20" />
      <path d="M20 12c0-2 1-3 1-4M26 12c0-2 1-3 1-4" />
    </>
  ),
  dripper: (
    <>
      <path d="M12 14h24l-9 13v6h-6v-6z" />
      <path d="M16 33h16l2 8H14z" />
      <path d="M17 20h14" />
    </>
  ),
  grinder: (
    <>
      <rect x="16" y="16" width="16" height="22" rx="2" />
      <path d="M24 16v-4M24 12h7" />
      <circle cx="32" cy="12" r="2" />
      <path d="M16 28h16" />
    </>
  ),
  kettle: (
    <>
      <path d="M14 22h16v14a3 3 0 01-3 3H17a3 3 0 01-3-3z" />
      <path d="M30 26c4 0 6-3 6-7v-6" />
      <path d="M18 22v-3a4 4 0 018 0v3" />
      <path d="M34 13h4" />
    </>
  ),
  scale: (
    <>
      <rect x="8" y="20" width="32" height="14" rx="3" />
      <rect x="14" y="25" width="11" height="5" rx="1" />
      <circle cx="33" cy="27" r="2" />
      <path d="M13 20v-3h22v3" />
    </>
  ),
  gift: (
    <>
      <rect x="9" y="17" width="30" height="21" rx="2" />
      <path d="M9 24h30M24 17v21" />
      <path d="M24 17c-4-6-11-4-8 0M24 17c4-6 11-4 8 0" />
    </>
  ),
};

/** Stable hash of the product id, so a product keeps the same ground every time. */
function tintIndex(seed: string): number {
  let h = 0;
  for (let i = 0; i < seed.length; i++) {
    h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  }
  return (h % 6) + 1;
}

export function ProductImage({
  productId,
  title,
  category,
  imageUrl,
  soldOut,
  large,
}: {
  productId: string;
  title: string;
  category?: string;
  /** A real photograph, when the merchant's platform provides one. */
  imageUrl?: string | null;
  soldOut?: boolean;
  large?: boolean;
}) {
  if (imageUrl) {
    return (
      <div className={large ? "pimage pimage-lg" : "pimage"}>
        <img src={imageUrl} alt={title} />
      </div>
    );
  }

  const glyph = glyphFor(title, category);

  return (
    <div
      className={
        (large ? "pimage pimage-lg" : "pimage") + (soldOut ? " pimage-out" : "")
      }
      style={{ background: `var(--tint-${tintIndex(productId)})` }}
      // Decorative. The product name sits directly beneath it, so announcing the
      // artwork would repeat what a screen reader has already said.
      aria-hidden="true"
    >
      <svg
        viewBox="0 0 48 48"
        fill="none"
        stroke="var(--tint-ink)"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {PATHS[glyph]}
      </svg>
    </div>
  );
}