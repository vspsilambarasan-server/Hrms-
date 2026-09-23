import unittest
import os
from datetime import datetime, date, timedelta
from app import create_app
from database import get_db_connection, init_db
from models.ot_engine import sync_overtime_from_attendance
from models.email_alerts import get_missed_morning_punch_employees
from models.payroll_engine import run_monthly_payroll, run_weekly_payroll

class TestAttendanceRulesAndAdmin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()

        self.conn = get_db_connection()
        self.cursor = self.conn.cursor()

        # Find or create a test department and shift
        self.cursor.execute("SELECT id FROM departments LIMIT 1")
        row = self.cursor.fetchone()
        self.dept_id = row["id"] if row else 1

        self.cursor.execute("SELECT id FROM shifts LIMIT 1")
        row = self.cursor.fetchone()
        self.shift_id = row["id"] if row else 1

    def tearDown(self):
        # Clean up test records created
        self.cursor.execute("DELETE FROM attendance_punches WHERE punch_time LIKE '2026-11-%'")
        self.cursor.execute("DELETE FROM attendance_records WHERE date LIKE '2026-11-%'")
        self.cursor.execute("DELETE FROM overtime_records WHERE date LIKE '2026-11-%'")
        self.cursor.execute("DELETE FROM payslips WHERE payroll_run_id IN (SELECT id FROM payroll_runs WHERE period_name LIKE '%Nov 2026%' OR period_name LIKE '%November 2026%' OR start_date LIKE '2026-11-%')")
        self.cursor.execute("DELETE FROM payroll_runs WHERE period_name LIKE '%Nov 2026%' OR period_name LIKE '%November 2026%' OR start_date LIKE '2026-11-%'")
        self.cursor.execute("DELETE FROM payslips WHERE employee_id IN (SELECT id FROM employees WHERE emp_no LIKE 'TEST-%')")
        self.cursor.execute("DELETE FROM employees WHERE emp_no LIKE 'TEST-%'")
        self.cursor.execute("DELETE FROM employee_deletion_logs WHERE emp_no LIKE 'TEST-%'")
        self.conn.commit()
        self.conn.close()

    def test_admin_exemption_from_morning_alert(self):
        """Verify an employee marked as admin is NEVER included in missed morning punch alert."""
        target_date = "2026-11-01"

        # 1. Create a normal employee with no punches
        self.cursor.execute("""
            INSERT INTO employees (emp_no, first_name, last_name, email, phone, department_id, designation, join_date, default_shift_id, is_admin, is_active)
            VALUES ('TEST-NORM', 'Normal', 'Worker', 'norm@test.com', '1234567890', ?, 'Operator', '2026-01-01', ?, 0, 1)
        """, (self.dept_id, self.shift_id))

        # 2. Create an admin employee with no punches
        self.cursor.execute("""
            INSERT INTO employees (emp_no, first_name, last_name, email, phone, department_id, designation, join_date, default_shift_id, is_admin, is_active)
            VALUES ('TEST-ADMIN', 'Super', 'Admin', 'admin@test.com', '9876543210', ?, 'General Manager', '2026-01-01', ?, 1, 1)
        """, (self.dept_id, self.shift_id))
        self.conn.commit()

        missed = get_missed_morning_punch_employees(target_date)
        missed_emp_nos = [m["emp_no"] for m in missed]

        # Normal worker should be flagged for missing morning punch
        self.assertIn("TEST-NORM", missed_emp_nos)
        # Admin worker MUST NOT be flagged
        self.assertNotIn("TEST-ADMIN", missed_emp_nos)

    def test_admin_exemption_in_payroll_monthly_and_weekly(self):
        """Verify an employee marked as admin receives full base pay without LOP deduction even with 0 attendance logs."""
        month = 11
        year = 2026

        # Create admin employee with monthly pay 40000
        self.cursor.execute("""
            INSERT INTO employees (emp_no, first_name, last_name, email, phone, department_id, designation, join_date, default_shift_id, is_admin, is_active, salary_type, pay_frequency, base_salary, shift_salary, hourly_rate, ot_hourly_rate)
            VALUES ('TEST-ADM-M', 'Admin', 'Monthly', 'admm@test.com', '1112223333', ?, 'Director', '2026-01-01', ?, 1, 1, 'MONTHLY', 'MONTHLY', 40000.0, 1538.46, 192.31, 192.31)
        """, (self.dept_id, self.shift_id))
        adm_id = self.cursor.lastrowid
        self.conn.commit()

        # Run monthly payroll for Nov 2026 (0 attendance punches for TEST-ADM-M)
        res = run_monthly_payroll(self.conn, month, year)
        self.assertTrue(res["success"])

        self.cursor.execute("""
            SELECT * FROM payslips WHERE employee_id = ? AND payroll_run_id = ?
        """, (adm_id, res["payroll_run_id"]))
        ps = self.cursor.fetchone()
        self.assertIsNotNone(ps)
        # Admin must have 0 absent days deducted and full gross pay
        self.assertEqual(ps["absent_days"], 0)
        self.assertEqual(ps["lop_deduction"], 0.0)
        self.assertGreaterEqual(ps["gross_earnings"], 40000.0)

        # Weekly payroll test for admin
        start_w = "2026-11-02"
        end_w = "2026-11-08"
        res_w = run_weekly_payroll(self.conn, start_date=start_w, end_date=end_w, filter_frequency="ALL")
        self.assertTrue(res_w["success"])

        self.cursor.execute("""
            SELECT * FROM payslips WHERE employee_id = ? AND payroll_run_id = ?
        """, (adm_id, res_w["payroll_run_id"]))
        ps_w = self.cursor.fetchone()
        self.assertIsNotNone(ps_w)
        self.assertEqual(ps_w["days_worked"], 6.0)
        self.assertEqual(ps_w["absent_days"], 0)

    def test_midnight_early_checkin_ot_approval(self):
        """Verify check-in between 12:00 AM and 5:30 AM strictly requires approval for OT (status=PENDING), even if <= 3.5h."""
        target_date = "2026-11-10"

        self.cursor.execute("""
            INSERT INTO employees (emp_no, first_name, last_name, email, phone, department_id, designation, join_date, default_shift_id, is_admin, is_active, salary_type, pay_frequency, base_salary, shift_salary, hourly_rate, ot_hourly_rate)
            VALUES ('TEST-NIGHT', 'Night', 'DutyStaff', 'night@test.com', '9998887777', ?, 'Printer', '2026-01-01', ?, 0, 1, 'MONTHLY', 'MONTHLY', 30000.0, 1153.85, 144.23, 100.0)
        """, (self.dept_id, self.shift_id))
        emp_id = self.cursor.lastrowid

        # Case 1: Check-in at 02:30 AM (between 12:00 AM and 5:30 AM) with 2.0 hours OT (<= 3.5h)
        self.cursor.execute("""
            INSERT INTO attendance_records (employee_id, date, shift_id, punch_in, punch_out, work_hours, status, raw_ot_hours)
            VALUES (?, ?, ?, '2026-11-10 02:30:00', '2026-11-10 11:30:00', 9.0, 'PRESENT', 2.0)
        """, (emp_id, target_date, self.shift_id))
        att_id = self.cursor.lastrowid
        self.conn.commit()

        sync_overtime_from_attendance(self.conn, att_id)

        self.cursor.execute("SELECT * FROM overtime_records WHERE attendance_id = ?", (att_id,))
        ot_rec = self.cursor.fetchone()
        self.assertIsNotNone(ot_rec)
        # MUST BE PENDING because check-in was at 02:30 AM!
        self.assertEqual(ot_rec["status"], "PENDING")
        self.assertEqual(ot_rec["approved_hours"], 0.0)
        self.assertIn("12:00 AM - 5:30 AM", ot_rec["reason"])

        # Case 2: Standard morning check-in at 09:00 AM with 2.0 hours OT (<= 3.5h)
        target_date_2 = "2026-11-11"
        self.cursor.execute("""
            INSERT INTO attendance_records (employee_id, date, shift_id, punch_in, punch_out, work_hours, status, raw_ot_hours)
            VALUES (?, ?, ?, '2026-11-11 09:00:00', '2026-11-11 20:00:00', 10.0, 'PRESENT', 2.0)
        """, (emp_id, target_date_2, self.shift_id))
        att_id_2 = self.cursor.lastrowid
        self.conn.commit()

        sync_overtime_from_attendance(self.conn, att_id_2)

        self.cursor.execute("SELECT * FROM overtime_records WHERE attendance_id = ?", (att_id_2,))
        ot_rec_2 = self.cursor.fetchone()
        self.assertIsNotNone(ot_rec_2)
        # Should be auto-approved because normal morning check-in and <= 3.5h
        self.assertEqual(ot_rec_2["status"], "APPROVED")
        self.assertEqual(ot_rec_2["approved_hours"], 2.0)

    def test_missed_middle_punch_detection_and_salary_gate(self):
        """
        Verify:
        1. Clock-in at 9:00 AM and logout with missing middle punches flags has_missed_mid_punch=1 and mid_punch_status='FLAGGED'.
        2. Unapproved day is NOT credited in payroll (salary held).
        3. Once approved via endpoint, salary is credited and paid.
        """
        target_date = "2026-11-15"

        self.cursor.execute("""
            INSERT INTO employees (emp_no, first_name, last_name, email, phone, department_id, designation, join_date, default_shift_id, is_admin, is_active, salary_type, pay_frequency, base_salary, shift_salary, hourly_rate, ot_hourly_rate)
            VALUES ('TEST-MID', 'Middle', 'Worker', 'mid@test.com', '4445556666', ?, 'Folder', '2026-01-01', ?, 0, 1, 'WEEKLY', 'WEEKLY', 3000.0, 500.0, 62.5, 62.5)
        """, (self.dept_id, self.shift_id))
        emp_id = self.cursor.lastrowid

        # Insert 2 punches: 09:00 AM (in) and 18:00 (logout), 0 intermediate punches
        self.cursor.execute("""
            INSERT INTO attendance_punches (employee_id, punch_time, punch_type)
            VALUES (?, '2026-11-15 09:00:00', 'IN'), (?, '2026-11-15 18:00:00', 'OUT')
        """, (emp_id, emp_id))
        self.cursor.execute("""
            INSERT INTO attendance_records (employee_id, date, shift_id, punch_in, punch_out, work_hours, status)
            VALUES (?, ?, ?, '2026-11-15 09:00:00', '2026-11-15 18:00:00', 8.0, 'PRESENT')
        """, (emp_id, target_date, self.shift_id))
        att_id = self.cursor.lastrowid
        self.conn.commit()

        # Load /attendance/?date=2026-11-15 to trigger evaluation and inspect UI
        resp = self.client.get(f"/attendance/?date={target_date}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Missed Mid Punch", resp.data)
        self.assertIn(b"Approve Salary", resp.data)

        # Check DB status: must be FLAGGED
        self.cursor.execute("SELECT has_missed_mid_punch, mid_punch_status FROM attendance_records WHERE id = ?", (att_id,))
        row = self.cursor.fetchone()
        self.assertEqual(row["has_missed_mid_punch"], 1)
        self.assertEqual(row["mid_punch_status"], "FLAGGED")

        # Run weekly payroll before approval: this day should NOT be credited!
        start_w = "2026-11-09"
        end_w = "2026-11-15"
        res_w1 = run_weekly_payroll(self.conn, start_date=start_w, end_date=end_w, filter_frequency="WEEKLY")
        self.assertTrue(res_w1["success"])

        self.cursor.execute("SELECT * FROM payslips WHERE employee_id = ? AND payroll_run_id = ?", (emp_id, res_w1["payroll_run_id"]))
        ps1 = self.cursor.fetchone()
        # Because this only day had an unapproved missed mid punch, 0 days worked are paid
        self.assertEqual(ps1["days_worked"], 0.0)
        self.assertEqual(ps1["gross_earnings"], 0.0)

        # Now approve via endpoint
        resp_approve = self.client.post(f"/attendance/approve-mid-punch/{att_id}")
        self.assertEqual(resp_approve.status_code, 302)

        # Verify DB status is now APPROVED
        self.cursor.execute("SELECT mid_punch_status, mid_punch_approved_by FROM attendance_records WHERE id = ?", (att_id,))
        row_approved = self.cursor.fetchone()
        self.assertEqual(row_approved["mid_punch_status"], "APPROVED")

        # Re-run weekly payroll after approval: day should NOW be credited!
        res_w2 = run_weekly_payroll(self.conn, start_date=start_w, end_date=end_w, filter_frequency="WEEKLY")
        self.assertTrue(res_w2["success"])

        self.cursor.execute("SELECT * FROM payslips WHERE employee_id = ? AND payroll_run_id = ?", (emp_id, res_w2["payroll_run_id"]))
        ps2 = self.cursor.fetchone()
        self.assertEqual(ps2["days_worked"], 1.0)
        self.assertEqual(ps2["gross_earnings"], 500.0)

if __name__ == "__main__":
    unittest.main()
