import unittest
import time
from datetime import datetime, date
from app import create_app
from database import get_db_connection

class TestAuditAndManualPunch(unittest.TestCase):

    def setUp(self):
        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()

        # Fetch a test employee
        conn = get_db_connection()
        emp = conn.execute("SELECT id, emp_no, first_name, last_name FROM employees WHERE is_active = 1 LIMIT 1").fetchone()
        conn.close()
        self.assertIsNotNone(emp, "Active employee required for test")
        self.emp_id = emp["id"]
        # Use a distinct test date
        self.test_date = "2026-09-30"

        # Clean any existing test punches for this date
        conn = get_db_connection()
        conn.execute("DELETE FROM attendance_punches WHERE employee_id = ? AND punch_time LIKE ?", (self.emp_id, f"{self.test_date}%"))
        conn.execute("DELETE FROM attendance_records WHERE employee_id = ? AND date = ?", (self.emp_id, self.test_date))
        conn.execute("DELETE FROM punch_edit_logs WHERE employee_id = ? AND (new_value LIKE ? OR old_value LIKE ?)", (self.emp_id, f"{self.test_date}%", f"{self.test_date}%"))
        conn.execute("DELETE FROM overtime_records WHERE date = ?", (self.test_date,))
        conn.commit()
        conn.close()

    def tearDown(self):
        # Cleanup test entries
        conn = get_db_connection()
        conn.execute("DELETE FROM attendance_punches WHERE employee_id = ? AND punch_time LIKE ?", (self.emp_id, f"{self.test_date}%"))
        conn.execute("DELETE FROM attendance_records WHERE employee_id = ? AND date = ?", (self.emp_id, self.test_date))
        conn.execute("DELETE FROM punch_edit_logs WHERE employee_id = ? AND (new_value LIKE ? OR old_value LIKE ?)", (self.emp_id, f"{self.test_date}%", f"{self.test_date}%"))
        conn.execute("DELETE FROM overtime_records WHERE date = ?", (self.test_date,))
        conn.commit()
        conn.close()

    def test_manual_punch_in_and_audit(self):
        reason = "Employee card sensor failed at entry gate"
        editor = "Supervisor Murugan"
        time_str = "09:05:00"

        res = self.client.post("/attendance/manual-punch", data={
            "employee_id": self.emp_id,
            "punch_type": "IN",
            "date": self.test_date,
            "time": time_str,
            "reason": reason,
            "edited_by": editor
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        # Verify in attendance_punches
        conn = get_db_connection()
        p = conn.execute("""
            SELECT * FROM attendance_punches 
            WHERE employee_id = ? AND punch_time = ?
        """, (self.emp_id, f"{self.test_date} {time_str}")).fetchone()
        self.assertIsNotNone(p, "Punch was not inserted")
        self.assertEqual(p["punch_type"], "IN")

        # Verify in punch_edit_logs
        log = conn.execute("""
            SELECT * FROM punch_edit_logs
            WHERE punch_id = ? AND employee_id = ?
        """, (p["id"], self.emp_id)).fetchone()
        self.assertIsNotNone(log, "Audit log entry was not created")
        self.assertEqual(log["edit_type"], "MANUAL_IN")
        self.assertEqual(log["new_value"], f"{self.test_date} {time_str}")
        self.assertEqual(log["reason"], reason)
        self.assertEqual(log["edited_by"], editor)

        # Verify attendance_records recomputed
        att = conn.execute("""
            SELECT * FROM attendance_records
            WHERE employee_id = ? AND date = ?
        """, (self.emp_id, self.test_date)).fetchone()
        self.assertIsNotNone(att, "Daily attendance record was not recomputed")
        self.assertIsNotNone(att["punch_in"])
        conn.close()

    def test_manual_punch_out_and_audit(self):
        reason = "Shift checkout missed due to power fluctuation"
        editor = "HR Manager"
        time_str = "18:15:00"

        res = self.client.post("/attendance/manual-punch", data={
            "employee_id": self.emp_id,
            "punch_type": "OUT",
            "date": self.test_date,
            "time": time_str,
            "reason": reason,
            "edited_by": editor
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        conn = get_db_connection()
        p = conn.execute("""
            SELECT * FROM attendance_punches 
            WHERE employee_id = ? AND punch_time = ?
        """, (self.emp_id, f"{self.test_date} {time_str}")).fetchone()
        self.assertIsNotNone(p)
        self.assertEqual(p["punch_type"], "OUT")

        log = conn.execute("""
            SELECT * FROM punch_edit_logs
            WHERE punch_id = ? AND employee_id = ?
        """, (p["id"], self.emp_id)).fetchone()
        self.assertIsNotNone(log)
        self.assertEqual(log["edit_type"], "MANUAL_OUT")
        self.assertEqual(log["reason"], reason)
        conn.close()

    def test_manual_punch_requires_reason(self):
        res = self.client.post("/attendance/manual-punch", data={
            "employee_id": self.emp_id,
            "punch_type": "IN",
            "date": self.test_date,
            "time": "08:55:00",
            "reason": "" # Empty reason
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Mandatory Audit Requirement", res.data)

    def test_edit_punch_with_audit_trail(self):
        # Insert a punch to edit
        conn = get_db_connection()
        cur = conn.cursor()
        orig_time = f"{self.test_date} 09:20:00"
        cur.execute("""
            INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
            VALUES (?, ?, 'IN', 'TEST_DEVICE')
        """, (self.emp_id, orig_time))
        conn.commit()
        punch_id = cur.lastrowid
        conn.close()

        # Edit punch
        new_time = f"{self.test_date} 09:02:00"
        edit_reason = "Correction: Bus arrived on time, terminal queue was backed up"
        editor = "Plant Supervisor"

        res = self.client.post(f"/attendance/edit-punch/{punch_id}", data={
            "new_time": new_time,
            "punch_type": "IN",
            "reason": edit_reason,
            "edited_by": editor
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        # Check punch updated
        conn = get_db_connection()
        updated_p = conn.execute("SELECT * FROM attendance_punches WHERE id = ?", (punch_id,)).fetchone()
        self.assertEqual(updated_p["punch_time"], new_time)

        # Check audit log entry
        audit_entry = conn.execute("""
            SELECT * FROM punch_edit_logs 
            WHERE punch_id = ? AND edit_type = 'PUNCH_EDIT'
            ORDER BY id DESC LIMIT 1
        """, (punch_id,)).fetchone()
        self.assertIsNotNone(audit_entry)
        self.assertEqual(audit_entry["old_value"], orig_time)
        self.assertEqual(audit_entry["new_value"], new_time)
        self.assertEqual(audit_entry["reason"], edit_reason)
        self.assertEqual(audit_entry["edited_by"], editor)
        conn.close()

    def test_delete_punch_requires_reason(self):
        # Insert a punch
        conn = get_db_connection()
        cur = conn.cursor()
        punch_time = f"{self.test_date} 12:30:00"
        cur.execute("""
            INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
            VALUES (?, ?, 'PUNCH', 'TEST_DEVICE')
        """, (self.emp_id, punch_time))
        conn.commit()
        punch_id = cur.lastrowid
        conn.close()

        # Attempt to delete without reason
        res = self.client.post(f"/attendance/delete-punch/{punch_id}", data={
            "reason": "",
            "edited_by": "Supervisor"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Mandatory Audit Requirement", res.data)

        # Verify punch still exists in DB
        conn = get_db_connection()
        check = conn.execute("SELECT * FROM attendance_punches WHERE id = ?", (punch_id,)).fetchone()
        self.assertIsNotNone(check, "Punch should NOT be deleted without a reason!")
        conn.close()

    def test_delete_punch_success_and_audit_trail(self):
        # Insert a punch to delete
        conn = get_db_connection()
        cur = conn.cursor()
        punch_time = f"{self.test_date} 13:45:00"
        cur.execute("""
            INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
            VALUES (?, ?, 'OUT', 'TEST_DEVICE')
        """, (self.emp_id, punch_time))
        conn.commit()
        punch_id = cur.lastrowid
        conn.close()

        del_reason = "Accidental duplicate card scan at gate terminal"
        author = "Security Officer Raman"

        res = self.client.post(f"/attendance/delete-punch/{punch_id}", data={
            "reason": del_reason,
            "edited_by": author
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"permanently deleted", res.data)

        # Verify punch was removed from attendance_punches
        conn = get_db_connection()
        check = conn.execute("SELECT * FROM attendance_punches WHERE id = ?", (punch_id,)).fetchone()
        self.assertIsNone(check, "Punch should have been deleted from attendance_punches!")

        # Verify deletion logged in punch_edit_logs
        log = conn.execute("""
            SELECT * FROM punch_edit_logs
            WHERE punch_id = ? AND edit_type = 'PUNCH_DELETE'
            ORDER BY id DESC LIMIT 1
        """, (punch_id,)).fetchone()
        self.assertIsNotNone(log, "Punch deletion MUST be recorded in punch_edit_logs!")
        self.assertEqual(log["target_field"], "punch_record")
        self.assertIn(punch_time, log["old_value"])
        self.assertEqual(log["new_value"], "DELETED")
        self.assertEqual(log["reason"], del_reason)
        self.assertEqual(log["edited_by"], author)
        conn.close()

    def test_delete_punch_recomputes_attendance(self):
        # Insert two punches (IN 09:00, OUT 18:00)
        conn = get_db_connection()
        cur = conn.cursor()
        p1_time = f"{self.test_date} 09:00:00"
        p2_time = f"{self.test_date} 18:00:00"
        cur.execute("INSERT INTO attendance_punches (employee_id, punch_time, punch_type) VALUES (?, ?, 'IN')", (self.emp_id, p1_time))
        cur.execute("INSERT INTO attendance_punches (employee_id, punch_time, punch_type) VALUES (?, ?, 'OUT')", (self.emp_id, p2_time))
        conn.commit()
        p2_id = cur.lastrowid

        # Recompute day
        from routes.attendance import recompute_employee_day_attendance
        recompute_employee_day_attendance(conn, self.emp_id, self.test_date)

        rec = conn.execute("SELECT * FROM attendance_records WHERE employee_id = ? AND date = ?", (self.emp_id, self.test_date)).fetchone()
        self.assertIsNotNone(rec)
        self.assertGreater(rec["work_hours"], 0.0)
        conn.close()

        # Now delete punch 2 (OUT punch)
        res = self.client.post(f"/attendance/delete-punch/{p2_id}", data={
            "reason": "Accidental phantom punch after shift",
            "edited_by": "Shift Lead"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # Verify attendance recomputed (only 1 punch remaining, so work_hours adjusted)
        conn = get_db_connection()
        updated_rec = conn.execute("SELECT * FROM attendance_records WHERE employee_id = ? AND date = ?", (self.emp_id, self.test_date)).fetchone()
        self.assertIsNotNone(updated_rec)
        self.assertEqual(updated_rec["punch_in"], p1_time)
        conn.close()

    def test_status_filters_present_leave_absent(self):
        # Test index route with status filters
        for st in ["ALL", "PRESENT", "LEAVE", "ABSENT", "LATE"]:
            res = self.client.get(f"/attendance/?date={self.test_date}&status={st}")
            self.assertEqual(res.status_code, 200)

        # Test in_out_report route with status filters
        for st in ["PRESENT", "LEAVE", "ABSENT"]:
            res_rep = self.client.get(f"/attendance/in-out-report?status={st}")
            self.assertEqual(res_rep.status_code, 200)

    def test_audit_log_view_and_csv_export(self):
        # View Audit Log HTML
        res = self.client.get("/attendance/audit-log")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Attendance & Punch Edit Audit Trail", res.data)
        self.assertIn(b"Audit Trail Policy", res.data)
        self.assertIn(b"Deleted Punches", res.data)

        # View Audit Log CSV
        res_csv = self.client.get("/attendance/export-audit-log-csv")
        self.assertEqual(res_csv.status_code, 200)
        self.assertEqual(res_csv.content_type, "text/csv; charset=utf-8")
        self.assertIn(b"Audit Log ID,Edit Timestamp,Employee ID,Employee Name", res_csv.data)

    def test_unlimited_manual_punches_for_person_with_collision_handling(self):
        """
        Verify that an employee has strictly NO LIMIT on manual punch entries per day,
        same-second timestamp collisions are automatically resolved with second offsets,
        and all punches generate immutable audit logs.
        """
        punch_specs = [
            ("IN", "08:30:00", "Morning shift start"),
            ("OUT", "11:00:00", "Tea break start"),
            ("IN", "11:15:00", "Tea break return"),
            ("OUT", "13:00:00", "Lunch departure"),
            ("OUT", "13:00:00", "Immediate re-punch collision test"),
            ("IN", "14:00:00", "Lunch return"),
            ("OUT", "18:00:00", "Standard shift checkout"),
            ("IN", "19:00:00", "Overtime shift start"),
            ("OUT", "21:30:00", "Overtime completion checkout")
        ]

        for idx, (p_type, p_time, reason) in enumerate(punch_specs, start=1):
            res = self.client.post("/attendance/manual-punch", data={
                "employee_id": self.emp_id,
                "punch_type": p_type,
                "date": self.test_date,
                "time": p_time,
                "reason": f"Punch #{idx}: {reason}",
                "edited_by": "Shift Supervisor Murugan"
            }, follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            self.assertIn(f"Punch #{idx} today - No Limit".encode("utf-8"), res.data)

        # Verify all 9 punches exist in DB for this employee
        conn = get_db_connection()
        total_punches = conn.execute(
            "SELECT COUNT(*) as cnt FROM attendance_punches WHERE employee_id = ? AND punch_time LIKE ?",
            (self.emp_id, f"{self.test_date}%")
        ).fetchone()["cnt"]
        self.assertEqual(total_punches, 9, "All 9 punches must be recorded without any limit cap")

        # Verify collision was resolved by offset (13:00:00 and 13:00:01)
        collision_punches = conn.execute(
            "SELECT punch_time FROM attendance_punches WHERE employee_id = ? AND punch_time LIKE ? ORDER BY punch_time ASC",
            (self.emp_id, f"{self.test_date} 13:00:%")
        ).fetchall()
        self.assertEqual(len(collision_punches), 2, "Collision was resolved so both punches exist")
        self.assertNotEqual(collision_punches[0]["punch_time"], collision_punches[1]["punch_time"])

        # Verify 9 audit log entries exist
        audit_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM punch_edit_logs WHERE employee_id = ? AND new_value LIKE ?",
            (self.emp_id, f"{self.test_date}%")
        ).fetchone()["cnt"]
        self.assertEqual(audit_count, 9, "Every manual punch must produce an immutable audit log entry")

        # Verify daily attendance record recomputed correctly (earliest IN=08:30:00, latest OUT=21:30:00)
        att = conn.execute(
            "SELECT * FROM attendance_records WHERE employee_id = ? AND date = ?",
            (self.emp_id, self.test_date)
        ).fetchone()
        self.assertIsNotNone(att)
        self.assertEqual(att["punch_in"], f"{self.test_date} 08:30:00")
        self.assertEqual(att["punch_out"], f"{self.test_date} 21:30:00")
        self.assertGreater(att["work_hours"], 8.0)
        conn.close()

    def test_break_return_in_punch_not_shift_clock_out(self):
        """Verify that an odd-numbered return punch (e.g. Punch #7 Break IN) is NOT mislabeled as Shift Clock OUT."""
        conn = get_db_connection()
        conn.execute("DELETE FROM attendance_punches WHERE employee_id = ? AND punch_time LIKE ?", (self.emp_id, f"{self.test_date}%"))
        
        # Insert 7 punches matching Pachimuthu's sequence
        punches = [
            (f"{self.test_date} 09:00:50", "PUNCH"),
            (f"{self.test_date} 11:05:46", "PUNCH"),
            (f"{self.test_date} 11:21:22", "PUNCH"),
            (f"{self.test_date} 13:00:00", "OUT"),
            (f"{self.test_date} 14:04:25", "PUNCH"),
            (f"{self.test_date} 16:02:34", "PUNCH"),
            (f"{self.test_date} 16:15:00", "IN"),
        ]
        for t, pt in punches:
            conn.execute(
                "INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id) VALUES (?, ?, ?, 'TEST_DEV')",
                (self.emp_id, t, pt)
            )
        conn.commit()
        conn.close()

        # Fetch all_punches page
        res = self.client.get(f"/attendance/all-punches?date={self.test_date}&emp_id={self.emp_id}")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        # Punch #7 must be Break Return / Duty IN, NOT Shift Clock OUT
        self.assertIn("Break Return / Duty IN", html)
        self.assertIn("Currently On Duty", html)
        self.assertNotIn("Shift Clock OUT", html)

if __name__ == "__main__":
    unittest.main()

