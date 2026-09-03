import { useState } from "react";
import { type Account, AccountError, signIn, signOut, signUp } from "./account";

/**
 * A strip in the chat header: sign in, or who you are.
 *
 * Collapsed to one line by default, because the chat is for chatting and a
 * permanent login form inside it would be the wrong emphasis.
 *
 * The copy leads with the reason rather than the action. Every sign-in panel says
 * "Sign in" and lists two fields, but nothing here is locked - there is no access to
 * gain. The only reason to do it is so the assistant remembers you next time, and
 * saying that is more use to a shopper than a heading that says Sign in.
 */
export function AccountStrip({
  connectionId,
  guestSession,
  account,
  onAccount,
}: {
  connectionId: string;
  /** The conversation they are having now, handed over when they sign in. */
  guestSession: string;
  account: Account | null;
  onAccount: (next: Account | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function attempt(creating: boolean) {
    if (!username.trim() || !password) return;

    setBusy(true);
    setError(null);

    try {
      const next = creating
        ? await signUp(connectionId, username.trim(), password, guestSession)
        : await signIn(connectionId, username.trim(), password, guestSession);

      // Cleared immediately. Leaving a password in a React state field for the
      // rest of the visit is careless in a way nothing here needs to be.
      setPassword("");
      setUsername("");
      setOpen(false);
      onAccount(next);
    } catch (e) {
      setError(e instanceof AccountError ? e.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  }

  if (account) {
    return (
      <div className="acct">
        <span className="acct-who">{account.display_name}</span>
        <button
          className="acct-link"
          onClick={async () => {
            await signOut(connectionId);
            onAccount(null);
          }}
        >
          {/* Prominent on purpose.
            *
            * Signing in adopts whatever the previous person was saying, so on a
            * shared computer this is the undo. Hiding it behind a menu would make
            * the convenient choice the unsafe one. */}
          Not you?
        </button>
      </div>
    );
  }

  if (!open) {
    return (
      <button className="acct-invite" onClick={() => setOpen(true)}>
        Sign in and I&rsquo;ll remember this next time
      </button>
    );
  }

  return (
    <div className="acct-form">
      <input
        value={username}
        onChange={(e) => {
          setUsername(e.target.value);
          setError(null);
        }}
        placeholder="Username"
        aria-label="Username"
        autoComplete="username"
        autoFocus
      />
      <input
        type="password"
        value={password}
        onChange={(e) => {
          setPassword(e.target.value);
          setError(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") attempt(false);
        }}
        placeholder="Password"
        aria-label="Password"
        autoComplete="current-password"
      />

      {error && <p className="acct-error">{error}</p>}

      <div className="acct-buttons">
        <button
          className="add"
          disabled={busy || !username.trim() || !password}
          onClick={() => attempt(false)}
        >
          {busy ? "..." : "Sign in"}
        </button>
        {/* Both on screen rather than a toggle between two modes.
          *
          * A shopper knows whether they have an account here; making them find the
          * right form first is a step that exists only because it was easier to
          * build. */}
        <button
          className="reject"
          disabled={busy || !username.trim() || !password}
          onClick={() => attempt(true)}
        >
          Create one
        </button>
      </div>

      <button
        className="acct-link"
        onClick={() => {
          setOpen(false);
          setError(null);
          setPassword("");
        }}
      >
        Not now
      </button>
    </div>
  );
}