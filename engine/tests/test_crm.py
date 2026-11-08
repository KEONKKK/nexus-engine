import time
import unittest

from nexus_master import crm, db, leads


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)
        self.lid, _ = leads.add_lead(
            self.c, self.t, text="מחפש חשמלאי דחוף", phone="0501234567", service="חשמלאי",
            area="חולון", consent=True)


class TestMigrations(Base):
    def test_crm_migrations_ran_and_are_idempotent(self):
        self.assertGreater(db.migrate(self.c), 0)
        v1 = self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='crm'").fetchone()[0]
        self.assertIsNotNone(v1)
        db.migrate(self.c)  # must not re-run and blow up on CREATE TABLE
        v2 = self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='crm'").fetchone()[0]
        self.assertEqual(v1, v2)

    def test_crm_is_a_discovered_plugin(self):
        from nexus_master.core.registry import discover_plugins
        names = [p.__name__ for p in discover_plugins(refresh=True)]
        self.assertIn("nexus_master.crm", names)


class TestContacts(Base):
    def test_upsert_creates_then_updates_not_duplicates(self):
        cid1 = crm.upsert_contact(self.c, self.t, "+972501234567", "דני")
        cid2 = crm.upsert_contact(self.c, self.t, "0501234567", "דני כהן")
        self.assertEqual(cid1, cid2)
        contact = crm.get_contact(self.c, self.t, "972501234567")
        self.assertEqual(contact["name"], "דני כהן")

    def test_phone_is_normalized_like_leads_phone(self):
        # same normalize_phone() leads.add_lead already applied - a contact created from a
        # "+972..." input must still be found by the bare digits leads.phone stores.
        crm.upsert_contact(self.c, self.t, "+972501234567")
        lead = leads.get(self.c, self.t, self.lid)
        self.assertIsNotNone(crm.get_contact(self.c, self.t, lead["phone"]))

    def test_bad_phone_raises(self):
        with self.assertRaises(ValueError):
            crm.upsert_contact(self.c, self.t, "abc")


class TestInteractionsAndNotes(Base):
    def test_log_interaction_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            crm.log_interaction(self.c, self.t, self.lid, "carrier_pigeon")

    def test_interactions_recorded_in_order(self):
        crm.log_interaction(self.c, self.t, self.lid, "call", "שיחה 1")
        crm.log_interaction(self.c, self.t, self.lid, "message", "הודעה 2")
        rows = crm.list_interactions(self.c, self.t, self.lid)
        self.assertEqual([r["kind"] for r in rows], ["call", "message"])

    def test_notes_empty_text_raises(self):
        with self.assertRaises(ValueError):
            crm.add_note(self.c, self.t, self.lid, "   ")

    def test_notes_round_trip(self):
        crm.add_note(self.c, self.t, self.lid, "נראה חם")
        self.assertEqual(len(crm.list_notes(self.c, self.t, self.lid)), 1)


class TestAssignment(Base):
    def test_default_assignee_is_owner(self):
        self.assertEqual(crm.assign(self.c, self.t, self.lid), crm.DEFAULT_OWNER)

    def test_reassign_updates_current_but_keeps_history(self):
        crm.assign(self.c, self.t, self.lid, "דני")
        crm.assign(self.c, self.t, self.lid, "רותי")
        self.assertEqual(crm.current_assignee(self.c, self.t, self.lid), "רותי")
        history = crm.assignment_history(self.c, self.t, self.lid)
        self.assertEqual([h["assignee"] for h in history], ["דני", "רותי"])

    def test_assign_emits_event_for_automation_to_subscribe_to(self):
        from nexus_master.core import events
        seen = []
        events.subscribe("crm.lead_assigned", lambda c, t, typ, data: seen.append(data))
        try:
            crm.assign(self.c, self.t, self.lid, "דני")
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0]["lead_id"], self.lid)
            self.assertEqual(seen[0]["assignee"], "דני")
        finally:
            events.unsubscribe_all()


