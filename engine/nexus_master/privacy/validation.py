"""Section 121: validate and sanitize external input. A utility library -- calling it
from engine/public/public_api.py (owned by the other window) is an integration step for
whoever edits that file next, not done here; see design/nigun/STATUS.md row 121."""
import re

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_text(s, max_len=5000):
    """Strips control characters (keeps \\t\\n) and truncates -- never raises, since
    "sanitize" means "make safe to store", not "reject"."""
    if s is None:
        return ""
    s = _CONTROL_CHARS.sub("", str(s))
    return s[:max_len]


def require_fields(body, fields):
    """Returns the list of missing fields (empty list = all present). Does not raise --
    callers decide what a missing field means for their own route."""
    return [f for f in fields if not body.get(f)]


def is_safe_json_value(value, max_depth=5, _depth=0):
    """Rejects absurdly deep/nested structures (a cheap defence against adversarial
    payloads designed to blow a naive recursive serializer, not a full schema
    validator)."""
    if _depth > max_depth:
        return False
    if isinstance(value, dict):
        return all(is_safe_json_value(v, max_depth, _depth + 1) for v in value.values())
    if isinstance(value, list):
        return all(is_safe_json_value(v, max_depth, _depth + 1) for v in value)
    return True
