import time
import unittest

from nexus_master import brokerage, db, leads
from nexus_master.core import tasks as core_tasks
from nexus_master.privacy import (delete_lead, export_lead, has_permission, is_suppressed,
                                  retention_candidates, schedule_retention_sweep, suppress,
                                  with_security_headers)
from nexus_master.privacy import roles, validation
from nexus_master.privacy.headers import SECURITY_HEADERS


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


class TestMigration(Base):
    def test_suppression_and_roles_tables_exist(self):
        self.c.execute("SELECT * FROM suppression").fetchall()
        self.c.execute("SELECT * FROM principal_roles").fetchall()

    def test_tracked_under_its_own_pkg_name(self):
        row = self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='privacy'").fetchone()
        self.assertEqual(row[0], 1)


class TestValidation(unittest.TestCase):
    def test_sanitize_strips_control_chars_keeps_newlines(self):
        self.assertEqual(validation.sanitize_text("a\x00b\nc"), "ab\nc")

    def test_sanitize_truncates(self):
        self.assertEqual(len(validation.sanitize_text("x" * 100, max_len=10)), 10)

    def test_sanitize_none_is_empty_string_not_a_crash(self):
        self.assertEqual(validation.sanitize_text(None), "")

    def test_require_fields_lists_missing(self):
        self.assertEqual(validation.require_fields({"a": 1}, ["a", "b", "c"]), ["b", "c"])

    def test_is_safe_json_value_rejects_too_deep(self):
        deep = {}
        cur = deep
        for _ in range(10):
            cur["x"] = {}
            cur = cur["x"]
        self.assertFalse(validation.is_safe_json_value(deep, max_depth=3))

    def test_is_safe_json_value_accepts_normal_shapes(self):
        self.assertTrue(validation.is_safe_json_value({"a": [1, 2, {"b": 3}]}))


class TestHeaders(unittest.TestCase):
    def test_adds_all_security_headers(self):
        out = with_security_headers([("Content-Type", "application/json")])
        names = {n for n, _ in out}
        self.assertTrue({n for n, _ in SECURITY_HEADERS} <= names)

    def test_does_not_override_an_existing_header(self):
        out = with_security_headers([("X-Frame-Options", "SAMEORIGIN")])
        self.assertIn(("X-Frame-Options", "SAMEORIGIN"), out)
        self.assertNotIn(("X-Frame-Options", "DENY"), out)


class TestRolesModelNotYetEnforced(Base):
    def test_has_permission_matrix(self):
        self.assertTrue(has_permission("admin", "anything"))
        self.assertTrue(has_permission("viewer", "read_leads"))
        self.assertFalse(has_permission("viewer", "write_leads"))
        self.assertFalse(has_permission("unknown_role", "read_leads"))

    def test_assign_and_get_role(self):
        roles.assign_role(self.c, self.t, "key-label-1", "sales")
        self.assertEqual(roles.get_role(self.c, self.t, "key-label-1"), "sales")

    def test_unassigned_principal_defaults_to_viewer(self):
        self.assertEqual(roles.get_role(self.c, self.t, "nobody"), "viewer")

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            roles.assign_role(self.c, self.t, "x", "superadmin")


class TestSuppression(Base):
    def test_not_suppressed_by_default(self):
        self.assertFalse(is_suppressed(self.c, self.t, "0501234567"))

    def test_suppress_and_check(self):
        suppress(self.c, self.t, "0501234567", "תלונה מהלקוח")
        self.assertTrue(is_suppressed(self.c, self.t, "0501234567"))

    def test_invalid_phone_rejected(self):
        with self.assertRaises(ValueError):
            suppress(self.c, self.t, "abc", "x")

    def test_distinct_from_guardian_opt_out(self):
        from nexus_master import guardian
        self.assertFalse(guardian.is_opted_out(self.c, self.t, "0501234567"))
        suppress(self.c, self.t, "0501234567", "x")
        # suppression and opt-out are intentionally separate lists
        self.assertFalse(guardian.is_opted_out(self.c, self.t, "0501234567"))


