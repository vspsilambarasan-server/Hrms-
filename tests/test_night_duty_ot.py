import unittest
import sqlite3
from datetime import datetime
from models.shift_engine import evaluate_attendance
from routes.attendance import recompute_employee_day_attendance

class TestNightDutyOT(unittest.TestCase):
    def setUp(self):
        self.day_shift = {
            "code": "GEN",
            "name": "General Plant Shift",
            "start_time": "09:00",
            "end_time": "18:00",
            "is_overnight": 0,
            "grace_late_mins": 15,
            "grace_early_mins": 15,
            "break_mins": 60,
            "min_hours_half_day": 4.5,
            "min_hours_full_day": 8.0,
            "allowance_rate": 0.0
        }

    def test_user_case_1030pm_to_0300am(self):
        """User exact case: Night 10:30 PM IN & next day 3:00 AM OUT."""
        res = evaluate_attendance(
            "2026-09-24",
            self.day_shift,
            "2026-09-24 22:30:00",
            "2026-09-25 03:00:00"
        )
        self.assertEqual(res["work_hours"], 4.5)
        self.assertEqual(res["total_ot_hours"], 4.5)
        self.assertEqual(res["late_mins"], 0)
        self.assertEqual(res["status"], "PRESENT")

    def test_full_night_duty_1030pm_to_0530am(self):
        """Full night duty: 10:30 PM IN & next day 05:30 AM OUT (7 hours)."""
        res = evaluate_attendance(
            "2026-09-24",
            self.day_shift,
            "2026-09-24 22:30:00",
            "2026-09-25 05:30:00"
        )
        self.assertEqual(res["work_hours"], 7.0)
        self.assertEqual(res["total_ot_hours"], 7.0)
        self.assertEqual(res["late_mins"], 0)
        self.assertEqual(res["status"], "PRESENT")

    def test_night_duty_late_arrival(self):
        """Night duty arrival at 23:00 (30 mins after 22:30 schedule) to 03:00 AM."""
        res = evaluate_attendance(
            "2026-09-24",
            self.day_shift,
            "2026-09-24 23:00:00",
            "2026-09-25 03:00:00"
        )
        self.assertEqual(res["work_hours"], 4.0)
        self.assertEqual(res["late_mins"], 30)
        self.assertEqual(res["late_deduction_mins"], 30)
        # 4.0h gross OT less 30m late = 3.5h net OT
        self.assertEqual(res["total_ot_hours"], 3.5)
        self.assertEqual(res["status"], "PRESENT")

    def test_in_progress_night_checkin(self):
        """Night check-in at 22:30 with no checkout yet should not auto-close at 18:00."""
        res = evaluate_attendance(
            "2026-09-24",
            self.day_shift,
            "2026-09-24 22:30:00",
            None
        )
        self.assertEqual(res["work_hours"], 0.0)
        self.assertEqual(res["late_mins"], 0)
        self.assertEqual(res["status"], "PRESENT")

    def test_combined_day_and_night_sessions(self):
        """Employee works day shift + evening OT + night duty."""
        punches = [
            {"punch_time": "2026-09-24 09:00:00", "punch_type": "IN"},
            {"punch_time": "2026-09-24 13:00:00", "punch_type": "OUT"},
            {"punch_time": "2026-09-24 14:00:00", "punch_type": "IN"},
            {"punch_time": "2026-09-24 21:15:00", "punch_type": "OUT"}, # 3.0h Day OT
            {"punch_time": "2026-09-24 22:30:00", "punch_type": "IN"},
            {"punch_time": "2026-09-25 03:00:00", "punch_type": "OUT"}  # 4.5h Night OT
        ]
        res = evaluate_attendance(
            "2026-09-24",
            self.day_shift,
            "2026-09-24 09:00:00",
            "2026-09-25 03:00:00",
            all_punches=punches
        )
        self.assertEqual(res["status"], "PRESENT")
        # Day OT: 18:00 to 21:15 minus 15m break = 3.0h
        # Night OT: 22:30 to 03:00 = 4.5h
        # Total OT: 7.5h
        self.assertAlmostEqual(res["total_ot_hours"], 7.5, places=2)
        self.assertEqual(res["late_mins"], 0)

    def test_recompute_cross_midnight_pairing(self):
        """Test database recompute handles punches spanning across midnight."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Minimal schema setup
        cur.executescript("""
            CREATE TABLE shifts (
                id INTEGER PRIMARY KEY, code TEXT, name TEXT, start_time TEXT, end_time TEXT,
                is_overnight INTEGER DEFAULT 0, grace_late_mins INTEGER DEFAULT 15,
                grace_early_mins INTEGER DEFAULT 15, break_mins INTEGER DEFAULT 60,
                min_hours_half_day REAL DEFAULT 4.5, min_hours_full_day REAL DEFAULT 8.0,
                allowance_rate REAL DEFAULT 0.0
            );
            INSERT INTO shifts (id, code, name, start_time, end_time) VALUES (1, 'GEN', 'General', '09:00', '18:00');

            CREATE TABLE employees (
                id INTEGER PRIMARY KEY, emp_no TEXT, default_shift_id INTEGER, weekly_off_day INTEGER DEFAULT 6,
                salary_type TEXT DEFAULT 'MONTHLY', pay_frequency TEXT DEFAULT 'MONTHLY',
                base_salary REAL DEFAULT 30000.0, hourly_rate REAL DEFAULT 144.23,
                ot_hourly_rate REAL DEFAULT 144.23, shift_salary REAL DEFAULT 0.0
            );
            INSERT INTO employees (id, emp_no, default_shift_id) VALUES (1, 'EMP0001', 1);

            CREATE TABLE roster_schedules (
                id INTEGER PRIMARY KEY, employee_id INTEGER, date TEXT, shift_id INTEGER, is_off_day INTEGER DEFAULT 0
            );

            CREATE TABLE attendance_punches (
                id INTEGER PRIMARY KEY, employee_id INTEGER, punch_time TEXT, punch_type TEXT, device_id TEXT
            );

            CREATE TABLE attendance_records (
                id INTEGER PRIMARY KEY, employee_id INTEGER, date TEXT, shift_id INTEGER,
                punch_in TEXT, punch_out TEXT, work_hours REAL DEFAULT 0.0, late_mins INTEGER DEFAULT 0,
                late_deduction_mins INTEGER DEFAULT 0, early_leave_mins INTEGER DEFAULT 0,
                status TEXT DEFAULT 'ABSENT', shift_allowance_amount REAL DEFAULT 0.0,
                gross_ot_hours REAL DEFAULT 0.0, raw_ot_hours REAL DEFAULT 0.0, notes TEXT,
                UNIQUE(employee_id, date)
            );

            CREATE TABLE overtime_records (
                id INTEGER PRIMARY KEY, employee_id INTEGER, attendance_id INTEGER, date TEXT,
                ot_type TEXT, pre_shift_ot_hours REAL DEFAULT 0.0, post_shift_ot_hours REAL DEFAULT 0.0,
                total_ot_hours REAL DEFAULT 0.0, approved_hours REAL DEFAULT 0.0, multiplier REAL DEFAULT 1.0,
                calculated_ot_pay REAL DEFAULT 0.0, hourly_rate REAL DEFAULT 0.0, status TEXT,
                approved_by TEXT, approved_at TEXT, reason TEXT
            );

            CREATE TABLE company_settings (id INTEGER PRIMARY KEY, standard_day_hours REAL DEFAULT 8.0);
            INSERT INTO company_settings (id, standard_day_hours) VALUES (1, 8.0);
        """)

        # Add 10:30 PM punch on Sep 24 and 03:00 AM punch on Sep 25
        cur.execute("INSERT INTO attendance_punches (employee_id, punch_time, punch_type) VALUES (1, '2026-09-24 22:30:00', 'IN')")
        cur.execute("INSERT INTO attendance_punches (employee_id, punch_time, punch_type) VALUES (1, '2026-09-25 03:00:00', 'OUT')")
        conn.commit()

        # Recompute Sep 24
        recompute_employee_day_attendance(conn, 1, "2026-09-24")

        rec24 = cur.execute("SELECT * FROM attendance_records WHERE employee_id = 1 AND date = '2026-09-24'").fetchone()
        self.assertIsNotNone(rec24)
        self.assertEqual(rec24["punch_in"], "2026-09-24 22:30:00")
        self.assertEqual(rec24["punch_out"], "2026-09-25 03:00:00")
        self.assertEqual(rec24["work_hours"], 4.5)
        self.assertEqual(rec24["raw_ot_hours"], 4.5)
        self.assertEqual(rec24["status"], "PRESENT")

        # Verify auto-approved OT claim exists
        ot_rec = cur.execute("SELECT * FROM overtime_records WHERE employee_id = 1 AND date = '2026-09-24'").fetchone()
        self.assertIsNotNone(ot_rec)
        self.assertEqual(ot_rec["total_ot_hours"], 4.5)
        self.assertEqual(ot_rec["approved_hours"], 4.5)
        self.assertEqual(ot_rec["status"], "APPROVED")

        # Verify Sep 25 does NOT have 03:00 as punch_in
        rec25 = cur.execute("SELECT * FROM attendance_records WHERE employee_id = 1 AND date = '2026-09-25'").fetchone()
        if rec25:
            self.assertNotEqual(rec25["punch_in"], "2026-09-25 03:00:00")

        conn.close()

if __name__ == "__main__":
    unittest.main()
