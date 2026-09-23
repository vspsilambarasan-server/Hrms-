import os
import threading
import time
from datetime import datetime, timedelta
from database import get_db_connection

# Global Mutex Lock: Guarantees only one thread connects to ZKTeco socket at a time
SYNC_LOCK = threading.Lock()

class BiometricAutoSyncer:
    """
    Background daemon worker that automatically synchronizes real punches from the
    physical ZKTeco biometric machine every 60 seconds (1 minute).
    """
    def __init__(self, interval_seconds=60, days_back=7):
        self.interval_seconds = interval_seconds
        self.days_back = days_back
        self.enabled = True
        self.device_ip = "192.168.101.201"
        self.device_port = 4370

        self.thread = None
        self.stop_event = threading.Event()
        self.is_syncing = False

        self.last_sync_time = None
        self.last_status = "IDLE"
        self.last_message = "Auto-sync ready (1-minute interval)"
        self.last_result = None
        self.next_sync_time = None
        self.sync_count = 0

        # Load initial settings from database if available
        self._load_db_settings()

    def _load_db_settings(self):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM company_settings LIMIT 1")
            row = cursor.fetchone()
            if row:
                d = dict(row)
                self.device_ip = d.get("biometric_device_ip") or self.device_ip
                self.device_port = d.get("biometric_device_port") or self.device_port
                self.enabled = bool(d.get("biometric_auto_sync_enabled", 1))
                self.interval_seconds = int(d.get("biometric_auto_sync_interval", 60))
                self.last_sync_time = d.get("biometric_last_sync_time")
                self.last_status = d.get("biometric_last_sync_status") or self.last_status
                self.last_message = d.get("biometric_last_sync_message") or self.last_message
                self.sync_count = int(d.get("biometric_auto_sync_count", 0))
            conn.close()
        except Exception:
            pass

    def _persist_sync_status(self):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE company_settings
                SET biometric_last_sync_time = ?,
                    biometric_last_sync_status = ?,
                    biometric_last_sync_message = ?,
                    biometric_auto_sync_count = ?
                WHERE id = (SELECT id FROM company_settings LIMIT 1)
            """, (self.last_sync_time, self.last_status, self.last_message, self.sync_count))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def start(self):
        """Starts background daemon thread if not already running."""
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run_loop, name="BiometricAutoSyncThread", daemon=True)
        self.thread.start()

    def stop(self):
        """Gracefully signals the worker to terminate."""
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)

    def _run_loop(self):
        """Main periodic loop executing every interval_seconds (60s)."""
        # Initial small delay of 5s on boot so Flask finishes loading
        if self.stop_event.wait(timeout=5.0):
            return

        # Perform initial sync upon startup if enabled
        if self.enabled:
            self.sync_now(trigger_source="BOOT")

        while not self.stop_event.is_set():
            # Update next scheduled sync time
            now = datetime.now()
            self.next_sync_time = now + timedelta(seconds=self.interval_seconds)

            # Wait for next tick (60 seconds) or stop signal
            if self.stop_event.wait(timeout=self.interval_seconds):
                break

            if self.enabled:
                self.sync_now(trigger_source="TIMER")

    def sync_now(self, trigger_source="MANUAL"):
        """
        Connects to ZKTeco machine, fetches real punches, and updates shift records.
        Protected by SYNC_LOCK to avoid socket collision.
        """
        # Try to acquire lock with 8s timeout
        acquired = SYNC_LOCK.acquire(timeout=8.0)
        if not acquired:
            return {
                "success": False,
                "error": "Another sync operation is currently in progress. Please wait a moment."
            }

        self.is_syncing = True
        self.last_status = "SYNCING"
        start_ts = time.time()

        try:
            from models.biometric_sync import sync_device_attendance
            # Run sync against the biometric machine
            res = sync_device_attendance(
                ip=self.device_ip,
                port=self.device_port,
                days_back=self.days_back
            )

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.last_sync_time = now_str
            self.last_result = res
            elapsed = time.time() - start_ts

            if res.get("success"):
                self.last_status = "SUCCESS"
                self.sync_count += 1
                logs = res.get("total_device_logs", 0)
                days = res.get("days_evaluated", 0)
                ot = res.get("ot_claims_created", 0)
                enrolled = res.get("enrolled_staff_count", 0)
                updated = res.get("updated_staff_count", 0)

                parts = []
                if enrolled > 0:
                    parts.append(f"+{enrolled} staff enrolled")
                if updated > 0:
                    parts.append(f"{updated} staff updated")
                parts.append(f"{days} days evaluated")
                if ot > 0:
                    parts.append(f"{ot} OT claims")
                
                # Scan for missing punch gaps against expected shift patterns
                try:
                    from models.missing_punch_detector import scan_and_record_missing_punches
                    scan_res = scan_and_record_missing_punches(days_back=2)
                    if scan_res.get("new_alerts_count", 0) > 0:
                        parts.append(f"{scan_res['new_alerts_count']} missing punch alert(s)")
                except Exception:
                    pass

                details = ", ".join(parts)
                self.last_message = f"Synced {logs:,} punches from ZKTeco ({details}) in {elapsed:.1f}s"
            else:
                err = res.get("error", "Unknown error")
                self.last_status = "FAILED"
                self.last_message = f"Biometric sync failed: {err}"

            self._persist_sync_status()
            return res

        except Exception as e:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.last_sync_time = now_str
            self.last_status = "FAILED"
            self.last_message = f"Unexpected error during sync: {str(e)}"
            self._persist_sync_status()
            return {"success": False, "error": str(e)}

        finally:
            self.is_syncing = False
            SYNC_LOCK.release()

    def set_enabled(self, enabled: bool, interval: int = None):
        """Toggle auto-sync enabled state and optionally update interval."""
        self.enabled = bool(enabled)
        if interval and interval >= 10:
            self.interval_seconds = int(interval)
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE company_settings
                SET biometric_auto_sync_enabled = ?,
                    biometric_auto_sync_interval = ?
                WHERE id = (SELECT id FROM company_settings LIMIT 1)
            """, (1 if self.enabled else 0, self.interval_seconds))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def get_status(self):
        """Returns comprehensive status dictionary for APIs and UI templates."""
        now = datetime.now()
        seconds_left = 0
        if self.next_sync_time and self.enabled:
            delta = (self.next_sync_time - now).total_seconds()
            seconds_left = max(0, int(delta))
        elif self.enabled:
            seconds_left = self.interval_seconds

        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "interval_label": f"{self.interval_seconds}s" if self.interval_seconds != 60 else "1 min",
            "device_ip": self.device_ip,
            "device_port": self.device_port,
            "is_running": bool(self.thread and self.thread.is_alive()),
            "is_syncing": self.is_syncing,
            "last_sync_time": self.last_sync_time,
            "last_status": self.last_status,
            "last_message": self.last_message,
            "next_sync_in_seconds": seconds_left,
            "next_sync_time": self.next_sync_time.strftime("%H:%M:%S") if self.next_sync_time else None,
            "sync_count": self.sync_count
        }

# Global Singleton Syncer Instance
_syncer_instance = None

def get_syncer():
    global _syncer_instance
    if _syncer_instance is None:
        _syncer_instance = BiometricAutoSyncer(interval_seconds=60, days_back=7)
    return _syncer_instance

def start_auto_sync():
    syncer = get_syncer()
    syncer.start()
    return syncer

def stop_auto_sync():
    syncer = get_syncer()
    syncer.stop()

def get_auto_sync_status():
    syncer = get_syncer()
    return syncer.get_status()

def trigger_sync_now():
    syncer = get_syncer()
    return syncer.sync_now(trigger_source="MANUAL_API")

def set_auto_sync_enabled(enabled, interval=60):
    syncer = get_syncer()
    syncer.set_enabled(enabled, interval)
    return syncer.get_status()
