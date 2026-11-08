"""Unified system configuration (NIGUN-134 module A, section 2): one place to read/write
settings, instead of every module calling db.get_setting/set_setting with its own
hardcoded default scattered through the codebase (guardian.py's max_sends_per_day being
the pre-existing example this generalizes). An env var with the same name, if set,
overrides the stored setting -- for ops/deploy-time knobs that should not need a DB write
(same "process-level override, never a silent code change" principle as PYTHON= in
tools/run-engine-tests.mjs)."""
import os

from .. import db

# Known settings and their defaults -- not an enforced schema, just the single place
# every other module should look instead of hardcoding its own default.
DEFAULTS = {
    "max_sends_per_day": 3,
    "hot_lead_score": 70,
    "duplicate_window_days": 7,
    "match_min_score": 0.3,
    "match_limit": 3,
}


def get(conn, tenant_id, key, default=None):
    env_val = os.environ.get(f"NEXUS_SETTING_{key.upper()}")
    if env_val is not None:
        return env_val
    fallback = DEFAULTS.get(key, default)
    return db.get_setting(conn, tenant_id, key, fallback)


def get_int(conn, tenant_id, key, default=None):
    return int(get(conn, tenant_id, key, default))


def get_float(conn, tenant_id, key, default=None):
    return float(get(conn, tenant_id, key, default))


def set(conn, tenant_id, key, value):
    db.set_setting(conn, tenant_id, key, value)


def all_settings(conn, tenant_id):
    """Every known key's effective value (env override, then stored, then default) --
    for the health/admin dashboard, not a free-form dump of the settings table."""
    return {k: get(conn, tenant_id, k) for k in DEFAULTS}
