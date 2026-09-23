from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from datetime import datetime
from database import get_db_connection
from models.ot_engine import compute_ot_payout, calculate_hourly_rate

overtime_bp = Blueprint("overtime", __name__, url_prefix="/overtime")

@overtime_bp.route("/")
def list_overtime():
    conn = get_db_connection()
    cursor = conn.cursor()

    status_filter = request.args.get("status", "ALL").upper()
    dept_filter = request.args.get("dept")
    date_filter = request.args.get("date")
    emp_filter = request.args.get("emp_id")

    query = """
        SELECT o.*, e.emp_no, e.first_name, e.last_name, e.designation,
               d.name as dept_name, a.work_hours, a.status as attendance_status,
               a.punch_in, a.punch_out,
               COALESCE(s.name, s_def.name, 'General Shift') as shift_name,
               COALESCE(s.code, s_def.code, 'GEN') as shift_code,
               COALESCE(s.start_time, s_def.start_time, '09:00') as shift_start,
               COALESCE(s.end_time, s_def.end_time, '18:00') as shift_end,
               COALESCE(s.is_overnight, s_def.is_overnight, 0) as is_overnight
        FROM overtime_records o
        JOIN employees e ON o.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN attendance_records a ON (o.attendance_id = a.id OR (o.attendance_id IS NULL AND a.employee_id = o.employee_id AND a.date = o.date))
        LEFT JOIN shifts s ON a.shift_id = s.id
        LEFT JOIN shifts s_def ON e.default_shift_id = s_def.id
        WHERE 1=1
    """
    params = []

    if status_filter in ("PENDING", "APPROVED", "REJECTED"):
        query += " AND o.status = ?"
        params.append(status_filter)

    if dept_filter and dept_filter.isdigit():
        query += " AND e.department_id = ?"
        params.append(int(dept_filter))

    if date_filter:
        query += " AND o.date = ?"
        params.append(date_filter)

    if emp_filter and emp_filter.isdigit():
        query += " AND o.employee_id = ?"
        params.append(int(emp_filter))

    query += " ORDER BY o.date DESC, o.id DESC"
    cursor.execute(query, params)
    raw_overtime_records = [dict(row) for row in cursor.fetchall()]

    # Format dates and punch timings for each OT record
    overtime_records = []
    for r in raw_overtime_records:
        date_str = r.get("date", "")

        # Fallback to punches table if attendance record punch_in is missing
        if not r.get("punch_in") and date_str:
            cursor.execute("SELECT MIN(punch_time) as p_in, MAX(punch_time) as p_out FROM attendance_punches WHERE employee_id = ? AND punch_time LIKE ?", (r["employee_id"], f"{date_str}%"))
            fb = cursor.fetchone()
            if fb and fb["p_in"]:
                r["punch_in"] = fb["p_in"]
                r["punch_out"] = fb["p_out"]
        r["date_weekday"] = ""
        r["date_short"] = date_str
        r["is_weekend"] = False
        if date_str:
            try:
                dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
                r["date_weekday"] = dt_obj.strftime("%A")
                r["date_short"] = dt_obj.strftime("%d %b %Y")
                r["is_weekend"] = (dt_obj.weekday() in (5, 6)) # Sat or Sun
            except Exception:
                pass

        # In time formatting
        p_in = r.get("punch_in")
        r["in_time"] = "--:--"
        r["in_ampm"] = "--:--"
        if p_in:
            try:
                dt_in = datetime.strptime(p_in, "%Y-%m-%d %H:%M:%S")
                r["in_time"] = dt_in.strftime("%H:%M:%S")
                r["in_ampm"] = dt_in.strftime("%I:%M %p")
            except Exception:
                r["in_time"] = p_in.split(" ")[1] if " " in p_in else p_in
                r["in_ampm"] = r["in_time"]

        # Out time formatting
        p_out = r.get("punch_out")
        r["out_time"] = "--:--"
        r["out_ampm"] = "--:--"
        r["is_next_day"] = False
        r["out_date_str"] = ""
        if p_out:
            try:
                dt_out = datetime.strptime(p_out, "%Y-%m-%d %H:%M:%S")
                r["out_time"] = dt_out.strftime("%H:%M:%S")
                r["out_ampm"] = dt_out.strftime("%I:%M %p")
                out_d = dt_out.strftime("%Y-%m-%d")
                if out_d != date_str:
                    r["is_next_day"] = True
                    r["out_date_str"] = dt_out.strftime("%d %b")
            except Exception:
                r["out_time"] = p_out.split(" ")[1] if " " in p_out else p_out
                r["out_ampm"] = r["out_time"]

        shift_s = r.get("shift_start") or "09:00"
        shift_e = r.get("shift_end") or "18:00"
        r["shift_display"] = f"{shift_s} - {shift_e}"
        r["has_punches"] = bool(p_in or p_out)
        r["pre_ot_val"] = float(r.get("pre_shift_ot_hours") or 0.0)
        r["post_ot_val"] = float(r.get("post_shift_ot_hours") or 0.0)

        overtime_records.append(r)

    # Stats cards
    cursor.execute("""
        SELECT 
            COUNT(*) as total_count,
            COUNT(CASE WHEN status = 'PENDING' THEN 1 END) as pending_count,
            COALESCE(SUM(CASE WHEN status = 'PENDING' THEN total_ot_hours END), 0.0) as pending_hours,
            COUNT(CASE WHEN status = 'APPROVED' THEN 1 END) as approved_count,
            COALESCE(SUM(CASE WHEN status = 'APPROVED' THEN approved_hours END), 0.0) as approved_hours,
            COALESCE(SUM(CASE WHEN status = 'APPROVED' THEN calculated_ot_pay END), 0.0) as approved_cost
        FROM overtime_records
    """)
    stats = dict(cursor.fetchone())

    # Company settings
    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    company_settings = dict(cursor.fetchone() or {})

    # Departments
    cursor.execute("SELECT * FROM departments ORDER BY name")
    departments = [dict(row) for row in cursor.fetchall()]

    # Active employees for filtering
    cursor.execute("SELECT id, emp_no, first_name, last_name FROM employees WHERE is_active = 1 ORDER BY first_name")
    active_employees = [dict(row) for row in cursor.fetchall()]

    # Available distinct dates for quick filter
    cursor.execute("SELECT DISTINCT date FROM overtime_records ORDER BY date DESC LIMIT 30")
    available_dates = [r["date"] for r in cursor.fetchall()]

    conn.close()

    return render_template(
        "overtime/index.html",
        overtime_records=overtime_records,
        stats=stats,
        settings=company_settings,
        departments=departments,
        active_employees=active_employees,
        available_dates=available_dates,
        status_filter=status_filter,
        selected_dept=dept_filter,
        selected_date=date_filter,
        selected_emp=emp_filter
    )

