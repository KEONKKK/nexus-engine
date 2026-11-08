"""Geo/category/intent filtering on top of the existing scoring engine (sections 21-23).

Deliberately does not re-implement area/service detection -- scoring.detect_area and
scoring.detect_service are the one place those rules live (nexus_master.scoring is
module D, owned by the other window; this module only calls it)."""
from .. import scoring

#: section 17-20's four "demand signal" categories, expressed as extra keyword sets on
#: top of scoring.INTENT -- not separate connectors, just a tag applied to whatever a
#: connector already fetched, documented together because a single candidate text can
#: legitimately match more than one.
DEMAND_SIGNAL_KEYWORDS = {
    "seeking_service": scoring.INTENT,
    "new_business_opening": ["נפתח", "פתחנו", "עסק חדש", "פתיחה חגיגית", "בקרוב נפתח"],
    "commercial_opportunity": ["מכרז", "דרוש קבלן", "דרושים ספקים", "הזמנה להציע הצעות"],
}


def classify_signals(text):
    """Sections 17-20: which demand-signal categories does `text` match? A plain list,
    not a single label -- "דרוש קבלן לפתיחת עסק חדש" legitimately matches two."""
    t = (text or "").lower()
    return [label for label, words in DEMAND_SIGNAL_KEYWORDS.items()
            if any(w.lower() in t for w in words)]


def apply_tenant_filters(candidates, config):
    """Sections 21-23. `config` may carry `areas` (list of scoring.AREAS entries) and/or
    `services` (list of scoring.SERVICES keys) to restrict a source to what this tenant
    actually wants; absent filters pass everything through unfiltered rather than
    silently dropping candidates nobody asked to restrict."""
    areas = set((config or {}).get("areas") or [])
    services = set((config or {}).get("services") or [])
    out = []
    for cand in candidates:
        parsed = scoring.parse_text(cand.get("text", ""))
        if areas and parsed["area"] not in areas:
            continue
        if services and parsed["service"] not in services:
            continue
        out.append(cand)
    return out
