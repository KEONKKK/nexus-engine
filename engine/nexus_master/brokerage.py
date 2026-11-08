"""Brokerage: suppliers, honest keyword-coverage matching, closing a deal once with the real amount."""
import time

from . import audit, scoring
from .core import events


def add_supplier(conn, tenant_id, name, phone=None, keywords="", areas="", commission_pct=10, is_demo=False):
    cur = conn.execute("INSERT INTO suppliers(tenant_id, name, phone, keywords, areas, commission_pct, is_demo)"
                       " VALUES (?,?,?,?,?,?,?)",
                       (tenant_id, name, phone, keywords, areas, float(commission_pct), int(is_demo)))
    conn.commit()
    audit.log(conn, tenant_id, "user", "supplier.add", {"supplier": cur.lastrowid, "name": name})
    return cur.lastrowid


def _words(s):
    return [w.strip().lower() for w in (s or "").split(",") if w.strip()]


def requirements_match(lead_requirements, supplier_keywords):
    """NIGUN-134 module I section 101: explicit customer requirements (a comma string,
    separate from the free-text keyword search match_score already does on lead['text'])
    against what the supplier declares it covers. Returns (fraction_met, reason) -- every
    requirement met is a perfect 1.0, partial coverage is proportional, no requirements
    given is vacuously 1.0 (nothing to fail)."""
    reqs = _words(lead_requirements)
    if not reqs:
        return 1.0, "אין דרישות מפורשות"
    kws = set(_words(supplier_keywords))
    met = [r for r in reqs if r in kws]
    frac = round(len(met) / len(reqs), 3)
    reason = (f"כל הדרישות נענות: {', '.join(met)}" if frac == 1.0 else
              f"נענו {len(met)}/{len(reqs)} דרישות" if met else "אף דרישה לא נענתה")
    return frac, reason


def match_score(lead, supplier):
    """0..1 — share of the supplier's keywords (plus service aliases) found in the lead,
    × area, × rating. Sections 99 (availability) and 100 (price range) are gates applied
    here, not folded into the multiplicative score invisibly: an unavailable supplier is
    excluded outright (0, named reason); a price mismatch is a visible down-weight."""
    if supplier["available"] == 0:
        return 0.0, "הספק לא זמין כרגע"
    text = " ".join(str(lead.get(k) or "") for k in ("text", "service")).lower()
    kws = _words(supplier["keywords"])
    if not kws:
        return 0.0, "לספק אין מילות מפתח"
    aliases = [a.lower() for svc, al in scoring.SERVICES.items() if svc.lower() in kws for a in al]
    hits = [k for k in kws if k in text] + [a for a in aliases if a in text]
    if not hits:
        return 0.0, "אין מילים משותפות"
    coverage = min(1.0, len(set(hits)) / max(1, len(kws)) + 0.5)
    areas = _words(supplier["areas"])
    area_ok = not areas or not lead.get("area") or lead["area"].lower() in areas
    if not area_ok:
        return 0.0, "אזור לא מתאים"
    s = min(1.0, coverage * float(supplier["rating"] or 1.0))
    reason = "התאמה: " + ", ".join(sorted(set(hits))[:5])
    budget = lead.get("budget")
    price_min, price_max = supplier["price_min"], supplier["price_max"]
    if budget and (price_min is not None or price_max is not None):
        in_range = (price_min is None or budget >= price_min) and (price_max is None or budget <= price_max)
        if not in_range:
            s *= 0.5
            reason += f" (מחוץ לטווח המחירים של הספק: {price_min}-{price_max})"
    return round(s, 3), reason


def set_availability(conn, tenant_id, supplier_id, available):
    conn.execute("UPDATE suppliers SET available=? WHERE tenant_id=? AND id=?",
                 (1 if available else 0, tenant_id, supplier_id))
    conn.commit()


def set_price_range(conn, tenant_id, supplier_id, price_min=None, price_max=None):
    conn.execute("UPDATE suppliers SET price_min=?, price_max=? WHERE tenant_id=? AND id=?",
                 (price_min, price_max, tenant_id, supplier_id))
    conn.commit()


def covered_areas(conn, tenant_id):
    """Union of every active, non-demo supplier's areas -- NIGUN-134 module D section 41's
    "coverage", and module I section 98's geographic match, feed off the same set."""
    out = set()
    for row in conn.execute("SELECT areas FROM suppliers WHERE tenant_id=? AND is_demo=0", (tenant_id,)):
        out.update(_words(row["areas"]))
    return out


