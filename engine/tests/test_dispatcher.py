"""Tests for nexus_master.dispatcher.Dispatcher -- the single WSGI entrypoint for `serve`
that routes /public/* to the public proxy (with CORS restricted to the live site) and
everything else to the unchanged admin API. Direct WSGI calls, no socket, no server.
"""
import io
import json
import unittest

from nexus_master import db
from nexus_master.dispatcher import ALLOWED_PUBLIC_ORIGIN, Dispatcher


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.app = Dispatcher(self.c)

    def call(self, method, path, body=None, remote="127.0.0.1"):
        raw = json.dumps(body).encode("utf-8") if body is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "REMOTE_ADDR": remote, "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
        st = {}
        out = self.app(env, lambda s, h: st.update(s=s, h=h))
        body_out = b"".join(out)
        return int(st["s"].split()[0]), dict(st["h"]), (json.loads(body_out) if body_out else None)


class TestPublicRoutesGetCors(Base):
    def test_public_health_has_cors_header(self):
        s, h, d = self.call("GET", "/public/health")
        self.assertEqual(s, 200)
        self.assertEqual(d, {"ok": True})
        self.assertEqual(h.get("Access-Control-Allow-Origin"), ALLOWED_PUBLIC_ORIGIN)

    def test_options_preflight_to_public_leads_is_204_with_cors(self):
        s, h, d = self.call("OPTIONS", "/public/leads")
        self.assertEqual(s, 204)
        self.assertEqual(h.get("Access-Control-Allow-Origin"), ALLOWED_PUBLIC_ORIGIN)
        self.assertIn("POST", h.get("Access-Control-Allow-Methods", ""))

    def test_public_leads_post_succeeds_through_dispatcher(self):
        s, h, d = self.call("POST", "/public/leads", {
            "category": "pro", "text": "מחפש חשמלאי", "phone": "+972501230009",
            "trade": "חשמלאי", "consent": True})
        self.assertEqual(s, 201)
        self.assertTrue(d["ok"])
        self.assertEqual(h.get("Access-Control-Allow-Origin"), ALLOWED_PUBLIC_ORIGIN)


class TestAdminRoutesUnaffected(Base):
    def test_admin_health_has_no_cors_header(self):
        s, h, d = self.call("GET", "/health")
        self.assertEqual(s, 200)
        self.assertNotIn("Access-Control-Allow-Origin", h)

    def test_admin_unknown_route_404_unaffected(self):
        s, h, d = self.call("GET", "/no-such-admin-route")
        self.assertEqual(s, 404)
        self.assertNotIn("Access-Control-Allow-Origin", h)


if __name__ == "__main__":
    unittest.main()
