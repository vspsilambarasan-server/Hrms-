import traceback
from datetime import datetime, timedelta
from zk import ZK
from database import get_db_connection
from models.shift_engine import evaluate_attendance
from models.ot_engine import sync_overtime_from_attendance

DEFAULT_DEVICE_IP = "192.168.101.201"
DEFAULT_DEVICE_PORT = 4370

def resolve_device_ip(ip_input=None):
    """
    Handles user IP input, resolving common typos (e.g., 198.168.101.201 -> 192.168.101.201).
    """
    if not ip_input or ip_input.strip() == "":
        return DEFAULT_DEVICE_IP
    ip = ip_input.strip()
    if ip == "198.168.101.201":
        # Check if 192.168.101.201 is reachable
        return "192.168.101.201"
    return ip

def get_zk_client(ip=None, port=DEFAULT_DEVICE_PORT, timeout=5):
    device_ip = resolve_device_ip(ip)
    return ZK(device_ip, port=port, timeout=timeout)

def test_device_connection(ip=None, port=DEFAULT_DEVICE_PORT):
    device_ip = resolve_device_ip(ip)
    zk = get_zk_client(device_ip, port, timeout=4)
    try:
        conn = zk.connect()
        info = {
            "success": True,
            "ip": device_ip,
            "port": port,
            "device_name": conn.get_device_name() or "ZKTeco Biometric Terminal",
            "serial_number": conn.get_serialnumber() or "Unknown",
            "firmware": conn.get_firmware_version() or "Standard",
            "users_count": len(conn.get_users()),
            "logs_count": len(conn.get_attendance())
        }
        conn.disconnect()
        return info
    except Exception as e:
        return {
            "success": False,
            "ip": device_ip,
            "port": port,
            "error": str(e)
        }

def _get_default_dept_and_shift(cursor, conn_db):
    """Ensure a default department and shift exist and return their IDs."""
    cursor.execute("SELECT id FROM departments LIMIT 1")
    dept_row = cursor.fetchone()
    if dept_row:
        default_dept_id = dept_row["id"]
    else:
        cursor.execute("INSERT INTO departments (code, name, description) VALUES ('PLANT', 'Plant Operations', 'Main facility line staff')")
        conn_db.commit()
        default_dept_id = cursor.lastrowid

    cursor.execute("SELECT id FROM shifts LIMIT 1")
    shift_row = cursor.fetchone()
    if shift_row:
        default_shift_id = shift_row["id"]
    else:
        cursor.execute("""
            INSERT INTO shifts (code, name, start_time, end_time, is_overnight, grace_late_mins, grace_early_mins, break_mins, allowance_rate, color)
            VALUES ('GEN', 'General Plant Shift', '09:00', '18:00', 0, 15, 15, 60, 0.0, '#2563eb')
        """)
        conn_db.commit()
        default_shift_id = cursor.lastrowid
    return default_dept_id, default_shift_id

def _get_deleted_staff_sets(cursor):
    """Fetch permanently deleted employee records - NEVER re-create them!"""
    cursor.execute("SELECT emp_no, original_emp_id FROM employee_deletion_logs")
    deleted_rows = cursor.fetchall()
    deleted_emp_nos = set()
    deleted_uids = set()
    for r in deleted_rows:
        if r["emp_no"]:
            deleted_emp_nos.add(r["emp_no"])
            num_part = r["emp_no"].replace("EMP", "").replace("EMP-", "").lstrip("0")
            if num_part:
                deleted_uids.add(num_part)
        if r["original_emp_id"]:
            deleted_uids.add(str(r["original_emp_id"]))
    return deleted_emp_nos, deleted_uids

def _is_placeholder_name(name_str):
    """Check if a name string is an automated hardware placeholder like NN-xx or Staff xx."""
    if not name_str:
        return True
    s = name_str.strip().upper()
    return (
        s == "" or
        s == "NO NAME" or
        s.startswith("NO NAME") or
        s.startswith("NN-") or
        s.startswith("NN ") or
        s.startswith("STAFF ") or
        s.startswith("STAFF-") or
        s.startswith("EMP") or
        s.startswith("DEV")
    )

