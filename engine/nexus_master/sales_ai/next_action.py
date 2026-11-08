"""Section 68: next-best-action rules, in the order given by the instruction that
created this module ("no phone -> ask; hot + no match -> search supplier; ..."), plus
the terminal/opt-out states every other rule must be checked against first."""
from .. import brokerage

HOT_SCORE = 70


def next_best_action(conn, tenant_id, lead):
    if lead.get("status") in ("duplicate", "spam", "lost"):
        return {"action": "no_action", "why": f"סטטוס {lead['status']} — לא פעולה נוספת"}
    if lead.get("status") in ("sold", "won"):
        return {"action": "no_action", "why": "הליד נסגר"}
    if not lead.get("phone"):
        return {"action": "ask_for_phone", "why": "אין מספר טלפון לליד"}
    matches = brokerage.match_lead(conn, tenant_id, lead)
    is_hot = (lead.get("score_total") or 0) >= HOT_SCORE
    if is_hot and not matches:
        return {"action": "search_supplier", "why": "ליד חם בלי התאמה קיימת — דרוש ספק חדש באזור/תחום"}
    if matches:
        return {"action": "propose_match", "why": f"יש {len(matches)} התאמות",
               "supplier_id": matches[0]["supplier_id"]}
    if lead.get("status") == "new":
        return {"action": "send_first_contact", "why": "ליד חדש, עדיין לא נוצר קשר"}
    return {"action": "follow_up", "why": f"סטטוס {lead.get('status')} בלי התאמה — מעקב"}
