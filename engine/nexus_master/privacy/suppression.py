"""Section 124 (the additive half -- consent/opt-out already exist in guardian.py and
are not re-implemented here). A suppression entry is operator-driven ("never contact
this number, for this reason") and distinct from guardian.opt_out, which only ever
records the recipient's own request. check_send() (guardian.py, owned elsewhere) does
not call is_suppressed() yet -- see design/nigun/STATUS.md row 124's integration note,
same pattern as validation.py/headers.py above."""
import time

from .. import guardian


def suppress(conn, tenant_id, phone, reason, actor="privacy"):
    p = guardian.normalize_phone(phone)
    if not p:
        raise ValueError("מספר טלפון לא תקין")
    conn.execute("INSERT OR REPLACE INTO suppression(tenant_id, phone, reason, ts, actor)"
                 " VALUES (?,?,?,?,?)", (tenant_id, p, reason, time.time(), actor))
    conn.commit()
    from .. import audit
    audit.log(conn, tenant_id, actor, "suppression.add", {"phone": p, "reason": reason})
    return p


def is_suppressed(conn, tenant_id, phone):
    p = guardian.normalize_phone(phone)
    return bool(p and conn.execute("SELECT 1 FROM suppression WHERE tenant_id=? AND phone=?",
                                   (tenant_id, p)).fetchone())


def list_suppressed(conn, tenant_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM suppression WHERE tenant_id=? ORDER BY ts DESC", (tenant_id,))]
