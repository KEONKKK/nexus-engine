import inspect
import io
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from nexus_master import (attribution, audit, brokerage, buyers, command, db, experiments, guardian,
                          kits, leads, partners, scoring, whatsapp)
from nexus_master import api as api_module
from nexus_master.api import App


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


class TestDB(Base):
    def test_migrations_idempotent(self):
        self.assertEqual(db.migrate(self.c), 8)
        self.assertEqual(
            self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='base'").fetchone()[0], 8)

    def test_migrate_is_idempotent_on_repeat_calls(self):
        # Running migrate() again on an already-migrated connection must not re-execute any
        # CREATE TABLE and blow up with "table already exists".
        db.migrate(self.c)
        db.migrate(self.c)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM schema_version WHERE pkg='base'")
                         .fetchone()[0], 8)


class TestPluginRegistry(Base):
    def test_core_package_is_a_discoverable_plugin_once_it_defines_migrations(self):
        from nexus_master.core import registry
        # core/ exists as a package (engine/nexus_master/core/) but defines none of
        # MIGRATIONS/routes/COMMANDS yet -- harmless to discover, not an error, and not
        # listed until module A actually adds one of those.
        found = registry.discover_plugins(refresh=True)
        self.assertIsInstance(found, list)

    def test_db_migrate_does_not_crash_when_no_plugin_defines_migrations(self):
        # Base case today: no subpackage under nexus_master/ defines MIGRATIONS yet.
        # db.migrate must still run the base schema cleanly.
        n = db.migrate(self.c)
        self.assertEqual(n, 8)

    def test_tenants_isolated(self):
        t2 = db.ensure_tenant(self.c, "other")
        leads.add_lead(self.c, self.t, text="מחפש חשמלאי בחיפה")
        self.assertEqual(len(leads.list_leads(self.c, t2)), 0)
        self.assertEqual(len(leads.list_leads(self.c, self.t)), 1)


class TestGuardian(Base):
    def test_normalize(self):
        self.assertEqual(guardian.normalize_phone("055-981-4818"), "972559814818")
        self.assertEqual(guardian.normalize_phone("+972 55 981 4818"), "972559814818")
        self.assertIsNone(guardian.normalize_phone("123"))

    def test_blocks(self):
        with self.assertRaises(guardian.Blocked):
            guardian.check_send(self.c, self.t, "0501234567", consent=False)
        with self.assertRaises(guardian.Blocked):
            guardian.check_send(self.c, self.t, "972500000000", consent=True)
        guardian.opt_out(self.c, self.t, "0501234567")
        with self.assertRaises(guardian.Blocked):
            guardian.check_send(self.c, self.t, "050-123-4567", consent=True)

    def test_rate_limit(self):
        for _ in range(3):
            guardian.check_send(self.c, self.t, "0501234567", consent=True)
            guardian.record_send(self.c, self.t, "0501234567")
        with self.assertRaises(guardian.Blocked):
            guardian.check_send(self.c, self.t, "0501234567", consent=True)

    def test_stop_message_opts_out(self):
        lid, _ = leads.add_lead(self.c, self.t, text="הסר", phone="0501234567")
        self.assertIsNone(lid)
        self.assertTrue(guardian.is_opted_out(self.c, self.t, "0501234567"))

    def test_business_number_default(self):
        self.assertEqual(guardian.business_number(self.c, self.t), "972559814818")


class TestScoring(Base):
    def test_parse(self):
        p = scoring.parse_text("מחפש אינסטלטור דחוף בחולון, תקציב 800 ₪, 052-333-4444")
        self.assertEqual(p["service"], "אינסטלטור")
        self.assertEqual(p["area"], "חולון")
        self.assertEqual(p["budget"], 800)
        self.assertEqual(p["urgency"], "דחוף")
        self.assertEqual(p["phone"], "972523334444")

    def test_five_scores_with_reasons(self):
        total, s, r = scoring.score({"text": "מחפש חשמלאי דחוף", "service": "חשמלאי", "area": "חיפה",
                                     "urgency": "דחוף", "phone": "0501234567", "consent": 1, "budget": 2500})
        self.assertEqual(set(s), {"intent", "urgency", "budget", "fit", "contact"})
        self.assertEqual(set(r), set(s))
        self.assertGreaterEqual(total, 80)

    def test_weak_lead_low(self):
        total, _, _ = scoring.score({"text": "שלום"})
        self.assertLess(total, 40)


