import { useState } from "react";
import { setOperatorKey } from "./api";

/**
 * Sign-in for the operations console.
 *
 * Short on purpose. This is an internal tool used by people who already have the
 * key, so anything more than one field and a button is ceremony.
 *
 * The copy says plainly that the key is not stored in the app, because the reason
 * an operator has to type it is worth explaining once rather than being a mystery
 * they resent.
 */
export function OperatorSignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);

  function submit() {
    const trimmed = key.trim();
    if (!trimmed) return;

    // Checked here only to catch a paste of the wrong key, which is the common
    // mistake when five of them sit in one file. The engine decides whether it is
    // valid; this just avoids a pointless round trip.
    if (!trimmed.startsWith("cv3_op_")) {
      setError(
        "That looks like a merchant key. The operations console needs the operator key.",
      );
      return;
    }

    setOperatorKey(trimmed);
    onSignedIn();
  }

  return (
    <div className="signin">
      <div className="eyebrow">CV3 operations</div>
      <h2 className="signin-title">Sign in to see the queue</h2>
      <p className="signin-note">
        This queue spans every client, so it needs the CV3 operator key rather than
        a merchant one. It is not stored in the app &mdash; the storefront and this
        console share code, so a key built in would be readable by any shopper.
      </p>

      <input
        className="signin-input"
        type="password"
        value={key}
        onChange={(e) => {
          setKey(e.target.value);
          setError(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        placeholder="cv3_op_..."
        aria-label="Operator key"
        autoFocus
      />

      {error && <p className="signin-error">{error}</p>}

      <button className="add signin-button" disabled={!key.trim()} onClick={submit}>
        Sign in
      </button>

      <p className="note">
        Kept for this browser session only. Closing the tab signs you out.
      </p>
    </div>
  );
}
