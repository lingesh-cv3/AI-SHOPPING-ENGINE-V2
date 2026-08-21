"""Capability registry.

The Decision Engine needs to know what a connection can do before it selects an
action. Asking the adapter on every decision would mean a network round trip
inside every shopper turn, so declarations are cached here.

The cache is deliberately not authoritative. A capability can be revoked between
a decision and its execution - a token expires, a scope is withdrawn, a merchant
downgrades their plan - so the adapter re-checks at execution time. This registry
exists to make decisions fast, not to be the final word.

That split matters: a stale cache can only ever cause a *wasted* decision, never
an unsafe execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from shared.interfaces import StandardCommerceInterface
from shared.models import CapabilitySet

#: How long a declaration is trusted before it is re-fetched. Short enough that a
#: revoked capability is noticed quickly, long enough that we are not calling
#: getCapabilities on every shopper message.
DEFAULT_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class _Entry:
    capabilities: CapabilitySet
    fetched_at: float


class CapabilityRegistry:
    """Per-connection capability cache.

    Holds adapters keyed by connection so the Decision Engine never has to know
    which platform it is talking to - it asks the registry for a CapabilitySet
    and gets one, whatever is underneath.
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._adapters: dict[str, StandardCommerceInterface] = {}
        self._cache: dict[str, _Entry] = {}
        self._ttl = ttl_seconds

    def register(self, adapter: StandardCommerceInterface) -> None:
        """Attach an adapter for a connection. Does not fetch capabilities yet."""
        self._adapters[adapter.connection_id] = adapter

    def unregister(self, connection_id: str) -> None:
        self._adapters.pop(connection_id, None)
        self._cache.pop(connection_id, None)

    def adapter_for(self, connection_id: str) -> StandardCommerceInterface | None:
        return self._adapters.get(connection_id)

    async def get(
        self, connection_id: str, *, force: bool = False
    ) -> CapabilitySet | None:
        """Return a connection's capabilities, fetching if stale or absent.

        Returns None for an unknown connection rather than raising. The Decision
        Engine treats an absent capability set as "nothing is supported", which
        escalates to a human - the safe direction when we do not know what a
        connection can do.
        """
        adapter = self._adapters.get(connection_id)
        if adapter is None:
            return None

        entry = self._cache.get(connection_id)
        if entry and not force and (time.monotonic() - entry.fetched_at) < self._ttl:
            return entry.capabilities

        capabilities = await adapter.get_capabilities()
        self._cache[connection_id] = _Entry(capabilities, time.monotonic())
        return capabilities

    def peek(self, connection_id: str) -> CapabilitySet | None:
        """Read the cache without fetching. For consoles and health views."""
        entry = self._cache.get(connection_id)
        return entry.capabilities if entry else None

    def invalidate(self, connection_id: str) -> None:
        """Drop a cached declaration.

        Called when a connection is reconfigured, reauthorized, or when an
        execution fails with CAPABILITY_UNSUPPORTED - that failure means the
        cache was wrong and should not be trusted again until refetched.
        """
        self._cache.pop(connection_id, None)

    def connection_ids(self) -> list[str]:
        return list(self._adapters)