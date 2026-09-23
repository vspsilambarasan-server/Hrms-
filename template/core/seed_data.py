import sqlite3
import random
from datetime import datetime, timedelta
from database import init_db, get_db_connection
from models.shift_engine import evaluate_attendance
from models.ot_engine import sync_overtime_from_attendance
from models.payroll_engine import run_monthly_payroll

def seed_database():
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()

    # Clear existing data
    tables = [
        "advance_repayments", "advances", "payslips", "payroll_runs", "overtime_records",
        "attendance_records", "attendance_punches", "roster_schedules",
        "employees", "shifts", "departments", "company_settings"
    ]
    for table in tables:
        cursor.execute(f"DELETE FROM {table};")
    conn.commit()

    print("Seeding Company Settings...")
    cursor.execute("""
        INSERT INTO company_settings (
            id, company_name, currency_symbol, default_ot_multiplier,
            weekend_ot_multiplier, holiday_ot_multiplier, standard_month_days, standard_day_hours
        ) VALUES (
            1, 'Vasantham Printers', '₹', 1.5, 2.0, 2.5, 26, 8.0
        );
    """)

    print("Seeding Departments...")
    departments = [
        ("PROD", "Production & Assembly", "Manufacturing shop-floor assembly lines and CNC units"),
        ("LOG", "Logistics & Warehousing", "Inventory handling, freight dispatch, and 24x7 dock operations"),
        ("OPS", "Plant Operations & Maintenance", "Heavy equipment upkeep, boiler ops, and facility management"),
        ("ENG", "Industrial Engineering & QA", "Quality compliance, safety assurance, and automation tooling"),
        ("ADMIN", "HR & Plant Administration", "Personnel records, shift planning, payroll & canteen supervision")
    ]
    cursor.executemany("""
        INSERT INTO departments (code, name, description)
        VALUES (?, ?, ?);
    """, departments)
    conn.commit()

    print("Seeding Shifts...")
    # Shift templates
    shifts = [
        ("GEN", "General Day Shift", "09:00", "18:00", 0, 15, 15, 60, 4.5, 8.0, 0.0, "#2563eb", "Standard office hours with 1h lunch"),
        ("MORN", "Morning Shift (Line A)", "06:00", "14:00", 0, 15, 10, 30, 4.0, 7.5, 100.0, "#059669", "Early shop floor production shift"),
        ("EVE", "Evening Shift (Line B)", "14:00", "22:00", 0, 15, 10, 30, 4.0, 7.5, 180.0, "#d97706", "Second production rotation"),
        ("NGT", "Graveyard Night Shift", "22:00", "06:00", 1, 15, 10, 30, 4.0, 7.5, 350.0, "#7c3aed", "Overnight 22:00 - 06:00 next day with prime night allowance"),
        ("WKND", "Weekend Maintenance Shift", "08:00", "16:00", 0, 15, 15, 45, 4.0, 7.25, 250.0, "#dc2626", "Critical plant maintenance shift")
    ]
    cursor.executemany("""
        INSERT INTO shifts (
            code, name, start_time, end_time, is_overnight,
            grace_late_mins, grace_early_mins, break_mins,
            min_hours_half_day, min_hours_full_day, allowance_rate,
            color, description
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, shifts)
    conn.commit()

    # Get shift IDs map
    cursor.execute("SELECT id, code FROM shifts")
    shift_map = {row["code"]: row["id"] for row in cursor.fetchall()}

    # Get department IDs map
    cursor.execute("SELECT id, code FROM departments")
    dept_map = {row["code"]: row["id"] for row in cursor.fetchall()}

    print("Seeding Employees...")
    employees = [
        ("EMP1001", "Rajesh", "Kumar", "rajesh.kumar@apexind.com", "+91 98765 43210", dept_map["PROD"], "Senior Offset Press Lead", "2023-01-15", shift_map["MORN"], "MONTHLY", "WEEKLY", 6500.0, 173.08, 1200.0, 600.0, 12.0, 0.75, 5.0, 6),
        ("EMP1002", "Amit", "Sharma", "amit.sharma@apexind.com", "+91 98765 43211", dept_map["PROD"], "Digital Press Specialist", "2022-04-10", shift_map["EVE"], "MONTHLY", "WEEKLY", 7000.0, 201.92, 1400.0, 700.0, 12.0, 0.75, 5.0, 6),
        ("EMP1003", "Vikram", "Singh", "vikram.singh@apexind.com", "+91 98765 43212", dept_map["PROD"], "Night Binding Specialist", "2023-08-01", shift_map["NGT"], "MONTHLY", "WEEKLY", 6200.0, 182.69, 1300.0, 600.0, 12.0, 0.75, 5.0, 6),
        ("EMP1004", "Sunil", "Verma", "sunil.verma@apexind.com", "+91 98765 43213", dept_map["LOG"], "Packaging & Dispatch Lead", "2021-11-20", shift_map["GEN"], "MONTHLY", "MONTHLY", 45000.0, 216.35, 7500.0, 4000.0, 12.0, 0.75, 5.0, 6),
        ("EMP1005", "Pooja", "Nair", "pooja.nair@apexind.com", "+91 98765 43214", dept_map["LOG"], "Night Logistics Coordinator", "2024-02-01", shift_map["NGT"], "MONTHLY", "MONTHLY", 34000.0, 163.46, 5500.0, 2500.0, 12.0, 0.75, 5.0, 6),
        ("EMP1006", "Ramesh", "Patel", "ramesh.patel@apexind.com", "+91 98765 43215", dept_map["OPS"], "Press Maintenance Engineer", "2020-07-15", shift_map["MORN"], "MONTHLY", "MONTHLY", 48000.0, 230.77, 8000.0, 4000.0, 12.0, 0.75, 5.0, 6),
        ("EMP1007", "Dinesh", "Yadav", "dinesh.yadav@apexind.com", "+91 98765 43216", dept_map["OPS"], "Plate Making & CTP Operator", "2023-03-12", shift_map["NGT"], "MONTHLY", "WEEKLY", 5800.0, 168.27, 1000.0, 500.0, 12.0, 0.75, 5.0, 6),
        ("EMP1008", "Kavita", "Deshmukh", "kavita.d@apexind.com", "+91 98765 43217", dept_map["ENG"], "Quality Assurance Inspector", "2022-09-01", shift_map["GEN"], "MONTHLY", "MONTHLY", 52000.0, 250.00, 9000.0, 5000.0, 12.0, 0.0, 10.0, 6),
        ("EMP1009", "Anand", "Rao", "anand.rao@apexind.com", "+91 98765 43218", dept_map["ENG"], "Pre-press & Color Specialist", "2021-03-18", shift_map["GEN"], "MONTHLY", "MONTHLY", 65000.0, 312.50, 11000.0, 6000.0, 12.0, 0.0, 10.0, 6),
        ("EMP1010", "Deepak", "Mishra", "deepak.mishra@apexind.com", "+91 98765 43219", dept_map["ADMIN"], "HR & Accounts Manager", "2023-05-10", shift_map["GEN"], "MONTHLY", "MONTHLY", 40000.0, 192.31, 7000.0, 3000.0, 12.0, 0.75, 5.0, 6),
        ("EMP1011", "Manoj", "Gupta", "manoj.gupta@apexind.com", "+91 98765 43220", dept_map["PROD"], "Die-Cutter Operator", "2024-01-10", shift_map["MORN"], "HOURLY", "WEEKLY", 0.0, 220.00, 0.0, 0.0, 0.0, 0.0, 5.0, 6),
        ("EMP1012", "Suresh", "Reddy", "suresh.reddy@apexind.com", "+91 98765 43221", dept_map["LOG"], "Forklift & Paper Handler", "2023-10-05", shift_map["EVE"], "MONTHLY", "WEEKLY", 5500.0, 153.85, 900.0, 400.0, 12.0, 0.75, 5.0, 6)
    ]

    cursor.executemany("""
        INSERT INTO employees (
            emp_no, first_name, last_name, email, phone, department_id,
            designation, join_date, default_shift_id, salary_type, pay_frequency,
            base_salary, hourly_rate, hra, special_allowance,
            pf_deduction_pct, esi_deduction_pct, tax_deduction_pct, weekly_off_day
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, employees)
    conn.commit()

    # Fetch employees back with IDs
    cursor.execute("SELECT id, emp_no, default_shift_id, weekly_off_day FROM employees")
    emp_records = cursor.fetchall()

    # Seed sample active salary advances
    print("Seeding Sample Salary Advances & Weekly Cuts...")
    today_dt = datetime.now()
    emp_dict = {r["emp_no"]: r["id"] for r in emp_records}
    if "EMP1001" in emp_dict:
        cursor.execute("""
            INSERT INTO advances (employee_id, advance_date, total_amount, weekly_deduction, amount_repaid, remaining_amount, status, notes)
            VALUES (?, ?, 6000.0, 1000.0, 1000.0, 5000.0, 'ACTIVE', 'Festival advance for Deepavali')
        """, (emp_dict["EMP1001"], (today_dt - timedelta(days=14)).strftime("%Y-%m-%d")))
        adv_id = cursor.lastrowid
        cursor.execute("""
            INSERT INTO advance_repayments (advance_id, repayment_date, amount, payment_type, notes)
            VALUES (?, ?, 1000.0, 'PAYROLL_CUT', 'First installment recovered')
        """, (adv_id, (today_dt - timedelta(days=7)).strftime("%Y-%m-%d")))
    if "EMP1003" in emp_dict:
        cursor.execute("""
            INSERT INTO advances (employee_id, advance_date, total_amount, weekly_deduction, amount_repaid, remaining_amount, status, notes)
            VALUES (?, ?, 4000.0, 800.0, 0.0, 4000.0, 'ACTIVE', 'Medical emergency advance')
        """, (emp_dict["EMP1003"], (today_dt - timedelta(days=10)).strftime("%Y-%m-%d")))
    conn.commit()

    # Fetch all shifts dictionary
    cursor.execute("SELECT * FROM shifts")
    all_shifts = {s["id"]: dict(s) for s in cursor.fetchall()}

    print("Generating 30 Days of Shifts, Rosters, Attendance Punches, and Overtime...")
    # Generate past 30 days up to yesterday
    today = datetime.now()
    start_date = today - timedelta(days=28)

    for i in range(29):
        current_dt = start_date + timedelta(days=i)
        date_str = current_dt.strftime("%Y-%m-%d")
        weekday = current_dt.weekday() # 0=Monday, 6=Sunday

        for emp in emp_records:
            emp_id = emp["id"]
            default_shift_id = emp["default_shift_id"]
            weekly_off = emp["weekly_off_day"]

            is_off = 1 if weekday == weekly_off else 0
            assigned_shift_id = default_shift_id if not is_off else None

            # Occasionally rotate shifts for plant operators (emp 1003, 1007)
            if not is_off and emp["emp_no"] in ("EMP1003", "EMP1007") and i % 7 in (3, 4):
                assigned_shift_id = shift_map["EVE"]

            # Save roster
            cursor.execute("""
                INSERT OR REPLACE INTO roster_schedules (employee_id, date, shift_id, is_off_day, notes)
                VALUES (?, ?, ?, ?, ?);
            """, (emp_id, date_str, assigned_shift_id, is_off, "Scheduled roster" if not is_off else "Weekly Off"))

            if is_off:
                # 10% chance of weekend overtime maintenance call
                if random.random() < 0.15 and emp["emp_no"] in ("EMP1001", "EMP1006", "EMP1007", "EMP1012"):
                    # Weekend OT shift!
                    assigned_shift_id = shift_map["WKND"]
                    shift_info = all_shifts[assigned_shift_id]
                    p_in_str = f"{date_str} 07:55:00"
                    p_out_str = f"{date_str} 16:30:00"

                    eval_res = evaluate_attendance(date_str, shift_info, p_in_str, p_out_str, is_off_day=True)

                    cursor.execute("""
                        INSERT OR REPLACE INTO attendance_records (
                            employee_id, date, shift_id, punch_in, punch_out,
                            work_hours, late_mins, early_leave_mins, status,
                            shift_allowance_amount, raw_ot_hours, notes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """, (
                        emp_id, date_str, assigned_shift_id, p_in_str, p_out_str,
                        eval_res["work_hours"], eval_res["late_mins"], eval_res["early_leave_mins"],
                        "PRESENT", eval_res["shift_allowance"], eval_res["total_ot_hours"], "Weekend Maintenance Overtime"
                    ))
                    att_id = cursor.lastrowid
                    sync_overtime_from_attendance(conn, att_id)
                else:
                    cursor.execute("""
                        INSERT OR REPLACE INTO attendance_records (
                            employee_id, date, shift_id, punch_in, punch_out,
                            work_hours, late_mins, early_leave_mins, status,
                            shift_allowance_amount, raw_ot_hours, notes
                        ) VALUES (?, ?, NULL, NULL, NULL, 0, 0, 0, 'WEEKLY_OFF', 0.0, 0.0, 'Weekly Off');
                    """, (emp_id, date_str))
                continue

            # Regular working day
            shift_info = all_shifts[assigned_shift_id]
            is_night = shift_info["is_overnight"]

            # Randomize attendance scenario
            rand_val = random.random()
            if rand_val < 0.05:
                # Absent
                cursor.execute("""
                    INSERT OR REPLACE INTO attendance_records (
                        employee_id, date, shift_id, punch_in, punch_out,
                        work_hours, late_mins, early_leave_mins, status,
                        shift_allowance_amount, raw_ot_hours, notes
                    ) VALUES (?, ?, ?, NULL, NULL, 0, 0, 0, 'ABSENT', 0.0, 0.0, 'Unexcused Absence');
                """, (emp_id, date_str, assigned_shift_id))
            elif rand_val < 0.10:
                # Half Day
                if is_night:
                    p_in_str = f"{date_str} 22:00:00"
                    next_day_str = (current_dt + timedelta(days=1)).strftime("%Y-%m-%d")
                    p_out_str = f"{next_day_str} 02:30:00"
                else:
                    p_in_str = f"{date_str} {shift_info['start_time']}:00"
                    p_out_str = f"{date_str} 13:30:00"

                eval_res = evaluate_attendance(date_str, shift_info, p_in_str, p_out_str)
                cursor.execute("""
                    INSERT OR REPLACE INTO attendance_records (
                        employee_id, date, shift_id, punch_in, punch_out,
                        work_hours, late_mins, early_leave_mins, status,
                        shift_allowance_amount, raw_ot_hours, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    emp_id, date_str, assigned_shift_id, p_in_str, p_out_str,
                    eval_res["work_hours"], eval_res["late_mins"], eval_res["early_leave_mins"],
                    eval_res["status"], eval_res["shift_allowance"], eval_res["total_ot_hours"], "Approved Half Day"
                ))
            else:
                # Present (with occasional Late or Overtime)
                has_ot = random.random() < 0.35 # 35% chance of overtime
                is_late = random.random() < 0.15 # 15% chance of slight late

                # Calculate punch in
                s_h, s_m = [int(p) for p in shift_info["start_time"].split(":")]
                if is_late:
                    in_mins = s_m + random.randint(18, 40)
                    punch_in_dt = current_dt.replace(hour=s_h, minute=0, second=0) + timedelta(minutes=in_mins)
                else:
                    pre_ot_mins = random.randint(30, 60) if (has_ot and random.random() < 0.3) else random.randint(0, 10)
                    punch_in_dt = current_dt.replace(hour=s_h, minute=s_m, second=0) - timedelta(minutes=pre_ot_mins)

                # Calculate punch out
                e_h, e_m = [int(p) for p in shift_info["end_time"].split(":")]
                out_base_dt = current_dt + timedelta(days=1) if is_night else current_dt
                
                post_ot_mins = random.randint(45, 150) if has_ot else random.randint(0, 10)
                punch_out_dt = out_base_dt.replace(hour=e_h, minute=e_m, second=0) + timedelta(minutes=post_ot_mins)

                p_in_str = punch_in_dt.strftime("%Y-%m-%d %H:%M:%S")
                p_out_str = punch_out_dt.strftime("%Y-%m-%d %H:%M:%S")

                eval_res = evaluate_attendance(date_str, shift_info, p_in_str, p_out_str)

                cursor.execute("""
                    INSERT OR REPLACE INTO attendance_records (
                        employee_id, date, shift_id, punch_in, punch_out,
                        work_hours, late_mins, early_leave_mins, status,
                        shift_allowance_amount, raw_ot_hours, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    emp_id, date_str, assigned_shift_id, p_in_str, p_out_str,
                    eval_res["work_hours"], eval_res["late_mins"], eval_res["early_leave_mins"],
                    eval_res["status"], eval_res["shift_allowance"], eval_res["total_ot_hours"],
                    "Late arrival" if eval_res["status"] == "LATE" else "Regular attendance"
                ))
                att_id = cursor.lastrowid

                if eval_res["total_ot_hours"] > 0:
                    sync_overtime_from_attendance(conn, att_id)

    conn.commit()

    print("Updating Overtime Workflow Statuses (Approved, Rejected, Pending)...")
    cursor.execute("SELECT id, total_ot_hours FROM overtime_records")
    ot_list = cursor.fetchall()
    
    # Randomly approve 70% of historical OT, reject 10%, leave 20% pending
    for ot in ot_list:
        rand_action = random.random()
        if rand_action < 0.70:
            cursor.execute("""
                UPDATE overtime_records
                SET status = 'APPROVED', approved_hours = total_ot_hours,
                    approved_by = 'Vikram Malhotra (Plant HR Head)', approved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (ot["id"],))
        elif rand_action < 0.85:
            cursor.execute("""
                UPDATE overtime_records
                SET status = 'REJECTED', approved_hours = 0.0,
                    approved_by = 'Vikram Malhotra (Plant HR Head)', approved_at = CURRENT_TIMESTAMP,
                    reason = 'Exceeded pre-authorized line quota'
                WHERE id = ?
            """, (ot["id"],))
        # remaining ~15% stays 'PENDING' for the user to review and approve!

    conn.commit()

    print("Running Payroll Calculation for current month...")
    run_res = run_monthly_payroll(conn, today.month, today.year)
    print(f"Payroll Run completed: {run_res}")

    conn.close()
    print("Seeding completed successfully!")

if __name__ == "__main__":
    seed_database()
