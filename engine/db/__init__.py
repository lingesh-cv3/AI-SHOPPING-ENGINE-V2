"""Persistence. Cases, approvals, outcomes.

Tenant isolation is structural: every repository function takes a connection_id and
there is no unfiltered variant, so a route cannot read another merchant's data by
forgetting a filter.

SQLite by default so no install is needed; Postgres by changing DATABASE_URL.
Nothing in the schema uses a SQLite-only feature.
"""
from . import idempotency
from . import keys
from .models import ApiKey, Approval, Base, Case, ExecutionAttempt, MerchantPolicy, Outcome
from .repository import (
    decide_approval,
    decided_across,
    expire_approvals,
    handovers_across,
    mark_handled,
    ops_stats,
    pending_across,
    get_case,
    list_cases,
    load_policies,
    merchant_report,
    pending_approvals,
    record_case,
    record_outcome,
    save_policy,
    stats,
)
from .session import create_schema, database_url, dispose, session_scope

# Last, because it needs session_scope and Shopper from this package - and
# importing it earlier asked a half-built module for both.
from . import shoppers
from . import shopper_sessions

from . import shopper_carts

__all__ = [
    "ApiKey",
    "Approval",
    "Base",
    "Case",
    "Outcome",
    "create_schema",
    "keys",
    "shoppers",
    "database_url",
    "decide_approval",
    "decided_across",
    "expire_approvals",
    "handovers_across",
    "mark_handled",
    "ops_stats",
    "pending_across",
    "dispose",
    "get_case",
    "list_cases",
    "pending_approvals",
    "record_case",
    "record_outcome",
    "session_scope",
    "stats","MerchantPolicy", "load_policies", "save_policy","merchant_report"
]