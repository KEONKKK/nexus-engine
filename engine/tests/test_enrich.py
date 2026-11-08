import os
import unittest

from nexus_master import db, guardian, leads
from nexus_master.enrich import dedup, history, normalize, set_email, backfill_category_and_area, fields_report
from nexus_master.enrich.enrichment import ExternalProvider, NullProvider, default_provider, enrich_lead


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


class TestMigration(Base):
    def test_email_column_exists(self):
        self.c.execute("SELECT email FROM leads LIMIT 1")  # raises if the column is missing

    def test_history_and_merge_tables_exist(self):
        self.c.execute("SELECT * FROM lead_history").fetchall()
        self.c.execute("SELECT * FROM lead_merges").fetchall()

    def test_tracked_under_its_own_pkg_name(self):
        row = self.c.execute("SELECT MAX(v) FROM schema_version WHERE pkg='enrich'").fetchone()
        self.assertEqual(row[0], 1)


class TestNormalize(unittest.TestCase):
    def test_normalize_name_collapses_whitespace(self):
        self.assertEqual(normalize.normalize_name("  דנה   כהן "), "דנה כהן")

    def test_normalize_name_title_cases_latin_not_hebrew(self):
        self.assertEqual(normalize.normalize_name("john smith"), "John Smith")

    def test_normalize_email_lowercases(self):
        self.assertEqual(normalize.normalize_email(" Dana@Example.COM "), "dana@example.com")

    def test_validate_email(self):
        self.assertTrue(normalize.validate_email("a@b.co"))
        self.assertFalse(normalize.validate_email("not-an-email"))
        self.assertFalse(normalize.validate_email(""))


class TestPhoneValidationDoesNotClaimOwnership(unittest.TestCase):
    """Section 29: format validation only, documented and tested here even though the
    function itself lives in guardian.py (owned elsewhere) -- it must never be mistaken
    for an ownership/deliverability check."""

    def test_well_formed_but_never_dialled_number_still_passes_format_check(self):
        # 050-0000000 is syntactically a valid Israeli mobile number; nothing about
        # guardian.normalize_phone contacts the network or confirms anyone owns it.
        self.assertEqual(guardian.normalize_phone("0500000000"), "972500000000")

    def test_known_placeholder_number_is_a_separate_later_gate_not_part_of_normalize(self):
        # guardian.PLACEHOLDER_NUMBERS is checked by check_send(), a different function --
        # normalize_phone's job is format only, and it does not reject this number itself.
        p = guardian.normalize_phone("0500000000")
        self.assertIn(p, guardian.PLACEHOLDER_NUMBERS)


