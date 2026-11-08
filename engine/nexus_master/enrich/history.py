"""Section 36: a complete history of data changes -- a plain table + two functions, not
a SQL TRIGGER. No table anywhere else in this project is managed by a native SQLite
TRIGGER (grep engine/nexus_master/*.py -- every write path is an explicit Python
function), so a trigger here would be the one inconsistent piece of magic in an
otherwise fully-explicit codebase. "Trigger" is implemented as: every enrich/ function
that changes a field calls record_change() itself, not as a database-level trigger."""
import time


def record_change(conn, tenant_id, lead_id, actor, field, old_value, new_value):
    conn.execute(
        "INSERT INTO lead_history(tenant_id, lead_id, ts, actor, field, old_value, new_value)"
        " VALUES (?,?,?,?,?,?,?)",
        (tenant_id, lead_id, time.time(), actor, field,
         None if old_value is None else str(old_value), None if new_value is None else str(new_value)))
    conn.commit()


def get_history(conn, tenant_id, lead_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM lead_history WHERE tenant_id=? AND lead_id=? ORDER BY id", (tenant_id, lead_id))]
