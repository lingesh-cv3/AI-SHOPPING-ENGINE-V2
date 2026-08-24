import type { MerchantTheme } from "./theme";

/**
 * The shop's opening statement.
 *
 * One band, not a screenful. A shop's job is to show stock, so this says what the
 * shop is and gets out of the way.
 *
 * The first version put everything in the left half of a wide screen and left the
 * right two thirds empty, which read as unfinished rather than as restraint. It now
 * runs as two columns: the statement on the left, the facts stacked on the right.
 * On a narrow screen they collapse back into one.
 *
 * The three facts are what a shopper wants before browsing: when it ships, when
 * delivery stops costing, and what happens if it is wrong. Not awards, not a
 * founder story, not a large number with a gradient behind it.
 */
export function Hero({ theme }: { theme: MerchantTheme }) {
  return (
    <section className="hero">
      <div className="hero-inner">
        <div className="hero-lede">
          <div className="eyebrow">{theme.eyebrow}</div>
          <h1>{theme.headline}</h1>
          <p>{theme.standfirst}</p>
        </div>

        <dl className="facts">
          {theme.facts.map((f) => (
            <div key={f.label} className="fact">
              <dt className="fact-label">{f.label}</dt>
              <dd className="fact-value">{f.value}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}