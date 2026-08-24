/**
 * Per-merchant identity.
 *
 * Two clients on one engine should not look like one template with different
 * products in it. A running shop and a coffee roaster are different businesses
 * selling to different people, and a storefront that ignores that reads as a demo.
 *
 * The visual tokens live in CSS, keyed off `data-theme` on the document root. This
 * file holds the things CSS cannot: the words, and the facts each shop leads with.
 *
 * Copy is written per brand rather than parameterised. "Free delivery over ₹2,000"
 * and "Roasted to order" are not the same sentence with a variable in it, and
 * templating them would produce the flat voice that makes storefronts feel
 * generated.
 */

export interface MerchantTheme {
  /** Sets the CSS token set. */
  theme: "northfield" | "kettle";
  name: string;
  /** Sits above the headline. Two or three words. */
  eyebrow: string;
  /** What this shop is, in the shop's own voice. */
  headline: string;
  standfirst: string;
  /** Three facts, shown as a strip. Chosen for what a shopper actually wants to
   *  know before browsing, not for what sounds impressive. */
  facts: { label: string; value: string }[];
  searchPlaceholder: string;
  couponHint: string;
  assistantIntro: string;
}

export const THEMES: Record<string, MerchantTheme> = {
  conn_demo: {
    theme: "northfield",
    name: "Northfield Running Co.",
    eyebrow: "Since 2011 · Puducherry",
    headline: "Kit that holds up past the twentieth kilometre",
    standfirst:
      "Road, trail and track. Everything here has been run in before it was stocked.",
    facts: [
      { label: "Dispatch", value: "Same day before 2pm" },
      { label: "Free delivery", value: "Over ₹2,000" },
      { label: "Returns", value: "30 days, worn or not" },
    ],
    searchPlaceholder: "Shoes, tights, gels…",
    couponHint: "Got a code? Add it here.",
    assistantIntro:
      "Ask about sizing, stock, or an order. If something went wrong earlier, I already know.",
  },

  conn_kettle: {
    theme: "kettle",
    name: "Kettle & Bloom",
    eyebrow: "Roastery · Lot 14",
    headline: "Green coffee, landed and roasted in small batches",
    standfirst:
      "Single origins and blends, roasted the week you order. Nothing sits.",
    facts: [
      { label: "Roasted", value: "Tuesdays and Fridays" },
      { label: "Free delivery", value: "Over ₹1,500" },
      { label: "Grind", value: "To your brewer, no charge" },
    ],
    searchPlaceholder: "Origin, roast, equipment…",
    couponHint: "Got a code? Add it here.",
    assistantIntro:
      "Ask about a coffee, a brew method, or an order. If something went wrong earlier, I already know.",
  },
};

export function themeFor(connectionId: string): MerchantTheme {
  return THEMES[connectionId] ?? THEMES.conn_demo;
}