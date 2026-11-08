"""core/ -- NIGUN-134 module A (control center): settings, event bus, notifications,
task queue, business rules. Also the home of registry.py, the plugin-discovery
mechanism every subpackage (this one included) rides on.

decision_log() lives here (not audit.py, which predates the NIGUN-134 module split)
as module A's "audit + reason" requirement (section 12): a thin wrapper that makes a
reason mandatory, rather than a new storage mechanism -- audit already has tamper
detection (audit.verify) and is not duplicated here.
"""
from . import events

MIGRATIONS = [
    # 0001 -- events table (core/events.py) and tasks queue (core/tasks.py).
    """
    CREATE TABLE core_events(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, ts REAL,
        type TEXT NOT NULL, data TEXT);
    CREATE INDEX idx_core_events_tenant_type ON core_events(tenant_id, type);
    CREATE TABLE core_tasks(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, kind TEXT NOT NULL,
        payload TEXT, priority INTEGER DEFAULT 0, status TEXT DEFAULT 'pending',
        attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 5, run_at REAL NOT NULL,
        created REAL NOT NULL, last_error TEXT);
    CREATE INDEX idx_core_tasks_due ON core_tasks(tenant_id, status, run_at);
    """,
]


def decision_log(conn, tenant_id, actor, action, reason, data=None):
    """audit.log, with a mandatory reason -- section 12's "decision log" is audit plus the
    why, not a second storage mechanism to keep in sync with the first."""
    from .. import audit
    payload = dict(data or {})
    payload["reason"] = reason
    audit.log(conn, tenant_id, actor, action, payload)
    events.emit(conn, tenant_id, f"decision.{action}", payload)
