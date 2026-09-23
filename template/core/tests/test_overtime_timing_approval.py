import unittest
from datetime import datetime, date
from app import create_app
from database import get_db_connection

class TestOvertimeTimingApproval(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Fetch active test employee
        conn = get_db_connection()
        emp = conn.execute("SELECT id, emp_no, first_name, last_name FROM employees WHERE is_active = 1 LIMIT 1").fetchone()
        self.emp_id = emp["id"]
        self.test_date = "2026-10-15"

        # Cleanup test entries
        conn.execute("DELETE FROM overtime_records WHERE employee_id = ? AND date = ?", (self.emp_id, self.test_date))
        conn.execute("DELETE FROM attendance_punches WHERE employee_id = ? AND punch_time LIKE ?", (self.emp_id, f"{self.test_date}%"))
        conn.execute("DELETE FROM attendance_records WHERE employee_id = ? AND date = ?", (self.emp_id, self.test_date))
        conn.commit()
        conn.close()

    def tearDown(self):
        conn = get_db_connection()
        conn.execute("DELETE FROM overtime_records WHERE employee_id = ? AND date = ?", (self.emp_id, self.test_date))
        conn.execute("DELETE FROM attendance_punches WHERE employee_id = ? AND punch_time LIKE ?", (self.emp_id, f"{self.test_date}%"))
        conn.execute("DELETE FROM attendance_records WHERE employee_id = ? AND date = ?", (self.emp_id, self.test_date))
        conn.commit()
        conn.close()

    def test_overtime_list_renders_punch_timings_and_date(self):
        """
        Verify that /overtime/ displays actual Punch IN, Punch OUT, Date with weekday,
        shift comparison, and verification controls.
        """
        # Create test punches and attendance record
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
            VALUES (?, ?, 'IN', 'TEST_DEVICE')
        """, (self.emp_id, f"{self.test_date} 08:45:00"))
        cur.execute("""
            INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
            VALUES (?, ?, 'OUT', 'TEST_DEVICE')
        """, (self.emp_id, f"{self.test_date} 21:15:00"))
        
        cur.execute("""
            INSERT INTO attendance_records (employee_id, date, punch_in, punch_out, work_hours, status)
            VALUES (?, ?, ?, ?, 11.5, 'PRESENT')
        """, (self.emp_id, self.test_date, f"{self.test_date} 08:45:00", f"{self.test_date} 21:15:00"))
        att_id = cur.lastrowid

        cur.execute("""
            INSERT INTO overtime_records (employee_id, attendance_id, date, ot_type, pre_shift_ot_hours, post_shift_ot_hours, total_ot_hours, multiplier, status, calculated_ot_pay)
            VALUES (?, ?, ?, 'NORMAL', 0.25, 3.25, 3.5, 1.5, 'PENDING', 350.0)
        """, (self.emp_id, att_id, self.test_date))
        conn.commit()
        conn.close()

        res = self.client.get("/overtime/")
        self.assertEqual(res.status_code, 200)

        # Assert presence of Timing Verification headers and columns
        self.assertIn(b"Actual Punch Timings (IN / OUT)", res.data)
        self.assertIn(b"Timing Verification Protocol", res.data)
        self.assertIn(b"Date & Day", res.data)
        self.assertIn(b"Scheduled Shift", res.data)
        self.assertIn(b"Work & OT Hours", res.data)
        self.assertIn(b"Verify & Review", res.data)

        # Assert punch timestamps rendered
        self.assertIn(b"08:45:00", res.data)
        self.assertIn(b"21:15:00", res.data)
        self.assertIn(b"IN:", res.data)
        self.assertIn(b"OUT:", res.data)

        # Assert timing verification modal elements
        self.assertIn(b"reviewPunchInTime", res.data)
        self.assertIn(b"reviewPunchOutTime", res.data)
        self.assertIn(b"reviewTimecardLink", res.data)
        self.assertIn(b"otSearchInput", res.data)

    def test_overtime_filter_by_date(self):
        """
        Verify that filtering by date isolates OT records for that specific date.
        """
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO overtime_records (employee_id, date, ot_type, total_ot_hours, multiplier, status, calculated_ot_pay)
            VALUES (?, ?, 'NORMAL', 2.0, 1.5, 'PENDING', 200.0)
        """, (self.emp_id, self.test_date))
        conn.commit()
        conn.close()

        res = self.client.get(f"/overtime/?date={self.test_date}")
        self.assertEqual(res.status_code, 200)
        self.assertIn(self.test_date.encode("utf-8"), res.data)

    def test_review_action_approve_with_remarks(self):
        """
        Verify supervisor approval updates approved hours and audit remarks.
        """
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO overtime_records (employee_id, date, ot_type, total_ot_hours, multiplier, status, calculated_ot_pay)
            VALUES (?, ?, 'NORMAL', 3.0, 1.5, 'PENDING', 300.0)
        """, (self.emp_id, self.test_date))
        ot_id = cur.lastrowid
        conn.commit()
        conn.close()

        res = self.client.post(f"/overtime/{ot_id}/action", data={
            "action": "APPROVE",
            "approved_hours": "2.5",
            "remarks": "Verified late checkout on Heidelberg offset press"
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute("SELECT * FROM overtime_records WHERE id = ?", (ot_id,)).fetchone()
        self.assertEqual(row["status"], "APPROVED")
        self.assertEqual(row["approved_hours"], 2.5)
        self.assertEqual(row["reason"], "Verified late checkout on Heidelberg offset press")
        conn.close()

    def test_review_action_reject_with_remarks(self):
        """
        Verify supervisor rejection marks record REJECTED with remarks.
        """
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO overtime_records (employee_id, date, ot_type, total_ot_hours, multiplier, status, calculated_ot_pay)
            VALUES (?, ?, 'NORMAL', 2.0, 1.5, 'PENDING', 200.0)
        """, (self.emp_id, self.test_date))
        ot_id = cur.lastrowid
        conn.commit()
        conn.close()

        res = self.client.post(f"/overtime/{ot_id}/action", data={
            "action": "REJECT",
            "remarks": "Unauthorized stay without prior work order"
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        row = conn.execute("SELECT * FROM overtime_records WHERE id = ?", (ot_id,)).fetchone()
        self.assertEqual(row["status"], "REJECTED")
        self.assertEqual(row["approved_hours"], 0.0)
        self.assertEqual(row["reason"], "Unauthorized stay without prior work order")
        conn.close()

if __name__ == "__main__":
    unittest.main()
