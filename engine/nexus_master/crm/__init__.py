"""CRM & customer management (NIGUN-134 module E, sections 49-60). Owned by window A.

Deliberately data/functions on TOP of the existing leads/buyers/brokerage/partners tables,
not a parallel "lead" concept: a contact, interaction, note, assignment, reminder or
segment always points at a real leads.id (or a buyer/supplier/partner phone), never
duplicates what those modules already store.

Sections covered here:
  49 contacts            - unified customer CRM, keyed by phone (one contact per phone,
                            many leads can point at the same person over time)
  50 partners overview   - "separate supplier/service-provider CRM", unified with
                            buyers/suppliers/pending-partners through a VIEW (below), not
                            a duplicate table - see partners_overview()
  51 lead_profile         - full lead profile: lead + interactions + notes + deal history
  52 pipeline_stages      - customizable STAGE LABELS from settings, layered on top of
                            leads.STATUSES (which stays the validated enum leads.set_status
                            checks against - not mine to change; this is presentation only)
  53 pipeline_counts      - visual sales pipeline's DATA (the admin UI renders it, section 5
                            of the round-4 brief)
  54 log_interaction      - calls/messages/meetings
  55 notes                - internal comments
  56 assign               - lead -> team member (single-owner business: assignee defaults
                            to DEFAULT_OWNER, but the mechanism supports any assignee string)
  57 assignment_history   - every assign() call, kept forever
  58 reminders            - follow-up dates
  59 segments             - saved queries (a small whitelisted filter dict, never raw SQL)
  60 relationship_history - full chronological history for one phone number, across leads,
                            interactions, notes and deals

Every mutation here also runs through audit.log (same hash-chained trail as the rest of
the project) and, where relevant to automation, core.events.emit() - so module G's
event-triggered workflows can subscribe to "crm.lead_assigned" etc. without this package
importing automation/ at all (events.py is the decoupling point, not a direct call).
"""
import json
import time

from .. import audit, leads
from ..guardian import normalize_phone

DEFAULT_OWNER = "owner"

INTERACTION_KINDS = ("call", "message", "meeting")

MIGRATIONS = [
    """
    CREATE TABLE contacts(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, phone TEXT NOT NULL,
        name TEXT, created REAL NOT NULL, UNIQUE(tenant_id, phone));

    CREATE TABLE interactions(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        lead_id INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT, actor TEXT, created REAL NOT NULL);
    CREATE INDEX idx_crm_interactions_lead ON interactions(tenant_id, lead_id);

    CREATE TABLE notes(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        lead_id INTEGER NOT NULL, text TEXT NOT NULL, actor TEXT, created REAL NOT NULL);
    CREATE INDEX idx_crm_notes_lead ON notes(tenant_id, lead_id);

    CREATE TABLE assignments(tenant_id INTEGER NOT NULL, lead_id INTEGER NOT NULL,
        assignee TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(tenant_id, lead_id));

    CREATE TABLE assignment_history(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        lead_id INTEGER NOT NULL, assignee TEXT NOT NULL, actor TEXT, created REAL NOT NULL);
    CREATE INDEX idx_crm_assignment_history_lead ON assignment_history(tenant_id, lead_id);

    CREATE TABLE reminders(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        lead_id INTEGER, due REAL NOT NULL, text TEXT, done INTEGER DEFAULT 0,
        actor TEXT, created REAL NOT NULL);
    CREATE INDEX idx_crm_reminders_due ON reminders(tenant_id, done, due);

    CREATE TABLE segments(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        name TEXT NOT NULL, query_json TEXT NOT NULL, created REAL NOT NULL,
        UNIQUE(tenant_id, name));

    -- Section 50: read-only union, not a new source of truth. "partner_pending" excludes
    -- approved rows on purpose - an approved partner already appears as a real buyer/
    -- supplier row (nexus_master.partners.approve() creates it), so including it here too
    -- would double-count the same business.
    CREATE VIEW partners_overview AS
      SELECT 'buyer' AS source, id, tenant_id, name, phone, services, areas, 'active' AS status
        FROM buyers WHERE active=1
      UNION ALL
      SELECT 'supplier' AS source, id, tenant_id, name, phone, keywords AS services, areas,
             'active' AS status
        FROM suppliers WHERE is_demo=0
      UNION ALL
      SELECT 'partner_pending' AS source, id, tenant_id, name, phone, services, areas, status
        FROM partners WHERE status='pending';
    """,
]