class TestLeads(Base):
    def test_add_and_duplicate(self):
        a, d1 = leads.add_lead(self.c, self.t, text="צריך מזגן בנתניה", phone="0541112222")
        b, d2 = leads.add_lead(self.c, self.t, text="צריך מזגן בנתניה שוב", phone="054-111-2222")
        self.assertIsNone(d1)
        self.assertEqual(d2, a)
        self.assertEqual(leads.get(self.c, self.t, b)["status"], "duplicate")

    def test_status(self):
        a, _ = leads.add_lead(self.c, self.t, text="מחפש מנעולן")
        self.assertTrue(leads.set_status(self.c, self.t, a, "contacted"))
        with self.assertRaises(ValueError):
            leads.set_status(self.c, self.t, a, "bogus")

    def test_demo_removed(self):
        leads.add_lead(self.c, self.t, text="דמו", is_demo=True)
        leads.remove_demo(self.c, self.t)
        self.assertEqual(leads.list_leads(self.c, self.t), [])


class TestBuyers(Base):
    def test_credit_and_sell_once(self):
        b = buyers.add_buyer(self.c, self.t, "שיפוצי דני", services="שיפוצים", price_per_lead=50)
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש קבלן שיפוצים ברמת גן")
        with self.assertRaises(buyers.NotEnoughCredit):
            buyers.sell_lead(self.c, self.t, b, lid)
        buyers.top_up(self.c, self.t, b, 200)
        self.assertEqual(buyers.sell_lead(self.c, self.t, b, lid), 150)
        with self.assertRaises(ValueError):
            buyers.sell_lead(self.c, self.t, b, lid)
        self.assertEqual(buyers.revenue(self.c, self.t), {"received": 200, "delivered_value": 50})

    def test_matching_buyers(self):
        buyers.add_buyer(self.c, self.t, "A", services="חשמלאי", areas="חיפה")
        buyers.add_buyer(self.c, self.t, "B", services="שיפוצים")
        lid, _ = leads.add_lead(self.c, self.t, text="צריך חשמלאי בחיפה")
        names = [b["name"] for b in buyers.matching_buyers(self.c, self.t, leads.get(self.c, self.t, lid))]
        self.assertEqual(names, ["A"])


class TestBrokerage(Base):
    def test_match_and_close_once(self):
        s = brokerage.add_supplier(self.c, self.t, "יוסי", keywords="אינסטלטור,נזילה", areas="חולון",
                                   commission_pct=15)
        brokerage.add_supplier(self.c, self.t, "חשמל", keywords="חשמלאי")
        lid, _ = leads.add_lead(self.c, self.t, text="נזילה בבית, מחפש אינסטלטור בחולון")
        m = brokerage.match_lead(self.c, self.t, leads.get(self.c, self.t, lid))
        self.assertEqual(m[0]["supplier_id"], s)
        self.assertEqual(len(m), 1)
        d = brokerage.propose(self.c, self.t, lid, s)
        self.assertEqual(brokerage.close_deal(self.c, self.t, d, 1000), 150)
        with self.assertRaises(ValueError):
            brokerage.close_deal(self.c, self.t, d, 1000)
        self.assertEqual(brokerage.commissions_total(self.c, self.t), 150)

    def test_wrong_area_no_match(self):
        brokerage.add_supplier(self.c, self.t, "יוסי", keywords="אינסטלטור", areas="חיפה")
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש אינסטלטור באילת")
        self.assertEqual(brokerage.match_lead(self.c, self.t, leads.get(self.c, self.t, lid)), [])

    def test_rating_learns(self):
        s = brokerage.add_supplier(self.c, self.t, "X", keywords="ניקיון")
        lid, _ = leads.add_lead(self.c, self.t, text="צריך ניקיון")
        d = brokerage.propose(self.c, self.t, lid, s)
        brokerage.reject_deal(self.c, self.t, d)
        r = self.c.execute("SELECT rating FROM suppliers WHERE id=?", (s,)).fetchone()[0]
        self.assertAlmostEqual(r, 0.9)

    def test_simulation_labeled(self):
        sim = brokerage.simulate_commission(1000, 10, 20, 30)
        self.assertEqual(sim["monthly"], 600)
        self.assertIn("סימולציה", sim["note"])


