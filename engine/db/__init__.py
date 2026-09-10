"""Persistence. Cases, approvals, outcomes.

Tenant isolation is structural: every repository function takes a connection_id and
there is no unfiltered variant, so a route cannot read another merchant's data by
forgetting a filter.

SQLite by default so no install is needed; Postgres by changing DATABASE_URL.
Nothing in the schema uses a SQLite-only feature.
"""
from . import idempotency
from . import keys
from .models import (
    ApiKey,
    Approval,
    Base,
    Case,
    ExecutionAttempt,
    FunnelEvent,
    MerchantPolicy,
    Outcome,
    OrderLine,
    ResourceOwner,
    SentMail,
    SessionHoldout,
)
from .repository import (
    checkout_conversion,
    decide_approval,
    decided_across,
    expire_approvals,
    friction_summary_across,
    handovers_across,
    holdout_status,
    mark_handled,
    ops_stats,
    pending_across,
    get_case,
    list_cases,
    load_policies,
    merchant_report,
    pending_approvals,
    product_performance,
    record_case,
    record_funnel_event,
    record_outcome,
    record_sent_mail,
    resolve_unresolved_payment_cases_for_cart,
    sales_period_comparison,
    save_policy,
    sent_mail_for_order,
    stats,
    total_sales,
    unmet_demand,
)
from .session import create_schema, database_url, dispose, session_scope

# Last, because it needs session_scope and Shopper from this package - and
# importing it earlier asked a half-built module for both.
from . import shoppers
from . import shopper_sessions

from . import shopper_carts

# Who a cart, a conversation or an order belongs to. Imported after the models
# for the same reason as the two above: it asks this package for session_scope.
from . import owners

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
    "stats",
    "MerchantPolicy",
    "load_policies",
    "save_policy",
    "merchant_report",
    "unmet_demand",
    "owners",
    "ResourceOwner",
    "SentMail",
    "record_sent_mail",
    "sent_mail_for_order",
    "SessionHoldout",
    "holdout_status",
    "resolve_unresolved_payment_cases_for_cart",
    "friction_summary_across",
    "total_sales",
    "OrderLine",
    "product_performance",
    "sales_period_comparison",
    "checkout_conversion",
    "FunnelEvent",
    "record_funnel_event",
]