def _now():
    return time.time()


# ---------------------------------------------------------------- 49: contacts

def upsert_contact(conn, tenant_id, phone, name=None, actor="system"):
    """One contact per (tenant, phone). A second call for the same phone updates the name
    (if a non-empty one is given) instead of creating a duplicate person.

    Normalizes `phone` the same way leads.phone is stored (guardian.normalize_phone, no
    "+") regardless of what format the caller passes in - otherwise a contact keyed on
    "+972..." would never match the lead rows relationship_history/lead_profile need to
    join against, which all read leads.phone as digits-only."""
    phone = normalize_phone(phone)
    if not phone:
        raise ValueError("contacts: מספר טלפון לא תקין")
    row = conn.execute("SELECT id FROM contacts WHERE tenant_id=? AND phone=?",
                       (tenant_id, phone)).fetchone()
    if row:
        if name:
            conn.execute("UPDATE contacts SET name=? WHERE id=?", (name, row["id"]))
            conn.commit()
        return row["id"]
    cur = conn.execute("INSERT INTO contacts(tenant_id, phone, name, created) VALUES (?,?,?,?)",
                       (tenant_id, phone, name, _now()))
    conn.commit()
    audit.log(conn, tenant_id, actor, "crm.contact.create", {"contact": cur.lastrowid, "phone": phone})
    return cur.lastrowid


def get_contact(conn, tenant_id, phone):
    row = conn.execute("SELECT * FROM contacts WHERE tenant_id=? AND phone=?",
                       (tenant_id, normalize_phone(phone))).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 50: partners overview

def partners_overview(conn, tenant_id, kind=None):
    """kind: optional filter on the 'source' column ('buyer'|'supplier'|'partner_pending')."""
    q = "SELECT * FROM partners_overview WHERE tenant_id=?"
    args = [tenant_id]
    if kind:
        q += " AND source=?"
        args.append(kind)
    q += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(q, args)]


# ---------------------------------------------------------------- 54/55: interactions, notes

def log_interaction(conn, tenant_id, lead_id, kind, text="", actor="user"):
    if kind not in INTERACTION_KINDS:
        raise ValueError(f"סוג אינטראקציה לא מוכר: {kind}")
    cur = conn.execute(
        "INSERT INTO interactions(tenant_id, lead_id, kind, text, actor, created) VALUES (?,?,?,?,?,?)",
        (tenant_id, lead_id, kind, text, actor, _now()))
    conn.commit()
    audit.log(conn, tenant_id, actor, "crm.interaction", {"lead": lead_id, "kind": kind})
    return cur.lastrowid


