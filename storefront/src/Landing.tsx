import type { MerchantTheme } from "./theme";

/**
 * What a shopper sees first: one sentence about the shop, and one question.
 *
 * The question is the design. A running shop that asks what you run on is showing
 * it knows the difference; a category nav labelled Footwear is showing it has a
 * database. Same for a roastery asking how you brew - the same bean is wrong in the
 * wrong brewer, and a shop that leads with that is one you believe.
 *
 * It replaced a headline with a tracked capital label above it and three facts
 * joined by middle dots. Those were applied to every heading in the app rather than
 * chosen for any of them, which is what made them worth removing.
 *
 * The dispatch facts moved down beside the question, where they answer the second
 * thing a shopper wants to know rather than competing with the first.
 */
export function Landing({
  theme,
  chosen,
  onChoose,
  onAsk,
}: {
  theme: MerchantTheme;
  chosen: string | null;
  onChoose: (label: string | null) => void;
  /** The last answer opens the assistant rather than filtering, because "not sure
   *  yet" is a request for help and a grid is not help. */
  onAsk: () => void;
}) {
  return (
    <section className="landing">
      <div className="landing-inner">
        <h1 className="landing-head">{theme.headline}</h1>
        <p className="landing-sub">{theme.standfirst}</p>

        <div className="asked">
          <h2 className="asked-question">{theme.opening.question}</h2>

          <div className="answers">
            {theme.opening.answers.map((answer) => {
              const isChosen = chosen === answer.label;
              const isAsk = answer.products.length === 0 && answer.label.length < 14;

              return (
                <button
                  key={answer.label}
                  className={isChosen ? "answer chosen" : "answer"}
                  aria-pressed={isChosen}
                  onClick={() => {
                    if (answer.label.toLowerCase().startsWith("not sure")) {
                      onAsk();
                      return;
                    }
                    onChoose(isChosen ? null : answer.label);
                  }}
                >
                  <span className="answer-label">{answer.label}</span>
                  <span className="answer-note">{answer.note}</span>
                </button>
              );
            })}
          </div>
        </div>

        <dl className="facts">
          {theme.facts.map((fact) => (
            <div key={fact.label}>
              <dt>{fact.label}</dt>
              <dd>{fact.value}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}
