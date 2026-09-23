import io
import csv
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, make_response
from datetime import datetime, date, timedelta
from database import get_db_connection
from models.payroll_engine import (
    run_monthly_payroll,
    run_weekly_payroll,
    finalize_payroll_run,
    adjust_draft_payslip
)

payroll_bp = Blueprint("payroll", __name__, url_prefix="/payroll")

@payroll_bp.route("/")
def index():
    conn = get_db_connection()
    cursor = conn.cursor()

    run_type_filter = request.args.get("type", "ALL").upper()

    if run_type_filter in ("MONTHLY", "WEEKLY"):
        cursor.execute("""
            SELECT * FROM payroll_runs 
            WHERE run_type = ?
            ORDER BY id DESC
        """, (run_type_filter,))
    else:
        cursor.execute("""
            SELECT * FROM payroll_runs 
            ORDER BY id DESC
        """)
    runs = [dict(row) for row in cursor.fetchall()]

    # Stats
    cursor.execute("""
        SELECT 
            COUNT(*) as total_runs,
            COALESCE(SUM(total_gross), 0.0) as all_time_gross,
            COALESCE(SUM(total_net), 0.0) as all_time_net,
            COALESCE(SUM(total_ot_pay), 0.0) as all_time_ot,
            COALESCE(SUM(total_shift_allowance), 0.0) as all_time_shift_allowance,
            COALESCE(SUM(total_bonus), 0.0) as all_time_bonus,
            COALESCE(SUM(total_advance_deductions), 0.0) as all_time_advances
        FROM payroll_runs
    """)
    stats = dict(cursor.fetchone())

    # Current dates for modals
    today = date.today()
    current_month = today.month
    current_year = today.year

    # Calculate default weekly range (most recent Monday to Sunday)
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    default_week_start = start_of_week.strftime("%Y-%m-%d")
    default_week_end = end_of_week.strftime("%Y-%m-%d")

    # Count weekly vs monthly active employees
    cursor.execute("SELECT COUNT(*) as cnt FROM employees WHERE is_active = 1 AND pay_frequency = 'WEEKLY'")
    weekly_emp_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM employees WHERE is_active = 1 AND pay_frequency = 'MONTHLY'")
    monthly_emp_count = cursor.fetchone()["cnt"]

    conn.close()
    return render_template(
        "payroll/index.html",
        runs=runs,
        stats=stats,
        current_month=current_month,
        current_year=current_year,
        default_week_start=default_week_start,
        default_week_end=default_week_end,
        weekly_emp_count=weekly_emp_count,
        monthly_emp_count=monthly_emp_count,
        active_filter=run_type_filter
    )

@payroll_bp.route("/run", methods=["POST"])
def process_run():
    month = int(request.form.get("month"))
    year = int(request.form.get("year"))
    bonus = float(request.form.get("bonus", 0.0) or 0.0)

    conn = get_db_connection()
    res = run_monthly_payroll(conn, month, year, bonus=bonus)
    conn.close()

    if res["success"]:
        flash(f"Monthly Payroll for {res['period_name']} processed successfully! {res['processed_employees']} employees calculated.", "success")
        return redirect(url_for("payroll.view_run", run_id=res["payroll_run_id"]))
    else:
        flash(res.get("error", "Failed to process payroll."), "danger")
        return redirect(url_for("payroll.index"))

@payroll_bp.route("/run-weekly", methods=["POST"])
def process_weekly_run():
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    period_name = request.form.get("period_name", "").strip()
    bonus = float(request.form.get("bonus", 0.0) or 0.0)
    filter_freq = request.form.get("filter_frequency", "WEEKLY")

    if not start_date or not end_date:
        flash("Start date and End date are required for weekly payroll.", "danger")
        return redirect(url_for("payroll.index"))

    conn = get_db_connection()
    res = run_weekly_payroll(
        conn,
        start_date=start_date,
        end_date=end_date,
        period_name=period_name or None,
        bonus=bonus,
        filter_frequency=filter_freq
    )
    conn.close()

    if res["success"]:
        flash(f"Weekly Payroll for {res['period_name']} processed successfully! {res['processed_employees']} staff payslips generated with weekly advance deductions.", "success")
        return redirect(url_for("payroll.view_run", run_id=res["payroll_run_id"]))
    else:
        flash(res.get("error", "Failed to process weekly payroll."), "danger")
        return redirect(url_for("payroll.index"))

