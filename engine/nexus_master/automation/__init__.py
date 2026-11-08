"""Automation engine (NIGUN-134 module G, sections 73-84). Owned by window A.

A workflow is {trigger_type: event|time, trigger_value, condition, action_type, action_value}.
Section 73 ("ויזואלי") is the admin UI listing/editing these rows (src/admin/index.html) - this
file is the data model and the engine, not a visual builder.

Reuses, rather than re-invents, the core/ infrastructure NIGUN-134 module A already built:
  * core.events   - event-triggered workflows (74) subscribe to the wildcard "*" topic once
                     (see routes() below) and re-dispatch by event_type, instead of each
                     workflow needing its own subscription wired at creation time.
  * core.tasks    - time-triggered workflows (75) and retry (82) run inside a SELF-RE-
                     ENQUEUING task (kind "automation.sweep"): each run, if it succeeds,
                     schedules its own next run. `python -m nexus_master tick` (one batch,
                     not a daemon - core/tasks.py's own docstring) is still what actually
                     advances time; a human/cron decides when that runs, same as every
                     other task in this project.
  * core.rules    - stale-lead detection (78) reuses rules.should_escalate_stale_lead
                     verbatim instead of a second threshold.
  * core.notify   - escalation (80) sends through the one notification entrypoint.
  * core.config   - pause/resume/emergency-stop (84) is a setting, not a new table.

Idempotency (83): every attempted action writes exactly one workflow_runs row keyed by a
UNIQUE (tenant_id, idempotency_key) - a second attempt with the same key is a no-op read,
never a second action. Retry (82) re-attempts only rows already marked 'failed', up to
MAX_RETRY_ATTEMPTS, updating that same row rather than inserting a new one.
"""
import json
import time

from .. import audit, guardian, leads
from ..core import config, events, notify, rules, tasks

TRIGGER_TYPES = ("event", "time")
ACTION_TYPES = ("assign", "reminder", "set_status", "escalate", "notify")
MAX_RETRY_ATTEMPTS = 3
SWEEP_INTERVAL_SECONDS = 300  # how far apart two automation.sweep tasks schedule each other

MIGRATIONS = [
    """
    CREATE TABLE workflows(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, name TEXT NOT NULL,
        trigger_type TEXT NOT NULL, trigger_value TEXT NOT NULL, condition_json TEXT,
        action_type TEXT NOT NULL, action_value TEXT, enabled INTEGER DEFAULT 1, created REAL NOT NULL);
    CREATE INDEX idx_automation_workflows_trigger ON workflows(tenant_id, trigger_type, trigger_value);

    CREATE TABLE workflow_runs(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        workflow_id INTEGER NOT NULL, lead_id INTEGER, idempotency_key TEXT NOT NULL,
        status TEXT NOT NULL, attempt INTEGER DEFAULT 1, error TEXT, created REAL NOT NULL,
        UNIQUE(tenant_id, idempotency_key));
    CREATE INDEX idx_automation_runs_workflow ON workflow_runs(tenant_id, workflow_id);
    """,
]


def _now():
    return time.time()


# ---------------------------------------------------------------- 73/84: workflows CRUD

def create_workflow(conn, tenant_id, *, name, trigger_type, trigger_value, action_type,
                     action_value=None, condition=None, actor="user"):
    if trigger_type not in TRIGGER_TYPES:
        raise ValueError(f"סוג טריגר לא מוכר: {trigger_type}")
    if action_type not in ACTION_TYPES:
        raise ValueError(f"סוג פעולה לא מוכר: {action_type}")
    cur = conn.execute(
        "INSERT INTO workflows(tenant_id, name, trigger_type, trigger_value, condition_json,"
        " action_type, action_value, enabled, created) VALUES (?,?,?,?,?,?,?,1,?)",
        (tenant_id, name, trigger_type, trigger_value,
         json.dumps(condition, ensure_ascii=False) if condition else None,
         action_type, action_value, _now()))
    conn.commit()
    wid = cur.lastrowid
    audit.log(conn, tenant_id, actor, "automation.workflow.create", {"workflow": wid, "name": name})
    return wid


def list_workflows(conn, tenant_id, enabled_only=False):
    q = "SELECT * FROM workflows WHERE tenant_id=?"
    if enabled_only:
        q += " AND enabled=1"
    q += " ORDER BY id"
    return [dict(r) for r in conn.execute(q, (tenant_id,))]


