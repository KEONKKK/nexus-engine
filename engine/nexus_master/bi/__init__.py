"""Revenue & business intelligence (NIGUN-134 module J, sections 107-116).

Does not re-implement revenue accounting: every number here is read from the existing
ledger/deals/kits tables via the existing buyers.revenue / brokerage.commissions_total /
kits.kit_revenue / attribution.report, combined and re-labeled. The one rule repeated
everywhere in this file, per the instruction that created this module: a projected
number is never merged into, or returned under the same key as, a received one.
"""
import time

from . import commission
from .commission import get_default_commission_pct, set_default_commission_pct


def revenue_dashboard(conn, tenant_id):
    """Section 107 (+112's separation made explicit at the top level)."""
    from .. import attribution, brokerage, buyers, kits
    rev = buyers.revenue(conn, tenant_id)
    return {
        "received": {
            "buyer_topups": rev["received"],
            "lead_sales_delivered_value": rev["delivered_value"],
            "commissions_closed": brokerage.commissions_total(conn, tenant_id),
            "kits_sold": kits.kit_revenue(conn, tenant_id),
        },
        "projected": {
            "commissions_open_deals": commission.projected_commission(conn, tenant_id),
        },
        "by_source": attribution.report(conn, tenant_id),
    }


def conversion_rate(conn, tenant_id):
    """Section 108. Tenant-wide lead -> customer conversion (won/sold over all non-demo,
    non-spam/duplicate leads) -- attribution.report already computes this per-source;
    this is the same definition rolled up across all sources."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(status IN ('sold','won')) AS converted FROM leads"
        " WHERE tenant_id=? AND is_demo=0 AND status NOT IN ('spam','duplicate')",
        (tenant_id,)).fetchone()
    total, converted = row["total"], row["converted"] or 0
    return {"total": total, "converted": converted,
           "conversion_pct": round(100 * converted / total, 1) if total else 0}


def deal_values(conn, tenant_id):
    """Section 109."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, IFNULL(SUM(amount),0) AS total, IFNULL(AVG(amount),0) AS avg"
        " FROM deals WHERE tenant_id=? AND status='closed'", (tenant_id,)).fetchone()
    return {"closed_deals": row["n"], "total_amount": row["total"], "avg_amount": round(row["avg"], 2)}


def revenue_by_salesperson(conn, tenant_id):
    """Section 114. `leads`/`deals` have no assignee column yet (module E, section 56
    "assign leads to team members" -- owned elsewhere, not yet built). Returns an
    explicit not-available marker rather than fabricating a breakdown with no underlying
    data; re-check pragma_table_info so this starts working the moment that column lands,
    with no code change needed here."""
    cols = {r["name"] for r in conn.execute("SELECT name FROM pragma_table_info('leads')")}
    if "assignee" not in cols:
        return {"available": False,
               "why": "אין עדיין עמודת assignee בטבלת leads (תלוי במודול E, סעיף 56)"}
    rows = conn.execute(  # pragma: no cover -- unreachable until the column exists
        "SELECT assignee, COUNT(*) AS leads, SUM(status IN ('sold','won')) AS won"
        " FROM leads WHERE tenant_id=? AND is_demo=0 GROUP BY assignee", (tenant_id,))
    return {"available": True, "by_salesperson": [dict(r) for r in rows]}


def supplier_performance(conn, tenant_id):
    """Section 115."""
    rows = conn.execute("""
        SELECT s.id, s.name, s.rating,
               COUNT(d.id) AS deals_proposed,
               SUM(d.status='closed') AS deals_closed,
               IFNULL(SUM(CASE WHEN d.status='closed' THEN d.commission END),0) AS commission_total
        FROM suppliers s LEFT JOIN deals d ON d.supplier_id=s.id AND d.tenant_id=s.tenant_id
        WHERE s.tenant_id=? AND s.is_demo=0 GROUP BY s.id ORDER BY commission_total DESC""",
        (tenant_id,))
    return [dict(r) for r in rows]


def profitability_report(conn, tenant_id):
    """Section 116. Revenue breakdown only -- this project records no cost/expense data
    anywhere (no cost column on any table), so "profitability" cannot subtract expenses
    it was never given; that limitation is stated here rather than a fabricated cost
    figure being invented to make the word "profitability" literally true."""
    dash = revenue_dashboard(conn, tenant_id)
    total_received = sum(dash["received"].values())
    return {"revenue_received_total": total_received, "breakdown": dash["received"],
           "note": "דוח הכנסות בלבד — אין נתוני-עלות בשום טבלה כיום, אז לא מוצג רווח נקי"}


def routes(app):
    def _dashboard(conn, tid, body, query):
        return 200, revenue_dashboard(conn, tid)

    def _conversion(conn, tid, body, query):
        return 200, conversion_rate(conn, tid)

    def _supplier_perf(conn, tid, body, query):
        return 200, supplier_performance(conn, tid)

    def _profitability(conn, tid, body, query):
        return 200, profitability_report(conn, tid)

    app.add_route("GET", "/bi/dashboard", _dashboard)
    app.add_route("GET", "/bi/conversion", _conversion)
    app.add_route("GET", "/bi/supplier-performance", _supplier_perf)
    app.add_route("GET", "/bi/profitability", _profitability)


COMMANDS = {
    r"(?:דוח הכנסות|revenue report)$": lambda conn, tid, m: (
        {"ok": True, "data": revenue_dashboard(conn, tid)}),
}
