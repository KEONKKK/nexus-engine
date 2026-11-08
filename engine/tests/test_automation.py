import time
import unittest

from nexus_master import automation, db, leads
from nexus_master.core import events


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)
        self.lid, _ = leads.add_lead(
            self.c, self.t, text="מחפש חשמלאי דחוף", phone="0501234567", service="חשמלאי", consent=True)

    def tearDown(self):
        events.unsubscribe_all()
        automation.reset_for_tests()


class TestMigrationsAndPlugin(Base):
    def test_migrations_ran_and_idempotent(self):
        v1 = self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='automation'").fetchone()[0]
        self.assertIsNotNone(v1)
        db.migrate(self.c)
        v2 = self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='automation'").fetchone()[0]
        self.assertEqual(v1, v2)

    def test_discovered(self):
        from nexus_master.core.registry import discover_plugins
        names = [p.__name__ for p in discover_plugins(refresh=True)]
        self.assertIn("nexus_master.automation", names)


class TestWorkflowCRUD(Base):
    def test_bad_trigger_type_rejected(self):
        with self.assertRaises(ValueError):
            automation.create_workflow(self.c, self.t, name="x", trigger_type="weather",
                                       trigger_value="rain", action_type="notify")

    def test_bad_action_type_rejected(self):
        with self.assertRaises(ValueError):
            automation.create_workflow(self.c, self.t, name="x", trigger_type="event",
                                       trigger_value="e", action_type="teleport")

    def test_enable_disable(self):
        wid = automation.create_workflow(self.c, self.t, name="x", trigger_type="event",
                                         trigger_value="e", action_type="notify")
        self.assertTrue(automation.list_workflows(self.c, self.t)[0]["enabled"])
        automation.set_enabled(self.c, self.t, wid, False)
        self.assertEqual(automation.list_workflows(self.c, self.t, enabled_only=True), [])
        automation.set_enabled(self.c, self.t, wid, True)
        self.assertEqual(len(automation.list_workflows(self.c, self.t, enabled_only=True)), 1)


class TestActions(Base):
    def test_assign_action(self):
        wf = {"id": 1, "action_type": "assign", "action_value": "דני"}
        automation.apply_action(self.c, self.t, wf, self.lid)
        from nexus_master import crm
        self.assertEqual(crm.current_assignee(self.c, self.t, self.lid), "דני")

    def test_set_status_action(self):
        wf = {"id": 1, "action_type": "set_status", "action_value": "contacted"}
        automation.apply_action(self.c, self.t, wf, self.lid)
        self.assertEqual(leads.get(self.c, self.t, self.lid)["status"], "contacted")

    def test_reminder_action_defaults_to_60_minutes(self):
        before = time.time()
        wf = {"id": 1, "action_type": "reminder", "action_value": None}
        automation.apply_action(self.c, self.t, wf, self.lid)
        from nexus_master import crm
        due = crm.due_reminders(self.c, self.t, now=before + 59 * 60)
        self.assertEqual(due, [])  # not due yet at 59 minutes
        due = crm.due_reminders(self.c, self.t, now=before + 61 * 60)
        self.assertEqual(len(due), 1)

    def test_escalate_action_creates_urgent_reminder_and_notifies(self):
        wf = {"id": 1, "action_type": "escalate", "action_value": None}
        result = automation.apply_action(self.c, self.t, wf, self.lid)
        self.assertTrue(result["ok"])
        self.assertEqual(result["channel"], "wa_link")
        from nexus_master import crm
        self.assertEqual(len(crm.due_reminders(self.c, self.t)), 1)

    def test_unknown_action_type_raises(self):
        wf = {"id": 1, "action_type": "nope", "action_value": None}
        with self.assertRaises(ValueError):
            automation.apply_action(self.c, self.t, wf, self.lid)


