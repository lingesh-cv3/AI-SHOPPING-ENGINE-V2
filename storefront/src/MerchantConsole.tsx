import { NotAuthorised, getConnection, merchantKey } from "./api";
import { MerchantSignIn } from "./MerchantSignIn";
import { useCallback, useEffect, useState } from "react";
import {
  console_api,
  type ActionInfo,
  type Capabilities,
  type Pipeline,
  type Policy,
  type Rule,
} from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";
import { MerchantReport } from "./MerchantReport";
import { InventoryPanel } from "./InventoryPanel";
import { PaymentsPanel } from "./PaymentsPanel";
import { Overview } from "./Overview";
import { ProductPerformance } from "./ProductPerformance";
import { OrdersConversion } from "./OrdersConversion";
import { CustomerInsights } from "./CustomerInsights";
import { Returns } from "./Returns";
import { AICommerce } from "./AICommerce";
import { Recovery } from "./Recovery";
import { Holdout } from "./Holdout";
import { BusinessInsights } from "./BusinessInsights";
import { PlatformCapabilities } from "./PlatformCapabilities";
import { StoreSettings } from "./StoreSettings";
import { TasksPanel } from "./TasksPanel";

type SectionId =
  | "overview"
  | "sales"
  | "orders"
  | "products"
  | "customers"
  | "inventory"
  | "payments"
  | "returns"
  | "ai-commerce"
  | "recovery"
  | "holdout"
  | "insights"
  | "platform"
  | "settings"
  | "copilot"
  | "tasks";

interface NavGroup {
  label: string;
  items: { id: SectionId; label: string }[];
}

const NAV: NavGroup[] = [
  { label: "Merchant", items: [{ id: "overview", label: "Overview" }] },
  {
    label: "Business",
    items: [
      { id: "sales", label: "Sales & Revenue" },
      { id: "orders", label: "Orders & Conversion" },
      { id: "products", label: "Product Performance" },
      { id: "customers", label: "Customer & Shopping Insights" },
    ],
  },
  {
    label: "Commerce",
    items: [
      { id: "inventory", label: "Inventory & Catalog" },
      { id: "payments", label: "Payments & Checkout" },
      { id: "returns", label: "Returns" },
    ],
  },
  {
    label: "AI & Outcomes",
    items: [
      { id: "ai-commerce", label: "AI Commerce" },
      { id: "recovery", label: "Recovery" },
      { id: "holdout", label: "Holdout / Experiment" },
      { id: "insights", label: "Business Insights" },
    ],
  },
  {
    label: "Store",
    items: [
      { id: "platform", label: "Platform / Capabilities" },
      { id: "settings", label: "Settings" },
    ],
  },
  {
    label: "AI",
    items: [
      { id: "copilot", label: "Merchant Copilot" },
      { id: "tasks", label: "Merchant Tasks" },
    ],
  },
];

/**
 * The merchant's own view of how the engine behaves on their store,
 * organised as one coherent product with left-side navigation rather than
 * a long scroll of unrelated cards.
 *
 * Every section reads from the same authoritative routes the engine
 * already exposes - nothing here fabricates a number. Where a genuine gap
 * exists (a full conversion funnel, customer-level analytics, returns),
 * the section says so honestly instead of showing one anyway. See
 * PROGRESS.md and Completed.md for exactly which of those gaps this
 * session closed and which remain open.
 */
