"""Sections 62-63, 70-71: first-contact / follow-up drafts, Hebrew (existing, default)
and English (new -- the actual second language this section asks for). These functions
only ever return text; nothing here sends anything (section 72, enforced structurally:
no function in this module imports nexus_master.whatsapp.send_manual or CloudAPI)."""
from .. import whatsapp

_EN_OPT_OUT = " (reply STOP to opt out)"


def first_contact_draft(lead, business="the business", lang="he"):
    """Section 62. Hebrew path reuses whatsapp.draft_to_client verbatim (already
    personalized, already carries the opt-out suffix) -- not re-implemented here."""
    if lang == "he":
        return whatsapp.draft_to_client(lead, business=business)
    if lang == "en":
        service = lead.get("service") or "your request"
        area = f" in {lead['area']}" if lead.get("area") else ""
        name = f" {lead['name']}" if lead.get("name") else ""
        return (f"Hi{name}, this is {business}. We saw you're looking for {service}{area}. "
               f"We have a professional who can help -- when's a good time to talk?{_EN_OPT_OUT}")
    raise ValueError(f"שפה לא נתמכת: {lang} (נתמך: he, en)")


def follow_up_draft(lead, business="the business", lang="he"):
    """Section 63. Hebrew text matches kits.WA_TEMPLATES[3]'s *proposed* wording from
    design/research/wa-templates-he.md (gender-neutral phrasing, a question instead of a
    first-person verb) -- not the original kits.py text, which that review already
    flagged as gendered. kits.py itself is not edited here (owned elsewhere)."""
    name = lead.get("name") or ""
    if lang == "he":
        greeting = f"היי {name}, " if name else "היי, "
        return greeting + "רק מעקב קצר — ההודעה הקודמת הגיעה? אם זה עדיין רלוונטי, אני כאן."
    if lang == "en":
        greeting = f"Hi {name}, " if name else "Hi, "
        return greeting + f"just a quick follow-up -- did our last message reach you?{_EN_OPT_OUT}"
    raise ValueError(f"שפה לא נתמכת: {lang} (נתמך: he, en)")
