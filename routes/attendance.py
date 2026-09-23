import io
import csv
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response, session
from datetime import datetime, date, timedelta
from database import get_db_connection
from models.shift_engine import evaluate_attendance
from models.ot_engine import sync_overtime_from_attendance

attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")

@attendance_bp.route("/")
def index():
    conn = get_db_connection()
    cursor = conn.cursor()

    selected_date = request.args.get("date")
    if not selected_date:
        # Default to latest date with records or today
        cursor.execute("SELECT MAX(date) as max_date FROM attendance_records")
        latest_row = cursor.fetchone()
        selected_date = latest_row["max_date"] or date.today().strftime("%Y-%m-%d")

    dept_id = request.args.get("dept")

    # Compute prev and next dates for easy day-flipping
    try:
        cur_dt = datetime.strptime(selected_date, "%Y-%m-%d").date()
    except ValueError:
        cur_dt = date.today()
        selected_date = cur_dt.strftime("%Y-%m-%d")

    prev_date = (cur_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (cur_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    today_date = date.today().strftime("%Y-%m-%d")

    # Show ALL active employees for the selected date
    query = """
        SELECT 
            e.id as employee_id,
            e.emp_no,
            e.first_name,
            e.last_name,
            e.designation,
            e.weekly_off_day,
            d.name as dept_name,
            e.is_admin,
            COALESCE(s_roster.name, s_def.name, 'General Shift') as shift_name,
            COALESCE(s_roster.code, s_def.code, 'GEN') as shift_code,
            COALESCE(s_roster.start_time, s_def.start_time, '09:00') as start_time,
            COALESCE(s_roster.end_time, s_def.end_time, '18:00') as end_time,
            COALESCE(s_roster.is_overnight, s_def.is_overnight, 0) as is_overnight,
            COALESCE(s_roster.color, s_def.color, '#2563eb') as shift_color,
            a.id as record_id,
            a.punch_in,
            a.punch_out,
            COALESCE(a.work_hours, 0.0) as work_hours,
            COALESCE(a.late_mins, 0) as late_mins,
            COALESCE(a.early_leave_mins, 0) as early_leave_mins,
            CASE 
                WHEN a.status IS NOT NULL THEN a.status
                WHEN r.is_off_day = 1 THEN 'WEEKLY_OFF'
                WHEN strftime('%w', ?) = '0' THEN 'WEEKLY_OFF'
                ELSE 'ABSENT'
            END as status,
            COALESCE(a.shift_allowance_amount, 0.0) as shift_allowance_amount,
            COALESCE(a.gross_ot_hours, 0.0) as gross_ot_hours,
            COALESCE(a.late_deduction_mins, 0) as late_deduction_mins,
            COALESCE(a.raw_ot_hours, 0.0) as raw_ot_hours,
            COALESCE(a.has_missed_mid_punch, 0) as has_missed_mid_punch,
            COALESCE(a.mid_punch_status, 'NORMAL') as mid_punch_status,
            a.mid_punch_notes,
            a.mid_punch_approved_by,
            a.mid_punch_approved_at,
            a.notes
        FROM employees e
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN roster_schedules r ON r.employee_id = e.id AND r.date = ?
        LEFT JOIN shifts s_roster ON r.shift_id = s_roster.id
        LEFT JOIN shifts s_def ON e.default_shift_id = s_def.id
        LEFT JOIN attendance_records a ON a.employee_id = e.id AND a.date = ?
        WHERE e.is_active = 1
    """
    params = [selected_date, selected_date, selected_date]

    if dept_id and dept_id.isdigit():
        query += " AND e.department_id = ?"
        params.append(int(dept_id))

    query += " ORDER BY e.emp_no ASC"
    cursor.execute(query, params)
    records = [dict(row) for row in cursor.fetchall()]

    # Fetch all raw punches for this date to display in daily row
    cursor.execute("""
        SELECT id, employee_id, punch_time, punch_type, device_id
        FROM attendance_punches
        WHERE punch_time LIKE ?
        ORDER BY punch_time ASC
    """, (f"{selected_date}%",))
    punch_rows = cursor.fetchall()

    punches_by_emp = {}
    for pr in punch_rows:
        eid = pr["employee_id"]
        if eid not in punches_by_emp:
            punches_by_emp[eid] = []
        t_str = pr["punch_time"].split(" ")[1] if " " in pr["punch_time"] else pr["punch_time"]
        punches_by_emp[eid].append({
            "id": pr["id"],
            "time": t_str,
            "full_time": pr["punch_time"],
            "type": pr["punch_type"]
        })

    now_dt = datetime.now()
    today_date_str = now_dt.strftime("%Y-%m-%d")
    now_hm = now_dt.strftime("%H:%M")

    for r in records:
        end_hm = r.get("end_time") or "18:00"
        is_shift_ended = (selected_date < today_date_str) or (selected_date == today_date_str and now_hm >= end_hm)

        r_punches = punches_by_emp.get(r["employee_id"], [])
        r["punches"] = r_punches
        r["punch_count"] = len(r_punches)
        if r_punches:
            r["punch_in"] = r_punches[0]["full_time"]
            out_candidates = [
                p for idx, p in enumerate(r_punches)
                if (p.get("type") == "OUT") or (p.get("type") != "IN" and idx % 2 == 1)
            ]
            last_p = r_punches[-1]
            is_last_in = (last_p.get("type") == "IN") or (last_p.get("type") != "OUT" and (len(r_punches) - 1) % 2 == 0)
            if not is_last_in and out_candidates:
                r["punch_out"] = out_candidates[-1]["full_time"]
                r["is_on_duty"] = False
            elif is_last_in and out_candidates:
                r["punch_out"] = out_candidates[-1]["full_time"]
                r["is_on_duty"] = False if is_shift_ended else True
            else:
                if is_shift_ended:
                    r["punch_out"] = f"{selected_date} {end_hm}:00"
                    r["is_on_duty"] = False
                else:
                    r["punch_out"] = None
                    r["is_on_duty"] = True
        else:
            r["punch_in"] = None
            r["punch_out"] = None
            r["is_on_duty"] = False

        # Admin Employee handling: Exempt from mandatory attendance tracking
        if r.get("is_admin"):
            if not r.get("punch_in"):
                r["status"] = "EXEMPT"
            r["has_missed_mid_punch"] = 0
            r["mid_punch_status"] = "NORMAL"
        else:
            # Check for Missed Middle Punch:
            # If an employee punched in at morning (e.g. around 9 am) and logged out later (e.g. > 4 hrs or afternoon/evening),
            # but missed intermediate break/lunch punches (e.g. exactly 2 punches total for the day with span >= 4 hrs,
            # or odd unpaired punches in the middle):
            p_in_str = r.get("punch_in")
            p_out_str = r.get("punch_out")
            punch_cnt = r.get("punch_count", 0)

            detected_missed_mid = False
            if p_in_str and p_out_str and p_in_str != p_out_str and punch_cnt >= 2:
                try:
                    dt_in = datetime.strptime(p_in_str, "%Y-%m-%d %H:%M:%S" if len(p_in_str) > 16 else "%Y-%m-%d %H:%M")
                    dt_out = datetime.strptime(p_out_str, "%Y-%m-%d %H:%M:%S" if len(p_out_str) > 16 else "%Y-%m-%d %H:%M")
                    span_hrs = (dt_out - dt_in).total_seconds() / 3600.0
                    
                    if dt_in.hour <= 11 and span_hrs >= 4.0:
                        if punch_cnt == 2 or (punch_cnt % 2 != 0):
                            detected_missed_mid = True
                except Exception:
                    pass

            if detected_missed_mid:
                r["has_missed_mid_punch"] = 1
                if r.get("mid_punch_status") != "APPROVED":
                    r["mid_punch_status"] = "FLAGGED"
                    r["mid_punch_notes"] = "Morning clock-in & evening logout recorded with missing intermediate break punches."
                
                # Update database record if exists
                if r.get("record_id"):
                    cursor.execute("""
                        UPDATE attendance_records
                        SET has_missed_mid_punch = 1,
                            mid_punch_status = CASE WHEN mid_punch_status = 'APPROVED' THEN 'APPROVED' ELSE 'FLAGGED' END,
                            mid_punch_notes = COALESCE(mid_punch_notes, 'Morning clock-in & evening logout recorded with missing intermediate break punches.')
                        WHERE id = ?
                    """, (r["record_id"],))

    conn.commit()

    # Fetch missing punch gap alerts for this selected date
    cursor.execute("""
        SELECT id, employee_id, date, gap_type, expected_time, suggested_punch_type,
               suggested_timestamp, description, status, approved_by
        FROM missing_punch_alerts
        WHERE date = ?
    """, (selected_date,))
    alerts_by_emp = {}
    for a_row in cursor.fetchall():
        eid = a_row["employee_id"]
        if eid not in alerts_by_emp:
            alerts_by_emp[eid] = []
        alerts_by_emp[eid].append(dict(a_row))

    for r in records:
        r_alerts = alerts_by_emp.get(r["employee_id"], [])
        r["missing_gaps"] = r_alerts
        r["pending_missing_gaps"] = [a for a in r_alerts if a["status"] == "PENDING"]
        r["approved_missing_gaps"] = [a for a in r_alerts if a["status"] == "APPROVED"]

    # Accurately compute stats for all active workforce on selected date
    total_count = len(records)
    exempt_count = sum(1 for r in records if r.get("is_admin") or r.get("status") == "EXEMPT")
    punched_count = sum(1 for r in records if r.get("punch_count", 0) > 0 or r.get("punch_in"))
    present_count = sum(1 for r in records if r.get("status") in ("PRESENT", "LATE") or (r.get("punch_count", 0) > 0 and not r.get("is_admin")))
    late_count = sum(1 for r in records if r.get("status") == "LATE" or ((r.get("late_mins") or 0) > 0 and not r.get("is_admin")))
    half_day_count = sum(1 for r in records if r.get("status") == "HALF_DAY")
    off_count = sum(1 for r in records if r.get("status") in ("WEEKLY_OFF", "HOLIDAY", "ON_LEAVE", "LEAVE"))
    leave_count = off_count + half_day_count
    absent_count = sum(1 for r in records if r.get("status") == "ABSENT" and r.get("punch_count", 0) == 0 and not r.get("is_admin"))
    total_ot_hours = sum(r.get("raw_ot_hours", 0.0) for r in records)
    total_allowance = sum(r.get("shift_allowance_amount", 0.0) for r in records)
    pending_mid_punch_count = sum(1 for r in records if r.get("has_missed_mid_punch") and r.get("mid_punch_status") == "FLAGGED")
    pending_missing_gap_count = sum(len(r.get("pending_missing_gaps", [])) for r in records)

    stats = {
        "total_count": total_count,
        "exempt_count": exempt_count,
        "punched_count": punched_count,
        "present_count": present_count,
        "late_count": late_count,
        "half_day_count": half_day_count,
        "absent_count": absent_count,
        "off_count": off_count,
        "leave_count": leave_count,
        "total_ot_hours": total_ot_hours,
        "total_allowance": total_allowance,
        "pending_mid_punch_count": pending_mid_punch_count,
        "pending_missing_gap_count": pending_missing_gap_count
    }

    # Get dropdown options
    cursor.execute("SELECT * FROM departments ORDER BY name")
    departments = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT id, emp_no, first_name, last_name, default_shift_id FROM employees WHERE is_active = 1 ORDER BY first_name")
    active_employees = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT * FROM shifts WHERE is_active = 1 ORDER BY name")
    shifts = [dict(row) for row in cursor.fetchall()]

    conn.close()

    selected_status = request.args.get("status", "ALL")

    return render_template(
        "attendance/index.html",
        records=records,
        stats=stats,
        selected_date=selected_date,
        prev_date=prev_date,
        next_date=next_date,
        today_date=today_date,
        departments=departments,
        shifts=shifts,
        active_employees=active_employees,
        selected_dept=dept_id,
        selected_status=selected_status
    )

@attendance_bp.route("/record-punch", methods=["POST"])
def record_punch():
    """
    Web punch simulator.
    Can record a single punch or evaluate a full in/out attendance block.
    """
    employee_id = int(request.form.get("employee_id"))
    punch_date = request.form.get("date", date.today().strftime("%Y-%m-%d"))
    punch_in = request.form.get("punch_in")
    punch_out = request.form.get("punch_out")
    shift_id = request.form.get("shift_id")

    conn = get_db_connection()
    cursor = conn.cursor()

    # If shift_id not provided, look up from roster or employee default
    if not shift_id:
        cursor.execute("SELECT shift_id, is_off_day FROM roster_schedules WHERE employee_id = ? AND date = ?", (employee_id, punch_date))
        roster = cursor.fetchone()
        if roster and roster["shift_id"]:
            shift_id = roster["shift_id"]
        else:
            cursor.execute("SELECT default_shift_id FROM employees WHERE id = ?", (employee_id,))
            emp_row = cursor.fetchone()
            shift_id = emp_row["default_shift_id"] if emp_row else None

    # Fetch shift details
    cursor.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,))
    shift_row = cursor.fetchone()
    shift_info = dict(shift_row) if shift_row else {
        "start_time": "09:00", "end_time": "18:00", "is_overnight": 0,
        "grace_late_mins": 15, "grace_early_mins": 15, "break_mins": 60,
        "min_hours_half_day": 4.5, "min_hours_full_day": 8.0, "allowance_rate": 0.0
    }

    # Evaluate attendance
    eval_res = evaluate_attendance(punch_date, shift_info, punch_in, punch_out)

    cursor.execute("""
        INSERT INTO attendance_records (
            employee_id, date, shift_id, punch_in, punch_out,
            work_hours, late_mins, early_leave_mins, status,
            shift_allowance_amount, raw_ot_hours, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Web Punch Entry')
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
        employee_id, punch_date, shift_id, punch_in, punch_out,
        eval_res["work_hours"], eval_res["late_mins"], eval_res["early_leave_mins"],
        eval_res["status"], eval_res["shift_allowance"], eval_res["total_ot_hours"]
    ))
    conn.commit()

    # Sync Overtime
    cursor.execute("SELECT id FROM attendance_records WHERE employee_id = ? AND date = ?", (employee_id, punch_date))
    att_rec = cursor.fetchone()
    if att_rec:
        sync_overtime_from_attendance(conn, att_rec["id"])

    conn.close()
    flash("Punch recorded & attendance evaluated successfully!", "success")
    return redirect(url_for("attendance.index", date=punch_date))

@attendance_bp.route("/import-logs", methods=["POST"])
def import_logs():
    """
    Bulk Biometric Punch Log Importer.
    Supports CSV upload or raw text lines in format:
    EMP_NO, YYYY-MM-DD HH:MM:SS, IN/OUT
    """
    raw_text = request.form.get("raw_text", "").strip()
    file = request.files.get("csv_file")

    lines = []
    if file and file.filename:
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        reader = csv.reader(stream)
        for row in reader:
            if len(row) >= 3:
                lines.append(row)
    elif raw_text:
        for line in raw_text.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                lines.append(parts)

    if not lines:
        flash("No valid punch lines found to import.", "warning")
        return redirect(url_for("attendance.index"))

    conn = get_db_connection()
    cursor = conn.cursor()

    # Preload employee map: emp_no -> employee dict
    cursor.execute("SELECT * FROM employees WHERE is_active = 1")
    emp_map = {e["emp_no"]: dict(e) for e in cursor.fetchall()}

    # Preload shifts
    cursor.execute("SELECT * FROM shifts")
    shift_map = {s["id"]: dict(s) for s in cursor.fetchall()}

    processed = 0
    # Group punches by (emp_no, shift_date)
    # Basic pairing logic: earliest IN and latest OUT for each date
    day_punches = {} # (emp_no, date) -> {'IN': [], 'OUT': []}

    for row in lines:
        emp_no = row[0].strip().upper()
        punch_time_str = row[1].strip()
        punch_type = row[2].strip().upper()

        if emp_no not in emp_map:
            continue

        try:
            dt = datetime.strptime(punch_time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.strptime(punch_time_str, "%Y-%m-%d %H:%M")
                punch_time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

        # For cross-midnight night shifts, if punch is OUT between 04:00 and 10:00,
        # it may belong to previous day's shift
        shift_date_str = dt.strftime("%Y-%m-%d")
        if punch_type == "OUT" and dt.hour < 11:
            prev_date = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
            # If an IN punch exists for yesterday, attribute this OUT to yesterday
            if (emp_no, prev_date) in day_punches and day_punches[(emp_no, prev_date)]["IN"]:
                shift_date_str = prev_date

        key = (emp_no, shift_date_str)
        if key not in day_punches:
            day_punches[key] = {"IN": [], "OUT": []}

        if punch_type in ("IN", "OUT"):
            day_punches[key][punch_type].append(punch_time_str)

    # Now evaluate each day
    for (emp_no, s_date), p_dict in day_punches.items():
        emp = emp_map[emp_no]
        emp_id = emp["id"]

        p_in = min(p_dict["IN"]) if p_dict["IN"] else None
        p_out = max(p_dict["OUT"]) if p_dict["OUT"] else None

        # Look up roster shift
        cursor.execute("SELECT shift_id, is_off_day FROM roster_schedules WHERE employee_id = ? AND date = ?", (emp_id, s_date))
        roster = cursor.fetchone()
        shift_id = roster["shift_id"] if (roster and roster["shift_id"]) else emp["default_shift_id"]
        is_off = roster["is_off_day"] if roster else 0

        shift_info = shift_map.get(shift_id, {})

        eval_res = evaluate_attendance(s_date, shift_info, p_in, p_out, is_off_day=bool(is_off))

        cursor.execute("""
            INSERT INTO attendance_records (
                employee_id, date, shift_id, punch_in, punch_out,
                work_hours, late_mins, early_leave_mins, status,
                shift_allowance_amount, raw_ot_hours, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Imported Biometric Punch')
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
        conn.commit()

        cursor.execute("SELECT id FROM attendance_records WHERE employee_id = ? AND date = ?", (emp_id, s_date))
        att_rec = cursor.fetchone()
        if att_rec and eval_res["total_ot_hours"] > 0:
            sync_overtime_from_attendance(conn, att_rec["id"])

        processed += 1

    conn.close()
    flash(f"Successfully processed {processed} daily attendance evaluations from biometric log!", "success")
    return redirect(url_for("attendance.index"))

@attendance_bp.route("/sync-machine", methods=["POST"])
def sync_machine():
    device_ip = request.form.get("device_ip", "192.168.101.201").strip()
    days_back = int(request.form.get("days_back", 30))
    from models.auto_sync import get_syncer
    syncer = get_syncer()
    syncer.device_ip = device_ip
    syncer.days_back = days_back
    res = syncer.sync_now(trigger_source="MANUAL_BUTTON")
    if res.get("success"):
        new_punches = res.get("new_punches_added", 0)
        flash(f"Connected to ZKTeco machine at {device_ip}! Synced {res['total_device_logs']:,} total logs ({new_punches} new punches saved, {res['days_evaluated']} days evaluated, {res['ot_claims_created']} OT claims).", "success")
    else:
        flash(f"Failed to connect to machine at {device_ip}: {res.get('error')}", "danger")
    return redirect(request.referrer or url_for("attendance.index"))

@attendance_bp.route("/auto-sync-status")
def auto_sync_status():
    """Returns real-time auto-sync status and countdown seconds."""
    from models.auto_sync import get_auto_sync_status
    status = get_auto_sync_status()
    return jsonify(status)

@attendance_bp.route("/toggle-auto-sync", methods=["POST"])
def toggle_auto_sync():
    """Enable, disable, or adjust auto-sync interval."""
    from models.auto_sync import get_syncer
    syncer = get_syncer()

    # Determine desired state
    req_enabled = request.form.get("enabled")
    if req_enabled is not None:
        enabled = req_enabled in ("1", "true", "True", "on")
    else:
        # Toggle current state if not explicitly specified
        enabled = not syncer.enabled

    interval = request.form.get("interval")
    interval_int = int(interval) if (interval and interval.isdigit()) else syncer.interval_seconds

    syncer.set_enabled(enabled, interval_int)

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(syncer.get_status())

    state_str = "ENABLED (Every 1 Minute)" if enabled else "PAUSED"
    flash(f"Biometric Punch Auto-Sync is now {state_str}.", "info")
    return redirect(request.referrer or url_for("attendance.index"))

@attendance_bp.route("/trigger-sync", methods=["POST"])
def trigger_sync():
    """Triggers an immediate background or direct sync."""
    from models.auto_sync import trigger_sync_now, get_auto_sync_status
    res = trigger_sync_now()
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        status = get_auto_sync_status()
        status["result"] = res
        return jsonify(status)

    if res.get("success"):
        flash(f"Biometric sync complete! {res.get('total_device_logs', 0):,} logs synced ({res.get('new_punches_added', 0)} new punches).", "success")
    else:
        flash(f"Biometric sync error: {res.get('error', 'Device error')}", "danger")
    return redirect(request.referrer or url_for("attendance.index"))

@attendance_bp.route("/sync-machine-users", methods=["POST"])
def sync_machine_users():
    device_ip = request.form.get("device_ip", "192.168.101.201").strip()
    from models.auto_sync import SYNC_LOCK
    from models.biometric_sync import sync_device_users
    with SYNC_LOCK:
        res = sync_device_users(ip=device_ip)
    if res["success"]:
        flash(f"Enrolled staff synced from machine ({device_ip})! Total: {res['total_device_users']} (Added: {res['added_employees']}, Existing: {res['existing_employees']}).", "success")
    else:
        flash(f"Failed to sync users from machine at {device_ip}: {res.get('error')}", "danger")
    return redirect(request.referrer or url_for("employees.list_employees"))

@attendance_bp.route("/test-machine")
def test_machine():
    device_ip = request.args.get("ip", "192.168.101.201").strip()
    from models.auto_sync import SYNC_LOCK
    from models.biometric_sync import test_device_connection
    with SYNC_LOCK:
        res = test_device_connection(ip=device_ip)
    return jsonify(res)


@attendance_bp.route("/in-out-report")
def in_out_report():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Determine default date range
    cursor.execute("SELECT MAX(date) as max_d, MIN(date) as min_d FROM attendance_records")
    date_ext = cursor.fetchone()
    latest_db_date = date_ext["max_d"] or date.today().strftime("%Y-%m-%d")

    try:
        latest_dt = datetime.strptime(latest_db_date, "%Y-%m-%d").date()
    except ValueError:
        latest_dt = date.today()

    default_start = (latest_dt - timedelta(days=6)).strftime("%Y-%m-%d")
    default_end = latest_dt.strftime("%Y-%m-%d")

    start_date = request.args.get("start_date", default_start)
    end_date = request.args.get("end_date", default_end)
    dept_id = request.args.get("dept")
    emp_id = request.args.get("emp_id")
    status_filter = request.args.get("status")

    query = """
        SELECT a.*, e.emp_no, e.first_name, e.last_name, e.designation,
               d.name as dept_name, s.name as shift_name, s.code as shift_code,
               s.start_time, s.end_time, s.color as shift_color
        FROM attendance_records a
        JOIN employees e ON a.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN shifts s ON a.shift_id = s.id
        WHERE a.date BETWEEN ? AND ?
    """
    params = [start_date, end_date]

    if dept_id and dept_id.isdigit():
        query += " AND e.department_id = ?"
        params.append(int(dept_id))

    if emp_id and emp_id.isdigit():
        query += " AND e.id = ?"
        params.append(int(emp_id))

    if status_filter:
        if status_filter == "PRESENT":
            query += " AND a.status IN ('PRESENT', 'LATE')"
        elif status_filter == "LEAVE":
            query += " AND a.status IN ('WEEKLY_OFF', 'HOLIDAY', 'LEAVE', 'ON_LEAVE', 'HALF_DAY')"
        elif status_filter == "ABSENT":
            query += " AND a.status = 'ABSENT'"
        else:
            query += " AND a.status = ?"
            params.append(status_filter)

    query += " ORDER BY a.date DESC, e.emp_no ASC"
    cursor.execute(query, params)
    records = [dict(row) for row in cursor.fetchall()]

    # Summary metrics
    total_records = len(records)
    unique_employees = len(set(r["employee_id"] for r in records))
    total_hours = sum(r.get("work_hours", 0.0) for r in records)
    total_ot = sum(r.get("raw_ot_hours", 0.0) for r in records)
    present_records = sum(1 for r in records if r.get("status") in ("PRESENT", "LATE"))

    stats = {
        "total_records": total_records,
        "unique_employees": unique_employees,
        "total_hours": round(total_hours, 1),
        "total_ot": round(total_ot, 1),
        "present_records": present_records
    }

    # Dropdowns
    cursor.execute("SELECT * FROM departments ORDER BY name")
    departments = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT id, emp_no, first_name, last_name FROM employees WHERE is_active = 1 ORDER BY first_name")
    active_employees = [dict(row) for row in cursor.fetchall()]

    conn.close()

    return render_template(
        "attendance/in_out_report.html",
        records=records,
        stats=stats,
        start_date=start_date,
        end_date=end_date,
        selected_dept=dept_id,
        selected_emp=emp_id,
        selected_status=status_filter,
        departments=departments,
        active_employees=active_employees
    )


@attendance_bp.route("/export-in-out-csv")
def export_in_out_csv():
    conn = get_db_connection()
    cursor = conn.cursor()

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    dept_id = request.args.get("dept")
    emp_id = request.args.get("emp_id")
    status_filter = request.args.get("status")

    if not start_date or not end_date:
        cursor.execute("SELECT MAX(date) as max_d FROM attendance_records")
        row = cursor.fetchone()
        end_date = row["max_d"] or date.today().strftime("%Y-%m-%d")
        start_date = (datetime.strptime(end_date, "%Y-%m-%d").date() - timedelta(days=6)).strftime("%Y-%m-%d")

    query = """
        SELECT a.date, e.emp_no, e.first_name, e.last_name, d.name as dept_name,
               COALESCE(s.code, 'GEN') as shift_code,
               a.punch_in, a.punch_out, a.work_hours, a.late_mins, a.early_leave_mins,
               a.raw_ot_hours, a.status
        FROM attendance_records a
        JOIN employees e ON a.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN shifts s ON a.shift_id = s.id
        WHERE a.date BETWEEN ? AND ?
    """
    params = [start_date, end_date]

    if dept_id and dept_id.isdigit():
        query += " AND e.department_id = ?"
        params.append(int(dept_id))

    if emp_id and emp_id.isdigit():
        query += " AND e.id = ?"
        params.append(int(emp_id))

    if status_filter:
        if status_filter == "PRESENT":
            query += " AND a.status IN ('PRESENT', 'LATE')"
        elif status_filter == "LEAVE":
            query += " AND a.status IN ('WEEKLY_OFF', 'HOLIDAY', 'LEAVE', 'ON_LEAVE', 'HALF_DAY')"
        elif status_filter == "ABSENT":
            query += " AND a.status = 'ABSENT'"
        else:
            query += " AND a.status = ?"
            params.append(status_filter)

    query += " ORDER BY a.date DESC, e.emp_no ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Date", "Employee ID", "Employee Name", "Department", "Shift",
        "Punch In", "Punch Out", "Work Hours", "Late (mins)", "Early Leave (mins)",
        "Overtime (hrs)", "Status"
    ])

    for r in rows:
        p_in = r["punch_in"] if r["punch_in"] else "--:--:--"
        p_out = r["punch_out"] if r["punch_out"] else "--:--:--"
        writer.writerow([
            r["date"],
            r["emp_no"],
            f"{r['first_name']} {r['last_name']}".strip(),
            r["dept_name"],
            r["shift_code"],
            p_in,
            p_out,
            r["work_hours"],
            r["late_mins"],
            r["early_leave_mins"],
            r["raw_ot_hours"],
            r["status"]
        ])

    csv_data = output.getvalue()
    filename = f"Vasantham_Printers_Attendance_InOut_{start_date}_to_{end_date}.csv"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@attendance_bp.route("/matrix")
