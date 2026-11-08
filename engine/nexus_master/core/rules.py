"""Configurable business rules (NIGUN-134 module A, section 9): small named predicates
reading from core.config instead of scattered literal thresholds, so a one-person
business can tune behavior via settings without a code change. Each rule is a thin,
testable function -- not a rule-engine DSL, which this project does not need.
"""
from . import config


def is_hot_lead(conn, tenant_id, lead):
    return lead.get("score_total", 0) >= config.get_int(conn, tenant_id, "hot_lead_score")


def should_escalate_stale_lead(conn, tenant_id, lead, now=None):
    """A 'new' lead older than stale_lead_hours (default 48) with no status change yet."""
    import time
    now = now if now is not None else time.time()
    hours = config.get_float(conn, tenant_id, "stale_lead_hours", 48)
    return lead.get("status") == "new" and (now - lead.get("created", now)) >= hours * 3600


def within_daily_send_cap(conn, tenant_id, sends_today):
    return sends_today < config.get_int(conn, tenant_id, "max_sends_per_day")
