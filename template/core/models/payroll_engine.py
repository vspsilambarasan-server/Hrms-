import calendar
from datetime import datetime, date, timedelta
from models.ot_engine import calculate_hourly_rate

def get_employee_active_advance(cursor, employee_id):
    """Fetch the primary active advance for an employee with remaining balance > 0."""
    cursor.execute("""
        SELECT * FROM advances
        WHERE employee_id = ? AND status = 'ACTIVE' AND remaining_amount > 0
        ORDER BY id ASC
        LIMIT 1
    """, (employee_id,))
    row = cursor.fetchone()
    return dict(row) if row else None


def run_monthly_payroll(conn, month, year, bonus=0.0, user="HR Admin"):
    """
    Executes complete payroll calculation for all active employees for the given month/year.
    Accurately accounts for:
      - Shifts worked & Shift Allowances (Night shift bonuses)
      - Approved Overtime hours and OT payouts
      - Base pay & attendance (Days worked, Half-days, LOP)
      - Fixed Allowances (HRA, Special)
      - Bonus / Incentive pay
      - Statutory Deductions (PF, ESI, TDS)
      - Salary Advance Deductions
    """
    cursor = conn.cursor()

    num_days = calendar.monthrange(year, month)[1]
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{year:04d}-{month:02d}-{num_days:02d}"
    period_name = f"{calendar.month_name[month]} {year}"
    bonus_amount = max(0.0, float(bonus or 0.0))

    # Fetch settings
    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    settings_row = cursor.fetchone()
    settings = dict(settings_row) if settings_row else {
        "standard_month_days": 26,
        "standard_day_hours": 8.0,
        "currency_symbol": "₹"
    }
    std_days = int(settings.get("standard_month_days", 26))

    # Check if a payroll run already exists for this period
    cursor.execute("""
        SELECT id, status FROM payroll_runs
        WHERE month = ? AND year = ? AND run_type = 'MONTHLY'
    """, (month, year))
    existing_run = cursor.fetchone()

    if existing_run and existing_run["status"] == "FINALIZED":
        return {
            "success": False,
            "error": f"Payroll for {period_name} is already FINALIZED and locked against changes."
        }

    if existing_run:
        payroll_run_id = existing_run["id"]
        cursor.execute("DELETE FROM payslips WHERE payroll_run_id = ?", (payroll_run_id,))
    else:
        cursor.execute("""
            INSERT INTO payroll_runs (run_type, month, year, period_name, start_date, end_date, status)
            VALUES ('MONTHLY', ?, ?, ?, ?, ?, 'DRAFT')
        """, (month, year, period_name, start_date, end_date))
        payroll_run_id = cursor.lastrowid

    # Fetch all active employees
    cursor.execute("""
        SELECT e.*, d.name as dept_name
        FROM employees e
        JOIN departments d ON e.department_id = d.id
        WHERE e.is_active = 1
        ORDER BY e.emp_no ASC
    """)
    employees = cursor.fetchall()

    run_total_gross = 0.0
    run_total_net = 0.0
    run_total_ot = 0.0
    run_total_shift_allowance = 0.0
    run_total_bonus = 0.0
    run_total_advances = 0.0
    processed_count = 0

    for emp_row in employees:
        emp = dict(emp_row)
        emp_id = emp["id"]

        # 1. Attendance & Shift Breakdown
        cursor.execute("""
            SELECT a.*, s.is_overnight, s.allowance_rate
            FROM attendance_records a
            LEFT JOIN shifts s ON a.shift_id = s.id
            WHERE a.employee_id = ? AND a.date >= ? AND a.date <= ?
        """, (emp_id, start_date, end_date))
        att_records = cursor.fetchall()

        present_days = 0
        half_days = 0
        absent_days = 0
        weekly_offs = 0
        night_shifts_count = 0
        total_shift_allowance = 0.0

        is_admin_emp = int(emp.get("is_admin") or 0) == 1

        for r_row in att_records:
            r = dict(r_row)
            status = r["status"]

            if status in ("PRESENT", "LATE"):
                present_days += 1
            elif status == "HALF_DAY":
                half_days += 1
            elif status == "WEEKLY_OFF":
                weekly_offs += 1
            elif status == "ABSENT":
                absent_days += 1

            if r["is_overnight"] and status in ("PRESENT", "LATE", "HALF_DAY"):
                night_shifts_count += 1

            total_shift_allowance += float(r["shift_allowance_amount"] or 0.0)

        # 2. Overtime Hours & Pay (auto-credited, no manual approval required)
        cursor.execute("""
            SELECT COALESCE(SUM(CASE WHEN approved_hours > 0 THEN approved_hours ELSE total_ot_hours END), 0.0) as total_ot_hrs,
                   COALESCE(SUM(calculated_ot_pay), 0.0) as total_ot_payout
            FROM overtime_records
            WHERE employee_id = ? AND date >= ? AND date <= ? AND status != 'REJECTED'
        """, (emp_id, start_date, end_date))
        ot_summary = cursor.fetchone()
        approved_ot_hours = round(float(ot_summary["total_ot_hrs"] or 0.0), 2)
        ot_pay = round(float(ot_summary["total_ot_payout"] or 0.0), 2)

        base_salary = float(emp.get("base_salary", 0.0))
        daily_rate = round(base_salary / std_days, 2) if std_days > 0 else 0.0
        hourly_rate = float(emp.get("ot_hourly_rate") or emp.get("hourly_rate") or (daily_rate / float(settings.get("standard_day_hours", 8.0))))

        if is_admin_emp:
            # Administrators are completely exempt from attendance tracking and receive full base salary
            present_days = std_days
            absent_days = 0
            half_days = 0
            effective_days_worked = float(std_days)
            lop_days = 0
            lop_deduction = 0.0
        else:
            effective_days_worked = present_days + (half_days * 0.5)
            lop_days = max(0, absent_days)
            lop_deduction = round(lop_days * daily_rate, 2)

        hra = float(emp.get("hra", 0.0))
        special_allowance = float(emp.get("special_allowance", 0.0))

        if emp["salary_type"] == "HOURLY":
            total_work_hours = sum(float(r["work_hours"] or 0.0) for r in att_records)
            base_earned = round(total_work_hours * hourly_rate, 2)
        else:
            base_earned = max(0.0, base_salary - lop_deduction)

        # Gross Earnings with Bonus
        emp_bonus = bonus_amount
        gross_earnings = round(base_earned + total_shift_allowance + ot_pay + hra + special_allowance + emp_bonus, 2)

        # Statutory Deductions (Deducted strictly if enabled on employee profile)
        pf_enabled = int(emp.get("pf_enabled", 0))
        esi_enabled = int(emp.get("esi_enabled", 0))
        pf_pct = float(emp.get("pf_deduction_pct", 12.0))
        esi_pct = float(emp.get("esi_deduction_pct", 0.75))
        tax_pct = float(emp.get("tax_deduction_pct", 0.0))

        pf_deduction = round(base_earned * (pf_pct / 100.0), 2) if pf_enabled == 1 else 0.0
        esi_deduction = round(gross_earnings * (esi_pct / 100.0), 2) if (esi_enabled == 1 and gross_earnings <= 25000) else 0.0
        tax_deduction = round(gross_earnings * (tax_pct / 100.0), 2) if tax_pct > 0 else 0.0
        statutory_deductions = round(pf_deduction + esi_deduction + tax_deduction, 2)

        # Salary Advance Deduction
        advance_rec = get_employee_active_advance(cursor, emp_id)
        advance_deduction = 0.0
        if advance_rec and advance_rec["remaining_amount"] > 0:
            scheduled_cut = float(advance_rec["weekly_deduction"] * 4.0 if advance_rec["weekly_deduction"] > 0 else advance_rec["remaining_amount"])
            max_available = max(0.0, gross_earnings - statutory_deductions)
            advance_deduction = round(min(scheduled_cut, float(advance_rec["remaining_amount"]), max_available), 2)

        total_deductions = round(statutory_deductions + advance_deduction, 2)
        net_pay = max(0.0, round(gross_earnings - total_deductions, 2))

        # Save Payslip
        cursor.execute("""
            INSERT INTO payslips (
                payroll_run_id, employee_id, pay_frequency, base_salary, daily_rate, hourly_rate,
                days_in_month, days_worked, half_days, weekly_offs, absent_days,
                total_shift_allowance, night_shifts_count, approved_ot_hours, ot_pay,
                hra, special_allowance, bonus, gross_earnings,
                pf_deduction, esi_deduction, tax_deduction, lop_deduction, advance_deduction,
                total_deductions, net_pay, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'GENERATED')
        """, (
            payroll_run_id, emp_id, emp.get("pay_frequency", "MONTHLY"), base_salary, daily_rate, hourly_rate,
            num_days, effective_days_worked, half_days, weekly_offs, absent_days,
            total_shift_allowance, night_shifts_count, approved_ot_hours, ot_pay,
            hra, special_allowance, emp_bonus, gross_earnings,
            pf_deduction, esi_deduction, tax_deduction, lop_deduction, advance_deduction,
            total_deductions, net_pay
        ))
        payslip_id = cursor.lastrowid
        try:
            build_and_store_payslip_day_timings(conn, payslip_id)
        except Exception:
            pass

        run_total_gross += gross_earnings
        run_total_net += net_pay
        run_total_ot += ot_pay
        run_total_shift_allowance += total_shift_allowance
        run_total_bonus += emp_bonus
        run_total_advances += advance_deduction
        processed_count += 1

    # Update payroll run summary
    cursor.execute("""
        UPDATE payroll_runs
        SET total_employees = ?, total_gross = ?, total_net = ?,
            total_ot_pay = ?, total_shift_allowance = ?,
            total_bonus = ?, total_advance_deductions = ?
        WHERE id = ?
    """, (
        processed_count,
        round(run_total_gross, 2),
        round(run_total_net, 2),
        round(run_total_ot, 2),
        round(run_total_shift_allowance, 2),
        round(run_total_bonus, 2),
        round(run_total_advances, 2),
        payroll_run_id
    ))

    conn.commit()

    return {
        "success": True,
        "payroll_run_id": payroll_run_id,
        "period_name": period_name,
        "processed_employees": processed_count,
        "total_gross": round(run_total_gross, 2),
        "total_net": round(run_total_net, 2),
        "total_ot_pay": round(run_total_ot, 2),
        "total_shift_allowance": round(run_total_shift_allowance, 2),
        "total_bonus": round(run_total_bonus, 2),
        "total_advance_deductions": round(run_total_advances, 2)
    }


