import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AccountError, signIn, signUp } from "../account";
import { useTheme } from "../useTheme";
import { getConnection, pathFor } from "../api";
/**
 * Signing in to a shop.
 *
 * A page rather than a modal, because a sign-in needs to be linkable: emailed after
 * a password reset, bookmarked, returned to after a redirect. A modal can be none of
 * those, and building one now would mean building this later anyway.
 *
 * The heading says what signing in gets you rather than naming the action. Nothing
 * here is locked - a shopper can browse, fill a basket and buy without an account -
 * so "Sign in" as a heading answers a question nobody asked. What they want to know
 * is why they would bother.
 *
 * And there is a way past it. Forcing an account before somebody can shop is how a
 * shop loses people, and every real one lets you buy as a guest.
 */
export function SignInPage({ creating = false }: { creating?: boolean }) {
  const connection = getConnection();
  const merchant = useTheme();
  const navigate = useNavigate();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!username.trim() || !password) return;

    setBusy(true);
    setError(null);

    try {
      // The conversation and cart this browser already has, handed over so
      // signing in adds to what somebody was doing rather than replacing it.
      // The conversation and basket this browser already has, handed over so
      // signing in adds to what somebody was doing rather than replacing it.
      const guestSession =
        sessionStorage.getItem(`cv3_session_${connection}`) ?? "";
      const guestCart = sessionStorage.getItem(`cv3_cart_${connection}`);

      if (creating) {
        await signUp(connection, username.trim(), password, guestSession, guestCart);
      } else {
        await signIn(connection, username.trim(), password, guestSession, guestCart);
      }

      // Cleared before navigating. Leaving a password in state for the rest of
      // the visit is careless in a way nothing here needs to be.
      setPassword("");
      // Back to the shop they came from, not to the default.
      //
      // This went to "/", which redirects to Northfield - so signing in at Kettle
      // dropped somebody into another client's shop. The account is Kettle's and
      // the address should be too.
      navigate(pathFor(connection));
    } catch (e) {
      setError(e instanceof AccountError ? e.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="authpage" data-merchant={connection}>
      <div className="authcard">
        <span className="authshop">{merchant.name}</span>

        <h1 className="authtitle">
          {creating
            ? "Make an account and I'll keep track"
            : "Welcome back"}
        </h1>

        <p className="authwhy">
          {creating
            ? "Your basket and your conversation with the assistant stay with you - close the tab, come back tomorrow, and they are still here."
            : "Your basket and your conversation are where you left them."}
        </p>

        <div className="authform">
          <label>
            Username
            <input
              value={username}
              onChange={(e) => {
                setUsername(e.target.value);
                setError(null);
              }}
              autoComplete="username"
              autoFocus
            />
          </label>

          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                setError(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit();
              }}
              autoComplete={creating ? "new-password" : "current-password"}
            />
            {creating && (
              <span className="authhint">
                Eight characters or more. Long and memorable beats short and
                complicated.
              </span>
            )}
          </label>

          {error && <p className="autherror">{error}</p>}

          <button
            className="add authgo"
            disabled={busy || !username.trim() || !password}
            onClick={submit}
          >
            {busy
              ? "One moment..."
              : creating
                ? "Create account"
                : "Sign in"}
          </button>
        </div>

        <div className="authalt">
          {creating ? (
            <>
              Already have one? <Link to="/signin">Sign in</Link>
            </>
          ) : (
            <>
              New here? <Link to="/signup">Create an account</Link>
            </>
          )}
        </div>

        {/* The way past. Nothing here is locked, and a shop that insists on an
            account before you can look at a shoe is a shop you leave. */}
        <Link className="authskip" to={pathFor(connection)}>
          Carry on without an account
        </Link>
      </div>
    </div>
  );
}