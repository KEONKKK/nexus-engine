"""Source attribution: which campaign link (#ref) brought each lead and how much money it made."""
import re

KNOWN_CAMPAIGNS = ("wa_friends", "fb_biz", "fb_pro", "status")
_REF = re.compile(r"(?:#|ref=|מקור:\s*)([A-Za-z0-9_\-]{2,40})")


def parse_ref(value):
    """'https://x/page#fb_biz' / 'ref=fb_biz' / 'מקור: fb_biz' / 'fb_biz' → 'fb_biz'."""
    if not value:
        return None
    m = _REF.search(str(value))
    if m:
        return m.group(1)
    v = str(value).strip()
    return v if re.fullmatch(r"[A-Za-z0-9_\-]{2,40}", v) else None


def report(conn, tenant_id):
    """Per source: leads, sold/won, revenue from lead sales and from closed deals."""
    rows = conn.execute("""
        SELECT l.source AS source, COUNT(*) AS leads,
               SUM(l.status IN ('sold','won')) AS converted,
               IFNULL((SELECT SUM(g.amount) FROM ledger g JOIN leads x ON x.id=g.lead_id
                       WHERE g.kind='sale' AND x.source=l.source AND x.tenant_id=l.tenant_id
                       AND x.is_demo=0),0) AS lead_sales,
               IFNULL((SELECT SUM(d.commission) FROM deals d JOIN leads y ON y.id=d.lead_id
                       WHERE d.status='closed' AND y.source=l.source AND y.tenant_id=l.tenant_id
                       AND y.is_demo=0),0) AS commissions
        FROM leads l WHERE l.tenant_id=? AND l.is_demo=0 AND l.status NOT IN ('spam','duplicate')
        GROUP BY l.source ORDER BY leads DESC""", (tenant_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["lead_sales"] = -d["lead_sales"]  # sales are stored as debits from buyer credit
        d["revenue"] = d["lead_sales"] + d["commissions"]
        d["conversion_pct"] = round(100 * (d["converted"] or 0) / d["leads"], 1) if d["leads"] else 0
        out.append(d)
    return out


def campaign_link(base_url, code):
    return f"{base_url.split('#')[0]}#{code}"