class TestEventTriggered(Base):
    def test_trigger_event_runs_matching_workflow(self):
        automation.create_workflow(self.c, self.t, name="assign on new lead", trigger_type="event",
                                   trigger_value="lead.created", action_type="assign", action_value="דני")
        results = automation.trigger_event(self.c, self.t, "lead.created", self.lid)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "done")

    def test_condition_filters_out_non_matching_leads(self):
        automation.create_workflow(self.c, self.t, name="only contacted", trigger_type="event",
                                   trigger_value="x", action_type="set_status", action_value="won",
                                   condition={"status": "contacted"})
        results = automation.trigger_event(self.c, self.t, "x", self.lid)  # lead status is "new"
        self.assertEqual(results, [])

    def test_idempotent_second_call_does_not_rerun(self):
        automation.create_workflow(self.c, self.t, name="w", trigger_type="event",
                                   trigger_value="x", action_type="assign", action_value="דני")
        automation.trigger_event(self.c, self.t, "x", self.lid)
        from nexus_master import crm
        crm.assign(self.c, self.t, self.lid, "רותי")  # manually override
        automation.trigger_event(self.c, self.t, "x", self.lid)  # fires again, same idem key
        # still "רותי" - the second dispatch was a no-op read, not a re-run of assign("דני")
        self.assertEqual(crm.current_assignee(self.c, self.t, self.lid), "רותי")

    def test_disabled_workflow_does_not_fire(self):
        wid = automation.create_workflow(self.c, self.t, name="w", trigger_type="event",
                                         trigger_value="x", action_type="assign", action_value="דני")
        automation.set_enabled(self.c, self.t, wid, False)
        self.assertEqual(automation.trigger_event(self.c, self.t, "x", self.lid), [])

    def test_globally_paused_blocks_everything(self):
        automation.create_workflow(self.c, self.t, name="w", trigger_type="event",
                                   trigger_value="x", action_type="assign", action_value="דני")
        automation.set_global_pause(self.c, self.t, True)
        self.assertEqual(automation.trigger_event(self.c, self.t, "x", self.lid), [])

    def test_real_event_bus_dispatches_through_subscription(self):
        automation.create_workflow(self.c, self.t, name="w", trigger_type="event",
                                   trigger_value="crm.lead_assigned", action_type="notify")
        automation.ensure_event_subscription()
        from nexus_master import crm
        crm.assign(self.c, self.t, self.lid, "דני")  # emits crm.lead_assigned itself
        history = automation.run_history(self.c, self.t)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "done")

    def test_subscribing_twice_does_not_double_fire(self):
        automation.create_workflow(self.c, self.t, name="w", trigger_type="event",
                                   trigger_value="crm.lead_assigned", action_type="notify")
        automation.ensure_event_subscription()
        automation.ensure_event_subscription()  # must be a no-op the second time
        from nexus_master import crm
        crm.assign(self.c, self.t, self.lid, "דני")
        self.assertEqual(len(automation.run_history(self.c, self.t)), 1)


class TestTimeTriggered(Base):
    def test_stale_lead_workflow_fires_once_past_threshold(self):
        from nexus_master.core import config
        config.set(self.c, self.t, "stale_lead_hours", "1")
        automation.create_workflow(self.c, self.t, name="stale", trigger_type="time",
                                   trigger_value="stale_lead", action_type="escalate")
        now = time.time()
        result = automation.run_time_based(self.c, self.t, now=now)
        self.assertEqual(result["ran"], 0)  # lead was just created, not stale yet
        result = automation.run_time_based(self.c, self.t, now=now + 2 * 3600)
        self.assertEqual(result["ran"], 1)
        # running again at the same (later) "now" must not re-escalate - one-time fact
        result = automation.run_time_based(self.c, self.t, now=now + 3 * 3600)
        self.assertEqual(result["ran"], 0)

    def test_generic_daily_workflow_fires_once_per_day_per_lead(self):
        automation.create_workflow(self.c, self.t, name="digest", trigger_type="time",
                                   trigger_value="daily_digest", action_type="notify")
        day1 = time.mktime((2026, 11, 8, 12, 0, 0, 0, 0, 0))
        r1 = automation.run_time_based(self.c, self.t, now=day1)
        self.assertEqual(r1["ran"], 1)
        r1b = automation.run_time_based(self.c, self.t, now=day1 + 3600)  # same day
        self.assertEqual(r1b["ran"], 0)
        day2 = day1 + 86400
        r2 = automation.run_time_based(self.c, self.t, now=day2)
        self.assertEqual(r2["ran"], 1)

    def test_paused_skips_time_based_too(self):
        automation.create_workflow(self.c, self.t, name="digest", trigger_type="time",
                                   trigger_value="daily_digest", action_type="notify")
        automation.set_global_pause(self.c, self.t, True)
        result = automation.run_time_based(self.c, self.t)
        self.assertEqual(result["ran"], 0)
        self.assertTrue(result["paused"])