def set_enabled(conn, tenant_id, workflow_id, enabled, actor="user"):
    """Per-workflow pause/resume (84) - the OTHER half is the global kill switch below."""
    n = conn.execute("UPDATE workflows SET enabled=? WHERE tenant_id=? AND id=?",
                     (1 if enabled else 0, tenant_id, workflow_id)).rowcount
    conn.commit()
    if n:
        audit.log(conn, tenant_id, actor, "automation.workflow.enabled", {"workflow": workflow_id, "enabled": enabled})
    return bool(n)


def is_globally_paused(conn, tenant_id):
    return str(config.get(conn, tenant_id, "automation_paused", "0")) == "1"


def set_global_pause(conn, tenant_id, paused, actor="user"):
    """84's emergency stop: ALL workflows, event- and time-triggered alike, stop acting
    (trigger_event/run_time_based both check this first) without touching any workflow's
    own `enabled` flag - resuming restores exactly what was running before."""
    config.set(conn, tenant_id, "automation_paused", "1" if paused else "0")
    audit.log(conn, tenant_id, actor, "automation.global_pause", {"paused": paused})


# ---------------------------------------------------------------- condition + action

def _condition_matches(lead, condition):
    if not condition:
        return True
    if "status" in condition and lead.get("status") != condition["status"]:
        return False
    if "min_score" in condition and (lead.get("score_total") or 0) < condition["min_score"]:
        return False
    if "service" in condition and lead.get("service") != condition["service"]:
        return False
    return True


def apply_action(conn, tenant_id, workflow, lead_id, actor="automation"):
    """Dispatches by action_type. May raise - callers (_run_once) catch it and record the
    failure instead of letting one bad action stop a whole sweep/event dispatch."""
    from .. import crm  # local import: crm does not import automation, so this is one-way
    action_type = workflow["action_type"]
    value = workflow["action_value"]
    if action_type == "assign":
        return crm.assign(conn, tenant_id, lead_id, value, actor=actor)
    if action_type == "set_status":
        return leads.set_status(conn, tenant_id, lead_id, value, actor=actor)
    if action_type == "reminder":
        minutes = float(value) if value else 60.0
        return crm.add_reminder(conn, tenant_id, lead_id, _now() + minutes * 60,
                                f"תזכורת אוטומטית (workflow {workflow['id']})", actor=actor)
    if action_type == "escalate":
        lead = leads.get(conn, tenant_id, lead_id)
        crm.add_reminder(conn, tenant_id, lead_id, _now(),
                         f"הסלמה: ליד {lead_id} דורש תשומת לב מיידית", actor=actor)
        business = guardian.business_number(conn, tenant_id)
        text = f"הסלמה אוטומטית על ליד #{lead_id}" + (f" ({lead.get('service')})" if lead else "")
        return notify.notify(conn, tenant_id, channel="wa_link", target=business, text=text, actor=actor)
    if action_type == "notify":
        lead = leads.get(conn, tenant_id, lead_id)
        target = value or (lead.get("phone") if lead else None)
        return notify.notify(conn, tenant_id, channel="wa_link", target=target,
                             text=f"עדכון אוטומטי על ליד #{lead_id}", actor=actor)
    raise ValueError(f"סוג פעולה לא מוכר: {action_type}")


def _run_once(conn, tenant_id, workflow, lead_id, idem, actor="automation"):
    """83: idempotency. A second call with the same key reads the existing row back and
    does NOT re-run the action - `skipped: True` on that result tells the caller this was
    a no-op read, not a fresh action, so callers counting "how many actions actually ran
    this time" (run_time_based's `ran`) do not double-count a lead that is still a
    candidate (still stale, still matches the condition) on a later sweep."""
    existing = conn.execute("SELECT * FROM workflow_runs WHERE tenant_id=? AND idempotency_key=?",
                            (tenant_id, idem)).fetchone()
    if existing:
        return dict(existing, skipped=True)
    try:
        apply_action(conn, tenant_id, workflow, lead_id, actor=actor)
        status, error = "done", None
    except Exception as e:  # noqa: BLE001 - one workflow's bug must not stop the batch
        status, error = "failed", str(e)
    conn.execute(
        "INSERT INTO workflow_runs(tenant_id, workflow_id, lead_id, idempotency_key, status, attempt, error, created)"
        " VALUES (?,?,?,?,?,1,?,?)",
        (tenant_id, workflow["id"], lead_id, idem, status, error, _now()))
    conn.commit()
    audit.log(conn, tenant_id, actor, "automation.run",
             {"workflow": workflow["id"], "lead": lead_id, "status": status})
    return {"workflow_id": workflow["id"], "lead_id": lead_id, "status": status, "error": error, "skipped": False}


# ---------------------------------------------------------------- 74: event-triggered

