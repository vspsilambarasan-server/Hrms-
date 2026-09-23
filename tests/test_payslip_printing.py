import unittest
from app import create_app
from database import get_db_connection

class TestPayslipPrinting(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Ensure minimal fixture data exists for testing payslip rendering
        conn = get_db_connection()
        cur = conn.cursor()
        self.clean_fixture = False

        # Ensure an employee exists
        cur.execute("SELECT id FROM employees WHERE is_active = 1 LIMIT 1")
        emp = cur.fetchone()
        if not emp:
            cur.execute("""
                INSERT OR REPLACE INTO employees (
                    id, emp_no, first_name, last_name, email, department_id,
                    designation, join_date, salary_type, pay_frequency,
                    base_salary, shift_salary, ot_hourly_rate, employment_status, is_active
                ) VALUES (
                    9901, 'PRINT01', 'PrintTest', 'User', 'print.test@press.com', 1,
                    'Operator', '2026-01-01', 'WEEKLY', 'WEEKLY',
                    3000.0, 500.0, 75.0, 'ACTIVE', 1
                )
            """)
            emp_id = 9901
            self.clean_fixture = True
        else:
            emp_id = emp["id"]

        # Ensure a payroll run exists
        cur.execute("SELECT id FROM payroll_runs LIMIT 1")
        run = cur.fetchone()
        if not run:
            cur.execute("""
                INSERT INTO payroll_runs (
                    id, run_type, month, year, period_name, start_date, end_date,
                    total_employees, total_gross, total_net, status
                ) VALUES (
                    9901, 'MONTHLY', 9, 2026, 'September 2026', '2026-09-01', '2026-09-30',
                    1, 15000.0, 13500.0, 'DRAFT'
                )
            """)
            run_id = 9901
            self.clean_fixture = True
        else:
            run_id = run["id"]

        # Ensure a payslip exists
        cur.execute("SELECT id FROM payslips LIMIT 1")
        slip = cur.fetchone()
        if not slip:
            cur.execute("""
                INSERT INTO payslips (
                    id, payroll_run_id, employee_id, pay_frequency, base_salary, daily_rate,
                    hourly_rate, days_in_month, days_worked, gross_earnings, total_deductions,
                    net_pay, status
                ) VALUES (
                    9901, ?, ?, 'MONTHLY', 15000.0, 576.92,
                    72.12, 30, 26, 15000.0, 1500.0,
                    13500.0, 'GENERATED'
                )
            """, (run_id, emp_id))
            self.clean_fixture = True

        conn.commit()
        conn.close()

    def tearDown(self):
        if getattr(self, "clean_fixture", False):
            conn = get_db_connection()
            conn.execute("DELETE FROM payslips WHERE id = 9901")
            conn.execute("DELETE FROM payroll_runs WHERE id = 9901")
            conn.execute("DELETE FROM employees WHERE id = 9901")
            conn.commit()
            conn.close()

    def test_single_payslip_view_and_branding(self):
        conn = get_db_connection()
        slip = conn.execute("SELECT payslips.id as id, employees.emp_no, employees.first_name, employees.last_name FROM payslips JOIN employees ON payslips.employee_id = employees.id LIMIT 1").fetchone()
        conn.close()

        if not slip:
            self.skipTest("No payslip records available in database.")

        res = self.client.get(f"/payroll/payslip/{slip['id']}")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        # Verify company branding
        self.assertIn("Vasantham Printers", html)
        self.assertIn("Pay Advice / Payslip", html)

        # Verify employee details
        self.assertIn(slip["emp_no"], html)
        self.assertIn(slip["first_name"], html)
        self.assertIn(slip["last_name"], html)

        # Verify key payslip sections
        self.assertIn("Shift Attendance & Overtime Summary", html)
        self.assertIn("Earnings Component", html)
        self.assertIn("Deductions Component", html)
        self.assertIn("Net Payable Salary Disbursed", html)
        self.assertIn("Employee Signature", html)
        self.assertIn("Authorized Signatory", html)

        # Verify print button exists
        self.assertIn("window.print()", html)

    def test_single_payslip_autoprint_param(self):
        conn = get_db_connection()
        slip = conn.execute("SELECT id FROM payslips LIMIT 1").fetchone()
        conn.close()

        if not slip:
            self.skipTest("No payslip records available in database.")

        res = self.client.get(f"/payroll/payslip/{slip['id']}?print=1")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        # Verify auto-print script is present
        self.assertIn("new URLSearchParams(window.location.search).get('print') === '1'", html)
        self.assertIn("window.print()", html)

    def test_batch_print_all_payslips_route(self):
        conn = get_db_connection()
        run = conn.execute("SELECT id, period_name, total_employees FROM payroll_runs LIMIT 1").fetchone()
        conn.close()

        if not run:
            self.skipTest("No payroll runs available in database.")

        res = self.client.get(f"/payroll/run/{run['id']}/print-all")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        # Verify batch header & branding
        self.assertIn("Batch Payslip Print Preview", html)
        self.assertIn("Vasantham Printers", html)
        self.assertIn(run["period_name"], html)
        self.assertIn("Print All", html)
        self.assertIn("Back to Register", html)

        # Verify page-break CSS structure for clean multi-page printing
        self.assertIn("payslip-page", html)
        self.assertIn("page-break-after: always", html)

        # Verify at least one employee payslip rendered
        self.assertIn("Pay Advice / Payslip", html)
        self.assertIn("Employee Signature", html)
        self.assertIn("Authorized Signatory", html)

    def test_view_run_has_print_all_and_individual_print_links(self):
        conn = get_db_connection()
        run = conn.execute("SELECT id FROM payroll_runs LIMIT 1").fetchone()
        conn.close()

        if not run:
            self.skipTest("No payroll runs available in database.")

        res = self.client.get(f"/payroll/run/{run['id']}")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")

        # Verify "Print All Payslips" button is present in register
        self.assertIn(f"/payroll/run/{run['id']}/print-all", html)
        self.assertIn("Print All Payslips", html)

        # Verify individual "Print" link is present in row actions
        self.assertIn("?print=1", html)
        self.assertIn("Print", html)

if __name__ == "__main__":
    unittest.main()
