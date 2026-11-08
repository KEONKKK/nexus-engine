"""Tests for NIGUN-134 module I (customer/supplier matching), sections 96-106.
Sections 96/97/98/102/103/104/106 are pre-existing (brokerage.match_lead/match_score/
propose/close_deal/reject_deal, already covered by test_engine.py:TestBrokerage) -- only
the genuinely new sections (99/100/101/105) get dedicated tests here.
"""
import unittest

from nexus_master import brokerage, db, leads


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


# ---- section 99: availability --------------------------------------------------------

class TestAvailability(Base):
    def test_unavailable_supplier_is_excluded_from_matches(self):
        s = brokerage.add_supplier(self.c, self.t, "יוסי", keywords="אינסטלטור", areas="חולון")
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש אינסטלטור בחולון")
        self.assertEqual(len(brokerage.match_lead(self.c, self.t, leads.get(self.c, self.t, lid))), 1)
        brokerage.set_availability(self.c, self.t, s, False)
        self.assertEqual(brokerage.match_lead(self.c, self.t, leads.get(self.c, self.t, lid)), [])

    def test_available_defaults_to_true_for_existing_suppliers(self):
        s = brokerage.add_supplier(self.c, self.t, "יוסי", keywords="חשמלאי")
        row = self.c.execute("SELECT available FROM suppliers WHERE id=?", (s,)).fetchone()
        self.assertEqual(row["available"], 1)


# ---- section 100: price range ---------------------------------------------------------

class TestPriceRange(Base):
    def test_budget_outside_price_range_is_down_weighted_not_excluded(self):
        s = brokerage.add_supplier(self.c, self.t, "יוסי", keywords="אינסטלטור", areas="חולון")
        brokerage.set_price_range(self.c, self.t, s, price_min=1000, price_max=5000)
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש אינסטלטור בחולון", budget=100)
        matches = brokerage.match_lead(self.c, self.t, leads.get(self.c, self.t, lid))
        self.assertEqual(len(matches), 1)
        self.assertIn("מחוץ לטווח", matches[0]["why"])

    def test_no_price_range_set_has_no_effect(self):
        s = brokerage.add_supplier(self.c, self.t, "יוסי", keywords="אינסטלטור", areas="חולון")
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש אינסטלטור בחולון", budget=100)
        matches = brokerage.match_lead(self.c, self.t, leads.get(self.c, self.t, lid))
        self.assertNotIn("מחוץ לטווח", matches[0]["why"])


# ---- section 101: customer requirements ------------------------------------------------

class TestRequirementsMatch(Base):
    def test_all_requirements_met(self):
        frac, reason = brokerage.requirements_match("אינסטלטור,24 שעות", "אינסטלטור,24 שעות,נזילה")
        self.assertEqual(frac, 1.0)

    def test_partial_requirements_met(self):
        frac, reason = brokerage.requirements_match("אינסטלטור,24 שעות", "אינסטלטור")
        self.assertEqual(frac, 0.5)
        self.assertIn("1/2", reason)

    def test_no_requirements_given_is_vacuously_satisfied(self):
        frac, _ = brokerage.requirements_match("", "אינסטלטור")
        self.assertEqual(frac, 1.0)


# ---- section 105: referral/matching history --------------------------------------------

class TestMatchHistory(Base):
    def test_shown_matches_are_recorded(self):
        brokerage.add_supplier(self.c, self.t, "יוסי", keywords="אינסטלטור", areas="חולון")
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש אינסטלטור בחולון")
        brokerage.match_lead(self.c, self.t, leads.get(self.c, self.t, lid))
        hist = brokerage.match_history(self.c, self.t, lead_id=lid)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["data"]["lead_id"], lid)

    def test_history_filters_by_lead_id(self):
        brokerage.add_supplier(self.c, self.t, "יוסי", keywords="אינסטלטור", areas="חולון")
        lid1, _ = leads.add_lead(self.c, self.t, text="מחפש אינסטלטור בחולון", phone="0501230030")
        lid2, _ = leads.add_lead(self.c, self.t, text="מחפש אינסטלטור בחולון", phone="0501230031")
        brokerage.match_lead(self.c, self.t, leads.get(self.c, self.t, lid1))
        brokerage.match_lead(self.c, self.t, leads.get(self.c, self.t, lid2))
        self.assertEqual(len(brokerage.match_history(self.c, self.t, lead_id=lid1)), 1)
        self.assertEqual(len(brokerage.match_history(self.c, self.t)), 2)


if __name__ == "__main__":
    unittest.main()