class TestReminders(Base):
    def test_due_reminders_only_returns_past_due_and_undone(self):
        past = crm.add_reminder(self.c, self.t, self.lid, time.time() - 10, "עבר")
        future = crm.add_reminder(self.c, self.t, self.lid, time.time() + 1000, "עתידי")
        due = crm.due_reminders(self.c, self.t)
        self.assertEqual([r["id"] for r in due], [past])
        crm.mark_reminder_done(self.c, self.t, past)
        self.assertEqual(crm.due_reminders(self.c, self.t), [])  # done, so excluded by default
        # include_done=True still respects the due<=now filter: the future one stays out
        still_due_ids = [r["id"] for r in crm.due_reminders(self.c, self.t, include_done=True)]
        self.assertEqual(still_due_ids, [past])
        self.assertNotIn(future, still_due_ids)


class TestPipeline(Base):
    def test_default_stage_labels_cover_every_status(self):
        stages = crm.pipeline_stages(self.c, self.t)
        self.assertEqual([s["status"] for s in stages], list(leads.STATUSES))
        for s in stages:
            self.assertTrue(s["label"])

    def test_custom_labels_override_only_what_they_name(self):
        crm.set_pipeline_stage_labels(self.c, self.t, {"new": "טרי טרי"})
        stages = {s["status"]: s["label"] for s in crm.pipeline_stages(self.c, self.t)}
        self.assertEqual(stages["new"], "טרי טרי")
        self.assertEqual(stages["contacted"], "בטיפול")  # default, untouched

    def test_pipeline_counts_match_real_statuses(self):
        counts = {c["status"]: c["count"] for c in crm.pipeline_counts(self.c, self.t)}
        self.assertEqual(counts["new"], 1)
        self.assertEqual(counts["contacted"], 0)
        leads.set_status(self.c, self.t, self.lid, "contacted")
        counts = {c["status"]: c["count"] for c in crm.pipeline_counts(self.c, self.t)}
        self.assertEqual(counts["new"], 0)
        self.assertEqual(counts["contacted"], 1)

    def test_demo_leads_excluded_by_default(self):
        leads.add_lead(self.c, self.t, text="דמו", phone="0509999999", consent=True, is_demo=True)
        counts = {c["status"]: c["count"] for c in crm.pipeline_counts(self.c, self.t)}
        self.assertEqual(counts["new"], 1)  # the real lead only, not the demo one
        counts_with_demo = {c["status"]: c["count"] for c in crm.pipeline_counts(self.c, self.t, include_demo=True)}
        self.assertEqual(counts_with_demo["new"], 2)


class TestLeadProfile(Base):
    def test_profile_combines_everything(self):
        crm.log_interaction(self.c, self.t, self.lid, "call", "שיחה")
        crm.add_note(self.c, self.t, self.lid, "הערה")
        crm.assign(self.c, self.t, self.lid, "דני")
        crm.add_reminder(self.c, self.t, self.lid, time.time() + 10, "תזכורת")
        profile = crm.lead_profile(self.c, self.t, self.lid)
        self.assertEqual(profile["lead"]["id"], self.lid)
        self.assertEqual(len(profile["interactions"]), 1)
        self.assertEqual(len(profile["notes"]), 1)
        self.assertEqual(profile["assignee"], "דני")
        self.assertEqual(len(profile["reminders"]), 1)
        self.assertEqual(profile["deals"], [])

    def test_profile_missing_lead_returns_none(self):
        self.assertIsNone(crm.lead_profile(self.c, self.t, 999999))


class TestSegments(Base):
    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            crm.save_segment(self.c, self.t, "bad", {"nope": 1})

    def test_save_and_run(self):
        leads.add_lead(self.c, self.t, text="מחפש אינסטלטור", phone="0507654321",
                       service="אינסטלטור", consent=True)
        crm.save_segment(self.c, self.t, "חשמלאים", {"service": "חשמלאי"})
        result = crm.run_segment(self.c, self.t, "חשמלאים")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], self.lid)

    def test_resave_same_name_overwrites_not_duplicates(self):
        crm.save_segment(self.c, self.t, "x", {"service": "חשמלאי"})
        crm.save_segment(self.c, self.t, "x", {"service": "אינסטלטור"})
        self.assertEqual(len(crm.list_segments(self.c, self.t)), 1)

    def test_unknown_segment_name_returns_none(self):
        self.assertIsNone(crm.run_segment(self.c, self.t, "לא קיים"))


