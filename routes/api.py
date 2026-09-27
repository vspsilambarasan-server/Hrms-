from flask import Blueprint, jsonify, request, redirect, url_for, flash
from datetime import datetime, date
from database import get_db_connection
from models.shift_engine import evaluate_attendance
from models.ot_engine import sync_overtime_from_attendance

api_bp = Blueprint("api", __name__, url_prefix="/api")

@api_bp.route("/punch-now", methods=["POST"])
def punch_now():
    data = request.get_json() or {}
    employee_id = data.get("employee_id")
    punch_type = data.get("punch_type", "IN").upper() # 'IN' or 'OUT'

    if not employee_id:
        return jsonify({"success": False, "error": "Employee ID is required"}), 400

    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    today_str = now.strftime("%Y-%m-%d")

    conn = get_db_connection()
    cursor = conn.cursor()

    # Record raw punch
    cursor.execute("""
        INSERT INTO attendance_punches (employee_id, punch_time, punch_type, device_id)
        VALUES (?, ?, ?, 'DASHBOARD_WIDGET')
    """, (employee_id, now_str, punch_type))

    # Look up shift
    cursor.execute("SELECT shift_id, is_off_day FROM roster_schedules WHERE employee_id = ? AND date = ?", (employee_id, today_str))
    roster = cursor.fetchone()
    if roster and roster["shift_id"]:
        shift_id = roster["shift_id"]
        is_off = roster["is_off_day"]
    else:
        cursor.execute("SELECT default_shift_id FROM employees WHERE id = ?", (employee_id,))
        emp_row = cursor.fetchone()
        shift_id = emp_row["default_shift_id"] if emp_row else None
        is_off = 0

    cursor.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,))
    shift_row = cursor.fetchone()
    shift_info = dict(shift_row) if shift_row else {
        "start_time": "09:00", "end_time": "18:00", "is_overnight": 0,
        "grace_late_mins": 10, "grace_early_mins": 15, "break_mins": 60,
        "min_hours_half_day": 4.5, "min_hours_full_day": 8.0, "allowance_rate": 0.0
    }

    # Fetch existing attendance record for today
    cursor.execute("SELECT * FROM attendance_records WHERE employee_id = ? AND date = ?", (employee_id, today_str))
    att_row = cursor.fetchone()

    if att_row:
        p_in = att_row["punch_in"] if punch_type == "OUT" else now_str
        p_out = now_str if punch_type == "OUT" else att_row["punch_out"]
    else:
        p_in = now_str if punch_type == "IN" else None
        p_out = now_str if punch_type == "OUT" else None

    eval_res = evaluate_attendance(today_str, shift_info, p_in, p_out, is_off_day=bool(is_off))

    cursor.execute("""
        INSERT INTO attendance_records (
            employee_id, date, shift_id, punch_in, punch_out,
            work_hours, late_mins, late_deduction_mins, early_leave_mins, status,
            shift_allowance_amount, gross_ot_hours, raw_ot_hours, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Live Web Punch')
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
        employee_id, today_str, shift_id, p_in, p_out,
        eval_res["work_hours"], eval_res["late_mins"], eval_res.get("late_deduction_mins", 0),
        eval_res["early_leave_mins"], eval_res["status"], eval_res["shift_allowance"],
        eval_res.get("gross_ot_hours", 0.0), eval_res["total_ot_hours"]
    ))
    conn.commit()

    cursor.execute("SELECT id FROM attendance_records WHERE employee_id = ? AND date = ?", (employee_id, today_str))
    att_rec = cursor.fetchone()
    if att_rec and eval_res["total_ot_hours"] > 0:
        sync_overtime_from_attendance(conn, att_rec["id"])

    conn.close()

    return jsonify({
        "success": True,
        "message": f"Recorded Punch {punch_type} at {now.strftime('%H:%M:%S')}",
        "punch_time": now_str,
        "status": eval_res["status"],
        "work_hours": eval_res["work_hours"],
        "late_mins": eval_res["late_mins"],
        "ot_hours": eval_res["total_ot_hours"]
    })

@api_bp.route("/charts-data")
def charts_data():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Department OT Cost
    cursor.execute("""
        SELECT d.name as dept_name,
               ROUND(SUM(o.approved_hours), 1) as total_ot_hours,
               ROUND(SUM(o.calculated_ot_pay), 2) as total_ot_cost
        FROM overtime_records o
        JOIN employees e ON o.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        WHERE o.status = 'APPROVED'
        GROUP BY d.id
    """)
    dept_ot = [dict(row) for row in cursor.fetchall()]

    # Shift distribution
    cursor.execute("""
        SELECT s.name, s.color, COUNT(e.id) as emp_count
        FROM shifts s
        LEFT JOIN employees e ON s.id = e.default_shift_id AND e.is_active = 1
        GROUP BY s.id
    """)
    shifts_dist = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return jsonify({
        "dept_ot": dept_ot,
        "shifts_dist": shifts_dist
    })

@api_bp.route("/clear-all-data", methods=["POST"])
def clear_data_endpoint():
    from clear_data import clear_all_data
    clear_all_data()
    flash("All database records cleared. System is in a clean fresh state.", "warning")
    return redirect(request.referrer or url_for("dashboard.index"))

@api_bp.route("/load-sample-data", methods=["POST"])
def seed_data_endpoint():
    from seed_data import seed_database
    seed_database()
    flash("Sample departments, employees, shifts, punches, and payroll loaded successfully!", "success")
    return redirect(request.referrer or url_for("dashboard.index"))

