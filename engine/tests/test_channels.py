import json
import unittest

from nexus_master import channels, db, leads


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


class TestMigrationsAndPlugin(Base):
    def test_migrations_ran_and_idempotent(self):
        v1 = self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='channels'").fetchone()[0]
        self.assertIsNotNone(v1)
        db.migrate(self.c)
        v2 = self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='channels'").fetchone()[0]
        self.assertEqual(v1, v2)

    def test_discovered(self):
        from nexus_master.core.registry import discover_plugins
        names = [p.__name__ for p in discover_plugins(refresh=True)]
        self.assertIn("nexus_master.channels", names)


class TestChannelsCRUD(Base):
    def test_bad_kind_rejected(self):
        with self.assertRaises(ValueError):
            channels.create_channel(self.c, self.t, "x", "carrier_pigeon")

    def test_create_and_list(self):
        channels.create_channel(self.c, self.t, "fb_biz", "facebook_lead_ads")
        rows = channels.list_channels(self.c, self.t)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "facebook_lead_ads")

    def test_duplicate_name_rejected_by_unique_constraint(self):
        channels.create_channel(self.c, self.t, "fb_biz", "facebook_lead_ads")
        with self.assertRaises(Exception):
            channels.create_channel(self.c, self.t, "fb_biz", "other")


class TestTemplates(Base):
    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            channels.render_channel_template("carrier_pigeon", name="דני")

    def test_every_declared_kind_has_a_template_and_renders(self):
        for kind in channels.CHANNEL_KINDS:
            text = channels.render_channel_template(kind, name="דני")
            self.assertIn("דני", text)

    def test_missing_name_still_renders_without_crashing(self):
        text = channels.render_channel_template("whatsapp")
        self.assertIsInstance(text, str)


