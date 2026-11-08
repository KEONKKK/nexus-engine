"""Structured monitoring, error reporting and operational alerts (NIGUN-134 module L
section 132). Errors are logged through the same core_events bus as everything else
(module A) rather than inventing a second logging mechanism; active_alerts() is the
thing a dashboard or /health?deep=1 polls, not a background daemon (this project has
none)."""
from . import config, events, tasks
from .. import audit


def log_error(conn, tenant_id, component, message, data=None):
    payload = dict(data or {})
    payload.update(component=component, message=message)
    events.emit(conn, tenant_id, "error", payload)


def recent_errors(conn, tenant_id, limit=20):
    return events.recent(conn, tenant_id, "error", limit=limit)


def active_alerts(conn, tenant_id):
    """Named, explained conditions -- never a bare boolean. Empty list = healthy."""
    alerts = []
    ok, bad_id = audit.verify(conn, tenant_id)
    if not ok:
        alerts.append({"level": "critical", "code": "audit_tamper",
                       "message": f"שרשרת ה-audit נפגמה בפריט {bad_id}"})
    depth = tasks.queue_depth(conn, tenant_id)
    max_depth = config.get_int(conn, tenant_id, "task_queue_alert_depth", 100)
    if depth >= max_depth:
        alerts.append({"level": "warning", "code": "task_queue_backlog",
                       "message": f"{depth} משימות ממתינות (סף: {max_depth})"})
    failed = conn.execute(
        "SELECT COUNT(*) FROM core_tasks WHERE tenant_id=? AND status='failed'", (tenant_id,)).fetchone()[0]
    if failed:
        alerts.append({"level": "warning", "code": "failed_tasks",
                       "message": f"{failed} משימות נכשלו לצמיתות (מעל max_attempts)"})
    recent_err_count = len(recent_errors(conn, tenant_id, limit=1000))
    if recent_err_count >= config.get_int(conn, tenant_id, "error_alert_count", 20):
        alerts.append({"level": "warning", "code": "high_error_rate",
                       "message": f"{recent_err_count} שגיאות רשומות"})
    return alerts