class TestAttribution(Base):
    def test_parse_ref(self):
        self.assertEqual(attribution.parse_ref("https://x/page#fb_biz"), "fb_biz")
        self.assertEqual(attribution.parse_ref("מקור: wa_friends"), "wa_friends")
        self.assertEqual(attribution.parse_ref("status"), "status")

    def test_report_revenue(self):
        b = buyers.add_buyer(self.c, self.t, "B", price_per_lead=40)
        buyers.top_up(self.c, self.t, b, 100)
        lid, _ = leads.add_lead(self.c, self.t, text="צריך גנן מקור: fb_biz")
        buyers.sell_lead(self.c, self.t, b, lid)
        rep = {r["source"]: r for r in attribution.report(self.c, self.t)}
        self.assertEqual(rep["fb_biz"]["revenue"], 40)


class TestExperiments(Base):
    def test_stable_assign_and_results(self):
        experiments.create(self.c, self.t, "headline", ["A", "B"])
        v = experiments.assign(self.c, self.t, "headline", "user1")
        self.assertEqual(v, experiments.assign(self.c, self.t, "headline", "user1"))
        self.assertTrue(experiments.convert(self.c, self.t, "headline", "user1", 50))
        self.assertFalse(experiments.convert(self.c, self.t, "headline", "user1", 50))
        res = experiments.results(self.c, self.t, "headline")
        self.assertIsNone(res["decision"])


class TestKits(Base):
    def test_kit_sizes(self):
        self.assertEqual(len(kits.build("whatsapp", "סטודיו נועה", "beauty")), 10)
        self.assertEqual(len(kits.build("scripts", "פיט", "fitness")), 12)
        self.assertTrue(kits.build("cv", "x"))

    def test_deliver_once(self):
        k = kits.create_kit(self.c, self.t, "whatsapp", "שיפוצי אבי", "renovation", price=99)
        parts = kits.as_whatsapp_parts(kits.get_kit(self.c, self.t, k))
        self.assertTrue(all(len(p) <= 3500 for p in parts))
        kits.deliver(self.c, self.t, k, 99)
        with self.assertRaises(ValueError):
            kits.deliver(self.c, self.t, k, 99)
        self.assertEqual(kits.kit_revenue(self.c, self.t), 99)


class TestWhatsApp(Base):
    def test_manual_link(self):
        url = whatsapp.send_manual(self.c, self.t, "0501234567", "היי", consent=True)
        self.assertTrue(url.startswith("https://wa.me/972501234567?text="))

    def test_cloud_off(self):
        api = whatsapp.CloudAPI(token="", phone_id="")
        self.assertFalse(api.enabled)
        with self.assertRaises(RuntimeError):
            api.send(self.c, self.t, "0501234567", "x", True)

    def test_drafts_include_optout(self):
        self.assertIn("הסר", whatsapp.draft_to_client({"service": "חשמלאי"}))


