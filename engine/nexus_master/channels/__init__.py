"""Marketing & channels (NIGUN-134 module H, sections 85-95). Owned by window A.

  85 channels              - a channel is {name, kind}; `name` doubles as the
                              attribution.py source code (leads.source), so cost and
                              performance can be joined against the SAME string leads
                              already carry instead of inventing a second identifier.
  86-87 Lead Ads webhook    - BLOCKED until META_APP_SECRET is configured (same .enabled-
                              property pattern as whatsapp.CloudAPI). The payload parser
                              is written and tested against Meta's publicly documented
                              Lead Ads webhook shape (developers.facebook.com/docs/
                              marketing-api/guides/lead-ads/webhooks) now, so the only
                              thing a real key changes is whether add_lead is actually
                              called - not whether the code exists.
  88-89 website forms /     - already built: src/pages/find-pro, src/pages/kits,
        landing-page capture  src/pages/partners (round 4) all post to engine/public/
                              public_api.py. Nothing to add here - see design/STATE-
                              product.md round 4 for the connecting work itself.
  90-91 attribution / UTM   - attribution.py (core, B) already turns a short #ref code
                              into leads.source; this module adds STRUCTURED utm_source/
                              utm_medium/utm_campaign capture (lead_utm table) for when a
                              page sends the full triple, not just a short code - not yet
                              wired from src/ (flagged in STATE-product, same pattern as
                              api-client.mjs before it was connected).
  92 cost per channel       - manual entry, one row per (channel, period)
  93 WhatsApp               - already exists (whatsapp.py) - N/A here, nothing to add
  94 channel templates      - a first-contact message template PER CHANNEL KIND, distinct
                              from sales_ai.drafts (module F, section 62: a draft generated
                              per LEAD's own content) - this is the static, channel-level
                              default a new channel starts with.
  95 channel performance    - leads + conversion + revenue (attribution.report) joined
                              with recorded cost -> cost-per-lead
"""
import json
import re
import time
import os

from .. import attribution, audit, leads

MIGRATIONS = [
    """
    CREATE TABLE channels(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, name TEXT NOT NULL,
        kind TEXT NOT NULL, created REAL NOT NULL, UNIQUE(tenant_id, name));

    CREATE TABLE channel_costs(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL, period TEXT NOT NULL, amount INTEGER NOT NULL,
        created REAL NOT NULL, UNIQUE(tenant_id, channel_id, period));

    CREATE TABLE lead_utm(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, lead_id INTEGER NOT NULL,
        utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, created REAL NOT NULL);
    CREATE INDEX idx_channels_lead_utm ON lead_utm(tenant_id, lead_id);

    CREATE TABLE lead_ad_webhooks(id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL,
        received REAL NOT NULL, payload_json TEXT NOT NULL, lead_id INTEGER, status TEXT NOT NULL);
    """,
]

CHANNEL_KINDS = ("whatsapp", "facebook_lead_ads", "website_form", "landing_page", "referral", "other")

# 94: a static per-kind default. Deliberately a plain str.format() template, not a
# rules engine - same "small testable function, not a DSL" philosophy as core/rules.py.
CHANNEL_MESSAGE_TEMPLATES = {
    "whatsapp": "היי {name}, קיבלנו את ההודעה שלך בוואטסאפ ואנחנו כאן לעזור.",
    "facebook_lead_ads": "היי {name}, תודה שהשארת פרטים בפייסבוק - נחזור אליך בהקדם.",
    "website_form": "היי {name}, קיבלנו את הפנייה שלך מהטופס באתר.",
    "landing_page": "היי {name}, תודה שפנית אלינו מדף הנחיתה.",
    "referral": "היי {name}, קיבלנו הפניה בשמך - נשמח לעזור.",
    "other": "היי {name}, קיבלנו את הפנייה שלך.",
}


def _now():
    return time.time()


# ---------------------------------------------------------------- 85: channels

