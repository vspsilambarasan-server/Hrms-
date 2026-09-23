import unittest
from datetime import datetime
from models.shift_engine import evaluate_attendance, compute_daily_lateness
from models.missing_punch_detector import analyze_employee_shift_gaps
from database import get_db_connection

class TestOvertimeLatenessAndBreaks(unittest.TestCase):

    def setUp(self):
        self.day_shift = {
            "id": 1,
            "name": "General Shift",
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

    def test_six_pm_shift_end_no_punch_required(self):
        res = evaluate_attendance("2026-09-08", self.day_shift, "2026-09-08 09:00:00", None)
        self.assertEqual(res["status"], "PRESENT")
        self.assertEqual(res["work_hours"], 8.0)
        self.assertEqual(res["late_mins"], 0)
        self.assertEqual(res["gross_ot_hours"], 0.0)
        self.assertEqual(res["total_ot_hours"], 0.0)

    def test_ot_with_19_00_to_19_15_break_deduction(self):
        res = evaluate_attendance("2026-09-08", self.day_shift, "2026-09-08 09:00:00", "2026-09-08 20:00:00")
        self.assertEqual(res["work_hours"], 10.0)
        self.assertEqual(res["late_mins"], 0)
        self.assertEqual(res["gross_ot_hours"], 1.75)
        self.assertEqual(res["total_ot_hours"], 1.75)

    def test_ot_capped_during_break_window(self):
        res = evaluate_attendance("2026-09-08", self.day_shift, "2026-09-08 09:00:00", "2026-09-08 19:10:00")
        self.assertEqual(res["gross_ot_hours"], 1.0)
        self.assertEqual(res["total_ot_hours"], 1.0)

    def test_lateness_offset_from_ot(self):
        punches = [
            {"punch_time": "2026-09-08 09:20:00", "punch_type": "IN"},
            {"punch_time": "2026-09-08 13:00:00", "punch_type": "OUT"},
            {"punch_time": "2026-09-08 14:10:00", "punch_type": "IN"},
            {"punch_time": "2026-09-08 20:00:00", "punch_type": "OUT"}
        ]
        res = evaluate_attendance("2026-09-08", self.day_shift, "2026-09-08 09:20:00", "2026-09-08 20:00:00", all_punches=punches)
        self.assertEqual(res["status"], "LATE")
        self.assertEqual(res["late_mins"], 30)
        self.assertEqual(res["late_deduction_mins"], 30)
        self.assertEqual(res["gross_ot_hours"], 1.75)
        self.assertEqual(res["total_ot_hours"], 1.25)

    def test_lateness_exceeding_ot_absorbs_to_zero(self):
        punches = [
            {"punch_time": "2026-09-08 09:45:00", "punch_type": "IN"},
            {"punch_time": "2026-09-08 18:30:00", "punch_type": "OUT"}
        ]
        res = evaluate_attendance("2026-09-08", self.day_shift, "2026-09-08 09:45:00", "2026-09-08 18:30:00", all_punches=punches)
        self.assertEqual(res["late_mins"], 45)
        self.assertEqual(res["gross_ot_hours"], 0.5)
        self.assertEqual(res["total_ot_hours"], 0.0)

    def test_all_four_breaks_lateness(self):
        punches = [
            {"punch_time": "2026-09-08 09:00:00", "punch_type": "IN"},
            {"punch_time": "2026-09-08 11:00:00", "punch_type": "OUT"},
            {"punch_time": "2026-09-08 11:20:00", "punch_type": "IN"},
            {"punch_time": "2026-09-08 13:00:00", "punch_type": "OUT"},
            {"punch_time": "2026-09-08 14:10:00", "punch_type": "IN"},
            {"punch_time": "2026-09-08 16:00:00", "punch_type": "OUT"},
            {"punch_time": "2026-09-08 16:20:00", "punch_type": "IN"},
            {"punch_time": "2026-09-08 19:00:00", "punch_type": "OUT"},
            {"punch_time": "2026-09-08 19:25:00", "punch_type": "IN"},
            {"punch_time": "2026-09-08 21:00:00", "punch_type": "OUT"}
        ]
        res = evaluate_attendance("2026-09-08", self.day_shift, "2026-09-08 09:00:00", "2026-09-08 21:00:00", all_punches=punches)
        self.assertEqual(res["late_mins"], 30)
        self.assertEqual(res["gross_ot_hours"], 2.75)
        self.assertEqual(res["total_ot_hours"], 2.25)

    def test_missing_punch_detector_no_clock_out_alert_at_18_00(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM employees WHERE is_active = 1 AND is_admin = 0 LIMIT 1")
        emp = cursor.fetchone()
        if emp:
            emp_id = emp["id"]
            gaps = analyze_employee_shift_gaps(emp_id, "2026-09-08", conn=conn)
            gap_types = [g["gap_type"] for g in gaps]
            self.assertNotIn("MISSING_CLOCK_OUT", gap_types)
        conn.close()

if __name__ == '__main__':
    unittest.main()
