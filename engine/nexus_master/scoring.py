"""Lead intake (free Hebrew text → fields) and 5 scores, each with a reason."""
import re

from .guardian import normalize_phone

# Profession → aliases (Hebrew + English). The key is the canonical service name.
SERVICES = {
    "אינסטלטור": ["אינסטלטור", "אינסטלציה", "נזילה", "סתימה", "דוד", "ברז", "plumber"],
    "חשמלאי": ["חשמלאי", "חשמל", "קצר", "לוח חשמל", "שקע", "electrician"],
    "שיפוצים": ["שיפוץ", "שיפוצים", "קבלן", "ריצוף", "גבס", "צבע", "צבעי", "renovation"],
    "מיזוג": ["מזגן", "מיזוג", "טכנאי מזגנים", "ac"],
    "ניקיון": ["ניקיון", "מנקה", "ניקוי", "cleaning"],
    "הובלות": ["הובלה", "הובלות", "מוביל", "moving"],
    "מנעולן": ["מנעולן", "פריצת דלת", "מנעול", "locksmith"],
    "גינון": ["גנן", "גינון", "גינה", "דשא"],
    "יופי": ["מניקור", "פדיקור", "ציפורניים", "גבות", "איפור", "קוסמטיקאית", "ריסים"],
    "כושר": ["מאמן", "מאמנת", "כושר", "אימון אישי", "פילאטיס"],
    "ערכה": ["תבניות וואטסאפ", "ערכה", "קורות חיים", "תסריטים", "תסריטי תוכן"],
}

AREAS = ["תל אביב", "ירושלים", "חיפה", "באר שבע", "פתח תקווה", "ראשון לציון", "חולון",
         "בת ים", "רמת גן", "גבעתיים", "בני ברק", "נתניה", "הרצליה", "רעננה", "כפר סבא",
         "הוד השרון", "ראש העין", "רחובות", "נס ציונה", "לוד", "רמלה", "מודיעין", "אשדוד",
         "אשקלון", "בית שמש", "אילת", "עפולה", "נהריה", "קריות", "טבריה", "צפת",
         "השרון", "המרכז", "הצפון", "הדרום", "השפלה"]

URGENT = ["דחוף", "עכשיו", "מיד", "היום", "בהקדם", "asap", "חירום", "מוצף", "נתקע"]
SOON = ["מחר", "השבוע", "בימים הקרובים", "עד סוף השבוע", "בקרוב"]
NEGATION_WORDS = ("לא", "בלי")
INTENT = ["מחפש", "מחפשת", "צריך", "צריכה", "מבקש", "מבקשת", "המלצה", "ממליצים",
          "מישהו מכיר", "הצעת מחיר", "כמה עולה", "מי יכול"]


def detect_service(text):
    t = (text or "").lower()
    best, hits = None, 0
    for service, words in SERVICES.items():
        n = sum(1 for w in words if w.lower() in t)
        if n > hits:
            best, hits = service, n
    return best


# General regions (not specific cities) written with a ב/ל prefix drop the Hebrew
# definite article ה ("ב"+"הדרום"="בדרום", not "בהדרום"); with מ it is kept ("מהדרום"),
# which already matches as a plain substring of "הדרום". Scoped to exactly these five
# names, not "any AREAS entry starting with ה" -- that would also catch "הוד השרון".
GENERAL_AREAS = ("השרון", "המרכז", "הצפון", "הדרום", "השפלה")
GENERAL_AREA_PREFIXES = ("ב", "ל", "מ")


def detect_area(text):
    # Hyphenated city names ("תל-אביב") must match the space form already in AREAS --
    # no area name in the list itself contains a hyphen, so this is one-directional.
    t = (text or "").replace("-", " ")
    for a in AREAS:
        if a in t:
            return a
    for a in GENERAL_AREAS:
        stem = a[1:]
        if any((p + stem) in t for p in GENERAL_AREA_PREFIXES):
            return a
    return None


