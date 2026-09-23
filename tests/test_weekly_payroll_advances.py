import unittest
from datetime import date
from database import get_db_connection, init_db
from models.payroll_engine import (
    run_weekly_payroll,
    run_monthly_payroll,
    finalize_payroll_run,
    adjust_draft_payslip
)

class TestWeeklyPayrollAndAdvances(unittest.TestCase):

    def setUp(self):
        init_db()
        self.conn = get_db_connection()
        cur = self.conn.cursor()

        # Ensure test department
        cur.execute("INSERT OR IGNORE INTO departments (id, code, name) VALUES (888, 'PRINT', 'Printing Operations')")
        
        # Insert a dedicated test weekly employee
        cur.execute("""
            INSERT OR REPLACE INTO employees (
                id, emp_no, first_name, last_name, email, department_id,
                designation, join_date, salary_type, pay_frequency,
                base_salary, hourly_rate, hra, special_allowance,
                pf_deduction_pct, esi_deduction_pct, tax_deduction_pct, is_active
            ) VALUES (
                8888, 'WTEST88', 'Murugan', 'Pressman', 'murugan@vasantham.test', 888,
                'Offset Press Operator', '2026-01-01', 'MONTHLY', 'WEEKLY',
                6000.0, 133.33, 1000.0, 500.0,
                12.0, 0.75, 5.0, 1
            )
        """)
        
        # Clean any old test advances or payroll runs
        cur.execute("DELETE FROM advance_repayments WHERE advance_id IN (SELECT id FROM advances WHERE employee_id = 8888)")
        cur.execute("DELETE FROM advances WHERE employee_id = 8888")
        cur.execute("DELETE FROM payslips WHERE employee_id = 8888")
        cur.execute("DELETE FROM payroll_runs WHERE start_date = '2026-09-01' AND end_date = '2026-09-07'")
        
        self.conn.commit()

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM advance_repayments WHERE advance_id IN (SELECT id FROM advances WHERE employee_id = 8888)")
        cur.execute("DELETE FROM advances WHERE employee_id = 8888")
        cur.execute("DELETE FROM payslips WHERE payroll_run_id IN (SELECT id FROM payroll_runs WHERE period_name LIKE 'Test Week%' OR (start_date = '2026-09-01' AND end_date = '2026-09-07'))")
        cur.execute("DELETE FROM payslips WHERE employee_id = 8888")
        cur.execute("DELETE FROM payroll_runs WHERE period_name LIKE 'Test Week%' OR (start_date = '2026-09-01' AND end_date = '2026-09-07')")
        cur.execute("DELETE FROM employees WHERE id = 8888")
        cur.execute("DELETE FROM employee_deletion_logs WHERE emp_no = 'WTEST88' OR original_emp_id = 8888")
        self.conn.commit()
        self.conn.close()

    def test_weekly_payroll_with_advance_and_bonus(self):
        cur = self.conn.cursor()

        # 1. Grant advance of ₹5,000 with ₹1,000 weekly cut
        cur.execute("""
            INSERT INTO advances (
                employee_id, advance_date, total_amount, weekly_deduction,
                amount_repaid, remaining_amount, status, notes
            ) VALUES (8888, '2026-08-25', 5000.0, 1000.0, 0.0, 5000.0, 'ACTIVE', 'Test loan')
        """)
        adv_id = cur.lastrowid
        self.conn.commit()

        # 2. Run weekly payroll for 2026-09-01 to 2026-09-07 with ₹500 bonus
        res = run_weekly_payroll(
            self.conn,
            start_date="2026-09-01",
            end_date="2026-09-07",
            period_name="Test Week 36",
            bonus=500.0,
            filter_frequency="WEEKLY"
        )
        self.assertTrue(res["success"])
        run_id = res["payroll_run_id"]

        # 3. Verify payslip generated for employee
        cur.execute("SELECT * FROM payslips WHERE payroll_run_id = ? AND employee_id = 8888", (run_id,))
        p = dict(cur.fetchone())
        self.assertIsNotNone(p)
        self.assertEqual(p["bonus"], 500.0)
        self.assertEqual(p["advance_deduction"], 1000.0)
        self.assertGreater(p["gross_earnings"], p["base_salary"])
        self.assertEqual(p["pay_frequency"], "WEEKLY")

        # Net pay formula: gross - (statutory deductions + advance deduction)
        expected_net = round(p["gross_earnings"] - p["total_deductions"], 2)
        self.assertEqual(p["net_pay"], expected_net)

        # 4. Finalize payroll run and check advance repayment applied
        fin_res = finalize_payroll_run(self.conn, run_id)
        self.assertTrue(fin_res["success"])

        # Advance remaining balance should now be 5000 - 1000 = 4000
        cur.execute("SELECT * FROM advances WHERE id = ?", (adv_id,))
        adv_after = dict(cur.fetchone())
        self.assertEqual(adv_after["remaining_amount"], 4000.0)
        self.assertEqual(adv_after["amount_repaid"], 1000.0)
        self.assertEqual(adv_after["status"], "ACTIVE")

        # Check repayment logged in ledger
        cur.execute("SELECT * FROM advance_repayments WHERE advance_id = ?", (adv_id,))
        rep = dict(cur.fetchone())
        self.assertEqual(rep["amount"], 1000.0)
        self.assertEqual(rep["payment_type"], "PAYROLL_CUT")

    def test_adjust_draft_payslip(self):
        # Create a weekly run
        res = run_weekly_payroll(
            self.conn,
            start_date="2026-09-01",
            end_date="2026-09-07",
            period_name="Test Week 36",
            bonus=0.0,
            filter_frequency="WEEKLY"
        )
        run_id = res["payroll_run_id"]

        cur = self.conn.cursor()
        cur.execute("SELECT id FROM payslips WHERE payroll_run_id = ? AND employee_id = 8888", (run_id,))
        slip = cur.fetchone()
        self.assertIsNotNone(slip)
        slip_id = slip["id"]

        # Adjust bonus to 750 and advance cut to 500
        adj_res = adjust_draft_payslip(self.conn, slip_id, bonus=750.0, advance_deduction=500.0)
        self.assertTrue(adj_res["success"])

        cur.execute("SELECT * FROM payslips WHERE id = ?", (slip_id,))
        updated_p = dict(cur.fetchone())
        self.assertEqual(updated_p["bonus"], 750.0)
        self.assertEqual(updated_p["advance_deduction"], 500.0)

if __name__ == "__main__":
    unittest.main()
