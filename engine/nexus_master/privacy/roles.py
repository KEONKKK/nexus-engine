"""Section 118: role-based access control.

As of B's round-2 api.py, a real two-role RBAC now holds at the HTTP boundary:
NEXUS_API_KEY -> admin (full access), NEXUS_VIEWER_KEY -> viewer (GET only, every
non-GET gets a generic 403) -- enforced in App.__call__ before routing, so it already
covers every route this package (and every other plugin) registers, with no change
needed here. See test_privacy.py:TestRoleEnforcementIntegration for a direct
confirmation against this package's own routes.

What is *not* real yet: a third distinct principal (e.g. "sales", someone who may write
leads but not change settings). api.py's gate only ever asks "does this key match admin
or viewer?", not "who, specifically, is this?" -- so ROLE_PERMISSIONS/assign_role/
get_role below stay a ready extension point, not something any real request reaches.
"""

ROLE_PERMISSIONS = {
    "admin": {"*"},
    "sales": {"read_leads", "write_leads", "read_bi"},
    "viewer": {"read_leads", "read_bi"},
}


def has_permission(role, action):
    perms = ROLE_PERMISSIONS.get(role, set())
    return "*" in perms or action in perms


def assign_role(conn, tenant_id, principal, role):
    if role not in ROLE_PERMISSIONS:
        raise ValueError(f"תפקיד לא מוכר: {role} (זמינים: {', '.join(ROLE_PERMISSIONS)})")
    conn.execute("INSERT OR REPLACE INTO principal_roles(tenant_id, principal, role) VALUES (?,?,?)",
                 (tenant_id, principal, role))
    conn.commit()


def get_role(conn, tenant_id, principal, default="viewer"):
    row = conn.execute("SELECT role FROM principal_roles WHERE tenant_id=? AND principal=?",
                       (tenant_id, principal)).fetchone()
    return row["role"] if row else default