def list_interactions(conn, tenant_id, lead_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM interactions WHERE tenant_id=? AND lead_id=? ORDER BY id", (tenant_id, lead_id))]


def add_note(conn, tenant_id, lead_id, text, actor="user"):
    if not text or not text.strip():
        raise ValueError("notes: טקסט ריק")
    cur = conn.execute(
        "INSERT INTO notes(tenant_id, lead_id, text, actor, created) VALUES (?,?,?,?,?)",
        (tenant_id, lead_id, text, actor, _now()))
    conn.commit()
    audit.log(conn, tenant_id, actor, "crm.note", {"lead": lead_id})
    return cur.lastrowid


def list_notes(conn, tenant_id, lead_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM notes WHERE tenant_id=? AND lead_id=? ORDER BY id", (tenant_id, lead_id))]


# ---------------------------------------------------------------- 56/57: assignment

def assign(conn, tenant_id, lead_id, assignee=None, actor="user"):
    """Upserts the current assignee and appends to assignment_history (never overwritten -
    section 57's "ownership history"). Emits crm.lead_assigned so module G's event-triggered
    workflows (section 74/76) can react without this file importing automation/ at all."""
    assignee = assignee or DEFAULT_OWNER
    now = _now()
    conn.execute(
        "INSERT INTO assignments(tenant_id, lead_id, assignee, updated) VALUES (?,?,?,?)"
        " ON CONFLICT(tenant_id, lead_id) DO UPDATE SET assignee=excluded.assignee, updated=excluded.updated",
        (tenant_id, lead_id, assignee, now))
    conn.execute(
        "INSERT INTO assignment_history(tenant_id, lead_id, assignee, actor, created) VALUES (?,?,?,?,?)",
        (tenant_id, lead_id, assignee, actor, now))
    conn.commit()
    audit.log(conn, tenant_id, actor, "crm.assign", {"lead": lead_id, "assignee": assignee})
    from ..core import events
    events.emit(conn, tenant_id, "crm.lead_assigned", {"lead_id": lead_id, "assignee": assignee})
    return assignee


def current_assignee(conn, tenant_id, lead_id):
    row = conn.execute("SELECT assignee FROM assignments WHERE tenant_id=? AND lead_id=?",
                       (tenant_id, lead_id)).fetchone()
    return row["assignee"] if row else None


def assignment_history(conn, tenant_id, lead_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM assignment_history WHERE tenant_id=? AND lead_id=? ORDER BY id",
        (tenant_id, lead_id))]


# ---------------------------------------------------------------- 58: reminders

def add_reminder(conn, tenant_id, lead_id, due, text="", actor="user"):
    cur = conn.execute(
        "INSERT INTO reminders(tenant_id, lead_id, due, text, actor, created) VALUES (?,?,?,?,?,?)",
        (tenant_id, lead_id, due, text, actor, _now()))
    conn.commit()
    audit.log(conn, tenant_id, actor, "crm.reminder.create", {"lead": lead_id, "due": due})
    return cur.lastrowid


def due_reminders(conn, tenant_id, now=None, include_done=False):
    now = now if now is not None else _now()
    q = "SELECT * FROM reminders WHERE tenant_id=? AND due<=?"
    args = [tenant_id, now]
    if not include_done:
        q += " AND done=0"
    q += " ORDER BY due"
    return [dict(r) for r in conn.execute(q, args)]


def mark_reminder_done(conn, tenant_id, reminder_id, actor="user"):
    n = conn.execute("UPDATE reminders SET done=1 WHERE tenant_id=? AND id=?",
                     (tenant_id, reminder_id)).rowcount
    conn.commit()
    if n:
        audit.log(conn, tenant_id, actor, "crm.reminder.done", {"reminder": reminder_id})
    return bool(n)


# ---------------------------------------------------------------- 52/53: statuses, pipeline

def pipeline_stages(conn, tenant_id):
    """Customizable LABELS for leads.STATUSES, read from settings (core.config) - the
    validated status enum itself stays leads.STATUSES (leads.set_status's own check);
    this only controls what the admin UI/pipeline calls each stage. Falls back to a
    sensible Hebrew default when nothing is configured."""
    from .. import leads as leads_mod
    from ..core import config
    raw = config.get(conn, tenant_id, "pipeline_stage_labels", None)
    default = {
        "new": "חדש", "contacted": "בטיפול", "sold": "נסגר - נמכר",
        "won": "נסגר - הצליח", "lost": "נסגר - לא הצליח", "spam": "ספאם", "duplicate": "כפילות",
    }
    labels = default
    if raw:
        try:
            custom = json.loads(raw)
            labels = {**default, **custom}
        except (ValueError, TypeError):
            pass
    return [{"status": s, "label": labels.get(s, s)} for s in leads_mod.STATUSES]


def set_pipeline_stage_labels(conn, tenant_id, labels):
    from ..core import config
    config.set(conn, tenant_id, "pipeline_stage_labels", json.dumps(labels, ensure_ascii=False))


def pipeline_counts(conn, tenant_id, include_demo=False):
    """Section 53's DATA half - counts per status, in leads.STATUSES order. The admin UI
    (round-4 brief, section 5) renders this as the visual pipeline."""
    from .. import leads as leads_mod
    q = "SELECT status, COUNT(*) AS n FROM leads WHERE tenant_id=?"
    args = [tenant_id]
    if not include_demo:
        q += " AND is_demo=0"
    q += " GROUP BY status"
    counts = {r["status"]: r["n"] for r in conn.execute(q, args)}
    return [{"status": s, "label": next((x["label"] for x in pipeline_stages(conn, tenant_id) if x["status"] == s), s),
             "count": counts.get(s, 0)} for s in leads_mod.STATUSES]


# ---------------------------------------------------------------- 51: full lead profile

def _deals_for_lead(conn, tenant_id, lead_id):
    # Direct query, not brokerage.match_history: that function returns "match.shown" EVENT
    # records (section 105's proposal history), not rows from the `deals` table itself -
    # a lead profile needs the actual proposed/closed deals, a different, narrower thing.
    return [dict(r) for r in conn.execute(
        "SELECT * FROM deals WHERE tenant_id=? AND lead_id=? ORDER BY id", (tenant_id, lead_id))]


def lead_profile(conn, tenant_id, lead_id):
    """lead + interactions + notes + deal history + reminders + current assignee - one call
    for the admin UI's lead-detail view instead of five."""
    lead = leads.get(conn, tenant_id, lead_id)
    if not lead:
        return None
    return {
        "lead": lead,
        "interactions": list_interactions(conn, tenant_id, lead_id),
        "notes": list_notes(conn, tenant_id, lead_id),
        "deals": _deals_for_lead(conn, tenant_id, lead_id),
        "reminders": [dict(r) for r in conn.execute(
            "SELECT * FROM reminders WHERE tenant_id=? AND lead_id=? ORDER BY due", (tenant_id, lead_id))],
        "assignee": current_assignee(conn, tenant_id, lead_id),
        "assignment_history": assignment_history(conn, tenant_id, lead_id),
    }


# ---------------------------------------------------------------- 59: segments

# The ONLY filter keys a saved segment may use - run_segment builds parameterized SQL from
# this whitelist, never from a free-form query string, so a segment can never become an
# injection vector no matter who is allowed to create one.
_SEGMENT_FIELDS = {
    "status": ("status = ?", lambda v: v),
    "min_score": ("score_total >= ?", lambda v: int(v)),
    "service": ("service = ?", lambda v: v),
    "area": ("area = ?", lambda v: v),
}


def save_segment(conn, tenant_id, name, query, actor="user"):
    bad = [k for k in query if k not in _SEGMENT_FIELDS]
    if bad:
        raise ValueError(f"segments: שדות לא מוכרים: {', '.join(bad)}")
    cur = conn.execute(
        "INSERT INTO segments(tenant_id, name, query_json, created) VALUES (?,?,?,?)"
        " ON CONFLICT(tenant_id, name) DO UPDATE SET query_json=excluded.query_json",
        (tenant_id, name, json.dumps(query, ensure_ascii=False), _now()))
    conn.commit()
    audit.log(conn, tenant_id, actor, "crm.segment.save", {"name": name, "query": query})
    return cur.lastrowid


def list_segments(conn, tenant_id):
    return [dict(r, query=json.loads(r["query_json"])) for r in
            conn.execute("SELECT * FROM segments WHERE tenant_id=? ORDER BY name", (tenant_id,))]


def run_segment(conn, tenant_id, name, include_demo=False):
    row = conn.execute("SELECT query_json FROM segments WHERE tenant_id=? AND name=?",
                       (tenant_id, name)).fetchone()
    if not row:
        return None
    query = json.loads(row["query_json"])
    clauses = ["tenant_id=?"]
    args = [tenant_id]
    if not include_demo:
        clauses.append("is_demo=0")
    for key, value in query.items():
        sql, cast = _SEGMENT_FIELDS[key]
        clauses.append(sql)
        args.append(cast(value))
    rows = conn.execute(f"SELECT * FROM leads WHERE {' AND '.join(clauses)} ORDER BY id DESC", args)
    return [leads.get(conn, tenant_id, r["id"]) for r in rows]


# ---------------------------------------------------------------- 60: relationship history

def relationship_history(conn, tenant_id, phone):
    """Every lead ever submitted from this phone, plus every interaction/note on each of
    those leads, plus any deal history where this phone is the supplier, plus the partner
    record if any - merged into one chronological timeline. "Full... relationship
    histories": the honest cross-section, not a single table."""
    phone = normalize_phone(phone)
    lead_rows = conn.execute(
        "SELECT id FROM leads WHERE tenant_id=? AND phone=? ORDER BY id", (tenant_id, phone)).fetchall()
    timeline = []
    for r in lead_rows:
        lead = leads.get(conn, tenant_id, r["id"])
        timeline.append({"ts": lead["created"], "kind": "lead", "data": lead})
        for i in list_interactions(conn, tenant_id, r["id"]):
            timeline.append({"ts": i["created"], "kind": "interaction", "data": i})
        for n in list_notes(conn, tenant_id, r["id"]):
            timeline.append({"ts": n["created"], "kind": "note", "data": n})
    supplier = conn.execute("SELECT id FROM suppliers WHERE tenant_id=? AND phone=?",
                            (tenant_id, phone)).fetchone()
    if supplier:
        for deal in conn.execute("SELECT * FROM deals WHERE tenant_id=? AND supplier_id=? ORDER BY id",
                                 (tenant_id, supplier["id"])):
            timeline.append({"ts": deal["created"] or 0, "kind": "deal", "data": dict(deal)})
    partner_row = conn.execute("SELECT * FROM partners WHERE tenant_id=? AND phone=?",
                               (tenant_id, phone)).fetchone()
    if partner_row:
        timeline.append({"ts": partner_row["created"], "kind": "partner", "data": dict(partner_row)})
    timeline.sort(key=lambda x: x["ts"] or 0)
    contact = get_contact(conn, tenant_id, phone)
    return {"phone": phone, "contact": contact, "timeline": timeline}


# ---------------------------------------------------------------- admin HTTP surface

# Query params, not path segments: nexus_master.api.App's route() does exact-string
# matching only (no "/crm/lead/<id>" capture) - every id/phone here travels as ?id=/
# ?phone=, read via query.get(key, [None])[0] (parse_qs's own list-per-key shape).
def _qget(query, key, default=None):
    v = (query or {}).get(key)
    return v[0] if v else default


def routes(app):
    def pipeline(conn, tenant_id, body, query):
        return 200, {"counts": pipeline_counts(conn, tenant_id), "stages": pipeline_stages(conn, tenant_id)}

    def reminders_due(conn, tenant_id, body, query):
        return 200, {"reminders": due_reminders(conn, tenant_id)}

    def partners_overview_route(conn, tenant_id, body, query):
        return 200, {"partners": partners_overview(conn, tenant_id, kind=_qget(query, "kind"))}

    def lead_profile_route(conn, tenant_id, body, query):
        lid = _qget(query, "id")
        if lid is None:
            return 400, {"error": "missing id"}
        profile = lead_profile(conn, tenant_id, int(lid))
        return (200, profile) if profile else (404, {"error": "not found"})

    def relationship_route(conn, tenant_id, body, query):
        phone = _qget(query, "phone")
        if not phone:
            return 400, {"error": "missing phone"}
        return 200, relationship_history(conn, tenant_id, phone)

    def assign_route(conn, tenant_id, body, query):
        return 200, {"assignee": assign(conn, tenant_id, int(body["lead_id"]), body.get("assignee"), actor="admin")}

    def note_route(conn, tenant_id, body, query):
        return 201, {"id": add_note(conn, tenant_id, int(body["lead_id"]), body["text"], actor="admin")}

    def interaction_route(conn, tenant_id, body, query):
        return 201, {"id": log_interaction(conn, tenant_id, int(body["lead_id"]), body["kind"],
                                           body.get("text", ""), actor="admin")}

    def reminder_route(conn, tenant_id, body, query):
        return 201, {"id": add_reminder(conn, tenant_id, int(body["lead_id"]), float(body["due"]),
                                        body.get("text", ""), actor="admin")}

    def reminder_done_route(conn, tenant_id, body, query):
        return 200, {"ok": mark_reminder_done(conn, tenant_id, int(body["reminder_id"]), actor="admin")}

    app.add_route("GET", "/crm/pipeline", pipeline)
    app.add_route("GET", "/crm/reminders/due", reminders_due)
    app.add_route("GET", "/crm/partners/overview", partners_overview_route)
    app.add_route("GET", "/crm/lead_profile", lead_profile_route)
    app.add_route("GET", "/crm/relationship", relationship_route)
    app.add_route("POST", "/crm/assign", assign_route)
    app.add_route("POST", "/crm/note", note_route)
    app.add_route("POST", "/crm/interaction", interaction_route)
    app.add_route("POST", "/crm/reminder", reminder_route)
    app.add_route("POST", "/crm/reminder/done", reminder_done_route)
