"""Application wiring.

One place where the engine's pieces are assembled and handed to the API layer.

Two merchant connections are registered here, deliberately unlike each other:

- Northfield runs on a REST platform with integer-paise money, stock counts, errors
  returned as HTTP 200, and no payment recovery at all.
- Kettle & Bloom runs on GraphQL with decimal-string money, boolean-only stock,
  proper HTTP status codes, and working payment recovery.

Nothing below this file knows either of those things. The engine holds two
StandardCommerceInterface instances and cannot tell them apart. That is the claim
the second connection exists to test - and the difference in what happens to a
declined card on each is the proof.

In production these come from the database when a merchant authorizes CV3 against
their platform, and their credentials are loaded from a vault rather than an
environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from adapters.kettle import KettleAdapter
from adapters.sample import SampleMerchantAdapter
from engine.decision import CapabilityRegistry, DecisionEngine
from engine.execution import ExecutionService
from engine.reasoning import ReasoningService
from engine.risk import PolicyStore, RiskGate

# Read .env before anything checks for a key. Loaded here rather than in the
# reasoning service so that module stays free of environment side effects and
# testable without a filesystem.
load_dotenv()

NORTHFIELD_ID = "conn_demo"
NORTHFIELD_NAME = "Northfield Running Co."
NORTHFIELD_URL = os.getenv("SAMPLE_MERCHANT_URL", "http://127.0.0.1:8001")

KETTLE_ID = "conn_kettle"
KETTLE_NAME = "Kettle & Bloom Coffee"
KETTLE_URL = os.getenv("KETTLE_MERCHANT_URL", "http://127.0.0.1:8002")

#: Display names for the consoles. The engine itself never reads this - it works in
#: connection ids, because a merchant's trading name is not an identifier.
MERCHANT_NAMES: dict[str, str] = {
    NORTHFIELD_ID: NORTHFIELD_NAME,
    KETTLE_ID: KETTLE_NAME,
}

# Kept for backwards compatibility with earlier code that imported it directly.
DEV_CONNECTION_ID = NORTHFIELD_ID
DEV_MERCHANT_NAME = NORTHFIELD_NAME


@dataclass
class Engine:
    """Everything the API needs, assembled once at startup."""

    registry: CapabilityRegistry
    policies: PolicyStore
    decision: DecisionEngine
    gate: RiskGate
    reasoning: ReasoningService
    execution: ExecutionService

    async def close(self) -> None:
        for connection_id in self.registry.connection_ids():
            adapter = self.registry.adapter_for(connection_id)
            close = getattr(adapter, "close", None)
            if close is not None:
                await close()


def build_engine() -> Engine:
    """Assemble the engine and register both development connections."""
    registry = CapabilityRegistry()
    policies = PolicyStore()

    registry.register(
        SampleMerchantAdapter(
            connection_id=NORTHFIELD_ID,
            base_url=NORTHFIELD_URL,
            currency="INR",
            storefront_url="http://localhost:5173",
        )
    )
    registry.register(
        KettleAdapter(
            connection_id=KETTLE_ID,
            base_url=KETTLE_URL,
            storefront_url="http://localhost:5173",
        )
    )

    return Engine(
        registry=registry,
        policies=policies,
        decision=DecisionEngine(registry, policies),
        gate=RiskGate(policies),
        # Builds disabled if no key is configured, rather than failing. The engine
        # runs on rule-based proposals without a model.
        reasoning=ReasoningService.from_env(),
        execution=ExecutionService(registry),
    )


#: Module-level singleton, created on import. FastAPI routes read this.
engine = build_engine()