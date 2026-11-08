"""Public proxy WSGI app (stdlib). Adopted from design/plans/proxy-draft/ (window C,
20/20 tests green) per design/reviews/2026-11-08-client-vs-proxy.md.

NOT the admin API (nexus_master.api). This is a separate, narrow App that calls engine
functions directly in-process -- no NEXUS_API_KEY in this process at all, because it
never talks to api.py. Wrapped together with api.App by nexus_master.dispatcher.Dispatcher
for `serve` -- one process, one port, /public/* here and everything else there.
"""
import json
import re
import secrets
import time

from nexus_master import brokerage, db, kits, leads, partners, whatsapp
from nexus_master.guardian import Blocked

# Separate from api.py's MAX_BODY_BYTES on purpose (design/plans/proxy-public-tokens-migration.md
# / proxy-requests.json p-13): this process must not rely on sharing a cap with a module it
# never imports or talks to.
MAX_BODY_BYTES = 1_000_000

# Strict E.164 at the public boundary ("+" required), per glossary.md's open "טלפון"
# recommendation -- deliberately stricter than guardian.normalize_phone, which stays lenient
# for internal callers. A request that fails this never reaches add_lead at all.
_PHONE_E164_RE = re.compile(r"^\+\d{10,15}$")


def _valid_phone_e164(raw):
    return bool(raw) and bool(_PHONE_E164_RE.match(str(raw).strip()))


# Provisional per-category required fields. category itself, text, phone and consent are
# checked unconditionally in _validate_lead regardless of this table. These are C's first
# cut at what "pro"/"realestate"/"kit" need beyond the common fields -- round-3-proxy-spec.md
# does not pin this down any tighter; flagged here for A/B to challenge, not final.
REQUIRED_BY_CATEGORY = {
    "pro": ("trade",),
    "realestate": ("deal_type", "property_type"),
    "kit": ("business_type",),
}


def _validate_lead(body):
    errors = []
    category = body.get("category")
    if category not in REQUIRED_BY_CATEGORY:
        errors.append({"field": "category", "reason": "required" if category is None else "unknown_category"})
        category = None
    if not body.get("text"):
        errors.append({"field": "text", "reason": "required"})
    phone = body.get("phone")
    if not phone:
        errors.append({"field": "phone", "reason": "required"})
    elif not _valid_phone_e164(phone):
        errors.append({"field": "phone", "reason": "bad_format"})
    if "consent" not in body or not body.get("consent"):
        errors.append({"field": "consent", "reason": "required"})
    if category:
        for field in REQUIRED_BY_CATEGORY[category]:
            if not body.get(field):
                errors.append({"field": field, "reason": "required"})
    return errors


def _validate_partner(body):
    errors = []
    if not body.get("name"):
        errors.append({"field": "name", "reason": "required"})
    phone = body.get("phone")
    if not phone:
        errors.append({"field": "phone", "reason": "required"})
    elif not _valid_phone_e164(phone):
        errors.append({"field": "phone", "reason": "bad_format"})
    kind = body.get("type")
    if kind not in partners.KINDS:
        errors.append({"field": "type", "reason": "required" if kind is None else "unknown_type"})
    if "consent" not in body or not body.get("consent"):
        errors.append({"field": "consent", "reason": "required"})
    return errors


class RateLimiter:
    """In-memory sliding window, one process, stdlib only -- same spirit as
    guardian.check_send's max_per_day, not a new dependency. Does not survive a restart and
    is not shared across instances; documented as a known limit in round-3-proxy-spec.md,
    not something to solve here."""

    def __init__(self):
        self._hits = {}  # (route_key, ip) -> [timestamps]

    def allow(self, route_key, ip, limit, window_seconds=60):
        now = time.time()
        key = (route_key, ip)
        hits = [t for t in self._hits.get(key, []) if now - t < window_seconds]
        if len(hits) >= limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