class TestUTM(Base):
    def test_parse_full_query_string(self):
        utm = channels.parse_utm("https://x.test/?utm_source=fb&utm_medium=cpc&utm_campaign=summer")
        self.assertEqual(utm, {"utm_source": "fb", "utm_medium": "cpc", "utm_campaign": "summer"})

    def test_parse_partial_leaves_others_none(self):
        utm = channels.parse_utm("?utm_source=fb")
        self.assertEqual(utm["utm_source"], "fb")
        self.assertIsNone(utm["utm_medium"])
        self.assertIsNone(utm["utm_campaign"])

    def test_parse_empty_never_raises(self):
        self.assertEqual(channels.parse_utm(""), {"utm_source": None, "utm_medium": None, "utm_campaign": None})
        self.assertEqual(channels.parse_utm(None), {"utm_source": None, "utm_medium": None, "utm_campaign": None})

    def test_record_and_read_back(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", phone="0501234567", consent=True)
        channels.record_utm(self.c, self.t, lid, {"utm_source": "fb", "utm_medium": "cpc", "utm_campaign": None})
        stored = channels.utm_for_lead(self.c, self.t, lid)
        self.assertEqual(stored["utm_source"], "fb")
        self.assertEqual(stored["utm_medium"], "cpc")

    def test_no_utm_recorded_returns_none(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", phone="0501234567", consent=True)
        self.assertIsNone(channels.utm_for_lead(self.c, self.t, lid))


class TestCostAndPerformance(Base):
    def test_record_cost_upserts_per_period(self):
        cid = channels.create_channel(self.c, self.t, "fb_biz", "facebook_lead_ads")
        channels.record_cost(self.c, self.t, cid, "2026-11", 1000)
        channels.record_cost(self.c, self.t, cid, "2026-11", 1500)  # corrected figure, same period
        self.assertEqual(channels.total_cost(self.c, self.t, cid), 1500)
        channels.record_cost(self.c, self.t, cid, "2026-12", 500)
        self.assertEqual(channels.total_cost(self.c, self.t, cid), 2000)

    def test_performance_joins_leads_by_channel_name_as_source(self):
        channels.create_channel(self.c, self.t, "fb_biz", "facebook_lead_ads")
        leads.add_lead(self.c, self.t, text="x", phone="0501111111", source="fb_biz", consent=True)
        leads.add_lead(self.c, self.t, text="y", phone="0502222222", source="fb_biz", consent=True)
        perf = channels.channel_performance(self.c, self.t)
        self.assertEqual(len(perf), 1)
        self.assertEqual(perf[0]["leads"], 2)

    def test_cost_per_lead_computed_and_none_without_leads(self):
        cid = channels.create_channel(self.c, self.t, "fb_biz", "facebook_lead_ads")
        channels.record_cost(self.c, self.t, cid, "2026-11", 100)
        perf = channels.channel_performance(self.c, self.t)
        self.assertIsNone(perf[0]["cost_per_lead"])  # 0 leads yet
        leads.add_lead(self.c, self.t, text="x", phone="0501111111", source="fb_biz", consent=True)
        perf = channels.channel_performance(self.c, self.t)
        self.assertEqual(perf[0]["cost_per_lead"], 100.0)

    def test_channel_with_no_leads_still_appears_with_zeros(self):
        channels.create_channel(self.c, self.t, "unused", "other")
        perf = channels.channel_performance(self.c, self.t)
        self.assertEqual(perf[0]["leads"], 0)
        self.assertEqual(perf[0]["cost"], 0)


class TestLeadAdsWebhook(Base):
    SAMPLE = {
        "entry": [{
            "changes": [{
                "value": {
                    "leadgen_id": "123456",
                    "field_data": [
                        {"name": "full_name", "values": ["דני כהן"]},
                        {"name": "phone_number", "values": ["+972501234567"]},
                    ],
                }
            }]
        }]
    }

    def test_parse_payload_extracts_fields(self):
        parsed = channels.LeadAdsWebhook.parse_payload(self.SAMPLE)
        self.assertEqual(parsed["leadgen_id"], "123456")
        self.assertEqual(parsed["fields"]["full_name"], "דני כהן")
        self.assertEqual(parsed["fields"]["phone_number"], "+972501234567")

    def test_parse_payload_never_raises_on_garbage(self):
        self.assertEqual(channels.LeadAdsWebhook.parse_payload({}), {"leadgen_id": None, "fields": {}})
        self.assertEqual(channels.LeadAdsWebhook.parse_payload({"entry": [{}]}), {"leadgen_id": None, "fields": {}})

    def test_blocked_without_app_secret_but_still_logged(self):
        hook = channels.LeadAdsWebhook(app_secret=None)
        self.assertFalse(hook.enabled)
        result = hook.receive(self.c, self.t, self.SAMPLE)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        history = channels.webhook_history(self.c, self.t)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "blocked")
        self.assertIsNone(history[0]["lead_id"])

    def test_auto_activates_once_secret_is_configured(self):
        # "the code is written and tested with a mock, auto-activates once the key is
        # set" - the top-level NIGUN-134 rule this directly satisfies.
        hook = channels.LeadAdsWebhook(app_secret="test-secret-not-real")
        self.assertTrue(hook.enabled)
        result = hook.receive(self.c, self.t, self.SAMPLE)
        self.assertTrue(result["ok"])
        lead = leads.get(self.c, self.t, result["lead_id"])
        self.assertEqual(lead["name"], "דני כהן")
        self.assertEqual(lead["phone"], "972501234567")
        history = channels.webhook_history(self.c, self.t)
        self.assertEqual(history[0]["status"], "processed")
        self.assertEqual(history[0]["lead_id"], result["lead_id"])

    def test_env_var_also_enables_it(self):
        import os
        os.environ["META_APP_SECRET"] = "from-env"
        try:
            self.assertTrue(channels.LeadAdsWebhook().enabled)
        finally:
            del os.environ["META_APP_SECRET"]


class TestAdminRoutes(Base):
    def test_routes_registered_and_reachable(self):
        from nexus_master.api import App
        app = App(self.c, api_key=None)
        status, data = app.route("POST", "/channels", {"name": "fb_biz", "kind": "facebook_lead_ads"}, {})
        self.assertEqual(status, 201)
        status, data = app.route("GET", "/channels", {}, {})
        self.assertEqual(len(data["channels"]), 1)
        status, data = app.route("POST", "/channels/cost",
                                 {"channel_id": data["channels"][0]["id"], "period": "2026-11", "amount": 100}, {})
        self.assertTrue(data["ok"])
        status, data = app.route("GET", "/channels/performance", {}, {})
        self.assertEqual(status, 200)
        status, data = app.route("POST", "/channels/lead_ads_webhook", TestLeadAdsWebhook.SAMPLE, {})
        self.assertEqual(status, 503)  # no META_APP_SECRET in this test process
        self.assertTrue(data["blocked"])


if __name__ == "__main__":
    unittest.main()
