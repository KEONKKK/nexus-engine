"""Section 32: enrichment using an authorized external data provider -- BLOCKED by
design, same on/off-by-env-var pattern as whatsapp.CloudAPI and discovery's
LeadAdsConnector. No specific vendor is named or invented: this is a generic adapter
(configured base URL + API key) so it does not fabricate a successful integration with
any particular company this project has no agreement with."""
import json
import os
import urllib.parse
import urllib.request


class EnrichmentProvider:
    def lookup(self, phone=None, email=None):
        raise NotImplementedError


class NullProvider(EnrichmentProvider):
    """Default provider: always returns None, never pretends to have found anything."""

    enabled = True  # "enabled" in the sense that calling it is always safe -- it just finds nothing.

    def lookup(self, phone=None, email=None):
        return None


class ExternalProvider(EnrichmentProvider):
    """Generic HTTP enrichment adapter -- GET {url}?phone=...&email=... with a bearer
    token, expecting a JSON object with optional company/category fields back. Off
    unless both ENRICHMENT_API_URL and ENRICHMENT_API_KEY are set."""

    URL_VAR = "ENRICHMENT_API_URL"
    KEY_VAR = "ENRICHMENT_API_KEY"

    def __init__(self, url=None, key=None):
        self.url = url if url is not None else os.environ.get(self.URL_VAR)
        self.key = key if key is not None else os.environ.get(self.KEY_VAR)

    @property
    def enabled(self):
        return bool(self.url and self.key)

    def lookup(self, phone=None, email=None):  # pragma: no cover -- requires real credentials
        if not self.enabled:
            raise RuntimeError(
                f"enrichment provider כבוי — חסרים {self.URL_VAR}/{self.KEY_VAR} (BLOCKED)")
        qs = urllib.parse.urlencode({k: v for k, v in (("phone", phone), ("email", email)) if v})
        req = urllib.request.Request(f"{self.url}?{qs}", headers={"Authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())


def default_provider():
    """Picks ExternalProvider when configured, NullProvider otherwise -- callers never
    need to know which one they got; the BLOCKED provider never raises just from being
    constructed, only from being used (lookup())."""
    ext = ExternalProvider()
    return ext if ext.enabled else NullProvider()


def enrich_lead(conn, tenant_id, lead_id, provider=None):
    """Section 32/33-34 (company/category when the provider supplies them). Non-fatal
    when the provider is disabled -- enrichment is supplementary, never required for a
    lead to exist or be acted on."""
    from . import history
    provider = provider or default_provider()
    if not provider.enabled:
        return {"ok": False, "reason": "blocked_no_provider_configured"}
    row = conn.execute("SELECT * FROM leads WHERE tenant_id=? AND id=?",
                       (tenant_id, lead_id)).fetchone()
    if not row:
        raise ValueError("ליד לא קיים")
    data = provider.lookup(phone=row["phone"], email=row["email"]) or {}
    changed = {}
    for field in ("service", "area"):  # company/category map onto the fields this model has
        value = data.get(field)
        if value and not row[field]:
            changed[field] = value
    if changed:
        set_clause = ", ".join(f"{k}=?" for k in changed)
        conn.execute(f"UPDATE leads SET {set_clause} WHERE tenant_id=? AND id=?",
                     (*changed.values(), tenant_id, lead_id))
        conn.commit()
        for field, new_val in changed.items():
            history.record_change(conn, tenant_id, lead_id, "enrichment_provider", field, None, new_val)
    return {"ok": True, "changed": changed}
