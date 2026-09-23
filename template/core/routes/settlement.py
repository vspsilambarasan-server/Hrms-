from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from database import get_db_connection
from routes.auth import login_required

settlement_bp = Blueprint("settlement", __name__, url_prefix="/settlement")

@settlement_bp.route("/")
@login_required
def index():
    """Statutory Portal Dashboard overview."""
    conn = get_db_connection()
    cursor = conn.cursor()

    current_year = date.today().year

    # 1. Diwali Bonus Summary
    cursor.execute("""
        SELECT COUNT(*) as total_calculated,
               COALESCE(SUM(bonus_amount), 0.0) as total_bonus_amount,
               COALESCE(SUM(CASE WHEN payout_status = 'PAID' THEN bonus_amount ELSE 0.0 END), 0.0) as paid_amount,
               COALESCE(SUM(CASE WHEN payout_status = 'PENDING' THEN bonus_amount ELSE 0.0 END), 0.0) as pending_amount
        FROM diwali_bonus_records
        WHERE year = ?
    """, (current_year,))
    diwali_summary = dict(cursor.fetchone() or {})

    # 2. Final Settlements Summary
    cursor.execute("""
        SELECT COUNT(*) as settled_count,
               COALESCE(SUM(gratuity_amount), 0.0) as total_gratuity,
               COALESCE(SUM(net_settlement_amount), 0.0) as total_disbursed
        FROM final_settlement_records
    """)
    settlement_summary = dict(cursor.fetchone() or {})

    # 3. Pongal Holiday Summary
    cursor.execute("""
        SELECT COUNT(*) as records_count,
               COALESCE(SUM(leave_salary_rate), 0.0) as total_holiday_pay
        FROM pongal_holiday_records
        WHERE year = ?
    """, (current_year,))
    pongal_summary = dict(cursor.fetchone() or {})

    # Recent settlements
    cursor.execute("""
        SELECT f.*, e.emp_no, e.first_name, e.last_name, e.designation
        FROM final_settlement_records f
        JOIN employees e ON f.employee_id = e.id
        ORDER BY f.settlement_date DESC
        LIMIT 5
    """)
    recent_settlements = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return render_template(
        "settlement/index.html",
        current_year=current_year,
        diwali_summary=diwali_summary,
        settlement_summary=settlement_summary,
        pongal_summary=pongal_summary,
        recent_settlements=recent_settlements
    )

