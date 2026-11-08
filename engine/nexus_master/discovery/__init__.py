"""Lead-source discovery engine (NIGUN-134 module B, sections 13-25).

A "source" is a configured instance of a connector (paste / rss / lead_ads) that can be
run to produce candidate leads, which are then filtered (geo/category/intent) and fed
through the existing nexus_master.leads.add_lead -- discovery does not re-implement
intake, scoring, or dedup, it only produces text for the pipeline that already does that.

Explicitly out of scope, by the instruction that created this module (design/research/
nigun-134.md, "MODULE H" note carried over here too): no scraping of private accounts/
profiles, no bypassing platform access controls. The rss/atom connector only reads
public feed URLs an operator configures by hand -- it is not a web crawler.
"""
import time

from . import connectors, filters
from .connectors import LeadAdsConnector, PasteConnector, RssConnector

MIGRATIONS = [
    # 0001 -- configured source instances (section 15: "Create configurable source
    # connectors"; sections 24-25: track origin + reliability/freshness per source).
    """
    CREATE TABLE discovery_sources(
        id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, created REAL,
        connector TEXT NOT NULL, name TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',
        active INTEGER DEFAULT 1,
        reliability REAL NOT NULL DEFAULT 1.0,
        last_run REAL, last_run_found INTEGER, last_run_error TEXT);
    CREATE INDEX idx_discovery_sources_tenant ON discovery_sources(tenant_id, active);
    """,
]

CONNECTORS = {
    "paste": PasteConnector,
    "rss": RssConnector,
    "lead_ads": LeadAdsConnector,
}


def register_source(conn, tenant_id, connector, name, config=None):
    """Section 15. `connector` must be a key in CONNECTORS; `config` is connector-specific
    (e.g. {"url": "..."} for rss)."""
    if connector not in CONNECTORS:
        raise ValueError(f"מחבר לא מוכר: {connector} (זמינים: {', '.join(CONNECTORS)})")
    import json
    cur = conn.execute(
        "INSERT INTO discovery_sources(tenant_id, created, connector, name, config_json)"
        " VALUES (?,?,?,?,?)", (tenant_id, time.time(), connector, name, json.dumps(config or {})))
    conn.commit()
    return cur.lastrowid


def list_sources(conn, tenant_id, active_only=True):
    q = "SELECT * FROM discovery_sources WHERE tenant_id=?"
    if active_only:
        q += " AND active=1"
    return [dict(r) for r in conn.execute(q, (tenant_id,))]


def run_source(conn, tenant_id, source_id, actor="discovery"):
    """Sections 13, 16-23. Fetch candidates from one configured source, filter them, and
    feed survivors through leads.add_lead. Returns a report dict -- never raises on a
    connector failure (recorded on the source row instead), so one broken source does not
    stop a batch run (run_all, below) partway through the rest.

    Section 16 ("scheduled lead discovery"): this function (and run_all) is also the unit
    core.tasks actually calls now -- see schedule_run()/schedule_run_all() and
    register_task_handlers() below, wired to core/tasks.py (module A, now built)."""
    import json
    from .. import leads

    row = conn.execute("SELECT * FROM discovery_sources WHERE tenant_id=? AND id=?",
                       (tenant_id, source_id)).fetchone()
    if not row:
        raise ValueError("מקור לא קיים")
    config = json.loads(row["config_json"] or "{}")
    connector_cls = CONNECTORS[row["connector"]]
    found = added = duplicates = 0
    error = None
    try:
        candidates = connector_cls().fetch(config)
        candidates = filters.apply_tenant_filters(candidates, config)
        found = len(candidates)
        # attribution.parse_ref only accepts a short alnum/underscore/hyphen code (or a
        # "#code"/"ref=code" marker) -- a free-form Hebrew source *name* like "הדבקה
        # ידנית" fails that and silently becomes source=None. The connector key
        # ("paste"/"rss"/"lead_ads") is always parse_ref-safe; the human-readable name
        # stays on the discovery_sources row itself for display.
        for cand in candidates:
            lid, dup = leads.add_lead(conn, tenant_id, text=cand.get("text", ""),
                                      phone=cand.get("phone"), source=f"discovery_{row['connector']}",
                                      consent=bool(cand.get("consent", False)), actor=actor)
            if lid is None:
                continue
            if dup:
                duplicates += 1
            else:
                added += 1
    except Exception as e:  # noqa: BLE001 -- a connector failure must not crash the batch
        error = str(e)
    conn.execute(
        "UPDATE discovery_sources SET last_run=?, last_run_found=?, last_run_error=?,"
        " reliability=? WHERE id=?",
        (time.time(), found, error, _next_reliability(row["reliability"], error is None), source_id))
    conn.commit()
    return {"source_id": source_id, "found": found, "added": added, "duplicates": duplicates,
            "error": error}


