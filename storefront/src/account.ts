/** Signing a shopper in, and knowing who they are.
 *
 *  Separate from api.ts because these are the only calls that carry a cookie
 *  rather than a key, and that difference is worth being able to see.
 */

const ENGINE = "";

export interface Account {
  username: string;
  display_name: string;
  /** The conversation that belongs to them, rather than to this tab. */
  session_id: string;
}

export class AccountError extends Error {}

function publishableKey(connectionId: string): string {
  const keys: Record<string, string | undefined> = {
    conn_demo: import.meta.env.VITE_CV3_PK_CONN_DEMO,
    conn_kettle: import.meta.env.VITE_CV3_PK_CONN_KETTLE,
  };
  return keys[connectionId] || "";
}

async function post<T>(path: string, connectionId: string, body: object): Promise<T> {
  const res = await fetch(`${ENGINE}${path}`, {
    method: "POST",
    // Sends the session cookie. The cookie is httpOnly so nothing here can read
    // it - the browser attaches it and that is the point.
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${publishableKey(connectionId)}`,
    },
    body: JSON.stringify(body),
  });

  const data = await res.json().catch(() => null);

  if (!res.ok) {
    // The engine's own message, because these are all things a person can act on:
    // a taken username, a short password, too many attempts. Replacing them with
    // something generic would be unhelpful rather than secure.
    // FastAPI returns a string for our own errors and a list of objects for
    // validation failures, and dropping the second into a message renders
    // [object Object]. A shopper who typed a short password deserves to be told
    // that rather than shown our serialisation.
    let message = "That did not work. Try again.";

    if (typeof data?.detail === "string") {
      message = data.detail;
    } else if (Array.isArray(data?.detail) && data.detail.length > 0) {
      const first = data.detail[0];
      const field = String(first?.loc?.at(-1) ?? "");
      message =
        field === "password"
          ? "A password needs at least eight characters."
          : field === "username"
            ? "A username needs at least three characters."
            : String(first?.msg ?? message);
    }

    throw new AccountError(message);
  }

  return data as T;
}

/** Who is signed in, if anybody.
 *
 *  A guest is not an error, so this returns null rather than throwing. Most
 *  shoppers will never sign in, and treating the common case as a failure would be
 *  the wrong shape.
 */
export async function whoAmI(connectionId: string): Promise<Account | null> {
  try {
    const res = await fetch(
      `${ENGINE}/api/account/me?connection_id=${connectionId}`,
      {
        credentials: "same-origin",
        headers: { Authorization: `Bearer ${publishableKey(connectionId)}` },
      },
    );
    if (!res.ok) return null;
    const data = await res.json();
    return data?.signed_in ? (data as Account) : null;
  } catch {
    return null;
  }
}

export function signIn(
  connectionId: string,
  username: string,
  password: string,
  guestSession: string,
): Promise<Account> {
  return post<Account>("/api/account/signin", connectionId, {
    connection_id: connectionId,
    username,
    password,
    // Handed over so the conversation they are in the middle of comes with them.
    guest_session: guestSession,
  });
}

export function signUp(
  connectionId: string,
  username: string,
  password: string,
  guestSession: string,
): Promise<Account> {
  return post<Account>("/api/account/signup", connectionId, {
    connection_id: connectionId,
    username,
    password,
    guest_session: guestSession,
  });
}

export async function signOut(connectionId: string): Promise<void> {
  await fetch(`${ENGINE}/api/account/signout`, {
    method: "POST",
    credentials: "same-origin",
  }).catch(() => {
    // A failed sign-out still clears the local state. Leaving somebody signed in
    // because the network hiccuped is the worse outcome, and the session expires
    // regardless.
  });
}