export function MerchantConsole() {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [actions, setActions] = useState<ActionInfo[]>([]);
  const [test, setTest] = useState<Pipeline | null>(null);
  const [tested, setTested] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [section, setSection] = useState<SectionId>("overview");
  // Signed out is the normal first state, not a failure, so it is tracked
  // separately from `error`.
  const [signedIn, setSignedIn] = useState(
    () => merchantKey(getConnection()) !== null,
  );

  const refresh = useCallback(async () => {
    try {
      const [c, p, r, a] = await Promise.all([
        console_api.capabilities(),
        console_api.policy(),
        console_api.rules(),
        console_api.actions(),
      ]);
      setCaps(c);
      setPolicy(p);
      setRules(r);
      setActions(a);
    } catch (e) {
      // A refused key is not an outage. Conflating them has a merchant restarting
      // services when they need to sign in.
      if (e instanceof NotAuthorised) {
        setSignedIn(false);
        return;
      }
      setError("Could not reach the engine. Is it running on port 8000?");
    }
  }, []);

  useEffect(() => {
    if (!signedIn) return;
    refresh();
  }, [refresh, signedIn]);

  async function setMode(mode: string) {
    if (!policy) return;
    setPolicy(
      await console_api.savePolicy(mode, [], policy.blocked, policy.holdout_percent),
    );
    setTest(null);
  }

  async function onBlock(actionType: string, blocked: boolean) {
    if (!policy) return;
    const next = blocked
      ? [...policy.blocked, actionType]
      : policy.blocked.filter((a) => a !== actionType);
    // auto_allowed is left empty deliberately. It is now an optional restriction
    // rather than a permission list, and most merchants want every safe action.
    setPolicy(
      await console_api.savePolicy(policy.mode, [], next, policy.holdout_percent),
    );
    setTested(actionType);
    setTest(await console_api.testAction(actionType));
  }

  async function onHoldout(percent: number) {
    if (!policy) return;
    setPolicy(
      await console_api.savePolicy(policy.mode, [], policy.blocked, percent),
    );
  }

  async function onApprovalTimeout(minutes: number) {
    if (!policy) return;
    setPolicy(
      await console_api.savePolicy(
        policy.mode,
        [],
        policy.blocked,
        policy.holdout_percent,
        minutes,
      ),
    );
  }
  if (!signedIn) {
    return (
      <MerchantSignIn
        connectionId={getConnection()}
        merchantName={caps?.platform ?? getConnection()}
        onSignedIn={() => {
          setSignedIn(true);
          setError(null);
        }}
      />
    );
  }

  if (error) return <p className="empty">{error}</p>;
  if (!caps || !policy) return <p className="empty">Loading…</p>;

  return (
    <div className="merchant-shell">
      <nav className="merchant-nav" aria-label="Merchant sections">
        {NAV.map((group) => (
          <div key={group.label} className="merchant-nav-group">
            <div className="merchant-nav-label">{group.label}</div>
            {group.items.map((item) => (
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
        ))}
      </nav>

      <main className="merchant-content">
        {section === "overview" && <Overview onNavigate={(s) => setSection(s as SectionId)} />}
        {section === "sales" && (
          <MerchantReport onNavigate={(s) => setSection(s as SectionId)} />
        )}
        {section === "orders" && (
          <OrdersConversion onNavigate={(s) => setSection(s as SectionId)} />
        )}
        {section === "products" && <ProductPerformance />}
        {section === "customers" && <CustomerInsights />}
        {section === "inventory" && <InventoryPanel />}
        {section === "payments" && <PaymentsPanel />}
        {section === "returns" && <Returns />}
        {section === "ai-commerce" && <AICommerce />}
        {section === "recovery" && (
          <Recovery onNavigate={(s) => setSection(s as SectionId)} />
        )}
        {section === "holdout" && <Holdout />}
        {section === "insights" && <BusinessInsights />}
        {section === "platform" && <PlatformCapabilities caps={caps} rules={rules} />}
        {section === "settings" && (
          <StoreSettings
            policy={policy}
            actions={actions}
            test={test}
            tested={tested}
            onMode={setMode}
            onBlock={onBlock}
            onHoldout={onHoldout}
            onApprovalTimeout={onApprovalTimeout}
          />
        )}
        {section === "copilot" && (
          <MerchantCopilot onNavigate={(s) => setSection(s as SectionId)} />
        )}
        {section === "tasks" && <TasksPanel />}
      </main>
    </div>
  );
}