def matrix():
    conn = get_db_connection()
    cursor = conn.cursor()

    selected_year = request.args.get("year", type=int)
    selected_month = request.args.get("month", type=int)

    if not selected_year or not selected_month:
        cursor.execute("SELECT MAX(date) as max_d FROM attendance_records")
        row = cursor.fetchone()
        if row and row["max_d"]:
            dt = datetime.strptime(row["max_d"], "%Y-%m-%d")
            selected_year = dt.year
            selected_month = dt.month
        else:
            now = datetime.now()
            selected_year = now.year
            selected_month = now.month

    dept_id = request.args.get("dept")

    import calendar
    _, num_days = calendar.monthrange(selected_year, selected_month)
    month_days = list(range(1, num_days + 1))
    month_name = calendar.month_name[selected_month]

    month_start = f"{selected_year:04d}-{selected_month:02d}-01"
    month_end = f"{selected_year:04d}-{selected_month:02d}-{num_days:02d}"

    emp_query = "SELECT e.*, d.name as dept_name FROM employees e JOIN departments d ON e.department_id = d.id WHERE e.is_active = 1"
    emp_params = []
    if dept_id and dept_id.isdigit():
        emp_query += " AND e.department_id = ?"
        emp_params.append(int(dept_id))
    emp_query += " ORDER BY e.emp_no ASC"
    cursor.execute(emp_query, emp_params)
    employees = [dict(r) for r in cursor.fetchall()]

    cursor.execute("""
        SELECT employee_id, date, punch_in, punch_out, work_hours, status, raw_ot_hours
        FROM attendance_records
        WHERE date BETWEEN ? AND ?
    """, (month_start, month_end))
    att_rows = cursor.fetchall()

    att_matrix = {}
    for r in att_rows:
        e_id = r["employee_id"]
        d_num = int(r["date"].split("-")[2])
        if e_id not in att_matrix:
            att_matrix[e_id] = {}
        att_matrix[e_id][d_num] = dict(r)

    employee_data = []
    for emp in employees:
        e_id = emp["id"]
        emp_records = att_matrix.get(e_id, {})
        present_count = 0
        total_hours = 0.0
        total_ot = 0.0

        daily_cells = []
        for day in month_days:
            cell = emp_records.get(day)
            day_str = f"{selected_year:04d}-{selected_month:02d}-{day:02d}"
            try:
                day_dt = datetime.strptime(day_str, "%Y-%m-%d")
                weekday = day_dt.weekday()
            except Exception:
                weekday = 0

            is_sunday = (weekday == 6)

            if cell:
                status = cell["status"]
                p_in_time = cell["punch_in"].split(" ")[1] if cell.get("punch_in") else "--"
                p_out_time = cell["punch_out"].split(" ")[1] if cell.get("punch_out") else "--"
                hours = cell.get("work_hours", 0.0)
                ot = cell.get("raw_ot_hours", 0.0)

                if status in ("PRESENT", "LATE"):
                    present_count += 1
                elif status == "HALF_DAY":
                    present_count += 0.5
                total_hours += hours
                total_ot += ot

                daily_cells.append({
                    "day": day,
                    "date": day_str,
                    "has_record": True,
                    "status": status,
                    "in_time": p_in_time,
                    "out_time": p_out_time,
                    "hours": hours,
                    "ot": ot,
                    "is_sunday": is_sunday
                })
            else:
                default_status = "WO" if is_sunday else "A"
                daily_cells.append({
                    "day": day,
                    "date": day_str,
                    "has_record": False,
                    "status": default_status,
                    "in_time": "--",
                    "out_time": "--",
                    "hours": 0.0,
                    "ot": 0.0,
                    "is_sunday": is_sunday
                })

        employee_data.append({
            "employee": emp,
            "cells": daily_cells,
            "present_days": present_count,
            "total_hours": round(total_hours, 1),
            "total_ot": round(total_ot, 1)
        })

    cursor.execute("SELECT * FROM departments ORDER BY name")
    departments = [dict(r) for r in cursor.fetchall()]
    conn.close()

    return render_template(
        "attendance/matrix.html",
        employee_data=employee_data,
        month_days=month_days,
        selected_year=selected_year,
        selected_month=selected_month,
        month_name=month_name,
        departments=departments,
        selected_dept=dept_id
    )


