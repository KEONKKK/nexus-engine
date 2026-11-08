"""WhatsApp: manual mode (wa.me links, on) and Cloud API adapter (built, off until keys exist)."""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import audit, guardian

# engine/nexus_master/whatsapp.py -> engine/ -> project root, sibling of .claude/.
# Same directory, same one-shot 15-minute JSON files, and the same {type, expiresAt}
# shape that .claude/hooks/create-approval.mjs writes when a human double-clicks
# NEXUS-APPROVE-CLOUD-WRITE.bat -- one approval mechanism, read from both sides.
APPROVALS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "approvals"


def _consume_cloud_write_approval():
    """True (and deletes the file) iff an unexpired 'cloud-write' approval exists.
    One-shot, mirroring .claude/hooks/lib/approvals.mjs: a copy of a spent approval
    is a copy of a spent permission, so the file is consumed, not just read."""
    try:
        names = [n for n in os.listdir(APPROVALS_DIR) if n.endswith(".json")]
    except OSError:
        return False  # no approvals directory = no approval, not an error
    now_ms = time.time() * 1000
    for name in names:
        full = APPROVALS_DIR / name
        try:
            obj = json.loads(full.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # unreadable or broken -- not ours to touch
        if not isinstance(obj, dict):
            continue
        if not isinstance(obj.get("type"), str) or not isinstance(obj.get("expiresAt"), (int, float)):
            continue
        if obj["expiresAt"] <= now_ms or obj["type"] != "cloud-write":
            continue
        try:
            full.unlink()
        except OSError:
            pass
        return True
    return False


def wa_link(phone, text):
    p = guardian.normalize_phone(phone)
    return f"https://wa.me/{p}?text={urllib.parse.quote(text)}"


def draft_to_client(lead, business="העסק"):
    service = lead.get("service") or "השירות"
    return (f"היי{(' ' + lead['name']) if lead.get('name') else ''}, כאן {business}. "
            f"ראינו שאתה מחפש {service}{(' ב' + lead['area']) if lead.get('area') else ''}. "
            "יש לנו בעל מקצוע מתאים — מתי נוח לדבר? (להסרה השב 'הסר')")


def draft_to_supplier(lead, supplier_name):
    parts = [f"היי {supplier_name}, יש ליד חדש:", f"שירות: {lead.get('service') or '-'}",
             f"אזור: {lead.get('area') or '-'}", f"דחיפות: {lead.get('urgency') or '-'}"]
    if lead.get("budget"):
        parts.append(f"תקציב: ₪{lead['budget']}")
    parts.append("מעוניין? השב כן ואעביר פרטים.")
    return "\n".join(parts)


def send_manual(conn, tenant_id, phone, text, consent):
    """Guardian check → wa.me link to open on the phone. Records the send."""
    p = guardian.check_send(conn, tenant_id, phone, consent=consent)
    guardian.record_send(conn, tenant_id, p)
    audit.log(conn, tenant_id, "user", "wa.manual", {"to": p})
    return wa_link(p, text)


def _lead_notify_text(lead, matches):
    lines = [f"ליד חדש #{lead['id']}", f"שירות: {lead.get('service') or '-'}",
             f"אזור: {lead.get('area') or '-'}", f"דחיפות: {lead.get('urgency') or '-'}",
             f"ציון: {lead.get('score_total')}"]
    if matches:
        lines.append("התאמות: " + ", ".join(m["name"] for m in matches[:3]))
    return "\n".join(lines)


def notify_link_for_business(lead, matches, business_phone=None):
    """Compose (not send) a wa.me link to the business number summarizing a new lead --
    same composition-only pattern as draft_to_supplier above, no guardian gate: this never
    sends anything by itself, it only builds a URL a human (or CloudAPI.send, separately
    gated below) may act on."""
    phone = business_phone or guardian.DEFAULT_BUSINESS_NUMBER
    return wa_link(phone, _lead_notify_text(lead, matches))


def notify_business_new_lead(conn, tenant_id, lead, matches, business_phone=None):
    """notify_link_for_business, plus -- only when Cloud API is configured, an unexpired
    cloud-write approval exists, and the existing guardian send-rate cap allows it -- an
    actual automatic send. Any Blocked (no approval yet, rate cap, placeholder number) is
    swallowed: the link is still returned and still worth having even when the automatic
    send does not fire. Deliberately does not change guardian's existing per-recipient
    daily cap for this new caller -- see design/STATE-infra.md round 4 for the known
    consequence (a busy day of leads can exhaust the business number's own cap, same as
    any other recipient; raising it is a settings.py config decision for later, not a
    code change made silently here)."""
    phone = business_phone or guardian.DEFAULT_BUSINESS_NUMBER
    link = notify_link_for_business(lead, matches, phone)
    api = CloudAPI()
    if api.enabled:
        try:
            api.send(conn, tenant_id, phone, _lead_notify_text(lead, matches), consent=True)
        except guardian.Blocked:
            pass
    return link


def notify_link_for_partner(partner):
    """Same composition-only pattern as notify_link_for_business, for a pending partner
    (buyer/supplier) self-registration -- see nexus_master.partners."""
    lines = [f"שותף חדש מחכה לאישור #{partner['id']}", f"שם: {partner['name']}",
             f"סוג: {'קונה' if partner['kind'] == 'buyer' else 'ספק'}",
             f"שירותים: {partner.get('services') or '-'}", f"אזורים: {partner.get('areas') or '-'}",
             f"לאישור: 'אשר שותף {partner['id']}'"]
    return wa_link(guardian.DEFAULT_BUSINESS_NUMBER, "\n".join(lines))


class CloudAPI:
    """Meta WhatsApp Cloud API. Off unless WA_TOKEN and WA_PHONE_ID are set."""

    def __init__(self, token=None, phone_id=None):
        self.token = token or os.environ.get("WA_TOKEN")
        self.phone_id = phone_id or os.environ.get("WA_PHONE_ID")

    @property
    def enabled(self):
        return bool(self.token and self.phone_id)

    def send(self, conn, tenant_id, phone, text, consent):
        if not self.enabled:
            raise RuntimeError("Cloud API כבוי — חסרים WA_TOKEN / WA_PHONE_ID")
        p = guardian.check_send(conn, tenant_id, phone, consent=consent)
        if not _consume_cloud_write_approval():
            raise guardian.Blocked(
                "כתיבה לענן דורשת אישור פיזי מראש — לחיצה כפולה על NEXUS-APPROVE-CLOUD-WRITE.bat")
        body = json.dumps({"messaging_product": "whatsapp", "to": p, "type": "text",
                           "text": {"body": text}}).encode()
        req = urllib.request.Request(f"https://graph.facebook.com/v20.0/{self.phone_id}/messages", data=body,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read())
        guardian.record_send(conn, tenant_id, p)
        audit.log(conn, tenant_id, "system", "wa.cloud", {"to": p})
        return res
