"""Command center: short Hebrew/English commands → actions."""
import re

from . import attribution, brokerage, buyers, leads, partners


def dashboard(conn, tenant_id):
    q = lambda sql: conn.execute(sql, (tenant_id,)).fetchone()[0]
    from .kits import kit_revenue
    rev = buyers.revenue(conn, tenant_id)
    return {
        "leads_new": q("SELECT COUNT(*) FROM leads WHERE tenant_id=? AND status='new' AND is_demo=0"),
        "leads_total": q("SELECT COUNT(*) FROM leads WHERE tenant_id=? AND is_demo=0"
                         " AND status NOT IN ('spam','duplicate')"),
        "duplicates": q("SELECT COUNT(*) FROM leads WHERE tenant_id=? AND is_demo=0 AND status='duplicate'"),
        "hot_leads": q("SELECT COUNT(*) FROM leads WHERE tenant_id=? AND status='new' AND score_total>=70 AND is_demo=0"),
        "buyers": q("SELECT COUNT(*) FROM buyers WHERE tenant_id=? AND active=1"),
        "suppliers": q("SELECT COUNT(*) FROM suppliers WHERE tenant_id=? AND is_demo=0"),
        "money_received_from_buyers": rev["received"],
        "commissions_closed": brokerage.commissions_total(conn, tenant_id),
        "kits_sold": kit_revenue(conn, tenant_id),
        # round 4: notify_link is composed on every successful public lead (see
        # whatsapp.notify_business_new_lead) but never auto-marked clicked -- a wa.me
        # link is opened by a human, there is no delivery webhook to watch instead.
        "pending_notifications": [
            {"lead_id": r["id"], "notify_link": r["notify_link"]}
            for r in conn.execute(
                "SELECT id, notify_link FROM leads WHERE tenant_id=? AND notify_link IS NOT NULL"
                " AND notify_clicked=0 AND is_demo=0 ORDER BY id DESC", (tenant_id,))
        ],
        "pending_partners": partners.list_pending(conn, tenant_id),
    }


HELP = ("פקודות: 'לידים חמים' · 'לוח' · 'מקורות' · 'התאם ליד 5' · 'מכור ליד 5 לקונה 2' · "
        "'סגור עסקה 3 1500' · 'סמן ליד 5 lost' · 'הוסף ליד <טקסט>' · 'אשר שותף 3' · "
        "'טען קונה 3 200' · 'סמן התראה 5 נלחצה'")


def run(conn, tenant_id, text):
    t = (text or "").strip()
    if not t or t in ("עזרה", "help"):
        return {"ok": True, "message": HELP}
    if t in ("לוח", "dashboard", "סיכום"):
        return {"ok": True, "data": dashboard(conn, tenant_id)}
    if t in ("לידים חמים", "hot"):
        hot = leads.hot_list(conn, tenant_id)
        return {"ok": True, "data": [{"id": l["id"], "score": l["score_total"], "service": l["service"],
                                       "area": l["area"]} for l in hot]}
    if t in ("מקורות", "sources"):
        return {"ok": True, "data": attribution.report(conn, tenant_id)}
    m = re.match(r"(?:התאם ליד|match)\s+(\d+)$", t)
    if m:
        lead = leads.get(conn, tenant_id, int(m.group(1)))
        if not lead:
            return {"ok": False, "message": "ליד לא נמצא"}
        return {"ok": True, "data": brokerage.match_lead(conn, tenant_id, lead)}
    m = re.match(r"(?:מכור ליד|sell)\s+(\d+)\s+(?:לקונה|to)\s+(\d+)$", t)
    if m:
        try:
            bal = buyers.sell_lead(conn, tenant_id, int(m.group(2)), int(m.group(1)))
            return {"ok": True, "message": f"נמכר. יתרת קונה: ₪{bal}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}
    m = re.match(r"(?:סגור עסקה|close)\s+(\d+)\s+(\d+)$", t)
    if m:
        try:
            c = brokerage.close_deal(conn, tenant_id, int(m.group(1)), int(m.group(2)))
            return {"ok": True, "message": f"נסגר. עמלה: ₪{c}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}
    m = re.match(r"(?:סמן ליד|status)\s+(\d+)\s+(\w+)$", t)
    if m:
        try:
            ok = leads.set_status(conn, tenant_id, int(m.group(1)), m.group(2))
            return {"ok": ok, "message": "עודכן" if ok else "ליד לא נמצא"}
        except ValueError as e:
            return {"ok": False, "message": str(e)}
    m = re.match(r"(?:הוסף ליד|add)\s+(.+)$", t, re.S)
    if m:
        lid, dup = leads.add_lead(conn, tenant_id, text=m.group(1), actor="command")
        if lid is None:
            return {"ok": True, "message": "המספר הוסר מרשימת התפוצה"}
        return {"ok": True, "message": f"ליד {lid} נוסף" + (f" (כפול של {dup})" if dup else ""),
                "data": leads.get(conn, tenant_id, lid)}
    m = re.match(r"(?:אשר שותף|approve partner)\s+(\d+)$", t)
    if m:
        new_id = partners.approve(conn, tenant_id, int(m.group(1)))
        if new_id is None:
            return {"ok": False, "message": "שותף לא נמצא או לא ממתין לאישור"}
        return {"ok": True, "message": f"שותף אושר, מזהה חדש: {new_id}"}
    m = re.match(r"(?:טען קונה|topup)\s+(\d+)\s+(\d+)$", t)
    if m:
        try:
            bal = buyers.top_up(conn, tenant_id, int(m.group(1)), int(m.group(2)))
            return {"ok": True, "message": f"נטען. יתרה: ₪{bal}"}
        except ValueError as e:
            return {"ok": False, "message": str(e)}
    m = re.match(r"(?:סמן התראה|notify)\s+(\d+)\s+(?:נלחצה|clicked)$", t)
    if m:
        ok = leads.mark_notify_clicked(conn, tenant_id, int(m.group(1)))
        return {"ok": ok, "message": "עודכן" if ok else "ליד לא נמצא"}
    # NIGUN-134 plugin model: each subpackage may define COMMANDS = {regex: handler},
    # tried only after every builtin command above has had a chance to match.
    from .core.registry import discover_plugins
    for plugin in discover_plugins():
        commands = getattr(plugin, "COMMANDS", None)
        if not commands:
            continue
        for pattern, handler in commands.items():
            pm = re.match(pattern, t)
            if pm:
                return handler(conn, tenant_id, pm)
    return {"ok": False, "message": "לא הבנתי. " + HELP}
