import os
import unittest

from nexus_master import brokerage, db, leads
from nexus_master.sales_ai import assist, drafts, needs, next_action, objections, scripts, summary
from nexus_master.sales_ai.provider import LLMProvider, RuleBasedProvider, default_provider


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


class TestDraftsHebrewAndEnglish(unittest.TestCase):
    def test_first_contact_hebrew_matches_existing_whatsapp_draft(self):
        lead = {"name": "דנה", "service": "חשמלאי", "area": "חיפה"}
        from nexus_master import whatsapp
        self.assertEqual(drafts.first_contact_draft(lead, business="העסק"),
                         whatsapp.draft_to_client(lead, business="העסק"))

    def test_first_contact_english_is_a_real_second_language_not_a_copy(self):
        lead = {"name": "Dana", "service": "אינסטלטור", "area": "תל אביב"}
        text = drafts.first_contact_draft(lead, business="Acme", lang="en")
        self.assertIn("Acme", text)
        self.assertIn("Dana", text)
        self.assertIn("STOP", text)

    def test_follow_up_english(self):
        text = drafts.follow_up_draft({"name": "Dana"}, lang="en")
        self.assertIn("follow-up", text)

    def test_unsupported_language_rejected(self):
        with self.assertRaises(ValueError):
            drafts.first_contact_draft({}, lang="fr")


class TestNeedsAndService(unittest.TestCase):
    def test_identify_needs_reuses_parse_text(self):
        from nexus_master import scoring
        text = "מחפש חשמלאי דחוף בחיפה"
        self.assertEqual(needs.identify_needs(text), scoring.parse_text(text))

    def test_suggest_service(self):
        self.assertEqual(needs.suggest_service("צריך אינסטלטור"), "אינסטלטור")

    def test_suggest_kit_maps_industry(self):
        out = needs.suggest_kit({"service": "יופי", "text": ""})
        self.assertEqual(out["industry"], "beauty")


class TestObjections(unittest.TestCase):
    def test_matches_price_objection(self):
        resp = objections.suggest_response("זה יקר לי מדי")
        self.assertEqual(resp["objection"], "יקר")

    def test_no_match_returns_none(self):
        self.assertIsNone(objections.suggest_response("הכל מצוין"))

    def test_multiple_matches_returned_by_suggest_responses(self):
        out = objections.suggest_responses("יקר לי ואני צריך לחשוב")
        self.assertEqual({o["objection"] for o in out}, {"יקר", "לחשוב"})


class TestScripts(Base):
    def test_default_script(self):
        self.assertEqual(scripts.get_script(self.c, self.t, "opening"), scripts.DEFAULT_SCRIPTS["opening"])

    def test_unknown_scenario_raises(self):
        with self.assertRaises(ValueError):
            scripts.get_script(self.c, self.t, "nonsense")

    def test_tenant_override_persists(self):
        scripts.set_script(self.c, self.t, "opening", "נוסח מותאם")
        self.assertEqual(scripts.get_script(self.c, self.t, "opening"), "נוסח מותאם")
        # a second tenant must not see the first tenant's override
        t2 = db.ensure_tenant(self.c, "other")
        self.assertEqual(scripts.get_script(self.c, t2, "opening"), scripts.DEFAULT_SCRIPTS["opening"])


