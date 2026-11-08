import os
import unittest

from nexus_master import db, leads
from nexus_master.core import tasks as core_tasks
from nexus_master.discovery import (list_sources, register_source, run_all, run_source,
                                    schedule_run)
from nexus_master.discovery import connectors, filters
from nexus_master.discovery.connectors import LeadAdsConnector, PasteConnector, RssConnector


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


class TestMigrationRegistered(Base):
    def test_discovery_sources_table_exists(self):
        self.c.execute("SELECT * FROM discovery_sources").fetchall()  # raises if missing

    def test_tracked_under_its_own_pkg_name(self):
        row = self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='discovery'").fetchone()
        self.assertEqual(row[0], 1)


class TestPasteConnector(unittest.TestCase):
    def test_wraps_pasted_text(self):
        self.assertEqual(PasteConnector().fetch({"text": "מחפש חשמלאי בחולון"}),
                         [{"text": "מחפש חשמלאי בחולון"}])

    def test_empty_text_yields_nothing(self):
        self.assertEqual(PasteConnector().fetch({}), [])


RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>דרוש אינסטלטור דחוף</title><description>נזילה בדירה בתל אביב</description></item>
<item><title></title><description></description></item>
</channel></rss>"""

ATOM_SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>עסק חדש נפתח</title><summary>חשמלאי חדש בפתח תקווה</summary></entry>
</feed>"""


class TestRssConnector(unittest.TestCase):
    def test_parses_rss2_items_and_skips_empty_ones(self):
        out = RssConnector.parse(RSS_SAMPLE)
        self.assertEqual(len(out), 1)
        self.assertIn("דרוש אינסטלטור דחוף", out[0]["text"])
        self.assertIn("נזילה בדירה בתל אביב", out[0]["text"])

    def test_parses_atom_entries(self):
        out = RssConnector.parse(ATOM_SAMPLE)
        self.assertEqual(len(out), 1)
        self.assertIn("חשמלאי חדש בפתח תקווה", out[0]["text"])

    def test_fetch_without_url_raises_before_any_network_call(self):
        with self.assertRaises(ValueError):
            RssConnector().fetch({})


class TestLeadAdsConnectorBlocked(unittest.TestCase):
    def test_disabled_without_env_token(self):
        os.environ.pop(LeadAdsConnector.ENV_VAR, None)
        self.assertFalse(LeadAdsConnector().enabled)

    def test_fetch_raises_clear_blocked_error_when_disabled(self):
        conn = LeadAdsConnector(token=None)
        with self.assertRaises(RuntimeError):
            conn.fetch({"form_id": "123"})

    def test_enabled_once_token_is_set(self):
        self.assertTrue(LeadAdsConnector(token="fake-token-for-test").enabled)


class TestFilters(unittest.TestCase):
    def test_classify_signals_can_match_more_than_one_category(self):
        labels = filters.classify_signals("דרוש קבלן לעסק חדש שנפתח בעיר")
        self.assertIn("new_business_opening", labels)
        self.assertIn("commercial_opportunity", labels)

    def test_classify_signals_neutral_text_matches_nothing(self):
        self.assertEqual(filters.classify_signals("יום נעים לכולם"), [])

    def test_apply_tenant_filters_restricts_by_area_and_service(self):
        cands = [{"text": "מחפש חשמלאי בחיפה"}, {"text": "מחפש אינסטלטור בתל אביב"}]
        out = filters.apply_tenant_filters(cands, {"areas": ["חיפה"]})
        self.assertEqual(len(out), 1)
        self.assertIn("חיפה", out[0]["text"])

    def test_apply_tenant_filters_without_config_passes_everything(self):
        cands = [{"text": "מחפש חשמלאי בחיפה"}, {"text": "מחפש אינסטלטור בתל אביב"}]
        self.assertEqual(len(filters.apply_tenant_filters(cands, {})), 2)


