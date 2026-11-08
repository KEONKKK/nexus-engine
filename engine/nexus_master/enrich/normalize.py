"""Section 27-29: name/email normalization and validation, plus a doc-only pointer for
phone (29) -- phone format validation already exists (guardian.normalize_phone) and is
not re-implemented here, only exercised by this package's own tests to document that it
is format-only, never an ownership check (no SMS/call verification exists anywhere in
this project)."""
import re
import unicodedata

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def normalize_name(name):
    """Section 27. Collapses whitespace and strips -- Hebrew has no letter case to fold,
    so this does not title-case Hebrew text (that would be meaningless); Latin names are
    title-cased."""
    if not name:
        return None
    s = unicodedata.normalize("NFKC", str(name)).strip()
    s = re.sub(r"\s+", " ", s)
    if not s:
        return None
    return s if re.search(r"[֐-׿]", s) else s.title()


def normalize_email(email):
    """Section 27. Lowercase + strip. Returns None for empty input, same convention as
    guardian.normalize_phone (None means "not present", not "invalid")."""
    if not email:
        return None
    s = str(email).strip().lower()
    return s or None


def validate_email(email):
    """Section 28. Format only (one '@', a '.' in the domain part) -- same spirit as
    guardian.normalize_phone/section 29: never claims the mailbox actually exists, let
    alone that it belongs to this lead."""
    if not email:
        return False
    return bool(_EMAIL_RE.match(email))