class TestPartnersOverview(Base):
    def test_unions_buyers_suppliers_and_pending_only(self):
        from nexus_master import brokerage, buyers, partners
        buyers.add_buyer(self.c, self.t, "קונה א")
        brokerage.add_supplier(self.c, self.t, "ספק א")
        pid = partners.register(self.c, self.t, name="שותף חדש", phone="+972501112222",
                                services="חשמלאי", areas="מרכז", kind="supplier", consent=True)
        overview = crm.partners_overview(self.c, self.t)
        sources = sorted(o["source"] for o in overview)
        self.assertEqual(sources, ["buyer", "partner_pending", "supplier"])

        # once approved, the pending row must disappear from the view (it is now a real
        # supplier row instead) - never double-counted
        partners.approve(self.c, self.t, pid)
        overview2 = crm.partners_overview(self.c, self.t)
        self.assertEqual(sorted(o["source"] for o in overview2), ["buyer", "supplier", "supplier"])

    def test_kind_filter(self):
        from nexus_master import buyers
        buyers.add_buyer(self.c, self.t, "קונה א")
        self.assertEqual(len(crm.partners_overview(self.c, self.t, kind="buyer")), 1)
        self.assertEqual(len(crm.partners_overview(self.c, self.t, kind="supplier")), 0)


class TestRelationshipHistory(Base):
    def test_timeline_sorted_and_combined(self):
        crm.log_interaction(self.c, self.t, self.lid, "call", "שיחה")
        crm.add_note(self.c, self.t, self.lid, "הערה")
        lead = leads.get(self.c, self.t, self.lid)
        hist = crm.relationship_history(self.c, self.t, lead["phone"])
        kinds = [x["kind"] for x in hist["timeline"]]
        self.assertEqual(kinds, ["lead", "interaction", "note"])
        self.assertEqual(hist["timeline"][0]["ts"] <= hist["timeline"][-1]["ts"], True)

    def test_second_lead_same_phone_both_appear(self):
        lead = leads.get(self.c, self.t, self.lid)
        leads.add_lead(self.c, self.t, text="עוד פנייה", phone=lead["phone"], consent=True)
        hist = crm.relationship_history(self.c, self.t, lead["phone"])
        lead_entries = [x for x in hist["timeline"] if x["kind"] == "lead"]
        self.assertEqual(len(lead_entries), 2)

    def test_includes_contact_when_one_exists(self):
        lead = leads.get(self.c, self.t, self.lid)
        crm.upsert_contact(self.c, self.t, lead["phone"], "דני")
        hist = crm.relationship_history(self.c, self.t, lead["phone"])
        self.assertEqual(hist["contact"]["name"], "דני")


class TestAdminRoutes(Base):
    def test_routes_registered_and_reachable_through_the_real_app(self):
        from nexus_master.api import App
        app = App(self.c, api_key=None)  # no key -> localhost-only, fine for an in-process call
        status, data = app.route("GET", "/crm/pipeline", {}, {})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["counts"]), len(leads.STATUSES))

        status, data = app.route("POST", "/crm/note", {"lead_id": self.lid, "text": "מהממשק"}, {})
        self.assertEqual(status, 201)
        self.assertEqual(len(crm.list_notes(self.c, self.t, self.lid)), 1)

        status, data = app.route("GET", "/crm/lead_profile", {}, {"id": [str(self.lid)]})
        self.assertEqual(status, 200)
        self.assertEqual(data["lead"]["id"], self.lid)

        status, data = app.route("GET", "/crm/lead_profile", {}, {"id": ["999999"]})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