class TestSourcesAndRun(Base):
    def test_register_and_list(self):
        sid = register_source(self.c, self.t, "paste", "ידני", {"text": "x"})
        sources = list_sources(self.c, self.t)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], sid)
        self.assertEqual(sources[0]["reliability"], 1.0)

    def test_unknown_connector_rejected(self):
        with self.assertRaises(ValueError):
            register_source(self.c, self.t, "carrier_pigeon", "x")

    def test_run_source_paste_feeds_leads_add_lead(self):
        sid = register_source(self.c, self.t, "paste", "הדבקה ידנית",
                              {"text": "מחפש חשמלאי דחוף בחיפה, 050-1234567"})
        report = run_source(self.c, self.t, sid)
        self.assertEqual(report["added"], 1)
        self.assertIsNone(report["error"])
        found = leads.list_leads(self.c, self.t)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source"], "discovery_paste")
        row = self.c.execute("SELECT reliability, last_run FROM discovery_sources WHERE id=?",
                             (sid,)).fetchone()
        self.assertEqual(row["reliability"], 1.0)
        self.assertIsNotNone(row["last_run"])

    def test_run_source_records_error_without_raising_and_drops_reliability(self):
        sid = register_source(self.c, self.t, "rss", "פיד שבור", {})  # missing required 'url'
        report = run_source(self.c, self.t, sid)
        self.assertIsNotNone(report["error"])
        self.assertEqual(report["added"], 0)
        row = self.c.execute("SELECT reliability FROM discovery_sources WHERE id=?", (sid,)).fetchone()
        self.assertLess(row["reliability"], 1.0)

    def test_run_all_runs_every_active_source_independently(self):
        register_source(self.c, self.t, "paste", "א", {"text": "מחפש גנן ברעננה"})
        register_source(self.c, self.t, "rss", "שבור", {})
        reports = run_all(self.c, self.t)
        self.assertEqual(len(reports), 2)
        self.assertTrue(any(r["added"] == 1 for r in reports))
        self.assertTrue(any(r["error"] for r in reports))

    def test_duplicate_candidate_counted_as_duplicate_not_added(self):
        sid1 = register_source(self.c, self.t, "paste", "פעם 1",
                               {"text": "מחפש חשמלאי בחולון, 0501112233"})
        sid2 = register_source(self.c, self.t, "paste", "פעם 2",
                               {"text": "מחפש חשמלאי בחולון, 0501112233"})
        run_source(self.c, self.t, sid1)
        report = run_source(self.c, self.t, sid2)
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["added"], 0)


class TestCommandsAndRoutes(Base):
    def test_command_lists_sources(self):
        from nexus_master import command
        register_source(self.c, self.t, "paste", "מ", {"text": "x"})
        out = command.run(self.c, self.t, "מקורות גילוי")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["data"]), 1)

    def test_command_runs_discovery(self):
        from nexus_master import command
        register_source(self.c, self.t, "paste", "מ", {"text": "מחפש גנן בנתניה"})
        out = command.run(self.c, self.t, "הרץ גילוי")
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"][0]["added"], 1)

    def test_routes_registered_on_app(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default")
        status, data = app.route("POST", "/discovery/sources", {"connector": "paste", "name": "ר"})
        self.assertEqual(status, 201)
        status, data = app.route("GET", "/discovery/sources", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(data), 1)


class TestScheduledDiscovery(Base):
    """Section 16, now that core/tasks.py (module A) exists -- confirms the handler is
    actually registered and that core_tasks.tick() really calls run_source/run_all, not
    just that enqueue() returns an id."""

    def test_handlers_registered_on_import(self):
        self.assertIn("discovery.run_source", core_tasks._handlers)
        self.assertIn("discovery.run_all", core_tasks._handlers)

    def test_schedule_run_all_executes_via_tick(self):
        register_source(self.c, self.t, "paste", "מ", {"text": "מחפש חשמלאי בחיפה"})
        task_id = schedule_run(self.c, self.t)
        self.assertEqual(core_tasks.queue_depth(self.c, self.t), 1)
        result = core_tasks.tick(self.c, self.t)
        self.assertEqual(result, {"done": 1, "retried": 0, "failed": 0})
        self.assertEqual(core_tasks.queue_depth(self.c, self.t), 0)
        self.assertEqual(len(leads.list_leads(self.c, self.t)), 1)

    def test_schedule_run_one_source_executes_only_that_source(self):
        sid1 = register_source(self.c, self.t, "paste", "א", {"text": "מחפש חשמלאי"})
        register_source(self.c, self.t, "paste", "ב", {"text": "מחפש גנן"})
        schedule_run(self.c, self.t, source_id=sid1)
        core_tasks.tick(self.c, self.t)
        self.assertEqual(len(leads.list_leads(self.c, self.t)), 1)

    def test_schedule_route(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default")
        status, data = app.route("POST", "/discovery/schedule", {})
        self.assertEqual(status, 201)
        self.assertIn("task_id", data)


if __name__ == "__main__":
    unittest.main()