class TestWhatsAppNotifyLink(Base):
    """round 4: wa.me link to the business number on every successful public lead."""

    def test_notify_link_for_business_points_at_business_number(self):
        lead = {"id": 7, "service": "חשמלאי", "area": "חיפה", "urgency": "דחוף", "score_total": 80}
        link = whatsapp.notify_link_for_business(lead, matches=[])
        self.assertTrue(link.startswith("https://wa.me/972559814818?text="))

    def test_notify_link_includes_up_to_3_match_names(self):
        lead = {"id": 8, "service": "אינסטלטור", "area": None, "urgency": None, "score_total": 50}
        matches = [{"name": f"ספק {i}"} for i in range(5)]
        link = whatsapp.notify_link_for_business(lead, matches)
        import urllib.parse
        text = urllib.parse.unquote(link.split("?text=", 1)[1])
        self.assertIn("ספק 0", text)
        self.assertIn("ספק 2", text)
        self.assertNotIn("ספק 3", text)

    def test_notify_business_new_lead_returns_link_even_when_cloud_api_off(self):
        lead = leads.get(self.c, self.t, leads.add_lead(self.c, self.t, text="מחפש גנן", phone="0501230010")[0])
        link = whatsapp.notify_business_new_lead(self.c, self.t, lead, matches=[])
        self.assertTrue(link.startswith("https://wa.me/972559814818"))

    def test_notify_link_for_partner_mentions_approve_command(self):
        partner = {"id": 3, "name": "דני", "kind": "supplier", "services": "חשמלאי", "areas": "חולון"}
        link = whatsapp.notify_link_for_partner(partner)
        import urllib.parse
        text = urllib.parse.unquote(link.split("?text=", 1)[1])
        self.assertIn("אשר שותף 3", text)


class TestPartners(Base):
    """round 4: /public/partners pending queue + 'אשר שותף <id>' activation."""

    def test_register_creates_pending_row(self):
        pid = partners.register(self.c, self.t, name="דני אינסטלציה", phone="972501230011",
                                services="אינסטלטור", areas="חולון", kind="supplier", consent=True)
        p = partners.get(self.c, self.t, pid)
        self.assertEqual(p["status"], "pending")
        self.assertIn(p, partners.list_pending(self.c, self.t))

    def test_approve_supplier_creates_real_supplier_row(self):
        pid = partners.register(self.c, self.t, name="אור חשמל", phone="972501230012",
                                services="חשמלאי", areas="הרצליה", kind="supplier", consent=True)
        new_id = partners.approve(self.c, self.t, pid)
        self.assertIsNotNone(new_id)
        row = self.c.execute("SELECT name FROM suppliers WHERE id=?", (new_id,)).fetchone()
        self.assertEqual(row["name"], "אור חשמל")
        self.assertEqual(partners.get(self.c, self.t, pid)["status"], "approved")

    def test_approve_buyer_creates_real_buyer_row(self):
        pid = partners.register(self.c, self.t, name="קונה גדול", phone="972501230013",
                                services="pro", areas="", kind="buyer", consent=True)
        new_id = partners.approve(self.c, self.t, pid)
        row = self.c.execute("SELECT name FROM buyers WHERE id=?", (new_id,)).fetchone()
        self.assertEqual(row["name"], "קונה גדול")

    def test_approve_twice_is_a_noop_second_time(self):
        pid = partners.register(self.c, self.t, name="x", phone="972501230014", kind="supplier", consent=True)
        self.assertIsNotNone(partners.approve(self.c, self.t, pid))
        self.assertIsNone(partners.approve(self.c, self.t, pid))

    def test_approve_unknown_partner_returns_none(self):
        self.assertIsNone(partners.approve(self.c, self.t, 999999))


class TestAudit(Base):
    def test_chain_and_tamper(self):
        leads.add_lead(self.c, self.t, text="מחפש חשמלאי")
        leads.add_lead(self.c, self.t, text="מחפש גנן")
        self.assertEqual(audit.verify(self.c, self.t), (True, None))
        self.c.execute("UPDATE audit SET data='{\"x\":1}' WHERE id=1")
        ok, bad = audit.verify(self.c, self.t)
        self.assertFalse(ok)
        self.assertEqual(bad, 1)


