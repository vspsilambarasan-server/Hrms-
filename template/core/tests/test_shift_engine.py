import unittest
from datetime import datetime
from models.shift_engine import get_shift_datetimes, evaluate_attendance

class TestShiftEngine(unittest.TestCase):

    def setUp(self):
        self.day_shift = {
            "code": "GEN",
            "name": "General Day Shift",
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
        self.night_shift = {
            "code": "NGT",
            "name": "Night Graveyard Shift",
            "start_time": "22:00",
            "end_time": "06:00",
            "is_overnight": 1,
            "grace_late_mins": 15,
            "grace_early_mins": 15,
            "break_mins": 30,
            "min_hours_half_day": 4.0,
            "min_hours_full_day": 7.5,
            "allowance_rate": 350.0
        }

    def test_day_shift_datetimes(self):
        s_dt, e_dt = get_shift_datetimes("2026-09-08", self.day_shift)
        self.assertEqual(s_dt.strftime("%Y-%m-%d %H:%M"), "2026-09-08 09:00")
        self.assertEqual(e_dt.strftime("%Y-%m-%d %H:%M"), "2026-09-08 18:00")

    def test_night_shift_cross_midnight(self):
        s_dt, e_dt = get_shift_datetimes("2026-09-08", self.night_shift)
        self.assertEqual(s_dt.strftime("%Y-%m-%d %H:%M"), "2026-09-08 22:00")
        self.assertEqual(e_dt.strftime("%Y-%m-%d %H:%M"), "2026-09-09 06:00")
        # Ensure difference is exactly 8 hours
        diff_hours = (e_dt - s_dt).total_seconds() / 3600.0
        self.assertEqual(diff_hours, 8.0)

    def test_on_time_attendance(self):
        res = evaluate_attendance("2026-09-08", self.day_shift, "2026-09-08 08:58:00", "2026-09-08 18:02:00")
        self.assertEqual(res["status"], "PRESENT")
        self.assertEqual(res["late_mins"], 0)
        self.assertEqual(res["early_leave_mins"], 0)
        self.assertAlmostEqual(res["work_hours"], 8.07, delta=0.1)

    def test_late_arrival_beyond_grace(self):
        # 09:25 is 25 mins late (grace is 15 mins)
        res = evaluate_attendance("2026-09-08", self.day_shift, "2026-09-08 09:25:00", "2026-09-08 18:30:00")
        self.assertEqual(res["status"], "LATE")
        self.assertEqual(res["late_mins"], 25)

    def test_night_shift_allowance_and_ot(self):
        # Employee punches in at 21:30 (30 mins early -> pre-shift OT)
        # and punches out next day at 07:15 (75 mins late -> post-shift OT)
        res = evaluate_attendance("2026-09-08", self.night_shift, "2026-09-08 21:30:00", "2026-09-09 07:15:00")
        self.assertEqual(res["status"], "PRESENT")
        self.assertEqual(res["shift_allowance"], 350.0)
        # As per user requirement, OT is calculated strictly after shift end (06:00 to 07:15 = 1.25 hrs), eliminating pre-shift OT
        self.assertAlmostEqual(res["total_ot_hours"], 1.25, places=2)

    def test_absent_when_no_punches(self):
        res = evaluate_attendance("2026-09-08", self.day_shift, None, None)
        self.assertEqual(res["status"], "ABSENT")
        self.assertEqual(res["work_hours"], 0.0)

if __name__ == "__main__":
    unittest.main()
