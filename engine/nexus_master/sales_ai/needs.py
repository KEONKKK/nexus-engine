"""Sections 65-66: needs identification and service suggestion -- both thin, deliberate
wrappers over the one real implementation of these ideas (scoring.py), per the
instruction that created this module ("identify needs = parse_text on the records";
"suggest service = from SERVICES"). No second classifier is built here."""
from .. import kits, scoring


def identify_needs(conversation_text):
    """Section 65. `conversation_text` is whatever text a caller already has (a single
    message, or interactions joined by the caller) -- this module does not read any CRM
    table directly, so it works whether that text came from module E's interactions log
    or from a single lead's own intake text."""
    return scoring.parse_text(conversation_text)


def suggest_service(text):
    """Section 66 (service side)."""
    return scoring.detect_service(text)


def suggest_kit(lead):
    """Section 66 (product side): which kits.py kind/industry combination fits this lead,
    reusing the same INDUSTRY_WORDS keys kits.build already understands -- this function
    only picks a key, kits.create_kit (existing, unedited) still does the building."""
    service = lead.get("service") or suggest_service(lead.get("text", ""))
    industry_by_service = {"יופי": "beauty", "שיפוצים": "renovation", "כושר": "fitness"}
    industry = industry_by_service.get(service, "general")
    return {"kind": "whatsapp", "industry": industry, "available_industries": list(kits.INDUSTRY_WORDS)}