def _next_reliability(previous, ok, alpha=0.3):
    """Section 25. Exponential moving average of run success (1.0 ok / 0.0 failed), so one
    bad run degrades a source gradually instead of a single boolean "last run failed"."""
    target = 1.0 if ok else 0.0
    return round(previous * (1 - alpha) + target * alpha, 3)


def run_all(conn, tenant_id, actor="discovery"):
    """Section 13/16. Run every active source for a tenant; one source's failure (recorded
    on its own row by run_source) does not stop the others."""
    return [run_source(conn, tenant_id, s["id"], actor=actor) for s in list_sources(conn, tenant_id)]


# ---- section 16: scheduled discovery, now wired to core.tasks (module A) ----------

def register_task_handlers():
    """Called once at import time (bottom of this file) so discovery's task kinds are
    registered the moment core.registry.discover_plugins() imports this package --
    exactly how db.migrate/App already pick up MIGRATIONS/routes without a central list."""
    from ..core import tasks as core_tasks
    core_tasks.register_handler(
        "discovery.run_source", lambda conn, tid, payload: run_source(conn, tid, payload["source_id"]))
    core_tasks.register_handler("discovery.run_all", lambda conn, tid, payload: run_all(conn, tid))


def schedule_run(conn, tenant_id, source_id=None, run_at=None):
    """Enqueues a real core_tasks row -- `python -m nexus_master tick` (or a cron calling
    it) is what actually executes it; this function only schedules. source_id=None
    schedules run_all (every active source) instead of one."""
    from ..core import tasks as core_tasks
    if source_id is not None:
        return core_tasks.enqueue(conn, tenant_id, "discovery.run_source",
                                  {"source_id": source_id}, run_at=run_at)
    return core_tasks.enqueue(conn, tenant_id, "discovery.run_all", {}, run_at=run_at)


COMMANDS = {
    r"(?:מקורות גילוי|discovery sources)$": lambda conn, tid, m: (
        {"ok": True, "data": list_sources(conn, tid)}),
    r"(?:הרץ גילוי|run discovery)$": lambda conn, tid, m: (
        {"ok": True, "data": run_all(conn, tid, actor="command")}),
    r"(?:תזמן גילוי|schedule discovery)$": lambda conn, tid, m: (
        {"ok": True, "message": f"משימה {schedule_run(conn, tid)} נכנסה לתור"}),
}


def routes(app):
    def _list(conn, tid, body, query):
        return 200, list_sources(conn, tid)

    def _register(conn, tid, body, query):
        sid = register_source(conn, tid, body["connector"], body["name"], body.get("config"))
        return 201, {"id": sid}

    def _run_all(conn, tid, body, query):
        return 200, run_all(conn, tid)

    def _schedule(conn, tid, body, query):
        task_id = schedule_run(conn, tid, source_id=body.get("source_id"), run_at=body.get("run_at"))
        return 201, {"task_id": task_id}

    app.add_route("GET", "/discovery/sources", _list)
    app.add_route("POST", "/discovery/sources", _register)
    app.add_route("POST", "/discovery/run", _run_all)
    app.add_route("POST", "/discovery/schedule", _schedule)


register_task_handlers()
