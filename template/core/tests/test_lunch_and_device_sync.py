import unittest
from unittest.mock import MagicMock, patch
from routes.attendance import compute_lunch_pattern
from models.biometric_sync import sync_employee_name_to_device

class TestLunchAndDeviceSync(unittest.TestCase):

    def test_lunch_pattern_completed(self):
        day_first_in = "09:00:00"
        day_last_out = "18:00:00"
        punches = [
            {"nature": "Shift Clock IN", "time": "09:00:00"},
            {"nature": "Break / Departure OUT", "time": "13:02:10"},
            {"nature": "Break Return / Duty IN", "time": "14:04:15"},
            {"nature": "Shift Clock OUT", "time": "18:00:00"}
        ]
        res = compute_lunch_pattern(punches, day_first_in, day_last_out)
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["out_time"], "13:02")
        self.assertEqual(res["in_time"], "14:04")
        self.assertEqual(res["duration_mins"], 62)
        self.assertIn("13:02", res["badge_text"])
        self.assertIn("14:04", res["badge_text"])

    def test_lunch_pattern_missing_return(self):
        day_first_in = "09:00:00"
        day_last_out = "18:00:00"
        punches = [
            {"nature": "Shift Clock IN", "time": "09:00:00"},
            {"nature": "Break / Departure OUT", "time": "13:05:00"},
            {"nature": "Shift Clock OUT", "time": "18:00:00"}
        ]
        res = compute_lunch_pattern(punches, day_first_in, day_last_out)
        self.assertEqual(res["status"], "MISSING_RETURN")
        self.assertEqual(res["suggested_time"], "14:00:00")
        self.assertEqual(res["suggested_type"], "IN")

    def test_lunch_pattern_missing_out(self):
        day_first_in = "09:00:00"
        day_last_out = "18:00:00"
        punches = [
            {"nature": "Shift Clock IN", "time": "09:00:00"},
            {"nature": "Break Return / Duty IN", "time": "14:05:00"},
            {"nature": "Shift Clock OUT", "time": "18:00:00"}
        ]
        res = compute_lunch_pattern(punches, day_first_in, day_last_out)
        self.assertEqual(res["status"], "MISSING_OUT")
        self.assertEqual(res["suggested_time"], "13:00:00")
        self.assertEqual(res["suggested_type"], "OUT")

    def test_lunch_pattern_no_lunch_logged(self):
        day_first_in = "09:00:00"
        day_last_out = "18:00:00"
        punches = [
            {"nature": "Shift Clock IN", "time": "09:00:00"},
            {"nature": "Shift Clock OUT", "time": "18:00:00"}
        ]
        res = compute_lunch_pattern(punches, day_first_in, day_last_out)
        self.assertEqual(res["status"], "NO_LUNCH_LOGGED")
        self.assertEqual(res["suggested_time"], "14:00:00")
        self.assertEqual(res["suggested_type"], "IN")

    @patch("models.biometric_sync.ZK")
    def test_sync_employee_name_to_device_success(self, mock_zk_class):
        mock_zk_instance = MagicMock()
        mock_conn = MagicMock()
        mock_zk_instance.connect.return_value = mock_conn
        mock_zk_class.return_value = mock_zk_instance

        mock_user = MagicMock()
        mock_user.user_id = "10"
        mock_user.uid = 10
        mock_conn.get_users.return_value = [mock_user]

        res = sync_employee_name_to_device("EMP0010", "NN-10")
        self.assertTrue(res["success"])
        self.assertIn("NN-10", res["message"])
        mock_conn.set_user.assert_called_once()
        mock_conn.refresh_data.assert_called_once()
        mock_conn.disconnect.assert_called_once()

    @patch("models.biometric_sync.ZK")
    def test_sync_employee_name_to_device_user_not_found(self, mock_zk_class):
        mock_zk_instance = MagicMock()
        mock_conn = MagicMock()
        mock_zk_instance.connect.return_value = mock_conn
        mock_zk_class.return_value = mock_zk_instance

        mock_conn.get_users.return_value = []

        res = sync_employee_name_to_device("EMP9999", "Ghost")
        self.assertFalse(res["success"])
        self.assertIn("not found", res["error"])
        mock_conn.disconnect.assert_called_once()

if __name__ == "__main__":
    unittest.main()
