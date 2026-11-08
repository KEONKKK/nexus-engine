"""AI sales intelligence (NIGUN-134 module F, sections 61-72).

Section 61 ("build an AI sales assistant") is this whole package acting together --
assist() below is the one call that ties next-best-action, a drafted message, and
objection handling into a single recommendation for a human seller to act on.

Section 72 ("human approval before automated external messaging"): structurally
enforced, not just promised -- no file in this package imports
nexus_master.whatsapp.send_manual, notify_business_new_lead, or CloudAPI.send (grep
confirms it; test_sales_ai.py:TestNeverSendsDirectly asserts it too). Every function
here returns text or a recommendation; sending still goes through the existing
guardian.check_send / whatsapp.send_manual / CloudAPI gates, untouched.
"""
from . import drafts, needs, next_action, objections, scripts, summary
from .next_action import next_best_action


def assist(conn, tenant_id, lead, interactions=None, business="העסק", lang="he"):
    """One bundled recommendation: next action, a ready-to-review draft for it (when the
    action is a message), objection responses for anything the customer already said,
    and a conversation summary when interactions are supplied."""
    action = next_best_action(conn, tenant_id, lead)
    draft = None
    if action["action"] == "send_first_contact":
        draft = drafts.first_contact_draft(lead, business=business, lang=lang)
    elif action["action"] == "follow_up":
        draft = drafts.follow_up_draft(lead, business=business, lang=lang)
    out = {"action": action, "draft": draft,
          "objections": objections.suggest_responses(lead.get("text", ""))}
    if interactions is not None:
        out["summary"] = summary.summarize_conversation(interactions)
    return out


def routes(app):
    def _assist(conn, tid, body, query):
        from .. import leads
        lead = leads.get(conn, tid, int(body["lead_id"]))
        if not lead:
            return 404, {"error": "ליד לא נמצא"}
        return 200, assist(conn, tid, lead, interactions=body.get("interactions"),
                          business=body.get("business", "העסק"), lang=body.get("lang", "he"))

    def _script(conn, tid, body, query):
        scenario = query.get("scenario", ["opening"])[0]
        return 200, {"scenario": scenario, "text": scripts.get_script(conn, tid, scenario)}

    app.add_route("POST", "/sales-ai/assist", _assist)
    app.add_route("GET", "/sales-ai/script", _script)


COMMANDS = {
    r"(?:ייעץ ליד|assist)\s+(\d+)$": lambda conn, tid, m: _command_assist(conn, tid, m),
}


def _command_assist(conn, tid, m):
    from .. import leads
    lead = leads.get(conn, tid, int(m.group(1)))
    if not lead:
        return {"ok": False, "message": "ליד לא נמצא"}
    return {"ok": True, "data": assist(conn, tid, lead)}