def detect_budget(text):
    t = (text or "").replace(",", "")
    # ש\"ח only matched the ASCII quote; real Hebrew writing uses the gershayim
    # mark ״ (U+05F4) -- "2000 ש״ח" was invisible to this regex entirely.
    m = re.search(r"(?:₪\s*(\d{2,6})|(\d{2,6})\s*(?:₪|ש[\"״]ח|שח|שקל))", t)
    if m:
        return int(m.group(1) or m.group(2))
    # \b prevents "עד" inside "מועד"/"תיעוד" from being read as the עד-fallback.
    m = re.search(r"\b(?:תקציב|עד)\s*(\d{2,6})", t)
    return int(m.group(1)) if m else None


def _non_negated_hit(t, word):
    """True iff `word` occurs in `t` without "לא"/"בלי" in the two words right before it."""
    start = 0
    while True:
        idx = t.find(word, start)
        if idx == -1:
            return False
        before = t[:idx].split()
        if not any(w in NEGATION_WORDS for w in before[-2:]):
            return True
        start = idx + len(word)


def detect_urgency(text):
    t = (text or "").lower()
    # "לא דחוף" / "לא ממש דחוף" contain "דחוף" but mean the opposite -- a plain
    # substring check read them as urgent.
    if any(_non_negated_hit(t, w) for w in URGENT):
        return "דחוף"
    if any(w in t for w in SOON):
        return "בקרוב"
    return "רגיל"


def detect_phone(text):
    for m in re.finditer(r"(?:\+?972[-\s]?|0)(?:[2-9]\d?)[-\s]?\d{3}[-\s]?\d{4}", text or ""):
        p = normalize_phone(m.group(0))
        if p:
            return p
    return None


def parse_text(text):
    """Extract structured lead fields from a pasted post / message."""
    return {
        "service": detect_service(text),
        "area": detect_area(text),
        "budget": detect_budget(text),
        "urgency": detect_urgency(text),
        "phone": detect_phone(text),
        "text": text,
    }


DEFAULT_WEIGHTS = {"intent": .25, "urgency": .2, "budget": .15, "fit": .2, "contact": .2}


def score(lead, conn=None, tenant_id=None):
    """Five 0–100 scores with Hebrew reasons, plus a weighted total. conn+tenant_id are
    optional (section 46, see below) -- every pre-existing call site passing just `lead`
    is unaffected."""
    text = (lead.get("text") or "").lower()
    s, r = {}, {}

    intent_hits = [w for w in INTENT if w in text]
    s["intent"] = min(100, 40 + 20 * len(intent_hits)) if intent_hits else (50 if lead.get("service") else 20)
    r["intent"] = (f"מילות כוונה: {', '.join(intent_hits)}" if intent_hits
                   else ("יש שירות מוגדר" if lead.get("service") else "אין סימן לכוונת קנייה"))

    u = lead.get("urgency") or "רגיל"
    s["urgency"] = {"דחוף": 95, "בקרוב": 70}.get(u, 35)
    r["urgency"] = f"דחיפות: {u}"

    b = lead.get("budget")
    s["budget"] = 30 if not b else (90 if b >= 2000 else 70 if b >= 500 else 50)
    r["budget"] = f"תקציב ₪{b}" if b else "לא צוין תקציב"

    fit = (50 if lead.get("service") else 0) + (50 if lead.get("area") else 0)
    s["fit"] = fit
    r["fit"] = "שירות + אזור ידועים" if fit == 100 else ("חסר אזור" if lead.get("service") else
                                                         "חסר שירות" if lead.get("area") else "חסרים שירות ואזור")

    has_phone = bool(normalize_phone(lead.get("phone")))
    s["contact"] = (60 if has_phone else 10) + (40 if lead.get("consent") else 0)
    r["contact"] = ("טלפון" if has_phone else "אין טלפון") + (" + הסכמה" if lead.get("consent") else " בלי הסכמה")

    weights = DEFAULT_WEIGHTS
    if conn is not None and tenant_id is not None:
        # NIGUN-134 module D section 46: configurable weights, via core.config -- an
        # explicit opt-in (conn+tenant_id given) so every existing call site that passes
        # only `lead` keeps using the hardcoded defaults unchanged, byte-for-byte.
        from .core import config
        weights = {k: config.get_float(conn, tenant_id, f"score_weight_{k}", v)
                  for k, v in DEFAULT_WEIGHTS.items()}
    total = round(sum(s[k] * w for k, w in weights.items()))
    return total, s, r


