"""Leads: intake, scoring, duplicates, status changes."""
import json
import time

from . import audit, scoring
from .attribution import parse_ref
from .guardian import normalize_phone, opt_out, is_stop_message

STATUSES = ("new", "contacted", "sold", "won", "lost", "spam", "duplicate")


def add_lead(conn, tenant_id, *, text="", name=None, phone=None, service=None, area=None,
             budget=None, urgency=None, source=None, consent=False, is_demo=False, actor="system"):
    """Create a lead. Missing fields are extracted from text. Returns (lead_id, duplicate_of)."""
    if is_stop_message(text) and phone:
        opt_out(conn, tenant_id, phone)
        audit.log(conn, tenant_id, actor, "optout", {"phone": normalize_phone(phone)})
        return None, None

    parsed = scoring.parse_text(text)
    lead = {
        "name": name, "text": text,
        "phone": normalize_phone(phone) or parsed["phone"],
        "service": service or parsed["service"],
        "area": area or parsed["area"],
        "budget": budget if budget is not None else parsed["budget"],
        "urgency": urgency or parsed["urgency"],
        "source": parse_ref(source) if source else (parse_ref(text) or "direct"),
        "consent": 1 if consent else 0,
    }
    dup = find_duplicate(conn, tenant_id, lead["phone"], lead["service"])
    total, scores, reasons = scoring.score(lead, conn, tenant_id)
    cur = conn.execute(
        "INSERT INTO leads(tenant_id, created, name, phone, service, area, budget, urgency, text,"
        " source, consent, is_demo, score_total, scores_json, reasons_json, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tenant_id, time.time(), lead["name"], lead["phone"], lead["service"], lead["area"],
         lead["budget"], lead["urgency"], text, lead["source"], lead["consent"], int(is_demo),
         total, json.dumps(scores), json.dumps(reasons, ensure_ascii=False),
         "duplicate" if dup else "new"))
    conn.commit()
    lid = cur.lastrowid
    audit.log(conn, tenant_id, actor, "lead.add", {"lead": lid, "score": total, "dup_of": dup})
    return lid, dup


def find_duplicate(conn, tenant_id, phone, service, window_days=7):
    if not phone:
        return None
    row = conn.execute(
        "SELECT id FROM leads WHERE tenant_id=? AND phone=? AND IFNULL(service,'')=IFNULL(?,'')"
        " AND created>? AND status NOT IN ('spam','duplicate') ORDER BY id DESC LIMIT 1",
        (tenant_id, phone, service, time.time() - window_days * 86400)).fetchone()
    return row["id"] if row else None


def get(conn, tenant_id, lead_id):
    row = conn.execute("SELECT * FROM leads WHERE tenant_id=? AND id=?", (tenant_id, lead_id)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["scores"] = json.loads(d.pop("scores_json") or "{}")
    d["reasons"] = json.loads(d.pop("reasons_json") or "{}")
    return d


def list_leads(conn, tenant_id, status=None, include_demo=True, limit=100, offset=0):
    """NIGUN-134 module L section 130: `offset` added for real pagination -- page 2 is
    `offset=limit`, not "fetch everything and slice in Python" (leads_tenant index on
    (tenant_id, status) already covers the status filter; LIMIT/OFFSET does the rest)."""
    q = "SELECT id FROM leads WHERE tenant_id=?"
    args = [tenant_id]
    if status:
        q += " AND status=?"
        args.append(status)
    if not include_demo:
        q += " AND is_demo=0"
    q += " ORDER BY score_total DESC, id DESC LIMIT ? OFFSET ?"
    args.extend([limit, offset])
    return [get(conn, tenant_id, r["id"]) for r in conn.execute(q, args)]


def set_status(conn, tenant_id, lead_id, status, actor="user"):
    if status not in STATUSES:
        raise ValueError(f"סטטוס לא מוכר: {status}")
    n = conn.execute("UPDATE leads SET status=? WHERE tenant_id=? AND id=?",
                     (status, tenant_id, lead_id)).rowcount
    conn.commit()
    if n:
        audit.log(conn, tenant_id, actor, "lead.status", {"lead": lead_id, "status": status})
    return bool(n)


def recalc_score(conn, tenant_id, lead_id):
    """NIGUN-134 module D section 47: re-run scoring.score() against the lead's current
    row and persist it. Returns the new total, or None if the lead does not exist."""
    lead = get(conn, tenant_id, lead_id)
    if not lead:
        return None
    total, scores, reasons = scoring.score(lead, conn, tenant_id)
    conn.execute("UPDATE leads SET score_total=?, scores_json=?, reasons_json=? WHERE tenant_id=? AND id=?",
                 (total, json.dumps(scores), json.dumps(reasons, ensure_ascii=False), tenant_id, lead_id))
    conn.commit()
    return total


def update_fields(conn, tenant_id, lead_id, **fields):
    """Updates the given lead columns and recalculates its score -- section 47's
    "relevant information changed" means exactly this: a field scoring.score() reads
    changed, so the score it produced is now stale."""
    allowed = {"name", "phone", "service", "area", "budget", "urgency"}
    cols = {k: v for k, v in fields.items() if k in allowed}
    if cols:
        set_clause = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE leads SET {set_clause} WHERE tenant_id=? AND id=?",
                     (*cols.values(), tenant_id, lead_id))
        conn.commit()
    return recalc_score(conn, tenant_id, lead_id)


def hot_list(conn, tenant_id):
    """NIGUN-134 module D section 48: automatic high-priority list, via the configurable
    rule (core.rules.is_hot_lead) rather than a hardcoded threshold -- replaces the inline
    `score_total >= 70` that command.py used to run directly."""
    from .core import rules
    return [l for l in list_leads(conn, tenant_id, status="new", include_demo=False)
            if rules.is_hot_lead(conn, tenant_id, l)]


def set_notify_link(conn, tenant_id, lead_id, link):
    conn.execute("UPDATE leads SET notify_link=? WHERE tenant_id=? AND id=?",
                 (link, tenant_id, lead_id))
    conn.commit()


def mark_notify_clicked(conn, tenant_id, lead_id):
    n = conn.execute("UPDATE leads SET notify_clicked=1 WHERE tenant_id=? AND id=?",
                     (tenant_id, lead_id)).rowcount
    conn.commit()
    return bool(n)


def remove_demo(conn, tenant_id):
    """Delete demo leads and suppliers, plus the deals and ledger rows that point at them --
    otherwise those rows outlive the lead/supplier they reference."""
    demo_lead_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM leads WHERE tenant_id=? AND is_demo=1", (tenant_id,))]
    demo_supplier_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM suppliers WHERE tenant_id=? AND is_demo=1", (tenant_id,))]
    if demo_lead_ids:
        marks = ",".join("?" * len(demo_lead_ids))
        conn.execute(f"DELETE FROM deals WHERE tenant_id=? AND lead_id IN ({marks})",
                     (tenant_id, *demo_lead_ids))
        conn.execute(f"DELETE FROM ledger WHERE tenant_id=? AND lead_id IN ({marks})",
                     (tenant_id, *demo_lead_ids))
    if demo_supplier_ids:
        marks = ",".join("?" * len(demo_supplier_ids))
        conn.execute(f"DELETE FROM deals WHERE tenant_id=? AND supplier_id IN ({marks})",
                     (tenant_id, *demo_supplier_ids))
    for t in ("leads", "suppliers"):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id=? AND is_demo=1", (tenant_id,))
    conn.commit()
    audit.log(conn, tenant_id, "user", "demo.remove")