def _sync_users_data(cursor, conn_db, users, deleted_emp_nos, deleted_uids, default_dept_id, default_shift_id, emp_map=None):
    """
    Ingests enrolled staff from biometric machine into database:
    - Automatically enrolls new staff as ACTIVE employees.
    - Updates staff names if updated/completed on biometric terminal with actual names.
    - Preserves human-configured names in HRMS (machine default NN-xx placeholders never overwrite real names).
    - Populates and updates emp_map in place.
    - Returns (added_count, updated_count).
    """
    if emp_map is None:
        cursor.execute("SELECT * FROM employees")
        emp_rows = [dict(r) for r in cursor.fetchall()]
        emp_map = {}
        for e in emp_rows:
            emp_map[e["emp_no"]] = e
            emp_num_str = e["emp_no"].replace("EMP", "").replace("EMP-", "").lstrip("0")
            if emp_num_str:
                emp_map[emp_num_str] = e

    added_count = 0
    updated_count = 0
    today_str = datetime.now().strftime("%Y-%m-%d")

    for u in users:
        uid_str = str(u.user_id).strip()
        if not uid_str:
            continue
        emp_no = f"EMP{int(uid_str):04d}" if uid_str.isdigit() else f"EMP-{uid_str}"

        # If this user was intentionally deleted by management, NEVER re-create
        if emp_no in deleted_emp_nos or uid_str in deleted_uids:
            continue

        raw_name = (u.name or f"Staff {uid_str}").strip()
        name_parts = raw_name.split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        emp = emp_map.get(emp_no) or emp_map.get(uid_str)
        if not emp:
            cursor.execute("""
                INSERT INTO employees (
                    emp_no, first_name, last_name, email, phone,
                    department_id, designation, join_date, default_shift_id,
                    salary_type, pay_frequency, base_salary, shift_salary, ot_hourly_rate,
                    hourly_rate, hra, special_allowance,
                    pf_deduction_pct, esi_deduction_pct, tax_deduction_pct,
                    pf_enabled, esi_enabled, safety_training_completed,
                    employment_status, weekly_off_day, is_active
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, 'Press Operator', ?, ?,
                    'WEEKLY', 'WEEKLY', 3000.0, 500.0, 75.0,
                    75.0, 0.0, 0.0,
                    12.0, 0.75, 5.0,
                    0, 0, 1,
                    'ACTIVE', 6, 1
                )
            """, (
                emp_no, first_name, last_name,
                f"{first_name.lower().replace(' ', '')}{uid_str}@vasanthamprinters.com",
                "+91 98000 00000",
                default_dept_id,
                today_str,
                default_shift_id
            ))
            new_id = cursor.lastrowid
            new_emp = {
                "id": new_id,
                "emp_no": emp_no,
                "first_name": first_name,
                "last_name": last_name,
                "default_shift_id": default_shift_id,
                "weekly_off_day": 6,
                "is_admin": 0,
                "employment_status": "ACTIVE"
            }
            emp_map[emp_no] = new_emp
            emp_map[uid_str] = new_emp
            num_clean = uid_str.lstrip("0")
            if num_clean:
                emp_map[num_clean] = new_emp
            added_count += 1
        else:
            # Existing employee in HRMS
            dev_name = (u.name or "").strip()
            dev_is_placeholder = _is_placeholder_name(dev_name)

            curr_first = (emp.get("first_name") or "").strip()
            curr_last = (emp.get("last_name") or "").strip()
            curr_full = f"{curr_first} {curr_last}".strip()
            curr_is_placeholder = _is_placeholder_name(curr_full)

            # ONLY update existing employee if:
            # 1. The HRMS profile currently has an automated placeholder name (e.g. NN-xx, NO NAME, Staff xx)
            # 2. AND the machine now has an actual real name (not a placeholder)
            if curr_is_placeholder and not dev_is_placeholder:
                cursor.execute("UPDATE employees SET first_name = ?, last_name = ? WHERE id = ?", (first_name, last_name, emp["id"]))
                emp["first_name"] = first_name
                emp["last_name"] = last_name
                updated_count += 1

    if added_count > 0 or updated_count > 0:
        conn_db.commit()

    return added_count, updated_count

