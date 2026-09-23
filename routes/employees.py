import json
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from database import get_db_connection
from models.ot_engine import calculate_hourly_rate
from routes.auth import login_required

employees_bp = Blueprint("employees", __name__, url_prefix="/employees")

@employees_bp.route("/")
@login_required
def list_employees():
    conn = get_db_connection()
    cursor = conn.cursor()

    dept_id = request.args.get("dept")
    freq = request.args.get("freq")
    status_filter = request.args.get("status", "ACTIVE").strip().upper()
    if not status_filter or status_filter == "ALL":
        status_filter = "ACTIVE"
    search = request.args.get("q", "").strip()

    query = """
        SELECT e.*, d.name as dept_name, d.code as dept_code, s.name as default_shift_name, s.color as shift_color
        FROM employees e
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN shifts s ON e.default_shift_id = s.id
        WHERE 1=1
    """
    params = []

    if dept_id and dept_id.isdigit():
        query += " AND e.department_id = ?"
        params.append(int(dept_id))

    if freq and freq in ("WEEKLY", "MONTHLY"):
        query += " AND (e.pay_frequency = ? OR e.salary_type = ?)"
        params.extend([freq, freq])

    tab_map = {
        "RELIEVED": "relieved",
        "SUSPENDED": "suspended",
        "NO_CALL_NO_SHOW": "ncns"
    }
    if status_filter in tab_map:
        return redirect(url_for("employees.separated_employees", tab=tab_map[status_filter]))

    # Strict Business Rule: "All Employees" directory must STRICTLY and EXCLUSIVELY show ACTIVE working employees.
    # NCNS, Suspended, Relieved, and Deleted personnel are NEVER shown here and are isolated in /employees/separated.
    query += " AND e.employment_status = 'ACTIVE' AND e.is_active = 1"
    status_filter = "ACTIVE"

    if search:
        query += " AND (e.first_name LIKE ? OR e.last_name LIKE ? OR e.emp_no LIKE ? OR e.designation LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term, term])

    query += " ORDER BY e.emp_no ASC"
    cursor.execute(query, params)
    employees = [dict(row) for row in cursor.fetchall()]

    # Status counts for filter pills and inactive registers
    cursor.execute("""
        SELECT 
            COUNT(CASE WHEN employment_status = 'ACTIVE' AND is_active = 1 THEN 1 END) as active,
            COUNT(CASE WHEN employment_status = 'RELIEVED' THEN 1 END) as relieved,
            COUNT(CASE WHEN employment_status = 'SUSPENDED' THEN 1 END) as suspended,
            COUNT(CASE WHEN employment_status = 'NO_CALL_NO_SHOW' THEN 1 END) as ncns
        FROM employees
    """)
    status_counts = dict(cursor.fetchone() or {"active": 0, "relieved": 0, "suspended": 0, "ncns": 0})
    cursor.execute("SELECT COUNT(*) as cnt FROM employee_deletion_logs")
    status_counts["deleted"] = cursor.fetchone()["cnt"]
    status_counts["total"] = status_counts["active"]

    cursor.execute("SELECT * FROM departments ORDER BY name")
    departments = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT * FROM shifts WHERE is_active = 1 ORDER BY name")
    shifts = [dict(row) for row in cursor.fetchall()]

    conn.close()

    from models.auto_sync import get_syncer
    syncer = get_syncer()
    device_ip = syncer.device_ip if syncer else "192.168.101.201"

    return render_template(
        "employees/index.html",
        employees=employees,
        departments=departments,
        shifts=shifts,
        selected_dept=dept_id,
        selected_freq=freq,
        selected_status=status_filter,
        status_counts=status_counts,
        search_term=search,
        device_ip=device_ip,
        today_date=date.today().strftime("%Y-%m-%d")
    )