@payroll_bp.route("/run/<int:run_id>")
def view_run(run_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM payroll_runs WHERE id = ?", (run_id,))
    run_row = cursor.fetchone()
    if not run_row:
        flash("Payroll run not found", "danger")
        conn.close()
        return redirect(url_for("payroll.index"))

    payroll_run = dict(run_row)

    # Fetch payslips
    cursor.execute("""
        SELECT p.*, e.emp_no, e.first_name, e.last_name, e.designation, d.name as dept_name
        FROM payslips p
        JOIN employees e ON p.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        WHERE p.payroll_run_id = ?
        ORDER BY e.emp_no ASC
    """, (run_id,))
    payslips = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return render_template("payroll/view_run.html", payroll_run=payroll_run, payslips=payslips)

@payroll_bp.route("/run/<int:run_id>/finalize", methods=["POST"])
def finalize_run(run_id):
    conn = get_db_connection()
    res = finalize_payroll_run(conn, run_id)
    conn.close()

    if res["success"]:
        flash(f"Payroll {res['period_name']} finalized and locked! Advance repayments logged to ledger.", "success")
    else:
        flash(res.get("error", "Could not finalize payroll."), "danger")
    return redirect(url_for("payroll.view_run", run_id=run_id))

@payroll_bp.route("/payslip/<int:payslip_id>/adjust", methods=["POST"])
def adjust_payslip(payslip_id):
    bonus = float(request.form.get("bonus", 0.0) or 0.0)
    advance_deduction = float(request.form.get("advance_deduction", 0.0) or 0.0)

    conn = get_db_connection()
    res = adjust_draft_payslip(conn, payslip_id, bonus=bonus, advance_deduction=advance_deduction)
    conn.close()

    if res["success"]:
        flash("Payslip adjusted successfully with updated bonus and advance deduction.", "success")
    else:
        flash(res.get("error", "Failed to adjust payslip."), "danger")

    return redirect(request.referrer or url_for("payroll.index"))

@payroll_bp.route("/payslip/<int:payslip_id>")
def view_payslip(payslip_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT p.*, 
               r.period_name, r.start_date, r.end_date, r.status as run_status, r.run_type,
               e.emp_no, e.first_name, e.last_name, e.email, e.phone, e.designation, e.join_date,
               e.salary_type, e.pay_frequency as emp_pay_frequency, d.name as dept_name, s.name as default_shift_name
        FROM payslips p
        JOIN payroll_runs r ON p.payroll_run_id = r.id
        JOIN employees e ON p.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN shifts s ON e.default_shift_id = s.id
        WHERE p.id = ?
    """, (payslip_id,))
    row = cursor.fetchone()
    if not row:
        flash("Payslip not found", "danger")
        conn.close()
        return redirect(url_for("payroll.index"))

    payslip = dict(row)

    # Check active advance for employee
    cursor.execute("""
        SELECT * FROM advances 
        WHERE employee_id = ? AND remaining_amount > 0
        ORDER BY id ASC LIMIT 1
    """, (payslip["employee_id"],))
    active_advance = cursor.fetchone()
    if active_advance:
        active_advance = dict(active_advance)

    # Company settings
    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    settings = dict(cursor.fetchone() or {
        "company_name": "Vasantham Printers",
        "currency_symbol": "₹"
    })

    # --- Daily Punch Timeline for payslip ---
    # Fetch every working day's attendance record in the pay period
    cursor.execute("""
        SELECT ar.date, ar.punch_in, ar.punch_out, ar.work_hours,
               ar.raw_ot_hours, ar.status, ar.has_missed_mid_punch, ar.mid_punch_status
        FROM attendance_records ar
        WHERE ar.employee_id = ? AND ar.date >= ? AND ar.date <= ?
        ORDER BY ar.date ASC
    """, (payslip["employee_id"], payslip["start_date"], payslip["end_date"]))
    att_rows = [dict(r) for r in cursor.fetchall()]

    # For each day, gather all individual punches in order
    daily_attendance = []
    for ar in att_rows:
        cursor.execute("""
            SELECT punch_time
            FROM attendance_punches
            WHERE employee_id = ? AND punch_time >= ? AND punch_time < ?
            ORDER BY punch_time ASC
        """, (
            payslip["employee_id"],
            ar["date"] + " 00:00:00",
            ar["date"] + " 23:59:59"
        ))
        punch_rows = cursor.fetchall()
        punch_times = [r["punch_time"][11:16] for r in punch_rows]  # HH:MM only

        daily_attendance.append({
            "date": ar["date"],
            "status": ar["status"],
            "punch_in": ar["punch_in"][11:16] if ar["punch_in"] else None,
            "punch_out": ar["punch_out"][11:16] if ar["punch_out"] else None,
            "work_hours": ar["work_hours"] or 0.0,
            "ot_hours": ar["raw_ot_hours"] or 0.0,
            "punches": punch_times,
            "has_missed_mid_punch": ar["has_missed_mid_punch"],
            "mid_punch_status": ar["mid_punch_status"],
        })

    conn.close()
    return render_template(
        "payroll/payslip.html",
        payslip=payslip,
        settings=settings,
        active_advance=active_advance,
        daily_attendance=daily_attendance
    )

@payroll_bp.route("/run/<int:run_id>/print-all")
def print_all_payslips(run_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM payroll_runs WHERE id = ?", (run_id,))
    run_row = cursor.fetchone()
    if not run_row:
        flash("Payroll run not found", "danger")
        conn.close()
        return redirect(url_for("payroll.index"))

    payroll_run = dict(run_row)

    # Fetch all payslips for this run with employee & department info
    cursor.execute("""
        SELECT p.*, 
               r.period_name, r.start_date, r.end_date, r.status as run_status, r.run_type,
               e.emp_no, e.first_name, e.last_name, e.email, e.phone, e.designation, e.join_date,
               e.salary_type, e.pay_frequency as emp_pay_frequency, d.name as dept_name, s.name as default_shift_name
        FROM payslips p
        JOIN payroll_runs r ON p.payroll_run_id = r.id
        JOIN employees e ON p.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN shifts s ON e.default_shift_id = s.id
        WHERE p.payroll_run_id = ?
        ORDER BY e.emp_no ASC
    """, (run_id,))
    payslips = [dict(row) for row in cursor.fetchall()]

    # Fetch active advances map {employee_id: advance_dict}
    cursor.execute("""
        SELECT * FROM advances 
        WHERE remaining_amount > 0
        ORDER BY id ASC
    """)
    advances = cursor.fetchall()
    advances_by_emp = {}
    for adv in advances:
        emp_id = adv["employee_id"]
        if emp_id not in advances_by_emp:
            advances_by_emp[emp_id] = dict(adv)

    for p in payslips:
        p["active_advance"] = advances_by_emp.get(p["employee_id"])

    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    settings = dict(cursor.fetchone() or {
        "company_name": "Vasantham Printers",
        "currency_symbol": "₹"
    })

    # --- Daily Punch Timeline for each payslip ---
    for p in payslips:
        cursor.execute("""
            SELECT ar.date, ar.punch_in, ar.punch_out, ar.work_hours,
                   ar.raw_ot_hours, ar.status, ar.has_missed_mid_punch, ar.mid_punch_status
            FROM attendance_records ar
            WHERE ar.employee_id = ? AND ar.date >= ? AND ar.date <= ?
            ORDER BY ar.date ASC
        """, (p["employee_id"], p["start_date"], p["end_date"]))
        att_rows = [dict(r) for r in cursor.fetchall()]

        daily_list = []
        for ar in att_rows:
            cursor.execute("""
                SELECT punch_time FROM attendance_punches
                WHERE employee_id = ? AND punch_time >= ? AND punch_time <= ?
                ORDER BY punch_time ASC
            """, (
                p["employee_id"],
                ar["date"] + " 00:00:00",
                ar["date"] + " 23:59:59"
            ))
            punch_times = [r["punch_time"][11:16] for r in cursor.fetchall()]
            daily_list.append({
                "date": ar["date"],
                "status": ar["status"],
                "punch_in": ar["punch_in"][11:16] if ar["punch_in"] else None,
                "punch_out": ar["punch_out"][11:16] if ar["punch_out"] else None,
                "work_hours": ar["work_hours"] or 0.0,
                "ot_hours": ar["raw_ot_hours"] or 0.0,
                "punches": punch_times,
                "has_missed_mid_punch": ar["has_missed_mid_punch"],
                "mid_punch_status": ar["mid_punch_status"],
            })
        p["daily_attendance"] = daily_list

    conn.close()
    return render_template(
        "payroll/print_all_payslips.html",
        payroll_run=payroll_run,
        payslips=payslips,
        settings=settings
    )

# -------------------------------------------------------------
# SALARY ADVANCES & WEEKLY DEDUCTIONS MANAGEMENT
# -------------------------------------------------------------
@payroll_bp.route("/advances")
def advances_portal():
    conn = get_db_connection()
    cursor = conn.cursor()

    status_filter = request.args.get("status", "ACTIVE")

    # Fetch advances with employee details
    if status_filter in ("ACTIVE", "COMPLETED"):
        cursor.execute("""
            SELECT a.*, e.emp_no, e.first_name, e.last_name, e.designation, e.pay_frequency, d.name as dept_name
            FROM advances a
            JOIN employees e ON a.employee_id = e.id
            JOIN departments d ON e.department_id = d.id
            WHERE a.status = ?
            ORDER BY a.id DESC
        """, (status_filter,))
    else:
        cursor.execute("""
            SELECT a.*, e.emp_no, e.first_name, e.last_name, e.designation, e.pay_frequency, d.name as dept_name
            FROM advances a
            JOIN employees e ON a.employee_id = e.id
            JOIN departments d ON e.department_id = d.id
            ORDER BY a.id DESC
        """)
    advances = [dict(row) for row in cursor.fetchall()]

    # Stats
    cursor.execute("""
        SELECT 
            COUNT(CASE WHEN status = 'ACTIVE' THEN 1 END) as active_count,
            COALESCE(SUM(total_amount), 0.0) as total_disbursed,
            COALESCE(SUM(amount_repaid), 0.0) as total_recovered,
            COALESCE(SUM(CASE WHEN status = 'ACTIVE' THEN remaining_amount ELSE 0.0 END), 0.0) as total_outstanding,
            COALESCE(SUM(CASE WHEN status = 'ACTIVE' THEN weekly_deduction ELSE 0.0 END), 0.0) as weekly_cuts_due
        FROM advances
    """)
    stats = dict(cursor.fetchone())

    # Fetch recent repayments
    cursor.execute("""
        SELECT r.*, a.employee_id, e.emp_no, e.first_name, e.last_name
        FROM advance_repayments r
        JOIN advances a ON r.advance_id = a.id
        JOIN employees e ON a.employee_id = e.id
        ORDER BY r.id DESC
        LIMIT 15
    """)
    recent_repayments = [dict(row) for row in cursor.fetchall()]

    # Active employees for the "Issue Advance" modal
    cursor.execute("""
        SELECT id, emp_no, first_name, last_name, designation, pay_frequency, base_salary
        FROM employees
        WHERE is_active = 1
        ORDER BY emp_no ASC
    """)
    employees = [dict(row) for row in cursor.fetchall()]

    today_str = date.today().strftime("%Y-%m-%d")

    conn.close()
    return render_template(
        "payroll/advances.html",
        advances=advances,
        stats=stats,
        recent_repayments=recent_repayments,
        employees=employees,
        active_status=status_filter,
        today_date=today_str
    )

@payroll_bp.route("/advances/add", methods=["POST"])
def add_advance():
    employee_id = int(request.form.get("employee_id"))
    advance_date = request.form.get("advance_date", date.today().strftime("%Y-%m-%d")).strip()
    total_amount = float(request.form.get("total_amount", 0.0) or 0.0)
    weekly_deduction = float(request.form.get("weekly_deduction", 0.0) or 0.0)
    notes = request.form.get("notes", "").strip()

    if total_amount <= 0:
        flash("Advance amount must be greater than zero.", "danger")
        return redirect(url_for("payroll.advances_portal"))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO advances (
                employee_id, advance_date, total_amount, weekly_deduction,
                amount_repaid, remaining_amount, status, notes
            ) VALUES (?, ?, ?, ?, 0.0, ?, 'ACTIVE', ?)
        """, (employee_id, advance_date, total_amount, weekly_deduction, total_amount, notes))
        conn.commit()
        flash(f"Salary advance of ₹{total_amount:.2f} issued successfully with weekly cut of ₹{weekly_deduction:.2f}!", "success")
    except Exception as e:
        flash(f"Error recording advance: {str(e)}", "danger")
    finally:
        conn.close()

    return redirect(url_for("payroll.advances_portal"))

@payroll_bp.route("/advances/<int:adv_id>/settle-cash", methods=["POST"])
def settle_advance_cash(adv_id):
    amount = float(request.form.get("amount", 0.0) or 0.0)
    notes = request.form.get("notes", "Direct cash repayment").strip()

    if amount <= 0:
        flash("Repayment amount must be positive.", "danger")
        return redirect(url_for("payroll.advances_portal"))

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM advances WHERE id = ?", (adv_id,))
    adv_row = cursor.fetchone()
    if not adv_row:
        flash("Advance record not found.", "danger")
        conn.close()
        return redirect(url_for("payroll.advances_portal"))

    adv = dict(adv_row)
    repay_amount = min(amount, float(adv["remaining_amount"]))
    new_repaid = round(float(adv["amount_repaid"]) + repay_amount, 2)
    new_remaining = max(0.0, round(float(adv["remaining_amount"]) - repay_amount, 2))
    new_status = "COMPLETED" if new_remaining <= 0.01 else "ACTIVE"

    cursor.execute("""
        UPDATE advances
        SET amount_repaid = ?, remaining_amount = ?, status = ?
        WHERE id = ?
    """, (new_repaid, new_remaining, new_status, adv_id))

    cursor.execute("""
        INSERT INTO advance_repayments (
            advance_id, repayment_date, amount, payment_type, notes
        ) VALUES (?, ?, ?, 'CASH_DIRECT', ?)
    """, (adv_id, date.today().strftime("%Y-%m-%d"), repay_amount, notes))

    conn.commit()
    conn.close()

    flash(f"Cash repayment of ₹{repay_amount:.2f} logged successfully! Remaining balance: ₹{new_remaining:.2f}.", "success")
    return redirect(url_for("payroll.advances_portal"))

@payroll_bp.route("/advances/<int:adv_id>/delete", methods=["POST"])
def delete_advance(adv_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM advance_repayments WHERE advance_id = ?", (adv_id,))
    cursor.execute("DELETE FROM advances WHERE id = ?", (adv_id,))
    conn.commit()
    conn.close()
    flash("Advance record removed.", "info")
    return redirect(url_for("payroll.advances_portal"))

@payroll_bp.route("/run/<int:run_id>/export-csv")
def export_csv(run_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM payroll_runs WHERE id = ?", (run_id,))
    run_row = cursor.fetchone()
    if not run_row:
        conn.close()
        return "Not found", 404

    cursor.execute("""
        SELECT p.*, e.emp_no, e.first_name, e.last_name, e.designation, d.name as dept_name
        FROM payslips p
        JOIN employees e ON p.employee_id = e.id
        JOIN departments d ON e.department_id = d.id
        WHERE p.payroll_run_id = ?
        ORDER BY e.emp_no ASC
    """, (run_id,))
    payslips = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Emp No", "Employee Name", "Department", "Designation", "Pay Frequency",
        "Days Worked", "Night Shifts", "Shift Allowance",
        "Approved OT Hours", "Overtime Pay", "Base Salary", "HRA", "Special Allowance", "Bonus",
        "Gross Earnings", "PF Deduction", "ESI Deduction", "Tax Deduction", "LOP Deduction",
        "Advance Deduction", "Total Deductions", "Net Pay"
    ])

    for p in payslips:
        writer.writerow([
            p["emp_no"], f"{p['first_name']} {p['last_name']}", p["dept_name"], p["designation"], p["pay_frequency"],
            p["days_worked"], p["night_shifts_count"], p["total_shift_allowance"],
            p["approved_ot_hours"], p["ot_pay"], p["base_salary"], p["hra"], p["special_allowance"], p.get("bonus", 0.0),
            p["gross_earnings"], p["pf_deduction"], p["esi_deduction"], p["tax_deduction"], p["lop_deduction"],
            p.get("advance_deduction", 0.0), p["total_deductions"], p["net_pay"]
        ])

    csv_data = output.getvalue()
    filename = f"payroll_{run_row['period_name'].replace(' ', '_').replace('(', '').replace(')', '')}.csv"
    response = make_response(csv_data)
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    response.headers["Content-type"] = "text/csv"
    return response