@settlement_bp.route("/diwali-bonus", methods=["GET", "POST"])
@login_required
def diwali_bonus():
    """Diwali Annual Bonus Calculator under Payment of Bonus Act, 1965."""
    conn = get_db_connection()
    cursor = conn.cursor()

    year = int(request.args.get("year", date.today().year))

    if request.method == "POST":
        action = request.form.get("action")

        if action == "calculate":
            calc_year = int(request.form.get("year", year))
            bonus_pct = float(request.form.get("bonus_percentage", 8.33))
            
            # Bound bonus percentage between legal limits: min 8.33% up to 20.0%
            bonus_pct = max(8.33, min(20.0, bonus_pct))

            cursor.execute("SELECT * FROM employees WHERE is_active = 1 ORDER BY emp_no ASC")
            employees = [dict(r) for r in cursor.fetchall()]

            calculated_count = 0
            for emp in employees:
                emp_id = emp["id"]

                # Aggregate annual wages earned from completed payroll runs for this year
                cursor.execute("""
                    SELECT COALESCE(SUM(p.base_salary - p.lop_deduction + p.total_shift_allowance), 0.0) as annual_wages
                    FROM payslips p
                    JOIN payroll_runs r ON p.payroll_run_id = r.id
                    WHERE p.employee_id = ? AND r.year = ?
                """, (emp_id, calc_year))
                wages_row = cursor.fetchone()
                earned_wages = float(wages_row["annual_wages"] or 0.0)

                # If no payroll records generated yet, estimate from shift salary (26 days * 12 months)
                if earned_wages <= 0.0:
                    shift_wage = float(emp.get("shift_salary") or 500.0)
                    if shift_wage <= 0 and emp.get("base_salary", 0) > 0:
                        shift_wage = emp["base_salary"] / 26.0
                    earned_wages = round(shift_wage * 26 * 12, 2)

                bonus_amt = round(earned_wages * (bonus_pct / 100.0), 2)

                cursor.execute("""
                    INSERT INTO diwali_bonus_records (
                        year, employee_id, annual_earned_wages, bonus_percentage, bonus_amount, payout_status
                    ) VALUES (?, ?, ?, ?, ?, 'PENDING')
                    ON CONFLICT(year, employee_id) DO UPDATE SET
                        annual_earned_wages = excluded.annual_earned_wages,
                        bonus_percentage = excluded.bonus_percentage,
                        bonus_amount = excluded.bonus_amount
                """, (calc_year, emp_id, earned_wages, bonus_pct, bonus_amt))
                calculated_count += 1

            conn.commit()
            flash(f"Diwali Bonus successfully calculated for {calculated_count} employees at {bonus_pct:.2f}% for year {calc_year}!", "success")
            conn.close()
            return redirect(url_for("settlement.diwali_bonus", year=calc_year))

        elif action == "mark_paid":
            record_id = int(request.form.get("record_id"))
            ref = request.form.get("payout_reference", "").strip() or "Bank Transfer / Cash"
            today_str = date.today().strftime("%Y-%m-%d")

            cursor.execute("""
                UPDATE diwali_bonus_records
                SET payout_status = 'PAID', payout_date = ?, payout_reference = ?
                WHERE id = ?
            """, (today_str, ref, record_id))
            conn.commit()
            flash("Bonus payout marked as PAID.", "success")
            conn.close()
            return redirect(url_for("settlement.diwali_bonus", year=year))

        elif action == "mark_all_paid":
            calc_year = int(request.form.get("year", year))
            ref = request.form.get("payout_reference", "").strip() or "Diwali Festival Disbursement"
            today_str = date.today().strftime("%Y-%m-%d")

            cursor.execute("""
                UPDATE diwali_bonus_records
                SET payout_status = 'PAID', payout_date = ?, payout_reference = ?
                WHERE year = ? AND payout_status = 'PENDING'
            """, (today_str, ref, calc_year))
            conn.commit()
            flash(f"All pending bonus payouts for year {calc_year} marked as PAID!", "success")
            conn.close()
            return redirect(url_for("settlement.diwali_bonus", year=calc_year))

    # Fetch bonus records for year
    cursor.execute("""
        SELECT b.*, e.emp_no, e.first_name, e.last_name, e.designation, e.shift_salary, e.base_salary, d.name as dept_name
        FROM diwali_bonus_records b
        JOIN employees e ON b.employee_id = e.id
        LEFT JOIN departments d ON e.department_id = d.id
        WHERE b.year = ?
        ORDER BY e.emp_no ASC
    """, (year,))
    bonus_records = [dict(r) for r in cursor.fetchall()]

    total_pool = sum(r["bonus_amount"] for r in bonus_records)
    total_paid = sum(r["bonus_amount"] for r in bonus_records if r["payout_status"] == "PAID")
    total_pending = sum(r["bonus_amount"] for r in bonus_records if r["payout_status"] == "PENDING")

    conn.close()
    return render_template(
        "settlement/diwali_bonus.html",
        year=year,
        bonus_records=bonus_records,
        total_pool=total_pool,
        total_paid=total_paid,
        total_pending=total_pending
    )

