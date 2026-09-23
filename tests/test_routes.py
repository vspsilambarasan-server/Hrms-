import unittest
from app import create_app
from database import get_db_connection

class TestFlaskRoutes(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_dashboard_route(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Payroll Dashboard", res.data)
        self.assertIn(b"Total Workforce", res.data)

    def test_employees_routes(self):
        res = self.client.get("/employees/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Compensation Directory", res.data)

        # View specific employee dynamically from DB
        conn = get_db_connection()
        emp_row = conn.execute("SELECT id FROM employees LIMIT 1").fetchone()
        conn.close()

        if emp_row:
            res_emp = self.client.get(f"/employees/{emp_row['id']}")
            self.assertEqual(res_emp.status_code, 200)
            self.assertIn(b"Shift Attendance Logs", res_emp.data)

    def test_shifts_and_roster_routes(self):
        res_shifts = self.client.get("/shifts/")
        self.assertEqual(res_shifts.status_code, 200)
        self.assertIn(b"Operational Shifts", res_shifts.data)

        res_roster = self.client.get("/shifts/roster")
        self.assertEqual(res_roster.status_code, 200)
        self.assertIn(b"Roster Scheduler", res_roster.data)

    def test_attendance_route(self):
        res = self.client.get("/attendance/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Shift Attendance", res.data)
        self.assertIn(b"Single Day View", res.data)

        # Test In & Out Time Card Register
        res_report = self.client.get("/attendance/in-out-report")
        self.assertEqual(res_report.status_code, 200)
        self.assertIn(b"Daily In & Out Register", res_report.data)

        # Test Monthly Matrix
        res_matrix = self.client.get("/attendance/matrix")
        self.assertEqual(res_matrix.status_code, 200)
        self.assertIn(b"Monthly Attendance", res_matrix.data)

        # Test All Punches per Person view
        res_all_punches = self.client.get("/attendance/all-punches")
        self.assertEqual(res_all_punches.status_code, 200)
        self.assertIn(b"Daily All Punches Register", res_all_punches.data)

        # Test Export All Punches CSV
        res_punches_csv = self.client.get("/attendance/export-all-punches-csv")
        self.assertEqual(res_punches_csv.status_code, 200)
        self.assertEqual(res_punches_csv.content_type, "text/csv; charset=utf-8")
        self.assertIn(b"Exact Timestamp,Employee ID", res_punches_csv.data)

    def test_all_punches_range_filters(self):
        # 1. Today filter
        res_today = self.client.get("/attendance/all-punches?range=today")
        self.assertEqual(res_today.status_code, 200)
        self.assertIn(b"Today", res_today.data)

        # 2. 7 Days filter
        res_7days = self.client.get("/attendance/all-punches?range=7days")
        self.assertEqual(res_7days.status_code, 200)
        self.assertIn(b"7 Days", res_7days.data)
        self.assertIn(b"Last 7 Days", res_7days.data)

        # 3. Last Week filter
        res_lastweek = self.client.get("/attendance/all-punches?range=lastweek")
        self.assertEqual(res_lastweek.status_code, 200)
        self.assertIn(b"Last Week", res_lastweek.data)

        # 4. Last Week alias ('lastweak' from user request)
        res_lastweak = self.client.get("/attendance/all-punches?range=lastweak")
        self.assertEqual(res_lastweak.status_code, 200)
        self.assertIn(b"Last Week", res_lastweak.data)

        # 5. Last Month filter
        res_lastmonth = self.client.get("/attendance/all-punches?range=lastmonth")
        self.assertEqual(res_lastmonth.status_code, 200)
        self.assertIn(b"Last Month", res_lastmonth.data)

        # 6. CSV Export with range=7days
        res_csv_7days = self.client.get("/attendance/export-all-punches-csv?range=7days")
        self.assertEqual(res_csv_7days.status_code, 200)
        self.assertEqual(res_csv_7days.content_type, "text/csv; charset=utf-8")
        self.assertIn(b"Exact Timestamp,Employee ID", res_csv_7days.data)
        self.assertIn("attachment; filename=Vasantham_Printers_All_Punches_", res_csv_7days.headers.get("Content-Disposition", ""))

        # 7. CSV Export with range=lastmonth
        res_csv_lastmonth = self.client.get("/attendance/export-all-punches-csv?range=lastmonth")
        self.assertEqual(res_csv_lastmonth.status_code, 200)
        self.assertIn("attachment; filename=Vasantham_Printers_All_Punches_", res_csv_lastmonth.headers.get("Content-Disposition", ""))

    def test_overtime_route(self):
        res = self.client.get("/overtime/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Overtime", res.data)

    def test_payroll_and_payslip_routes(self):
        res = self.client.get("/payroll/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Monthly Payroll", res.data)

        conn = get_db_connection()
        run_row = conn.execute("SELECT id FROM payroll_runs LIMIT 1").fetchone()
        slip_row = conn.execute("SELECT id FROM payslips LIMIT 1").fetchone()
        conn.close()

        if run_row:
            res_run = self.client.get(f"/payroll/run/{run_row['id']}")
            self.assertEqual(res_run.status_code, 200)
            self.assertIn(b"Payroll Register", res_run.data)

        if slip_row:
            res_slip = self.client.get(f"/payroll/payslip/{slip_row['id']}")
            self.assertEqual(res_slip.status_code, 200)
            self.assertIn(b"Pay Advice / Payslip", res_slip.data)

    def test_api_punch_now(self):
        conn = get_db_connection()
        dept = conn.execute("SELECT id FROM departments LIMIT 1").fetchone()
        dept_id = dept["id"] if dept else 1
        shift = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()
        shift_id = shift["id"] if shift else 1
        conn.execute("""
            INSERT OR REPLACE INTO employees (id, emp_no, first_name, last_name, email, department_id, designation, join_date, default_shift_id)
            VALUES (99999, 'TEST-API-PUNCH', 'Test', 'Punch', 'testpunch@test.com', ?, 'Tester', '2026-01-01', ?)
        """, (dept_id, shift_id))
        conn.commit()
        conn.close()

        res = self.client.post("/api/punch-now", json={"employee_id": 99999, "punch_type": "IN"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("Recorded Punch IN", data["message"])

        conn = get_db_connection()
        conn.execute("DELETE FROM attendance_punches WHERE employee_id = 99999 OR device_id != '192.168.101.201'")
        conn.execute("DELETE FROM attendance_records WHERE employee_id = 99999 OR notes = 'Live Web Punch'")
        conn.execute("DELETE FROM employees WHERE id = 99999")
        conn.commit()
        conn.close()

if __name__ == "__main__":
    unittest.main()
