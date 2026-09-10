import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import type { CopilotAnswer } from "./api";

interface CopilotTurn {
  question: string;
  answer: string;
  usedModel: boolean;
}

/** Turns **bold** and simple "- " bullet lines into real markup, and a
 *  "| a | b |" markdown table into an actual table - without pulling in a
 *  markdown library for what the model reliably produces: short prose,
 *  the occasional bold figure, an occasional bulleted or tabular list. */
function renderAnswer(text: string) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let bullets: string[] = [];
  let table: string[][] = [];

  const flushBullets = () => {
    if (bullets.length) {
      blocks.push(
        <ul className="copilot-list" key={`ul-${blocks.length}`}>
          {bullets.map((b, i) => (
            <li key={i}>{renderInline(b)}</li>
          ))}
        </ul>,
      );
      bullets = [];
    }
  };
  const flushTable = () => {
    if (table.length) {
      const [header, ...rows] = table;
      blocks.push(
        <div className="copilot-table-wrap" key={`tbl-${blocks.length}`}>
          <table className="copilot-table">
            <thead>
              <tr>
                {header.map((h, i) => (
                  <th key={i}>{renderInline(h)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>{renderInline(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      table = [];
    }
  };

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      flushBullets();
      flushTable();
      continue;
    }
    if (/^\|.*\|$/.test(line)) {
      const cells = line
        .slice(1, -1)
        .split("|")
        .map((c) => c.trim());
      // Skip a markdown separator row ("---|---").
      if (!cells.every((c) => /^:?-{2,}:?$/.test(c))) table.push(cells);
      continue;
    }
    flushTable();
    if (/^[-*]\s+/.test(line)) {
      bullets.push(line.replace(/^[-*]\s+/, ""));
      continue;
    }
    flushBullets();
    blocks.push(<p key={`p-${blocks.length}`}>{renderInline(line)}</p>);
  }
  flushBullets();
  flushTable();
  return blocks;
}

function renderInline(text: string): ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? (
      <strong key={i}>{part.slice(2, -2)}</strong>
    ) : (
      part
    ),
  );
}

interface ActionBanner {
  /** The real, current fact - e.g. "3 payment recovery cases are waiting". */
  text: string;
  buttonLabel: string;
  onClick: () => void;
}

interface CopilotProps {
  /** Shown above the title, e.g. "Merchant copilot" / "Operations copilot". */
  eyebrow: string;
  title: string;
  /** One line explaining what it can answer, shown before the first question. */
  intro: string;
  /** Real questions the data can actually answer, shown as starter chips. */
  suggestions: string[];
  placeholder: string;
  ask: (question: string) => Promise<CopilotAnswer>;
  /** A real, currently-true fact with one reachable action attached, shown
   *  above the transcript regardless of what's been asked - e.g. pending
   *  payment-recovery cases, deep-linking straight to the panel that can
   *  act on them. `null`/omitted when there is nothing actionable right
   *  now, or nothing has been checked yet - never a placeholder. This is
   *  the one place this otherwise read-only panel can point somewhere
   *  that actually does something, closing the "answers a problem, then
   *  leaves the merchant to go and act on it elsewhere" gap for whichever
   *  problem types already have a real action wired up. */
  actionBanner?: ActionBanner | null;
}

/**
 * A read-only Q&A panel over real, live data - grounded, never invented, and
 * unable to take any action itself. Shared between the merchant console
 * (its own store's figures) and the operations console (workload across
 * every merchant an operator covers) because the two differ only in which
 * question they answer and where the data comes from, never in how the
 * panel itself behaves. Keeps a running transcript rather than one
 * overwritten answer, since a second question building on the first is the
 * normal way to use something like this.
 */
export function Copilot({
  eyebrow,
  title,
  intro,
  suggestions,
  placeholder,
  ask: askApi,
  actionBanner,
}: CopilotProps) {
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<CopilotTurn[]>([]);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const transcriptRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, asking]);

  async function ask(text?: string) {
    const q = (text ?? question).trim();
    if (!q || asking) return;
    setQuestion("");
    setAsking(true);
    setError(null);
    try {
      const reply = await askApi(q);
      setTurns((prev) => [
        ...prev,
        { question: q, answer: reply.answer, usedModel: reply.used_model },
      ]);
    } catch {
      setError("Could not reach the copilot. Is the engine running?");
    } finally {
      setAsking(false);
    }
  }

  return (
    <section className="panel copilot-panel">
      <div className="panel-head">
        <div>
          <span className="eyebrow">{eyebrow}</span>
          <h3 className="copilot-title">{title}</h3>
        </div>
        <span className="copilot-badge">Read-only</span>
      </div>
      <div className="panel-body copilot-body">
        {actionBanner && (
          <div className="copilot-action-banner">
            <span>{actionBanner.text}</span>
            <button
              type="button"
              className="copilot-action-banner-btn"
              onClick={actionBanner.onClick}
            >
              {actionBanner.buttonLabel} →
            </button>
          </div>
        )}
        {turns.length === 0 ? (
          <div className="copilot-empty">
            <p>{intro}</p>
            <div className="copilot-chips">
              {suggestions.map((q) => (
                <button
                  key={q}
                  className="copilot-chip"
                  onClick={() => ask(q)}
                  disabled={asking}
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="copilot-transcript" ref={transcriptRef}>
            {turns.map((t, i) => (
              <div className="copilot-turn" key={i}>
                <div className="copilot-question">{t.question}</div>
                <div className="copilot-answer">
                  {renderAnswer(t.answer)}
                  {!t.usedModel && (
                    <p className="copilot-fallback-note">
                      Not generated by the model - shown as a fallback.
                    </p>
                  )}
                </div>
              </div>
            ))}
            {asking && (
              <div className="copilot-turn">
                <div className="copilot-answer copilot-thinking">
                  <span />
                  <span />
                  <span />
                </div>
              </div>
            )}
          </div>
        )}

        {error && <p className="note">{error}</p>}

        <div className="copilot-inputrow">
          <input
            type="text"
            className="copilot-input"
            value={question}
            placeholder={placeholder}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") ask();
            }}
            aria-label="Ask the copilot a question"
          />
          <button
            className="copilot-ask"
            disabled={asking || !question.trim()}
            onClick={() => ask()}
          >
            {asking ? "Asking…" : "Ask"}
          </button>
        </div>
      </div>
    </section>
  );
}
