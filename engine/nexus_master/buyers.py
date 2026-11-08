"""Lead buyers (businesses that buy leads) with a prepaid credit ledger. A lead is sold to a buyer once."""
import sqlite3
import time

from . import audit


class NotEnoughCredit(Exception):
    pass


def add_buyer(conn, tenant_id, name, phone=None, services="", areas="", price_per_lead=0):
    cur = conn.execute("INSERT INTO buyers(tenant_id, name, phone, services, areas, price_per_lead)"
                       " VALUES (?,?,?,?,?,?)", (tenant_id, name, phone, services, areas, int(price_per_lead)))
    conn.commit()
    audit.log(conn, tenant_id, "user", "buyer.add", {"buyer": cur.lastrowid, "name": name})
    return cur.lastrowid


def balance(conn, tenant_id, buyer_id):
    return conn.execute("SELECT IFNULL(SUM(amount),0) FROM ledger WHERE tenant_id=? AND buyer_id=?",
                        (tenant_id, buyer_id)).fetchone()[0]


def top_up(conn, tenant_id, buyer_id, amount, note="תשלום"):
    """Record a real payment received from the buyer (₪)."""
    if amount <= 0:
        raise ValueError("סכום חייב להיות חיובי")
    conn.execute("INSERT INTO ledger(tenant_id, buyer_id, ts, amount, kind, note) VALUES (?,?,?,?, 'topup', ?)",
                 (tenant_id, buyer_id, time.time(), int(amount), note))
    conn.commit()
    audit.log(conn, tenant_id, "user", "buyer.topup", {"buyer": buyer_id, "amount": int(amount)})
    return balance(conn, tenant_id, buyer_id)


def matching_buyers(conn, tenant_id, lead):
    out = []
    for b in conn.execute("SELECT * FROM buyers WHERE tenant_id=? AND active=1", (tenant_id,)):
        svc = [s.strip() for s in (b["services"] or "").split(",") if s.strip()]
        areas = [a.strip() for a in (b["areas"] or "").split(",") if a.strip()]
        if svc and lead.get("service") not in svc:
            continue
        if areas and lead.get("area") and lead["area"] not in areas:
            continue
        out.append(dict(b))
    return out


def sell_lead(conn, tenant_id, buyer_id, lead_id, price=None):
    """Deduct credit and mark the lead sold. Raises if already sold to this buyer or no credit."""
    b = conn.execute("SELECT * FROM buyers WHERE tenant_id=? AND id=?", (tenant_id, buyer_id)).fetchone()
    if not b:
        raise ValueError("קונה לא קיים")
    price = int(b["price_per_lead"] if price is None else price)
    if balance(conn, tenant_id, buyer_id) < price:
        raise NotEnoughCredit(f"אין מספיק קרדיט ל{b['name']}")
    try:
        conn.execute("INSERT INTO ledger(tenant_id, buyer_id, ts, amount, kind, lead_id, note)"
                     " VALUES (?,?,?,?, 'sale', ?, 'מכירת ליד')",
                     (tenant_id, buyer_id, time.time(), -price, lead_id))
    except sqlite3.IntegrityError:
        raise ValueError("הליד כבר נמכר לקונה הזה")
    conn.execute("UPDATE leads SET status='sold' WHERE tenant_id=? AND id=?", (tenant_id, lead_id))
    conn.commit()
    audit.log(conn, tenant_id, "user", "lead.sell", {"lead": lead_id, "buyer": buyer_id, "price": price})
    return balance(conn, tenant_id, buyer_id)


def revenue(conn, tenant_id):
    """Money actually received from buyers (top-ups) and value of leads delivered."""
    r = conn.execute("SELECT IFNULL(SUM(CASE WHEN kind='topup' THEN amount END),0),"
                     " IFNULL(-SUM(CASE WHEN kind='sale' THEN amount END),0) FROM ledger WHERE tenant_id=?",
                     (tenant_id,)).fetchone()
    return {"received": r[0], "delivered_value": r[1]}