def resolve_punch_date_range(args, default_date=None):
    """
    Resolves date filter options for 'All Punches per Person':
    - today: current date
    - 7days: last 7 calendar days inclusive (today - 6 days through today)
    - lastweek / lastweak: previous calendar week (Monday to Sunday)
    - lastmonth: entire previous calendar month (1st to last day)
    - custom / date: specific single date or custom range
    Returns (start_date, end_date, range_opt, range_label, is_multi_day)
    """
    today = date.today()
    range_opt = (args.get("range") or "").lower().strip()
    selected_date = args.get("date")

    if range_opt == "today":
        start_date = today.strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")
        range_label = f"Today ({today.strftime('%d %b %Y')})"
    elif range_opt in ("7days", "7_days", "7-days"):
        range_opt = "7days"
        start_dt = today - timedelta(days=6)
        end_dt = today
        start_date = start_dt.strftime("%Y-%m-%d")
        end_date = end_dt.strftime("%Y-%m-%d")
        range_label = f"Last 7 Days ({start_dt.strftime('%d %b')} - {end_dt.strftime('%d %b %Y')})"
    elif range_opt in ("lastweek", "last_week", "last-week", "lastweak"):
        range_opt = "lastweek"
        # Previous week: Monday to Sunday
        curr_mon = today - timedelta(days=today.weekday())
        start_dt = curr_mon - timedelta(days=7)
        end_dt = curr_mon - timedelta(days=1)
        start_date = start_dt.strftime("%Y-%m-%d")
        end_date = end_dt.strftime("%Y-%m-%d")
        range_label = f"Last Week ({start_dt.strftime('%d %b')} - {end_dt.strftime('%d %b %Y')})"
    elif range_opt in ("lastmonth", "last_month", "last-month"):
        range_opt = "lastmonth"
        # 1st to last day of previous calendar month
        first_curr = today.replace(day=1)
        end_dt = first_curr - timedelta(days=1)
        start_dt = end_dt.replace(day=1)
        start_date = start_dt.strftime("%Y-%m-%d")
        end_date = end_dt.strftime("%Y-%m-%d")
        range_label = f"Last Month ({start_dt.strftime('%d %b')} - {end_dt.strftime('%d %b %Y')})"
    elif selected_date:
        range_opt = "custom"
        start_date = selected_date
        end_date = selected_date
        try:
            s_dt = datetime.strptime(selected_date, "%Y-%m-%d").date()
            if s_dt == today:
                range_opt = "today"
                range_label = f"Today ({today.strftime('%d %b %Y')})"
            else:
                range_label = s_dt.strftime('%d %b %Y')
        except Exception:
            range_label = selected_date
    else:
        if default_date:
            start_date = default_date
            end_date = default_date
            range_opt = "custom"
            try:
                s_dt = datetime.strptime(default_date, "%Y-%m-%d").date()
                if s_dt == today:
                    range_opt = "today"
                    range_label = f"Today ({today.strftime('%d %b %Y')})"
                else:
                    range_label = s_dt.strftime('%d %b %Y')
            except Exception:
                range_label = default_date
        else:
            range_opt = "today"
            start_date = today.strftime("%Y-%m-%d")
            end_date = today.strftime("%Y-%m-%d")
            range_label = f"Today ({today.strftime('%d %b %Y')})"

    is_multi_day = (start_date != end_date)
    return start_date, end_date, range_opt, range_label, is_multi_day


