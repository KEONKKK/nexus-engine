"""SQLite storage with versioned migrations.

Base schema (this module's own MIGRATIONS, tracked under schema_version pkg='base') plus
per-package migrations contributed by nexus_master subpackages under the NIGUN-134 plugin
model (design/research/nigun-134.md) -- each subpackage defines its own MIGRATIONS in its
__init__.py, discovered by core.registry.discover_plugins() and tracked under
schema_version pkg=<package name>, so two packages numbering their own migrations
1, 2, 3... independently never collide."""
import sqlite3
import time

MIGRATIONS = [
    # 0001 — core: tenants, leads, audit
    """
    CREATE TABLE tenants(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created REAL);
    CREATE TABLE leads(
        id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, created REAL,
        name TEXT, phone TEXT, service TEXT, area TEXT, budget INTEGER,
        urgency TEXT, text TEXT, source TEXT, consent INTEGER DEFAULT 0,
        status TEXT DEFAULT 'new', is_demo INTEGER DEFAULT 0,
        score_total INTEGER, scores_json TEXT, reasons_json TEXT);
    CREATE INDEX leads_tenant ON leads(tenant_id, status);
    CREATE TABLE audit(id INTEGER PRIMARY KEY, tenant_id INTEGER, ts REAL,
        actor TEXT, action TEXT, data TEXT, prev_hash TEXT, hash TEXT);
    """,
    # 0002 — buyers and credit ledger
    """
    CREATE TABLE buyers(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        name TEXT, phone TEXT, services TEXT, areas TEXT, price_per_lead INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1);
    CREATE TABLE ledger(id INTEGER PRIMARY KEY, tenant_id INTEGER, buyer_id INTEGER,
        ts REAL, amount INTEGER, kind TEXT, lead_id INTEGER, note TEXT);
    CREATE UNIQUE INDEX ledger_sale_once ON ledger(buyer_id, lead_id) WHERE kind='sale';
    """,
    # 0003 — brokerage
    """
    CREATE TABLE suppliers(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        name TEXT, phone TEXT, keywords TEXT, areas TEXT, rating REAL DEFAULT 1.0,
        commission_pct REAL DEFAULT 10, is_demo INTEGER DEFAULT 0);
    CREATE TABLE deals(id INTEGER PRIMARY KEY, tenant_id INTEGER, lead_id INTEGER,
        supplier_id INTEGER, score REAL, status TEXT DEFAULT 'proposed',
        amount INTEGER, commission INTEGER, created REAL, closed REAL,
        UNIQUE(lead_id, supplier_id));
    """,
    # 0004 — opt-out, experiments, kits, settings
    """
    CREATE TABLE optout(tenant_id INTEGER, phone TEXT, ts REAL, PRIMARY KEY(tenant_id, phone));
    CREATE TABLE experiments(id INTEGER PRIMARY KEY, tenant_id INTEGER, name TEXT,
        variants TEXT, active INTEGER DEFAULT 1, UNIQUE(tenant_id, name));
    CREATE TABLE exposures(experiment_id INTEGER, subject TEXT, variant TEXT,
        converted INTEGER DEFAULT 0, value INTEGER DEFAULT 0, PRIMARY KEY(experiment_id, subject));
    CREATE TABLE kits(id INTEGER PRIMARY KEY, tenant_id INTEGER, lead_id INTEGER,
        kind TEXT, business TEXT, industry TEXT, content TEXT, status TEXT DEFAULT 'draft',
        price INTEGER DEFAULT 0, created REAL, delivered REAL);
    CREATE TABLE settings(tenant_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(tenant_id, key));
    CREATE TABLE sends(id INTEGER PRIMARY KEY, tenant_id INTEGER, phone TEXT, ts REAL);
    """,
    # 0005 — public API tracking tokens (engine/public/public_api.py). Was created ad hoc
    # by that module's own _ensure_schema() in the design/plans/proxy-draft/ version
    # (tenant_id there was first drafted as TEXT, fixed to INTEGER before this landed --
    # design/plans/proxy-public-tokens-migration.md); folded into a real numbered
    # migration here per that document's own recommendation.
    """
    CREATE TABLE public_tokens(token TEXT PRIMARY KEY, lead_id INTEGER NOT NULL,
        tenant_id INTEGER NOT NULL, created REAL NOT NULL);
    CREATE INDEX idx_public_tokens_lead ON public_tokens(lead_id);
    """,
    # 0006 — business-owner notification link per lead (round 4, section 88-89/93): the
    # wa.me link summarizing a new lead, and whether it has been acted on yet. Not a
    # "sent" flag -- wa.me links are opened by a human, there is no delivery webhook.
    """
    ALTER TABLE leads ADD COLUMN notify_link TEXT;
    ALTER TABLE leads ADD COLUMN notify_clicked INTEGER DEFAULT 0;
    """,
    # 0007 — partner (buyer/supplier) self-registration queue (round 4, section 88-89):
    # public submissions land here as 'pending' and need an explicit admin command
    # ("אשר שותף <id>") to become a real buyers/suppliers row -- see nexus_master.partners.
    """
    CREATE TABLE partners(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, created REAL,
        name TEXT, phone TEXT, services TEXT, areas TEXT, kind TEXT NOT NULL,
        consent INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', notify_link TEXT);
    CREATE INDEX idx_partners_tenant_status ON partners(tenant_id, status);
    """,
    # 0008 — NIGUN-134 module I sections 99-100: supplier availability and typical price
    # range, both optional (NULL/1 = "unknown" / "available", never a hard requirement
    # for a supplier that never set them -- backward compatible with every existing
    # supplier row).
    """
    ALTER TABLE suppliers ADD COLUMN available INTEGER DEFAULT 1;
    ALTER TABLE suppliers ADD COLUMN price_min INTEGER;
    ALTER TABLE suppliers ADD COLUMN price_max INTEGER;
    """,
]


def connect(path=":memory:"):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version(pkg TEXT NOT NULL DEFAULT 'base',"
                 " v INTEGER, PRIMARY KEY(pkg, v))")
    _run_migrations(conn, "base", MIGRATIONS)
    from .core.registry import discover_plugins
    for plugin in discover_plugins():
        statements = getattr(plugin, "MIGRATIONS", None)
        if statements:
            _run_migrations(conn, plugin.__name__.rsplit(".", 1)[-1], statements)
    conn.commit()
    return len(MIGRATIONS)


def _run_migrations(conn, pkg, statements):
    row = conn.execute("SELECT MAX(v) FROM schema_version WHERE pkg=?", (pkg,)).fetchone()
    current = row[0] or 0
    for i, sql in enumerate(statements, start=1):
        if i > current:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version(pkg, v) VALUES (?,?)", (pkg, i))


def ensure_tenant(conn, name="default"):
    row = conn.execute("SELECT id FROM tenants WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO tenants(name, created) VALUES (?,?)", (name, time.time()))
    conn.commit()
    return cur.lastrowid


def get_setting(conn, tenant_id, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE tenant_id=? AND key=?",
                       (tenant_id, key)).fetchone()
    return row["value"] if row else default


def set_setting(conn, tenant_id, key, value):
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (tenant_id, key, str(value)))
    conn.commit()
