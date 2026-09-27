import unittest
import sqlite3
from datetime import datetime
from database import get_db_connection, init_db
from models.payroll_engine import build_and_store_payslip_day_timings, run_weekly_payroll
from app import create_app

class TestPayslipDayTimings(unittest.TestCase):
    def setUp(self):
        init_db()
        self.conn = get_db_connection()
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        self.conn.close()

    def test_payslip_day_timings_table_exists(self):
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(payslip_day_timings)")
        cols = [r["name"] for r in cur.fetchall()]
        required = [
            "id", "payslip_id", "payroll_run_id", "employee_id", "emp_no", "employee_name",
            "date", "day_name", "status", "punch_in", "punch_out", "punches_text",
            "work_hours", "ot_hours", "shift_wage", "ot_rate", "ot_pay", "day_total_pay", "created_at"
        ]
        for col in required:
            self.assertIn(col, cols, f"Column {col} missing from payslip_day_timings")

    def test_cross_midnight_punch_attribution(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM employees WHERE emp_no = 'EMP0018'")
        emp = cur.fetchone()
        if not emp:
            self.skipTest("EMP0018 not present in test database")

        emp_id = emp["id"]
        # Find latest weekly payslip for EMP0018
        cur.execute("""
            SELECT p.id
            FROM payslips p
            JOIN payroll_runs pr ON p.payroll_run_id = pr.id
            WHERE p.employee_id = ? AND pr.run_type = 'WEEKLY'
            ORDER BY p.id DESC LIMIT 1
        """, (emp_id,))
        p_row = cur.fetchone()
        if not p_row:
            self.skipTest("No weekly payslip for EMP0018")

        payslip_id = p_row["id"]
        timings = build_and_store_payslip_day_timings(self.conn, payslip_id)
        self.assertTrue(len(timings) > 0)

        timings_map = {t["date"]: t for t in timings}
        # Check Tuesday Sep 22: Last OUT must be 02:11
        if "2026-09-22" in timings_map:
            tue = timings_map["2026-09-22"]
            self.assertEqual(tue["punch_in"], "09:33")
            self.assertEqual(tue["punch_out"], "02:11")
            self.assertIn("02:11", tue["punches"])

        # Check Wednesday Sep 23: First IN must be 09:33, NOT 02:11!
        if "2026-09-23" in timings_map:
            wed = timings_map["2026-09-23"]
            self.assertEqual(wed["punch_in"], "09:33")
            self.assertNotIn("02:11", wed["punches"])
            self.assertEqual(wed["punch_out"], "21:11")

        # Verify records stored in database
        cur.execute("SELECT COUNT(*) as cnt FROM payslip_day_timings WHERE payslip_id = ?", (payslip_id,))
        cnt = cur.fetchone()["cnt"]
        self.assertEqual(cnt, len(timings))

    def test_export_timings_csv_route(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM payslips ORDER BY id DESC LIMIT 1")
        p_row = cur.fetchone()
        if not p_row:
            self.skipTest("No payslip available to test export")

        payslip_id = p_row["id"]
        resp = self.client.get(f"/payroll/payslip/{payslip_id}/timings/export")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.content_type)
        csv_text = resp.data.decode("utf-8")
        self.assertIn("Employee No", csv_text)
        self.assertIn("All Punches", csv_text)
        self.assertIn("Daily Shift Wage", csv_text)

if __name__ == "__main__":
    unittest.main()
