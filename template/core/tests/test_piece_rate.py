import unittest
from app import create_app
from database import get_db_connection

class TestPieceRateModule(unittest.TestCase):
    def setUp(self):
        from models.auto_sync import stop_auto_sync
        stop_auto_sync()
        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()
        conn = get_db_connection()
        conn.execute("""
            INSERT OR IGNORE INTO piece_rate_workers (id, worker_code, name, department_category, is_active)
            VALUES (1, 'CW-001', 'Karthik Raja', 'Book Binding', 1)
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM piece_rate_work_entries WHERE job_card_no IN ('JC-UNITTEST', 'JC-BATCH-TEST') OR remarks = 'Automated Unit Test Log' OR payout_reference IN ('VOUCHER-TEST-1234', 'WEEKLY-BATCH-REF-9999')")
        conn.execute("DELETE FROM piece_rate_workers WHERE worker_code IN ('CW-TEST-99', 'CW-001')")
        conn.execute("DELETE FROM piece_rate_items WHERE item_code = 'ITEM-TEST-88'")
        conn.commit()
        conn.close()

    def test_piece_rate_index_page(self):
        """Test piece-rate dashboard & quantity tracker register page loads successfully."""
        res = self.client.get("/piece-rate/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Contract Piece-Rate Quantity Tracker", html)
        self.assertIn("Log Work Qty", html)
        self.assertIn("Work Logs & Item Quantity Register", html)

    def test_workers_directory_page(self):
        """Test contract workers directory page loads with workers listed."""
        res = self.client.get("/piece-rate/workers")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Contract Workers Registration & Directory", html)
        self.assertIn("CW-001", html)
        self.assertIn("Karthik Raja", html)

    def test_register_worker(self):
        """Test registering a new contract worker."""
        conn = get_db_connection()
        conn.execute("DELETE FROM piece_rate_workers WHERE worker_code = 'CW-TEST-99'")
        conn.commit()
        conn.close()

        res = self.client.post("/piece-rate/workers/create", data={
            "worker_code": "CW-TEST-99",
            "name": "Murugan Contractor",
            "phone": "9944001122",
            "id_proof_number": "TN-VTR-9988",
            "department_category": "Screen Printing",
            "payment_mode": "UPI",
            "upi_or_bank_details": "murugan@upi",
            "daily_target_qty": "1200",
            "notes": "Experienced screen printer"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        w = conn.execute("SELECT * FROM piece_rate_workers WHERE worker_code = 'CW-TEST-99'").fetchone()
        self.assertIsNotNone(w)
        self.assertEqual(w["name"], "Murugan Contractor")
        self.assertEqual(w["department_category"], "Screen Printing")
        conn.close()

    def test_items_master_page_and_create(self):
        """Test item master page and creating a new piece-rate item."""
        res = self.client.get("/piece-rate/items")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Piece-Rate Items & Rates Master", html)
        self.assertIn("ITEM-001", html)

        conn = get_db_connection()
        conn.execute("DELETE FROM piece_rate_items WHERE item_code = 'ITEM-TEST-88'")
        conn.commit()
        conn.close()

        create_res = self.client.post("/piece-rate/items/create", data={
            "item_code": "ITEM-TEST-88",
            "item_name": "Gold Foil Stamping Cover",
            "category": "Finishing",
            "unit_measure": "Pcs",
            "default_rate": "3.25"
        }, follow_redirects=True)
        self.assertEqual(create_res.status_code, 200)

        conn = get_db_connection()
        itm = conn.execute("SELECT * FROM piece_rate_items WHERE item_code = 'ITEM-TEST-88'").fetchone()
        self.assertIsNotNone(itm)
        self.assertEqual(itm["default_rate"], 3.25)
        conn.close()

    def test_log_quantity_entry_and_calculations(self):
        """Test logging an item quantity entry with gross, rejected, payable, and total wage calculations."""
        conn = get_db_connection()
        worker = conn.execute("SELECT id FROM piece_rate_workers LIMIT 1").fetchone()
        item = conn.execute("SELECT id, default_rate, unit_measure FROM piece_rate_items LIMIT 1").fetchone()
        conn.close()

        self.assertIsNotNone(worker)
        self.assertIsNotNone(item)

        res = self.client.post("/piece-rate/log-entry", data={
            "worker_id": worker["id"],
            "work_date": "2026-09-09",
            "item_id": item["id"],
            "job_card_no": "JC-UNITTEST",
            "quantity_completed": "500",
            "rejected_quantity": "10",
            "rate_per_unit": "4.50",
            "unit_measure": item["unit_measure"],
            "remarks": "Automated Unit Test Log"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        entry = conn.execute("""
            SELECT * FROM piece_rate_work_entries 
            WHERE worker_id = ? AND job_card_no = 'JC-UNITTEST'
            ORDER BY id DESC LIMIT 1
        """, (worker["id"],)).fetchone()
        self.assertIsNotNone(entry)
        # Payable Qty = 500 - 10 = 490
        self.assertEqual(entry["payable_quantity"], 490)
        # Total Amount = 490 * 4.50 = 2205.00
        self.assertEqual(entry["total_amount"], 2205.0)
        self.assertEqual(entry["payment_status"], "UNPAID")
        conn.close()

    def test_worker_statement_and_settlement(self):
        """Test item-wise worker statement page and batch payment settlement."""
        conn = get_db_connection()
        worker = conn.execute("SELECT id FROM piece_rate_workers LIMIT 1").fetchone()
        conn.close()

        # Check statement page
        res = self.client.get(f"/piece-rate/worker/{worker['id']}/statement")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Item-Wise Production & Earnings Summary", html)
        self.assertIn("Print Wage Slip", html)

        # Test settling payment
        settle_res = self.client.post(f"/piece-rate/worker/{worker['id']}/settle-payment", data={
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
            "payment_mode": "CASH",
            "payout_reference": "VOUCHER-TEST-1234"
        }, follow_redirects=True)
        self.assertEqual(settle_res.status_code, 200)

        conn = get_db_connection()
        unpaid = conn.execute("""
            SELECT COUNT(*) as cnt FROM piece_rate_work_entries
            WHERE worker_id = ? AND payment_status = 'UNPAID'
        """, (worker["id"],)).fetchone()["cnt"]
        self.assertEqual(unpaid, 0, "All entries in range should be settled to PAID")
        conn.close()

    def test_export_csv(self):
        """Test piece-rate CSV export endpoint."""
        res = self.client.get("/piece-rate/export-csv")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "text/csv")
        csv_text = res.get_data(as_text=True)
        self.assertIn("Worker Code,Worker Name", csv_text)
        self.assertIn("Item Name,Unit,Rate (Rs)", csv_text)

    def test_weekly_salary_dashboard(self):
        """Test weekly salary calculation dashboard with week filter and item-wise aggregates."""
        res = self.client.get("/piece-rate/weekly-salary?start_date=2026-09-07&end_date=2026-09-13")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Weekly Contractor Salary Calculation", html)
        self.assertIn("Total Weekly Contractor Wages", html)
        self.assertIn("Payable Pieces This Week", html)
        self.assertIn("Contractors Active", html)
        self.assertIn("Batch Print Weekly Slips", html)
        self.assertIn("Item-Wise Production Breakdown", html)

    def test_weekly_individual_slip(self):
        """Test individual contractor weekly wage slip view."""
        conn = get_db_connection()
        worker = conn.execute("SELECT id, name, worker_code FROM piece_rate_workers LIMIT 1").fetchone()
        conn.close()
        self.assertIsNotNone(worker)

        res = self.client.get(f"/piece-rate/worker/{worker['id']}/weekly-slip?start=2026-09-07&end=2026-09-13")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("WEEKLY CONTRACTOR PIECE-RATE WAGE SLIP", html)
        self.assertIn(worker["worker_code"], html)
        self.assertIn("Authorized Signatory", html)
        self.assertIn("Contractor Signature / Thumb", html)

    def test_weekly_slips_batch(self):
        """Test batch printable wage slips for all active contractors in week."""
        res = self.client.get("/piece-rate/weekly-slips-batch?start=2026-09-07&end=2026-09-13")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Batch Weekly Wage Slips", html)
        self.assertIn("Print All", html)

    def test_weekly_salary_batch_settle(self):
        """Test batch payout settlement marking all contractor entries in the week as PAID."""
        conn = get_db_connection()
        worker = conn.execute("SELECT id FROM piece_rate_workers LIMIT 1").fetchone()
        item = conn.execute("SELECT id, default_rate, unit_measure FROM piece_rate_items LIMIT 1").fetchone()

        # Insert a fresh unpaid work entry for the week
        conn.execute("""
            INSERT INTO piece_rate_work_entries (
                worker_id, work_date, item_id, item_name, job_card_no,
                rate_per_unit, unit_measure, quantity_completed, rejected_quantity,
                payable_quantity, total_amount, payment_status
            ) VALUES (?, '2026-09-08', ?, 'Test Batch Item', 'JC-BATCH-TEST', 5.0, 'Pcs', 200, 0, 200, 1000.0, 'UNPAID')
        """, (worker["id"], item["id"]))
        conn.commit()
        conn.close()

        res = self.client.post("/piece-rate/weekly-salary/settle-all", data={
            "start_date": "2026-09-07",
            "end_date": "2026-09-13",
            "payment_mode": "BANK TRANSFER",
            "payout_reference": "WEEKLY-BATCH-REF-9999"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        unpaid = conn.execute("""
            SELECT COUNT(*) as cnt FROM piece_rate_work_entries
            WHERE work_date BETWEEN '2026-09-07' AND '2026-09-13'
              AND payment_status = 'UNPAID'
        """).fetchone()["cnt"]
        self.assertEqual(unpaid, 0, "All weekly entries should be marked as PAID after batch settlement")
        conn.close()

    def test_weekly_salary_export_csv(self):
        """Test exporting weekly salary calculation register to CSV."""
        res = self.client.get("/piece-rate/weekly-salary/export-csv?start=2026-09-07&end=2026-09-13")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "text/csv")
        csv_text = res.get_data(as_text=True)
        self.assertIn("Worker Code,Worker Name,Trade Category,Week Start,Week End", csv_text)
        self.assertIn("Days Worked,Total Pieces Completed,Rejected Pieces,Payable Pieces", csv_text)
        self.assertIn("Total Weekly Wages (Rs),Paid Wages (Rs),Unpaid Balance (Rs)", csv_text)

if __name__ == "__main__":
    unittest.main()
