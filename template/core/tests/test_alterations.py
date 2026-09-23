import unittest
from datetime import date
from app import create_app
from database import get_db_connection, init_db
from werkzeug.security import check_password_hash
from models.ot_engine import sync_overtime_from_attendance
from models.email_alerts import get_missed_morning_punch_employees, send_missed_morning_punch_alert
from models.payroll_engine import run_weekly_payroll, run_monthly_payroll

class TestAlterations(unittest.TestCase):

    def setUp(self):
        init_db()
        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()

        conn = get_db_connection()
        dept = conn.execute("SELECT id FROM departments LIMIT 1").fetchone()
        if not dept:
            conn.execute("INSERT INTO departments (code, name, description) VALUES ('PLANT', 'Plant Operations', 'Main facility')")
            conn.commit()
            dept = conn.execute("SELECT id FROM departments LIMIT 1").fetchone()
        self.dept_id = dept["id"]
        conn.close()

    def test_admin_user_seeded(self):
        """Verify default admin user credentials in database."""
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        conn.close()

        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "admin")
        self.assertTrue(check_password_hash(user["password_hash"], "admin123"))

    def test_login_flow(self):
        """Verify login page, invalid login rejection, and successful login."""
        # 1. GET login page
        res = self.client.get("/login")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Sign in to your account", res.data)

        # 2. Invalid login
        res_bad = self.client.post("/login", data={"username": "admin", "password": "wrongpassword"})
        self.assertEqual(res_bad.status_code, 200)
        self.assertIn(b"Invalid username or password", res_bad.data)

        # 3. Successful login
        res_good = self.client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)
        self.assertEqual(res_good.status_code, 200)
        self.assertIn(b"Welcome back", res_good.data)

    def test_ot_auto_approval_under_3_5_hours(self):
        """Verify OT <= 3.5h is auto-approved, and > 3.5h is marked PENDING."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Insert test employee
            cur.execute("""
                INSERT OR REPLACE INTO employees (
                    id, emp_no, first_name, last_name, email, department_id,
                    designation, join_date, salary_type, pay_frequency,
                    base_salary, shift_salary, ot_hourly_rate, is_active
                ) VALUES (
                    9901, 'OTTEST1', 'Ravi', 'Kumar', 'ravi@test.com', ?,
                    'Operator', '2026-01-01', 'WEEKLY', 'WEEKLY',
                    3000.0, 500.0, 75.0, 1
                )
            """, (self.dept_id,))

            # Clean old attendance / OT
            cur.execute("DELETE FROM overtime_records WHERE employee_id = 9901 OR attendance_id IN (99011, 99012)")
            cur.execute("DELETE FROM attendance_records WHERE employee_id = 9901 OR id IN (99011, 99012)")
            conn.commit()

            # Case A: 2.0 hours OT (<= 3.5h) -> Must be APPROVED automatically
            cur.execute("""
                INSERT OR REPLACE INTO attendance_records (
                    id, employee_id, date, raw_ot_hours, work_hours, status
                ) VALUES (99011, 9901, '2026-09-08', 2.0, 10.0, 'PRESENT')
            """)
            conn.commit()

            sync_overtime_from_attendance(conn, 99011)

            ot_rec = cur.execute("SELECT * FROM overtime_records WHERE attendance_id = 99011").fetchone()
            self.assertIsNotNone(ot_rec)
            self.assertEqual(ot_rec["status"], "APPROVED")
            self.assertEqual(ot_rec["approved_hours"], 2.0)
            self.assertEqual(ot_rec["calculated_ot_pay"], 150.0)  # 2.0h * 75.0/hr

            # Case B: 4.5 hours OT (> 3.5h) -> Must remain PENDING for supervisor review
            cur.execute("""
                INSERT OR REPLACE INTO attendance_records (
                    id, employee_id, date, raw_ot_hours, work_hours, status
                ) VALUES (99012, 9901, '2026-09-09', 4.5, 12.5, 'PRESENT')
            """)
            conn.commit()

            sync_overtime_from_attendance(conn, 99012)

            ot_rec2 = cur.execute("SELECT * FROM overtime_records WHERE attendance_id = 99012").fetchone()
            self.assertIsNotNone(ot_rec2)
            self.assertEqual(ot_rec2["status"], "PENDING")
            self.assertEqual(ot_rec2["approved_hours"], 0.0)
        finally:
            # Cleanup
            try:
                cur.execute("DELETE FROM overtime_records WHERE employee_id = 9901 OR attendance_id IN (99011, 99012)")
                cur.execute("DELETE FROM attendance_records WHERE employee_id = 9901 OR id IN (99011, 99012)")
                cur.execute("DELETE FROM employees WHERE id = 9901")
                conn.commit()
            except Exception:
                pass
            conn.close()

    def test_weekly_employee_pf_esi_toggles(self):
        """Verify PF and ESI are deducted ONLY if explicitly enabled."""
        conn = get_db_connection()
        cur = conn.cursor()
        run_id = None
        try:
            # Clean before test
            cur.execute("DELETE FROM employees WHERE id IN (9902, 9903)")
            conn.commit()

            # Employee 1: PF Disabled, ESI Disabled
            cur.execute("""
                INSERT OR REPLACE INTO employees (
                    id, emp_no, first_name, last_name, email, department_id,
                    designation, join_date, salary_type, pay_frequency,
                    shift_salary, ot_hourly_rate, base_salary,
                    pf_enabled, esi_enabled, is_active
                ) VALUES (
                    9902, 'WTESTNO', 'Suresh', 'Babu', 'suresh@test.com', ?,
                    'Helper', '2026-01-01', 'WEEKLY', 'WEEKLY',
                    500.0, 75.0, 3000.0,
                    0, 0, 1
                )
            """, (self.dept_id,))

            # Employee 2: PF Enabled, ESI Enabled
            cur.execute("""
                INSERT OR REPLACE INTO employees (
                    id, emp_no, first_name, last_name, email, department_id,
                    designation, join_date, salary_type, pay_frequency,
                    shift_salary, ot_hourly_rate, base_salary,
                    pf_enabled, esi_enabled, is_active
                ) VALUES (
                    9903, 'WTESTYES', 'Ganesh', 'Murthy', 'ganesh@test.com', ?,
                    'Binder', '2026-01-01', 'WEEKLY', 'WEEKLY',
                    500.0, 75.0, 3000.0,
                    1, 1, 1
                )
            """, (self.dept_id,))
            conn.commit()

            # Run weekly payroll
            res = run_weekly_payroll(
                conn,
                start_date="2026-09-15",
                end_date="2026-09-21",
                period_name="Test Week 38",
                filter_frequency="WEEKLY"
            )
            self.assertTrue(res["success"])
            run_id = res["payroll_run_id"]

            p_no = cur.execute("SELECT * FROM payslips WHERE payroll_run_id = ? AND employee_id = 9902", (run_id,)).fetchone()
            p_yes = cur.execute("SELECT * FROM payslips WHERE payroll_run_id = ? AND employee_id = 9903", (run_id,)).fetchone()

            self.assertIsNotNone(p_no)
            self.assertIsNotNone(p_yes)

            # Employee 1: No PF, No ESI
            self.assertEqual(p_no["pf_deduction"], 0.0)
            self.assertEqual(p_no["esi_deduction"], 0.0)

            # Employee 2: PF & ESI deducted
            self.assertGreater(p_yes["pf_deduction"], 0.0)
            self.assertGreater(p_yes["esi_deduction"], 0.0)
        finally:
            # Cleanup
            try:
                if run_id:
                    cur.execute("DELETE FROM payslips WHERE payroll_run_id = ?", (run_id,))
                    cur.execute("DELETE FROM payroll_runs WHERE id = ?", (run_id,))
                cur.execute("DELETE FROM employees WHERE id IN (9902, 9903)")
                conn.commit()
            except Exception:
                pass
            conn.close()

    def test_diwali_bonus_calculation(self):
        """Verify statutory Diwali bonus computation between 8.33% and 20%."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["full_name"] = "Administrator"
            sess["role"] = "admin"

        res = self.client.post("/settlement/diwali-bonus", data={
            "action": "calculate",
            "year": 2026,
            "bonus_percentage": 10.0
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        records = conn.execute("SELECT * FROM diwali_bonus_records WHERE year = 2026").fetchall()
        conn.close()

        self.assertGreater(len(records), 0)
        for r in records:
            self.assertEqual(r["year"], 2026)
            self.assertEqual(r["bonus_percentage"], 10.0)
            self.assertGreater(r["bonus_amount"], 0.0)

        # Clean up test diwali bonus records
        conn = get_db_connection()
        conn.execute("DELETE FROM diwali_bonus_records WHERE year = 2026")
        conn.commit()
        conn.close()

    def test_company_profile_settings(self):
        """Verify updating company profile, license and supervisor name."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["full_name"] = "Administrator"
            sess["role"] = "admin"

        res = self.client.post("/settings/company", data={
            "company_name": "Vasantham Printers Madurai",
            "address": "123 Press Works Road, Madurai",
            "phone": "+91 98765 11111",
            "email": "press@vasantham.com",
            "gstin": "33AAAAA9999Z1Z5",
            "factory_license_no": "FACT/MDU/2026/888",
            "supervisor_name": "K. Murugesan",
            "smtp_host": "smtp.gmail.com",
            "smtp_port": "587",
            "smtp_user": "alert@vasantham.com",
            "smtp_password": "secretpassword",
            "smtp_use_tls": "1",
            "alert_recipient_email": "owner@vasantham.com"
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        s = conn.execute("SELECT * FROM company_settings LIMIT 1").fetchone()

        self.assertEqual(s["company_name"], "Vasantham Printers Madurai")
        self.assertEqual(s["factory_license_no"], "FACT/MDU/2026/888")
        self.assertEqual(s["supervisor_name"], "K. Murugesan")
        self.assertEqual(s["alert_recipient_email"], "owner@vasantham.com")

        # Restore original company profile
        conn.execute("""
            UPDATE company_settings SET
                company_name = 'Vasantham Printers',
                address = '124, Press Colony, Sivakasi Road, Virudhunagar Dist, Tamil Nadu - 626123',
                phone = '+91 94431 23456',
                email = 'admin@vasanthamprinters.com',
                gstin = '33AAAAA0000A1Z5',
                factory_license_no = 'FAC/TN/VNR/2021/489',
                supervisor_name = 'Authorized Press Supervisor',
                alert_recipient_email = 'supervisor@vasanthamprinters.com'
            WHERE id = 1
        """)
        conn.commit()
        conn.close()

if __name__ == "__main__":
    unittest.main()