class TestCommand(Base):
    def test_commands(self):
        r = command.run(self.c, self.t, "הוסף ליד מחפש חשמלאי דחוף בחיפה 0501234567")
        self.assertTrue(r["ok"])
        self.assertIn("leads_new", command.run(self.c, self.t, "לוח")["data"])
        self.assertFalse(command.run(self.c, self.t, "בלה בלה")["ok"])
        self.assertTrue(command.run(self.c, self.t, "התאם ליד 1")["ok"])

    def test_approve_partner_command(self):
        pid = partners.register(self.c, self.t, name="דני", phone="972501230015", kind="supplier", consent=True)
        r = command.run(self.c, self.t, f"אשר שותף {pid}")
        self.assertTrue(r["ok"])
        self.assertEqual(command.run(self.c, self.t, f"אשר שותף {pid}")["ok"], False)  # already approved

    def test_approve_partner_command_unknown_id(self):
        self.assertFalse(command.run(self.c, self.t, "אשר שותף 999999")["ok"])

    def test_topup_command(self):
        bid = buyers.add_buyer(self.c, self.t, "קונה")
        r = command.run(self.c, self.t, f"טען קונה {bid} 200")
        self.assertTrue(r["ok"])
        self.assertEqual(buyers.balance(self.c, self.t, bid), 200)

    def test_notify_clicked_command(self):
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש גנן", phone="0501230016")
        leads.set_notify_link(self.c, self.t, lid, "https://wa.me/972559814818?text=x")
        r = command.run(self.c, self.t, f"סמן התראה {lid} נלחצה")
        self.assertTrue(r["ok"])
        self.assertEqual(leads.get(self.c, self.t, lid)["notify_clicked"], 1)

    def test_dashboard_lists_pending_notifications_and_partners(self):
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש גנן", phone="0501230017")
        leads.set_notify_link(self.c, self.t, lid, "https://wa.me/972559814818?text=x")
        partners.register(self.c, self.t, name="דני", phone="972501230018", kind="supplier", consent=True)
        data = command.run(self.c, self.t, "לוח")["data"]
        self.assertIn(lid, [n["lead_id"] for n in data["pending_notifications"]])
        self.assertEqual(len(data["pending_partners"]), 1)


class TestAPI(Base):
    def call(self, app, method, path, body=None, key=None, remote="127.0.0.1"):
        raw = json.dumps(body or {}).encode()
        path_info, _, query_string = path.partition("?")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path_info, "QUERY_STRING": query_string,
               "REMOTE_ADDR": remote, "CONTENT_LENGTH": str(len(raw)) if body else "0",
               "wsgi.input": io.BytesIO(raw)}
        if key:
            env["HTTP_X_API_KEY"] = key
        st = {}
        out = app(env, lambda s, h: st.setdefault("s", s))
        return int(st["s"].split()[0]), json.loads(b"".join(out))

    def test_flow(self):
        app = App(self.c, api_key="")
        s, _ = self.call(app, "POST", "/suppliers", {"name": "יוסי", "keywords": "אינסטלטור"})
        self.assertEqual(s, 201)
        s, d = self.call(app, "POST", "/leads", {"text": "מחפש אינסטלטור בחולון", "phone": "0521234567",
                                                 "consent": True, "source": "#fb_biz"})
        self.assertEqual(s, 201)
        self.assertEqual(d["lead"]["source"], "fb_biz")
        self.assertEqual(len(d["matches"]), 1)
        s, d = self.call(app, "POST", "/wa/link", {"phone": "0521234567", "text": "x", "consent": False})
        self.assertEqual(s, 422)
        s, d = self.call(app, "GET", "/health?deep=1")
        self.assertTrue(d["audit_intact"])

    def test_auth(self):
        app = App(self.c, api_key="secret")
        self.assertEqual(self.call(app, "GET", "/leads")[0], 401)
        self.assertEqual(self.call(app, "GET", "/leads", key="secret")[0], 200)
        open_app = App(self.c, api_key="")
        self.assertEqual(self.call(open_app, "GET", "/leads", remote="8.8.8.8")[0], 403)