def compute_lunch_pattern(day_formatted, day_first_in, day_last_out):
    """
    Analyzes punch stream for midday lunch break patterns (12:30 to 14:45):
    - Identifies completed lunch break, missing return punch, missing out punch, or full-day without lunch break.
    - Suggests target prefill times (e.g. 13:00 / 14:00) for instant manual punch correction.
    """
    if not day_formatted or len(day_formatted) < 1:
        return {"status": "NOT_APPLICABLE", "badge_text": "", "badge_color": "slate"}

    # Find punch OUT around lunch (12:30:00 - 13:45:00)
    lunch_out_punch = next((p for p in day_formatted if "12:30:00" <= p["time"] <= "13:45:00" and ("OUT" in p.get("nature", "") or "Break" in p.get("nature", ""))), None)
    if not lunch_out_punch:
        lunch_out_punch = next((p for p in day_formatted if "12:40:00" <= p["time"] <= "13:30:00"), None)

    lunch_out_time = lunch_out_punch["time"] if lunch_out_punch else None

    # Find punch IN around lunch (13:30:00 - 14:45:00, strictly following lunch_out if present)
    lunch_in_punch = None
    if lunch_out_time:
        lunch_in_punch = next((p for p in day_formatted if p["time"] > lunch_out_time and p["time"] <= "14:45:00" and ("IN" in p.get("nature", "") or "Return" in p.get("nature", ""))), None)
        if not lunch_in_punch:
            lunch_in_punch = next((p for p in day_formatted if p["time"] > lunch_out_time and p["time"] <= "14:45:00"), None)
    else:
        lunch_in_punch = next((p for p in day_formatted if "13:35:00" <= p["time"] <= "14:35:00" and ("IN" in p.get("nature", "") or "Return" in p.get("nature", ""))), None)
        if not lunch_in_punch:
            lunch_in_punch = next((p for p in day_formatted if "13:45:00" <= p["time"] <= "14:30:00"), None)

    lunch_in_time = lunch_in_punch["time"] if lunch_in_punch else None

    if lunch_out_time and lunch_in_time:
        try:
            dt1 = datetime.strptime(lunch_out_time, "%H:%M:%S")
            dt2 = datetime.strptime(lunch_in_time, "%H:%M:%S")
            diff_mins = int((dt2 - dt1).total_seconds() / 60)
        except Exception:
            diff_mins = 60
        
        hrs = diff_mins // 60
        mins = diff_mins % 60
        dur_str = f"{hrs}h {mins}m" if hrs > 0 else f"{mins}m"
        color = "emerald" if 30 <= diff_mins <= 75 else ("amber" if diff_mins > 75 else "blue")
        return {
            "status": "COMPLETED",
            "out_time": lunch_out_time[:5],
            "in_time": lunch_in_time[:5],
            "duration_mins": diff_mins,
            "badge_color": color,
            "badge_text": f"Lunch: {lunch_out_time[:5]} – {lunch_in_time[:5]} ({dur_str})",
            "suggested_time": None,
            "suggested_type": None,
            "suggested_label": None
        }
    elif lunch_out_time and not lunch_in_time:
        return {
            "status": "MISSING_RETURN",
            "out_time": lunch_out_time[:5],
            "in_time": None,
            "duration_mins": None,
            "badge_color": "rose",
            "badge_text": f"Missing Lunch Return (Out at {lunch_out_time[:5]})",
            "suggested_time": "14:00:00",
            "suggested_type": "IN",
            "suggested_label": "+ Add Return (~14:00)"
        }
    elif not lunch_out_time and lunch_in_time:
        return {
            "status": "MISSING_OUT",
            "out_time": None,
            "in_time": lunch_in_time[:5],
            "duration_mins": None,
            "badge_color": "amber",
            "badge_text": f"Missing Lunch Out (Returned at {lunch_in_time[:5]})",
            "suggested_time": "13:00:00",
            "suggested_type": "OUT",
            "suggested_label": "+ Add Lunch Out (~13:00)"
        }
    else:
        if day_first_in and day_first_in < "12:00:00" and day_last_out and day_last_out > "15:00:00":
            return {
                "status": "NO_LUNCH_LOGGED",
                "out_time": None,
                "in_time": None,
                "duration_mins": None,
                "badge_color": "slate",
                "badge_text": "No Lunch Break Logged (09:00 - 18:00)",
                "suggested_time": "14:00:00",
                "suggested_type": "IN",
                "suggested_label": "+ Add Lunch In"
            }
        return {
            "status": "NOT_APPLICABLE",
            "badge_text": "",
            "badge_color": "slate",
            "suggested_time": None,
            "suggested_type": None,
            "suggested_label": None
        }


