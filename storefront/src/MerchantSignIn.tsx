import { useState } from "react";
import { setMerchantKey } from "./api";

/**
 * Sign-in for one merchant's console.
 *
 * Names the merchant, because the reason this appears again after switching client
 * is the whole point: a secret key speaks for one merchant and one only. Without
 * saying so, being asked twice reads as a bug rather than as isolation working.
 */
export function MerchantSignIn({
  connectionId,
  merchantName,
  onSignedIn,
}: {
  connectionId: string;
  merchantName: string;
  onSignedIn: () => void;
}) {
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);

  function submit() {
    const trimmed = key.trim();
    if (!trimmed) return;

    // Caught locally because five keys sit in one file and pasting the wrong one is
    // the mistake that actually happens. The engine decides validity; this only
    // saves a pointless round trip and says something useful.
    if (trimmed.startsWith("cv3_pk_")) {
      setError(
        "That is a publishable key. It can browse the shop but not read settings.",
      );
      return;
    }
    if (trimmed.startsWith("cv3_op_")) {
      setError(
        "That is the CV3 operator key. This console wants this merchant's own key.",
      );
      return;
    }
    if (!trimmed.startsWith("cv3_sk_")) {
      setError("A merchant key starts with cv3_sk_.");
      return;
    }

    setMerchantKey(connectionId, trimmed);
    onSignedIn();
  }

  return (
    <div className="signin">
      <div className="eyebrow">{merchantName}</div>
      <h2 className="signin-title">Sign in to see your settings</h2>
      <p className="signin-note">
        This console shows one merchant&rsquo;s revenue, policy and approvals, so it
        needs that merchant&rsquo;s own key. Switching client asks again &mdash;
        holding one client&rsquo;s key tells the engine nothing about another.
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
        placeholder="cv3_sk_..."
        aria-label="Merchant key"
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
