import unittest
import sqlite3
from database import get_db_connection, init_db
from models.payroll_engine import run_monthly_payroll
from models.ot_engine import calculate_hourly_rate, compute_ot_payout

class TestPayrollEngine(unittest.TestCase):

    def setUp(self):
        self.conn = get_db_connection()

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM payroll_runs WHERE month = 9 AND year = 2026")
        for r in cur.fetchall():
            cur.execute("DELETE FROM payslips WHERE payroll_run_id = ?", (r["id"],))
            cur.execute("DELETE FROM payroll_runs WHERE id = ?", (r["id"],))
        cur.execute("DELETE FROM employees WHERE id = 999")
        cur.execute("DELETE FROM departments WHERE id = 999")
        self.conn.commit()
        self.conn.close()

    def test_hourly_rate_calculation(self):
        emp = {"salary_type": "MONTHLY", "base_salary": 41600.0}
        settings = {"standard_month_days": 26, "standard_day_hours": 8.0}
        # 41600 / (26 * 8) = 41600 / 208 = 200.00
        rate = calculate_hourly_rate(emp, settings)
        self.assertEqual(rate, 200.0)

    def test_ot_payout_with_multipliers(self):
        hourly_rate = 200.0
        # 3 hours at 1.5x
        ot_pay_norm = compute_ot_payout(3.0, hourly_rate, 1.5)
        self.assertEqual(ot_pay_norm, 900.0)
        
        # 4 hours at 2.0x (Weekend)
        ot_pay_wknd = compute_ot_payout(4.0, hourly_rate, 2.0)
        self.assertEqual(ot_pay_wknd, 1600.0)

    def test_payroll_run_execution(self):
        # Ensure at least one active employee exists for calculation test
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM employees")
        if cur.fetchone()["cnt"] == 0:
            cur.execute("INSERT OR IGNORE INTO departments (id, code, name) VALUES (999, 'TEST', 'Test Dept')")
            cur.execute("""
                INSERT INTO employees (id, emp_no, first_name, last_name, email, department_id, designation, join_date, base_salary, hourly_rate)
                VALUES (999, 'TEST99', 'Test', 'User', 'test@example.com', 999, 'Tester', '2026-01-01', 35000.0, 168.27)
            """)
            self.conn.commit()

        res = run_monthly_payroll(self.conn, 9, 2026)
        self.assertTrue(res["success"])
        self.assertGreater(res["processed_employees"], 0)
        self.assertGreater(res["total_gross"], 0)
        self.assertGreater(res["total_net"], 0)
        self.assertLess(res["total_net"], res["total_gross"]) # Net must be less than gross due to deductions

if __name__ == "__main__":
    unittest.main()