class TestRetry(Base):
    def test_failed_run_can_be_retried_and_succeed(self):
        wid = automation.create_workflow(self.c, self.t, name="bad-then-fixed", trigger_type="event",
                                         trigger_value="x", action_type="set_status", action_value="not_a_real_status")
        automation.trigger_event(self.c, self.t, "x", self.lid)
        history = automation.run_history(self.c, self.t)
        self.assertEqual(history[0]["status"], "failed")
        self.assertEqual(history[0]["attempt"], 1)
        # "fix" the workflow's action_value directly (as an admin would), then retry
        self.c.execute("UPDATE workflows SET action_value='contacted' WHERE id=?", (wid,))
        self.c.commit()
        retried = automation.retry_failed(self.c, self.t)
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["status"], "done")
        self.assertEqual(leads.get(self.c, self.t, self.lid)["status"], "contacted")

    def test_retry_stops_after_max_attempts(self):
        automation.create_workflow(self.c, self.t, name="always-bad", trigger_type="event",
                                   trigger_value="x", action_type="set_status", action_value="still_not_real")
        automation.trigger_event(self.c, self.t, "x", self.lid)
        for _ in range(automation.MAX_RETRY_ATTEMPTS):
            automation.retry_failed(self.c, self.t)
        # one more retry sweep must find nothing left eligible (attempt has reached the cap)
        self.assertEqual(automation.retry_failed(self.c, self.t), [])


class TestSweepScheduling(Base):
    def test_ensure_sweep_scheduled_is_idempotent(self):
        automation.ensure_sweep_scheduled(self.c, self.t)
        automation.ensure_sweep_scheduled(self.c, self.t)
        pending = self.c.execute(
            "SELECT COUNT(*) FROM core_tasks WHERE tenant_id=? AND kind='automation.sweep' AND status='pending'",
            (self.t,)).fetchone()[0]
        self.assertEqual(pending, 1)

    def test_tick_runs_sweep_and_reschedules_itself(self):
        from nexus_master.core import tasks as core_tasks
        core_tasks.enqueue(self.c, self.t, "automation.sweep", run_at=time.time() - 1)
        result = core_tasks.tick(self.c, self.t)
        self.assertEqual(result["done"], 1)
        pending = self.c.execute(
            "SELECT COUNT(*) FROM core_tasks WHERE tenant_id=? AND kind='automation.sweep' AND status='pending'",
            (self.t,)).fetchone()[0]
        self.assertEqual(pending, 1)  # the handler re-enqueued its own successor


class TestAdminRoutesAndCommands(Base):
    def test_routes_registered_and_reachable(self):
        from nexus_master.api import App
        app = App(self.c, api_key=None)
        status, data = app.route("POST", "/automation/workflows", {
            "name": "w", "trigger_type": "event", "trigger_value": "x", "action_type": "notify"}, {})
        self.assertEqual(status, 201)
        wid = data["id"]
        status, data = app.route("GET", "/automation/workflows", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["workflows"]), 1)
        status, data = app.route("POST", "/automation/workflows/disable", {"id": wid}, {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, data = app.route("POST", "/automation/pause", {}, {})
        self.assertTrue(data["paused"])
        status, data = app.route("POST", "/automation/resume", {}, {})
        self.assertFalse(data["paused"])
        status, data = app.route("POST", "/automation/run_now", {}, {})
        self.assertEqual(status, 200)
        status, data = app.route("GET", "/automation/runs", {}, {})
        self.assertEqual(status, 200)

    def test_hebrew_commands(self):
        from nexus_master import command
        r = command.run(self.c, self.t, "עצור אוטומציה")
        self.assertTrue(r["ok"])
        self.assertTrue(automation.is_globally_paused(self.c, self.t))
        r = command.run(self.c, self.t, "הפעל אוטומציה")
        self.assertFalse(automation.is_globally_paused(self.c, self.t))
        r = command.run(self.c, self.t, "הרץ אוטומציה")
        self.assertTrue(r["ok"])


if __name__ == "__main__":
    unittest.main()