def trigger_event(conn, tenant_id, event_name, lead_id, actor="system"):
    """79's "action on status change" is just a workflow with trigger_value =
    "status_changed:<status>" - callers emit that event themselves (see crm.assign's
    "crm.lead_assigned" for the pattern); this file does not special-case status changes."""
    if is_globally_paused(conn, tenant_id) or lead_id is None:
        return []
    lead = leads.get(conn, tenant_id, lead_id)
    if lead is None:
        return []
    results = []
    for wf in list_workflows(conn, tenant_id, enabled_only=True):
        if wf["trigger_type"] != "event" or wf["trigger_value"] != event_name:
            continue
        condition = json.loads(wf["condition_json"]) if wf["condition_json"] else {}
        if not _condition_matches(lead, condition):
            continue
        idem = f"{wf['id']}:{event_name}:{lead_id}"
        results.append(_run_once(conn, tenant_id, wf, lead_id, idem, actor=actor))
    return results


_wired = False  # module-level guard - see reset_for_tests()


def _on_any_event(conn, tenant_id, event_type, data):
    lead_id = data.get("lead_id") if isinstance(data, dict) else None
    if lead_id is not None:
        trigger_event(conn, tenant_id, event_type, lead_id, actor="event:" + event_type)


def ensure_event_subscription():
    """Subscribes _on_any_event to core.events' wildcard topic exactly once per process -
    events.subscribe has no "already subscribed" check of its own, and subscribing the
    same handler twice would run every matching workflow twice per real event."""
    global _wired
    if not _wired:
        events.subscribe("*", _on_any_event)
        _wired = True


def reset_for_tests():
    """Test-only: undoes ensure_event_subscription's module-level guard, paired with
    core.events.unsubscribe_all() in a test's tearDown - otherwise one test's subscription
    silently survives into the next (events._subscribers is process-global state)."""
    global _wired
    _wired = False


# ---------------------------------------------------------------- 75/78: time-triggered

def run_time_based(conn, tenant_id, now=None):
    now = now if now is not None else _now()
    if is_globally_paused(conn, tenant_id):
        return {"ran": 0, "paused": True}
    runs = []
    for wf in list_workflows(conn, tenant_id, enabled_only=True):
        if wf["trigger_type"] != "time":
            continue
        condition = json.loads(wf["condition_json"]) if wf["condition_json"] else {}
        if wf["trigger_value"] == "stale_lead":
            # 78: reuses core.rules' own threshold (config "stale_lead_hours"), not a
            # second one - a lead is eligible exactly once, forever, from the moment it
            # goes stale (period_key is constant, not per-day: staleness is a one-time
            # fact about a lead, not something that re-fires daily).
            candidates = [l for l in leads.list_leads(conn, tenant_id, status="new", include_demo=False)
                          if rules.should_escalate_stale_lead(conn, tenant_id, l, now=now)]
            period_key = "stale"
        else:
            # generic time trigger (e.g. a daily digest-style workflow): every enabled
            # lead is re-checked once per calendar day.
            candidates = leads.list_leads(conn, tenant_id, include_demo=False)
            period_key = time.strftime("%Y-%m-%d", time.gmtime(now))
        for lead in candidates:
            if not _condition_matches(lead, condition):
                continue
            idem = f"{wf['id']}:{lead['id']}:{period_key}"
            runs.append(_run_once(conn, tenant_id, wf, lead["id"], idem))
    fresh = [r for r in runs if not r.get("skipped")]
    return {"ran": len(fresh), "skipped": len(runs) - len(fresh), "runs": runs}


# ---------------------------------------------------------------- 82: retry

def retry_failed(conn, tenant_id, limit=20, actor="automation"):
    rows = conn.execute(
        "SELECT * FROM workflow_runs WHERE tenant_id=? AND status='failed' AND attempt<? ORDER BY id LIMIT ?",
        (tenant_id, MAX_RETRY_ATTEMPTS, limit)).fetchall()
    retried = []
    for row in rows:
        wf = conn.execute("SELECT * FROM workflows WHERE tenant_id=? AND id=?",
                          (tenant_id, row["workflow_id"])).fetchone()
        if not wf:
            continue
        try:
            apply_action(conn, tenant_id, dict(wf), row["lead_id"], actor=actor)
            status, error = "done", None
        except Exception as e:  # noqa: BLE001
            status, error = "failed", str(e)
        conn.execute("UPDATE workflow_runs SET status=?, attempt=attempt+1, error=? WHERE id=?",
                     (status, error, row["id"]))
        conn.commit()
        audit.log(conn, tenant_id, actor, "automation.retry", {"run": row["id"], "status": status})
        retried.append({"run_id": row["id"], "status": status})
    return retried


