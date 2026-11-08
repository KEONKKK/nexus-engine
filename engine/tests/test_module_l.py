"""Tests for NIGUN-134 module L (final engineering & production), sections 127-133.
Section 126 (comprehensive unit tests) is the whole engine/tests/ suite, not a single
test here. Section 134 (final acceptance audit) is tools/nigun-acceptance-audit.mjs,
outside Python's test runner by nature (it reads design/nigun/STATUS.md).
"""
import os
import tempfile
import time
import unittest
from pathlib import Path

from nexus_master import backup, brokerage, buyers, command, db, leads
from nexus_master.core import monitoring


class Base(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(":memory:")
        self.t = db.ensure_tenant(self.c)


# ---- sections 127/128: integration + end-to-end lead-to-conversion -------------------

class TestEndToEndLeadToConversion(Base):
    def test_lead_through_match_sale_and_revenue(self):
        # A supplier exists, available, covering the right area and service.
        supplier_id = brokerage.add_supplier(self.c, self.t, "יוסי אינסטלציה", keywords="אינסטלטור",
                                             areas="חולון", commission_pct=15)
        # A buyer with credit, who will purchase the lead.
        buyer_id = buyers.add_buyer(self.c, self.t, "קונה גדול", price_per_lead=50)
        buyers.top_up(self.c, self.t, buyer_id, 500)

        # Lead acquisition (as if through engine/public/public_api.py -- same add_lead call).
        lid, dup = leads.add_lead(self.c, self.t, text="מחפש אינסטלטור דחוף בחולון, נזילה",
                                  phone="+972501230040", consent=True, actor="public_api")
        self.assertIsNone(dup)
        lead = leads.get(self.c, self.t, lid)
        self.assertGreater(lead["score_total"], 0)

        # Matching: the real supplier shows up, ranked.
        matches = brokerage.match_lead(self.c, self.t, lead)
        self.assertEqual(matches[0]["supplier_id"], supplier_id)

        # Sale to the buyer -- credit moves, lead marked sold.
        balance = buyers.sell_lead(self.c, self.t, buyer_id, lid)
        self.assertEqual(balance, 450)
        self.assertEqual(leads.get(self.c, self.t, lid)["status"], "sold")

        # Conversion shows up on the dashboard.
        dash = command.dashboard(self.c, self.t)
        self.assertEqual(dash["money_received_from_buyers"], 500)

        # Referral proposal to the supplier + closing it adds commission revenue too.
        deal_id = brokerage.propose(self.c, self.t, lid, supplier_id)
        commission = brokerage.close_deal(self.c, self.t, deal_id, 2000)
        self.assertEqual(commission, 300)
        self.assertEqual(brokerage.commissions_total(self.c, self.t), 300)


# ---- section 129: migration + recovery -----------------------------------------------

class TestMigrationAndRecovery(unittest.TestCase):
    def test_file_db_survives_close_and_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "nexus-test.db")
            conn = db.connect(path)
            t = db.ensure_tenant(conn)
            lid, _ = leads.add_lead(conn, t, text="מחפש גנן", phone="0501230041")
            conn.close()

            # Simulated restart: fresh connection, migrate() runs again against an
            # already-migrated file and must not crash or lose the row.
            conn2 = db.connect(path)
            t2 = db.ensure_tenant(conn2, "default")
            self.assertEqual(t2, t)
            self.assertIsNotNone(leads.get(conn2, t2, lid))
            conn2.close()


# ---- section 130: pagination ----------------------------------------------------------

class TestPagination(Base):
    def test_pages_do_not_overlap_and_cover_everything(self):
        ids = []
        for i in range(5):
            lid, _ = leads.add_lead(self.c, self.t, text="מחפש גנן", phone=f"050123005{i}")
            ids.append(lid)
        page1 = leads.list_leads(self.c, self.t, limit=2, offset=0)
        page2 = leads.list_leads(self.c, self.t, limit=2, offset=2)
        page3 = leads.list_leads(self.c, self.t, limit=2, offset=4)
        seen = [l["id"] for l in page1 + page2 + page3]
        self.assertEqual(len(seen), len(set(seen)), "pages overlapped")
        self.assertEqual(set(seen), set(ids))
        self.assertEqual(leads.list_leads(self.c, self.t, limit=2, offset=10), [])


# ---- section 132: monitoring/alerts ---------------------------------------------------

class TestMonitoring(Base):
    def test_healthy_system_has_no_alerts(self):
        self.assertEqual(monitoring.active_alerts(self.c, self.t), [])

    def test_log_error_is_queryable(self):
        monitoring.log_error(self.c, self.t, "test", "משהו נכשל", {"x": 1})
        errs = monitoring.recent_errors(self.c, self.t)
        self.assertEqual(errs[0]["data"]["message"], "משהו נכשל")

    def test_audit_tamper_raises_a_critical_alert(self):
        leads.add_lead(self.c, self.t, text="מחפש גנן", phone="0501230060")
        self.c.execute("UPDATE audit SET data='{\"x\":1}' WHERE id=1")
        alerts = monitoring.active_alerts(self.c, self.t)
        self.assertTrue(any(a["code"] == "audit_tamper" for a in alerts))


# ---- section 133: backup + tested restore ---------------------------------------------

class TestBackupAndRestore(unittest.TestCase):
    def test_backup_then_restore_round_trips_data(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "nexus.db")
            conn = db.connect(db_path)
            t = db.ensure_tenant(conn)
            lid, _ = leads.add_lead(conn, t, text="מחפש חשמלאי", phone="0501230070")
            conn.close()

            backup_path = backup.make_backup(db_path, str(Path(d) / "backups"))
            self.assertTrue(Path(backup_path).exists())

            # Simulate data loss, then restore.
            os.remove(db_path)
            backup.restore_backup(backup_path, db_path)
            restored = db.connect(db_path)
            rt = db.ensure_tenant(restored)
            self.assertIsNotNone(leads.get(restored, rt, lid))
            restored.close()

    def test_restore_moves_existing_db_aside_instead_of_destroying_it(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "nexus.db")
            conn = db.connect(db_path)
            leads.add_lead(conn, db.ensure_tenant(conn), text="מקורי", phone="0501230080")
            conn.close()
            backup_path = backup.make_backup(db_path, str(Path(d) / "backups"))

            # Write something different into the "live" db before restoring over it.
            conn2 = db.connect(db_path)
            leads.add_lead(conn2, db.ensure_tenant(conn2), text="שונה-אחרי-הגיבוי", phone="0501230081")
            conn2.close()

            backup.restore_backup(backup_path, db_path)
            aside_files = list(Path(d).glob("nexus.db.before-restore-*"))
            self.assertEqual(len(aside_files), 1)


if __name__ == "__main__":
    unittest.main()