@attendance_bp.route("/all-punches")
def all_punches():
    conn = get_db_connection()
    cursor = conn.cursor()

    dept_id = request.args.get("dept")
    emp_id = request.args.get("emp_id")

    start_date, end_date, range_opt, range_label, is_multi_day = resolve_punch_date_range(request.args)

    today_date = date.today().strftime("%Y-%m-%d")
    try:
        cur_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        cur_dt = date.today()

    prev_date = (cur_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (cur_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    emp_query = """
        SELECT e.id, e.emp_no, e.first_name, e.last_name, e.designation,
               d.name as dept_name,
               COALESCE(s_roster.code, s_def.code, 'GEN') as shift_code,
               COALESCE(s_roster.name, s_def.name, 'General Shift') as shift_name,
               COALESCE(s_roster.start_time, s_def.start_time, '09:00') as start_time,
               COALESCE(s_roster.end_time, s_def.end_time, '18:00') as end_time,
               COALESCE(s_roster.color, s_def.color, '#2563eb') as shift_color,
               a.work_hours, a.status, a.late_mins, a.raw_ot_hours
        FROM employees e
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN roster_schedules r ON r.employee_id = e.id AND r.date = ?
        LEFT JOIN shifts s_roster ON r.shift_id = s_roster.id
        LEFT JOIN shifts s_def ON e.default_shift_id = s_def.id
        LEFT JOIN attendance_records a ON a.employee_id = e.id AND a.date = ?
        WHERE e.is_active = 1
    """
    emp_params = [end_date, end_date]

    if dept_id and dept_id.isdigit():
        emp_query += " AND e.department_id = ?"
        emp_params.append(int(dept_id))

    if emp_id and emp_id.isdigit():
        emp_query += " AND e.id = ?"
        emp_params.append(int(emp_id))

    emp_query += " ORDER BY e.emp_no ASC"
    cursor.execute(emp_query, emp_params)
    employees = [dict(r) for r in cursor.fetchall()]

    punch_query = """
        SELECT p.*, e.emp_no, e.first_name, e.last_name, d.name as dept_name
        FROM attendance_punches p
        JOIN employees e ON p.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        WHERE SUBSTR(p.punch_time, 1, 10) BETWEEN ? AND ?
    """
    punch_params = [start_date, end_date]

    if dept_id and dept_id.isdigit():
        punch_query += " AND e.department_id = ?"
        punch_params.append(int(dept_id))

    if emp_id and emp_id.isdigit():
        punch_query += " AND e.id = ?"
        punch_params.append(int(emp_id))

    punch_query += " ORDER BY p.punch_time ASC"
    cursor.execute(punch_query, punch_params)
    all_raw_punches = [dict(r) for r in cursor.fetchall()]

    punches_by_emp = {}
    for p in all_raw_punches:
        eid = p["employee_id"]
        if eid not in punches_by_emp:
            punches_by_emp[eid] = []
        punches_by_emp[eid].append(p)

    emp_punch_cards = []
    total_punches_day = len(all_raw_punches)
    total_active_staff = len(punches_by_emp)

    # Fetch missing punch alerts in period
    cursor.execute("""
        SELECT id, employee_id, date, gap_type, expected_time, suggested_punch_type,
               suggested_timestamp, description, status, approved_by
        FROM missing_punch_alerts
        WHERE date BETWEEN ? AND ?
    """, (start_date, end_date))
    alerts_by_emp_date = {}
    for a_row in cursor.fetchall():
        key = (a_row["employee_id"], a_row["date"])
        if key not in alerts_by_emp_date:
            alerts_by_emp_date[key] = []
        alerts_by_emp_date[key].append(dict(a_row))

    for emp in employees:
        eid = emp["id"]
        raw_list = punches_by_emp.get(eid, [])

        # Group punches by date: { '2026-09-12': [...], '2026-09-11': [...] }
        punches_by_date = {}
        for p in raw_list:
            p_date = p["punch_time"][:10]
            if p_date not in punches_by_date:
                punches_by_date[p_date] = []
            punches_by_date[p_date].append(p)

        sorted_dates = sorted(punches_by_date.keys(), reverse=True)

        day_groups = []
        formatted_punches = []

        for p_date in sorted_dates:
            day_raw_list = punches_by_date[p_date]
            day_formatted = []
            day_last_dt = None

            for i, p in enumerate(day_raw_list):
                try:
                    p_dt = datetime.strptime(p["punch_time"], "%Y-%m-%d %H:%M:%S")
                    time_display = p_dt.strftime("%I:%M:%S %p")
                    time_24 = p_dt.strftime("%H:%M:%S")
                    datetime_display = p_dt.strftime("%d %b, %I:%M %p")
                except Exception:
                    time_24 = p["punch_time"]
                    time_display = p["punch_time"]
                    datetime_display = p["punch_time"]
                    p_dt = None

                interval_str = ""
                if day_last_dt and p_dt:
                    diff_sec = int((p_dt - day_last_dt).total_seconds())
                    hrs = diff_sec // 3600
                    mins = (diff_sec % 3600) // 60
                    if hrs > 0:
                        interval_str = f"+{hrs}h {mins}m"
                    else:
                        interval_str = f"+{mins}m"

                p_type = (p.get("punch_type") or "").upper()
                if p_type == "IN":
                    is_in_punch = True
                elif p_type == "OUT":
                    is_in_punch = False
                else:
                    is_in_punch = (i % 2 == 0)

                if i == 0 and is_in_punch:
                    nature = "Shift Clock IN"
                    nature_color = "emerald"
                    icon = "fa-sign-in-alt"
                elif not is_in_punch:
                    if i == len(day_raw_list) - 1 and (len(day_raw_list) % 2 == 0 or p_type == "OUT"):
                        nature = "Shift Clock OUT"
                        nature_color = "indigo"
                        icon = "fa-sign-out-alt"
                    else:
                        nature = "Break / Departure OUT"
                        nature_color = "amber"
                        icon = "fa-mug-saucer"
                else:
                    nature = "Break Return / Duty IN"
                    nature_color = "blue"
                    icon = "fa-arrow-right-to-bracket"

                p_item = {
                    "id": p.get("id"),
                    "raw_time": p.get("punch_time"),
                    "punch_date": p_date,
                    "datetime_display": datetime_display,
                    "punch_type": p.get("punch_type") or "PUNCH",
                    "punch_num": i + 1,
                    "time": time_24,
                    "time_ampm": time_display,
                    "nature": nature,
                    "nature_color": nature_color,
                    "icon": icon,
                    "interval": interval_str,
                    "device_id": p.get("device_id") or "192.168.101.201"
                }
                day_formatted.append(p_item)
                formatted_punches.append(p_item)
                if p_dt:
                    day_last_dt = p_dt

            try:
                d_obj = datetime.strptime(p_date, "%Y-%m-%d").date()
                d_label = d_obj.strftime("%d %b %Y (%a)")
            except Exception:
                d_label = p_date

            day_first_in = day_formatted[0]["time"] if day_formatted else "--:--:--"
            day_outs = [p for p in day_formatted if "OUT" in p["nature"]]
            day_last_out = day_outs[-1]["time"] if day_outs else ("No Out Punch" if len(day_formatted) > 1 else "--:--:--")

            day_lunch = compute_lunch_pattern(day_formatted, day_first_in, day_last_out)
            day_alerts = alerts_by_emp_date.get((eid, p_date), [])
            day_pending_gaps = [a for a in day_alerts if a["status"] == "PENDING"]
            day_approved_gaps = [a for a in day_alerts if a["status"] == "APPROVED"]

            day_groups.append({
                "date": p_date,
                "date_label": d_label,
                "punches": day_formatted,
                "punch_count": len(day_formatted),
                "first_in": day_first_in,
                "last_out": day_last_out,
                "lunch": day_lunch,
                "missing_gaps": day_alerts,
                "pending_gaps": day_pending_gaps,
                "approved_gaps": day_approved_gaps
            })

        first_in = formatted_punches[0]["time"] if formatted_punches else "--:--:--"
        out_punches = [p for p in formatted_punches if "OUT" in p["nature"]]
        last_out = out_punches[-1]["time"] if out_punches else ("No Out Punch" if len(formatted_punches) > 1 else "--:--:--")

        is_currently_on_duty = False
        if formatted_punches and not is_multi_day:
            if "IN" in formatted_punches[-1]["nature"]:
                is_currently_on_duty = True

        # Calculate employee's typical lunch habit across the measured range
        completed_lunches = [d["lunch"] for d in day_groups if d["lunch"].get("status") == "COMPLETED"]
        if completed_lunches:
            avg_dur = round(sum(d["duration_mins"] for d in completed_lunches) / len(completed_lunches))
            typical_lunch = {
                "has_pattern": True,
                "sample_days": len(completed_lunches),
                "avg_duration": avg_dur,
                "avg_out": completed_lunches[0]["out_time"],
                "avg_in": completed_lunches[0]["in_time"],
                "summary": f"Typical Habit: ~{completed_lunches[0]['out_time']} to ~{completed_lunches[0]['in_time']} ({avg_dur}m)"
            }
        else:
            typical_lunch = {
                "has_pattern": False,
                "sample_days": 0,
                "avg_duration": 60,
                "avg_out": "13:00",
                "avg_in": "14:00",
                "summary": "Standard Factory Lunch: 1:00 PM – 2:00 PM (60m)"
            }

        # Collect all pending gaps across all days in period for this card
        card_pending_gaps = [a for d in day_groups for a in d.get("pending_gaps", [])]
        if not is_multi_day:
            card_pending_gaps = alerts_by_emp_date.get((eid, start_date), [])

        emp_punch_cards.append({
            "employee": emp,
            "punches": formatted_punches,
            "punch_count": len(formatted_punches),
            "days_present_count": len(day_groups),
            "day_groups": day_groups,
            "first_in": first_in,
            "last_out": last_out,
            "is_on_duty": is_currently_on_duty,
            "has_punches": len(formatted_punches) > 0,
            "typical_lunch": typical_lunch,
            "pending_gaps": card_pending_gaps
        })

    cursor.execute("SELECT * FROM departments ORDER BY name")
    departments = [dict(r) for r in cursor.fetchall()]

    cursor.execute("SELECT id, emp_no, first_name, last_name FROM employees WHERE is_active = 1 ORDER BY first_name")
    active_employees = [dict(r) for r in cursor.fetchall()]

    conn.close()

    return render_template(
        "attendance/all_punches.html",
        emp_punch_cards=emp_punch_cards,
        all_raw_punches=all_raw_punches,
        total_punches_day=total_punches_day,
        total_active_staff=total_active_staff,
        selected_date=start_date if not is_multi_day else end_date,
        start_date=start_date,
        end_date=end_date,
        range_opt=range_opt,
        range_label=range_label,
        is_multi_day=is_multi_day,
        prev_date=prev_date,
        next_date=next_date,
        today_date=today_date,
        departments=departments,
        active_employees=active_employees,
        selected_dept=dept_id,
        selected_emp=emp_id
    )


@attendance_bp.route("/export-all-punches-csv")
def export_all_punches_csv():
    conn = get_db_connection()
    cursor = conn.cursor()

    dept_id = request.args.get("dept")
    emp_id = request.args.get("emp_id")

    start_date, end_date, range_opt, range_label, is_multi_day = resolve_punch_date_range(request.args)

    query = """
        SELECT p.punch_time, e.emp_no, e.first_name, e.last_name, d.name as dept_name,
               p.punch_type, p.device_id
        FROM attendance_punches p
        JOIN employees e ON p.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        WHERE SUBSTR(p.punch_time, 1, 10) BETWEEN ? AND ?
    """
    params = [start_date, end_date]

    if dept_id and dept_id.isdigit():
        query += " AND e.department_id = ?"
        params.append(int(dept_id))

    if emp_id and emp_id.isdigit():
        query += " AND e.id = ?"
        params.append(int(emp_id))

    query += " ORDER BY p.punch_time ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Date", "Exact Timestamp", "Employee ID", "Employee Name", "Department",
        "Punch Type", "Biometric Terminal Device"
    ])

    for r in rows:
        writer.writerow([
            r["punch_time"][:10],
            r["punch_time"],
            r["emp_no"],
            f"{r['first_name']} {r['last_name']}".strip(),
            r["dept_name"],
            r["punch_type"],
            r["device_id"]
        ])

    csv_data = output.getvalue()
    if start_date == end_date:
        filename = f"Vasantham_Printers_All_Punches_{start_date}.csv"
    else:
        filename = f"Vasantham_Printers_All_Punches_{start_date}_to_{end_date}.csv"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


