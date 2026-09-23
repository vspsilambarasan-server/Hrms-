import unittest
from datetime import datetime
from app import create_app
from database import get_db_connection
from models.auto_sync import BiometricAutoSyncer, get_syncer, SYNC_LOCK

class TestAutoSyncModule(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        from models.auto_sync import stop_auto_sync
        stop_auto_sync()

    def test_auto_syncer_initialization(self):
        """Test BiometricAutoSyncer default configuration."""
        syncer = BiometricAutoSyncer(interval_seconds=60, days_back=7)
        self.assertEqual(syncer.interval_seconds, 60)
        self.assertEqual(syncer.days_back, 7)
        self.assertEqual(syncer.device_ip, "192.168.101.201")
        self.assertEqual(syncer.device_port, 4370)
        self.assertTrue(syncer.enabled)

    def test_auto_syncer_status_dictionary(self):
        """Test get_status() returns required fields for UI and API."""
        syncer = get_syncer()
        status = syncer.get_status()
        self.assertIn("enabled", status)
        self.assertIn("interval_seconds", status)
        self.assertIn("interval_label", status)
        self.assertIn("device_ip", status)
        self.assertIn("device_port", status)
        self.assertIn("is_running", status)
        self.assertIn("is_syncing", status)
        self.assertIn("last_status", status)
        self.assertIn("next_sync_in_seconds", status)
        self.assertEqual(status["interval_seconds"], 60)

    def test_auto_syncer_set_enabled_and_db_persistence(self):
        """Test toggle enabled and interval updates database settings."""
        syncer = get_syncer()
        
        # Disable auto-sync
        syncer.set_enabled(False, interval=60)
        self.assertFalse(syncer.enabled)

        conn = get_db_connection()
        row = conn.execute("SELECT biometric_auto_sync_enabled, biometric_auto_sync_interval FROM company_settings LIMIT 1").fetchone()
        self.assertEqual(row["biometric_auto_sync_enabled"], 0)
        self.assertEqual(row["biometric_auto_sync_interval"], 60)
        conn.close()

        # Re-enable auto-sync
        syncer.set_enabled(True, interval=60)
        self.assertTrue(syncer.enabled)

        conn = get_db_connection()
        row = conn.execute("SELECT biometric_auto_sync_enabled, biometric_auto_sync_interval FROM company_settings LIMIT 1").fetchone()
        self.assertEqual(row["biometric_auto_sync_enabled"], 1)
        conn.close()

    def test_api_auto_sync_status(self):
        """Test GET /attendance/auto-sync-status returns 200 and JSON."""
        res = self.client.get("/attendance/auto-sync-status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIsNotNone(data)
        self.assertIn("enabled", data)
        self.assertIn("interval_seconds", data)
        self.assertEqual(data["interval_seconds"], 60)
        self.assertEqual(data["device_ip"], "192.168.101.201")

    def test_api_toggle_auto_sync(self):
        """Test POST /attendance/toggle-auto-sync via AJAX and form."""
        # Toggle via AJAX
        res = self.client.post("/attendance/toggle-auto-sync", 
                               data={"enabled": "0"},
                               headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertFalse(data["enabled"])

        # Re-enable via AJAX
        res = self.client.post("/attendance/toggle-auto-sync", 
                               data={"enabled": "1"},
                               headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["enabled"])

    def test_sync_mutex_lock(self):
        """Test that SYNC_LOCK provides mutual exclusion."""
        acquired = SYNC_LOCK.acquire(timeout=1.0)
        self.assertTrue(acquired)
        try:
            # Second acquire with 0 timeout should fail while locked
            second_attempt = SYNC_LOCK.acquire(blocking=False)
            self.assertFalse(second_attempt)
        finally:
            SYNC_LOCK.release()

    def test_base_template_header_badge(self):
        """Test that base template renders the Auto-Sync badge in top header."""
        res = self.client.get("/attendance/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Auto-Sync:", html)
        self.assertIn("1m Active", html)

    def test_auto_sync_enrolled_staff_and_update_names_mocked(self):
        """
        Verify sync_device_attendance automatically enrolls new staff from device,
        updates placeholder employee names when changed on machine, and respects deletion blacklist.
        """
        from unittest.mock import MagicMock, patch
        from models.biometric_sync import sync_device_attendance

        conn = get_db_connection()
        # Add a blacklisted deleted employee log
        conn.execute("""
            INSERT OR REPLACE INTO employee_deletion_logs (id, original_emp_id, emp_no, first_name, last_name, deletion_reason)
            VALUES (6666, 6666, 'EMP6666', 'Purged', 'User', 'Resigned')
        """)
        # Ensure test employee EMP0010 exists with placeholder name
        dept = conn.execute("SELECT id FROM departments LIMIT 1").fetchone()
        dept_id = dept["id"] if dept else 1
        shift = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()
        shift_id = shift["id"] if shift else 1
        conn.execute("""
            INSERT OR REPLACE INTO employees (
                id, emp_no, first_name, last_name, email, department_id,
                designation, join_date, default_shift_id, salary_type, pay_frequency, is_active
            ) VALUES (
                9010, 'EMP0010', 'NO NAME', 'NO NAME', 'emp0010@test.com', ?,
                'Operator', '2026-01-01', ?, 'WEEKLY', 'WEEKLY', 1
            )
        """, (dept_id, shift_id))
        # Ensure test new employee EMP7771 does not exist
        conn.execute("DELETE FROM employees WHERE emp_no = 'EMP7771'")
        conn.commit()
        conn.close()

        # Mock ZK machine
        mock_u_new = MagicMock()
        mock_u_new.user_id = "7771"
        mock_u_new.name = "Karthik Raj"

        mock_u_update = MagicMock()
        mock_u_update.user_id = "10"
        mock_u_update.name = "Kannan Murugan"

        mock_u_deleted = MagicMock()
        mock_u_deleted.user_id = "6666"
        mock_u_deleted.name = "Purged User"

        # Mock punch log for new user
        mock_log = MagicMock()
        mock_log.user_id = "7771"
        mock_log.timestamp = datetime.now()

        with patch("models.biometric_sync.get_zk_client") as mock_zk_fn:
            mock_zk_inst = MagicMock()
            mock_conn = MagicMock()
            mock_conn.get_users.return_value = [mock_u_new, mock_u_update, mock_u_deleted]
            mock_conn.get_attendance.return_value = [mock_log]
            mock_zk_inst.connect.return_value = mock_conn
            mock_zk_fn.return_value = mock_zk_inst

            res = sync_device_attendance("127.0.0.1", days_back=7)
            self.assertTrue(res["success"])
            self.assertEqual(res["enrolled_staff_count"], 1)
            self.assertEqual(res["updated_staff_count"], 1)

        # Verify DB state
        conn = get_db_connection()
        # 1. New staff enrolled
        emp_new = conn.execute("SELECT * FROM employees WHERE emp_no = 'EMP7771'").fetchone()
        self.assertIsNotNone(emp_new, "Newly enrolled staff should be added to employees automatically")
        self.assertEqual(emp_new["first_name"], "Karthik")
        self.assertEqual(emp_new["last_name"], "Raj")
        self.assertEqual(emp_new["employment_status"], "ACTIVE")

        # 2. Updated staff name
        emp_10 = conn.execute("SELECT * FROM employees WHERE emp_no = 'EMP0010'").fetchone()
        self.assertEqual(emp_10["first_name"], "Kannan")
        self.assertEqual(emp_10["last_name"], "Murugan")

        # 3. Deleted staff was not re-enrolled
        emp_del = conn.execute("SELECT * FROM employees WHERE emp_no = 'EMP6666'").fetchone()
        self.assertIsNone(emp_del, "Deleted staff should never be re-enrolled")

        # 4. Punch record recorded for new staff
        punch = conn.execute("SELECT * FROM attendance_punches WHERE employee_id = ?", (emp_new["id"],)).fetchone()
        self.assertIsNotNone(punch, "Punch log should be recorded for newly enrolled employee")

        # Cleanup
        conn.execute("DELETE FROM employees WHERE emp_no = 'EMP7771'")
        conn.execute("DELETE FROM attendance_punches WHERE employee_id = ?", (emp_new["id"],))
        conn.execute("DELETE FROM attendance_records WHERE employee_id = ?", (emp_new["id"],))
        conn.execute("DELETE FROM employee_deletion_logs WHERE id = 6666")
        conn.execute("UPDATE employees SET first_name = 'NO NAME', last_name = 'NO NAME' WHERE emp_no = 'EMP0010'")
        conn.commit()
        conn.close()

    def test_auto_sync_message_formatting_with_enrolled_staff(self):
        """Test that last_message in auto_syncer includes staff enrolled and updated counts."""
        from unittest.mock import patch
        syncer = BiometricAutoSyncer()

        mock_sync_result = {
            "success": True,
            "total_device_logs": 450,
            "recent_logs_considered": 12,
            "new_punches_added": 2,
            "days_evaluated": 5,
            "ot_claims_created": 1,
            "enrolled_staff_count": 2,
            "updated_staff_count": 1
        }

        with patch("models.biometric_sync.sync_device_attendance", return_value=mock_sync_result):
            res = syncer.sync_now(trigger_source="TEST")
            self.assertTrue(res["success"])
            self.assertIn("+2 staff enrolled", syncer.last_message)
            self.assertIn("1 staff updated", syncer.last_message)
            self.assertIn("5 days evaluated", syncer.last_message)

if __name__ == "__main__":
    unittest.main()