# ---- NIGUN-134 module D: additional scoring dimensions (sections 39-48) -----------
# Separate, informational signals -- NOT folded into the 5-key weighted score() above,
# which predates NIGUN-134 and whose shape other code (and existing tests) already
# depend on. Mixing them into `total` silently would change the number for every
# existing caller without a product decision to do so.

SERVICE_AVG_VALUE = {  # rough per-job ₪ estimate when the lead gave no budget --
    "אינסטלטור": 400, "חשמלאי": 350, "שיפוצים": 5000, "מיזוג": 600, "ניקיון": 250,
    "הובלות": 1200, "מנעולן": 300, "גינון": 300, "יופי": 200, "כושר": 300, "ערכה": 150,
}  # estimates, not measured facts -- see commercial_value()'s reason string.


def commercial_value(lead):
    """Section 39. Budget, when the lead gave one, is the real number and always wins;
    otherwise a per-service rough average -- explicitly labeled an estimate."""
    budget = lead.get("budget")
    if budget:
        return budget, "תקציב שצוין בפועל"
    service = lead.get("service")
    if service in SERVICE_AVG_VALUE:
        return SERVICE_AVG_VALUE[service], f"הערכה לפי שירות ({service}) -- לא תקציב בפועל"
    return None, "אין תקציב ואין הערכת-שירות לשירות הזה"


def completeness_score(lead):
    """Section 43: share of the fields a usable lead should have."""
    fields = ("name", "phone", "service", "area", "budget", "urgency")
    present = sum(1 for f in fields if lead.get(f))
    pct = round(100 * present / len(fields))
    missing = [f for f in fields if not lead.get(f)]
    reason = "כל השדות קיימים" if not missing else f"חסרים: {', '.join(missing)}"
    return pct, reason


def geographic_relevance_score(area, covered_areas):
    """Section 41: whether this lead's area falls inside the business's actual supplier
    coverage (covered_areas, e.g. from brokerage.covered_areas()) -- distinct from `fit`
    above, which only checks that *some* area was detected at all."""
    if not area:
        return 0, "לא זוהה אזור"
    if not covered_areas:
        return 50, "אין עדיין נתוני-כיסוי (אין ספקים פעילים עם אזורים מוגדרים)"
    if area.lower() in covered_areas:
        return 100, f"{area} בתוך אזורי הכיסוי"
    return 20, f"{area} מחוץ לאזורי הכיסוי הידועים"


def source_quality_score(source, attribution_rows):
    """Section 42: this source's historical conversion rate, from attribution.report()'s
    own numbers -- not a new tracking mechanism. Too little history (or none) -> a
    neutral 50, not a confident-looking 0."""
    row = next((r for r in attribution_rows if r["source"] == source), None)
    if not row or row["leads"] < 5:
        return 50, "אין עדיין היסטוריה מספקת למקור הזה"
    return row["conversion_pct"], f"{row['conversion_pct']}% המרה היסטורית ({row['leads']} לידים)"


def conversion_probability(conn, tenant_id, lead, min_sample=30):
    """Section 44: P(convert) from real historical outcomes for the same service -- never
    a guess dressed as a number. Returns (None, reason) below min_sample, per the explicit
    rule: never present an estimate as a certainty."""
    service = lead.get("service")
    q = "SELECT status FROM leads WHERE tenant_id=? AND is_demo=0 AND status IN ('won','lost')"
    args = [tenant_id]
    if service:
        q += " AND service=?"
        args.append(service)
    rows = conn.execute(q, args).fetchall()
    n = len(rows)
    if n < min_sample:
        return None, f"רק {n} לידים סגורים היסטוריים (נדרש {min_sample}) -- לא מספיק לאמוד הסתברות"
    won = sum(1 for row in rows if row["status"] == "won")
    pct = round(100 * won / n, 1)
    return pct, f"{pct}% מתוך {n} לידים סגורים היסטוריים (שירות: {service or 'הכל'})"
