"""Background task queue (NIGUN-134 module A, sections 6-8): SQLite-backed, priority +
retry with exponential backoff, processed by `python -m nexus_master tick` -- a single
batch per invocation, not a daemon (CLAUDE.md: no background processes in this project).
A cron-like external trigger (cron, a scheduled task, or a human running it) is what
turns this into "scheduled" -- this module only does one batch and returns.
"""
import json
import time

_handlers = {}  # kind -> handler(conn, tenant_id, payload) -- may raise to trigger a retry


def register_handler(kind, handler):
    _handlers[kind] = handler


def enqueue(conn, tenant_id, kind, payload=None, priority=0, run_at=None, max_attempts=5):
    cur = conn.execute(
        "INSERT INTO core_tasks(tenant_id, kind, payload, priority, run_at, created, max_attempts)"
        " VALUES (?,?,?,?,?,?,?)",
        (tenant_id, kind, json.dumps(payload or {}, ensure_ascii=False, default=str), priority,
         run_at if run_at is not None else time.time(), time.time(), max_attempts))
    conn.commit()
    return cur.lastrowid


def _due(conn, tenant_id, limit):
    rows = conn.execute(
        "SELECT * FROM core_tasks WHERE tenant_id=? AND status='pending' AND run_at<=?"
        " ORDER BY priority DESC, run_at ASC LIMIT ?", (tenant_id, time.time(), limit)).fetchall()
    return [dict(r) for r in rows]


def tick(conn, tenant_id, limit=10):
    """Runs every due task once. Returns {done, failed, retried} counts. A handler that
    raises is retried with exponential backoff (2**attempts seconds) until max_attempts,
    then marked 'failed' -- it stays in the table for inspection, never silently dropped."""
    result = {"done": 0, "retried": 0, "failed": 0}
    for row in _due(conn, tenant_id, limit):
        handler = _handlers.get(row["kind"])
        if handler is None:
            conn.execute("UPDATE core_tasks SET status='failed', last_error=? WHERE id=?",
                         (f"no handler registered for kind={row['kind']}", row["id"]))
            result["failed"] += 1
            continue
        payload = json.loads(row["payload"] or "{}")
        try:
            handler(conn, tenant_id, payload)
            conn.execute("UPDATE core_tasks SET status='done' WHERE id=?", (row["id"],))
            result["done"] += 1
        except Exception as e:  # noqa: BLE001 -- a task handler's own bug must not stop the batch
            attempts = row["attempts"] + 1
            if attempts >= row["max_attempts"]:
                conn.execute("UPDATE core_tasks SET status='failed', attempts=?, last_error=? WHERE id=?",
                             (attempts, str(e), row["id"]))
                result["failed"] += 1
            else:
                backoff = 2 ** attempts
                conn.execute(
                    "UPDATE core_tasks SET attempts=?, run_at=?, last_error=? WHERE id=?",
                    (attempts, time.time() + backoff, str(e), row["id"]))
                result["retried"] += 1
        conn.commit()
    return result


def queue_depth(conn, tenant_id):
    return conn.execute("SELECT COUNT(*) FROM core_tasks WHERE tenant_id=? AND status='pending'",
                        (tenant_id,)).fetchone()[0]
