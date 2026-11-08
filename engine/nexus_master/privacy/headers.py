"""Section 122: protection against common web application attacks, at the HTTP-header
level. A constant + a pure helper -- wiring this into App._send (api.py, owned
elsewhere) is an integration step for whoever edits that file next, same as
validation.py; this is NEEDS_IMPROVEMENT in design/nigun/STATUS.md for exactly that
reason, not because the headers themselves are untested."""

SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "default-src 'none'"),  # a JSON API serves no HTML/JS of its own
    ("Referrer-Policy", "no-referrer"),
]


def with_security_headers(existing_headers):
    """Appends SECURITY_HEADERS to a list of (name, value) tuples, skipping any name
    already present so a caller's own explicit header always wins."""
    present = {name for name, _ in existing_headers}
    return list(existing_headers) + [(n, v) for n, v in SECURITY_HEADERS if n not in present]