class TestNextBestAction(Base):
    def test_no_phone_asks_for_it(self):
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש חשמלאי", consent=True)
        lead = leads.get(self.c, self.t, lid)
        self.assertEqual(next_action.next_best_action(self.c, self.t, lead)["action"], "ask_for_phone")

    def test_terminal_status_means_no_action(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", phone="0501234567", consent=True)
        leads.set_status(self.c, self.t, lid, "lost")
        lead = leads.get(self.c, self.t, lid)
        self.assertEqual(next_action.next_best_action(self.c, self.t, lead)["action"], "no_action")

    def test_hot_lead_without_supplier_match_searches_supplier(self):
        lid, _ = leads.add_lead(self.c, self.t, text="דחוף! מחפש חשמלאי בחיפה, תקציב 3000",
                               phone="0501234567", consent=True)
        lead = leads.get(self.c, self.t, lid)
        self.assertGreaterEqual(lead["score_total"], next_action.HOT_SCORE)
        self.assertEqual(next_action.next_best_action(self.c, self.t, lead)["action"], "search_supplier")

    def test_match_found_proposes_it(self):
        brokerage.add_supplier(self.c, self.t, "אור חשמל", keywords="חשמלאי", areas="חיפה")
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש חשמלאי בחיפה", phone="0501234567", consent=True)
        lead = leads.get(self.c, self.t, lid)
        result = next_action.next_best_action(self.c, self.t, lead)
        self.assertEqual(result["action"], "propose_match")
        self.assertIn("supplier_id", result)

    def test_new_lead_with_no_match_and_not_hot_sends_first_contact(self):
        lid, _ = leads.add_lead(self.c, self.t, text="סתם", phone="0501234567", consent=True)
        lead = leads.get(self.c, self.t, lid)
        self.assertEqual(next_action.next_best_action(self.c, self.t, lead)["action"], "send_first_contact")


class TestSummaryProvider(unittest.TestCase):
    def setUp(self):
        os.environ.pop(LLMProvider.ENV_VAR, None)

    def test_default_provider_is_rule_based_without_key(self):
        self.assertIsInstance(default_provider(), RuleBasedProvider)

    def test_rule_based_summary_counts_and_quotes(self):
        interactions = [{"role": "business", "text": "היי"}, {"role": "customer", "text": "כמה עולה?"}]
        s = summary.summarize_conversation(interactions)
        self.assertIn("2 הודעות", s)
        self.assertIn("1 מהלקוח", s)
        self.assertIn("1 שאלות", s)

    def test_empty_interactions(self):
        self.assertIn("אין", summary.summarize_conversation([]))

    def test_llm_provider_blocked_without_key(self):
        self.assertFalse(LLMProvider().enabled)
        with self.assertRaises(RuntimeError):
            LLMProvider().summarize([])


class TestAssistBundle(Base):
    def test_assist_bundles_action_and_draft(self):
        lid, _ = leads.add_lead(self.c, self.t, text="סתם טקסט", phone="0501234567", consent=True)
        lead = leads.get(self.c, self.t, lid)
        out = assist(self.c, self.t, lead)
        self.assertEqual(out["action"]["action"], "send_first_contact")
        self.assertIsNotNone(out["draft"])

    def test_assist_includes_summary_when_interactions_given(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", phone="0501234567", consent=True)
        lead = leads.get(self.c, self.t, lid)
        out = assist(self.c, self.t, lead, interactions=[{"role": "customer", "text": "הי"}])
        self.assertIn("summary", out)


class TestNeverSendsDirectly(unittest.TestCase):
    """Section 72, enforced structurally, not just documented."""

    def test_no_module_in_this_package_imports_a_send_function(self):
        import ast
        import pathlib
        pkg_dir = pathlib.Path(__file__).resolve().parents[1] / "nexus_master" / "sales_ai"
        forbidden = {"send_manual", "notify_business_new_lead", "CloudAPI"}
        for path in pkg_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names = {n.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                    for n in node.names}
            self.assertFalse(names & forbidden, f"{path.name} imports a send-capable name: {names & forbidden}")


class TestCommandsAndRoutes(Base):
    def test_command_assist(self):
        from nexus_master import command
        lid, _ = leads.add_lead(self.c, self.t, text="x", phone="0501234567", consent=True)
        out = command.run(self.c, self.t, f"ייעץ ליד {lid}")
        self.assertTrue(out["ok"])
        self.assertIn("action", out["data"])

    def test_routes_registered(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default")
        lid, _ = leads.add_lead(self.c, self.t, text="x", phone="0501234567", consent=True)
        status, data = app.route("POST", "/sales-ai/assist", {"lead_id": lid})
        self.assertEqual(status, 200)
        status, data = app.route("GET", "/sales-ai/script", {}, {"scenario": ["opening"]})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
