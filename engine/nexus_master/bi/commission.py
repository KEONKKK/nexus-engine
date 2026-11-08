"""Sections 110-111: configurable commission rules and expected (projected) commission
for deals that have not closed yet -- an open ('proposed') deal has no real `amount` in
the deals table (brokerage.close_deal is what fills it in, only on close), so any number
here is necessarily an estimate off historical closed-deal values, never a real figure.
Every function/key here says "projected"/"estimate" -- section 112's rule."""
from .. import db

_SETTINGS_KEY = "bi_default_commission_pct"
_DEFAULT_PCT = 10.0  # matches brokerage.add_supplier's own hardcoded default


def get_default_commission_pct(conn, tenant_id):
    return float(db.get_setting(conn, tenant_id, _SETTINGS_KEY, _DEFAULT_PCT))


def set_default_commission_pct(conn, tenant_id, pct):
    if pct < 0 or pct > 100:
        raise ValueError("עמלה חייבת להיות בין 0 ל-100 אחוז")
    db.set_setting(conn, tenant_id, _SETTINGS_KEY, pct)


def _avg_closed_deal_amount(conn, tenant_id):
    row = conn.execute("SELECT AVG(amount) FROM deals WHERE tenant_id=? AND status='closed'",
                       (tenant_id,)).fetchone()
    return row[0] or 0.0


def projected_commission(conn, tenant_id):
    """Section 111. Sum, over every still-open ('proposed') deal, of
    (that deal's supplier commission_pct, falling back to the tenant default) times the
    tenant's own historical average closed-deal amount (0 when there is no history yet --
    not invented). Explicitly an estimate: these deals have no real amount until closed."""
    default_pct = get_default_commission_pct(conn, tenant_id)
    avg_amount = _avg_closed_deal_amount(conn, tenant_id)
    rows = conn.execute(
        "SELECT d.id, s.commission_pct FROM deals d JOIN suppliers s ON s.id=d.supplier_id"
        " WHERE d.tenant_id=? AND d.status='proposed'", (tenant_id,))
    total = 0.0
    per_deal = []
    for r in rows:
        pct = r["commission_pct"] if r["commission_pct"] is not None else default_pct
        estimate = round(avg_amount * pct / 100, 2)
        total += estimate
        per_deal.append({"deal_id": r["id"], "estimated_commission": estimate})
    return {"total_estimate": round(total, 2), "based_on_avg_closed_deal": round(avg_amount, 2),
           "open_deals": per_deal,
           "note": "הערכה בלבד — העסקאות עדיין פתוחות, אין סכום אמיתי עד סגירה"}
