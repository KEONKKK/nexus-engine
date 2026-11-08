"""Section 67: objection-handling suggestions -- a real, small, static response library
keyed by the same NEGATION_WORDS-adjacent reality every Hebrew lead already shows (too
expensive / need to think / already have someone), not a learned model."""

OBJECTIONS = [
    ("יקר", "זה בגלל שאנחנו משקיעים באיכות שתשתלם לך לאורך זמן — רוצה שנפרט מה כלול במחיר?"),
    ("לחשוב", "בטח, אין בעיה. אשמח לשלוח עוד פרטים בכתב כדי שיהיה לך נוח להחליט בזמן שלך."),
    ("כבר יש לי", "מגניב שיש לך כבר מישהו! אם זה ישתנה בעתיד, אנחנו כאן."),
    ("לא דחוף", "מבינים. מתי יהיה נוח לחזור אליך עם התזכורת?"),
    ("תקציב", "אפשר לבדוק פתרון שמתאים לתקציב שלך — כמה בערך חשבת להשקיע?"),
]


def suggest_response(text):
    """Returns the first matching (objection_keyword, response) pair, or None. Returns a
    list via suggest_responses when more than one keyword matches the same message."""
    matches = suggest_responses(text)
    return matches[0] if matches else None


def suggest_responses(text):
    t = (text or "").lower()
    return [{"objection": kw, "response": resp} for kw, resp in OBJECTIONS if kw in t]
