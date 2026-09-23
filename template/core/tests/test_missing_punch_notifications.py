import unittest
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock
from app import create_app
from database import get_db_connection
from models.missing_punch_detector import (
    analyze_employee_shift_gaps,
    scan_and_record_missing_punches,
    auto_resolve_alerts_for_punch
)
from models.email_alerts import send_missing_punch_manager_notification

class TestMissingPunchNotifications(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})
        cls.client = cls.app.test_client()

    def setUp(self):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM departments LIMIT 1")
            dept_id = cur.fetchone()["id"]

            cur.execute("SELECT id FROM shifts WHERE code = 'GEN' LIMIT 1")
            shift = cur.fetchone()
            shift_id = shift["id"] if shift else 1

            cur.execute("DELETE FROM employees WHERE emp_no = 'TESTMP01'")
            cur.execute("""
                INSERT INTO employees (
                    emp_no, first_name, last_name, email, department_id, designation,
                    join_date, default_shift_id, salary_type, shift_salary, ot_hourly_rate,
                    is_active, is_admin
                ) VALUES (
                    'TESTMP01', 'Test', 'Worker', 'testmp01@vasanthamprinters.com',
                    ?, 'Press Operator', '2026-01-01', ?, 'WEEKLY', 600.0, 100.0, 1, 0
                )
            """, (dept_id, shift_id))
            self.emp_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM missing_punch_alerts WHERE employee_id = ?", (self.emp_id,))
            cur.execute("DELETE FROM punch_edit_logs WHERE employee_id = ?", (self.emp_id,))
            cur.execute("DELETE FROM attendance_punches WHERE employee_id = ?", (self.emp_id,))
            cur.execute("DELETE FROM attendance_records WHERE employee_id = ?", (self.emp_id,))
            cur.execute("DELETE FROM employees WHERE id = ?", (self.emp_id,))
            conn.commit()
        finally:
            conn.close()

    def test_detect_missing_clock_out(self):
        """Verify 18:00 shift end requires no punch (no gap), while other shifts detect MISSING_CLOCK_OUT."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            past_date = "2026-09-01"
            cur.execute("""
                INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
                VALUES (?, '2026-09-01 09:02:15', 'IN', 'DEVICE_TEST')
            """, (self.emp_id,))
            conn.commit()

            # 1. 18:00 shift: no MISSING_CLOCK_OUT per company policy
            gaps_18 = analyze_employee_shift_gaps(self.emp_id, past_date, conn=conn)
            gap_types_18 = [g["gap_type"] for g in gaps_18]
            self.assertNotIn("MISSING_CLOCK_OUT", gap_types_18)

            # 2. Non-18:00 shift (e.g., 17:00 end time): MISSING_CLOCK_OUT is detected
            cur.execute("SELECT id FROM shifts WHERE end_time != '18:00' LIMIT 1")
            other_shift = cur.fetchone()
            if not other_shift:
                cur.execute("""
                    INSERT INTO shifts (name, code, start_time, end_time, is_overnight)
                    VALUES ('Early Shift', 'EARLY', '08:00', '17:00', 0)
                """)
                other_shift_id = cur.lastrowid
            else:
                other_shift_id = other_shift["id"]

            cur.execute("UPDATE employees SET default_shift_id = ? WHERE id = ?", (other_shift_id, self.emp_id))
            conn.commit()

            gaps_other = analyze_employee_shift_gaps(self.emp_id, past_date, conn=conn)
            gap_types_other = [g["gap_type"] for g in gaps_other]
            self.assertIn("MISSING_CLOCK_OUT", gap_types_other)
        finally:
            conn.close()

    def test_detect_missing_lunch_return(self):
        """When an employee clocked out for lunch at 13:02 but never clocked back in, detect MISSING_LUNCH_RETURN."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            past_date = "2026-09-01"
            cur.execute("""
                INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
                VALUES (?, '2026-09-01 09:00:00', 'IN', 'DEVICE_TEST'),
                       (?, '2026-09-01 13:02:00', 'OUT', 'DEVICE_TEST')
            """, (self.emp_id, self.emp_id))
            conn.commit()

            gaps = analyze_employee_shift_gaps(self.emp_id, past_date, conn=conn)
        finally:
            conn.close()

        gap_types = [g["gap_type"] for g in gaps]
        self.assertIn("MISSING_LUNCH_RETURN", gap_types)

        return_gap = next(g for g in gaps if g["gap_type"] == "MISSING_LUNCH_RETURN")
        self.assertEqual(return_gap["suggested_punch_type"], "IN")
        self.assertEqual(return_gap["suggested_timestamp"], "2026-09-01 14:00:00")

    def test_detect_missing_lunch_out(self):
        """When an employee clocked in at 09:00 and 14:05 and 18:00 but missed lunch departure, detect MISSING_LUNCH_OUT."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            past_date = "2026-09-01"
            cur.execute("""
                INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
                VALUES (?, '2026-09-01 09:00:00', 'IN', 'DEVICE_TEST'),
                       (?, '2026-09-01 14:05:00', 'IN', 'DEVICE_TEST'),
                       (?, '2026-09-01 18:00:00', 'OUT', 'DEVICE_TEST')
            """, (self.emp_id, self.emp_id, self.emp_id))
            conn.commit()

            gaps = analyze_employee_shift_gaps(self.emp_id, past_date, conn=conn)
        finally:
            conn.close()

        gap_types = [g["gap_type"] for g in gaps]
        self.assertIn("MISSING_LUNCH_OUT", gap_types)

        out_gap = next(g for g in gaps if g["gap_type"] == "MISSING_LUNCH_OUT")
        self.assertEqual(out_gap["suggested_punch_type"], "OUT")
        self.assertEqual(out_gap["suggested_timestamp"], "2026-09-01 13:00:00")

    def test_one_click_approval(self):
        """1-Click approve endpoint inserts the punch, logs audit, and updates alert to APPROVED."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO missing_punch_alerts (
                    employee_id, date, gap_type, expected_time,
                    suggested_punch_type, suggested_timestamp,
                    description, status
                ) VALUES (?, '2026-09-01', 'MISSING_CLOCK_OUT', '18:00:00', 'OUT', '2026-09-01 18:00:00', 'Test missing out', 'PENDING')
            """, (self.emp_id,))
            alert_id = cur.lastrowid

            # Initial morning punch
            cur.execute("""
                INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
                VALUES (?, '2026-09-01 09:00:00', 'IN', 'DEVICE_TEST')
            """, (self.emp_id,))
            conn.commit()
        finally:
            conn.close()

        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["role"] = "ADMIN"

        res = self.client.post(f"/attendance/missing-punches/approve/{alert_id}", follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT status, approved_by, inserted_punch_id FROM missing_punch_alerts WHERE id = ?", (alert_id,))
            row = cur.fetchone()
            self.assertEqual(row["status"], "APPROVED")
            self.assertEqual(row["approved_by"], "admin")
            self.assertIsNotNone(row["inserted_punch_id"])

            punch_id = row["inserted_punch_id"]
            cur.execute("SELECT * FROM attendance_punches WHERE id = ?", (punch_id,))
            p_row = cur.fetchone()
            self.assertEqual(p_row["punch_time"], "2026-09-01 18:00:00")
            self.assertEqual(p_row["punch_type"], "OUT")
            self.assertEqual(p_row["device_id"], "SYSTEM_CORRECTION")

            cur.execute("SELECT * FROM punch_edit_logs WHERE punch_id = ?", (punch_id,))
            log_row = cur.fetchone()
            self.assertIsNotNone(log_row)
            self.assertIn("1CLICK_CORRECTION_OUT", log_row["edit_type"])
        finally:
            conn.close()

    def test_one_click_approve_all(self):
        """1-Click approve-all endpoint approves all pending alerts in one request."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO missing_punch_alerts (
                    employee_id, date, gap_type, expected_time,
                    suggested_punch_type, suggested_timestamp,
                    description, status
                ) VALUES (?, '2026-09-01', 'MISSING_CLOCK_OUT', '18:00:00', 'OUT', '2026-09-01 18:00:00', 'Gap 1', 'PENDING'),
                         (?, '2026-09-02', 'MISSING_CLOCK_OUT', '18:00:00', 'OUT', '2026-09-02 18:00:00', 'Gap 2', 'PENDING')
            """, (self.emp_id, self.emp_id))
            conn.commit()
        finally:
            conn.close()

        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["role"] = "ADMIN"

        res = self.client.post("/attendance/missing-punches/approve-all", follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) as cnt FROM missing_punch_alerts WHERE employee_id = ? AND status = 'APPROVED'", (self.emp_id,))
            self.assertEqual(cur.fetchone()["cnt"], 2)
        finally:
            conn.close()

    def test_dismiss_alert(self):
        """Dismiss endpoint marks alert as DISMISSED with manager note."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO missing_punch_alerts (
                    employee_id, date, gap_type, expected_time,
                    suggested_punch_type, suggested_timestamp,
                    description, status
                ) VALUES (?, '2026-09-01', 'MISSING_CLOCK_OUT', '18:00:00', 'OUT', '2026-09-01 18:00:00', 'Dismiss test', 'PENDING')
            """, (self.emp_id,))
            alert_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["role"] = "ADMIN"

        res = self.client.post(f"/attendance/missing-punches/dismiss/{alert_id}", data={"reason": "Left early unauthorized"}, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT status, description FROM missing_punch_alerts WHERE id = ?", (alert_id,))
            row = cur.fetchone()
            self.assertEqual(row["status"], "DISMISSED")
            self.assertIn("Left early unauthorized", row["description"])
        finally:
            conn.close()

    @patch("models.email_alerts.smtplib.SMTP")
    def test_send_missing_punch_manager_notification(self, mock_smtp_class):
        """Manager email notification generates HTML alert and sends via SMTP."""
        mock_server = MagicMock()
        mock_smtp_class.return_value = mock_server

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                UPDATE company_settings
                SET alert_recipient_email = 'manager@vasanthamprinters.com',
                    smtp_user = 'notifications@vasanthamprinters.com',
                    smtp_password = 'dummy-app-password'
            """)
            cur.execute("""
                INSERT INTO missing_punch_alerts (
                    employee_id, date, gap_type, expected_time,
                    suggested_punch_type, suggested_timestamp,
                    description, status
                ) VALUES (?, '2026-09-01', 'MISSING_CLOCK_OUT', '18:00:00', 'OUT', '2026-09-01 18:00:00', 'Notification test gap', 'PENDING')
            """, (self.emp_id,))
            conn.commit()
        finally:
            conn.close()

        res = send_missing_punch_manager_notification()
        self.assertTrue(res["success"])
        self.assertGreaterEqual(res["count"], 1)
        mock_server.sendmail.assert_called_once()
        mock_server.quit.assert_called_once()

    def test_sidebar_and_subnav_no_missing_punches_link(self):
        """Verify Missing Punches link is removed from sidebar, header and subnav in registers."""
        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["role"] = "ADMIN"

        res_att = self.client.get("/attendance/")
        self.assertEqual(res_att.status_code, 200)
        html_att = res_att.data.decode("utf-8")
        # Sidebar should not have the link under shift operations
        self.assertNotIn('href="/attendance/missing-punches"', html_att)

        res_punch = self.client.get("/attendance/all-punches")
        self.assertEqual(res_punch.status_code, 200)
        html_punch = res_punch.data.decode("utf-8")
        self.assertNotIn('href="/attendance/missing-punches"', html_punch)

    def test_inline_missing_timings_in_daily_registers(self):
        """Verify pending missed timing badge and 1-click approve button appear inline in both registers."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO missing_punch_alerts (
                    employee_id, date, gap_type, expected_time,
                    suggested_punch_type, suggested_timestamp,
                    description, status
                ) VALUES (?, '2026-09-01', 'MISSING_CLOCK_OUT', '18:00:00', 'OUT', '2026-09-01 18:00:00', 'Missed clock-out test', 'PENDING')
            """, (self.emp_id,))
            alert_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        with self.client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["username"] = "admin"
            sess["role"] = "ADMIN"

        # 1. Daily Attendance Register (/attendance/?date=2026-09-01)
        res_idx = self.client.get("/attendance/?date=2026-09-01")
        self.assertEqual(res_idx.status_code, 200)
        html_idx = res_idx.data.decode("utf-8")
        self.assertIn("Missed OUT: 18:00", html_idx)
        self.assertIn(f"/attendance/missing-punches/approve/{alert_id}", html_idx)
        self.assertIn("Approve (18:00)", html_idx)

        # 2. Daily All Punches Register (/attendance/all-punches?date=2026-09-01)
        res_punches = self.client.get("/attendance/all-punches?date=2026-09-01")
        self.assertEqual(res_punches.status_code, 200)
        html_punches = res_punches.data.decode("utf-8")
        self.assertIn(f"/attendance/missing-punches/approve/{alert_id}", html_punches)
        self.assertIn("Approve Missed OUT (18:00)", html_punches)

if __name__ == "__main__":
    unittest.main()
