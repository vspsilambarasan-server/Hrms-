from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from datetime import datetime, timedelta, date
from database import get_db_connection

shifts_bp = Blueprint("shifts", __name__, url_prefix="/shifts")

@shifts_bp.route("/")
def list_shifts():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT s.*, 
               (SELECT COUNT(*) FROM employees WHERE default_shift_id = s.id AND is_active = 1) as assigned_count
        FROM shifts s
        ORDER BY s.id ASC
    """)
    shifts = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return render_template("shifts/index.html", shifts=shifts)

@shifts_bp.route("/add", methods=["POST"])
def add_shift():
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    start_time = request.form.get("start_time", "").strip()
    end_time = request.form.get("end_time", "").strip()
    is_overnight = 1 if request.form.get("is_overnight") == "1" else 0
    grace_late = int(request.form.get("grace_late_mins", 10))
    grace_early = int(request.form.get("grace_early_mins", 15))
    break_mins = int(request.form.get("break_mins", 60))
    min_hours_half = float(request.form.get("min_hours_half_day", 4.5))
    min_hours_full = float(request.form.get("min_hours_full_day", 8.0))
    allowance_rate = float(request.form.get("allowance_rate", 0.0) or 0.0)
    color = request.form.get("color", "#3b82f6").strip()
    description = request.form.get("description", "").strip()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO shifts (
                code, name, start_time, end_time, is_overnight,
                grace_late_mins, grace_early_mins, break_mins,
                min_hours_half_day, min_hours_full_day, allowance_rate,
                color, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            code, name, start_time, end_time, is_overnight,
            grace_late, grace_early, break_mins,
            min_hours_half, min_hours_full, allowance_rate,
            color, description
        ))
        conn.commit()
        flash(f"Shift '{name}' ({code}) created successfully!", "success")
    except Exception as e:
        flash(f"Error creating shift: {str(e)}", "danger")
    finally:
        conn.close()

    return redirect(url_for("shifts.list_shifts"))

@shifts_bp.route("/roster")
def roster_scheduler():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Get target week start date (default to current Monday or requested date)
    date_param = request.args.get("week_start")
    if date_param:
        try:
            start_dt = datetime.strptime(date_param, "%Y-%m-%d")
        except ValueError:
            start_dt = datetime.now()
    else:
        start_dt = datetime.now()

    # Align to Monday of that week
    monday_dt = start_dt - timedelta(days=start_dt.weekday())
    
    # 7 days of the week
    week_days = []
    for i in range(7):
        d = monday_dt + timedelta(days=i)
        week_days.append({
            "date_str": d.strftime("%Y-%m-%d"),
            "display": d.strftime("%a, %b %d"),
            "weekday": d.weekday(),
            "is_today": d.strftime("%Y-%m-%d") == date.today().strftime("%Y-%m-%d")
        })

    prev_week = (monday_dt - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (monday_dt + timedelta(days=7)).strftime("%Y-%m-%d")

    # Fetch departments & shifts
    cursor.execute("SELECT * FROM departments ORDER BY name")
    departments = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT * FROM shifts WHERE is_active = 1 ORDER BY name")
    shifts = [dict(row) for row in cursor.fetchall()]
    shift_dict = {s["id"]: s for s in shifts}

    # Department filter
    dept_id = request.args.get("dept")
    emp_query = "SELECT id, emp_no, first_name, last_name, default_shift_id, department_id FROM employees WHERE is_active = 1"
    emp_params = []
    if dept_id and dept_id.isdigit():
        emp_query += " AND department_id = ?"
        emp_params.append(int(dept_id))
    emp_query += " ORDER BY first_name ASC"

    cursor.execute(emp_query, emp_params)
    employees = [dict(row) for row in cursor.fetchall()]

    # Fetch roster for this week
    start_str = week_days[0]["date_str"]
    end_str = week_days[-1]["date_str"]

    cursor.execute("""
        SELECT * FROM roster_schedules
        WHERE date >= ? AND date <= ?
    """, (start_str, end_str))
    roster_rows = cursor.fetchall()
    
    # Map (employee_id, date) -> roster entry
    roster_map = {}
    for r in roster_rows:
        roster_map[(r["employee_id"], r["date"])] = dict(r)

    # Build matrix
    matrix = []
    for emp in employees:
        row_days = []
        for wd in week_days:
            dt_str = wd["date_str"]
            entry = roster_map.get((emp["id"], dt_str))
            if entry:
                shift_id = entry["shift_id"]
                is_off = entry["is_off_day"]
                shift_obj = shift_dict.get(shift_id) if shift_id else None
            else:
                # Default logic: if Sunday (or weekly off), off day, else default shift
                is_off = 1 if wd["weekday"] == 6 else 0
                shift_id = None if is_off else emp["default_shift_id"]
                shift_obj = shift_dict.get(shift_id) if shift_id else None

            row_days.append({
                "date": dt_str,
                "is_off": is_off,
                "shift": shift_obj,
                "shift_id": shift_id
            })

        matrix.append({
            "employee": emp,
            "days": row_days
        })

    conn.close()

    return render_template(
        "shifts/roster.html",
        matrix=matrix,
        week_days=week_days,
        monday_str=monday_dt.strftime("%Y-%m-%d"),
        prev_week=prev_week,
        next_week=next_week,
        departments=departments,
        shifts=shifts,
        selected_dept=dept_id
    )

@shifts_bp.route("/roster/assign", methods=["POST"])
def assign_roster():
    employee_id = int(request.form.get("employee_id"))
    date_str = request.form.get("date").strip()
    shift_id = request.form.get("shift_id")
    is_off_day = 1 if request.form.get("is_off_day") == "1" else 0
    shift_id = int(shift_id) if (shift_id and not is_off_day) else None

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO roster_schedules (employee_id, date, shift_id, is_off_day)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(employee_id, date) DO UPDATE SET
                shift_id = excluded.shift_id,
                is_off_day = excluded.is_off_day
        """, (employee_id, date_str, shift_id, is_off_day))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        conn.close()

@shifts_bp.route("/duty-intervals")
def duty_intervals_view():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM duty_intervals ORDER BY segment_number ASC")
    intervals = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return render_template("shifts/intervals.html", intervals=intervals)

@shifts_bp.route("/duty-intervals/update", methods=["POST"])
def update_duty_intervals():
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        for i in range(1, 8):
            time_in = request.form.get(f"time_in_{i}", "").strip()
            time_out = request.form.get(f"time_out_{i}", "").strip()

            cursor.execute("""
                UPDATE duty_intervals
                SET time_in = ?, time_out = ?, updated_at = CURRENT_TIMESTAMP
                WHERE segment_number = ?
            """, (time_in, time_out, i))

        # Synchronize General Day Shift to 09:00 - 18:00 with 90 min breaks
        cursor.execute("""
            UPDATE shifts
            SET start_time = '09:00', end_time = '18:00', break_mins = 90
            WHERE code = 'GEN'
        """)

        conn.commit()
        flash("Daily Duty & Break Intervals successfully updated and applied to shift engine!", "success")
    except Exception as e:
        flash(f"Failed to update duty intervals: {str(e)}", "danger")
    finally:
        conn.close()

    return redirect(url_for("shifts.duty_intervals_view"))

