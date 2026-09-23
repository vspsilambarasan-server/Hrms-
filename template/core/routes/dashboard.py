from flask import Blueprint, render_template, jsonify
from datetime import datetime, date
from database import get_db_connection

dashboard_bp = Blueprint("dashboard", __name__)

@dashboard_bp.route("/")
def index():
    conn = get_db_connection()
    cursor = conn.cursor()

    today_str = date.today().strftime("%Y-%m-%d")

    # 1. High-level metric counts
    cursor.execute("SELECT COUNT(*) as cnt FROM employees WHERE is_active = 1")
    total_employees = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM shifts WHERE is_active = 1")
    total_shifts = cursor.fetchone()["cnt"]

    # 2. Today's attendance summary
    cursor.execute("""
        SELECT 
            COUNT(CASE WHEN status IN ('PRESENT', 'LATE') THEN 1 END) as present_count,
            COUNT(CASE WHEN status = 'LATE' THEN 1 END) as late_count,
            COUNT(CASE WHEN status = 'HALF_DAY' THEN 1 END) as half_day_count,
            COUNT(CASE WHEN status = 'ABSENT' THEN 1 END) as absent_count,
            COUNT(CASE WHEN status = 'WEEKLY_OFF' THEN 1 END) as off_count
        FROM attendance_records
        WHERE date = ?
    """, (today_str,))
    today_att = dict(cursor.fetchone())

    # If no records for today, show most recent date with records
    if sum(today_att.values()) == 0:
        cursor.execute("SELECT MAX(date) as max_date FROM attendance_records")
        latest_date_row = cursor.fetchone()
        eval_date = latest_date_row["max_date"] or today_str
        cursor.execute("""
            SELECT 
                COUNT(CASE WHEN status IN ('PRESENT', 'LATE') THEN 1 END) as present_count,
                COUNT(CASE WHEN status = 'LATE' THEN 1 END) as late_count,
                COUNT(CASE WHEN status = 'HALF_DAY' THEN 1 END) as half_day_count,
                COUNT(CASE WHEN status = 'ABSENT' THEN 1 END) as absent_count,
                COUNT(CASE WHEN status = 'WEEKLY_OFF' THEN 1 END) as off_count
            FROM attendance_records
            WHERE date = ?
        """, (eval_date,))
        today_att = dict(cursor.fetchone())
    else:
        eval_date = today_str

    # 3. Overtime Pending Approvals
    cursor.execute("""
        SELECT COUNT(*) as pending_count, COALESCE(SUM(total_ot_hours), 0.0) as pending_hours,
               COALESCE(SUM(calculated_ot_pay), 0.0) as pending_cost
        FROM overtime_records
        WHERE status = 'PENDING'
    """)
    pending_ot = dict(cursor.fetchone())

    # 4. Latest Payroll Summary
    cursor.execute("""
        SELECT * FROM payroll_runs 
        ORDER BY year DESC, month DESC 
        LIMIT 1
    """)
    latest_payroll_row = cursor.fetchone()
    latest_payroll = dict(latest_payroll_row) if latest_payroll_row else None

    # 5. Shift Roster Breakdown for today/eval_date
    cursor.execute("""
        SELECT s.name, s.code, s.color, COUNT(r.id) as emp_count
        FROM shifts s
        LEFT JOIN roster_schedules r ON s.id = r.shift_id AND r.date = ?
        GROUP BY s.id
        ORDER BY emp_count DESC
    """, (eval_date,))
    shift_breakdown = [dict(row) for row in cursor.fetchall()]

    # 6. Recent Overtime Requests (Top 5)
    cursor.execute("""
        SELECT o.*, e.first_name, e.last_name, e.emp_no, d.name as dept_name
        FROM overtime_records o
        JOIN employees e ON o.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        ORDER BY o.date DESC, o.id DESC
        LIMIT 6
    """)
    recent_ot = [dict(row) for row in cursor.fetchall()]

    # 7. Department Overtime Hours & Cost Breakdown
    cursor.execute("""
        SELECT d.name as dept_name,
               ROUND(SUM(o.approved_hours), 1) as total_ot_hours,
               ROUND(SUM(o.calculated_ot_pay), 2) as total_ot_cost
        FROM overtime_records o
        JOIN employees e ON o.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        WHERE o.status = 'APPROVED'
        GROUP BY d.id
        ORDER BY total_ot_cost DESC
    """)
    dept_ot_stats = [dict(row) for row in cursor.fetchall()]

    # Fetch active employees for quick punch simulator
    cursor.execute("SELECT id, emp_no, first_name, last_name, default_shift_id FROM employees WHERE is_active = 1 ORDER BY first_name")
    active_employees = [dict(row) for row in cursor.fetchall()]

    conn.close()

    return render_template(
        "dashboard.html",
        total_employees=total_employees,
        total_shifts=total_shifts,
        today_att=today_att,
        eval_date=eval_date,
        pending_ot=pending_ot,
        latest_payroll=latest_payroll,
        shift_breakdown=shift_breakdown,
        recent_ot=recent_ot,
        dept_ot_stats=dept_ot_stats,
        active_employees=active_employees
    )
