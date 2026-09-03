
import { getConnection } from "../api";
import { MerchantConsole } from "../MerchantConsole";
import { useTheme } from "../useTheme";
/**
 * One merchant's own console, on its own page.
 *
 * A thin wrapper, because the console already handles its own sign-in and refresh.
 *
 * Themed as that merchant rather than as CV3, which is the opposite of the
 * operations page and deliberately so: this page belongs to the client, and it
 * should look like their shop rather than like our tooling.
 */
export function MerchantPage() {
  const connection = getConnection();
  const merchant = useTheme();

  return (
    <div className="page" data-merchant={connection}>
      <header className="pagehead">
        <div>
          <span className="pagehead-org">{merchant.name}</span>
          <h1 className="pagehead-title">Your shop</h1>
        </div>

      </header>

      <main className="pagebody">
        <MerchantConsole />
      </main>
    </div>
  );
}