def recompute_employee_day_attendance(conn, employee_id, punch_date):
    """
    Re-evaluates and updates attendance_records for a given employee and date
    based on all current punches in attendance_punches.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT punch_time, punch_type 
        FROM attendance_punches 
        WHERE employee_id = ? AND punch_time LIKE ?
        ORDER BY punch_time ASC
    """, (employee_id, f"{punch_date}%"))
    punches = cursor.fetchall()

    # Look up roster or employee default shift
    cursor.execute("SELECT shift_id, is_off_day FROM roster_schedules WHERE employee_id = ? AND date = ?", (employee_id, punch_date))
    roster = cursor.fetchone()
    if roster and roster["shift_id"]:
        shift_id = roster["shift_id"]
        is_off = roster["is_off_day"]
    else:
        cursor.execute("SELECT default_shift_id, weekly_off_day FROM employees WHERE id = ?", (employee_id,))
        emp_row = cursor.fetchone()
        shift_id = emp_row["default_shift_id"] if emp_row else None
        
        # Check if weekly off
        try:
            p_dt = datetime.strptime(punch_date, "%Y-%m-%d")
            is_off = 1 if (emp_row and emp_row["weekly_off_day"] == p_dt.weekday()) else 0
        except Exception:
            is_off = 0

    cursor.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,))
    shift_row = cursor.fetchone()
    shift_info = dict(shift_row) if shift_row else {
        "start_time": "09:00", "end_time": "18:00", "is_overnight": 0,
        "grace_late_mins": 15, "grace_early_mins": 15, "break_mins": 60,
        "min_hours_half_day": 4.5, "min_hours_full_day": 8.0, "allowance_rate": 0.0
    }

    now_dt = datetime.now()
    today_date_str = now_dt.strftime("%Y-%m-%d")
    now_hm = now_dt.strftime("%H:%M")
    sched_end_hm = shift_info.get("end_time", "18:00")
    is_shift_ended = (punch_date < today_date_str) or (punch_date == today_date_str and now_hm >= sched_end_hm)

    if punches:
        p_in = punches[0]["punch_time"]
        out_punches = [
            p["punch_time"] for idx, p in enumerate(punches)
            if (p["punch_type"] == "OUT") or (p["punch_type"] != "IN" and idx % 2 == 1)
        ]
        last_punch = punches[-1]
        is_last_in = (last_punch["punch_type"] == "IN") or (last_punch["punch_type"] != "OUT" and (len(punches) - 1) % 2 == 0)

        # If employee has clocked out (last punch is OUT or even completed count)
        if not is_last_in and out_punches:
            p_out = out_punches[-1]
        else:
            if is_shift_ended:
                # Per factory floor policy: on 6 pm shift ends no punch required
                p_out = f"{punch_date} {sched_end_hm}:00"
            else:
                p_out = None
    else:
        p_in = None
        p_out = None

    eval_res = evaluate_attendance(punch_date, shift_info, p_in, p_out, is_off_day=bool(is_off), all_punches=punches)

    cursor.execute("""
        INSERT INTO attendance_records (
            employee_id, date, shift_id, punch_in, punch_out,
            work_hours, late_mins, late_deduction_mins, early_leave_mins, status,
            shift_allowance_amount, gross_ot_hours, raw_ot_hours, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Recalculated from Punches')
        ON CONFLICT(employee_id, date) DO UPDATE SET
            shift_id = excluded.shift_id,
            punch_in = excluded.punch_in,
            punch_out = excluded.punch_out,
            work_hours = excluded.work_hours,
            late_mins = excluded.late_mins,
            late_deduction_mins = excluded.late_deduction_mins,
            early_leave_mins = excluded.early_leave_mins,
            status = excluded.status,
            shift_allowance_amount = excluded.shift_allowance_amount,
            gross_ot_hours = excluded.gross_ot_hours,
            raw_ot_hours = excluded.raw_ot_hours,
            notes = excluded.notes
    """, (
        employee_id, punch_date, shift_id, p_in, p_out,
        eval_res["work_hours"], eval_res["late_mins"], eval_res.get("late_deduction_mins", 0),
        eval_res["early_leave_mins"], eval_res["status"], eval_res["shift_allowance"],
        eval_res.get("gross_ot_hours", 0.0), eval_res["total_ot_hours"]
    ))
    conn.commit()

    cursor.execute("SELECT id FROM attendance_records WHERE employee_id = ? AND date = ?", (employee_id, punch_date))
    att_rec = cursor.fetchone()
    if att_rec:
        sync_overtime_from_attendance(conn, att_rec["id"])