@settlement_bp.route("/diwali-bonus/print")
@login_required
def print_diwali_register():
    """Printable Diwali Bonus Register under Payment of Bonus Rules (Form C)."""
    year = int(request.args.get("year", date.today().year))
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT b.*, e.emp_no, e.first_name, e.last_name, e.designation, e.join_date, d.name as dept_name
        FROM diwali_bonus_records b
        JOIN employees e ON b.employee_id = e.id
        LEFT JOIN departments d ON e.department_id = d.id
        WHERE b.year = ?
        ORDER BY e.emp_no ASC
    """, (year,))
    records = [dict(r) for r in cursor.fetchall()]

    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    company = dict(cursor.fetchone() or {})
    conn.close()

    total_wages = sum(r["annual_earned_wages"] for r in records)
    total_bonus = sum(r["bonus_amount"] for r in records)

    return render_template(
        "settlement/print_diwali_register.html",
        year=year,
        records=records,
        company=company,
        total_wages=total_wages,
        total_bonus=total_bonus
    )

@settlement_bp.route("/final-settlement", methods=["GET", "POST"])
@login_required
def final_settlement():
    """Full & Final Settlement under Payment of Gratuity Act 1972 & Factories Act."""
    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        employee_id = int(request.form.get("employee_id"))
        settlement_date = request.form.get("settlement_date", date.today().strftime("%Y-%m-%d"))
        service_years = float(request.form.get("service_years", 0.0))
        last_drawn_wage = float(request.form.get("last_drawn_wage", 0.0))
        gratuity_amount = float(request.form.get("gratuity_amount", 0.0))
        leave_encashment_days = float(request.form.get("leave_encashment_days", 0.0))
        leave_encashment_amount = float(request.form.get("leave_encashment_amount", 0.0))
        unpaid_wages = float(request.form.get("unpaid_wages", 0.0))
        advance_recovery = float(request.form.get("advance_recovery", 0.0))
        approved_by = request.form.get("approved_by", "").strip() or "Factory Manager"
        payout_reference = request.form.get("payout_reference", "").strip() or "NEFT / Cheque"
        remarks = request.form.get("remarks", "").strip()
        deactivate_employee = request.form.get("deactivate_employee") == "1"

        net_settlement_amount = round(gratuity_amount + leave_encashment_amount + unpaid_wages - advance_recovery, 2)

        cursor.execute("""
            INSERT INTO final_settlement_records (
                employee_id, settlement_date, service_years, last_drawn_wage,
                gratuity_amount, leave_encashment_days, leave_encashment_amount,
                unpaid_wages, advance_recovery, net_settlement_amount,
                settlement_status, approved_by, payout_reference, remarks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SETTLED', ?, ?, ?)
        """, (
            employee_id, settlement_date, service_years, last_drawn_wage,
            gratuity_amount, leave_encashment_days, leave_encashment_amount,
            unpaid_wages, advance_recovery, net_settlement_amount,
            approved_by, payout_reference, remarks
        ))
        record_id = cursor.lastrowid

        # Settle active advances if any
        cursor.execute("""
            UPDATE advances
            SET remaining_amount = 0.0, status = 'SETTLED'
            WHERE employee_id = ? AND status = 'ACTIVE'
        """, (employee_id,))

        if deactivate_employee:
            cursor.execute("UPDATE employees SET is_active = 0 WHERE id = ?", (employee_id,))

        conn.commit()
        flash("Full & Final Settlement voucher created and finalized successfully!", "success")
        conn.close()
        return redirect(url_for("settlement.view_settlement_slip", record_id=record_id))

    # GET
    cursor.execute("""
        SELECT f.*, e.emp_no, e.first_name, e.last_name, e.designation, d.name as dept_name
        FROM final_settlement_records f
        JOIN employees e ON f.employee_id = e.id
        LEFT JOIN departments d ON e.department_id = d.id
        ORDER BY f.settlement_date DESC, f.id DESC
    """)
    settlements = [dict(r) for r in cursor.fetchall()]

    # Active employees for dropdown
    cursor.execute("""
        SELECT e.id, e.emp_no, e.first_name, e.last_name, e.designation, e.join_date,
               e.shift_salary, e.base_salary, e.pay_frequency
        FROM employees e
        WHERE e.is_active = 1
        ORDER BY e.emp_no ASC
    """)
    active_employees = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return render_template(
        "settlement/final_settlement.html",
        settlements=settlements,
        active_employees=active_employees,
        today_date=date.today().strftime("%Y-%m-%d")
    )

@settlement_bp.route("/final-settlement/<int:record_id>/slip")
@login_required
def view_settlement_slip(record_id):
    """Printable Full & Final Settlement Voucher & Clearance Certificate."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT f.*, e.emp_no, e.first_name, e.last_name, e.designation, e.join_date,
               e.phone, e.email, d.name as dept_name
        FROM final_settlement_records f
        JOIN employees e ON f.employee_id = e.id
        LEFT JOIN departments d ON e.department_id = d.id
        WHERE f.id = ?
    """)
    # Fix query placeholder
    cursor.execute("""
        SELECT f.*, e.emp_no, e.first_name, e.last_name, e.designation, e.join_date,
               e.phone, e.email, d.name as dept_name
        FROM final_settlement_records f
        JOIN employees e ON f.employee_id = e.id
        LEFT JOIN departments d ON e.department_id = d.id
        WHERE f.id = ?
    """, (record_id,))
    settlement = cursor.fetchone()
    if not settlement:
        conn.close()
        flash("Settlement record not found.", "danger")
        return redirect(url_for("settlement.final_settlement"))

    settlement_dict = dict(settlement)

    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    company = dict(cursor.fetchone() or {})
    conn.close()

    return render_template("settlement/settlement_slip.html", s=settlement_dict, company=company)

@settlement_bp.route("/pongal-holidays", methods=["GET", "POST"])
@login_required
def pongal_holidays():
    """Tamil Nadu National and Festival Holidays Act, 1958 compliance portal."""
    conn = get_db_connection()
    cursor = conn.cursor()

    year = int(request.args.get("year", date.today().year))

    if request.method == "POST":
        calc_year = int(request.form.get("year", year))
        holiday_date = request.form.get("holiday_date", f"{calc_year}-01-15")
        festival_name = request.form.get("festival_name", "Pongal Festival").strip()

        cursor.execute("SELECT * FROM employees WHERE is_active = 1")
        employees = [dict(r) for r in cursor.fetchall()]

        inserted_count = 0
        for emp in employees:
            shift_rate = float(emp.get("shift_salary") or (emp.get("base_salary", 0) / 26.0) or 500.0)
            cursor.execute("""
                INSERT INTO pongal_holiday_records (
                    year, employee_id, holiday_date, festival_name, attended_preceding_day, leave_salary_rate, paid_status
                ) VALUES (?, ?, ?, ?, 1, ?, 'PAID')
                ON CONFLICT(year, employee_id, holiday_date) DO UPDATE SET
                    festival_name = excluded.festival_name,
                    leave_salary_rate = excluded.leave_salary_rate
            """, (calc_year, emp["id"], holiday_date, festival_name, shift_rate))
            inserted_count += 1

        conn.commit()
        flash(f"Pongal Holiday leave wages generated for {inserted_count} employees under TN Festival Holidays Act!", "success")
        conn.close()
        return redirect(url_for("settlement.pongal_holidays", year=calc_year))

    cursor.execute("""
        SELECT p.*, e.emp_no, e.first_name, e.last_name, e.designation, d.name as dept_name
        FROM pongal_holiday_records p
        JOIN employees e ON p.employee_id = e.id
        LEFT JOIN departments d ON e.department_id = d.id
        WHERE p.year = ?
        ORDER BY p.holiday_date DESC, e.emp_no ASC
    """, (year,))
    records = [dict(r) for r in cursor.fetchall()]

    total_paid = sum(float(r["leave_salary_rate"] or 0.0) for r in records)

    conn.close()
    return render_template(
        "settlement/pongal_holidays.html",
        year=year,
        records=records,
        total_paid=total_paid
    )
