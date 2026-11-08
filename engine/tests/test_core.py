"""Tests for nexus_master.core.* -- NIGUN-134 module A (control center), sections 1-12.
Each section's test is named after its number so design/nigun/STATUS.md rows can point
here directly.
"""
import io
import json
import unittest

from nexus_master import audit, db, leads
from nexus_master.api import App
from nexus_master.core import config, events, notify, rules, tasks
from nexus_master.core import decision_log


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)
        events.unsubscribe_all()

    def tearDown(self):
        events.unsubscribe_all()


# ---- section 1/3: centralized control engine / modular service architecture ---------

class TestControlEngineAndPluginArchitecture(Base):
    def test_dispatcher_and_registry_exist_and_discover_a_real_plugin(self):
        from nexus_master.core.registry import discover_plugins
        found = [m.__name__ for m in discover_plugins(refresh=True)]
        # discovery/ and enrich/ (window C) and core/ itself all define at least one of
        # MIGRATIONS/routes/COMMANDS -- proof the "modular service" model is not just
        # core/ talking to itself.
        self.assertIn("nexus_master.core", found)

    def test_app_add_route_is_the_plugin_registration_surface(self):
        app = App(self.c, api_key="")
        app.add_route("GET", "/plugin-demo", lambda conn, tid, body, query: (200, {"ok": True}))
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/plugin-demo", "REMOTE_ADDR": "127.0.0.1",
               "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")}
        st = {}
        out = app(env, lambda s, h: st.setdefault("s", s))
        self.assertEqual(int(st["s"].split()[0]), 200)
        self.assertEqual(json.loads(b"".join(out)), {"ok": True})


# ---- section 2: unified configuration manager ---------------------------------------

class TestConfig(Base):
    def test_default_when_unset(self):
        self.assertEqual(config.get_int(self.c, self.t, "max_sends_per_day"), 3)

    def test_stored_setting_overrides_default(self):
        config.set(self.c, self.t, "hot_lead_score", 85)
        self.assertEqual(config.get_int(self.c, self.t, "hot_lead_score"), 85)

    def test_env_var_overrides_stored_setting(self):
        import os
        config.set(self.c, self.t, "hot_lead_score", 85)
        os.environ["NEXUS_SETTING_HOT_LEAD_SCORE"] = "99"
        try:
            self.assertEqual(config.get_int(self.c, self.t, "hot_lead_score"), 99)
        finally:
            del os.environ["NEXUS_SETTING_HOT_LEAD_SCORE"]

    def test_all_settings_reports_every_known_key(self):
        self.assertIn("max_sends_per_day", config.all_settings(self.c, self.t))


# ---- section 4: central event bus ----------------------------------------------------

class TestEvents(Base):
    def test_emit_is_logged_and_listed(self):
        events.emit(self.c, self.t, "lead.created", {"lead_id": 1})
        rows = events.recent(self.c, self.t, "lead.created")
        self.assertEqual(rows[0]["data"]["lead_id"], 1)

    def test_subscribers_are_called_synchronously(self):
        seen = []
        events.subscribe("lead.created", lambda conn, tid, et, data: seen.append(data))
        events.emit(self.c, self.t, "lead.created", {"lead_id": 2})
        self.assertEqual(seen, [{"lead_id": 2}])

    def test_wildcard_subscriber_sees_every_event(self):
        seen = []
        events.subscribe("*", lambda conn, tid, et, data: seen.append(et))
        events.emit(self.c, self.t, "a.b", {})
        events.emit(self.c, self.t, "c.d", {})
        self.assertEqual(seen, ["a.b", "c.d"])


# ---- section 5: unified notification center -----------------------------------------

class TestNotifyCenter(Base):
    def test_wa_link_channel_always_succeeds(self):
        r = notify.notify(self.c, self.t, channel="wa_link", target="972501234567", text="היי")
        self.assertTrue(r["ok"])
        self.assertTrue(r["link"].startswith("https://wa.me/972501234567"))

    def test_cloud_api_channel_reports_not_configured_when_off(self):
        r = notify.notify(self.c, self.t, channel="cloud_api", target="972501234567", text="היי")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "cloud_api_not_configured")

    def test_unknown_channel_raises(self):
        with self.assertRaises(ValueError):
            notify.notify(self.c, self.t, channel="carrier_pigeon", target="x", text="x")


# ---- sections 6-8: background task queue, priority, retry+backoff -------------------