@attendance_bp.route("/manual-punch", methods=["POST"])
def manual_punch():
    """
    Dedicated Manual Punch IN / OUT entry route with mandatory reason & audit logging.
    """
    employee_id = request.form.get("employee_id")
    punch_type = request.form.get("punch_type", "IN").strip().upper()
    punch_date = request.form.get("date", date.today().strftime("%Y-%m-%d")).strip()
    punch_time_raw = request.form.get("time", "").strip()
    reason = request.form.get("reason", "").strip()
    edited_by = request.form.get("edited_by", "Supervisor / HR Manager").strip()

    if not employee_id or not employee_id.isdigit():
        flash("Invalid employee selection.", "danger")
        return redirect(request.referrer or url_for("attendance.index"))

    employee_id = int(employee_id)

    if not reason:
        flash("Mandatory Audit Requirement: You must provide a valid justification reason for recording a manual punch.", "danger")
        return redirect(request.referrer or url_for("attendance.index"))

    # Format time
    if not punch_time_raw:
        punch_time_raw = datetime.now().strftime("%H:%M:%S")
    elif len(punch_time_raw) == 5:
        punch_time_raw += ":00"

    full_timestamp = f"{punch_date} {punch_time_raw}"

    # Validate timestamp format
    try:
        datetime.strptime(full_timestamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        flash("Invalid timestamp format. Expected YYYY-MM-DD and HH:MM:SS.", "danger")
        return redirect(request.referrer or url_for("attendance.index"))

    if punch_type not in ("IN", "OUT"):
        punch_type = "IN"

    conn = get_db_connection()
    cursor = conn.cursor()

    # Check for existing punch at this exact second; if collision, shift by seconds so there is strictly NO LIMIT on punches
    cursor.execute("SELECT id FROM attendance_punches WHERE employee_id = ? AND punch_time = ?", (employee_id, full_timestamp))
    if cursor.fetchone():
        dt_obj = datetime.strptime(full_timestamp, "%Y-%m-%d %H:%M:%S")
        for offset in range(1, 60):
            candidate = (dt_obj + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("SELECT id FROM attendance_punches WHERE employee_id = ? AND punch_time = ?", (employee_id, candidate))
            if not cursor.fetchone():
                full_timestamp = candidate
                break

    # Insert into raw punches
    cursor.execute("""
        INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id, notes)
        VALUES (?, ?, ?, 'MANUAL_ENTRY', ?)
    """, (employee_id, full_timestamp, punch_type, f"Manual {punch_type}: {reason}"))
    conn.commit()
    new_punch_id = cursor.lastrowid

    # Create immutable audit log entry
    edit_action = f"MANUAL_{punch_type}"
    cursor.execute("""
        INSERT INTO punch_edit_logs (
            punch_id, employee_id, edit_type, target_field,
            old_value, new_value, reason, edited_by
        ) VALUES (?, ?, ?, 'punch_time', NULL, ?, ?, ?)
    """, (new_punch_id, employee_id, edit_action, full_timestamp, reason, edited_by))
    conn.commit()

    # Recompute day's attendance
    recompute_employee_day_attendance(conn, employee_id, punch_date)

    # Auto-resolve any matching missing punch alerts
    try:
        from models.missing_punch_detector import auto_resolve_alerts_for_punch
        auto_resolve_alerts_for_punch(employee_id, punch_date, conn=conn)
    except Exception:
        pass

    # Get employee details and current punch count for friendly notification
    cursor.execute("SELECT emp_no, first_name, last_name FROM employees WHERE id = ?", (employee_id,))
    emp_info = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) as cnt FROM attendance_punches WHERE employee_id = ? AND punch_time LIKE ?", (employee_id, f"{punch_date}%"))
    total_punches_count = cursor.fetchone()["cnt"]
    conn.close()

    emp_name = f"{emp_info['first_name']} {emp_info['last_name']}" if emp_info else f"Employee #{employee_id}"
    flash(f"Manual Punch {punch_type} successfully recorded for {emp_name} at {full_timestamp} (Punch #{total_punches_count} today - No Limit). Immutable Audit record created.", "success")
    return redirect(request.referrer or url_for("attendance.all_punches", date=punch_date))


@attendance_bp.route("/edit-punch/<int:punch_id>", methods=["POST"])
def edit_punch(punch_id):
    """
    Edits a punch timestamp or type with mandatory reason and immutable audit log.
    Punches cannot be deleted.
    """
    new_timestamp = request.form.get("new_time", "").strip()
    new_punch_type = request.form.get("punch_type", "PUNCH").strip().upper()
    reason = request.form.get("reason", "").strip()
    edited_by = request.form.get("edited_by", "Supervisor / HR Manager").strip()

    if not reason:
        flash("Audit Policy Requirement: A documented reason is required to modify attendance punches.", "danger")
        return redirect(request.referrer or url_for("attendance.all_punches"))

    # Validate timestamp format
    try:
        dt = datetime.strptime(new_timestamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.strptime(new_timestamp, "%Y-%m-%d %H:%M")
            new_timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            flash("Invalid timestamp format. Expected 'YYYY-MM-DD HH:MM:SS'.", "danger")
            return redirect(request.referrer or url_for("attendance.all_punches"))

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM attendance_punches WHERE id = ?", (punch_id,))
    old_punch = cursor.fetchone()

    if not old_punch:
        conn.close()
        flash("Punch record not found.", "danger")
        return redirect(request.referrer or url_for("attendance.all_punches"))

    old_time = old_punch["punch_time"]
    old_type = old_punch["punch_type"]
    employee_id = old_punch["employee_id"]
    old_date = old_time.split(" ")[0] if " " in old_time else old_time[:10]
    new_date = new_timestamp.split(" ")[0]

    # Check if target timestamp already exists on another punch for this employee
    cursor.execute("SELECT id FROM attendance_punches WHERE employee_id = ? AND punch_time = ? AND id != ?", (employee_id, new_timestamp, punch_id))
    if cursor.fetchone():
        conn.close()
        flash(f"Another punch already exists for this employee at {new_timestamp}. Please choose a distinct timestamp.", "danger")
        return redirect(request.referrer or url_for("attendance.all_punches"))

    # Update punch
    cursor.execute("""
        UPDATE attendance_punches
        SET punch_time = ?, punch_type = ?, notes = ?
        WHERE id = ?
    """, (new_timestamp, new_punch_type, f"Edited: {reason}", punch_id))

    # Log to immutable punch_edit_logs
    cursor.execute("""
        INSERT INTO punch_edit_logs (
            punch_id, employee_id, edit_type, target_field,
            old_value, new_value, reason, edited_by
        ) VALUES (?, ?, 'PUNCH_EDIT', 'punch_time', ?, ?, ?, ?)
    """, (punch_id, employee_id, old_time, new_timestamp, reason, edited_by))
    conn.commit()

    # Recompute attendance for affected dates
    recompute_employee_day_attendance(conn, employee_id, old_date)
    if new_date != old_date:
        recompute_employee_day_attendance(conn, employee_id, new_date)

    conn.close()
    flash(f"Punch successfully modified from '{old_time}' to '{new_timestamp}'. Change permanently recorded in Audit Log.", "success")
    return redirect(request.referrer or url_for("attendance.all_punches", date=new_date))


@attendance_bp.route("/delete-punch/<int:punch_id>", methods=["GET", "POST", "DELETE"])
def delete_punch(punch_id):
    """
    Delete an erroneous/accidental punch with mandatory justification reason,
    permanently recording the deletion in punch_edit_logs and recalculating day attendance.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM attendance_punches WHERE id = ?", (punch_id,))
    punch = cursor.fetchone()
    if not punch:
        conn.close()
        flash("Punch record not found or already deleted.", "warning")
        return redirect(request.referrer or url_for("attendance.all_punches"))

    # Extract reason and author
    reason = (request.form.get("reason") or request.args.get("reason") or "").strip()
    edited_by = (request.form.get("edited_by") or request.args.get("edited_by") or "Supervisor / HR Manager").strip()

    if not reason:
        conn.close()
        flash("Mandatory Audit Requirement: A valid justification reason is required to delete an attendance punch.", "danger")
        return redirect(request.referrer or url_for("attendance.all_punches"))

    employee_id = punch["employee_id"]
    punch_time = punch["punch_time"]
    punch_type = punch["punch_type"] or "PUNCH"
    punch_date = punch_time[:10]

    # 1. Permanently record deletion in immutable punch_edit_logs
    cursor.execute("""
        INSERT INTO punch_edit_logs (
            punch_id, employee_id, edit_type, target_field,
            old_value, new_value, reason, edited_by
        ) VALUES (?, ?, 'PUNCH_DELETE', 'punch_record', ?, 'DELETED', ?, ?)
    """, (punch_id, employee_id, f"{punch_time} ({punch_type})", reason, edited_by))

    # 2. Remove row from attendance_punches
    cursor.execute("DELETE FROM attendance_punches WHERE id = ?", (punch_id,))
    conn.commit()

    # 3. Recalculate attendance records for affected day
    recompute_employee_day_attendance(conn, employee_id, punch_date)

    conn.close()
    flash(f"Punch '{punch_time} ({punch_type})' permanently deleted. Deletion record preserved in Audit Log.", "success")
    return redirect(request.referrer or url_for("attendance.all_punches", date=punch_date))


@attendance_bp.route("/delete-record/<int:record_id>", methods=["GET", "POST", "DELETE"])
def delete_record(record_id):
    """
    Strict Compliance Route: Attendance records cannot be deleted.
    """
    flash("Compliance Policy Violation: Attendance day records CANNOT be deleted under statutory audit regulations.", "danger")
    return redirect(request.referrer or url_for("attendance.index"))


@attendance_bp.route("/audit-log")
def audit_log():
    """
    View immutable punch edit and modification audit trail.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT l.*, e.emp_no, e.first_name, e.last_name, d.name as dept_name
        FROM punch_edit_logs l
        JOIN employees e ON l.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        ORDER BY l.created_at DESC, l.id DESC
    """)
    logs = [dict(r) for r in cursor.fetchall()]

    total_edits = len(logs)
    manual_punches = sum(1 for l in logs if "MANUAL" in (l.get("edit_type") or ""))
    punch_edits = sum(1 for l in logs if l.get("edit_type") == "PUNCH_EDIT")
    deleted_punches = sum(1 for l in logs if l.get("edit_type") == "PUNCH_DELETE")
    unique_staff = len(set(l["employee_id"] for l in logs))

    stats = {
        "total_edits": total_edits,
        "manual_punches": manual_punches,
        "punch_edits": punch_edits,
        "deleted_punches": deleted_punches,
        "unique_staff": unique_staff
    }

    conn.close()
    return render_template("attendance/audit_log.html", logs=logs, stats=stats)


@attendance_bp.route("/export-audit-log-csv")
def export_audit_log_csv():
    """
    Export immutable punch audit trail as CSV.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT l.*, e.emp_no, e.first_name, e.last_name, d.name as dept_name
        FROM punch_edit_logs l
        JOIN employees e ON l.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        ORDER BY l.created_at DESC, l.id DESC
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Audit Log ID", "Edit Timestamp", "Employee ID", "Employee Name", "Department",
        "Action Type", "Target Field", "Original Value", "New Adjusted Value",
        "Reason for Modification", "Authorized By"
    ])

    for r in rows:
        writer.writerow([
            r["id"],
            r["created_at"],
            r["emp_no"],
            f"{r['first_name']} {r['last_name']}".strip(),
            r["dept_name"],
            r["edit_type"],
            r["target_field"],
            r["old_value"] or "N/A",
            r["new_value"],
            r["reason"],
            r["edited_by"]
        ])

    csv_data = output.getvalue()
    filename = f"Vasantham_Printers_Attendance_Audit_Log_{date.today().strftime('%Y%m%d')}.csv"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )





@attendance_bp.route("/send-missed-alert", methods=["POST"])
def trigger_missed_punch_alert():
    """Triggers the automated email alert notifying management of missed morning punches."""
    from models.email_alerts import send_missed_morning_punch_alert
    target_date = request.form.get("date") or request.args.get("date")
    res = send_missed_morning_punch_alert(target_date)
    if res.get("success"):
        flash(res.get("message", "Morning missed punch alert email dispatched successfully!"), "success")
    else:
        flash(f"Missed punch alert notice: {res.get('error')}", "warning")
    return redirect(url_for("attendance.index", date=target_date))