@employees_bp.route("/sync-device", methods=["POST"])
@login_required
def sync_device():
    """Import newly enrolled personnel from the physical ZKTeco biometric machine."""
    device_ip = request.form.get("device_ip", "192.168.101.201").strip()
    from models.auto_sync import SYNC_LOCK
    from models.biometric_sync import sync_device_users, sync_device_attendance

    with SYNC_LOCK:
        user_res = sync_device_users(ip=device_ip)
        # Also run recent punch sync so newly enrolled staff get their logs immediately
        if user_res.get("success"):
            sync_device_attendance(ip=device_ip, days_back=7)

    if user_res.get("success"):
        added = user_res.get("added_employees", 0)
        updated = user_res.get("updated_employees", 0)
        total = user_res.get("total_device_users", 0)
        if added > 0:
            flash(f"Biometric sync successful! Found {total} enrolled staff on machine ({device_ip}). Successfully enrolled {added} new active staff profile(s) into the workforce directory.", "success")
        elif updated > 0:
            flash(f"Biometric sync complete! Found {total} enrolled staff on machine ({device_ip}). Updated {updated} profiles with machine details.", "info")
        else:
            flash(f"Biometric sync complete! All {total} enrolled staff on machine ({device_ip}) are already present and active in the directory.", "info")
    else:
        flash(f"Failed to sync enrolled staff from device ({device_ip}): {user_res.get('error')}", "danger")

    return redirect(url_for("employees.list_employees"))

@employees_bp.route("/add", methods=["POST"])
@login_required
def add_employee():
    emp_no = request.form.get("emp_no", "").strip().upper()
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    department_id = int(request.form.get("department_id"))
    designation = request.form.get("designation", "").strip()
    join_date = request.form.get("join_date", "").strip()
    default_shift_id = request.form.get("default_shift_id")
    default_shift_id = int(default_shift_id) if default_shift_id else None

    # Salary type strictly WEEKLY or MONTHLY
    salary_type = request.form.get("salary_type", "WEEKLY").upper()
    if salary_type not in ("WEEKLY", "MONTHLY"):
        salary_type = "WEEKLY"
    pay_frequency = salary_type

    shift_salary = float(request.form.get("shift_salary", 0.0) or 0.0)
    ot_hourly_rate = float(request.form.get("ot_hourly_rate", 0.0) or request.form.get("hourly_rate", 0.0) or 0.0)
    base_salary = float(request.form.get("base_salary", 0.0) or 0.0)

    if salary_type == "WEEKLY":
        if shift_salary <= 0 and base_salary > 0:
            shift_salary = round(base_salary / 6.0, 2)
        elif base_salary <= 0 and shift_salary > 0:
            base_salary = round(shift_salary * 6.0, 2)
        if ot_hourly_rate <= 0:
            ot_hourly_rate = round(shift_salary / 8.0, 2) if shift_salary > 0 else 75.0
    else: # MONTHLY
        if base_salary <= 0 and shift_salary > 0:
            base_salary = round(shift_salary * 26.0, 2)
        elif shift_salary <= 0 and base_salary > 0:
            shift_salary = round(base_salary / 26.0, 2)
        if ot_hourly_rate <= 0:
            ot_hourly_rate = round(shift_salary / 8.0, 2) if shift_salary > 0 else 75.0

    # Hourly payment is strictly for OT
    hourly_rate = ot_hourly_rate

    # PF & ESI Available Checkboxes (Deducted only if enabled)
    pf_enabled = 1 if request.form.get("pf_enabled") in ("1", "on", "true") else 0
    esi_enabled = 1 if request.form.get("esi_enabled") in ("1", "on", "true") else 0

    # Safety & Machine Training Completed (Factories Act compliance)
    safety_training_completed = 1 if request.form.get("safety_training_completed") in ("1", "on", "true") else 0
    safety_training_date = request.form.get("safety_training_date") or date.today().strftime("%Y-%m-%d")
    safety_training_approved_by = request.form.get("safety_training_approved_by", "").strip() or "Press Safety Officer"

    hra = float(request.form.get("hra", 0.0) or 0.0)
    special_allowance = float(request.form.get("special_allowance", 0.0) or 0.0)
    pf_pct = float(request.form.get("pf_deduction_pct", 12.0) or 12.0)
    esi_pct = float(request.form.get("esi_deduction_pct", 0.75) or 0.75)
    tax_pct = float(request.form.get("tax_deduction_pct", 0.0) or 0.0)
    weekly_off = int(request.form.get("weekly_off_day", 6))

    # Employment status & active flag
    employment_status = request.form.get("employment_status", "ACTIVE").strip().upper()
    if employment_status not in ("ACTIVE", "RELIEVED", "SUSPENDED", "NO_CALL_NO_SHOW"):
        employment_status = "ACTIVE"
    is_active = 1 if employment_status == "ACTIVE" else 0
    status_date = request.form.get("status_date") or date.today().strftime("%Y-%m-%d")
    status_reason = request.form.get("status_reason", "").strip() or ("New Joining" if employment_status == "ACTIVE" else employment_status)

    # Admin status: Exempt from attendance tracking
    is_admin = 1 if request.form.get("is_admin") in ("1", "on", "true") else 0

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO employees (
                emp_no, first_name, last_name, email, phone, department_id,
                designation, join_date, default_shift_id, salary_type, pay_frequency,
                base_salary, hourly_rate, shift_salary, ot_hourly_rate,
                pf_enabled, esi_enabled, is_admin,
                safety_training_completed, safety_training_date, safety_training_approved_by,
                hra, special_allowance,
                pf_deduction_pct, esi_deduction_pct, tax_deduction_pct, weekly_off_day,
                employment_status, status_date, status_reason, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            emp_no, first_name, last_name, email, phone, department_id,
            designation, join_date, default_shift_id, salary_type, pay_frequency,
            base_salary, hourly_rate, shift_salary, ot_hourly_rate,
            pf_enabled, esi_enabled, is_admin,
            safety_training_completed, safety_training_date, safety_training_approved_by,
            hra, special_allowance,
            pf_pct, esi_pct, tax_pct, weekly_off,
            employment_status, status_date, status_reason, is_active
        ))
        conn.commit()
        flash(f"Employee {first_name} {last_name} ({emp_no}) registered successfully!", "success")
    except Exception as e:
        flash(f"Error adding employee: {str(e)}", "danger")
    finally:
        conn.close()

    return redirect(url_for("employees.list_employees"))