class TestSetEmail(Base):
    def test_sets_valid_email_and_records_history(self):
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש חשמלאי", consent=True)
        set_email(self.c, self.t, lid, "Dana@Example.com")
        row = self.c.execute("SELECT email FROM leads WHERE id=?", (lid,)).fetchone()
        self.assertEqual(row["email"], "dana@example.com")
        hist = history.get_history(self.c, self.t, lid)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["field"], "email")
        self.assertEqual(hist[0]["new_value"], "dana@example.com")

    def test_rejects_malformed_email(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        with self.assertRaises(ValueError):
            set_email(self.c, self.t, lid, "not-an-email")

    def test_missing_lead_raises(self):
        with self.assertRaises(ValueError):
            set_email(self.c, self.t, 9999, "a@b.com")


class TestBackfill(Base):
    def test_fills_missing_service_and_area_from_text(self):
        lid, _ = leads.add_lead(self.c, self.t, text="סתם טקסט בלי מילות מפתח", consent=True)
        self.c.execute("UPDATE leads SET text=? WHERE id=?",
                       ("מחפש חשמלאי בחיפה", lid))
        self.c.commit()
        changed = backfill_category_and_area(self.c, self.t, lid)
        self.assertEqual(changed, {"service": "חשמלאי", "area": "חיפה"})

    def test_never_overwrites_an_existing_value(self):
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש חשמלאי בחיפה", service="אינסטלטור", consent=True)
        changed = backfill_category_and_area(self.c, self.t, lid)
        self.assertNotIn("service", changed)


class TestFieldsReport(Base):
    def test_flags_missing_required_fields(self):
        leads.add_lead(self.c, self.t, text="בלי כלום", consent=True)
        report = fields_report(self.c, self.t)
        self.assertEqual(len(report), 1)
        self.assertIn("phone", report[0]["missing"])

    def test_complete_lead_not_flagged(self):
        leads.add_lead(self.c, self.t, text="מחפש חשמלאי בחיפה, 0501234567", consent=True)
        self.assertEqual(fields_report(self.c, self.t), [])


class TestDedupAndMerge(Base):
    def test_finds_duplicates_by_phone(self):
        lid1, _ = leads.add_lead(self.c, self.t, text="מחפש חשמלאי", phone="0501234567", consent=True)
        self.c.execute("UPDATE leads SET status='new' WHERE id=?", (lid1,))
        lid2, dup = leads.add_lead(self.c, self.t, text="מחפש גנן", phone="0501234567",
                                    consent=True)  # different service -> not flagged by leads.find_duplicate
        self.assertIsNone(dup)
        matches = dedup.find_cross_source_duplicates(self.c, self.t, lid1)
        self.assertEqual([m["lead_id"] for m in matches], [lid2])
        self.assertIn("same_phone", matches[0]["reasons"])

    def test_finds_duplicates_by_name_and_area(self):
        lid1, _ = leads.add_lead(self.c, self.t, text="x", name="דנה כהן", area="חיפה", consent=True)
        lid2, _ = leads.add_lead(self.c, self.t, text="y", name="דנה כהן", area="חיפה", consent=True)
        matches = dedup.find_cross_source_duplicates(self.c, self.t, lid1)
        self.assertEqual([m["lead_id"] for m in matches], [lid2])

    def test_merge_fills_gaps_marks_duplicate_and_records_provenance(self):
        primary, _ = leads.add_lead(self.c, self.t, text="מחפש חשמלאי", consent=True)
        other, _ = leads.add_lead(self.c, self.t, text="y", phone="0501234567", area="חיפה", consent=True)
        result = dedup.merge_leads(self.c, self.t, primary, other)
        self.assertEqual(result["filled_from_duplicate"], {"phone": "972501234567", "area": "חיפה"})
        row = self.c.execute("SELECT * FROM leads WHERE id=?", (primary,)).fetchone()
        self.assertEqual(row["phone"], "972501234567")
        dup_row = self.c.execute("SELECT status FROM leads WHERE id=?", (other,)).fetchone()
        self.assertEqual(dup_row["status"], "duplicate")
        merges = self.c.execute("SELECT * FROM lead_merges WHERE primary_lead_id=?", (primary,)).fetchall()
        self.assertEqual(len(merges), 1)

    def test_merge_never_overwrites_a_field_primary_already_has(self):
        primary, _ = leads.add_lead(self.c, self.t, text="x", area="תל אביב", consent=True)
        other, _ = leads.add_lead(self.c, self.t, text="y", area="חיפה", consent=True)
        result = dedup.merge_leads(self.c, self.t, primary, other)
        self.assertNotIn("area", result["filled_from_duplicate"])

    def test_merge_with_self_rejected(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        with self.assertRaises(ValueError):
            dedup.merge_leads(self.c, self.t, lid, lid)


class TestEnrichmentProviderBlocked(unittest.TestCase):
    def setUp(self):
        os.environ.pop(ExternalProvider.URL_VAR, None)
        os.environ.pop(ExternalProvider.KEY_VAR, None)

    def test_default_provider_is_null_without_config(self):
        self.assertIsInstance(default_provider(), NullProvider)

    def test_null_provider_always_returns_none(self):
        self.assertIsNone(NullProvider().lookup(phone="972500000000"))

    def test_external_provider_disabled_without_env(self):
        self.assertFalse(ExternalProvider().enabled)

    def test_external_provider_enabled_once_configured(self):
        p = ExternalProvider(url="https://example.invalid/lookup", key="fake-key-for-test")
        self.assertTrue(p.enabled)


class TestEnrichLead(Base):
    def test_blocked_without_provider_is_non_fatal(self):
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        result = enrich_lead(self.c, self.t, lid, provider=ExternalProvider())  # disabled, no env set
        self.assertEqual(result, {"ok": False, "reason": "blocked_no_provider_configured"})

    def test_missing_lead_raises_once_enabled(self):
        class _FakeEnabled:
            enabled = True

            def lookup(self, phone=None, email=None):
                return {}
        with self.assertRaises(ValueError):
            enrich_lead(self.c, self.t, 9999, provider=_FakeEnabled())


class TestCommandsAndRoutes(Base):
    def test_fields_report_command(self):
        from nexus_master import command
        leads.add_lead(self.c, self.t, text="בלי כלום", consent=True)
        out = command.run(self.c, self.t, "דוח שדות חסרים")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["data"]), 1)

    def test_routes_registered(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default")
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        status, data = app.route("POST", "/enrich/email", {"lead_id": lid, "email": "a@b.com"})
        self.assertEqual(status, 200)
        self.assertEqual(data["email"], "a@b.com")


if __name__ == "__main__":
    unittest.main()