def run_weekly_payroll(conn, start_date, end_date, period_name=None, bonus=0.0, filter_frequency="WEEKLY", user="HR Admin"):
    """
    Executes weekly payroll calculation for employees for a given date range (usually 7 days).
    Specialized for:
      - Weekly paid printing press workers
      - Accurate weekly attendance & night shift allowances
      - Weekly overtime payouts
      - Automatic deduction of weekly salary advance installments
      - Bonus / production incentives
    """
    cursor = conn.cursor()

    if not period_name:
        s_obj = datetime.strptime(start_date, "%Y-%m-%d")
        e_obj = datetime.strptime(end_date, "%Y-%m-%d")
        period_name = f"Week ({s_obj.strftime('%d %b')} - {e_obj.strftime('%d %b %Y')})"

    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    month = s_dt.month
    year = s_dt.year
    bonus_amount = max(0.0, float(bonus or 0.0))

    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    period_days = max(1, (e_dt - s_dt).days + 1)

    # Fetch settings
    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    settings_row = cursor.fetchone()
    settings = dict(settings_row) if settings_row else {
        "standard_month_days": 26,
        "standard_day_hours": 8.0,
        "currency_symbol": "₹"
    }

    # Check if this weekly payroll run already exists
    cursor.execute("""
        SELECT id, status FROM payroll_runs
        WHERE start_date = ? AND end_date = ? AND run_type = 'WEEKLY'
    """, (start_date, end_date))
    existing_run = cursor.fetchone()

    if existing_run and existing_run["status"] == "FINALIZED":
        return {
            "success": False,
            "error": f"Weekly Payroll for {period_name} is already FINALIZED and locked against changes."
        }

    if existing_run:
        payroll_run_id = existing_run["id"]
        cursor.execute("DELETE FROM payslips WHERE payroll_run_id = ?", (payroll_run_id,))
    else:
        cursor.execute("""
            INSERT INTO payroll_runs (run_type, month, year, period_name, start_date, end_date, status)
            VALUES ('WEEKLY', ?, ?, ?, ?, ?, 'DRAFT')
        """, (month, year, period_name, start_date, end_date))
        payroll_run_id = cursor.lastrowid

    # Filter target employees
    if filter_frequency == "WEEKLY":
        cursor.execute("""
            SELECT e.*, d.name as dept_name
            FROM employees e
            JOIN departments d ON e.department_id = d.id
            WHERE e.is_active = 1 AND e.pay_frequency = 'WEEKLY'
            ORDER BY e.emp_no ASC
        """)
        employees = cursor.fetchall()
        if not employees:
            # Fallback if no employees have been explicitly assigned WEEKLY yet
            cursor.execute("""
                SELECT e.*, d.name as dept_name
                FROM employees e
                JOIN departments d ON e.department_id = d.id
                WHERE e.is_active = 1
                ORDER BY e.emp_no ASC
            """)
            employees = cursor.fetchall()
    else:
        cursor.execute("""
            SELECT e.*, d.name as dept_name
            FROM employees e
            JOIN departments d ON e.department_id = d.id
            WHERE e.is_active = 1
            ORDER BY e.emp_no ASC
        """)
        employees = cursor.fetchall()

    run_total_gross = 0.0
    run_total_net = 0.0
    run_total_ot = 0.0
    run_total_shift_allowance = 0.0
    run_total_bonus = 0.0
    run_total_advances = 0.0
    processed_count = 0

    for emp_row in employees:
        emp = dict(emp_row)
        emp_id = emp["id"]

        # 1. Weekly Attendance Breakdown
        cursor.execute("""
            SELECT a.*, s.is_overnight, s.allowance_rate
            FROM attendance_records a
            LEFT JOIN shifts s ON a.shift_id = s.id
            WHERE a.employee_id = ? AND a.date >= ? AND a.date <= ?
        """, (emp_id, start_date, end_date))
        att_records = cursor.fetchall()

        present_days = 0
        half_days = 0
        absent_days = 0
        weekly_offs = 0
        night_shifts_count = 0
        total_shift_allowance = 0.0
        is_admin_emp = int(emp.get("is_admin") or 0) == 1

        for r_row in att_records:
            r = dict(r_row)
            status = r["status"]

            if status in ("PRESENT", "LATE"):
                present_days += 1
            elif status == "HALF_DAY":
                half_days += 1
            elif status == "WEEKLY_OFF":
                weekly_offs += 1
            elif status == "ABSENT":
                absent_days += 1

            if r["is_overnight"] and status in ("PRESENT", "LATE", "HALF_DAY"):
                night_shifts_count += 1

            total_shift_allowance += float(r["shift_allowance_amount"] or 0.0)

        effective_days_worked = present_days + (half_days * 0.5)

        # 2. Overtime Hours & Pay in the week (auto-credited, no manual approval required)
        cursor.execute("""
            SELECT COALESCE(SUM(CASE WHEN approved_hours > 0 THEN approved_hours ELSE total_ot_hours END), 0.0) as total_ot_hrs,
                   COALESCE(SUM(calculated_ot_pay), 0.0) as total_ot_payout
            FROM overtime_records
            WHERE employee_id = ? AND date >= ? AND date <= ? AND status != 'REJECTED'
        """, (emp_id, start_date, end_date))
        ot_summary = cursor.fetchone()
        approved_ot_hours = round(float(ot_summary["total_ot_hrs"] or 0.0), 2)
        ot_pay = round(float(ot_summary["total_ot_payout"] or 0.0), 2)

        # 3. Base Salary, Shift Salary & Rates
        shift_salary = float(emp.get("shift_salary") or (float(emp.get("base_salary", 0)) / 6.0) or 500.0)
        daily_rate = shift_salary
        base_salary = float(emp.get("base_salary") or (shift_salary * 6.0))

        # Hourly rate is strictly and exclusively applied for Overtime, NOT for shift
        ot_rate = float(emp.get("ot_hourly_rate") or emp.get("hourly_rate") or (shift_salary / 8.0))
        hourly_rate = ot_rate

        # Regular shift wages: based on actual shifts worked when logs exist; base_salary fallback
        if is_admin_emp:
            present_days = 6
            effective_days_worked = 6.0
            absent_days = 0
            base_earned = base_salary
        elif att_records:
            base_earned = round(effective_days_worked * shift_salary, 2)
        else:
            base_earned = max(0.0, base_salary - (absent_days * daily_rate))
        lop_deduction = 0.0

        # Simplified weekly model: No HRA, No Special Allowance, No TDS for weekly factory workers
        hra = 0.0
        special_allowance = 0.0

        # Gross Earnings with Bonus
        emp_bonus = bonus_amount
        gross_earnings = round(base_earned + total_shift_allowance + ot_pay + emp_bonus, 2)

        # Statutory Deductions: Deducted ONLY if pf_enabled or esi_enabled checkbox is checked
        pf_enabled = int(emp.get("pf_enabled", 0))
        esi_enabled = int(emp.get("esi_enabled", 0))
        pf_pct = float(emp.get("pf_deduction_pct", 12.0))
        esi_pct = float(emp.get("esi_deduction_pct", 0.75))

        pf_deduction = round(base_earned * (pf_pct / 100.0), 2) if pf_enabled == 1 else 0.0
        esi_deduction = round(gross_earnings * (esi_pct / 100.0), 2) if (esi_enabled == 1 and gross_earnings <= 6000) else 0.0
        tax_deduction = 0.0
        statutory_deductions = round(pf_deduction + esi_deduction, 2)

        # Weekly Salary Advance Cut ("advance required to cut weakly")
        advance_rec = get_employee_active_advance(cursor, emp_id)
        advance_deduction = 0.0
        if advance_rec and advance_rec["remaining_amount"] > 0:
            scheduled_cut = float(advance_rec["weekly_deduction"] if advance_rec["weekly_deduction"] > 0 else advance_rec["remaining_amount"])
            max_available = max(0.0, gross_earnings - statutory_deductions)
            advance_deduction = round(min(scheduled_cut, float(advance_rec["remaining_amount"]), max_available), 2)

        total_deductions = round(statutory_deductions + advance_deduction, 2)
        net_pay = max(0.0, round(gross_earnings - total_deductions, 2))

        # Save Weekly Payslip
        cursor.execute("""
            INSERT INTO payslips (
                payroll_run_id, employee_id, pay_frequency, base_salary, daily_rate, hourly_rate,
                days_in_month, days_worked, half_days, weekly_offs, absent_days,
                total_shift_allowance, night_shifts_count, approved_ot_hours, ot_pay,
                hra, special_allowance, bonus, gross_earnings,
                pf_deduction, esi_deduction, tax_deduction, lop_deduction, advance_deduction,
                total_deductions, net_pay, status
            ) VALUES (?, ?, 'WEEKLY', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'GENERATED')
        """, (
            payroll_run_id, emp_id, base_salary, daily_rate, hourly_rate,
            period_days, effective_days_worked, half_days, weekly_offs, absent_days,
            total_shift_allowance, night_shifts_count, approved_ot_hours, ot_pay,
            hra, special_allowance, emp_bonus, gross_earnings,
            pf_deduction, esi_deduction, tax_deduction, lop_deduction, advance_deduction,
            total_deductions, net_pay
        ))
        payslip_id = cursor.lastrowid
        try:
            build_and_store_payslip_day_timings(conn, payslip_id)
        except Exception:
            pass

        run_total_gross += gross_earnings
        run_total_net += net_pay
        run_total_ot += ot_pay
        run_total_shift_allowance += total_shift_allowance
        run_total_bonus += emp_bonus
        run_total_advances += advance_deduction
        processed_count += 1

    # Update payroll run summary
    cursor.execute("""
        UPDATE payroll_runs
        SET total_employees = ?, total_gross = ?, total_net = ?,
            total_ot_pay = ?, total_shift_allowance = ?,
            total_bonus = ?, total_advance_deductions = ?
        WHERE id = ?
    """, (
        processed_count,
        round(run_total_gross, 2),
        round(run_total_net, 2),
        round(run_total_ot, 2),
        round(run_total_shift_allowance, 2),
        round(run_total_bonus, 2),
        round(run_total_advances, 2),
        payroll_run_id
    ))

    conn.commit()

    return {
        "success": True,
        "payroll_run_id": payroll_run_id,
        "period_name": period_name,
        "processed_employees": processed_count,
        "total_gross": round(run_total_gross, 2),
        "total_net": round(run_total_net, 2),
        "total_ot_pay": round(run_total_ot, 2),
        "total_shift_allowance": round(run_total_shift_allowance, 2),
        "total_bonus": round(run_total_bonus, 2),
        "total_advance_deductions": round(run_total_advances, 2)
    }