# ---- B2-ENGINE fixes: one failing test per bug, written before its fix ----

class TestExperimentsRecreatePreservesId(Base):
    def test_create_again_keeps_id_and_old_exposures_reachable(self):
        eid1 = experiments.create(self.c, self.t, "headline", ["A", "B"])
        v = experiments.assign(self.c, self.t, "headline", "user1")
        eid2 = experiments.create(self.c, self.t, "headline", ["A", "B", "C"])
        self.assertEqual(eid1, eid2)
        row = self.c.execute("SELECT variant FROM exposures WHERE experiment_id=? AND subject=?",
                             (eid2, "user1")).fetchone()
        self.assertIsNotNone(row, "exposure orphaned: experiment got a new id")
        self.assertEqual(row["variant"], v)


class TestLeadsDuplicateStatus(Base):
    def test_duplicate_gets_own_status_and_counted_on_dashboard(self):
        a, d1 = leads.add_lead(self.c, self.t, text="צריך מזגן בנתניה", phone="0541112222")
        b, d2 = leads.add_lead(self.c, self.t, text="צריך מזגן בנתניה שוב", phone="054-111-2222")
        self.assertIsNone(d1)
        self.assertEqual(d2, a)
        self.assertEqual(leads.get(self.c, self.t, b)["status"], "duplicate")
        dash = command.dashboard(self.c, self.t)
        self.assertEqual(dash["duplicates"], 1)
        self.assertEqual(dash["leads_total"], 1)


class TestAttributionExcludesDemo(Base):
    def test_demo_sale_does_not_leak_into_real_source_revenue(self):
        b = buyers.add_buyer(self.c, self.t, "B", price_per_lead=40)
        buyers.top_up(self.c, self.t, b, 1000)
        demo_lid, _ = leads.add_lead(self.c, self.t, text="דמו גנן מקור: fb_biz", is_demo=True)
        buyers.sell_lead(self.c, self.t, b, demo_lid)
        leads.add_lead(self.c, self.t, text="צריך גנן מקור: fb_biz")
        rep = {r["source"]: r for r in attribution.report(self.c, self.t)}
        self.assertEqual(rep["fb_biz"]["revenue"], 0)


class TestRemoveDemoCascades(Base):
    def test_remove_demo_cleans_up_deals_and_ledger(self):
        s = brokerage.add_supplier(self.c, self.t, "ספק דמו", keywords="ניקיון", is_demo=True)
        lid, _ = leads.add_lead(self.c, self.t, text="דמו ניקיון", is_demo=True)
        d = brokerage.propose(self.c, self.t, lid, s)
        b = buyers.add_buyer(self.c, self.t, "קונה", price_per_lead=10)
        buyers.top_up(self.c, self.t, b, 100)
        buyers.sell_lead(self.c, self.t, b, lid)
        leads.remove_demo(self.c, self.t)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM deals WHERE id=?", (d,)).fetchone()[0], 0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM ledger WHERE lead_id=?", (lid,)).fetchone()[0], 0)


class TestAPIHardening(Base):
    def test_api_key_compared_with_hmac_not_eq(self):
        src = inspect.getsource(api_module)
        self.assertIn("hmac.compare_digest", src)

    def test_body_over_1mb_rejected_with_413(self):
        app = App(self.c, api_key="")
        big = b"x" * 1_000_001
        env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/leads", "REMOTE_ADDR": "127.0.0.1",
               "CONTENT_LENGTH": str(len(big)), "wsgi.input": io.BytesIO(big)}
        st = {}
        app(env, lambda s, h: st.setdefault("s", s))
        self.assertEqual(int(st["s"].split()[0]), 413)

    def test_no_wildcard_cors_header(self):
        app = App(self.c, api_key="")
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/health", "REMOTE_ADDR": "127.0.0.1",
               "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO(b"")}
        st = {}
        app(env, lambda s, h: st.setdefault("h", h))
        self.assertNotIn("Access-Control-Allow-Origin", [n for n, _ in st["h"]])