def sync_device_users(ip=None, port=DEFAULT_DEVICE_PORT):
    """
    Fetches enrolled staff from biometric machine and creates employee profiles
    if they do not already exist.
    """
    device_ip = resolve_device_ip(ip)
    zk = get_zk_client(device_ip, port, timeout=6)
    
    conn_db = get_db_connection()
    cursor = conn_db.cursor()

    default_dept_id, default_shift_id = _get_default_dept_and_shift(cursor, conn_db)
    deleted_emp_nos, deleted_uids = _get_deleted_staff_sets(cursor)

    try:
        conn = zk.connect()
        users = conn.get_users()
        conn.disconnect()

        added_count, updated_count = _sync_users_data(
            cursor, conn_db, users, deleted_emp_nos, deleted_uids, default_dept_id, default_shift_id
        )

        return {
            "success": True,
            "total_device_users": len(users),
            "added_employees": added_count,
            "updated_employees": updated_count,
            "existing_employees": len(users) - added_count
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        conn_db.close()

def sync_employee_name_to_device(emp_no, full_name, ip=None, port=DEFAULT_DEVICE_PORT):
    """
    Directly updates an employee's name on the physical ZKTeco biometric machine
    so that the hardware LCD screen immediately displays their updated name.
    Thread-safe with auto-sync mutex lock.
    """
    if not emp_no or not full_name:
        return {"success": False, "error": "emp_no and full_name are required."}

    from models.auto_sync import SYNC_LOCK
    # Acquire lock with 6s timeout to avoid collision with background auto-sync thread
    acquired = SYNC_LOCK.acquire(timeout=6.0)
    if not acquired:
        return {"success": False, "error": "Biometric terminal is busy with attendance sync. Please retry."}

    device_ip = resolve_device_ip(ip)
    clean_name = str(full_name).strip()[:24]
    # Derive numeric user_id (e.g. EMP0010 -> 10, EMP-3 -> 3)
    uid_str = emp_no.replace("EMP", "").replace("EMP-", "").lstrip("0")
    if not uid_str:
        uid_str = emp_no

    zk = get_zk_client(device_ip, port, timeout=5)
    try:
        conn = zk.connect()
        users = conn.get_users()
        target_user = None
        for u in users:
            u_id = str(u.user_id).strip()
            # Match user_id as string or integer
            if u_id == uid_str:
                target_user = u
                break
            if uid_str.isdigit() and u_id.isdigit() and int(u_id) == int(uid_str):
                target_user = u
                break

        if not target_user:
            conn.disconnect()
            return {"success": False, "error": f"Employee {emp_no} (User ID {uid_str}) not found on biometric machine."}

        # Push updated name to machine hardware
        conn.set_user(
            uid=target_user.uid,
            name=clean_name,
            privilege=target_user.privilege,
            password=target_user.password,
            group_id=target_user.group_id,
            user_id=target_user.user_id,
            card=target_user.card
        )
        try:
            conn.refresh_data()
        except Exception:
            pass
        conn.disconnect()
        return {
            "success": True,
            "message": f"Updated name on biometric machine to '{clean_name}'",
            "device_ip": device_ip,
            "user_id": target_user.user_id
        }
    except Exception as e:
        return {"success": False, "error": f"Failed to update biometric terminal: {str(e)}"}
    finally:
        SYNC_LOCK.release()

def sync_device_attendance(ip=None, port=DEFAULT_DEVICE_PORT, days_back=30):
    """
    Downloads attendance punch logs directly from biometric machine,
    automatically enrolling new staff and updating changed staff details,
    pairs daily punches, and calculates shift evaluations and overtime.
    """
    device_ip = resolve_device_ip(ip)
    zk = get_zk_client(device_ip, port, timeout=10)

    conn_db = get_db_connection()
    cursor = conn_db.cursor()

    default_dept_id, default_shift_id = _get_default_dept_and_shift(cursor, conn_db)
    deleted_emp_nos, deleted_uids = _get_deleted_staff_sets(cursor)

    # Preload shifts map
    cursor.execute("SELECT * FROM shifts")
    shifts_map = {s["id"]: dict(s) for s in cursor.fetchall()}

    # Preload employee map: user_id string and emp_no -> employee dict
    cursor.execute("SELECT * FROM employees")
    emp_rows = [dict(r) for r in cursor.fetchall()]
    
    emp_map = {}
    for e in emp_rows:
        emp_map[e["emp_no"]] = e
        emp_num_str = e["emp_no"].replace("EMP", "").replace("EMP-", "").lstrip("0")
        if emp_num_str:
            emp_map[emp_num_str] = e

    try:
        conn = zk.connect()
        # Fetch both enrolled users and attendance logs in the SAME unified TCP socket session
        try:
            device_users = conn.get_users()
        except Exception as eu:
            print(f"[Biometric Sync] Warning: Could not fetch users during attendance sync: {eu}")
            device_users = []
        attendance_logs = conn.get_attendance()
        conn.disconnect()

        # Update enrolled staff / auto-enroll new staff from terminal into HRMS
        added_staff_count = 0
        updated_staff_count = 0
        if device_users:
            added_staff_count, updated_staff_count = _sync_users_data(
                cursor, conn_db, device_users, deleted_emp_nos, deleted_uids,
                default_dept_id, default_shift_id, emp_map=emp_map
            )

        cutoff_date = datetime.now() - timedelta(days=days_back)
        recent_logs = [log for log in attendance_logs if log.timestamp >= cutoff_date]

        # Group punches by (employee_id, shift_date)
        day_punches = {} # (employee_id, date_str) -> [datetime, ...]
        raw_punch_records = []

        for log in recent_logs:
            u_id = str(log.user_id)
            emp_no_cand = f"EMP{int(u_id):04d}" if u_id.isdigit() else f"EMP-{u_id}"

            # If user was permanently deleted by management, skip their historical punches
            if u_id in deleted_uids or emp_no_cand in deleted_emp_nos:
                continue

            emp = emp_map.get(u_id) or emp_map.get(emp_no_cand)
            if not emp:
                continue

            emp_id = emp["id"]
            p_dt = log.timestamp
            date_str = p_dt.strftime("%Y-%m-%d")
            p_str = p_dt.strftime("%Y-%m-%d %H:%M:%S")

            raw_punch_records.append((emp_id, p_str, device_ip))

            # Check for cross-midnight checkout (strictly for scheduled overnight shifts before 06:30 AM)
            if p_dt.hour < 7:
                prev_date_str = (p_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                if (emp_id, prev_date_str) in day_punches and len(day_punches[(emp_id, prev_date_str)]) > 0:
                    cursor.execute("SELECT shift_id FROM roster_schedules WHERE employee_id = ? AND date = ?", (emp_id, prev_date_str))
                    r_prev = cursor.fetchone()
                    prev_shift_id = r_prev["shift_id"] if r_prev and r_prev["shift_id"] else emp.get("default_shift_id")
                    prev_shift = shifts_map.get(prev_shift_id, {})
                    if prev_shift.get("is_overnight"):
                        date_str = prev_date_str

            key = (emp_id, date_str)
            if key not in day_punches:
                day_punches[key] = []
            day_punches[key].append(p_dt)

        new_punches_count = 0
        if raw_punch_records:
            cursor.execute("SELECT COUNT(*) as cnt FROM attendance_punches")
            before_cnt = cursor.fetchone()["cnt"]
            cursor.executemany("""
                INSERT OR IGNORE INTO attendance_punches (employee_id, punch_time, punch_type, device_id, notes)
                VALUES (?, ?, 'PUNCH', ?, 'Biometric Terminal Log')
            """, raw_punch_records)
            conn_db.commit()
            cursor.execute("SELECT COUNT(*) as cnt FROM attendance_punches")
            new_punches_count = max(0, cursor.fetchone()["cnt"] - before_cnt)

        processed_days = 0
        total_ot_logged = 0

        for (emp_id, s_date), punch_list in day_punches.items():
            if not punch_list:
                continue
            punch_list.sort()
            p_in = punch_list[0].strftime("%Y-%m-%d %H:%M:%S")
            p_out = punch_list[-1].strftime("%Y-%m-%d %H:%M:%S") if len(punch_list) > 1 else p_in

            # Lookup shift
            cursor.execute("SELECT shift_id, is_off_day FROM roster_schedules WHERE employee_id = ? AND date = ?", (emp_id, s_date))
            roster = cursor.fetchone()
            if roster and roster["shift_id"]:
                shift_id = roster["shift_id"]
                is_off = roster["is_off_day"]
            else:
                cursor.execute("SELECT default_shift_id, weekly_off_day FROM employees WHERE id = ?", (emp_id,))
                emp_info = cursor.fetchone()
                shift_id = emp_info["default_shift_id"] if emp_info else None
                s_weekday = datetime.strptime(s_date, "%Y-%m-%d").weekday()
                is_off = 1 if (emp_info and emp_info["weekly_off_day"] == s_weekday) else 0

            shift_obj = shifts_map.get(shift_id, {
                "start_time": "09:00", "end_time": "18:00", "is_overnight": 0,
                "grace_late_mins": 15, "grace_early_mins": 15, "break_mins": 60,
                "min_hours_half_day": 4.5, "min_hours_full_day": 8.0, "allowance_rate": 0.0
            })

            eval_res = evaluate_attendance(s_date, shift_obj, p_in, p_out, is_off_day=bool(is_off))

            cursor.execute("""
                INSERT INTO attendance_records (
                    employee_id, date, shift_id, punch_in, punch_out,
                    work_hours, late_mins, early_leave_mins, status,
                    shift_allowance_amount, raw_ot_hours, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Synced from ZKTeco Device')
                ON CONFLICT(employee_id, date) DO UPDATE SET
                    shift_id = excluded.shift_id,
                    punch_in = excluded.punch_in,
                    punch_out = excluded.punch_out,
                    work_hours = excluded.work_hours,
                    late_mins = excluded.late_mins,
                    early_leave_mins = excluded.early_leave_mins,
                    status = excluded.status,
                    shift_allowance_amount = excluded.shift_allowance_amount,
                    raw_ot_hours = excluded.raw_ot_hours,
                    notes = excluded.notes
            """, (
                emp_id, s_date, shift_id, p_in, p_out,
                eval_res["work_hours"], eval_res["late_mins"], eval_res["early_leave_mins"],
                eval_res["status"], eval_res["shift_allowance"], eval_res["total_ot_hours"]
            ))
            conn_db.commit()

            cursor.execute("SELECT id FROM attendance_records WHERE employee_id = ? AND date = ?", (emp_id, s_date))
            att_rec = cursor.fetchone()

            # Missed middle punch detection during biometric sync
            is_admin_emp = int(emp.get("is_admin") or 0) == 1
            if att_rec and not is_admin_emp and len(punch_list) >= 2:
                first_p = punch_list[0]
                last_p = punch_list[-1]
                span_sec = (last_p - first_p).total_seconds()
                if first_p.hour <= 11 and span_sec >= 14400:
                    if len(punch_list) == 2 or (len(punch_list) % 2 != 0):
                        cursor.execute("""
                            UPDATE attendance_records
                            SET has_missed_mid_punch = 1,
                                mid_punch_status = CASE WHEN mid_punch_status = 'APPROVED' THEN 'APPROVED' ELSE 'FLAGGED' END,
                                mid_punch_notes = COALESCE(mid_punch_notes, 'Morning clock-in & evening logout recorded with missing intermediate break punches.')
                            WHERE id = ?
                        """, (att_rec["id"],))
                        conn_db.commit()

            if att_rec and eval_res["total_ot_hours"] > 0:
                sync_overtime_from_attendance(conn_db, att_rec["id"])
                total_ot_logged += 1

            processed_days += 1

        conn_db.close()
        return {
            "success": True,
            "total_device_logs": len(attendance_logs),
            "recent_logs_considered": len(recent_logs),
            "new_punches_added": new_punches_count,
            "days_evaluated": processed_days,
            "ot_claims_created": total_ot_logged,
            "enrolled_staff_count": added_staff_count,
            "updated_staff_count": updated_staff_count,
            "added_employees": added_staff_count,
            "updated_employees": updated_staff_count
        }

    except Exception as e:
        conn_db.close()
        return {"success": False, "error": str(e)}

def delete_user_from_device(emp_no, ip=None, port=DEFAULT_DEVICE_PORT):
    """
    Attempts to delete an employee user profile from the physical ZKTeco terminal hardware.
    Fails gracefully if the biometric device is offline.
    """
    device_ip = resolve_device_ip(ip)
    zk = get_zk_client(device_ip, port, timeout=5)
    try:
        conn = zk.connect()
        uid_num = None
        if emp_no:
            clean = emp_no.replace("EMP", "").replace("EMP-", "").lstrip("0")
            if clean.isdigit():
                uid_num = int(clean)
        if uid_num is not None:
            conn.delete_user(uid=uid_num, user_id=str(uid_num))
        conn.disconnect()
        return True
    except Exception as e:
        print(f"[Device Sync] Warning: Could not delete user {emp_no} from terminal {device_ip}: {e}")
        return False
