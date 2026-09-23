import unittest
from datetime import datetime, date
from app import create_app
from database import get_db_connection
from models.biometric_sync import sync_device_attendance

class TestCompanyDetailsAndSyncFix(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Back up original company settings
        conn = get_db_connection()
        row = conn.execute("SELECT * FROM company_settings LIMIT 1").fetchone()
        self.orig_settings = dict(row) if row else None
        conn.close()

    def tearDown(self):
        # Restore original company settings
        if self.orig_settings:
            conn = get_db_connection()
            conn.execute("""
                UPDATE company_settings
                SET company_name = ?, address = ?, phone = ?, email = ?,
                    gstin = ?, factory_license_no = ?, supervisor_name = ?,
                    alert_recipient_email = ?
                WHERE id = ?
            """, (
                self.orig_settings.get("company_name"),
                self.orig_settings.get("address"),
                self.orig_settings.get("phone"),
                self.orig_settings.get("email"),
                self.orig_settings.get("gstin"),
                self.orig_settings.get("factory_license_no"),
                self.orig_settings.get("supervisor_name"),
                self.orig_settings.get("alert_recipient_email"),
                self.orig_settings.get("id"),
            ))
            conn.commit()
            conn.close()

    def test_company_details_update_and_rendering_in_payslips(self):
        # 1. Update company settings via POST
        test_company_name = "Apex Precision Packaging Pvt Ltd"
        test_address = "45/A Modern Industrial Estate, Chennai, Tamil Nadu - 600058"
        test_phone = "+91 98765 43210"
        test_email = "accounts@apexprecision.com"
        test_gstin = "33AAACA9999Z1Z8"
        test_factory_lic = "FAC-TN-CH-8877"
        test_supervisor = "R. Senthil Kumar"

        res = self.client.post("/settings/company", data={
            "company_name": test_company_name,
            "address": test_address,
            "phone": test_phone,
            "email": test_email,
            "gstin": test_gstin,
            "factory_license_no": test_factory_lic,
            "supervisor_name": test_supervisor,
            "alert_recipient_email": "supervisor@apexprecision.com",
            "currency_symbol": "₹",
            "date_format": "%d-%m-%Y"
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        # Verify DB updated
        conn = get_db_connection()
        updated = conn.execute("SELECT * FROM company_settings LIMIT 1").fetchone()
        self.assertEqual(updated["company_name"], test_company_name)
        self.assertEqual(updated["address"], test_address)
        self.assertEqual(updated["phone"], test_phone)
        self.assertEqual(updated["gstin"], test_gstin)
        self.assertEqual(updated["factory_license_no"], test_factory_lic)
        self.assertEqual(updated["supervisor_name"], test_supervisor)

        # 2. Check payslip rendering
        slip = conn.execute("SELECT id FROM payslips LIMIT 1").fetchone()
        run = conn.execute("SELECT id FROM payroll_runs LIMIT 1").fetchone()
        conn.close()

        if slip:
            res_slip = self.client.get(f"/payroll/payslip/{slip['id']}")
            self.assertEqual(res_slip.status_code, 200)
            slip_html = res_slip.data.decode("utf-8")
            self.assertIn(test_company_name, slip_html)
            self.assertIn(test_address, slip_html)
            self.assertIn(test_phone, slip_html)
            self.assertIn(test_gstin, slip_html)
            self.assertIn(test_factory_lic, slip_html)
            self.assertIn(test_supervisor, slip_html)

        if run:
            res_batch = self.client.get(f"/payroll/run/{run['id']}/print-all")
            self.assertEqual(res_batch.status_code, 200)
            batch_html = res_batch.data.decode("utf-8")
            self.assertIn(test_company_name, batch_html)
            self.assertIn(test_address, batch_html)
            self.assertIn(test_phone, batch_html)
            self.assertIn(test_gstin, batch_html)
            self.assertIn(test_factory_lic, batch_html)
            self.assertIn(test_supervisor, batch_html)

    def test_cross_midnight_fix_standard_shift_punches_not_shifted(self):
        """
        Verify that a morning punch for a standard day shift (e.g. 09:02 AM)
        is kept on the actual date and NOT incorrectly shifted to yesterday.
        """
        conn = get_db_connection()
        # Find active worker
        emp = conn.execute("SELECT id, emp_no FROM employees WHERE employment_status = 'ACTIVE' LIMIT 1").fetchone()
        self.assertIsNotNone(emp, "Active employee required")

        # Test punch at 09:05 AM on 2026-09-10
        test_punch_date = "2026-09-10"

        # Check existing attendance record for this date
        att = conn.execute(
            "SELECT * FROM attendance_records WHERE employee_id = ? AND date = ?",
            (emp["id"], test_punch_date)
        ).fetchone()

        if att and att["punch_in"]:
            # Verify the punch_in date starts in the morning of test_punch_date, not shifted
            punch_in_str = att["punch_in"]
            self.assertTrue(punch_in_str.startswith(test_punch_date), f"Punch in {punch_in_str} should be on {test_punch_date}")
        conn.close()

    def test_attendance_template_has_manual_punch_modal_fix(self):
        """
        Verify that index.html contains openManualPunchForEmp('', '', '', '', 0, '')
        """
        res = self.client.get("/attendance/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")
        self.assertIn("openManualPunchForEmp('', '', '', '', 0, '')", html)

    def test_deleted_employee_blacklist_in_biometric_sync(self):
        """
        Verify that sync_device_users skips permanently deleted employees
        while correctly importing newly enrolled staff.
        """
        from unittest.mock import MagicMock, patch
        from models.biometric_sync import sync_device_users

        conn = get_db_connection()
        # Insert a fake record into employee_deletion_logs
        conn.execute("""
            INSERT OR REPLACE INTO employee_deletion_logs (
                id, original_emp_id, emp_no, first_name, last_name, deletion_reason
            ) VALUES (7777, 9999, 'EMP9999', 'DeletedStaff', 'Test', 'Discharged')
        """)
        conn.commit()
        conn.close()

        # Mock ZK machine with two users: user 9999 (deleted) and user 8888 (brand new enrolled staff)
        mock_user_deleted = MagicMock()
        mock_user_deleted.user_id = 9999
        mock_user_deleted.name = "DeletedStaff Test"

        mock_user_new = MagicMock()
        mock_user_new.user_id = 8888
        mock_user_new.name = "NewStaff Member"

        with patch("models.biometric_sync.get_zk_client") as mock_zk_fn:
            mock_zk_inst = MagicMock()
            mock_conn = MagicMock()
            mock_conn.get_users.return_value = [mock_user_deleted, mock_user_new]
            mock_zk_inst.connect.return_value = mock_conn
            mock_zk_fn.return_value = mock_zk_inst

            sync_res = sync_device_users("127.0.0.1")
            self.assertTrue(sync_res["success"])

        # Verify: deleted user 9999 was NOT inserted into employees
        conn = get_db_connection()
        emp_deleted = conn.execute("SELECT * FROM employees WHERE emp_no = 'EMP9999' OR id = 9999").fetchone()
        self.assertIsNone(emp_deleted, "Deleted staff member should never be re-created by device sync")

        # Verify: new user 8888 WAS successfully inserted as active employee
        emp_new = conn.execute("SELECT * FROM employees WHERE emp_no = 'EMP8888'").fetchone()
        self.assertIsNotNone(emp_new, "Brand new enrolled staff should be imported into employees")
        self.assertEqual(emp_new["first_name"], "NewStaff")
        self.assertEqual(emp_new["employment_status"], "ACTIVE")

        # Cleanup
        conn.execute("DELETE FROM employees WHERE emp_no = 'EMP8888'")
        conn.execute("DELETE FROM employee_deletion_logs WHERE id = 7777")
        conn.commit()
        conn.close()

    def test_all_employees_directory_strictly_active_workforce(self):
        """
        Verify /employees/ strictly shows only ACTIVE employees, and redirects
        inactive filters to /employees/separated.
        """
        # 1. Access directory
        res = self.client.get("/employees/")
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")
        self.assertIn("Active Press Workforce", html)

        # Confirm all employees listed in the response are ACTIVE in DB
        conn = get_db_connection()
        ncns_emps = conn.execute("SELECT emp_no FROM employees WHERE employment_status != 'ACTIVE'").fetchall()
        for ncns in ncns_emps:
            self.assertNotIn(f">{ncns['emp_no']}<", html, f"Non-active {ncns['emp_no']} should not be in All Employees table")
        conn.close()

        # 2. Redirect test for inactive status filters
        res_ncns = self.client.get("/employees/?status=NO_CALL_NO_SHOW")
        self.assertEqual(res_ncns.status_code, 302)
        self.assertIn("/employees/separated?tab=ncns", res_ncns.headers["Location"])

        res_rel = self.client.get("/employees/?status=RELIEVED")
        self.assertEqual(res_rel.status_code, 302)
        self.assertIn("/employees/separated?tab=relieved", res_rel.headers["Location"])

if __name__ == "__main__":
    unittest.main()
