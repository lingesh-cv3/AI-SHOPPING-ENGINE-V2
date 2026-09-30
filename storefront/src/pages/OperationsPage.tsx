import { OperationsShell } from "../OperationsShell";

/**
 * CV3 Operations, on its own page: one console across every merchant.
 *
 * A thin wrapper on purpose - the shell owns its sign-in, its CV3 theme and its
 * navigation. It is not themed as a merchant and is not addressed through one: the
 * old /:merchant/operations route is gone (see main.tsx), because a cross-merchant
 * page has no merchant in its address.
 */
export function OperationsPage() {
  return <OperationsShell />;
}
