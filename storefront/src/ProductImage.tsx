/**
 * Generated product artwork.
 *
 * No photography, for two reasons: we have no rights to stock imagery, and these
 * are invented products that no photograph exists of. Generated artwork is the
 * honest option - it beats grey placeholder boxes, and it beats borrowing images we
 * should not use.
 *
 * A real client supplies photographs. The Product model already carries image_url
 * for exactly that, and this renders only when it is absent - which is the correct
 * fallback rather than the intended state.
 *
 * The ground tint is derived from the product id, so two running shoes look
 * distinct while the whole set stays coherent. Deterministic, so a product looks the
 * same on every page load and in every view.
 */

/** Muted grounds. Low saturation on purpose - the artwork sits behind price and
 *  stock information, and a vivid tile would compete with what matters. */
const GROUNDS: [string, string][] = [
  ["#E4E7E4", "#3A4A3F"],
  ["#E3E6EC", "#3B4457"],
  ["#EAE6E0", "#4A4238"],
  ["#E6E9E1", "#414A36"],
  ["#E1E5EA", "#374350"],
  ["#EBE4E4", "#4E3C3C"],
  ["#E9E3DA", "#4B3F30"],
  ["#E4E8E7", "#3A4744"],
  ["#E6E4EB", "#443E52"],
  ["#EAE7E1", "#4A463C"],
];

type GlyphName =
  | "shoe"
  | "garment"
  | "bottle"
  | "nutrition"
  | "watch"
  | "roller"
  | "bag"
  | "cup"
  | "dripper"
  | "gift";

/** Match on the title first, then the category.
 *
 *  Title first because it is more specific: "Insulated Water Bottle" is an accessory,
 *  but drawing a bottle says more than drawing a generic accessory. An unrecognised
 *  category gets a gift box - a plausible glyph reads better than an empty tile.
 */
function glyphFor(title: string, category: string | undefined): GlyphName {
  const t = title.toLowerCase();
  const c = (category ?? "").toLowerCase();

  if (/shoe|sneaker|slide|flat/.test(t)) return "shoe";
  if (/bottle|flask/.test(t)) return "bottle";
  if (/watch|scale|sensor|strap|headphone|torch/.test(t)) return "watch";
  if (/roller|band|ball|boots|massage/.test(t)) return "roller";
  if (/dripper|grinder|kettle|filter|pour/.test(t)) return "dripper";
  if (/subscription|espresso|latte/.test(t)) return "cup";
  if (/gift|box|kit|set/.test(t)) return "gift";
  if (/gel|tablet|protein|capsule|chew|bar/.test(t)) return "nutrition";
  if (/tee|jacket|shorts|tights|gilet|singlet|zip|sleeve|sock|cap/.test(t))
    return "garment";

  if (/footwear/.test(c)) return "shoe";
  if (/apparel/.test(c)) return "garment";
  if (/nutrition/.test(c)) return "nutrition";
  if (/tech/.test(c)) return "watch";
  if (/recovery/.test(c)) return "roller";
  if (/origin|blend/.test(c)) return "bag";
  if (/equipment/.test(c)) return "dripper";
  if (/subscription/.test(c)) return "cup";
  if (/accessor/.test(c)) return "bottle";
  return "gift";
}

const PATHS: Record<GlyphName, React.ReactElement> = {
  shoe: (
    <>
      <path d="M6 30c0-3 1-5 3-6l7-4 5 4h9c4 0 8 2 11 5l1 3H8c-1.2 0-2-1-2-2z" />
      <path d="M16 20l3 4M21 24h6" />
      <path d="M6 33h36" />
    </>
  ),
  garment: (
    <>
      <path d="M18 8l-8 5 3 7 3-1.5V40h16V18.5l3 1.5 3-7-8-5" />
      <path d="M18 8c0 3 2.7 5 6 5s6-2 6-5" />
    </>
  ),
  bottle: (
    <>
      <rect x="19" y="6" width="10" height="5" rx="1" />
      <path d="M20 11v3l-3 4v20a2 2 0 002 2h10a2 2 0 002-2V18l-3-4v-3" />
      <path d="M17 24h14" />
    </>
  ),
  nutrition: (
    <>
      <path d="M14 12h20l-2 26a3 3 0 01-3 3H19a3 3 0 01-3-3z" />
      <path d="M13 12l2-5h18l2 5" />
      <path d="M20 20v14M28 20v14" />
    </>
  ),
  watch: (
    <>
      <rect x="14" y="14" width="20" height="20" rx="5" />
      <path d="M18 14l1-6h10l1 6M18 34l1 6h10l1-6" />
      <path d="M24 20v5l3 2" />
    </>
  ),
  roller: (
    <>
      <rect x="8" y="18" width="32" height="12" rx="6" />
      <path d="M16 18v12M32 18v12" />
      <path d="M12 24h-3M39 24h-3" />
    </>
  ),
  bag: (
    <>
      <path d="M14 14h20v24a3 3 0 01-3 3H17a3 3 0 01-3-3z" />
      <path d="M14 14l3-6h14l3 6" />
      <path d="M19 22h10M19 28h10" />
    </>
  ),
  cup: (
    <>
      <path d="M12 16h22l-3 16a4 4 0 01-4 3h-8a4 4 0 01-4-3z" />
      <path d="M34 20h3a4 4 0 010 8h-4" />
      <path d="M14 41h20" />
    </>
  ),
  dripper: (
    <>
      <path d="M12 14h24l-9 14v8h-6v-8z" />
      <path d="M18 41h12" />
      <path d="M17 20h14" />
    </>
  ),
  gift: (
    <>
      <rect x="9" y="16" width="30" height="22" rx="2" />
      <path d="M9 23h30M24 16v22" />
      <path d="M24 16c-4-6-11-4-8 0M24 16c4-6 11-4 8 0" />
    </>
  ),
};

/** Stable hash of the product id, so a product keeps the same ground every time. */
function tintIndex(seed: string): number {
  let h = 0;
  for (let i = 0; i < seed.length; i++) {
    h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  }
  return h % GROUNDS.length;
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

  const [ground, ink] = GROUNDS[tintIndex(productId)];
  const glyph = glyphFor(title, category);

  return (
    <div
      className={
        (large ? "pimage pimage-lg" : "pimage") + (soldOut ? " pimage-out" : "")
      }
      style={{ background: ground }}
      // Decorative: the product name sits directly beneath it, so announcing the
      // artwork would repeat what a screen reader has already said.
      aria-hidden="true"
    >
      <svg
        viewBox="0 0 48 48"
        fill="none"
        stroke={ink}
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {PATHS[glyph]}
      </svg>
    </div>
  );
}