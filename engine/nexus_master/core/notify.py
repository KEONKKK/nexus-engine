"""Unified notification center (NIGUN-134 module A, section 5): one entrypoint for
"tell somebody something happened", routing to whichever channel is available --
wa.me link (always), Cloud API (only when configured) -- and logging every attempt as
a core_events row, instead of each caller (whatsapp.notify_business_new_lead, future
callers) rolling its own ad hoc logging."""
from . import events
from .. import guardian, whatsapp


def notify(conn, tenant_id, *, channel, target, text, actor="system"):
    """channel: 'wa_link' (compose-only, never blocked, always succeeds) or 'cloud_api'
    (real send, subject to guardian + the existing physical cloud-write approval -- may
    raise guardian.Blocked, same as calling whatsapp.CloudAPI.send directly)."""
    if channel == "wa_link":
        link = whatsapp.wa_link(target, text)
        events.emit(conn, tenant_id, "notify.sent", {"channel": channel, "target": target, "link": link})
        return {"ok": True, "channel": channel, "link": link}
    if channel == "cloud_api":
        api = whatsapp.CloudAPI()
        if not api.enabled:
            events.emit(conn, tenant_id, "notify.unavailable", {"channel": channel, "target": target})
            return {"ok": False, "channel": channel, "error": "cloud_api_not_configured"}
        try:
            api.send(conn, tenant_id, target, text, consent=True)
        except guardian.Blocked as e:
            events.emit(conn, tenant_id, "notify.blocked", {"channel": channel, "target": target, "reason": str(e)})
            return {"ok": False, "channel": channel, "error": str(e)}
        events.emit(conn, tenant_id, "notify.sent", {"channel": channel, "target": target})
        return {"ok": True, "channel": channel}
    raise ValueError(f"ערוץ לא מוכר: {channel}")
