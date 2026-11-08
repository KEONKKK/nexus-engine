"""Connectors: fetch(config) -> list of candidate dicts {text, phone?, consent?}.

Network I/O (RssConnector.fetch, LeadAdsConnector.fetch's real branch) is kept in a
thin wrapper around a pure parsing function, so tests exercise the parsing logic on a
canned string and never make a real HTTP call -- consistent with every existing test in
engine/tests/test_engine.py, none of which touches the network.
"""
import os
import re
import urllib.request
import xml.etree.ElementTree as ET

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class PasteConnector:
    """Section 13's simplest connector: a human pastes a post/message. This already exists
    as plain nexus_master.leads.add_lead(text=..., source=...) / `python -m nexus_master add`
    -- this class only gives it a name inside the connector registry (CONNECTORS["paste"])
    so discovery_sources can list it uniformly next to rss/lead_ads. No new behaviour."""

    def fetch(self, config):
        text = (config or {}).get("text", "")
        return [{"text": text}] if text else []


class RssConnector:
    """Section 14/17-20: any public RSS or Atom feed an operator configures by hand
    (config: {"url": "..."}) -- job boards, municipality announcement feeds, etc. This is
    a feed reader, not a crawler: it only ever fetches the one URL it is given."""

    def fetch(self, config):
        url = (config or {}).get("url")
        if not url:
            raise ValueError("rss connector: config חסר 'url'")
        with urllib.request.urlopen(url, timeout=15) as r:  # pragma: no cover -- network
            raw = r.read()
        return self.parse(raw)

    @staticmethod
    def parse(raw_xml):
        """Pure parsing, no I/O -- this is what tests call directly. Supports RSS 2.0
        (<item><title/><description/>) and Atom (<entry><title/><summary|content/>)."""
        root = ET.fromstring(raw_xml)
        out = []
        for item in root.iter("item"):  # RSS 2.0
            title = (item.findtext("title") or "").strip()
            desc = (item.findtext("description") or "").strip()
            text = f"{title}\n{desc}".strip()
            if text:
                out.append({"text": text})
        for entry in root.findall(".//atom:entry", _ATOM_NS):  # Atom
            title = (entry.findtext("atom:title", namespaces=_ATOM_NS) or "").strip()
            summary = (entry.findtext("atom:summary", namespaces=_ATOM_NS)
                      or entry.findtext("atom:content", namespaces=_ATOM_NS) or "").strip()
            text = f"{title}\n{summary}".strip()
            if text:
                out.append({"text": text})
        return out


class LeadAdsConnector:
    """Section 'lead_ads' (listed in design/research/nigun-134.md's module-B breakdown as
    BLOCKED+mock). Real Meta Lead Ads retrieval needs a System User token with
    leads_retrieval permission and a configured Lead Ads form -- none of that exists in
    this project (no credentials were invented to fill the gap). `enabled` mirrors the
    same on/off-by-env-var pattern as nexus_master.whatsapp.CloudAPI."""

    ENV_VAR = "LEAD_ADS_TOKEN"

    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get(self.ENV_VAR)

    @property
    def enabled(self):
        return bool(self.token)

    def fetch(self, config):
        form_id = (config or {}).get("form_id")
        if not self.enabled:
            raise RuntimeError(
                f"lead_ads כבוי — חסר {self.ENV_VAR} (BLOCKED, לפי העיצוב — ר' nigun-134.md)")
        if not form_id:
            raise ValueError("lead_ads connector: config חסר 'form_id'")
        return self._fetch_real(form_id)  # pragma: no cover -- requires real credentials

    def _fetch_real(self, form_id):  # pragma: no cover
        """Best-effort real call, structurally mirroring whatsapp.CloudAPI.send -- never
        exercised in tests because self.enabled is False without real credentials, and no
        fabricated response is ever returned in its place (section-K rule: never invent a
        successful response)."""
        import json
        req = urllib.request.Request(
            f"https://graph.facebook.com/v20.0/{form_id}/leads",
            headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return [{"text": _lead_ad_fields_to_text(item.get("field_data", []))}
                for item in data.get("data", [])]


def _lead_ad_fields_to_text(field_data):  # pragma: no cover
    parts = [f"{f.get('name')}: {', '.join(f.get('values', []))}" for f in field_data]
    return "\n".join(parts)
