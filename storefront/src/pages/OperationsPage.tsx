
import { OpsConsole } from "../OpsConsole";
import { useTheme } from "../useTheme";
/**
 * The CV3 queue, on its own page.
 *
 * A thin wrapper on purpose. The console already handles its own sign-in and its
 * own refresh, and rewriting a working component to move it to a URL would be
 * spending risk for nothing.
 *
 * The masthead says CV3 rather than a merchant's name, because this page belongs to
 * us. Every other page in this app is somebody's shop; this is the one that spans
 * them, and it should not look like it belongs to whichever client was open last.
 */
export function OperationsPage() {
  // The console inside uses the merchant tokens even though this head is CV3's own.
  useTheme();

  return (
    <div className="page">
      <header className="pagehead ops">
        <div>
          <span className="pagehead-org">CommerceV3</span>
          <h1 className="pagehead-title">Operations</h1>
        </div>

      </header>

      <main className="pagebody">
        <OpsConsole />
      </main>
    </div>
  );
}