class TestAPIHealthDepth(Base):
    def call(self, app, method, path, body=None, remote="127.0.0.1"):
        raw = json.dumps(body or {}).encode()
        path_info, _, query_string = path.partition("?")
        env = {"REQUEST_METHOD": method, "PATH_INFO": path_info, "QUERY_STRING": query_string,
               "REMOTE_ADDR": remote, "CONTENT_LENGTH": str(len(raw)) if body else "0",
               "wsgi.input": io.BytesIO(raw)}
        st = {}
        out = app(env, lambda s, h: st.setdefault("s", s))
        return int(st["s"].split()[0]), json.loads(b"".join(out))

    def test_default_health_is_shallow_count_only(self):
        leads.add_lead(self.c, self.t, text="מחפש חשמלאי")
        app = App(self.c, api_key="")
        s, d = self.call(app, "GET", "/health")
        self.assertEqual(s, 200)
        self.assertIn("audit_count", d)
        self.assertNotIn("audit_intact", d)

    def test_health_deep_1_runs_full_verify(self):
        app = App(self.c, api_key="")
        s, d = self.call(app, "GET", "/health?deep=1")
        self.assertEqual(s, 200)
        self.assertIn("audit_intact", d)

    def test_health_200_from_remote_without_api_key(self):
        # C's finding: a hosting platform's health probe arrives from a non-localhost
        # address with no X-API-Key. Without a key configured, /health must still
        # answer 200 -- a liveness check is not a sensitive route.
        app = App(self.c, api_key="")
        s, d = self.call(app, "GET", "/health", remote="169.254.1.2")
        self.assertEqual(s, 200)
        self.assertIn("audit_count", d)

    def test_health_200_from_remote_with_wrong_api_key(self):
        app = App(self.c, api_key="secret")
        s, d = self.call(app, "GET", "/health", remote="169.254.1.2")
        self.assertEqual(s, 200)


class TestCloudWriteApproval(Base):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.mkdtemp()
        self._orig_dir = whatsapp.APPROVALS_DIR
        whatsapp.APPROVALS_DIR = Path(self._tmp)

    def tearDown(self):
        whatsapp.APPROVALS_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_approval(self, type_="cloud-write", ttl_ms=15 * 60 * 1000):
        now = time.time() * 1000
        p = Path(self._tmp) / f"{type_}-{int(now)}.json"
        p.write_text(json.dumps({"type": type_, "createdAt": now, "expiresAt": now + ttl_ms, "nonce": "x"}),
                    encoding="utf-8")
        return p

    def test_blocked_without_any_approval(self):
        api = whatsapp.CloudAPI(token="t", phone_id="p")
        with self.assertRaises(guardian.Blocked):
            api.send(self.c, self.t, "0501234567", "hi", True)

    def test_blocked_with_expired_approval(self):
        self._write_approval(ttl_ms=-1000)
        api = whatsapp.CloudAPI(token="t", phone_id="p")
        with self.assertRaises(guardian.Blocked):
            api.send(self.c, self.t, "0501234567", "hi", True)

    def test_proceeds_and_consumes_approval_once(self):
        approval_path = self._write_approval()
        api = whatsapp.CloudAPI(token="t", phone_id="p")

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=15):
            return FakeResp(json.dumps({"messages": [{"id": "wamid.1"}]}).encode())

        orig = whatsapp.urllib.request.urlopen
        whatsapp.urllib.request.urlopen = fake_urlopen
        try:
            res = api.send(self.c, self.t, "0501234567", "hi", True)
        finally:
            whatsapp.urllib.request.urlopen = orig
        self.assertIn("messages", res)
        self.assertFalse(approval_path.exists(), "approval must be one-shot (consumed on use)")