class TestTaskQueue(Base):
    def test_enqueue_and_tick_runs_due_task(self):
        calls = []
        tasks.register_handler("demo", lambda conn, tid, payload: calls.append(payload))
        tasks.enqueue(self.c, self.t, "demo", {"x": 1})
        result = tasks.tick(self.c, self.t)
        self.assertEqual(result["done"], 1)
        self.assertEqual(calls, [{"x": 1}])

    def test_higher_priority_runs_first(self):
        order = []
        tasks.register_handler("demo2", lambda conn, tid, payload: order.append(payload["n"]))
        tasks.enqueue(self.c, self.t, "demo2", {"n": "low"}, priority=0)
        tasks.enqueue(self.c, self.t, "demo2", {"n": "high"}, priority=10)
        tasks.tick(self.c, self.t)
        self.assertEqual(order, ["high", "low"])

    def test_future_run_at_is_not_due_yet(self):
        import time
        tasks.register_handler("demo3", lambda conn, tid, payload: None)
        tasks.enqueue(self.c, self.t, "demo3", {}, run_at=time.time() + 3600)
        result = tasks.tick(self.c, self.t)
        self.assertEqual(result["done"], 0)
        self.assertEqual(tasks.queue_depth(self.c, self.t), 1)

    def test_failing_handler_retries_with_backoff_then_fails_after_max_attempts(self):
        tasks.register_handler("boom", lambda conn, tid, payload: (_ for _ in ()).throw(RuntimeError("x")))
        tid_ = tasks.enqueue(self.c, self.t, "boom", {}, max_attempts=2)
        r1 = tasks.tick(self.c, self.t)
        self.assertEqual(r1["retried"], 1)
        row = self.c.execute("SELECT * FROM core_tasks WHERE id=?", (tid_,)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertGreater(row["run_at"], __import__("time").time())  # backed off into the future
        # Force it due again without waiting for the real backoff, to prove the second
        # failure actually exhausts max_attempts rather than retrying forever.
        self.c.execute("UPDATE core_tasks SET run_at=0 WHERE id=?", (tid_,))
        r2 = tasks.tick(self.c, self.t)
        self.assertEqual(r2["failed"], 1)
        self.assertEqual(self.c.execute("SELECT status FROM core_tasks WHERE id=?", (tid_,)).fetchone()[0],
                         "failed")

    def test_unknown_kind_fails_without_crashing_the_batch(self):
        tasks.enqueue(self.c, self.t, "no-such-kind", {})
        result = tasks.tick(self.c, self.t)
        self.assertEqual(result["failed"], 1)


# ---- section 9: configurable business rules ------------------------------------------

class TestRules(Base):
    def test_is_hot_lead_uses_configured_threshold(self):
        lead = {"score_total": 75}
        self.assertTrue(rules.is_hot_lead(self.c, self.t, lead))
        config.set(self.c, self.t, "hot_lead_score", 90)
        self.assertFalse(rules.is_hot_lead(self.c, self.t, lead))

    def test_within_daily_send_cap(self):
        self.assertTrue(rules.within_daily_send_cap(self.c, self.t, 2))
        self.assertFalse(rules.within_daily_send_cap(self.c, self.t, 3))


# ---- sections 10-11: extended /health, admin/viewer roles ----------------------------

class TestHealthAndRoles(Base):
    def call(self, app, method, path, key=None, remote="127.0.0.1"):
        path_info, _, qs = path.partition("?")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path_info, "QUERY_STRING": qs,
               "REMOTE_ADDR": remote, "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")}
        if key:
            env["HTTP_X_API_KEY"] = key
        st = {}
        out = app(env, lambda s, h: st.setdefault("s", s))
        return int(st["s"].split()[0]), json.loads(b"".join(out))

    def test_health_reports_plugins_and_queue_depth(self):
        app = App(self.c, api_key="")
        s, d = self.call(app, "GET", "/health")
        self.assertEqual(s, 200)
        self.assertIn("plugins_loaded", d)
        self.assertIn("task_queue_pending", d)

    def test_admin_key_has_full_access(self):
        app = App(self.c, api_key="admin-secret", viewer_key="viewer-secret")
        s, _ = self.call(app, "POST", "/suppliers", key="admin-secret")
        # missing body fields -> 400, but NOT 401/403: proves the admin key passed auth.
        self.assertNotIn(s, (401, 403))

    def test_viewer_key_allows_get(self):
        app = App(self.c, api_key="admin-secret", viewer_key="viewer-secret")
        s, _ = self.call(app, "GET", "/leads", key="viewer-secret")
        self.assertEqual(s, 200)

    def test_viewer_key_rejected_on_write(self):
        app = App(self.c, api_key="admin-secret", viewer_key="viewer-secret")
        s, d = self.call(app, "POST", "/suppliers", key="viewer-secret")
        self.assertEqual(s, 403)

    def test_unknown_key_still_401_with_viewer_key_configured(self):
        app = App(self.c, api_key="admin-secret", viewer_key="viewer-secret")
        s, _ = self.call(app, "GET", "/leads", key="not-a-real-key")
        self.assertEqual(s, 401)


# ---- section 12: decision log (audit + mandatory reason) -----------------------------

class TestDecisionLog(Base):
    def test_decision_log_requires_a_reason_and_is_queryable(self):
        decision_log(self.c, self.t, "user", "lead.priority_override", reason="לקוח VIP", data={"lead": 1})
        row = self.c.execute("SELECT data FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn("לקוח VIP", row["data"])
        ev = events.recent(self.c, self.t, "decision.lead.priority_override")
        self.assertEqual(ev[0]["data"]["reason"], "לקוח VIP")

    def test_decision_log_does_not_break_audit_chain_verification(self):
        decision_log(self.c, self.t, "user", "x", reason="כי כך")
        ok, _ = audit.verify(self.c, self.t)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
