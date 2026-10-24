/**
 * Which baskets must not be paid right now, in the browser.
 *
 * A basket is HELD when the engine cannot say whether a payment on it went through (`UNKNOWN_OUTCOME` in the payment ledger),
 * or one is still going through (`in_flight`). Paying it again could charge the shopper twice, so every payment control for a
 * held basket is disabled and says why. The engine refuses the same payments server-side whatever this file does - this is
 * the shopper's view of that guard, never the guard itself.
 *
 * The state here is deliberately pessimistic and short-lived:
 *
 *  - It is SEEDED from the server (`Cart.payment_state`) every time a cart is read, so a page that was stale - an old tab, an
 *    old chat turn still showing card buttons - learns the basket is held as soon as it reads the cart.
 *  - It is SET by a payment reply that says so (`payment.unknown` / `payment.in_flight`, or a 409 `PAYMENT_OUTCOME_UNKNOWN` /
 *    `CHECKOUT_IN_PROGRESS`).
 *  - It fails CLOSED. A payment request that dies without a clean answer (a network error, a timeout, a 5xx, an unreadable
 *    reply) may still have been processed, so the basket is "checking" - controls disabled - until the server's own state has
 *    been read. If that read also fails the basket is "unreadable" and stays disabled, with a way to check again.
 *  - It is CLEARED only by reading the server's state and finding the basket free, never by a timer or a guess.
 */

import { useSyncExternalStore } from "react";
import { EngineError, api, getConnection, type Cart } from "./api";

/** unknown/in_flight: the server says so. checking: a payment failed ambiguously and we are asking. unreadable: we asked and could not find out. */
export type HoldKind = "unknown" | "in_flight" | "checking" | "unreadable";

export type PaymentState = "paid" | "in_flight" | "unknown" | null | undefined;

/** How often a held basket re-reads the server to see whether it has been resolved. */
export const HOLD_RECHECK_MS = 15_000;

let holds: Record<string, HoldKind> = {};

/** Holds are per merchant AND basket: cart ids repeat across merchants, and a hold on one must never lift or set another's. */
export function holdKey(cartId: string): string {
  return `${getConnection()}:${cartId}`;
}

const listeners = new Set<() => void>();

function emit() {
  holds = { ...holds };
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

/** All current holds, keyed by cart id. Re-renders when any change. */
export function useHolds(): Record<string, HoldKind> {
  return useSyncExternalStore(subscribe, () => holds);
}

/** The hold on one basket, or null when it is free (or unknown to this page). */
export function useHold(cartId: string | null | undefined): HoldKind | null {
  const all = useHolds();
  return cartId ? (all[holdKey(cartId)] ?? null) : null;
}

export function holdOf(cartId: string | null | undefined): HoldKind | null {
  return cartId ? (holds[holdKey(cartId)] ?? null) : null;
}

export function markHeld(cartId: string, kind: HoldKind): void {
  const key = holdKey(cartId);
  if (holds[key] === kind) return;
  holds[key] = kind;
  emit();
}

export function clearHold(cartId: string): void {
  const key = holdKey(cartId);
  if (!(key in holds)) return;
  delete holds[key];
  emit();
}

/**
 * Make the browser's view match a cart the server just returned: held while it says held.
 *
 * `onlyRaise` is for a cart object the page has been holding in state, which can be OLDER than a hold learned since (a stale
 * "free" cart must never silently lift a hold, or the screen fails open). A fresh read made for the purpose - `recheck` - lifts
 * the hold when the server says free or paid; nothing else does.
 */
export function applyCart(
  cart: Pick<Cart, "cart_id" | "payment_state"> | null | undefined,
  onlyRaise = false,
): void {
  if (!cart) return;
  const state: unknown = cart.payment_state;
  if (state === "unknown" || state === "in_flight") {
    markHeld(cart.cart_id, state);
  } else if (state === null || state === undefined || state === "paid") {
    // Only an explicit "free" (or "paid", which the cart flow retires) may lift a hold.
    if (!onlyRaise) clearHold(cart.cart_id);
  } else {
    // A value this page does not recognise is not "free": fail closed rather than guess.
    markHeld(cart.cart_id, "unknown");
  }
}

/** The hold a payment error implies, when the engine said so explicitly. */
export function holdFromError(err: unknown): HoldKind | null {
  if (!(err instanceof EngineError)) return null;
  if (err.code === "PAYMENT_OUTCOME_UNKNOWN" || err.code === "CART_PAYMENT_PENDING") return "unknown";
  if (err.code === "CHECKOUT_IN_PROGRESS") return "in_flight";
  return null;
}

/** What a payment REPLY (chat) says about the basket. */
export function holdFromPayment(p: { unknown?: boolean; in_flight?: boolean } | null | undefined): HoldKind | null {
  if (p?.unknown) return "unknown";
  if (p?.in_flight) return "in_flight";
  return null;
}

/**
 * A payment request failed without a clean answer: the basket may have been charged. Disable payment, then ask the server.
 *
 * Resolves once the question has been answered either way; never throws. The server's state decides: held stays held, free
 * is released, and if the server cannot be reached the basket stays disabled as "unreadable".
 */
export async function recheck(cartId: string): Promise<void> {
  const current = holdOf(cartId);
  markHeld(cartId, current === "unknown" || current === "in_flight" ? current : "checking");
  try {
    const cart = await api.getCart(cartId);
    applyCart(cart);
  } catch {
    // Could not find out. Fail closed: the controls stay disabled until a read succeeds.
    const now = holdOf(cartId);
    if (now === "checking" || now === null) markHeld(cartId, "unreadable");
  }
}

/** A payment failed: record what the error says, or fail closed and ask the server. */
export async function afterPaymentFailure(cartId: string, err: unknown): Promise<void> {
  const explicit = holdFromError(err);
  if (explicit) {
    markHeld(cartId, explicit);
    return;
  }
  // A clean, definite refusal that proves nothing was charged (an empty basket) still goes through the server's answer
  // rather than the browser's guess: the server says free, the hold is lifted.
  await recheck(cartId);
}

/** Shopper-facing copy for each hold. Fixed text: nothing from the engine or the platform is shown. */
export const HOLD_COPY: Record<HoldKind, { label: string; note: string }> = {
  unknown: {
    label: "Payment on hold",
    note: "We couldn't confirm whether your last payment went through, so this basket is on hold to avoid charging you twice. Please don't pay again. If you were charged, the shop can confirm it from their records.",
  },
  in_flight: {
    label: "Payment in progress",
    note: "A payment on this basket is going through now. Give it a moment rather than paying again.",
  },
  checking: {
    label: "Checking payment status…",
    note: "Your last payment didn't finish cleanly. We're checking what happened before anything else can be charged.",
  },
  unreadable: {
    label: "Payment on hold",
    note: "We couldn't check your payment status just now, so payment stays off to avoid charging you twice. Check again in a moment.",
  },
};

/** Test hook: forget every hold. */
export function resetHoldsForTests(): void {
  holds = {};
  emit();
}
