"""Data collection & enrichment (NIGUN-134 module C, sections 26-36).

Builds on top of the existing lead model (nexus_master.leads/db, not re-implementing
intake) rather than replacing it: this package only adds what intake does not already
cover -- email, cross-source dedup beyond the real-time phone+service+7-day check
leads.find_duplicate already does at add_lead() time, change history, and an optional
(off-by-default) external enrichment provider.
"""
import time

from . import dedup, history, normalize
from .enrichment import EnrichmentProvider, NullProvider

MIGRATIONS = [
    # 0001 -- section 26 (unified model + email, the one field add_lead's intake has no
    # way to capture today), 31 (merge provenance), 36 (change history).
    """
    ALTER TABLE leads ADD COLUMN email TEXT;
    CREATE TABLE lead_history(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        lead_id INTEGER NOT NULL, ts REAL NOT NULL, actor TEXT NOT NULL,
        field TEXT NOT NULL, old_value TEXT, new_value TEXT);
    CREATE INDEX idx_lead_history_lead ON lead_history(tenant_id, lead_id);
    CREATE TABLE lead_merges(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        primary_lead_id INTEGER NOT NULL, merged_lead_id INTEGER NOT NULL,
        ts REAL NOT NULL, actor TEXT NOT NULL, fields_json TEXT NOT NULL);
    """,
]


def set_email(conn, tenant_id, lead_id, email, actor="enrich"):
    """Section 26/28. Validates format (not deliverability -- see validate_email's own
    docstring) before writing, and records the change in lead_history (section 36)."""
    clean = normalize.normalize_email(email)
    if clean and not normalize.validate_email(clean):
        raise ValueError(f"כתובת אימייל לא תקינה בפורמט: {email!r}")
    old = conn.execute("SELECT email FROM leads WHERE tenant_id=? AND id=?",
                       (tenant_id, lead_id)).fetchone()
    if old is None:
        raise ValueError("ליד לא קיים")
    conn.execute("UPDATE leads SET email=? WHERE tenant_id=? AND id=?", (clean, tenant_id, lead_id))
    conn.commit()
    history.record_change(conn, tenant_id, lead_id, actor, "email", old["email"], clean)
    return clean


def backfill_category_and_area(conn, tenant_id, lead_id, actor="enrich"):
    """Sections 33-34. Re-runs the existing scoring detectors (not a second
    implementation of them) against a lead's free text to fill service/area that intake
    left null -- e.g. a discovery-sourced lead whose connector supplied a bare phone
    number with no parseable text at add_lead() time, enriched later once more context
    is pasted in. Never overwrites a field that already has a value."""
    from .. import scoring
    row = conn.execute("SELECT * FROM leads WHERE tenant_id=? AND id=?",
                       (tenant_id, lead_id)).fetchone()
    if not row:
        raise ValueError("ליד לא קיים")
    parsed = scoring.parse_text(row["text"] or "")
    changed = {}
    for field in ("service", "area"):
        if not row[field] and parsed[field]:
            changed[field] = parsed[field]
    if changed:
        set_clause = ", ".join(f"{k}=?" for k in changed)
        conn.execute(f"UPDATE leads SET {set_clause} WHERE tenant_id=? AND id=?",
                     (*changed.values(), tenant_id, lead_id))
        conn.commit()
        for field, new_val in changed.items():
            history.record_change(conn, tenant_id, lead_id, actor, field, None, new_val)
    return changed


def fields_report(conn, tenant_id, stale_days=30, required=("phone", "service", "area")):
    """Section 35. Per active (non-demo, non-spam/duplicate) lead: which `required` fields
    are missing, and whether the lead is old enough (`stale_days`) that "still missing"
    stops being "just not enriched yet" and starts being worth a human look."""
    cutoff = time.time() - stale_days * 86400
    out = []
    rows = conn.execute(
        "SELECT * FROM leads WHERE tenant_id=? AND is_demo=0 AND status NOT IN ('spam','duplicate')",
        (tenant_id,))
    for r in rows:
        missing = [f for f in required if not r[f]]
        if missing:
            out.append({"lead_id": r["id"], "missing": missing,
                       "stale": r["created"] < cutoff, "created": r["created"]})
    return out


def routes(app):
    def _set_email(conn, tid, body, query):
        return 200, {"email": set_email(conn, tid, int(body["lead_id"]), body["email"],
                                        actor="api")}

    def _duplicates(conn, tid, body, query):
        lead_id = int(query.get("lead_id", [0])[0])
        return 200, dedup.find_cross_source_duplicates(conn, tid, lead_id)

    def _merge(conn, tid, body, query):
        return 200, dedup.merge_leads(conn, tid, int(body["primary_lead_id"]),
                                      int(body["duplicate_lead_id"]), actor="api")

    def _fields_report(conn, tid, body, query):
        return 200, fields_report(conn, tid)

    def _history(conn, tid, body, query):
        lead_id = int(query.get("lead_id", [0])[0])
        return 200, history.get_history(conn, tid, lead_id)

    app.add_route("POST", "/enrich/email", _set_email)
    app.add_route("GET", "/enrich/duplicates", _duplicates)
    app.add_route("POST", "/enrich/merge", _merge)
    app.add_route("GET", "/enrich/fields-report", _fields_report)
    app.add_route("GET", "/enrich/history", _history)


COMMANDS = {
    r"(?:דוח שדות חסרים|fields report)$": lambda conn, tid, m: (
        {"ok": True, "data": fields_report(conn, tid)}),
}