def create_channel(conn, tenant_id, name, kind, actor="user"):
    if kind not in CHANNEL_KINDS:
        raise ValueError(f"סוג ערוץ לא מוכר: {kind}")
    cur = conn.execute("INSERT INTO channels(tenant_id, name, kind, created) VALUES (?,?,?,?)",
                       (tenant_id, name, kind, _now()))
    conn.commit()
    audit.log(conn, tenant_id, actor, "channel.create", {"channel": cur.lastrowid, "name": name, "kind": kind})
    return cur.lastrowid


def list_channels(conn, tenant_id):
    return [dict(r) for r in conn.execute("SELECT * FROM channels WHERE tenant_id=? ORDER BY name", (tenant_id,))]


# ---------------------------------------------------------------- 94: templates

def render_channel_template(kind, **values):
    if kind not in CHANNEL_MESSAGE_TEMPLATES:
        raise ValueError(f"סוג ערוץ לא מוכר: {kind}")
    values.setdefault("name", "")
    return CHANNEL_MESSAGE_TEMPLATES[kind].format(**values)


# ---------------------------------------------------------------- 91: UTM

_UTM_RE = re.compile(r"(?:^|[?&#])utm_(source|medium|campaign)=([^&#]+)")


def parse_utm(url_or_query):
    """Extracts utm_source/utm_medium/utm_campaign from a URL or a bare query string.
    Missing fields are None, never an error - a page sending only utm_source is normal."""
    if not url_or_query:
        return {"utm_source": None, "utm_medium": None, "utm_campaign": None}
    out = {"utm_source": None, "utm_medium": None, "utm_campaign": None}
    for key, value in _UTM_RE.findall(str(url_or_query)):
        out[f"utm_{key}"] = value
    return out


def record_utm(conn, tenant_id, lead_id, utm):
    cur = conn.execute(
        "INSERT INTO lead_utm(tenant_id, lead_id, utm_source, utm_medium, utm_campaign, created)"
        " VALUES (?,?,?,?,?,?)",
        (tenant_id, lead_id, utm.get("utm_source"), utm.get("utm_medium"), utm.get("utm_campaign"), _now()))
    conn.commit()
    return cur.lastrowid