@attendance_bp.route("/approve-mid-punch/<int:record_id>", methods=["POST"])
def approve_mid_punch(record_id):
    """Authorizes salary calculation for a day with a missed middle punch."""
    conn = get_db_connection()
    cursor = conn.cursor()

    approver = session.get("username", "Admin")
    cursor.execute("""
        UPDATE attendance_records
        SET mid_punch_status = 'APPROVED',
            mid_punch_approved_by = ?,
            mid_punch_approved_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (approver, record_id))
    conn.commit()

    cursor.execute("SELECT date FROM attendance_records WHERE id = ?", (record_id,))
    row = cursor.fetchone()
    target_date = row["date"] if row else None
    conn.close()

    flash("Middle punch approved! Salary calculation for this day is now authorized.", "success")
    if target_date:
        return redirect(url_for("attendance.index", date=target_date))
    return redirect(url_for("attendance.index"))

@attendance_bp.route("/approve-all-mid-punches", methods=["POST"])
def approve_all_mid_punches():
    """One-click batch approval for all pending missed mid punches on a given date."""
    target_date = request.form.get("date")
    if not target_date:
        flash("Target date is required to approve all middle punches.", "danger")
        return redirect(url_for("attendance.index"))

    conn = get_db_connection()
    cursor = conn.cursor()

    approver = session.get("username", "Admin")
    cursor.execute("""
        UPDATE attendance_records
        SET mid_punch_status = 'APPROVED',
            mid_punch_approved_by = ?,
            mid_punch_approved_at = CURRENT_TIMESTAMP
        WHERE date = ? AND has_missed_mid_punch = 1 AND mid_punch_status != 'APPROVED'
    """, (approver, target_date))
    approved_count = cursor.rowcount
    conn.commit()
    conn.close()

    flash(f"Successfully approved {approved_count} pending middle punch day(s) for {target_date}! Salary is now authorized.", "success")
    return redirect(url_for("attendance.index", date=target_date))


# ========================================================
# MISSING ATTENDANCE PUNCHES: GAP REVIEW & 1-CLICK APPROVALS
# ========================================================

@attendance_bp.route("/missing-punches")
def missing_punches_portal():
    """
    Dedicated portal for reviewing detected shift pattern gaps and approving
    corrections for specific dates and times with a single click.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    status_filter = request.args.get("status", "PENDING").upper()
    dept_filter = request.args.get("dept")
    date_filter = request.args.get("date")
    gap_type_filter = request.args.get("gap_type")

    # Metrics counts
    cursor.execute("SELECT COUNT(*) as cnt FROM missing_punch_alerts WHERE status = 'PENDING'")
    pending_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM missing_punch_alerts WHERE status = 'APPROVED'")
    approved_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM missing_punch_alerts WHERE status = 'DISMISSED'")
    dismissed_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM missing_punch_alerts")
    total_count = cursor.fetchone()["cnt"]

    # Main query
    query = """
        SELECT m.*, e.emp_no, e.first_name, e.last_name, e.designation,
               d.name as dept_name,
               COALESCE(s.code, 'GEN') as shift_code,
               COALESCE(s.name, 'General Shift') as shift_name,
               COALESCE(s.start_time, '09:00') as start_time,
               COALESCE(s.end_time, '18:00') as end_time
        FROM missing_punch_alerts m
        JOIN employees e ON m.employee_id = e.id
        LEFT JOIN departments d ON e.department_id = d.id
        LEFT JOIN shifts s ON e.default_shift_id = s.id
        WHERE 1=1
    """
    params = []

    if status_filter != "ALL":
        query += " AND m.status = ?"
        params.append(status_filter)

    if dept_filter and dept_filter.isdigit():
        query += " AND e.department_id = ?"
        params.append(int(dept_filter))

    if date_filter:
        query += " AND m.date = ?"
        params.append(date_filter)

    if gap_type_filter:
        query += " AND m.gap_type = ?"
        params.append(gap_type_filter)

    query += " ORDER BY m.date DESC, m.gap_type ASC, e.emp_no ASC"
    cursor.execute(query, params)
    raw_alerts = [dict(r) for r in cursor.fetchall()]

    # Attach day's existing punches to each alert so manager can inspect the full timeline
    alerts = []
    for a in raw_alerts:
        cursor.execute("""
            SELECT id, punch_time, punch_type, device_id
            FROM attendance_punches
            WHERE employee_id = ? AND punch_time LIKE ?
            ORDER BY punch_time ASC
        """, (a["employee_id"], f"{a['date']}%"))
        day_punches = [dict(p) for p in cursor.fetchall()]
        a["day_punches"] = day_punches
        a["day_punches_count"] = len(day_punches)
        alerts.append(a)

    cursor.execute("SELECT * FROM departments ORDER BY name")
    departments = [dict(r) for r in cursor.fetchall()]

    conn.close()

    return render_template(
        "attendance/missing_punches.html",
        alerts=alerts,
        status_filter=status_filter,
        dept_filter=dept_filter,
        date_filter=date_filter,
        gap_type_filter=gap_type_filter,
        departments=departments,
        pending_count=pending_count,
        approved_count=approved_count,
        dismissed_count=dismissed_count,
        total_count=total_count
    )


@attendance_bp.route("/missing-punches/approve/<int:alert_id>", methods=["POST"])
def approve_missing_punch(alert_id):
    """
    1-Click Approval for a specific missing punch gap:
    Inserts the suggested timestamped punch, creates immutable audit log,
    recalculates daily attendance and OT ledger, and marks alert as APPROVED.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT m.*, e.emp_no, e.first_name, e.last_name
        FROM missing_punch_alerts m
        JOIN employees e ON m.employee_id = e.id
        WHERE m.id = ?
    """, (alert_id,))
    alert = cursor.fetchone()

    if not alert:
        conn.close()
        flash("Alert not found.", "danger")
        return redirect(url_for("attendance.missing_punches_portal"))

    if alert["status"] == "APPROVED":
        conn.close()
        flash("This correction has already been approved.", "info")
        return redirect(url_for("attendance.missing_punches_portal"))

    approver = session.get("username", "Manager / Admin")
    emp_id = alert["employee_id"]
    p_date = alert["date"]
    p_type = alert["suggested_punch_type"]
    target_ts = alert["suggested_timestamp"]
    desc = alert["description"] or f"1-Click Correction for {alert['gap_type']}"

    # Check for exact timestamp collision; shift by seconds if needed
    cursor.execute("SELECT id FROM attendance_punches WHERE employee_id = ? AND punch_time = ?", (emp_id, target_ts))
    if cursor.fetchone():
        dt_obj = datetime.strptime(target_ts, "%Y-%m-%d %H:%M:%S")
        for offset in range(1, 60):
            candidate = (dt_obj + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("SELECT id FROM attendance_punches WHERE employee_id = ? AND punch_time = ?", (emp_id, candidate))
            if not cursor.fetchone():
                target_ts = candidate
                break

    # 1. Insert punch into attendance_punches
    cursor.execute("""
        INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id, notes)
        VALUES (?, ?, ?, 'SYSTEM_CORRECTION', ?)
    """, (emp_id, target_ts, p_type, f"1-Click Approved Correction: {desc} (by {approver})"))
    new_punch_id = cursor.lastrowid

    # 2. Immutable audit log entry
    cursor.execute("""
        INSERT INTO punch_edit_logs (
            punch_id, employee_id, edit_type, target_field,
            old_value, new_value, reason, edited_by
        ) VALUES (?, ?, ?, 'punch_time', NULL, ?, ?, ?)
    """, (
        new_punch_id, emp_id, f"1CLICK_CORRECTION_{p_type}", target_ts,
        f"Approved missing punch correction: {desc}", approver
    ))

    # 3. Recompute day attendance
    recompute_employee_day_attendance(conn, emp_id, p_date)

    # 4. Mark alert APPROVED
    cursor.execute("""
        UPDATE missing_punch_alerts
        SET status = 'APPROVED',
            approved_by = ?,
            approved_at = CURRENT_TIMESTAMP,
            inserted_punch_id = ?
        WHERE id = ?
    """, (approver, new_punch_id, alert_id))

    # 5. Auto-resolve any sister alerts for this employee on that date if cleared
    from models.missing_punch_detector import auto_resolve_alerts_for_punch
    auto_resolve_alerts_for_punch(emp_id, p_date, conn=conn)

    conn.commit()
    conn.close()

    flash(f"✓ 1-Click Approved: Recorded {p_type} punch at {target_ts} for {alert['first_name']} {alert['last_name']} ({alert['emp_no']}). Attendance & payroll synchronized!", "success")
    return redirect(request.referrer or url_for("attendance.index"))


@attendance_bp.route("/missing-punches/approve-all", methods=["POST"])
def approve_all_missing_punches():
    """
    1-Click Batch Approval: Approves all currently pending missing punch corrections.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT m.*, e.emp_no, e.first_name, e.last_name
        FROM missing_punch_alerts m
        JOIN employees e ON m.employee_id = e.id
        WHERE m.status = 'PENDING'
        ORDER BY m.date ASC, m.id ASC
    """)
    pending = [dict(r) for r in cursor.fetchall()]

    if not pending:
        conn.close()
        flash("No pending missing punch corrections to approve.", "info")
        return redirect(request.referrer or url_for("attendance.index"))

    approver = session.get("username", "Manager / Admin")
    approved_count = 0
    from models.missing_punch_detector import auto_resolve_alerts_for_punch

    for a in pending:
        emp_id = a["employee_id"]
        p_date = a["date"]
        p_type = a["suggested_punch_type"]
        target_ts = a["suggested_timestamp"]
        desc = a["description"] or f"Batch 1-Click Correction for {a['gap_type']}"

        # Collision check
        cursor.execute("SELECT id FROM attendance_punches WHERE employee_id = ? AND punch_time = ?", (emp_id, target_ts))
        if cursor.fetchone():
            dt_obj = datetime.strptime(target_ts, "%Y-%m-%d %H:%M:%S")
            for offset in range(1, 60):
                candidate = (dt_obj + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("SELECT id FROM attendance_punches WHERE employee_id = ? AND punch_time = ?", (emp_id, candidate))
                if not cursor.fetchone():
                    target_ts = candidate
                    break

        cursor.execute("""
            INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id, notes)
            VALUES (?, ?, ?, 'SYSTEM_CORRECTION', ?)
        """, (emp_id, target_ts, p_type, f"Batch 1-Click Approved: {desc} (by {approver})"))
        new_punch_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO punch_edit_logs (
                punch_id, employee_id, edit_type, target_field,
                old_value, new_value, reason, edited_by
            ) VALUES (?, ?, ?, 'punch_time', NULL, ?, ?, ?)
        """, (
            new_punch_id, emp_id, f"1CLICK_CORRECTION_{p_type}", target_ts,
            f"Batch approved missing punch: {desc}", approver
        ))

        recompute_employee_day_attendance(conn, emp_id, p_date)

        cursor.execute("""
            UPDATE missing_punch_alerts
            SET status = 'APPROVED',
                approved_by = ?,
                approved_at = CURRENT_TIMESTAMP,
                inserted_punch_id = ?
            WHERE id = ?
        """, (approver, new_punch_id, a["id"]))

        auto_resolve_alerts_for_punch(emp_id, p_date, conn=conn)
        approved_count += 1

    conn.commit()
    conn.close()

    flash(f"✓ 1-Click Success: Approved and inserted all {approved_count} missing punch corrections! Attendance and payroll updated.", "success")
    return redirect(request.referrer or url_for("attendance.index"))


@attendance_bp.route("/missing-punches/dismiss/<int:alert_id>", methods=["POST"])
def dismiss_missing_punch(alert_id):
    """
    Dismisses a missing punch alert (e.g. employee was genuinely absent or left early).
    """
    reason = request.form.get("reason", "Unapproved Absence / Dismissed by Manager").strip()
    approver = session.get("username", "Manager / Admin")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE missing_punch_alerts
        SET status = 'DISMISSED',
            approved_by = ?,
            approved_at = CURRENT_TIMESTAMP,
            description = description || ' [Dismissed: ' || ? || ']'
        WHERE id = ? AND status = 'PENDING'
    """, (approver, reason, alert_id))
    conn.commit()
    conn.close()

    flash("Missing punch gap alert dismissed.", "info")
    return redirect(request.referrer or url_for("attendance.index"))


@attendance_bp.route("/missing-punches/scan-now", methods=["POST"])
def scan_missing_punches_now():
    """
    Manually triggers full biometric shift pattern gap analysis across recent days.
    """
    from models.missing_punch_detector import scan_and_record_missing_punches
    days_back = int(request.form.get("days", 7))
    res = scan_and_record_missing_punches(days_back=days_back)
    flash(f"Scan complete: {res['new_alerts_count']} new missing punch gap(s) flagged. Total pending: {res['total_pending']}.", "success")
    return redirect(request.referrer or url_for("attendance.index"))


@attendance_bp.route("/missing-punches/send-alert-email", methods=["POST"])
def send_missing_punch_email_now():
    """
    Manually dispatches an automated notification email to the manager.
    """
    from models.email_alerts import send_missing_punch_manager_notification
    base_url = request.host_url.rstrip('/')
    res = send_missing_punch_manager_notification(server_base_url=base_url)
    if res.get("success"):
        flash(f"Notification email dispatched: {res['message']}", "success")
    else:
        flash(f"Failed to send email alert: {res.get('error')}", "warning")
    return redirect(request.referrer or url_for("attendance.index"))


