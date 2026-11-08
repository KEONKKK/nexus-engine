"""Sections 30-31: cross-source duplicate detection and merge-with-provenance.

Deliberately broader, and separate from, nexus_master.leads.find_duplicate: that function
is a real-time intake gate (same phone + same service, within a 7-day window) designed
to catch a customer re-submitting the same request. This module is an enrichment-pass
audit over the whole lead table -- any two leads sharing a phone, an email, or a
(name, area) pair, with no time window and no service match required -- because a
customer discovered via two different sources (e.g. a paste and an RSS item) may use
different wording for the same request."""
from . import history
from .normalize import normalize_name


def find_cross_source_duplicates(conn, tenant_id, lead_id):
    """Section 30. Returns other leads (not `lead_id` itself) that share a phone, an
    email, or a (name, area) pair with it."""
    lead = conn.execute("SELECT * FROM leads WHERE tenant_id=? AND id=?",
                        (tenant_id, lead_id)).fetchone()
    if not lead:
        raise ValueError("ליד לא קיים")
    matches = {}

    def _add(rows, why):
        for r in rows:
            if r["id"] == lead_id:
                continue
            matches.setdefault(r["id"], {"lead_id": r["id"], "reasons": []})
            matches[r["id"]]["reasons"].append(why)

    if lead["phone"]:
        _add(conn.execute("SELECT id FROM leads WHERE tenant_id=? AND phone=? AND id!=?",
                          (tenant_id, lead["phone"], lead_id)), "same_phone")
    if lead["email"]:
        _add(conn.execute("SELECT id FROM leads WHERE tenant_id=? AND email=? AND id!=?",
                          (tenant_id, lead["email"], lead_id)), "same_email")
    name = normalize_name(lead["name"])
    if name and lead["area"]:
        _add(conn.execute(
            "SELECT id FROM leads WHERE tenant_id=? AND name=? AND area=? AND id!=?",
            (tenant_id, name, lead["area"], lead_id)), "same_name_and_area")
    return list(matches.values())


def merge_leads(conn, tenant_id, primary_lead_id, duplicate_lead_id, actor="enrich"):
    """Section 31. Fills gaps in `primary` from `duplicate` (never overwrites a value
    `primary` already has), marks `duplicate` with the existing 'duplicate' status
    (leads.STATUSES already has this value -- not a new status invented here), and
    records which fields came from which lead (fields_json) plus one lead_history entry
    per field actually changed."""
    if primary_lead_id == duplicate_lead_id:
        raise ValueError("לא ניתן למזג ליד עם עצמו")
    primary = conn.execute("SELECT * FROM leads WHERE tenant_id=? AND id=?",
                           (tenant_id, primary_lead_id)).fetchone()
    dup = conn.execute("SELECT * FROM leads WHERE tenant_id=? AND id=?",
                       (tenant_id, duplicate_lead_id)).fetchone()
    if not primary or not dup:
        raise ValueError("ליד לא קיים")
    fillable = ("name", "phone", "email", "service", "area", "budget")
    filled = {}
    for field in fillable:
        if not primary[field] and dup[field]:
            filled[field] = dup[field]
    if filled:
        set_clause = ", ".join(f"{k}=?" for k in filled)
        conn.execute(f"UPDATE leads SET {set_clause} WHERE tenant_id=? AND id=?",
                     (*filled.values(), tenant_id, primary_lead_id))
        for field, new_val in filled.items():
            history.record_change(conn, tenant_id, primary_lead_id, actor, field, None, new_val)
    from .. import leads as leads_module
    leads_module.set_status(conn, tenant_id, duplicate_lead_id, "duplicate", actor=actor)
    import json
    import time
    conn.execute(
        "INSERT INTO lead_merges(tenant_id, primary_lead_id, merged_lead_id, ts, actor, fields_json)"
        " VALUES (?,?,?,?,?,?)",
        (tenant_id, primary_lead_id, duplicate_lead_id, time.time(), actor,
         json.dumps({"filled_from_duplicate": filled}, ensure_ascii=False)))
    conn.commit()
    return {"primary_lead_id": primary_lead_id, "duplicate_lead_id": duplicate_lead_id,
            "filled_from_duplicate": filled}