def utm_for_lead(conn, tenant_id, lead_id):
    row = conn.execute("SELECT * FROM lead_utm WHERE tenant_id=? AND lead_id=? ORDER BY id DESC LIMIT 1",
                       (tenant_id, lead_id)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 92/95: cost, performance

def record_cost(conn, tenant_id, channel_id, period, amount, actor="user"):
    """One row per (channel, period) - a second call for the same period REPLACES the
    amount (a corrected spend figure), not a second, additive entry."""
    conn.execute(
        "INSERT INTO channel_costs(tenant_id, channel_id, period, amount, created) VALUES (?,?,?,?,?)"
        " ON CONFLICT(tenant_id, channel_id, period) DO UPDATE SET amount=excluded.amount",
        (tenant_id, channel_id, period, int(amount), _now()))
    conn.commit()
    audit.log(conn, tenant_id, actor, "channel.cost", {"channel": channel_id, "period": period, "amount": amount})


def total_cost(conn, tenant_id, channel_id):
    row = conn.execute("SELECT IFNULL(SUM(amount),0) FROM channel_costs WHERE tenant_id=? AND channel_id=?",
                       (tenant_id, channel_id)).fetchone()
    return row[0]


def channel_performance(conn, tenant_id):
    """95: leads + conversion + revenue (attribution.report, already built) joined with
    recorded cost. A channel's `name` IS its attribution source code (see module
    docstring) - a channel with no matching leads yet still appears, with zeros."""
    by_source = {r["source"]: r for r in attribution.report(conn, tenant_id)}
    out = []
    for ch in list_channels(conn, tenant_id):
        stats = by_source.get(ch["name"], {"leads": 0, "converted": 0, "revenue": 0, "conversion_pct": 0})
        cost = total_cost(conn, tenant_id, ch["id"])
        leads_count = stats["leads"]
        out.append({
            **ch,
            "leads": leads_count,
            "converted": stats["converted"],
            "revenue": stats["revenue"],
            "conversion_pct": stats["conversion_pct"],
            "cost": cost,
            "cost_per_lead": round(cost / leads_count, 2) if leads_count else None,
        })
    return out


# ---------------------------------------------------------------- 86/87: Lead Ads webhook

class LeadAdsWebhook:
    """Off unless META_APP_SECRET is set - same .enabled-property pattern as
    whatsapp.CloudAPI. receive() ALWAYS logs the raw payload to lead_ad_webhooks (so
    nothing is lost while this is blocked), and only calls add_lead when enabled."""

    def __init__(self, app_secret=None):
        self.app_secret = app_secret or os.environ.get("META_APP_SECRET")

    @property
    def enabled(self):
        return bool(self.app_secret)

    @staticmethod
    def parse_payload(payload):
        """Meta's documented Lead Ads webhook shape:
        {"entry": [{"changes": [{"value": {"leadgen_id": "...",
         "field_data": [{"name": "full_name", "values": ["..."]},
                         {"name": "phone_number", "values": ["..."]}, ...]}}]}]}
        Returns a flat dict of field name -> first value, plus leadgen_id - never raises
        on an unexpected shape, just returns as much as it can find."""
        out = {"leadgen_id": None, "fields": {}}
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                if value.get("leadgen_id"):
                    out["leadgen_id"] = value["leadgen_id"]
                for fd in value.get("field_data", []) or []:
                    name = fd.get("name")
                    values = fd.get("values") or []
                    if name and values:
                        out["fields"][name] = values[0]
        return out

    def receive(self, conn, tenant_id, payload, channel_name="facebook_lead_ads"):
        parsed = self.parse_payload(payload)
        if not self.enabled:
            cur = conn.execute(
                "INSERT INTO lead_ad_webhooks(tenant_id, received, payload_json, lead_id, status)"
                " VALUES (?,?,?,NULL,'blocked')",
                (tenant_id, time.time(), json.dumps(payload, ensure_ascii=False)))
            conn.commit()
            return {"ok": False, "blocked": True, "reason": "META_APP_SECRET not configured",
                    "webhook_id": cur.lastrowid}
        fields = parsed["fields"]
        lid, dup = leads.add_lead(
            conn, tenant_id, text=f"ליד מ-Facebook Lead Ads ({parsed['leadgen_id']})",
            name=fields.get("full_name"), phone=fields.get("phone_number"),
            source=channel_name, consent=True, actor="lead_ads_webhook")
        conn.execute(
            "INSERT INTO lead_ad_webhooks(tenant_id, received, payload_json, lead_id, status)"
            " VALUES (?,?,?,?,?)",
            (tenant_id, time.time(), json.dumps(payload, ensure_ascii=False), lid, "processed"))
        conn.commit()
        audit.log(conn, tenant_id, "lead_ads_webhook", "channel.lead_ads.received",
                 {"leadgen_id": parsed["leadgen_id"], "lead_id": lid})
        return {"ok": True, "lead_id": lid, "duplicate_of": dup}


def webhook_history(conn, tenant_id, limit=50):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM lead_ad_webhooks WHERE tenant_id=? ORDER BY id DESC LIMIT ?", (tenant_id, limit))]


# ---------------------------------------------------------------- plugin hook

def routes(app):
    def list_route(conn, tenant_id, body, query):
        return 200, {"channels": list_channels(conn, tenant_id)}

    def create_route(conn, tenant_id, body, query):
        try:
            cid = create_channel(conn, tenant_id, body["name"], body["kind"], actor="admin")
        except (ValueError, KeyError) as e:
            return 400, {"error": str(e)}
        return 201, {"id": cid}

    def cost_route(conn, tenant_id, body, query):
        record_cost(conn, tenant_id, int(body["channel_id"]), body["period"], int(body["amount"]), actor="admin")
        return 200, {"ok": True}

    def performance_route(conn, tenant_id, body, query):
        return 200, {"channels": channel_performance(conn, tenant_id)}

    def webhook_route(conn, tenant_id, body, query):
        result = LeadAdsWebhook().receive(conn, tenant_id, body)
        return (200 if result["ok"] else 503), result

    app.add_route("GET", "/channels", list_route)
    app.add_route("POST", "/channels", create_route)
    app.add_route("POST", "/channels/cost", cost_route)
    app.add_route("GET", "/channels/performance", performance_route)
    app.add_route("POST", "/channels/lead_ads_webhook", webhook_route)
