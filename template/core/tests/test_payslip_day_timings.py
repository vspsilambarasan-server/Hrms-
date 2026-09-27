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

    def test_saturday_ot_carryover_and_morning_grace(self):
        from models.shift_engine import compute_daily_lateness
        sched_start = datetime.strptime("2026-09-26 09:00:00", "%Y-%m-%d %H:%M:%S")

        # 1. Ten minute grace: 09:10:20 is on-time (late_mins=0), 09:11:00 is late (late_mins=11)
        punches_on_time = ["2026-09-26 09:10:20"]
        late_mins, _ = compute_daily_lateness("2026-09-26", sched_start, punches_on_time, grace_late_mins=10)
        self.assertEqual(late_mins, 0)

        punches_late = ["2026-09-26 09:11:00"]
        late_mins_late, _ = compute_daily_lateness("2026-09-26", sched_start, punches_late, grace_late_mins=10)
        self.assertEqual(late_mins_late, 11)

        # 2. Verify Saturday OT deferral in weekly payslip 14407
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM payslips WHERE id = 14407")
        p = cur.fetchone()
        if p:
            self.assertEqual(p["approved_ot_hours"], 28.30)
            self.assertEqual(p["ot_pay"], 3820.50)
            self.assertEqual(p["gross_earnings"], 10420.50)
            self.assertEqual(p["net_pay"], 10420.50)

            # Check Saturday timing row
            timings = build_and_store_payslip_day_timings(self.conn, 14407)
            sat = next((t for t in timings if t["day_name"] == "Saturday"), None)
            self.assertIsNotNone(sat)
            self.assertEqual(sat["ot_hours"], 4.5)
            self.assertEqual(sat["ot_pay"], 0.0)
            self.assertEqual(sat["shift_wage"], 1100.0)
            self.assertEqual(sat["day_total_pay"], 1100.0)
            self.assertTrue(sat.get("is_saturday_carryover"))

if __name__ == "__main__":
    unittest.main()