class App:
    def __init__(self, conn, tenant="default"):
        self.conn = conn
        self.tid = db.ensure_tenant(conn, tenant)
        self.limiter = RateLimiter()

    # ---- routes -------------------------------------------------------
    def route(self, method, path, body, remote):
        if path == "/public/health":
            return 200, {"ok": True}
        if path == "/public/leads" and method == "POST":
            if not self.limiter.allow("leads_create", remote, 5):
                return 429, {"ok": False, "error": "too_many_requests"}
            return self._create_lead(body)
        if path.startswith("/public/leads/") and path.endswith("/status") and method == "GET":
            if not self.limiter.allow("leads_status", remote, 20):
                return 429, {"ok": False, "error": "too_many_requests"}
            token = path[len("/public/leads/"):-len("/status")]
            return self._lead_status(token)
        if path == "/public/kits" and method == "POST":
            if not self.limiter.allow("kits_create", remote, 5):
                return 429, {"ok": False, "error": "too_many_requests"}
            return self._create_kit(body)
        if path == "/public/partners" and method == "POST":
            if not self.limiter.allow("partners_create", remote, 5):
                return 429, {"ok": False, "error": "too_many_requests"}
            return self._create_partner(body)
        return 404, {"ok": False, "error": "not_found"}

    def _create_lead(self, body):
        errors = _validate_lead(body)
        if errors:
            return 400, {"ok": False, "errors": errors}
        category = body["category"]
        area = body.get("city")
        service = budget = urgency = None
        extra_lines = []
        # Mapping from the public per-category fields to add_lead's generic kwargs is a
        # provisional, documented choice (round-3-proxy-spec.md does not pin it down) --
        # anything that doesn't fit a generic slot is appended to the stored text instead of
        # silently dropped, so nothing the lead submitted is lost even when it isn't
        # structured.
        if category == "pro":
            service = body.get("trade")
            urgency = body.get("urgency")
        elif category == "realestate":
            service = body.get("property_type") or body.get("deal_type")
            budget = body.get("budget_max")
            urgency = body.get("timeline")
            if body.get("deal_type"):
                extra_lines.append(f"סוג עסקה: {body['deal_type']}")
        elif category == "kit":
            service = body.get("business_type")
            if body.get("business_size"):
                extra_lines.append(f"גודל עסק: {body['business_size']}")
            if body.get("pain"):
                extra_lines.append(f"קושי: {body['pain']}")
            if body.get("kit_requested"):
                extra_lines.append("ביקש ערכה")
        text = body["text"]
        full_text = text if not extra_lines else text + "\n" + "\n".join(extra_lines)
        lid, dup = leads.add_lead(
            self.conn, self.tid, text=full_text, name=body.get("name"), phone=body["phone"],
            service=service, area=area, budget=budget, urgency=urgency,
            source=body.get("source") or f"public:{category}", consent=True, actor="public_api")
        if lid is None:
            # add_lead returns (None, None) only for a STOP/opt-out message in `text` -- a
            # "normal" outcome from this caller's point of view, not an error, so 200/ok:true
            # rather than 4xx. There is nothing to track, so no tracking_token either, and no
            # business notification either (nothing to notify about but a removal).
            return 200, {"ok": True, "opted_out": True}
        lead = leads.get(self.conn, self.tid, lid)
        matches = brokerage.match_lead(self.conn, self.tid, lead)
        notify_link = whatsapp.notify_business_new_lead(self.conn, self.tid, lead, matches)
        leads.set_notify_link(self.conn, self.tid, lid, notify_link)
        token = self._issue_token(lid)
        return 201, {"ok": True, "tracking_token": token, "status": "duplicate" if dup else "new"}

    def _issue_token(self, lead_id):
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "INSERT INTO public_tokens(token, lead_id, tenant_id, created) VALUES (?,?,?,?)",
            (token, lead_id, self.tid, time.time()))
        self.conn.commit()
        return token

    def _resolve_token(self, token):
        row = self.conn.execute(
            "SELECT lead_id FROM public_tokens WHERE token=? AND tenant_id=?",
            (token, self.tid)).fetchone()
        return row["lead_id"] if row else None

    def _lead_status(self, token):
        lead_id = self._resolve_token(token)
        if lead_id is None:
            return 404, {"ok": False, "error": "not_found"}
        lead = leads.get(self.conn, self.tid, lead_id)
        if not lead:
            # Token resolves but the lead row is gone (e.g. remove_demo) -- same 404 as an
            # unknown token, not a 500: the caller cannot tell the difference and should not
            # need to.
            return 404, {"ok": False, "error": "not_found"}
        return 200, {"ok": True, "status": lead["status"]}

    def _create_kit(self, body):
        errors = []
        kind = body.get("kind")
        if kind not in ("whatsapp", "scripts", "cv"):
            errors.append({"field": "kind", "reason": "required" if kind is None else "unknown_kind"})
        if not body.get("business"):
            errors.append({"field": "business", "reason": "required"})
        if errors:
            return 400, {"ok": False, "errors": errors}
        lead_id = None
        token = body.get("tracking_token")
        if token:
            lead_id = self._resolve_token(token)
            if lead_id is None:
                return 400, {"ok": False, "errors": [{"field": "tracking_token", "reason": "unknown"}]}
        kits.create_kit(self.conn, self.tid, kind, body["business"], body.get("industry", "general"), lead_id)
        return 201, {"ok": True}

    def _create_partner(self, body):
        errors = _validate_partner(body)
        if errors:
            return 400, {"ok": False, "errors": errors}
        pid = partners.register(
            self.conn, self.tid, name=body["name"], phone=body["phone"],
            services=body.get("services", ""), areas=body.get("areas", ""),
            kind=body["type"], consent=True)
        partner = partners.get(self.conn, self.tid, pid)
        link = whatsapp.notify_link_for_partner(partner)
        partners.set_notify_link(self.conn, self.tid, pid, link)
        return 201, {"ok": True, "partner_id": pid, "status": "pending"}

    # ---- WSGI ---------------------------------------------------------
    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/").rstrip("/") or "/"
        remote = environ.get("REMOTE_ADDR", "") or "unknown"
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
            if size > MAX_BODY_BYTES:
                return self._send(start_response, 413, {"ok": False, "error": "payload_too_large"})
            raw = environ["wsgi.input"].read(size) if size else b""
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                return self._send(start_response, 400, {"ok": False, "error": "invalid_json"})
            status, data = self.route(method, path, body, remote)
        except Blocked as e:
            status, data = 422, {"ok": False, "blocked": str(e)}
        return self._send(start_response, status, data)

    @staticmethod
    def _send(start_response, status, data):
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        names = {200: "OK", 201: "Created", 400: "Bad Request", 404: "Not Found",
                 413: "Payload Too Large", 422: "Unprocessable Entity", 429: "Too Many Requests"}
        # No Access-Control-Allow-Origin here -- CORS for /public/* is added by
        # nexus_master.dispatcher.Dispatcher, which wraps this App for `serve`. Keeping it
        # out of this file lets test_public_api.py call the App directly with no CORS noise.
        start_response(f"{status} {names.get(status, '')}",
                       [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(raw)))])
        return [raw]
