import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { type Account, type RememberedFact, forgetFacts, rememberedFacts, signOut } from "./account";
import { getConnection, pathFor } from "./api";
/**
 * The shopper's name in the masthead, and the way out.
 *
 * Signing out was only reachable from inside the chat, which is a strange place to
 * keep it - and it matters more than convenience: signing in adopts whatever the
 * previous person was saying, so on a shared computer this is the undo. A shopper
 * who cannot find it is stuck as somebody else.
 */
export function AccountMenu({
  account,
  onAccount,
}: {
  account: Account | null;
  onAccount: (next: Account | null) => void;
}) {
  const [open, setOpen] = useState(false);
  // "What I remember": null = closed, otherwise the list as last read from the engine (never a guess).
  const [memory, setMemory] = useState<{ ok: boolean; facts: RememberedFact[] } | null>(null);
  const box = useRef<HTMLDivElement>(null);

  // Close on a click anywhere else, and on Escape.
  //
  // Both, because a menu that only closes by clicking the same button again is a
  // menu people leave open by accident - and then click something behind it.
  useEffect(() => {
    if (!open) return;

    function onDown(e: MouseEvent) {
      if (box.current && !box.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }

    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!account) {
    // The merchant in the address, not the bare /signin. The shop page this is
    // rendered from already names its merchant, so the sign-in should carry it
    // too - /kettle/signin rather than /signin, which would fall back to
    // whichever shop was visited last.
    return <Link to={pathFor(getConnection(), "signin")}>Sign in</Link>;
  }

  return (
    <div className="acctmenu" ref={box}>
      <button
        className="acctmenu-name"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen(!open)}
      >
        {account.display_name}
        <span className="acctmenu-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <div className="acctmenu-drop" role="menu">
          <span className="acctmenu-who">
            Signed in as {account.username}
          </span>

          <button
            className="acctmenu-item"
            role="menuitem"
            data-testid="memory-open"
            onClick={async () => setMemory(await rememberedFacts(getConnection()))}
          >
            What I remember about you
          </button>

          {memory && (
            <div className="acctmenu-memory" data-testid="memory-panel">
              {!memory.ok ? (
                <p>I couldn't load that just now.</p>
              ) : memory.facts.length === 0 ? (
                <p>Nothing yet. I only keep what you tell me you prefer.</p>
              ) : (
                <>
                  <ul>
                    {memory.facts.map((f) => (
                      <li key={f.key}>
                        <span>{f.value}</span>
                        <button
                          type="button"
                          data-testid="memory-forget"
                          onClick={async () => {
                            if (await forgetFacts(getConnection(), f.key)) setMemory(await rememberedFacts(getConnection()));
                          }}
                        >
                          Forget
                        </button>
                      </li>
                    ))}
                  </ul>
                  <button
                    type="button"
                    data-testid="memory-forget-all"
                    onClick={async () => {
                      if (await forgetFacts(getConnection())) setMemory(await rememberedFacts(getConnection()));
                    }}
                  >
                    Forget everything
                  </button>
                </>
              )}
            </div>
          )}

          <button
            className="acctmenu-item"
            role="menuitem"
            onClick={async () => {
              setOpen(false);
              await signOut();
              onAccount(null);
              // Reloaded rather than reset in place.
              //
              // The cart, the conversation and the session id are all derived
              // from who is signed in, and unpicking each by hand is how you end
              // up with one of them stale - which is the mistake behind three
              // bugs this week. A reload derives them all again from nothing.
              window.location.assign(pathFor(getConnection()));
            }}
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}