def build_and_store_payslip_day_timings(conn, payslip_id):
    """
    Builds day-by-day attendance & punch timing register with cross-midnight awareness,
    calculates daily wages & overtime pay, and permanently freezes them in payslip_day_timings table.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.*, pr.start_date, pr.end_date, pr.run_type,
               e.emp_no, (e.first_name || ' ' || COALESCE(e.last_name, '')) as emp_name,
               e.shift_salary, e.ot_hourly_rate, e.hourly_rate, e.weekly_off_day
        FROM payslips p
        JOIN payroll_runs pr ON p.payroll_run_id = pr.id
        JOIN employees e ON p.employee_id = e.id
        WHERE p.id = ?
    """, (payslip_id,))
    p_row = cursor.fetchone()
    if not p_row:
        return []
    payslip = dict(p_row)
    emp_id = payslip["employee_id"]
    start_date = payslip["start_date"]
    end_date = payslip["end_date"]

    s_dt = datetime.strptime(start_date, "%Y-%m-%d")
    e_dt = datetime.strptime(end_date, "%Y-%m-%d")
    curr_dt = s_dt
    all_dates = []
    while curr_dt <= e_dt:
        all_dates.append(curr_dt.strftime("%Y-%m-%d"))
        curr_dt += timedelta(days=1)

    cursor.execute("""
        SELECT ar.*
        FROM attendance_records ar
        WHERE ar.employee_id = ? AND ar.date >= ? AND ar.date <= ?
        ORDER BY ar.date ASC
    """, (emp_id, start_date, end_date))
    att_rows = [dict(r) for r in cursor.fetchall()]
    att_map = {r["date"]: r for r in att_rows}

    ext_end_date = (e_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    cursor.execute("""
        SELECT punch_time, punch_type
        FROM attendance_punches
        WHERE employee_id = ? AND punch_time >= ? AND punch_time <= ?
        ORDER BY punch_time ASC
    """, (emp_id, f"{start_date} 00:00:00", f"{ext_end_date} 12:00:00"))
    all_punches = [dict(r) for r in cursor.fetchall()]

    day_punches = {d: [] for d in all_dates}
    day_punches[ext_end_date] = []
    for p in all_punches:
        p_date = p["punch_time"][:10]
        if p_date in day_punches:
            day_punches[p_date].append(p["punch_time"])

    # Cross-midnight punch adjustment:
    # If early morning punch (<07:30) follows previous evening punch (>=20:00), move to prev day as checkout
    for i in range(1, len(all_dates) + 1):
        d_curr = all_dates[i] if i < len(all_dates) else ext_end_date
        d_prev = all_dates[i-1]
        curr_list = day_punches.get(d_curr, [])
        prev_list = day_punches.get(d_prev, [])

        if curr_list and prev_list:
            first_t = curr_list[0][11:19]
            last_prev_t = prev_list[-1][11:19]
            first_h = int(first_t[:2])
            first_m = int(first_t[3:5])
            last_prev_h = int(last_prev_t[:2])

            if (first_h < 7 or (first_h == 7 and first_m <= 30)) and last_prev_h >= 20:
                moved_punch = curr_list.pop(0)
                prev_list.append(moved_punch)

    daily_attendance = []
    daily_rate = float(payslip.get("daily_rate") or 0.0)
    hourly_rate = float(payslip.get("hourly_rate") or 0.0)

    for d in all_dates:
        ar = att_map.get(d)
        punches_list = [p[11:16] for p in day_punches.get(d, [])]
        dt_obj = datetime.strptime(d, "%Y-%m-%d")
        day_name = dt_obj.strftime("%A")

        if ar:
            status = ar["status"]
        elif dt_obj.weekday() == int(payslip.get("weekly_off_day", 6)):
            status = "WEEKLY_OFF"
        else:
            status = "ABSENT"

        if punches_list:
            first_in = punches_list[0]
            last_out = punches_list[-1] if len(punches_list) > 1 else (ar["punch_out"][11:16] if (ar and ar.get("punch_out")) else punches_list[0])
        elif ar and ar.get("punch_in"):
            first_in = ar["punch_in"][11:16]
            last_out = ar["punch_out"][11:16] if ar.get("punch_out") else None
        else:
            first_in = None
            last_out = None

        work_hours = round(float(ar["work_hours"] or 0.0), 2) if ar else 0.0
        ot_hours = round(float(ar["raw_ot_hours"] or 0.0), 2) if ar else 0.0

        if status in ("PRESENT", "LATE"):
            shift_wage = daily_rate
        elif status == "HALF_DAY":
            shift_wage = round(daily_rate * 0.5, 2)
        else:
            shift_wage = 0.0

        ot_pay = round(ot_hours * hourly_rate, 2)
        day_total_pay = round(shift_wage + ot_pay, 2)
        punches_text = ", ".join(punches_list)

        cursor.execute("""
            INSERT INTO payslip_day_timings (
                payslip_id, payroll_run_id, employee_id, emp_no, employee_name,
                date, day_name, status, punch_in, punch_out, punches_text,
                work_hours, ot_hours, shift_wage, ot_rate, ot_pay, day_total_pay
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(payslip_id, date) DO UPDATE SET
                payroll_run_id = excluded.payroll_run_id,
                employee_id = excluded.employee_id,
                emp_no = excluded.emp_no,
                employee_name = excluded.employee_name,
                day_name = excluded.day_name,
                status = excluded.status,
                punch_in = excluded.punch_in,
                punch_out = excluded.punch_out,
                punches_text = excluded.punches_text,
                work_hours = excluded.work_hours,
                ot_hours = excluded.ot_hours,
                shift_wage = excluded.shift_wage,
                ot_rate = excluded.ot_rate,
                ot_pay = excluded.ot_pay,
                day_total_pay = excluded.day_total_pay
        """, (
            payslip_id, payslip.get("payroll_run_id"), emp_id, payslip["emp_no"], payslip["emp_name"],
            d, day_name, status, first_in, last_out, punches_text,
            work_hours, ot_hours, shift_wage, hourly_rate, ot_pay, day_total_pay
        ))

        daily_attendance.append({
            "date": d,
            "day_name": day_name,
            "day_short": day_name[:3],
            "status": status,
            "punch_in": first_in,
            "punch_out": last_out,
            "punches": punches_list,
            "punches_text": punches_text,
            "work_hours": work_hours,
            "ot_hours": ot_hours,
            "shift_wage": shift_wage,
            "ot_rate": hourly_rate,
            "ot_pay": ot_pay,
            "day_total_pay": day_total_pay,
            "has_missed_mid_punch": ar["has_missed_mid_punch"] if ar else 0,
            "mid_punch_status": ar["mid_punch_status"] if ar else "NORMAL"
        })

    conn.commit()
    return daily_attendance


def finalize_payroll_run(conn, run_id):
    """
    Finalizes a payroll run and locks it against further changes.
    Also executes advance deduction settlements into advances & advance_repayments tables.
    """
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM payroll_runs WHERE id = ?", (run_id,))
    run_row = cursor.fetchone()
    if not run_row:
        return {"success": False, "error": "Payroll run not found."}

    run = dict(run_row)
    if run["status"] == "FINALIZED":
        return {"success": False, "error": "This payroll run is already finalized."}

    # Fetch all payslips for this run with advance deductions
    cursor.execute("SELECT * FROM payslips WHERE payroll_run_id = ?", (run_id,))
    payslips = cursor.fetchall()

    today_str = date.today().strftime("%Y-%m-%d")

    for p_row in payslips:
        p = dict(p_row)
        emp_id = p["employee_id"]
        deduction = float(p.get("advance_deduction", 0.0) or 0.0)

        if deduction > 0:
            cursor.execute("""
                SELECT * FROM advances
                WHERE employee_id = ? AND status = 'ACTIVE' AND remaining_amount > 0
                ORDER BY id ASC
                LIMIT 1
            """, (emp_id,))
            adv_row = cursor.fetchone()

            if adv_row:
                adv = dict(adv_row)
                adv_id = adv["id"]
                new_repaid = round(float(adv["amount_repaid"]) + deduction, 2)
                new_remaining = max(0.0, round(float(adv["remaining_amount"]) - deduction, 2))
                new_status = "COMPLETED" if new_remaining <= 0.01 else "ACTIVE"

                cursor.execute("""
                    UPDATE advances
                    SET amount_repaid = ?, remaining_amount = ?, status = ?
                    WHERE id = ?
                """, (new_repaid, new_remaining, new_status, adv_id))

                # Log into advance_repayments ledger
                cursor.execute("""
                    INSERT INTO advance_repayments (
                        advance_id, payroll_run_id, payslip_id, repayment_date, amount, payment_type, notes
                    ) VALUES (?, ?, ?, ?, ?, 'PAYROLL_CUT', ?)
                """, (
                    adv_id, run_id, p["id"], today_str, deduction,
                    f"Deducted from {run['period_name']} payslip"
                ))

    # Mark run and payslips as FINALIZED
    cursor.execute("UPDATE payroll_runs SET status = 'FINALIZED' WHERE id = ?", (run_id,))
    cursor.execute("UPDATE payslips SET status = 'FINALIZED' WHERE payroll_run_id = ?", (run_id,))

    conn.commit()
    return {"success": True, "period_name": run["period_name"]}


def adjust_draft_payslip(conn, payslip_id, bonus=None, advance_deduction=None):
    """
    Allows adjusting bonus and/or advance deduction on a draft payslip before finalization.
    Recalculates gross, statutory deductions, total deductions, and net pay.
    """
    cursor = conn.cursor()

    cursor.execute("""
        SELECT p.*, r.status as run_status
        FROM payslips p
        JOIN payroll_runs r ON p.payroll_run_id = r.id
        WHERE p.id = ?
    """, (payslip_id,))
    p_row = cursor.fetchone()
    if not p_row:
        return {"success": False, "error": "Payslip not found."}

    p = dict(p_row)
    if p["run_status"] == "FINALIZED" or p["status"] == "FINALIZED":
        return {"success": False, "error": "Cannot adjust payslip of a finalized payroll run."}

    new_bonus = float(bonus) if bonus is not None else float(p.get("bonus", 0.0) or 0.0)
    new_advance = float(advance_deduction) if advance_deduction is not None else float(p.get("advance_deduction", 0.0) or 0.0)

    base_earned = max(0.0, float(p["base_salary"]) - float(p["lop_deduction"]))
    shift_allowance = float(p["total_shift_allowance"])
    ot_pay = float(p["ot_pay"])
    hra = float(p["hra"])
    special = float(p["special_allowance"])

    gross_earnings = round(base_earned + shift_allowance + ot_pay + hra + special + new_bonus, 2)

    pf = float(p["pf_deduction"])
    esi = float(p["esi_deduction"])
    tax = float(p["tax_deduction"])

    statutory = pf + esi + tax
    total_deductions = round(statutory + new_advance, 2)
    net_pay = max(0.0, round(gross_earnings - total_deductions, 2))

    cursor.execute("""
        UPDATE payslips
        SET bonus = ?, advance_deduction = ?, gross_earnings = ?, total_deductions = ?, net_pay = ?
        WHERE id = ?
    """, (new_bonus, new_advance, gross_earnings, total_deductions, net_pay, payslip_id))

    # Recalculate parent run summary
    run_id = p["payroll_run_id"]
    cursor.execute("""
        SELECT 
            COALESCE(SUM(gross_earnings), 0.0) as total_gross,
            COALESCE(SUM(net_pay), 0.0) as total_net,
            COALESCE(SUM(bonus), 0.0) as total_bonus,
            COALESCE(SUM(advance_deduction), 0.0) as total_advances
        FROM payslips WHERE payroll_run_id = ?
    """, (run_id,))
    totals = dict(cursor.fetchone())

    cursor.execute("""
        UPDATE payroll_runs
        SET total_gross = ?, total_net = ?, total_bonus = ?, total_advance_deductions = ?
        WHERE id = ?
    """, (totals["total_gross"], totals["total_net"], totals["total_bonus"], totals["total_advances"], run_id))

    conn.commit()
    return {"success": True, "new_gross": gross_earnings, "new_net": net_pay}
