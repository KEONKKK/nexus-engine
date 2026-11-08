"""Section 125: retention, export and deletion. Export and delete are real and
immediate (the "ייצא ליד 5" / "מחק ליד 5" commands this section specifically calls
for). The *automatic* half ("retention אוטומטי דרך tasks") is the same situation as
discovery's section 16: core/tasks.py (module A/L) does not exist yet, so
retention_candidates() is the callable unit a scheduler will eventually call -- it
works today when called manually, it is just not wired to a clock yet."""
import json
import time

from .. import audit, leads


def export_lead(conn, tenant_id, lead_id):
    """Section 125 (export). Everything we hold on this lead, including its change
    history (enrich/history.py) if that package is also installed."""
    lead = leads.get(conn, tenant_id, lead_id)
    if not lead:
        raise ValueError("ליד לא קיים")
    out = {"lead": lead}
    try:
        from ..enrich import history
        out["history"] = history.get_history(conn, tenant_id, lead_id)
    except ImportError:  # pragma: no cover -- enrich/ always exists in this tree today
        out["history"] = []
    return out


def delete_lead(conn, tenant_id, lead_id, actor="privacy"):
    """Section 125 (delete). A real DELETE, not a soft-flag -- logged to the audit chain
    *before* the row disappears, including a full snapshot, so the deletion itself is
    the last thing provably known about the lead, same principle as
    leads.remove_demo's cascade (deals/ledger rows that point at a deleted lead must not
    outlive it). Returns False (not an exception) for a lead that is already gone --
    same convention as leads.set_status/mark_notify_clicked, both of which signal
    "nothing to do" with a bool rather than an error."""
    if not leads.get(conn, tenant_id, lead_id):
        return False
    snapshot = export_lead(conn, tenant_id, lead_id)
    audit.log(conn, tenant_id, actor, "lead.delete", {"lead_id": lead_id, "snapshot": snapshot})
    conn.execute("DELETE FROM deals WHERE tenant_id=? AND lead_id=?", (tenant_id, lead_id))
    conn.execute("DELETE FROM ledger WHERE tenant_id=? AND lead_id=?", (tenant_id, lead_id))
    conn.execute("UPDATE kits SET lead_id=NULL WHERE tenant_id=? AND lead_id=?", (tenant_id, lead_id))
    conn.execute("DELETE FROM lead_history WHERE tenant_id=? AND lead_id=?", (tenant_id, lead_id))
    n = conn.execute("DELETE FROM leads WHERE tenant_id=? AND id=?", (tenant_id, lead_id)).rowcount
    conn.commit()
    return bool(n)


def retention_candidates(conn, tenant_id, days=365):
    """The callable unit for automatic retention: leads that are both terminal
    (lost/spam/duplicate) and older than `days`. Returns candidates, does not delete
    them -- an explicit delete_lead() call (human or, once core/tasks exists,
    scheduled) is still required."""
    cutoff = time.time() - days * 86400
    rows = conn.execute(
        "SELECT id, created, status FROM leads WHERE tenant_id=? AND created<?"
        " AND status IN ('lost','spam','duplicate')", (tenant_id, cutoff))
    return [dict(r) for r in rows]