def match_lead(conn, tenant_id, lead, limit=3, min_score=0.3):
    out = []
    for sup in conn.execute("SELECT * FROM suppliers WHERE tenant_id=?", (tenant_id,)):
        s, why = match_score(lead, sup)
        if s >= min_score:
            out.append({"supplier_id": sup["id"], "name": sup["name"], "score": s, "why": why})
    out.sort(key=lambda m: -m["score"])
    out = out[:limit]
    # Section 105: every match actually shown is logged via the module-A event bus --
    # reusing core.events rather than a parallel history table.
    if out and lead.get("id") is not None:
        for m in out:
            events.emit(conn, tenant_id, "match.shown",
                       {"lead_id": lead["id"], "supplier_id": m["supplier_id"], "score": m["score"]})
    return out


def match_history(conn, tenant_id, lead_id=None, limit=50):
    """Section 105: past match proposals (match_lead's own emitted events), optionally
    filtered to one lead."""
    rows = events.recent(conn, tenant_id, "match.shown", limit=limit * 5 if lead_id else limit)
    if lead_id is not None:
        rows = [r for r in rows if r["data"].get("lead_id") == lead_id]
    return rows[:limit]


def propose(conn, tenant_id, lead_id, supplier_id, score=None):
    conn.execute("INSERT OR IGNORE INTO deals(tenant_id, lead_id, supplier_id, score, created)"
                 " VALUES (?,?,?,?,?)", (tenant_id, lead_id, supplier_id, score, time.time()))
    conn.commit()
    return conn.execute("SELECT id FROM deals WHERE lead_id=? AND supplier_id=?",
                        (lead_id, supplier_id)).fetchone()["id"]


def close_deal(conn, tenant_id, deal_id, amount_received):
    """Close once, with the amount the customer actually paid. Commission = supplier %."""
    d = conn.execute("SELECT d.*, s.commission_pct FROM deals d JOIN suppliers s ON s.id=d.supplier_id"
                     " WHERE d.tenant_id=? AND d.id=?", (tenant_id, deal_id)).fetchone()
    if not d:
        raise ValueError("עסקה לא קיימת")
    if d["status"] == "closed":
        raise ValueError("העסקה כבר נסגרה")
    if amount_received <= 0:
        raise ValueError("יש להזין את הסכום שהתקבל בפועל")
    commission = round(amount_received * d["commission_pct"] / 100)
    conn.execute("UPDATE deals SET status='closed', amount=?, commission=?, closed=? WHERE id=?",
                 (int(amount_received), commission, time.time(), deal_id))
    conn.execute("UPDATE leads SET status='won' WHERE id=?", (d["lead_id"],))
    _adjust_rating(conn, d["supplier_id"], 1.1)
    conn.commit()
    audit.log(conn, tenant_id, "user", "deal.close", {"deal": deal_id, "amount": int(amount_received),
                                                      "commission": commission})
    return commission


def reject_deal(conn, tenant_id, deal_id):
    d = conn.execute("SELECT * FROM deals WHERE tenant_id=? AND id=?", (tenant_id, deal_id)).fetchone()
    if not d or d["status"] == "closed":
        return False
    conn.execute("UPDATE deals SET status='rejected' WHERE id=?", (deal_id,))
    _adjust_rating(conn, d["supplier_id"], 0.9)
    conn.commit()
    audit.log(conn, tenant_id, "user", "deal.reject", {"deal": deal_id})
    return True


def _adjust_rating(conn, supplier_id, factor):
    """Learning: closes raise, rejects lower the rating, kept within 0.5–1.5."""
    conn.execute("UPDATE suppliers SET rating=MIN(1.5, MAX(0.5, rating*?)) WHERE id=?", (factor, supplier_id))


def simulate_commission(avg_job, commission_pct, deals_per_month, close_rate_pct=30):
    """What-if calculator. Clearly a simulation — not revenue."""
    closed = deals_per_month * close_rate_pct / 100
    monthly = round(avg_job * commission_pct / 100 * closed)
    return {"closed_per_month": round(closed, 1), "monthly": monthly, "yearly": monthly * 12,
            "note": "סימולציה בלבד — לא הכנסה בפועל"}


def commissions_total(conn, tenant_id):
    return conn.execute("SELECT IFNULL(SUM(commission),0) FROM deals WHERE tenant_id=? AND status='closed'",
                        (tenant_id,)).fetchone()[0]
