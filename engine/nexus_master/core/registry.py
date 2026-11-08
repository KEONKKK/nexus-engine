"""Plugin discovery for the NIGUN-134 module split (design/research/nigun-134.md).

Each of A/B/C owns a disjoint set of subpackages directly under nexus_master/
(core/, crm/, automation/, channels/, discovery/, enrich/, sales_ai/, bi/, privacy/,
...). A subpackage "is a plugin" simply by defining, at its __init__.py top level,
any of:
  MIGRATIONS  -- a list of executescript() strings, numbered from 1 like db.py's own,
                 tracked under schema_version with pkg=<package name> so two packages
                 numbering their own migrations 1, 2, 3... never collide.
  routes(app) -- called once per App instance; registers paths via app.add_route(...).
  COMMANDS    -- {regex_pattern: handler(conn, tenant_id, match)} for command.run().

No central list to keep in sync and forget: a window adds a subpackage under
nexus_master/ with one of these three names defined, and db.py/api.py/command.py
pick it up automatically the next time they run. Order is alphabetical by package
name -- stable and deterministic, which matters for migration ordering across runs.
"""
import importlib
import pkgutil

import nexus_master

_cache = None


def discover_plugins(refresh=False):
    global _cache
    if _cache is not None and not refresh:
        return _cache
    found = []
    for info in pkgutil.iter_modules(nexus_master.__path__):
        if not info.ispkg:
            continue
        mod = importlib.import_module(f"nexus_master.{info.name}")
        if any(hasattr(mod, attr) for attr in ("MIGRATIONS", "routes", "COMMANDS")):
            found.append(mod)
    found.sort(key=lambda m: m.__name__)
    _cache = found
    return found
