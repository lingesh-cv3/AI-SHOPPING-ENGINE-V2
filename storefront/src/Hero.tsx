import type { MerchantTheme } from "./theme";

/**
 * The shop's opening statement.
 *
 * One band, not a screenful. A shop's job is to show stock, so this says what the
 * shop is and gets out of the way.
 *
 * The three facts are chosen for what a shopper actually wants to know before
 * browsing: when it ships, when delivery stops costing, and what happens if it is
 * wrong. Not awards, not founder stories, not a number with a gradient behind it.
 *
 * Hidden once a shopper searches. They have said what they want; repeating the
 * shop's introduction above their results would be talking over them.
 */
export function Hero({ theme }: { theme: MerchantTheme }) {
  return (
    <section className="hero">
      <div className="hero-inner">
        <div className="eyebrow">{theme.eyebrow}</div>
        <h1>{theme.headline}</h1>
        <p>{theme.standfirst}</p>

        <div className="facts">
          {theme.facts.map((f) => (
            <div key={f.label}>
              <div className="fact-label">{f.label}</div>
              <div className="fact-value">{f.value}</div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}