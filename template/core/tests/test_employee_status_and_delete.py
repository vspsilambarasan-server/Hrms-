import unittest
import json
from datetime import date
from unittest.mock import patch
from app import create_app
from database import get_db_connection, init_db

class TestEmployeeStatusAndDelete(unittest.TestCase):

    def setUp(self):
        from models.auto_sync import stop_auto_sync
        stop_auto_sync()
        init_db()
        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()

        # Log in as admin
        self.client.post("/login", data={"username": "admin", "password": "admin123"})

        # Resolve valid department_id
        conn = get_db_connection()
        dept = conn.execute("SELECT id FROM departments LIMIT 1").fetchone()
        if not dept:
            conn.execute("INSERT INTO departments (code, name, description) VALUES ('PLANT', 'Plant Operations', 'Main facility')")
            conn.commit()
            dept = conn.execute("SELECT id FROM departments LIMIT 1").fetchone()
        self.dept_id = dept["id"]
        conn.close()

    def test_schema_migration_columns_exist(self):
        """Verify employment_status, status_date, status_reason and employee_deletion_logs table exist."""
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(employees)")
        cols = [r["name"] for r in cur.fetchall()]

        self.assertIn("employment_status", cols)
        self.assertIn("status_date", cols)
        self.assertIn("status_reason", cols)

        cur.execute("PRAGMA table_info(employee_deletion_logs)")
        del_cols = [r["name"] for r in cur.fetchall()]
        conn.close()

        self.assertIn("emp_no", del_cols)
        self.assertIn("deletion_reason", del_cols)
        self.assertIn("deleted_by", del_cols)
        self.assertIn("snapshot_json", del_cols)

    def test_status_update_to_relieved_and_reactivation(self):
        """Verify updating status to RELIEVED sets is_active=0, and restoring to ACTIVE sets is_active=1."""
        conn = get_db_connection()
        cur = conn.cursor()

        # Insert test employee
        cur.execute("""
            INSERT OR REPLACE INTO employees (
                id, emp_no, first_name, last_name, email, department_id,
                designation, join_date, salary_type, pay_frequency,
                base_salary, shift_salary, ot_hourly_rate, employment_status, is_active
            ) VALUES (
                9801, 'STAT01', 'Anand', 'Kumar', 'anand.test@press.com', ?,
                'Binder', '2026-01-01', 'WEEKLY', 'WEEKLY',
                3000.0, 500.0, 75.0, 'ACTIVE', 1
            )
        """, (self.dept_id,))
        conn.commit()

        # 1. Update status to RELIEVED
        res_relieve = self.client.post("/employees/9801/status", data={
            "employment_status": "RELIEVED",
            "status_date": "2026-09-12",
            "status_reason": "Resignation accepted with clearance"
        }, follow_redirects=True)
        self.assertEqual(res_relieve.status_code, 200)

        emp = cur.execute("SELECT * FROM employees WHERE id = 9801").fetchone()
        self.assertEqual(emp["employment_status"], "RELIEVED")
        self.assertEqual(emp["is_active"], 0)
        self.assertEqual(emp["status_date"], "2026-09-12")
        self.assertEqual(emp["status_reason"], "Resignation accepted with clearance")

        # 2. Reinstate back to ACTIVE
        res_active = self.client.post("/employees/9801/status", data={
            "employment_status": "ACTIVE",
            "status_date": "2026-09-15",
            "status_reason": "Reinstated to Active Duty"
        }, follow_redirects=True)
        self.assertEqual(res_active.status_code, 200)

        emp2 = cur.execute("SELECT * FROM employees WHERE id = 9801").fetchone()
        self.assertEqual(emp2["employment_status"], "ACTIVE")
        self.assertEqual(emp2["is_active"], 1)

        # Cleanup
        cur.execute("DELETE FROM employees WHERE id = 9801")
        conn.commit()
        conn.close()

    def test_status_update_to_suspended(self):
        """Verify setting status to SUSPENDED sets is_active=0 and stores disciplinary reason."""
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT OR REPLACE INTO employees (
                id, emp_no, first_name, last_name, email, department_id,
                designation, join_date, salary_type, pay_frequency,
                base_salary, shift_salary, ot_hourly_rate, employment_status, is_active
            ) VALUES (
                9802, 'STAT02', 'Muthu', 'Vel', 'muthu.test@press.com', ?,
                'Helper', '2026-01-01', 'WEEKLY', 'WEEKLY',
                3000.0, 500.0, 75.0, 'ACTIVE', 1
            )
        """, (self.dept_id,))
        conn.commit()

        res_suspend = self.client.post("/employees/9802/status", data={
            "employment_status": "SUSPENDED",
            "status_date": "2026-09-12",
            "status_reason": "Disciplinary suspension pending machinery safety inquiry"
        }, follow_redirects=True)
        self.assertEqual(res_suspend.status_code, 200)

        emp = cur.execute("SELECT * FROM employees WHERE id = 9802").fetchone()
        self.assertEqual(emp["employment_status"], "SUSPENDED")
        self.assertEqual(emp["is_active"], 0)
        self.assertIn("disciplinary", emp["status_reason"].lower())

        # Cleanup
        cur.execute("DELETE FROM employees WHERE id = 9802")
        conn.commit()
        conn.close()

    def test_status_update_to_no_call_no_show(self):
        """Verify setting status to NO_CALL_NO_SHOW sets is_active=0."""
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT OR REPLACE INTO employees (
                id, emp_no, first_name, last_name, email, department_id,
                designation, join_date, salary_type, pay_frequency,
                base_salary, shift_salary, ot_hourly_rate, employment_status, is_active
            ) VALUES (
                9803, 'STAT03', 'Karthik', 'Raja', 'karthik.test@press.com', ?,
                'Feeder', '2026-01-01', 'WEEKLY', 'WEEKLY',
                3000.0, 500.0, 75.0, 'ACTIVE', 1
            )
        """, (self.dept_id,))
        conn.commit()

        res_ncns = self.client.post("/employees/9803/status", data={
            "employment_status": "NO_CALL_NO_SHOW",
            "status_date": "2026-09-12",
            "status_reason": "Continuous absence without notice for 4 consecutive days"
        }, follow_redirects=True)
        self.assertEqual(res_ncns.status_code, 200)

        emp = cur.execute("SELECT * FROM employees WHERE id = 9803").fetchone()
        self.assertEqual(emp["employment_status"], "NO_CALL_NO_SHOW")
        self.assertEqual(emp["is_active"], 0)

        # Cleanup
        cur.execute("DELETE FROM employees WHERE id = 9803")
        conn.commit()
        conn.close()

    def test_employee_directory_status_filter_and_counts(self):
        """Verify /employees/?status=... filters directory and passes status counts."""
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT OR REPLACE INTO employees (
                id, emp_no, first_name, last_name, email, department_id,
                designation, join_date, salary_type, pay_frequency,
                base_salary, shift_salary, ot_hourly_rate, employment_status, is_active
            ) VALUES 
            (9804, 'FILT01', 'TestAct', 'One', 'act.one@press.com', ?, 'Op', '2026-01-01', 'WEEKLY', 'WEEKLY', 3000, 500, 75, 'ACTIVE', 1),
            (9805, 'FILT02', 'TestRel', 'Two', 'rel.two@press.com', ?, 'Op', '2026-01-01', 'WEEKLY', 'WEEKLY', 3000, 500, 75, 'RELIEVED', 0),
            (9806, 'FILT03', 'TestSusp', 'Three', 'susp.three@press.com', ?, 'Op', '2026-01-01', 'WEEKLY', 'WEEKLY', 3000, 500, 75, 'SUSPENDED', 0),
            (9807, 'FILT04', 'TestNcns', 'Four', 'ncns.four@press.com', ?, 'Op', '2026-01-01', 'WEEKLY', 'WEEKLY', 3000, 500, 75, 'NO_CALL_NO_SHOW', 0)
        """, (self.dept_id, self.dept_id, self.dept_id, self.dept_id))
        conn.commit()
        conn.close()

        # 0. Default view (/employees/) MUST ONLY show ACTIVE working employees
        res_default = self.client.get("/employees/")
        self.assertEqual(res_default.status_code, 200)
        self.assertIn(b"FILT01", res_default.data)
        self.assertNotIn(b"FILT02", res_default.data)
        self.assertNotIn(b"FILT03", res_default.data)
        self.assertNotIn(b"FILT04", res_default.data)

        # 1. Filter ACTIVE
        res_act = self.client.get("/employees/?status=ACTIVE")
        self.assertEqual(res_act.status_code, 200)
        self.assertIn(b"FILT01", res_act.data)
        self.assertNotIn(b"FILT02", res_act.data)

        # 2. Filter RELIEVED (redirects to dedicated separated register)
        res_rel = self.client.get("/employees/?status=RELIEVED", follow_redirects=True)
        self.assertEqual(res_rel.status_code, 200)
        self.assertIn(b"FILT02", res_rel.data)
        self.assertNotIn(b"FILT01", res_rel.data)

        # 3. Filter SUSPENDED (redirects to dedicated separated register)
        res_susp = self.client.get("/employees/?status=SUSPENDED", follow_redirects=True)
        self.assertEqual(res_susp.status_code, 200)
        self.assertIn(b"FILT03", res_susp.data)

        # 4. Filter NO_CALL_NO_SHOW (redirects to dedicated separated register)
        res_ncns = self.client.get("/employees/?status=NO_CALL_NO_SHOW", follow_redirects=True)
        self.assertEqual(res_ncns.status_code, 200)
        self.assertIn(b"FILT04", res_ncns.data)

        # Cleanup
        conn = get_db_connection()
        conn.execute("DELETE FROM employees WHERE id IN (9804, 9805, 9806, 9807)")
        conn.commit()
        conn.close()

    def test_employee_permanent_delete_with_audit_log(self):
        """Verify permanently deleting employee archives all data in employee_deletion_logs and cascades child records."""
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT OR REPLACE INTO employees (
                id, emp_no, first_name, last_name, email, department_id,
                designation, join_date, salary_type, pay_frequency,
                base_salary, shift_salary, ot_hourly_rate, employment_status, is_active
            ) VALUES (
                9808, 'DEL01', 'DeleteMe', 'Person', 'del.person@press.com', ?,
                'Packer', '2026-01-01', 'WEEKLY', 'WEEKLY',
                3000.0, 500.0, 75.0, 'ACTIVE', 1
            )
        """, (self.dept_id,))
        # Insert cascaded attendance record
        cur.execute("""
            INSERT INTO attendance_records (id, employee_id, date, status, work_hours)
            VALUES (98081, 9808, '2026-09-12', 'PRESENT', 8.0)
        """)
        conn.commit()

        # Delete via POST route with deletion reason
        res = self.client.post("/employees/9808/delete", data={
            "deletion_reason": "End of contract term and verified audit archiving"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"permanently deleted", res.data)
        self.assertIn(b"Deletion Audit Log", res.data)

        # 1. Verify active employee is gone from employees table
        emp = cur.execute("SELECT * FROM employees WHERE id = 9808").fetchone()
        self.assertIsNone(emp)

        # 2. Verify attendance record is cascaded and deleted
        att = cur.execute("SELECT * FROM attendance_records WHERE id = 98081").fetchone()
        self.assertIsNone(att)

        # 3. Verify permanent audit log row is created with all details
        log_row = cur.execute("SELECT * FROM employee_deletion_logs WHERE emp_no = 'DEL01'").fetchone()
        self.assertIsNotNone(log_row)
        self.assertEqual(log_row["first_name"], "DeleteMe")
        self.assertEqual(log_row["last_name"], "Person")
        self.assertEqual(log_row["designation"], "Packer")
        self.assertEqual(log_row["shift_salary"], 500.0)
        self.assertEqual(log_row["ot_hourly_rate"], 75.0)
        self.assertEqual(log_row["deletion_reason"], "End of contract term and verified audit archiving")
        self.assertIsNotNone(log_row["snapshot_json"])

        # Check serialized snapshot contains historical attendance count
        snap = json.loads(log_row["snapshot_json"])
        self.assertEqual(snap["historical_attendance_count"], 1)

        # Cleanup log record
        cur.execute("DELETE FROM employee_deletion_logs WHERE id = ?", (log_row["id"],))
        conn.commit()
        conn.close()

    def test_separated_employees_page_and_tabs(self):
        """Verify the dedicated separate page /employees/separated loads each tab."""
        # 1. Relieved tab
        res_rel = self.client.get("/employees/separated?tab=relieved")
        self.assertEqual(res_rel.status_code, 200)
        self.assertIn(b"Relieved Staff", res_rel.data)

        # 2. Suspended tab
        res_susp = self.client.get("/employees/separated?tab=suspended")
        self.assertEqual(res_susp.status_code, 200)
        self.assertIn(b"Suspended Staff", res_susp.data)

        # 3. NCNS tab
        res_ncns = self.client.get("/employees/separated?tab=ncns")
        self.assertEqual(res_ncns.status_code, 200)
        self.assertIn(b"No Call No Show", res_ncns.data)

        # 4. Deleted log tab
        res_del = self.client.get("/employees/separated?tab=deleted")
        self.assertEqual(res_del.status_code, 200)
        self.assertIn(b"Deletion Audit Log", res_del.data)

    def test_deletion_log_inspector_endpoint(self):
        """Verify /employees/deletion-log/<id> JSON API endpoint."""
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO employee_deletion_logs (
                emp_no, first_name, last_name, designation, shift_salary, ot_hourly_rate,
                deletion_reason, deleted_by, snapshot_json
            ) VALUES (
                'INSPECT01', 'TestInsp', 'Person', 'Cutter', 600.0, 80.0,
                'Audit inspector test', 'admin', '{"test_key": "test_val"}'
            )
        """)
        log_id = cur.lastrowid
        conn.commit()
        conn.close()

        res = self.client.get(f"/employees/deletion-log/{log_id}")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["emp_no"], "INSPECT01")
        self.assertEqual(data["first_name"], "TestInsp")
        self.assertEqual(data["snapshot"]["test_key"], "test_val")

        # Cleanup
        conn = get_db_connection()
        conn.execute("DELETE FROM employee_deletion_logs WHERE id = ?", (log_id,))
        conn.commit()
        conn.close()

    def test_employee_directory_active_workforce_only(self):
        """Verify the main /employees/ directory strictly lists active workforce and excludes NCNS/inactive."""
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            # Insert dedicated active and NCNS test records
            cur.execute("""
                INSERT OR REPLACE INTO employees (
                    id, emp_no, first_name, last_name, email, department_id,
                    designation, join_date, salary_type, pay_frequency,
                    base_salary, shift_salary, ot_hourly_rate, employment_status, is_active
                ) VALUES 
                (9850, 'ACT_DIR_TEST', 'ActiveDir', 'Person', 'act.dir@press.com', ?, 'Operator', '2026-01-01', 'WEEKLY', 'WEEKLY', 3000, 500, 75, 'ACTIVE', 1),
                (9851, 'NCNS_DIR_TEST', 'NcnsDir', 'Person', 'ncns.dir@press.com', ?, 'Operator', '2026-01-01', 'WEEKLY', 'WEEKLY', 3000, 500, 75, 'NO_CALL_NO_SHOW', 0)
            """, (self.dept_id, self.dept_id))
            conn.commit()

            res = self.client.get("/employees/")
            self.assertEqual(res.status_code, 200)
            html = res.data.decode("utf-8")

            # Active Press Workforce pill should be present
            self.assertIn("Active Press Workforce", html)
            self.assertIn("Sync from Device", html)

            # Active employees must appear
            self.assertIn("ACT_DIR_TEST", html)

            # NCNS employees must NOT appear in the main directory
            self.assertNotIn("NCNS_DIR_TEST", html)
        finally:
            cur.execute("DELETE FROM employees WHERE id IN (9850, 9851)")
            conn.commit()
            conn.close()

    @patch("models.biometric_sync.sync_device_users")
    @patch("models.biometric_sync.sync_device_attendance")
    def test_sync_device_route_mocked(self, mock_att, mock_users):
        """Verify POST /employees/sync-device imports enrolled users and punches."""
        mock_users.return_value = {
            "success": True,
            "total_device_users": 15,
            "added_employees": 2,
            "updated_employees": 1,
            "added_list": ["DEV01", "DEV02"],
            "error": None
        }
        mock_att.return_value = {
            "success": True,
            "total_device_records": 100,
            "new_punches_recorded": 5
        }

        res = self.client.post("/employees/sync-device", data={"device_ip": "192.168.101.201"}, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        mock_users.assert_called_with(ip="192.168.101.201")
        mock_att.assert_called_with(ip="192.168.101.201", days_back=7)

        html = res.data.decode("utf-8")
        self.assertIn("Biometric sync successful", html)
        self.assertIn("Successfully enrolled 2 new active staff profile", html)

if __name__ == "__main__":
    unittest.main()
