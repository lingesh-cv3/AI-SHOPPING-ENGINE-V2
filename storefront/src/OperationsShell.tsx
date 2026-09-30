import { useState } from "react";
import { clearOperatorKey, operatorKey } from "./api";
import { Copilot } from "./Copilot";
import { OperatorSignIn } from "./OperatorSignIn";
import { OpsOverview } from "./OpsOverview";
import { opsApi } from "./opsApi";
import { resetOverview, useOverview } from "./opsOverviewStore";
import { useOpsTheme } from "./useOpsTheme";

/**
 * The sections Operations shows.
 *
 * A section is listed here only when it has a real source behind it. The target
 * information architecture has more - Work Queue, Incidents, Merchants, Integrations,
 * Orders & Payments, Patterns, Playbooks, Reports & ROI - and each is added to this
 * list in the same change that adds what feeds it. Greyed-out or "coming soon"
 * entries would read as capability that is not there.
 */
const SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "copilot", label: "Copilot" },
] as const;

type SectionId = (typeof SECTIONS)[number]["id"];

/**
 * What the copilot behind this section can actually answer. It is the original
 * workload copilot: its data is shopper follow-ups, how many decisions each merchant
 * has waiting, and shopper issues recorded by category. It is NOT the Operations
 * Intelligence Copilot of the target design, which needs the shared operations read
 * model (incidents, health, integrations, orders, priorities) that does not exist
 * yet. The section says so rather than implying more.
 */
const COPILOT_SCOPE_NOTE =
  "Limited scope. This is the existing workload copilot, not the Operations " +
  "Intelligence Copilot. It answers only from shopper follow-ups, the decisions " +
  "merchants have waiting, and shopper issues recorded by category. It has no " +
  "incident, health, integration, order, payment or revenue data, and it cannot " +
  "take actions. It will be rebuilt on a shared operations read model.";

const SUGGESTED_QUESTIONS = [
  "Which merchants have shopper follow-ups nobody has picked up?",
  "Which merchant has the most decisions waiting?",
  "What are the most common shopper issues across merchants?",
];

/**
 * CV3 Operations: one console across every merchant.
 *
 * Signing in needs the CV3 operator key, typed rather than built in because this
 * bundle is shared with the shopper storefront. There is no sign-out on the server
 * side: signing out forgets the key in this browser.
 */
export function OperationsShell() {
  useOpsTheme();
  const [signedIn, setSignedIn] = useState(() => operatorKey() !== null);
  const [section, setSection] = useState<SectionId>("overview");
  const overview = useOverview();

  // A refused key (the engine answered 401) is handled like signing out.
  const active = signedIn && !overview.signedOut;

  function signOut() {
    clearOperatorKey();
    resetOverview();
    setSignedIn(false);
    setSection("overview");
  }

  return (
    <div className="page ops-page">
      <header className="pagehead ops">
        <div>
          <span className="pagehead-org">CommerceV3</span>
          <h1 className="pagehead-title">CV3 Operations</h1>
        </div>
        {active && (
          <button className="ops-signout" onClick={signOut}>
            Sign out
          </button>
        )}
      </header>

      <main className="pagebody">
        {!active ? (
          <OperatorSignIn
            onSignedIn={() => {
              resetOverview();
              setSignedIn(true);
            }}
          />
        ) : (
          <div className="merchant-shell">
            <nav className="merchant-nav" aria-label="Operations sections">
              <div className="merchant-nav-group">
                {SECTIONS.map((item) => (
                  <button
                    key={item.id}
                    className="merchant-nav-item"
                    aria-current={section === item.id}
                    onClick={() => setSection(item.id)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </nav>

            <div className="merchant-content">
              {section === "overview" && <OpsOverview />}
              {section === "copilot" && (
                <>
                <p className="ops-scope-note" role="note">
                  {COPILOT_SCOPE_NOTE}
                </p>
                <Copilot
                  eyebrow="Workload copilot"
                  title="Ask about follow-ups, waiting decisions and shopper issues"
                  intro="Ask in plain language about the workload Operations holds today. Questions about incidents, API or webhook health, orders, payments or revenue are outside what it can see."
                  suggestions={SUGGESTED_QUESTIONS}
                  placeholder="Ask about follow-ups, waiting decisions or shopper issues…"
                  ask={opsApi.askCopilot}
                />
                </>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
