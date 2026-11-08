"""Guardian: rules every outgoing message must pass (consent, opt-out, real number, rate limit)."""
import re
import time

from . import db

PLACEHOLDER_NUMBERS = {"972500000000", "972000000000", "9720500000000"}
DEFAULT_BUSINESS_NUMBER = "972559814818"   # 055-981-4818
STOP_WORDS = ("הסר", "הסירו", "stop", "unsubscribe", "די", "להסיר")


class Blocked(Exception):
    pass


def normalize_phone(raw):
    """Israeli numbers → 972XXXXXXXXX. Returns None when it is not a phone number."""
    if not raw:
        return None
    s = str(raw).strip()
    # \D below strips every "+", so "++972559814818" used to become "972559814818"
    # and pass as valid. A real number has at most one "+", and only as its very
    # first character -- reject anything else before stripping non-digits.
    plus_count = s.count("+")
    if plus_count > 1 or (plus_count == 1 and not s.startswith("+")):
        return None
    digits = re.sub(r"\D", "", s)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) in (9, 10):
        digits = "972" + digits[1:]
    if digits.startswith("972") and len(digits) in (11, 12):
        return digits
    if 10 <= len(digits) <= 15 and not digits.startswith("0"):
        return digits
    return None


def is_stop_message(text):
    t = (text or "").strip().lower()
    return any(t == w or t.startswith(w + " ") for w in STOP_WORDS)


def opt_out(conn, tenant_id, phone):
    p = normalize_phone(phone)
    if p:
        conn.execute("INSERT OR IGNORE INTO optout VALUES (?,?,?)", (tenant_id, p, time.time()))
        conn.commit()
    return p


def is_opted_out(conn, tenant_id, phone):
    p = normalize_phone(phone)
    return bool(p and conn.execute("SELECT 1 FROM optout WHERE tenant_id=? AND phone=?",
                                   (tenant_id, p)).fetchone())


def check_send(conn, tenant_id, phone, *, consent, max_per_day=None):
    """Raise Blocked with a Hebrew reason, or return the normalized number."""
    p = normalize_phone(phone)
    if not p:
        raise Blocked("מספר טלפון לא תקין")
    if p in PLACEHOLDER_NUMBERS:
        raise Blocked("זה מספר דמה — יש להגדיר מספר אמיתי")
    if not consent:
        raise Blocked("אין הסכמה מהנמען לקבל הודעות")
    if is_opted_out(conn, tenant_id, p):
        raise Blocked("הנמען ביקש הסרה")
    limit = max_per_day if max_per_day is not None else int(
        db.get_setting(conn, tenant_id, "max_sends_per_day", 3))
    since = time.time() - 86400
    n = conn.execute("SELECT COUNT(*) FROM sends WHERE tenant_id=? AND phone=? AND ts>?",
                     (tenant_id, p, since)).fetchone()[0]
    if n >= limit:
        raise Blocked(f"הגעת למגבלה של {limit} הודעות ביום לנמען הזה")
    return p


def record_send(conn, tenant_id, phone):
    conn.execute("INSERT INTO sends(tenant_id, phone, ts) VALUES (?,?,?)",
                 (tenant_id, normalize_phone(phone), time.time()))
    conn.commit()


def business_number(conn, tenant_id):
    return db.get_setting(conn, tenant_id, "business_whatsapp", DEFAULT_BUSINESS_NUMBER)