@employees_bp.route("/<int:emp_id>")
@login_required
def view_employee(emp_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT e.*, d.name as dept_name, s.name as default_shift_name, s.code as shift_code
        FROM employees e
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN shifts s ON e.default_shift_id = s.id
        WHERE e.id = ?
    """, (emp_id,))
    emp_row = cursor.fetchone()
    if not emp_row:
        flash("Employee not found", "danger")
        conn.close()
        return redirect(url_for("employees.list_employees"))

    employee = dict(emp_row)

    # Attendance history (last 30 records)
    cursor.execute("""
        SELECT a.*, s.name as shift_name, s.code as shift_code, s.color as shift_color
        FROM attendance_records a
        LEFT JOIN shifts s ON a.shift_id = s.id
        WHERE a.employee_id = ?
        ORDER BY a.date DESC
        LIMIT 30
    """, (emp_id,))
    attendances = [dict(row) for row in cursor.fetchall()]

    # Overtime history
    cursor.execute("""
        SELECT o.*, a.work_hours
        FROM overtime_records o
        LEFT JOIN attendance_records a ON o.attendance_id = a.id
        WHERE o.employee_id = ?
        ORDER BY o.date DESC
        LIMIT 20
    """, (emp_id,))
    overtimes = [dict(row) for row in cursor.fetchall()]

    # Payslips history
    cursor.execute("""
        SELECT p.*, r.period_name, r.run_type
        FROM payslips p
        JOIN payroll_runs r ON p.payroll_run_id = r.id
        WHERE p.employee_id = ?
        ORDER BY p.id DESC
        LIMIT 20
    """, (emp_id,))
    payslips = [dict(row) for row in cursor.fetchall()]

    # Advances and loans history
    cursor.execute("""
        SELECT * FROM advances
        WHERE employee_id = ?
        ORDER BY id DESC
    """, (emp_id,))
    advances = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT * FROM departments ORDER BY name")
    departments = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT * FROM shifts WHERE is_active = 1 ORDER BY name")
    shifts = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return render_template(
        "employees/view.html",
        employee=employee,
        attendances=attendances,
        overtimes=overtimes,
        payslips=payslips,
        advances=advances,
        departments=departments,
        shifts=shifts
    )

@employees_bp.route("/<int:emp_id>/edit", methods=["POST"])
@login_required
def edit_employee(emp_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        department_id = int(request.form.get("department_id"))
        designation = request.form.get("designation", "").strip()
        default_shift_id = request.form.get("default_shift_id")
        default_shift_id = int(default_shift_id) if default_shift_id else None

        salary_type = request.form.get("salary_type", "WEEKLY").upper()
        if salary_type not in ("WEEKLY", "MONTHLY"):
            salary_type = "WEEKLY"
        pay_frequency = salary_type

        shift_salary = float(request.form.get("shift_salary", 0.0) or 0.0)
        ot_hourly_rate = float(request.form.get("ot_hourly_rate", 0.0) or request.form.get("hourly_rate", 0.0) or 0.0)
        base_salary = float(request.form.get("base_salary", 0.0) or 0.0)

        if salary_type == "WEEKLY":
            if shift_salary <= 0 and base_salary > 0:
                shift_salary = round(base_salary / 6.0, 2)
            elif base_salary <= 0 and shift_salary > 0:
                base_salary = round(shift_salary * 6.0, 2)
            if ot_hourly_rate <= 0:
                ot_hourly_rate = round(shift_salary / 8.0, 2) if shift_salary > 0 else 75.0
        else: # MONTHLY
            if base_salary <= 0 and shift_salary > 0:
                base_salary = round(shift_salary * 26.0, 2)
            elif shift_salary <= 0 and base_salary > 0:
                shift_salary = round(base_salary / 26.0, 2)
            if ot_hourly_rate <= 0:
                ot_hourly_rate = round(shift_salary / 8.0, 2) if shift_salary > 0 else 75.0

        hourly_rate = ot_hourly_rate

        pf_enabled = 1 if request.form.get("pf_enabled") in ("1", "on", "true") else 0
        esi_enabled = 1 if request.form.get("esi_enabled") in ("1", "on", "true") else 0

        safety_training_completed = 1 if request.form.get("safety_training_completed") in ("1", "on", "true") else 0
        safety_training_date = request.form.get("safety_training_date") or date.today().strftime("%Y-%m-%d")
        safety_training_approved_by = request.form.get("safety_training_approved_by", "").strip() or "Press Safety Officer"

        employment_status = request.form.get("employment_status", "").strip().upper()
        if employment_status not in ("ACTIVE", "RELIEVED", "SUSPENDED", "NO_CALL_NO_SHOW"):
            employment_status = "ACTIVE" if request.form.get("is_active") == "1" else "RELIEVED"

        is_active = 1 if employment_status == "ACTIVE" else 0
        status_date = request.form.get("status_date") or date.today().strftime("%Y-%m-%d")
        status_reason = request.form.get("status_reason", "").strip()

        is_admin = 1 if request.form.get("is_admin") in ("1", "on", "true") else 0

        cursor.execute("""
            UPDATE employees
            SET first_name = ?, last_name = ?, email = ?, phone = ?,
                department_id = ?, designation = ?, default_shift_id = ?,
                salary_type = ?, pay_frequency = ?, base_salary = ?, hourly_rate = ?,
                shift_salary = ?, ot_hourly_rate = ?,
                pf_enabled = ?, esi_enabled = ?, is_admin = ?,
                safety_training_completed = ?, safety_training_date = ?, safety_training_approved_by = ?,
                employment_status = ?, status_date = ?, status_reason = ?,
                is_active = ?
            WHERE id = ?
        """, (
            first_name, last_name, email, phone,
            department_id, designation, default_shift_id,
            salary_type, pay_frequency, base_salary, hourly_rate,
            shift_salary, ot_hourly_rate,
            pf_enabled, esi_enabled, is_admin,
            safety_training_completed, safety_training_date, safety_training_approved_by,
            employment_status, status_date, status_reason,
            is_active, emp_id
        ))
        conn.commit()

        # Synchronize updated name directly to the physical ZKTeco biometric machine
        full_name = f"{first_name} {last_name}".strip()
        cursor.execute("SELECT emp_no FROM employees WHERE id = ?", (emp_id,))
        emp_row = cursor.fetchone()
        if emp_row and emp_row["emp_no"]:
            from models.biometric_sync import sync_employee_name_to_device
            sync_res = sync_employee_name_to_device(emp_row["emp_no"], full_name)
            if sync_res.get("success"):
                flash(f"Employee updated in HRMS and synced to Biometric Machine LCD ('{full_name}')!", "success")
            else:
                flash(f"Employee updated in HRMS. (Biometric device: {sync_res.get('error', '')})", "info")
        else:
            flash("Employee updated successfully", "success")
    except Exception as e:
        flash(f"Update failed: {str(e)}", "danger")
    finally:
        conn.close()

    return redirect(url_for("employees.view_employee", emp_id=emp_id))

@employees_bp.route("/<int:emp_id>/status", methods=["POST"])
@login_required
def update_status(emp_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id, emp_no, first_name, last_name, employment_status FROM employees WHERE id = ?", (emp_id,))
    emp = cursor.fetchone()
    if not emp:
        conn.close()
        flash("Employee not found.", "danger")
        return redirect(url_for("employees.list_employees"))

    new_status = request.form.get("employment_status", "").strip().upper()
    if new_status not in ("ACTIVE", "RELIEVED", "SUSPENDED", "NO_CALL_NO_SHOW"):
        conn.close()
        flash("Invalid status specified.", "danger")
        return redirect(url_for("employees.view_employee", emp_id=emp_id))

    status_date = request.form.get("status_date") or date.today().strftime("%Y-%m-%d")
    status_reason = request.form.get("status_reason", "").strip()
    is_active = 1 if new_status == "ACTIVE" else 0

    cursor.execute("""
        UPDATE employees
        SET employment_status = ?, status_date = ?, status_reason = ?, is_active = ?
        WHERE id = ?
    """, (new_status, status_date, status_reason, is_active, emp_id))
    conn.commit()
    conn.close()

    status_names = {
        "ACTIVE": "Active Employee",
        "RELIEVED": "Relieved (Resigned/Left Service)",
        "SUSPENDED": "Suspended (Disciplinary)",
        "NO_CALL_NO_SHOW": "No Call No Show (Job Abandonment)"
    }
    label = status_names.get(new_status, new_status)

    if new_status == "RELIEVED":
        flash(f"Status for {emp['first_name']} {emp['last_name']} ({emp['emp_no']}) updated to {label}. You can now process their Full & Final Settlement.", "warning")
    elif new_status in ("SUSPENDED", "NO_CALL_NO_SHOW"):
        flash(f"Status for {emp['first_name']} {emp['last_name']} ({emp['emp_no']}) updated to {label}. Attendance and regular payroll are now paused.", "warning")
    else:
        flash(f"Status for {emp['first_name']} {emp['last_name']} ({emp['emp_no']}) updated to {label}. Restored to active service.", "success")

    return redirect(url_for("employees.view_employee", emp_id=emp_id))

@employees_bp.route("/<int:emp_id>/delete", methods=["POST"])
@login_required
def delete_employee(emp_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT e.*, d.name as dept_name
        FROM employees e
        JOIN departments d ON e.department_id = d.id
        WHERE e.id = ?
    """, (emp_id,))
    emp_row = cursor.fetchone()
    if not emp_row:
        conn.close()
        flash("Employee not found.", "danger")
        return redirect(url_for("employees.list_employees"))

    emp = dict(emp_row)
    emp_name = f"{emp['first_name']} {emp['last_name']}"
    emp_no = emp["emp_no"]

    # Calculate counts of historical data to archive in snapshot
    cursor.execute("SELECT COUNT(*) as cnt FROM attendance_records WHERE employee_id = ?", (emp_id,))
    att_cnt = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM payslips WHERE employee_id = ?", (emp_id,))
    ps_cnt = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM advances WHERE employee_id = ?", (emp_id,))
    adv_cnt = cursor.fetchone()["cnt"]

    # Gather full serialized snapshot
    snapshot_data = dict(emp)
    snapshot_data["historical_attendance_count"] = att_cnt
    snapshot_data["historical_payslip_count"] = ps_cnt
    snapshot_data["historical_advances_count"] = adv_cnt
    snapshot_json = json.dumps(snapshot_data, default=str)

    deletion_reason = request.form.get("deletion_reason", "").strip() or "Administrative Deletion"
    deleted_by = session.get("username", "Administrator")

    # Insert immutable archive into employee_deletion_logs
    cursor.execute("""
        INSERT INTO employee_deletion_logs (
            original_emp_id, emp_no, first_name, last_name, email, phone,
            department_name, designation, join_date, salary_type, pay_frequency,
            base_salary, shift_salary, ot_hourly_rate, employment_status,
            status_date, status_reason, pf_enabled, esi_enabled, is_admin,
            deletion_reason, deleted_by, snapshot_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        emp["id"], emp["emp_no"], emp["first_name"], emp["last_name"], emp["email"], emp["phone"],
        emp["dept_name"], emp["designation"], emp["join_date"], emp["salary_type"], emp["pay_frequency"],
        emp["base_salary"], emp["shift_salary"], emp["ot_hourly_rate"], emp["employment_status"],
        emp["status_date"], emp["status_reason"], emp["pf_enabled"], emp["esi_enabled"], int(emp.get("is_admin") or 0),
        deletion_reason, deleted_by, snapshot_json
    ))

    # Now execute CASCADE delete from active employees table
    cursor.execute("DELETE FROM employees WHERE id = ?", (emp_id,))
    conn.commit()
    conn.close()

    # Attempt to purge user from physical biometric device hardware if reachable
    try:
        from models.biometric_sync import delete_user_from_device
        delete_user_from_device(emp_no)
    except Exception as e:
        print(f"[Employee Deletion] Notice: Biometric hardware user purge skipped: {e}")

    flash(f"Employee {emp_name} ({emp_no}) was permanently deleted. All details have been securely archived in the Deletion Audit Log.", "warning")
    return redirect(url_for("employees.separated_employees", tab="deleted"))

@employees_bp.route("/separated")
@login_required
def separated_employees():
    conn = get_db_connection()
    cursor = conn.cursor()

    tab = request.args.get("tab", "relieved").lower()
    if tab not in ("relieved", "suspended", "ncns", "deleted"):
        tab = "relieved"
    search = request.args.get("q", "").strip()

    # 1. Relieved staff
    relieved_query = """
        SELECT e.*, d.name as dept_name, fs.id as settlement_id, fs.net_settlement_amount, fs.settlement_status
        FROM employees e
        JOIN departments d ON e.department_id = d.id
        LEFT JOIN final_settlement_records fs ON e.id = fs.employee_id
        WHERE e.employment_status = 'RELIEVED'
    """
    relieved_params = []
    if search and tab == "relieved":
        relieved_query += " AND (e.first_name LIKE ? OR e.last_name LIKE ? OR e.emp_no LIKE ? OR e.designation LIKE ?)"
        term = f"%{search}%"
        relieved_params.extend([term, term, term, term])
    relieved_query += " ORDER BY e.status_date DESC, e.emp_no ASC"
    cursor.execute(relieved_query, relieved_params)
    relieved_list = [dict(r) for r in cursor.fetchall()]

    # 2. Suspended staff
    suspended_query = """
        SELECT e.*, d.name as dept_name
        FROM employees e
        JOIN departments d ON e.department_id = d.id
        WHERE e.employment_status = 'SUSPENDED'
    """
    suspended_params = []
    if search and tab == "suspended":
        suspended_query += " AND (e.first_name LIKE ? OR e.last_name LIKE ? OR e.emp_no LIKE ? OR e.designation LIKE ?)"
        term = f"%{search}%"
        suspended_params.extend([term, term, term, term])
    suspended_query += " ORDER BY e.status_date DESC, e.emp_no ASC"
    cursor.execute(suspended_query, suspended_params)
    suspended_list = [dict(r) for r in cursor.fetchall()]

    # 3. No Call No Show staff
    ncns_query = """
        SELECT e.*, d.name as dept_name
        FROM employees e
        JOIN departments d ON e.department_id = d.id
        WHERE e.employment_status = 'NO_CALL_NO_SHOW'
    """
    ncns_params = []
    if search and tab == "ncns":
        ncns_query += " AND (e.first_name LIKE ? OR e.last_name LIKE ? OR e.emp_no LIKE ? OR e.designation LIKE ?)"
        term = f"%{search}%"
        ncns_params.extend([term, term, term, term])
    ncns_query += " ORDER BY e.status_date DESC, e.emp_no ASC"
    cursor.execute(ncns_query, ncns_params)
    ncns_list = [dict(r) for r in cursor.fetchall()]

    # 4. Deleted staff logs
    deleted_query = """
        SELECT * FROM employee_deletion_logs
        WHERE 1=1
    """
    deleted_params = []
    if search and tab == "deleted":
        deleted_query += " AND (first_name LIKE ? OR last_name LIKE ? OR emp_no LIKE ? OR designation LIKE ? OR department_name LIKE ?)"
        term = f"%{search}%"
        deleted_params.extend([term, term, term, term, term])
    deleted_query += " ORDER BY deleted_at DESC, id DESC"
    cursor.execute(deleted_query, deleted_params)
    deleted_list = [dict(r) for r in cursor.fetchall()]

    # Global counts for tabs
    cursor.execute("SELECT COUNT(*) as cnt FROM employees WHERE employment_status = 'RELIEVED'")
    cnt_relieved = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM employees WHERE employment_status = 'SUSPENDED'")
    cnt_suspended = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM employees WHERE employment_status = 'NO_CALL_NO_SHOW'")
    cnt_ncns = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM employee_deletion_logs")
    cnt_deleted = cursor.fetchone()["cnt"]

    counts = {
        "relieved": cnt_relieved,
        "suspended": cnt_suspended,
        "ncns": cnt_ncns,
        "deleted": cnt_deleted
    }

    conn.close()

    return render_template(
        "employees/separated.html",
        active_tab=tab,
        search_term=search,
        counts=counts,
        relieved_list=relieved_list,
        suspended_list=suspended_list,
        ncns_list=ncns_list,
        deleted_list=deleted_list
    )

@employees_bp.route("/deletion-log/<int:log_id>")
@login_required
def get_deletion_log(log_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM employee_deletion_logs WHERE id = ?", (log_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "Log record not found"}), 404

    data = dict(row)
    if data.get("snapshot_json"):
        try:
            data["snapshot"] = json.loads(data["snapshot_json"])
        except Exception:
            data["snapshot"] = {}
    return jsonify(data)

