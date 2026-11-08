"""Central event bus (NIGUN-134 module A, section 4): emit() logs to core_events and
dispatches, synchronously and in-process, to every handler subscribed to that type (or
to "*"). No external queue or broker -- this project is stdlib-only and single-process,
same constraint as everything else in engine/.
"""
import json
import time

_subscribers = {}  # event_type -> [handler(conn, tenant_id, event_type, data)]


def subscribe(event_type, handler):
    _subscribers.setdefault(event_type, []).append(handler)


def unsubscribe_all():
    """Test-only reset -- subscribe() is otherwise append-only for the process lifetime."""
    _subscribers.clear()


def emit(conn, tenant_id, event_type, data=None):
    data = data or {}
    conn.execute("INSERT INTO core_events(tenant_id, ts, type, data) VALUES (?,?,?,?)",
                 (tenant_id, time.time(), event_type, json.dumps(data, ensure_ascii=False, default=str)))
    conn.commit()
    for handler in _subscribers.get(event_type, []) + _subscribers.get("*", []):
        handler(conn, tenant_id, event_type, data)


def recent(conn, tenant_id, event_type=None, limit=50):
    q = "SELECT * FROM core_events WHERE tenant_id=?"
    args = [tenant_id]
    if event_type:
        q += " AND type=?"
        args.append(event_type)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    out = []
    for row in conn.execute(q, args):
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        out.append(d)
    return out
