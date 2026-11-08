"""Security & data governance (NIGUN-134 module K, sections 117-125).

117 (secure auth), 118 (RBAC, as of B's round-2 api.py: NEXUS_API_KEY=admin,
NEXUS_VIEWER_KEY=read-only -- non-GET rejected generically for every route, including
every one this package registers, with no code change needed here), 120 (secrets via
env) and 123 (rate limiting) all already exist elsewhere in this codebase -- this
package documents and tests that they hold, it does not re-implement them. roles.py's
finer per-action permission model (admin/sales/viewer, not just admin/viewer) is still
not reachable from a real request: api.py has no notion of "which principal" beyond
"which of the two keys matched", so that model stays a ready-but-unused extension point,
not something currently enforced -- 121/122 are the same kind of ready-but-unwired
utility. See each submodule's own docstring for exactly what is, and is not, enforced
today.
"""
import time

from . import retention, roles, suppression, validation
from .headers import SECURITY_HEADERS, with_security_headers
from .retention import delete_lead, export_lead, retention_candidates
from .roles import has_permission
from .suppression import is_suppressed, suppress

MIGRATIONS = [
    # 0001 -- section 124 (suppression) and 118 (role records, not yet enforced).
    """
    CREATE TABLE suppression(tenant_id INTEGER NOT NULL, phone TEXT NOT NULL, reason TEXT,
        ts REAL NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(tenant_id, phone));
    CREATE TABLE principal_roles(tenant_id INTEGER NOT NULL, principal TEXT NOT NULL,
        role TEXT NOT NULL, PRIMARY KEY(tenant_id, principal));
    """,
]


# ---- section 125 (automatic half): retention sweep via core.tasks (module A) -----

def register_task_handlers():
    """Mirrors discovery.register_task_handlers -- called at import time, bottom of file."""
    from ..core import tasks as core_tasks
    core_tasks.register_handler("privacy.retention_sweep", _run_retention_sweep)


def _run_retention_sweep(conn, tenant_id, payload):
    days = payload.get("days", 365)
    deleted = [c["id"] for c in retention_candidates(conn, tenant_id, days=days)
              if delete_lead(conn, tenant_id, c["id"], actor="retention_sweep")]
    return {"deleted": deleted}


def schedule_retention_sweep(conn, tenant_id, days=365, run_at=None):
    """Enqueues a real core_tasks row; the actual sweep only runs when something calls
    `python -m nexus_master tick` (or a cron pointed at it) -- same as discovery.schedule_run."""
    from ..core import tasks as core_tasks
    return core_tasks.enqueue(conn, tenant_id, "privacy.retention_sweep", {"days": days}, run_at=run_at)


def routes(app):
    def _export(conn, tid, body, query):
        lead_id = int(query.get("lead_id", [0])[0])
        try:
            return 200, export_lead(conn, tid, lead_id)
        except ValueError:
            return 404, {"error": "ליד לא נמצא"}

    def _delete(conn, tid, body, query):
        ok = delete_lead(conn, tid, int(body["lead_id"]), actor="api")
        return (200, {"deleted": True}) if ok else (404, {"error": "ליד לא נמצא"})

    def _suppress(conn, tid, body, query):
        p = suppress(conn, tid, body["phone"], body.get("reason", ""), actor="api")
        return 200, {"phone": p}

    def _retention_candidates(conn, tid, body, query):
        return 200, retention_candidates(conn, tid)

    def _schedule_retention(conn, tid, body, query):
        task_id = schedule_retention_sweep(conn, tid, days=body.get("days", 365), run_at=body.get("run_at"))
        return 201, {"task_id": task_id}

    app.add_route("GET", "/privacy/export", _export)
    app.add_route("POST", "/privacy/delete", _delete)
    app.add_route("POST", "/privacy/suppress", _suppress)
    app.add_route("GET", "/privacy/retention-candidates", _retention_candidates)
    app.add_route("POST", "/privacy/schedule-retention", _schedule_retention)


def _cmd_export(conn, tid, m):
    try:
        return {"ok": True, "data": export_lead(conn, tid, int(m.group(1)))}
    except ValueError as e:
        return {"ok": False, "message": str(e)}


def _cmd_delete(conn, tid, m):
    ok = delete_lead(conn, tid, int(m.group(1)), actor="command")
    return {"ok": ok, "message": "נמחק" if ok else "ליד לא נמצא"}


def _cmd_suppress(conn, tid, m):
    try:
        p = suppress(conn, tid, m.group(1), m.group(2) or "", actor="command")
        return {"ok": True, "message": f"מספר {p} נוסף לרשימת הדיכוי"}
    except ValueError as e:
        return {"ok": False, "message": str(e)}


def _cmd_schedule_retention(conn, tid, m):
    task_id = schedule_retention_sweep(conn, tid)
    return {"ok": True, "message": f"משימת-ניקוי {task_id} נכנסה לתור"}


COMMANDS = {
    r"(?:ייצא ליד|export lead)\s+(\d+)$": _cmd_export,
    r"(?:מחק ליד|delete lead)\s+(\d+)$": _cmd_delete,
    r"(?:דכא מספר|suppress)\s+(\S+)(?:\s+(.*))?$": _cmd_suppress,
    r"(?:תזמן ניקוי|schedule retention)$": _cmd_schedule_retention,
}


register_task_handlers()
