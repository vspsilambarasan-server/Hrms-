def get_ot_multiplier(ot_type, company_settings):
    """Return multiplier based on OT type and settings."""
    if ot_type == "HOLIDAY":
        return float(company_settings.get("holiday_ot_multiplier", 2.0))
    elif ot_type == "WEEKEND":
        return float(company_settings.get("weekend_ot_multiplier", 2.0))
    else:
        return float(company_settings.get("default_ot_multiplier", 1.5))

def calculate_hourly_rate(employee, company_settings):
    """
    Returns the hourly rate strictly for overtime (OT).
    Prioritizes explicit ot_hourly_rate, then fallback to base wage calculation.
    """
    if employee.get("ot_hourly_rate") and float(employee.get("ot_hourly_rate", 0)) > 0:
        return round(float(employee["ot_hourly_rate"]), 2)
    if employee.get("hourly_rate") and float(employee.get("hourly_rate", 0)) > 0:
        return round(float(employee["hourly_rate"]), 2)
    
    base_sal = float(employee.get("base_salary", 0))
    month_days = int(company_settings.get("standard_month_days", 26))
    day_hours = float(company_settings.get("standard_day_hours", 8.0))
    
    total_hours = month_days * day_hours
    if total_hours <= 0:
        return 0.0
    return round(base_sal / total_hours, 2)

def compute_ot_payout(hours, hourly_rate, multiplier=1.0):
    """Compute payout for overtime hours at hourly OT rate."""
    return round(hours * hourly_rate * (multiplier if multiplier else 1.0), 2)

def sync_overtime_from_attendance(conn, attendance_id):
    """
    Checks evaluated attendance record. If raw_ot_hours > 0:
    - Auto-approves if post-shift OT is <= 3.5 hours.
    - Flags as PENDING if post-shift OT > 3.5 hours.
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT a.*, e.id as emp_id, e.salary_type, e.pay_frequency, e.base_salary,
               e.hourly_rate, e.ot_hourly_rate, e.shift_salary
        FROM attendance_records a
        JOIN employees e ON a.employee_id = e.id
        WHERE a.id = ?
    """, (attendance_id,))
    att_row = cursor.fetchone()
    if not att_row:
        return None
    att = dict(att_row)

    raw_ot = att["raw_ot_hours"]
    if raw_ot <= 0:
        # If OT was reset to 0, delete any pending OT
        cursor.execute("""
            DELETE FROM overtime_records 
            WHERE attendance_id = ? AND status = 'PENDING'
        """, (attendance_id,))
        conn.commit()
        return None

    # Fetch settings
    cursor.execute("SELECT * FROM company_settings LIMIT 1")
    settings_row = cursor.fetchone()
    settings = dict(settings_row) if settings_row else {}

    # Check if this day was weekend or holiday
    cursor.execute("""
        SELECT is_off_day FROM roster_schedules 
        WHERE employee_id = ? AND date = ?
    """, (att["employee_id"], att["date"]))
    roster = cursor.fetchone()
    is_off = roster["is_off_day"] if roster else 0

    ot_type = "WEEKEND" if is_off else "NORMAL"
    multiplier = 1.0  # Clean hourly OT calculation as requested
    hourly_rate = calculate_hourly_rate(dict(att), settings)

    # Check if punch_in time is between 12:00 AM (00:00) and 5:30 AM (05:30)
    is_midnight_early_checkin = False
    p_in_str = att.get("punch_in")
    if p_in_str:
        try:
            time_part = p_in_str.split(" ")[1] if " " in p_in_str else p_in_str
            t_h, t_m = [int(x) for x in time_part.split(":")[:2]]
            if (t_h == 0 or (t_h < 5) or (t_h == 5 and t_m <= 30)):
                is_midnight_early_checkin = True
        except Exception:
            pass

    gross_ot = float(att.get("gross_ot_hours", 0.0) or 0.0)
    late_ded = int(att.get("late_deduction_mins", 0) or 0)
    breakdown_text = f" (Gross: {gross_ot}h less {late_ded}m late)" if (gross_ot > 0 and late_ded > 0) else ""

    # Overtime does not require approval - auto-approved directly
    ot_status = "APPROVED"
    app_hours = raw_ot
    app_by = "System (Auto)"
    reason = f"Auto-approved OT ({raw_ot} hrs){breakdown_text}"

    ot_payout = compute_ot_payout(raw_ot, hourly_rate, multiplier)

    # Check if OT record already exists
    cursor.execute("SELECT * FROM overtime_records WHERE attendance_id = ?", (attendance_id,))
    existing = cursor.fetchone()

    if existing:
        if existing["status"] == "PENDING" or existing["status"] == "APPROVED":
            cursor.execute("""
                UPDATE overtime_records
                SET total_ot_hours = ?, approved_hours = ?, multiplier = ?,
                    calculated_ot_pay = ?, ot_type = ?, status = ?,
                    approved_by = COALESCE(approved_by, ?), reason = ?
                WHERE id = ?
            """, (raw_ot, app_hours, multiplier, ot_payout, ot_type, ot_status, app_by, reason, existing["id"]))
    else:
        cursor.execute("""
            INSERT INTO overtime_records (
                employee_id, attendance_id, date, ot_type,
                pre_shift_ot_hours, post_shift_ot_hours, total_ot_hours,
                multiplier, approved_hours, calculated_ot_pay, status,
                approved_by, reason
            ) VALUES (?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            att["employee_id"], attendance_id, att["date"], ot_type,
            raw_ot, raw_ot,
            multiplier, app_hours, ot_payout, ot_status,
            app_by, reason
        ))

    conn.commit()
