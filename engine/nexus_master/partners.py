"""Partner (buyer/supplier) self-registration: public submissions (engine/public/public_api.py
POST /public/partners) land here as 'pending' and require an explicit admin command
("אשר שותף <id>") to become a real buyers/suppliers row. Credit loading stays manual
(Bit/PayBox -> "טען קונה <id> <amount>") -- no payment processing here or anywhere else
in this project."""
import time

from . import audit, brokerage, buyers

KINDS = ("buyer", "supplier")


def register(conn, tenant_id, *, name, phone, services="", areas="", kind, consent):
    cur = conn.execute(
        "INSERT INTO partners(tenant_id, created, name, phone, services, areas, kind, consent, status)"
        " VALUES (?,?,?,?,?,?,?,?,'pending')",
        (tenant_id, time.time(), name, phone, services, areas, kind, 1 if consent else 0))
    conn.commit()
    pid = cur.lastrowid
    audit.log(conn, tenant_id, "public", "partner.register", {"partner": pid, "kind": kind})
    return pid


def get(conn, tenant_id, partner_id):
    row = conn.execute("SELECT * FROM partners WHERE tenant_id=? AND id=?",
                       (tenant_id, partner_id)).fetchone()
    return dict(row) if row else None


def list_pending(conn, tenant_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM partners WHERE tenant_id=? AND status='pending' ORDER BY id", (tenant_id,))]


def set_notify_link(conn, tenant_id, partner_id, link):
    conn.execute("UPDATE partners SET notify_link=? WHERE tenant_id=? AND id=?",
                 (link, tenant_id, partner_id))
    conn.commit()


def approve(conn, tenant_id, partner_id, actor="user"):
    """Pending -> approved, creating the real buyers/suppliers row. Returns the new row's
    id, or None if the partner does not exist or is not pending (no double-activation)."""
    p = get(conn, tenant_id, partner_id)
    if not p or p["status"] != "pending":
        return None
    if p["kind"] == "buyer":
        new_id = buyers.add_buyer(conn, tenant_id, p["name"], p["phone"], p["services"], p["areas"])
    else:
        new_id = brokerage.add_supplier(conn, tenant_id, p["name"], p["phone"], p["services"], p["areas"])
    conn.execute("UPDATE partners SET status='approved' WHERE tenant_id=? AND id=?", (tenant_id, partner_id))
    conn.commit()
    audit.log(conn, tenant_id, actor, "partner.approve",
             {"partner": partner_id, "kind": p["kind"], "new_id": new_id})
    return new_id
