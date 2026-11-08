"""WSGI JSON API (stdlib). Run: python -m nexus_master serve --port 8304 → http://localhost:8304

Auth: header  X-API-Key: <key>  (set NEXUS_API_KEY; without it the API only answers on localhost).
"""
import hmac
import json
import os
from urllib.parse import parse_qs

from . import attribution, audit, brokerage, buyers, command, db, experiments, kits, leads, whatsapp
from .guardian import Blocked

# 1MB. Enforced before the body is read, not after -- a request that never gets
# this far never gets to pay for its own parsing.
MAX_BODY_BYTES = 1_000_000


class App:
    def __init__(self, conn, tenant="default", api_key=None, viewer_key=None):
        self.conn = conn
        self.tid = db.ensure_tenant(conn, tenant)
        self.api_key = api_key if api_key is not None else os.environ.get("NEXUS_API_KEY")
        # NIGUN-134 module A section 11: a second, read-only key. None of this changes
        # behavior for anyone who never sets NEXUS_VIEWER_KEY -- self.viewer_key stays
        # None and the role check below never has anything to match against.
        self.viewer_key = viewer_key if viewer_key is not None else os.environ.get("NEXUS_VIEWER_KEY")
        # NIGUN-134 plugin model (design/research/nigun-134.md): each subpackage that
        # defines routes(app) gets called once here and registers its own paths via
        # self.add_route(...) below -- this App's own if/elif chain stays untouched,
        # unknown-route 404 only fires after both are checked.
        self._plugin_routes = {}
        from .core.registry import discover_plugins
        for plugin in discover_plugins():
            if hasattr(plugin, "routes"):
                plugin.routes(self)

    def add_route(self, method, path, handler):
        """Plugin registration: handler(conn, tenant_id, body, query) -> (status, data)."""
        self._plugin_routes[(method, path)] = handler

    # ---- routes -------------------------------------------------------
    def route(self, method, path, body, query=None):
        c, t = self.conn, self.tid
        query = query or {}
        if path == "/health":
            # audit.verify walks the whole chain -- fine as an opt-in, too slow as the
            # thing every health probe triggers. Default to a cheap count; ?deep=1 for the
            # real check. Extended (NIGUN-134 module A section 10) with plugin count and
            # pending task-queue depth -- a dashboard, not just a liveness bit.
            from .core import tasks as core_tasks
            from .core.registry import discover_plugins
            extra = {"plugins_loaded": len(discover_plugins()),
                     "task_queue_pending": core_tasks.queue_depth(c, t)}
            if query.get("deep") == ["1"]:
                from .core import monitoring
                ok, _ = audit.verify(c, t)
                extra["alerts"] = monitoring.active_alerts(c, t)
                return 200, {"ok": True, "audit_intact": ok, **extra}
            count = c.execute("SELECT COUNT(*) FROM audit WHERE tenant_id=?", (t,)).fetchone()[0]
            return 200, {"ok": True, "audit_count": count, **extra}
        if path == "/dashboard":
            return 200, command.dashboard(c, t)
        if path == "/leads" and method == "GET":
            return 200, leads.list_leads(c, t)
        if path == "/leads" and method == "POST":
            lid, dup = leads.add_lead(c, t, **{k: body.get(k) for k in
                                               ("text", "name", "phone", "service", "area", "budget",
                                                "urgency", "source") if body.get(k) is not None},
                                      consent=bool(body.get("consent")), actor="api")
            if lid is None:
                return 200, {"opted_out": True}
            lead = leads.get(c, t, lid)
            return 201, {"lead": lead, "duplicate_of": dup, "matches": brokerage.match_lead(c, t, lead),
                         "buyers": [b["id"] for b in buyers.matching_buyers(c, t, lead)]}
        if path.startswith("/leads/") and method == "GET":
            lead = leads.get(c, t, int(path.split("/")[2]))
            return (200, lead) if lead else (404, {"error": "not found"})
        if path == "/buyers" and method == "POST":
            return 201, {"id": buyers.add_buyer(c, t, body["name"], body.get("phone"), body.get("services", ""),
                                                body.get("areas", ""), body.get("price_per_lead", 0))}
        if path == "/buyers/topup":
            return 200, {"balance": buyers.top_up(c, t, int(body["buyer_id"]), int(body["amount"]))}
        if path == "/buyers/sell":
            return 200, {"balance": buyers.sell_lead(c, t, int(body["buyer_id"]), int(body["lead_id"]),
                                                     body.get("price"))}
        if path == "/suppliers" and method == "POST":
            return 201, {"id": brokerage.add_supplier(c, t, body["name"], body.get("phone"), body.get("keywords", ""),
                                                      body.get("areas", ""), body.get("commission_pct", 10))}
        if path == "/deals" and method == "POST":
            return 201, {"id": brokerage.propose(c, t, int(body["lead_id"]), int(body["supplier_id"]))}
        if path == "/deals/close":
            return 200, {"commission": brokerage.close_deal(c, t, int(body["deal_id"]), int(body["amount"]))}
        if path == "/simulate":
            return 200, brokerage.simulate_commission(float(body.get("avg_job", 1000)),
                                                      float(body.get("commission_pct", 10)),
                                                      float(body.get("deals_per_month", 10)),
                                                      float(body.get("close_rate_pct", 30)))
        if path == "/attribution":
            return 200, attribution.report(c, t)
        if path == "/kits" and method == "POST":
            kid = kits.create_kit(c, t, body["kind"], body["business"], body.get("industry", "general"),
                                  body.get("lead_id"), body.get("price", 0), body.get("details", ""))
            return 201, kits.get_kit(c, t, kid)
        if path == "/kits/deliver":
            return 200, {"ok": kits.deliver(c, t, int(body["kit_id"]), int(body["amount"]))}
        if path == "/wa/link":
            return 200, {"url": whatsapp.send_manual(c, t, body["phone"], body["text"], bool(body.get("consent")))}
        if path == "/experiments/assign":
            return 200, {"variant": experiments.assign(c, t, body["name"], body["subject"])}
        if path == "/command":
            return 200, command.run(c, t, body.get("text", ""))
        handler = self._plugin_routes.get((method, path))
        if handler:
            return handler(c, t, body, query)
        return 404, {"error": "unknown route"}

    # ---- WSGI ---------------------------------------------------------
    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/").rstrip("/") or "/"
        query = parse_qs(environ.get("QUERY_STRING", ""))
        remote = environ.get("REMOTE_ADDR", "")
        # /health is a liveness probe, not a route with anything sensitive in it, and a
        # hosting platform's own health check calls it from a non-localhost address with
        # no API key. Both gates below are for every OTHER route.
        if path != "/health":
            if self.api_key:
                # Timing-safe: a plain != leaks how many leading bytes of the guess were
                # right through how long the comparison takes.
                provided = environ.get("HTTP_X_API_KEY") or ""
                if hmac.compare_digest(provided, self.api_key):
                    pass  # admin: full access
                elif self.viewer_key and hmac.compare_digest(provided, self.viewer_key):
                    if method != "GET":
                        return self._send(start_response, 403, {"error": "viewer key is read-only"})
                else:
                    return self._send(start_response, 401, {"error": "bad api key"})
            elif remote not in ("127.0.0.1", "::1", ""):
                return self._send(start_response, 403, {"error": "set NEXUS_API_KEY to allow remote access"})
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
            if size > MAX_BODY_BYTES:
                return self._send(start_response, 413, {"error": "payload too large"})
            body = json.loads(environ["wsgi.input"].read(size) or b"{}") if size else {}
            status, data = self.route(method, path, body, query)
        except Blocked as e:
            status, data = 422, {"blocked": str(e)}
        except (KeyError, ValueError, TypeError) as e:
            status, data = 400, {"error": str(e)}
        except buyers.NotEnoughCredit as e:
            status, data = 402, {"error": str(e)}
        return self._send(start_response, status, data)

    @staticmethod
    def _send(start_response, status, data):
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        names = {200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized", 402: "Payment Required",
                 403: "Forbidden", 404: "Not Found", 413: "Payload Too Large", 422: "Unprocessable Entity"}
        # No Access-Control-Allow-Origin: no page served by this project calls this API from
        # a browser, and dist-allowlist.json already refuses any outbound URL that is not
        # wa.me/tel: -- a wildcard CORS header here would open a door nothing here uses.
        start_response(f"{status} {names.get(status, '')}", [("Content-Type", "application/json; charset=utf-8"),
                                                              ("Content-Length", str(len(raw)))])
        return [raw]
