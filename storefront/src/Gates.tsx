import type { Pipeline } from "./api";

/**
 * The engine's reasoning, drawn as timing gates.
 *
 * Chosen because the pipeline genuinely is a sequence of checkpoints an action
 * passes, is held at, or is turned away from - the same three outcomes a runner
 * meets at a timing gate. The metaphor is not decoration; it maps.
 *
 * Everything here comes from the engine. No text is composed in the frontend, so
 * what is displayed is what the engine actually decided.
 *
 * Note the two reply fields. shopper_reply is what a shopper sees; reply is what
 * the model wanted to say. They differ when the model proposed something the
 * platform cannot do, and showing both makes the substitution visible rather than
 * silent - which matters, because a system quietly rewriting its own AI's output
 * is exactly the sort of thing that should be auditable.
 */
export function Gates({ pipeline }: { pipeline: Pipeline }) {
  const rejectedTypes = new Set(pipeline.rejected.map((r) => r.action_type));
  const substituted =
    pipeline.reply !== null &&
    pipeline.shopper_reply !== null &&
    pipeline.reply !== pipeline.shopper_reply;

  let step = 0;
  const mark = () => String(++step).padStart(2, "0");

  return (
    <div className="gates">
      {pipeline.shopper_reply && (
        <div className="gate">
          <span className="gate-mark">{mark()}</span>
          <div>
            <div className="gate-label">What the shopper is told</div>
            <p className="said">{pipeline.shopper_reply}</p>
            {substituted && (
              <div className="gate-note swapped">
                Replaced. The AI wanted to say: &ldquo;{pipeline.reply}&rdquo; —
                but the actions it suggested aren&rsquo;t available on this
                platform, so that would have promised something we can&rsquo;t
                deliver.
              </div>
            )}
          </div>
        </div>
      )}

      {pipeline.diagnosis && (
        <div className="gate">
          <span className="gate-mark">{mark()}</span>
          <div>
            <div className="gate-label">
              Diagnosis
              <span className={pipeline.used_model ? "prov ai" : "prov rules"}>
                {pipeline.used_model ? "AI" : "rules"}
              </span>
            </div>
            <div className="gate-value">{pipeline.diagnosis}</div>
            {pipeline.evidence.length > 0 && (
              <ul className="evidence">
                {pipeline.evidence.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      <div className="gate">
        <span className="gate-mark">{mark()}</span>
        <div>
          <div className="gate-label">Considered</div>
          {pipeline.proposed.map((action) => (
            <div
              key={action}
              className={
                rejectedTypes.has(action) ? "gate-value struck" : "gate-value"
              }
            >
              {action.replace(/_/g, " ").toLowerCase()}
            </div>
          ))}
          {!pipeline.used_model && (
            <p className="note">
              {pipeline.fallback_reason
                ? `The AI wasn't used: ${pipeline.fallback_reason}.`
                : "Proposed by rules, not the AI."}
            </p>
          )}
        </div>
      </div>

      {pipeline.rejected.length > 0 && (
        <div className="gate">
          <span className="gate-mark">{mark()}</span>
          <div>
            <div className="gate-label">Turned away</div>
            {pipeline.rejected.map((r) => (
              <div key={r.action_type}>
                <div className="gate-value">
                  {r.action_type.replace(/_/g, " ").toLowerCase()}
                </div>
                <div className="gate-note">{r.detail}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="gate">
        <span className="gate-mark">{mark()}</span>
        <div>
          <div className="gate-label">Chosen</div>
          <div className="gate-value">
            {pipeline.selected_action.replace(/_/g, " ").toLowerCase()}
          </div>
          <div className="gate-note">{pipeline.selection_reason}</div>
        </div>
      </div>

      <div className="gate">
        <span className="gate-mark">{mark()}</span>
        <div>
          <div className="gate-label">Risk gate</div>
          <div className={`verdict ${pipeline.risk_outcome}`}>
            {pipeline.risk_outcome === "AUTO"
              ? "Runs now"
              : pipeline.risk_outcome === "HUMAN"
                ? "Needs a person"
                : "Blocked"}
          </div>
          <div className="rule">{pipeline.risk_rule}</div>
          <div className="gate-note">{pipeline.risk_reason}</div>
          {pipeline.financial && (
            <div className="gate-note">
              This moves money, so it can never run unattended.
            </div>
          )}
          {pipeline.used_model && pipeline.prompt_tokens && (
            <div className="gate-note num" style={{ opacity: 0.6 }}>
              {pipeline.model_name} · {pipeline.prompt_tokens}+
              {pipeline.completion_tokens} tokens
            </div>
          )}
        </div>
      </div>
    </div>
  );
}