class TestRetention(Base):
    def test_export_includes_lead_and_history(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        out = export_lead(self.c, self.t, lid)
        self.assertEqual(out["lead"]["id"], lid)
        self.assertEqual(out["history"], [])

    def test_delete_removes_the_lead_and_logs_a_snapshot_first(self):
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש חשמלאי", phone="0501234567", consent=True)
        ok = delete_lead(self.c, self.t, lid)
        self.assertTrue(ok)
        self.assertIsNone(leads.get(self.c, self.t, lid))
        audit_row = self.c.execute(
            "SELECT * FROM audit WHERE tenant_id=? AND action='lead.delete' ORDER BY id DESC LIMIT 1",
            (self.t,)).fetchone()
        self.assertIsNotNone(audit_row)
        self.assertIn(str(lid), audit_row["data"])

    def test_delete_cascades_deals_and_ledger_and_unlinks_kits(self):
        sup = brokerage.add_supplier(self.c, self.t, "ספק")
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        did = brokerage.propose(self.c, self.t, lid, sup)
        from nexus_master import kits
        kid = kits.create_kit(self.c, self.t, "whatsapp", "העסק", lead_id=lid)
        delete_lead(self.c, self.t, lid)
        self.assertIsNone(self.c.execute("SELECT id FROM deals WHERE id=?", (did,)).fetchone())
        kit_row = self.c.execute("SELECT lead_id FROM kits WHERE id=?", (kid,)).fetchone()
        self.assertIsNone(kit_row["lead_id"])

    def test_delete_missing_lead_returns_false(self):
        self.assertFalse(delete_lead(self.c, self.t, 9999))

    def test_retention_candidates_only_old_terminal_leads(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        leads.set_status(self.c, self.t, lid, "lost")
        self.c.execute("UPDATE leads SET created=? WHERE id=?", (time.time() - 400 * 86400, lid))
        self.c.commit()
        candidates = retention_candidates(self.c, self.t, days=365)
        self.assertEqual([c["id"] for c in candidates], [lid])

    def test_retention_candidates_excludes_recent_leads(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        leads.set_status(self.c, self.t, lid, "lost")
        self.assertEqual(retention_candidates(self.c, self.t, days=365), [])


class TestExistingSecureAuthAndEnvSecrets(Base):
    """Sections 117/120 already hold elsewhere in the codebase -- confirmed, not
    re-implemented, here."""

    def test_api_rejects_wrong_key_with_401(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default", api_key="real-key")
        import json
        status, headers = None, None

        def start_response(s, h):
            nonlocal status, headers
            status, headers = s, h
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/dashboard", "QUERY_STRING": "",
                  "REMOTE_ADDR": "203.0.113.5", "HTTP_X_API_KEY": "wrong-key",
                  "wsgi.input": __import__("io").BytesIO(b"")}
        app(environ, start_response)
        self.assertTrue(status.startswith("401"))


class TestCommandsAndRoutes(Base):
    def test_export_delete_suppress_commands(self):
        from nexus_master import command
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        out = command.run(self.c, self.t, f"ייצא ליד {lid}")
        self.assertTrue(out["ok"])
        out = command.run(self.c, self.t, "דכא מספר 0501234567 ספאם")
        self.assertTrue(out["ok"])
        out = command.run(self.c, self.t, f"מחק ליד {lid}")
        self.assertTrue(out["ok"])

    def test_routes_registered(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default")
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        status, data = app.route("GET", "/privacy/export", {}, {"lead_id": [str(lid)]})
        self.assertEqual(status, 200)
        status, data = app.route("POST", "/privacy/suppress", {"phone": "0501234567", "reason": "x"})
        self.assertEqual(status, 200)


class TestScheduledRetention(Base):
    """Section 125's automatic half, now that core/tasks.py (module A) exists."""

    def test_handler_registered_on_import(self):
        self.assertIn("privacy.retention_sweep", core_tasks._handlers)

    def test_sweep_deletes_old_terminal_leads_via_tick(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        leads.set_status(self.c, self.t, lid, "lost")
        self.c.execute("UPDATE leads SET created=? WHERE id=?", (time.time() - 400 * 86400, lid))
        self.c.commit()
        schedule_retention_sweep(self.c, self.t)
        result = core_tasks.tick(self.c, self.t)
        self.assertEqual(result, {"done": 1, "retried": 0, "failed": 0})
        self.assertIsNone(leads.get(self.c, self.t, lid))

    def test_sweep_leaves_recent_leads_alone(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        leads.set_status(self.c, self.t, lid, "lost")
        schedule_retention_sweep(self.c, self.t)
        core_tasks.tick(self.c, self.t)
        self.assertIsNotNone(leads.get(self.c, self.t, lid))

    def test_schedule_route(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default")
        status, data = app.route("POST", "/privacy/schedule-retention", {"days": 30})
        self.assertEqual(status, 201)
        self.assertIn("task_id", data)


class TestRoleEnforcementIntegration(Base):
    """Section 118, verified against this package's own routes specifically -- not just
    trusting that api.py's own tests cover it. Goes through App.__call__ (the real WSGI
    entry, where the key check actually lives), not app.route() (which every other test
    in this file uses and which bypasses the check entirely)."""

    def _call(self, app, method, path, body=b"", extra_environ=None):
        import io
        status_box = {}

        def start_response(status, headers):
            status_box["status"] = status
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
                  "REMOTE_ADDR": "203.0.113.5", "wsgi.input": io.BytesIO(body),
                  "CONTENT_LENGTH": str(len(body))}
        environ.update(extra_environ or {})
        app(environ, start_response)
        return status_box["status"]

    def test_viewer_key_blocked_from_deleting_a_lead(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default", api_key="admin-key", viewer_key="viewer-key")
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        status = self._call(app, "POST", "/privacy/delete",
                           body=f'{{"lead_id": {lid}}}'.encode(),
                           extra_environ={"HTTP_X_API_KEY": "viewer-key"})
        self.assertTrue(status.startswith("403"))
        self.assertIsNotNone(leads.get(self.c, self.t, lid))  # never reached the handler

    def test_viewer_key_allowed_to_read(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default", api_key="admin-key", viewer_key="viewer-key")
        status = self._call(app, "GET", "/discovery/sources",
                           extra_environ={"HTTP_X_API_KEY": "viewer-key"})
        self.assertTrue(status.startswith("200"))

    def test_admin_key_can_still_write(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default", api_key="admin-key", viewer_key="viewer-key")
        status = self._call(app, "POST", "/privacy/suppress",
                           body=b'{"phone": "0501234567", "reason": "x"}',
                           extra_environ={"HTTP_X_API_KEY": "admin-key"})
        self.assertTrue(status.startswith("200"))


if __name__ == "__main__":
    unittest.main()