@overtime_bp.route("/<int:ot_id>/action", methods=["POST"])
def review_action(ot_id):
    action = request.form.get("action") # 'APPROVE' or 'REJECT'
    approved_hours = request.form.get("approved_hours")
    remarks = request.form.get("remarks", "").strip()

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT o.*, e.salary_type, e.base_salary, e.hourly_rate
        FROM overtime_records o
        JOIN employees e ON o.employee_id = e.id
        WHERE o.id = ?
    """, (ot_id,))
    ot = cursor.fetchone()
    if not ot:
        flash("Overtime record not found.", "danger")
        conn.close()
        return redirect(url_for("overtime.list_overtime"))

    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    settings = dict(cursor.fetchone() or {})
    hourly_rate = calculate_hourly_rate(dict(ot), settings)

    if action == "APPROVE":
        hrs = float(approved_hours) if approved_hours else float(ot["total_ot_hours"])
        ot_pay = compute_ot_payout(hrs, hourly_rate, float(ot["multiplier"]))
        cursor.execute("""
            UPDATE overtime_records
            SET status = 'APPROVED', approved_hours = ?, calculated_ot_pay = ?,
                reason = CASE WHEN ? != '' THEN ? ELSE reason END,
                approved_by = 'HR Manager', approved_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (hrs, ot_pay, remarks, remarks, ot_id))
        flash(f"Overtime request #{ot_id} approved ({hrs} hrs)!", "success")
    elif action == "REJECT":
        cursor.execute("""
            UPDATE overtime_records
            SET status = 'REJECTED', approved_hours = 0.0, calculated_ot_pay = 0.0,
                reason = ?, approved_by = 'HR Manager', approved_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (remarks or "Rejected by HR", ot_id))
        flash(f"Overtime request #{ot_id} rejected.", "warning")

    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("overtime.list_overtime"))

@overtime_bp.route("/batch-approve", methods=["POST"])
def batch_approve():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM overtime_records WHERE status = 'PENDING'")
    pending_records = cursor.fetchall()

    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    settings = dict(cursor.fetchone() or {})

    count = 0
    for r in pending_records:
        ot_id = r["id"]
        cursor.execute("""
            SELECT o.*, e.salary_type, e.base_salary, e.hourly_rate
            FROM overtime_records o
            JOIN employees e ON o.employee_id = e.id
            WHERE o.id = ?
        """, (ot_id,))
        ot = cursor.fetchone()
        hourly_rate = calculate_hourly_rate(dict(ot), settings)
        hrs = float(ot["total_ot_hours"])
        ot_pay = compute_ot_payout(hrs, hourly_rate, float(ot["multiplier"]))

        cursor.execute("""
            UPDATE overtime_records
            SET status = 'APPROVED', approved_hours = ?, calculated_ot_pay = ?,
                approved_by = 'HR Batch Approval', approved_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (hrs, ot_pay, ot_id))
        count += 1

    conn.commit()
    conn.close()
    flash(f"Batch approved {count} pending overtime claims!", "success")
    return redirect(url_for("overtime.list_overtime"))

@overtime_bp.route("/settings", methods=["POST"])
def update_settings():
    default_ot_multiplier = float(request.form.get("default_ot_multiplier", 1.5))
    weekend_ot_multiplier = float(request.form.get("weekend_ot_multiplier", 2.0))
    holiday_ot_multiplier = float(request.form.get("holiday_ot_multiplier", 2.5))
    standard_month_days = int(request.form.get("standard_month_days", 26))
    standard_day_hours = float(request.form.get("standard_day_hours", 8.0))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE company_settings
        SET default_ot_multiplier = ?,
            weekend_ot_multiplier = ?,
            holiday_ot_multiplier = ?,
            standard_month_days = ?,
            standard_day_hours = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    """, (default_ot_multiplier, weekend_ot_multiplier, holiday_ot_multiplier, standard_month_days, standard_day_hours))
    conn.commit()
    conn.close()

    flash("Overtime policy settings updated successfully!", "success")
    return redirect(url_for("overtime.list_overtime"))
