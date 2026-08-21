"""Persistence. Cases, approvals, outcomes.

Tenant isolation is structural: every repository function takes a connection_id and
there is no unfiltered variant, so a route cannot read another merchant's data by
forgetting a filter.

SQLite by default so no install is needed; Postgres by changing DATABASE_URL.
Nothing in the schema uses a SQLite-only feature.
"""
from . import idempotency
from .models import Approval, Base, Case, ExecutionAttempt, Outcome
from .models import Approval, Base, Case, ExecutionAttempt, MerchantPolicy, Outcome
from .repository import (
    decide_approval,
    get_case,
    list_cases,
    load_policies,
    pending_approvals,
    record_case,
    record_outcome,
    save_policy,
    stats,
)
from .session import create_schema, database_url, dispose, session_scope

__all__ = [
    "Approval",
    "Base",
    "Case",
    "Outcome",
    "create_schema",
    "database_url",
    "decide_approval",
    "dispose",
    "get_case",
    "list_cases",
    "pending_approvals",
    "record_case",
    "record_outcome",
    "session_scope",
    "stats","MerchantPolicy", "load_policies", "save_policy"
]