class TestEngineReviewFixes(Base):
    """Round 3: narrow, verified fixes from design/reviews/2026-11-08-engine-and-lib.md,
    sections 3-4. Each test targets exactly one named finding; nothing broader is touched."""

    def test_double_plus_phone_rejected(self):
        self.assertIsNone(guardian.normalize_phone("++972559814818"))

    def test_plus_not_at_start_rejected(self):
        self.assertIsNone(guardian.normalize_phone("972+559814818"))

    def test_single_leading_plus_still_accepted(self):
        self.assertEqual(guardian.normalize_phone("+972-55-981-4818"), "972559814818")

    def test_budget_hebrew_gershayim(self):
        # No "תקציב"/"עד" here on purpose: must be caught by the currency-suffix
        # regex alone, not the separate תקציב/עד fallback.
        p = scoring.parse_text("מחפש הובלה, יש לי 2500 ש״ח.")
        self.assertEqual(p["budget"], 2500)

    def test_budget_fallback_word_boundary(self):
        p = scoring.parse_text("קבעתי מועד 15 לפגישה, בלי תקציב מוגדר")
        self.assertIsNone(p["budget"])

    def test_budget_fallback_still_matches_real_word(self):
        p = scoring.parse_text("תקציב עד 300 ₪")
        self.assertEqual(p["budget"], 300)

    def test_area_with_hyphen_matches_space_form(self):
        self.assertEqual(scoring.detect_area("מחפש חשמלאי בתל-אביב"), "תל אביב")

    def test_urgency_negation_lo(self):
        self.assertEqual(scoring.detect_urgency("מזגן לא דחוף, אפשר בשבוע הבא"), "רגיל")

    def test_urgency_negation_lo_mamash(self):
        self.assertEqual(scoring.detect_urgency("צריך חשמלאי, לא ממש דחוף"), "רגיל")

    def test_urgency_still_detects_real_urgency(self):
        self.assertEqual(scoring.detect_urgency("מחפש אינסטלטור דחוף"), "דחוף")

    def test_soon_recognizes_its_own_label_word(self):
        self.assertEqual(scoring.detect_urgency("צריך גנן בקרוב"), "בקרוב")


ADVERSARIAL_PATH = Path(__file__).resolve().parents[2] / "design" / "corpus" / "leads-he-adversarial.json"


def _adversarial_text(item_id):
    with open(ADVERSARIAL_PATH, encoding="utf-8") as f:
        items = json.load(f)
    return next(i["text"] for i in items if i["id"] == item_id)


class TestGeneralAreaPrefixes(Base):
    """Pre-close-out addition: design/corpus/leads-he-adversarial.json adv-05/adv-06 --
    a general region ("הדרום", "הצפון", "המרכז", "השרון", "השפלה") written with a
    ב/ל prefix drops the Hebrew definite article ה ("ב"+"הדרום" -> "בדרום", not
    "בהדרום"), so the old plain substring check against AREAS never found it."""

    def test_adv05_bet_prefix_tsafon_wins_over_darom(self):
        # Both "בדרום" and "בצפון" are present; AREAS tuple order (same existing
        # index-priority rule as adv-03/04) makes "הצפון" win.
        text = _adversarial_text("adv-05")
        self.assertEqual(scoring.detect_area(text), "הצפון")

    def test_adv06_bet_prefix_sharon_wins_over_merkaz(self):
        text = _adversarial_text("adv-06")
        self.assertEqual(scoring.detect_area(text), "השרון")

    def test_lamed_prefix_dropped_he(self):
        self.assertEqual(scoring.detect_area("מתכננים לנסוע לדרום בסופש"), "הדרום")

    def test_mem_prefix_he_kept_already_worked(self):
        # "מ"+"ה"+"דרום" keeps the ה ("מהדרום"), so plain substring already
        # matched before this fix -- guarded here against regression.
        self.assertEqual(scoring.detect_area("חוזרים מהדרום בערב"), "הדרום")


class TestMainPortRequired(unittest.TestCase):
    def test_serve_without_port_exits_nonzero(self):
        from nexus_master.__main__ import main
        with self.assertRaises(SystemExit) as cm:
            main(["serve"])
        self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
