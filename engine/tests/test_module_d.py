"""Tests for NIGUN-134 module D (AI scoring & prediction), sections 37-48.
Sections 37/38/40/45 are the pre-existing scoring.score() (intent/urgency/reasons) --
covered already by test_engine.py:TestScoring and exercised again here only where a new
section builds directly on it (46/47).
"""
import unittest

from nexus_master import attribution, brokerage, db, leads, scoring
from nexus_master.core import config


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


# ---- section 39: commercial value ----------------------------------------------------

class TestCommercialValue(Base):
    def test_real_budget_wins(self):
        v, reason = scoring.commercial_value({"budget": 3000, "service": "חשמלאי"})
        self.assertEqual(v, 3000)
        self.assertIn("בפועל", reason)

    def test_falls_back_to_service_average_without_budget(self):
        v, reason = scoring.commercial_value({"service": "שיפוצים"})
        self.assertEqual(v, scoring.SERVICE_AVG_VALUE["שיפוצים"])
        self.assertIn("הערכה", reason)

    def test_no_budget_and_unknown_service_returns_none(self):
        v, _ = scoring.commercial_value({})
        self.assertIsNone(v)


# ---- section 41: geographic relevance ------------------------------------------------

class TestGeographicRelevance(Base):
    def test_area_inside_coverage_scores_high(self):
        brokerage.add_supplier(self.c, self.t, "ספק", keywords="חשמלאי", areas="חולון,בת ים")
        covered = brokerage.covered_areas(self.c, self.t)
        score, _ = scoring.geographic_relevance_score("חולון", covered)
        self.assertEqual(score, 100)

    def test_area_outside_coverage_scores_low(self):
        brokerage.add_supplier(self.c, self.t, "ספק", keywords="חשמלאי", areas="חולון")
        covered = brokerage.covered_areas(self.c, self.t)
        score, _ = scoring.geographic_relevance_score("אילת", covered)
        self.assertEqual(score, 20)

    def test_no_area_detected_scores_zero(self):
        score, _ = scoring.geographic_relevance_score(None, {"חולון"})
        self.assertEqual(score, 0)

    def test_no_coverage_data_yet_is_neutral_not_zero(self):
        score, _ = scoring.geographic_relevance_score("חולון", set())
        self.assertEqual(score, 50)


# ---- section 42: source quality -------------------------------------------------------

class TestSourceQuality(Base):
    def test_unknown_source_is_neutral(self):
        score, _ = scoring.source_quality_score("new_campaign", [])
        self.assertEqual(score, 50)

    def test_known_source_uses_real_conversion_rate(self):
        for i in range(6):
            leads.add_lead(self.c, self.t, text="מחפש גנן", phone=f"05012340{i:02d}", source="fb_biz")
        leads.set_status(self.c, self.t, 1, "won")
        rows = attribution.report(self.c, self.t)
        score, reason = scoring.source_quality_score("fb_biz", rows)
        self.assertGreater(score, 0)
        self.assertIn("%", reason)


# ---- section 43: completeness --------------------------------------------------------

class TestCompleteness(Base):
    def test_full_lead_is_100(self):
        pct, reason = scoring.completeness_score(
            {"name": "x", "phone": "972500000001", "service": "a", "area": "b", "budget": 1, "urgency": "c"})
        self.assertEqual(pct, 100)
        self.assertEqual(reason, "כל השדות קיימים")

    def test_missing_fields_are_named(self):
        pct, reason = scoring.completeness_score({"phone": "972500000001"})
        self.assertLess(pct, 100)
        self.assertIn("name", reason)


# ---- section 44: conversion probability, >=30-sample rule ---------------------------

class TestConversionProbability(Base):
    def test_fewer_than_30_closed_leads_returns_none_with_explanation(self):
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש גנן", phone="0501111111")
        leads.set_status(self.c, self.t, lid, "won")
        p, reason = scoring.conversion_probability(self.c, self.t, {"service": None})
        self.assertIsNone(p)
        self.assertIn("30", reason)

    def test_30_plus_sample_returns_a_real_percentage(self):
        for i in range(20):
            lid, _ = leads.add_lead(self.c, self.t, text="מחפש גנן", phone=f"050222{i:04d}")
            leads.set_status(self.c, self.t, lid, "won")
        for i in range(15):
            lid, _ = leads.add_lead(self.c, self.t, text="מחפש גנן", phone=f"050333{i:04d}")
            leads.set_status(self.c, self.t, lid, "lost")
        p, reason = scoring.conversion_probability(self.c, self.t, {"service": None})
        self.assertIsNotNone(p)
        self.assertAlmostEqual(p, 100 * 20 / 35, places=1)


# ---- section 46: configurable weights -------------------------------------------------

class TestConfigurableWeights(Base):
    def test_default_weights_match_hardcoded_behavior(self):
        lead = {"text": "מחפש חשמלאי דחוף", "service": "חשמלאי", "urgency": "דחוף",
                "phone": "0501234567", "consent": 1, "budget": 2500}
        total_plain, _, _ = scoring.score(lead)
        total_with_conn, _, _ = scoring.score(lead, self.c, self.t)
        self.assertEqual(total_plain, total_with_conn)

    def test_changing_a_weight_changes_the_total(self):
        lead = {"text": "מחפש חשמלאי", "service": "חשמלאי", "urgency": "רגיל",
                "phone": "0501234567", "consent": 1, "budget": None}
        before, _, _ = scoring.score(lead, self.c, self.t)
        config.set(self.c, self.t, "score_weight_intent", 0.9)
        after, _, _ = scoring.score(lead, self.c, self.t)
        self.assertNotEqual(before, after)


# ---- section 47: recalc on change -----------------------------------------------------

class TestRecalcOnChange(Base):
    def test_update_fields_changes_score(self):
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש איש מקצוע", phone="0501230020")
        before = leads.get(self.c, self.t, lid)["score_total"]
        leads.update_fields(self.c, self.t, lid, urgency="דחוף", budget=3000)
        after = leads.get(self.c, self.t, lid)["score_total"]
        self.assertGreater(after, before)

    def test_recalc_score_on_missing_lead_returns_none(self):
        self.assertIsNone(leads.recalc_score(self.c, self.t, 999999))


# ---- section 48: automatic hot-list ---------------------------------------------------

class TestHotList(Base):
    def test_hot_list_uses_configured_threshold(self):
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש חשמלאי דחוף היום", service="חשמלאי",
                                phone="0501230021", urgency="דחוף", budget=3000, consent=True)
        self.assertIn(lid, [l["id"] for l in leads.hot_list(self.c, self.t)])
        config.set(self.c, self.t, "hot_lead_score", 101)  # nothing can reach it
        self.assertEqual(leads.hot_list(self.c, self.t), [])


if __name__ == "__main__":
    unittest.main()
