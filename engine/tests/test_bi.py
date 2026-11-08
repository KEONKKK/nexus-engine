import unittest

from nexus_master import brokerage, buyers, db, leads
from nexus_master.bi import (conversion_rate, deal_values, get_default_commission_pct,
                             profitability_report, revenue_by_salesperson, revenue_dashboard,
                             set_default_commission_pct, supplier_performance)
from nexus_master.bi.commission import projected_commission


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


class TestRevenueDashboard(Base):
    def test_structure_separates_received_from_projected(self):
        out = revenue_dashboard(self.c, self.t)
        self.assertIn("received", out)
        self.assertIn("projected", out)
        self.assertIn("by_source", out)

    def test_received_reflects_real_topups_and_commissions(self):
        bid = buyers.add_buyer(self.c, self.t, "קונה", price_per_lead=50)
        buyers.top_up(self.c, self.t, bid, 500)
        out = revenue_dashboard(self.c, self.t)
        self.assertEqual(out["received"]["buyer_topups"], 500)

    def test_projected_never_counted_inside_received(self):
        sup = brokerage.add_supplier(self.c, self.t, "ספק", keywords="חשמלאי")
        lid, _ = leads.add_lead(self.c, self.t, text="מחפש חשמלאי", consent=True)
        brokerage.propose(self.c, self.t, lid, sup)
        out = revenue_dashboard(self.c, self.t)
        self.assertEqual(out["received"]["commissions_closed"], 0)
        self.assertEqual(len(out["projected"]["commissions_open_deals"]["open_deals"]), 1)


class TestConversionRate(Base):
    def test_no_leads_is_zero_not_a_crash(self):
        self.assertEqual(conversion_rate(self.c, self.t), {"total": 0, "converted": 0, "conversion_pct": 0})

    def test_counts_won_and_sold_as_converted(self):
        l1, _ = leads.add_lead(self.c, self.t, text="a", consent=True)
        l2, _ = leads.add_lead(self.c, self.t, text="b", phone="0501111111", consent=True)
        leads.set_status(self.c, self.t, l1, "won")
        out = conversion_rate(self.c, self.t)
        self.assertEqual(out, {"total": 2, "converted": 1, "conversion_pct": 50.0})


class TestDealValues(Base):
    def test_only_counts_closed_deals(self):
        sup = brokerage.add_supplier(self.c, self.t, "ספק", commission_pct=10)
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        did = brokerage.propose(self.c, self.t, lid, sup)
        brokerage.close_deal(self.c, self.t, did, 1000)
        out = deal_values(self.c, self.t)
        self.assertEqual(out, {"closed_deals": 1, "total_amount": 1000, "avg_amount": 1000.0})


class TestCommission(Base):
    def test_default_pct_matches_brokerage_default(self):
        self.assertEqual(get_default_commission_pct(self.c, self.t), 10.0)

    def test_set_default_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            set_default_commission_pct(self.c, self.t, 150)

    def test_set_default_persists(self):
        set_default_commission_pct(self.c, self.t, 15)
        self.assertEqual(get_default_commission_pct(self.c, self.t), 15)

    def test_projected_commission_estimates_from_historical_average(self):
        sup = brokerage.add_supplier(self.c, self.t, "ספק", commission_pct=20)
        lid1, _ = leads.add_lead(self.c, self.t, text="a", consent=True)
        did1 = brokerage.propose(self.c, self.t, lid1, sup)
        brokerage.close_deal(self.c, self.t, did1, 1000)  # historical average = 1000
        lid2, _ = leads.add_lead(self.c, self.t, text="b", phone="0501111111", consent=True)
        brokerage.propose(self.c, self.t, lid2, sup)  # open deal, no real amount
        out = projected_commission(self.c, self.t)
        self.assertEqual(out["based_on_avg_closed_deal"], 1000.0)
        self.assertEqual(out["total_estimate"], 200.0)  # 1000 * 20%

    def test_no_history_yields_zero_not_a_crash(self):
        sup = brokerage.add_supplier(self.c, self.t, "ספק")
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        brokerage.propose(self.c, self.t, lid, sup)
        out = projected_commission(self.c, self.t)
        self.assertEqual(out["total_estimate"], 0.0)


class TestRevenueBySalesperson(Base):
    def test_not_available_without_assignee_column(self):
        out = revenue_by_salesperson(self.c, self.t)
        self.assertFalse(out["available"])
        self.assertIn("assignee", out["why"])


class TestSupplierPerformance(Base):
    def test_includes_suppliers_with_zero_deals(self):
        brokerage.add_supplier(self.c, self.t, "ספק חדש")
        out = supplier_performance(self.c, self.t)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["deals_proposed"], 0)

    def test_counts_closed_deals_and_commission(self):
        sup = brokerage.add_supplier(self.c, self.t, "ספק", commission_pct=10)
        lid, _ = leads.add_lead(self.c, self.t, text="x", consent=True)
        did = brokerage.propose(self.c, self.t, lid, sup)
        brokerage.close_deal(self.c, self.t, did, 1000)
        out = supplier_performance(self.c, self.t)
        self.assertEqual(out[0]["deals_closed"], 1)
        self.assertEqual(out[0]["commission_total"], 100)


class TestProfitabilityReport(Base):
    def test_notes_absence_of_cost_data(self):
        out = profitability_report(self.c, self.t)
        self.assertIn("note", out)
        self.assertEqual(out["revenue_received_total"], 0)


class TestCommandsAndRoutes(Base):
    def test_command_revenue_report(self):
        from nexus_master import command
        out = command.run(self.c, self.t, "דוח הכנסות")
        self.assertTrue(out["ok"])
        self.assertIn("received", out["data"])

    def test_routes_registered(self):
        from nexus_master.api import App
        app = App(self.c, tenant="default")
        status, data = app.route("GET", "/bi/dashboard", {})
        self.assertEqual(status, 200)
        status, data = app.route("GET", "/bi/conversion", {})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