def run_history(conn, tenant_id, workflow_id=None, limit=50):
    q = "SELECT * FROM workflow_runs WHERE tenant_id=?"
    args = [tenant_id]
    if workflow_id is not None:
        q += " AND workflow_id=?"
        args.append(workflow_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]


# ---------------------------------------------------------------- self-re-enqueuing sweep

def _sweep_handler(conn, tenant_id, payload):
    run_time_based(conn, tenant_id)
    retry_failed(conn, tenant_id)
    tasks.enqueue(conn, tenant_id, "automation.sweep", run_at=_now() + SWEEP_INTERVAL_SECONDS)


tasks.register_handler("automation.sweep", _sweep_handler)


def ensure_sweep_scheduled(conn, tenant_id):
    """Idempotent: enqueues the FIRST automation.sweep task only if none is already
    pending - every run after that re-enqueues its own successor (see _sweep_handler)."""
    pending = conn.execute(
        "SELECT COUNT(*) FROM core_tasks WHERE tenant_id=? AND kind='automation.sweep' AND status='pending'",
        (tenant_id,)).fetchone()[0]
    if not pending:
        tasks.enqueue(conn, tenant_id, "automation.sweep", run_at=_now())


# ---------------------------------------------------------------- plugin hooks

def routes(app):
    ensure_event_subscription()
    ensure_sweep_scheduled(app.conn, app.tid)

    def list_route(conn, tenant_id, body, query):
        return 200, {"workflows": list_workflows(conn, tenant_id)}

    def create_route(conn, tenant_id, body, query):
        try:
            wid = create_workflow(
                conn, tenant_id, name=body["name"], trigger_type=body["trigger_type"],
                trigger_value=body["trigger_value"], action_type=body["action_type"],
                action_value=body.get("action_value"), condition=body.get("condition"), actor="admin")
        except (ValueError, KeyError) as e:
            return 400, {"error": str(e)}
        return 201, {"id": wid}

    def enable_route(conn, tenant_id, body, query):
        ok = set_enabled(conn, tenant_id, int(body["id"]), True, actor="admin")
        return 200, {"ok": ok}

    def disable_route(conn, tenant_id, body, query):
        ok = set_enabled(conn, tenant_id, int(body["id"]), False, actor="admin")
        return 200, {"ok": ok}

    def pause_route(conn, tenant_id, body, query):
        set_global_pause(conn, tenant_id, True, actor="admin")
        return 200, {"paused": True}

    def resume_route(conn, tenant_id, body, query):
        set_global_pause(conn, tenant_id, False, actor="admin")
        return 200, {"paused": False}

    def run_now_route(conn, tenant_id, body, query):
        result = run_time_based(conn, tenant_id)
        result["retried"] = retry_failed(conn, tenant_id)
        return 200, result

    def history_route(conn, tenant_id, body, query):
        wid = query.get("workflow_id")
        return 200, {"runs": run_history(conn, tenant_id, int(wid[0]) if wid else None)}

    app.add_route("GET", "/automation/workflows", list_route)
    app.add_route("POST", "/automation/workflows", create_route)
    app.add_route("POST", "/automation/workflows/enable", enable_route)
    app.add_route("POST", "/automation/workflows/disable", disable_route)
    app.add_route("POST", "/automation/pause", pause_route)
    app.add_route("POST", "/automation/resume", resume_route)
    app.add_route("POST", "/automation/run_now", run_now_route)
    app.add_route("GET", "/automation/runs", history_route)


def _cmd_run_now(conn, tenant_id, match):
    result = run_time_based(conn, tenant_id)
    result["retried"] = retry_failed(conn, tenant_id)
    return {"ok": True, "message": f"רץ {result['ran']} בדיקות זמן, {len(result['retried'])} ניסיונות חזרה", "data": result}


def _cmd_pause(conn, tenant_id, match):
    set_global_pause(conn, tenant_id, True, actor="command")
    return {"ok": True, "message": "האוטומציה הופסקה (עצירת חירום)"}


def _cmd_resume(conn, tenant_id, match):
    set_global_pause(conn, tenant_id, False, actor="command")
    return {"ok": True, "message": "האוטומציה חודשה"}


COMMANDS = {
    r"(?:הרץ אוטומציה|run automation)$": _cmd_run_now,
    r"(?:עצור אוטומציה|stop automation)$": _cmd_pause,
    r"(?:הפעל אוטומציה|resume automation)$": _cmd_resume,
}
