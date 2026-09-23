import sqlite3
import os
from datetime import datetime

DB_PATH = os.environ.get("HRMS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hrms_payroll.db"))

def get_db_connection():
    """Get SQLite database connection with row factory enabled."""
    db_path = os.environ.get("HRMS_DB_PATH", DB_PATH)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn

def checkpoint_db():
    """Explicitly checkpoint and flush SQLite WAL log into main DB file."""
    try:
        conn = get_db_connection()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.close()
    except Exception:
        pass


def init_db():
    """Create all required tables for Shift & OT Payroll HRMS."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.executescript("""
    -- 1. Company Settings
    CREATE TABLE IF NOT EXISTS company_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT NOT NULL DEFAULT 'Vasantham Printers',

        currency_symbol TEXT NOT NULL DEFAULT '₹',
        default_ot_multiplier REAL NOT NULL DEFAULT 1.5,
        weekend_ot_multiplier REAL NOT NULL DEFAULT 2.0,
        holiday_ot_multiplier REAL NOT NULL DEFAULT 2.0,
        standard_month_days INTEGER NOT NULL DEFAULT 26,
        standard_day_hours REAL NOT NULL DEFAULT 8.0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 2. Departments
    CREATE TABLE IF NOT EXISTS departments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 3. Shifts
    CREATE TABLE IF NOT EXISTS shifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        start_time TEXT NOT NULL,         -- '09:00'
        end_time TEXT NOT NULL,           -- '18:00'
        is_overnight INTEGER NOT NULL DEFAULT 0, -- 1 if shift crosses midnight (e.g., 22:00 - 06:00)
        grace_late_mins INTEGER NOT NULL DEFAULT 15,
        grace_early_mins INTEGER NOT NULL DEFAULT 15,
        break_mins INTEGER NOT NULL DEFAULT 60,
        min_hours_half_day REAL NOT NULL DEFAULT 4.5,
        min_hours_full_day REAL NOT NULL DEFAULT 8.0,
        allowance_rate REAL NOT NULL DEFAULT 0.0, -- Extra bonus per shift (e.g., Night shift bonus)
        color TEXT NOT NULL DEFAULT '#3b82f6',
        description TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 4. Employees
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_no TEXT UNIQUE NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT,
        department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE RESTRICT,
        designation TEXT NOT NULL,
        join_date TEXT NOT NULL,
        default_shift_id INTEGER REFERENCES shifts(id) ON DELETE SET NULL,
        salary_type TEXT NOT NULL DEFAULT 'MONTHLY', -- 'MONTHLY' or 'HOURLY'
        pay_frequency TEXT NOT NULL DEFAULT 'MONTHLY', -- 'MONTHLY' or 'WEEKLY'
        base_salary REAL NOT NULL DEFAULT 30000.0,
        hourly_rate REAL NOT NULL DEFAULT 144.23,
        hra REAL NOT NULL DEFAULT 5000.0,
        special_allowance REAL NOT NULL DEFAULT 2500.0,
        pf_deduction_pct REAL NOT NULL DEFAULT 12.0,
        esi_deduction_pct REAL NOT NULL DEFAULT 0.75,
        tax_deduction_pct REAL NOT NULL DEFAULT 0.0,
        weekly_off_day INTEGER NOT NULL DEFAULT 6, -- 0=Monday, 6=Sunday
        employment_status TEXT NOT NULL DEFAULT 'ACTIVE', -- 'ACTIVE', 'RELIEVED', 'SUSPENDED', 'NO_CALL_NO_SHOW'
        status_date TEXT,
        status_reason TEXT,
        is_admin INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 5. Shift Roster Schedules (Employee assigned shift per date)
    CREATE TABLE IF NOT EXISTS roster_schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        date TEXT NOT NULL,               -- 'YYYY-MM-DD'
        shift_id INTEGER REFERENCES shifts(id) ON DELETE SET NULL,
        is_off_day INTEGER NOT NULL DEFAULT 0,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(employee_id, date)
    );

    -- 6. Raw Attendance Punches (Biometric / Web Clock)
    CREATE TABLE IF NOT EXISTS attendance_punches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        punch_time TEXT NOT NULL,         -- 'YYYY-MM-DD HH:MM:SS'
        punch_type TEXT NOT NULL,         -- 'IN' or 'OUT'
        device_id TEXT DEFAULT 'WEB_PORTAL',
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 6b. Punch Edit & Modification Audit Logs (Immutable audit trail - cannot be deleted)
    CREATE TABLE IF NOT EXISTS punch_edit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        punch_id INTEGER,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        edit_type TEXT NOT NULL,          -- 'MANUAL_IN', 'MANUAL_OUT', 'PUNCH_EDIT', 'RECORD_OVERRIDE'
        target_field TEXT NOT NULL,       -- 'punch_time', 'punch_in', 'punch_out'
        old_value TEXT,                   -- previous timestamp or value
        new_value TEXT NOT NULL,          -- new adjusted timestamp
        reason TEXT NOT NULL,             -- mandatory justification
        edited_by TEXT NOT NULL DEFAULT 'Supervisor / HR Manager',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 6c. Employee Permanent Deletion Audit Logs (Immutable archive of deleted workers)
    CREATE TABLE IF NOT EXISTS employee_deletion_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_emp_id INTEGER,
        emp_no TEXT NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        department_name TEXT,
        designation TEXT,
        join_date TEXT,
        salary_type TEXT,
        pay_frequency TEXT,
        base_salary REAL DEFAULT 0.0,
        shift_salary REAL DEFAULT 0.0,
        ot_hourly_rate REAL DEFAULT 0.0,
        employment_status TEXT,
        status_date TEXT,
        status_reason TEXT,
        pf_enabled INTEGER DEFAULT 0,
        esi_enabled INTEGER DEFAULT 0,
        is_admin INTEGER DEFAULT 0,
        deletion_reason TEXT,
        deleted_by TEXT NOT NULL DEFAULT 'Administrator',
        deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        snapshot_json TEXT
    );

    -- 7. Evaluated Attendance Records (Daily consolidated per employee)
    CREATE TABLE IF NOT EXISTS attendance_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        date TEXT NOT NULL,               -- 'YYYY-MM-DD' (Shift Date)
        shift_id INTEGER REFERENCES shifts(id) ON DELETE SET NULL,
        punch_in TEXT,                    -- 'YYYY-MM-DD HH:MM:SS'
        punch_out TEXT,                   -- 'YYYY-MM-DD HH:MM:SS'
        work_hours REAL NOT NULL DEFAULT 0.0,
        late_mins INTEGER NOT NULL DEFAULT 0,
        early_leave_mins INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'ABSENT', -- 'PRESENT', 'LATE', 'HALF_DAY', 'ABSENT', 'WEEKLY_OFF', 'HOLIDAY'
        shift_allowance_amount REAL NOT NULL DEFAULT 0.0,
        raw_ot_hours REAL NOT NULL DEFAULT 0.0,
        has_missed_mid_punch INTEGER NOT NULL DEFAULT 0,
        mid_punch_status TEXT NOT NULL DEFAULT 'NORMAL', -- 'NORMAL', 'FLAGGED', 'APPROVED', 'REJECTED'
        mid_punch_notes TEXT,
        mid_punch_approved_by TEXT,
        mid_punch_approved_at TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(employee_id, date)
    );

    -- 8. Overtime Records & Workflow
    CREATE TABLE IF NOT EXISTS overtime_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        attendance_id INTEGER REFERENCES attendance_records(id) ON DELETE SET NULL,
        date TEXT NOT NULL,               -- 'YYYY-MM-DD'
        ot_type TEXT NOT NULL DEFAULT 'NORMAL', -- 'NORMAL', 'WEEKEND', 'HOLIDAY'
        pre_shift_ot_hours REAL NOT NULL DEFAULT 0.0,
        post_shift_ot_hours REAL NOT NULL DEFAULT 0.0,
        total_ot_hours REAL NOT NULL DEFAULT 0.0,
        multiplier REAL NOT NULL DEFAULT 1.5,
        approved_hours REAL NOT NULL DEFAULT 0.0,
        calculated_ot_pay REAL NOT NULL DEFAULT 0.0,
        status TEXT NOT NULL DEFAULT 'PENDING', -- 'PENDING', 'APPROVED', 'REJECTED'
        reason TEXT,
        approved_by TEXT,
        approved_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 9. Payroll Runs (Monthly / Weekly Batches)
    CREATE TABLE IF NOT EXISTS payroll_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_type TEXT NOT NULL DEFAULT 'MONTHLY', -- 'MONTHLY' or 'WEEKLY'
        month INTEGER NOT NULL,           -- 1 to 12
        year INTEGER NOT NULL,            -- e.g., 2026
        period_name TEXT NOT NULL,        -- 'March 2026' or 'Week 37 (01 Sep - 07 Sep 2026)'
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        total_employees INTEGER NOT NULL DEFAULT 0,
        total_gross REAL NOT NULL DEFAULT 0.0,
        total_net REAL NOT NULL DEFAULT 0.0,
        total_ot_pay REAL NOT NULL DEFAULT 0.0,
        total_shift_allowance REAL NOT NULL DEFAULT 0.0,
        total_bonus REAL NOT NULL DEFAULT 0.0,
        total_advance_deductions REAL NOT NULL DEFAULT 0.0,
        status TEXT NOT NULL DEFAULT 'DRAFT', -- 'DRAFT', 'FINALIZED'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 10. Individual Payslips
    CREATE TABLE IF NOT EXISTS payslips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payroll_run_id INTEGER NOT NULL REFERENCES payroll_runs(id) ON DELETE CASCADE,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        pay_frequency TEXT NOT NULL DEFAULT 'MONTHLY', -- 'MONTHLY' or 'WEEKLY'
        base_salary REAL NOT NULL DEFAULT 0.0,
        daily_rate REAL NOT NULL DEFAULT 0.0,
        hourly_rate REAL NOT NULL DEFAULT 0.0,
        days_in_month INTEGER NOT NULL DEFAULT 30,
        days_worked REAL NOT NULL DEFAULT 0.0,
        half_days INTEGER NOT NULL DEFAULT 0,
        weekly_offs INTEGER NOT NULL DEFAULT 0,
        absent_days INTEGER NOT NULL DEFAULT 0,
        total_shift_allowance REAL NOT NULL DEFAULT 0.0,
        night_shifts_count INTEGER NOT NULL DEFAULT 0,
        approved_ot_hours REAL NOT NULL DEFAULT 0.0,
        ot_pay REAL NOT NULL DEFAULT 0.0,
        hra REAL NOT NULL DEFAULT 0.0,
        special_allowance REAL NOT NULL DEFAULT 0.0,
        bonus REAL NOT NULL DEFAULT 0.0,
        gross_earnings REAL NOT NULL DEFAULT 0.0,
        pf_deduction REAL NOT NULL DEFAULT 0.0,
        esi_deduction REAL NOT NULL DEFAULT 0.0,
        tax_deduction REAL NOT NULL DEFAULT 0.0,
        lop_deduction REAL NOT NULL DEFAULT 0.0,
        advance_deduction REAL NOT NULL DEFAULT 0.0,
        total_deductions REAL NOT NULL DEFAULT 0.0,
        net_pay REAL NOT NULL DEFAULT 0.0,
        status TEXT NOT NULL DEFAULT 'GENERATED',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(payroll_run_id, employee_id)
    );

    -- 11. Daily Duty & Break Intervals (Working Timing & Overtime Segments)
    CREATE TABLE IF NOT EXISTS duty_intervals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        segment_number INTEGER NOT NULL UNIQUE,
        segment_name TEXT NOT NULL,
        time_in TEXT NOT NULL,
        time_out TEXT NOT NULL,
        is_overtime INTEGER NOT NULL DEFAULT 0,
        is_night INTEGER NOT NULL DEFAULT 0,
        break_after_mins INTEGER NOT NULL DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 12. Employee Salary Advances & Loans
    CREATE TABLE IF NOT EXISTS advances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        advance_date TEXT NOT NULL,
        total_amount REAL NOT NULL,
        weekly_deduction REAL NOT NULL DEFAULT 0.0,
        amount_repaid REAL NOT NULL DEFAULT 0.0,
        remaining_amount REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE', -- 'ACTIVE', 'COMPLETED'
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 13. Advance Repayments (Deductions from Payroll or Cash Settlements)
    CREATE TABLE IF NOT EXISTS advance_repayments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        advance_id INTEGER NOT NULL REFERENCES advances(id) ON DELETE CASCADE,
        payroll_run_id INTEGER REFERENCES payroll_runs(id) ON DELETE SET NULL,
        payslip_id INTEGER REFERENCES payslips(id) ON DELETE SET NULL,
        repayment_date TEXT NOT NULL,
        amount REAL NOT NULL,
        payment_type TEXT NOT NULL DEFAULT 'PAYROLL_CUT', -- 'PAYROLL_CUT', 'CASH_DIRECT'
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 14. Contract Piece-Rate Workers
    CREATE TABLE IF NOT EXISTS piece_rate_workers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        phone TEXT,
        id_proof_number TEXT,
        department_category TEXT NOT NULL,
        payment_mode TEXT NOT NULL DEFAULT 'CASH',
        upi_or_bank_details TEXT,
        daily_target_qty INTEGER DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 15. Piece-Rate Items & Rates Master
    CREATE TABLE IF NOT EXISTS piece_rate_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_code TEXT UNIQUE NOT NULL,
        item_name TEXT NOT NULL,
        category TEXT NOT NULL,
        unit_measure TEXT NOT NULL DEFAULT 'Pcs',
        default_rate REAL NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- 16. Piece-Rate Work Entries (Item-wise Qty Tracker)
    CREATE TABLE IF NOT EXISTS piece_rate_work_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_id INTEGER NOT NULL REFERENCES piece_rate_workers(id) ON DELETE CASCADE,
        work_date TEXT NOT NULL,
        item_id INTEGER REFERENCES piece_rate_items(id) ON DELETE SET NULL,
        item_name TEXT NOT NULL,
        job_card_no TEXT,
        rate_per_unit REAL NOT NULL,
        unit_measure TEXT NOT NULL DEFAULT 'Pcs',
        quantity_completed INTEGER NOT NULL,
        rejected_quantity INTEGER NOT NULL DEFAULT 0,
        payable_quantity INTEGER NOT NULL,
        total_amount REAL NOT NULL,
        payment_status TEXT NOT NULL DEFAULT 'UNPAID',
        payout_reference TEXT,
        payout_date TEXT,
        remarks TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Seed duty_intervals if empty
    cursor.execute("SELECT COUNT(*) as cnt FROM duty_intervals")
    if cursor.fetchone()["cnt"] == 0:
        cursor.executemany("""
            INSERT INTO duty_intervals (segment_number, segment_name, time_in, time_out, is_overtime, is_night, break_after_mins)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            (1, "1. Morning Shift (Start to Tea)", "09:00 AM", "11:00 AM", 0, 0, 15),
            (2, "2. Morning Shift (Tea to Lunch)", "11:15 AM", "01:00 PM", 0, 0, 60),
            (3, "3. Afternoon Shift (Lunch to Break)", "02:00 PM", "04:00 PM", 0, 0, 15),
            (4, "4. Afternoon Shift (Break to Shift End)", "04:15 PM", "06:00 PM", 0, 0, 0),
            (5, "5. Overtime Session 1", "06:00 PM", "07:00 PM", 1, 0, 15),
            (6, "6. Overtime Session 2", "07:15 PM", "09:15 PM", 1, 0, 0),
            (7, "7. Night Duty Session", "09:30 PM", "05:30 AM", 0, 1, 0)
        ])

    conn.commit()

    # Migrate any existing database schema seamlessly
    migrate_db(conn)

    conn.close()

def migrate_db(conn):
    """Safely apply migrations (ALTER TABLE) to an existing database if columns or tables are missing."""
    cursor = conn.cursor()

    # Employees migrations
    cursor.execute("PRAGMA table_info(employees)")
    emp_cols = [r["name"] for r in cursor.fetchall()]
    if "pay_frequency" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN pay_frequency TEXT NOT NULL DEFAULT 'MONTHLY'")

    # Payroll Runs migrations
    cursor.execute("PRAGMA table_info(payroll_runs)")
    pr_cols = [r["name"] for r in cursor.fetchall()]
    if "run_type" not in pr_cols:
        cursor.execute("ALTER TABLE payroll_runs ADD COLUMN run_type TEXT NOT NULL DEFAULT 'MONTHLY'")
    if "total_bonus" not in pr_cols:
        cursor.execute("ALTER TABLE payroll_runs ADD COLUMN total_bonus REAL NOT NULL DEFAULT 0.0")
    if "total_advance_deductions" not in pr_cols:
        cursor.execute("ALTER TABLE payroll_runs ADD COLUMN total_advance_deductions REAL NOT NULL DEFAULT 0.0")

    # Payslips migrations
    cursor.execute("PRAGMA table_info(payslips)")
    ps_cols = [r["name"] for r in cursor.fetchall()]
    if "pay_frequency" not in ps_cols:
        cursor.execute("ALTER TABLE payslips ADD COLUMN pay_frequency TEXT NOT NULL DEFAULT 'MONTHLY'")
    if "bonus" not in ps_cols:
        cursor.execute("ALTER TABLE payslips ADD COLUMN bonus REAL NOT NULL DEFAULT 0.0")
    if "advance_deduction" not in ps_cols:
        cursor.execute("ALTER TABLE payslips ADD COLUMN advance_deduction REAL NOT NULL DEFAULT 0.0")

    # Piece-Rate Tables creation (if database existed before this migration)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS piece_rate_workers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        phone TEXT,
        id_proof_number TEXT,
        department_category TEXT NOT NULL,
        payment_mode TEXT NOT NULL DEFAULT 'CASH',
        upi_or_bank_details TEXT,
        daily_target_qty INTEGER DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS piece_rate_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_code TEXT UNIQUE NOT NULL,
        item_name TEXT NOT NULL,
        category TEXT NOT NULL,
        unit_measure TEXT NOT NULL DEFAULT 'Pcs',
        default_rate REAL NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS piece_rate_work_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_id INTEGER NOT NULL REFERENCES piece_rate_workers(id) ON DELETE CASCADE,
        work_date TEXT NOT NULL,
        item_id INTEGER REFERENCES piece_rate_items(id) ON DELETE SET NULL,
        item_name TEXT NOT NULL,
        job_card_no TEXT,
        rate_per_unit REAL NOT NULL,
        unit_measure TEXT NOT NULL DEFAULT 'Pcs',
        quantity_completed INTEGER NOT NULL,
        rejected_quantity INTEGER NOT NULL DEFAULT 0,
        payable_quantity INTEGER NOT NULL,
        total_amount REAL NOT NULL,
        payment_status TEXT NOT NULL DEFAULT 'UNPAID',
        payout_reference TEXT,
        payout_date TEXT,
        remarks TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Seed default printing piece-rate items if empty
    cursor.execute("SELECT COUNT(*) as cnt FROM piece_rate_items")
    if cursor.fetchone()["cnt"] == 0:
        cursor.executemany("""
            INSERT INTO piece_rate_items (item_code, item_name, category, unit_measure, default_rate)
            VALUES (?, ?, ?, ?, ?)
        """, [
            ("ITEM-001", "Hardcover Book Binding (Rexine/Paper)", "Binding", "Books", 12.00),
            ("ITEM-002", "Spiral / Wiro Binding (Desk Calendar)", "Binding", "Pcs", 5.00),
            ("ITEM-003", "Sweet Box 4-Corner Pasting (Rigid)", "Pasting", "Boxes", 0.80),
            ("ITEM-004", "A4 Brochure 3-Fold & Scoring", "Folding", "1000 Pcs", 150.00),
            ("ITEM-005", "Thermal Gloss Lamination Sheet", "Finishing", "Sheets", 2.00),
            ("ITEM-006", "Paper Bag String Eyeleting & Fitting", "Packaging", "Pcs", 1.50),
            ("ITEM-007", "Numbering & Perforation (Bill Books)", "Finishing", "100 Books", 25.00),
        ])

    # Biometric Auto-Sync Settings migrations
    cursor.execute("PRAGMA table_info(company_settings)")
    cs_cols = [r["name"] for r in cursor.fetchall()]
    if "biometric_device_ip" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN biometric_device_ip TEXT NOT NULL DEFAULT '192.168.101.201'")
    if "biometric_device_port" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN biometric_device_port INTEGER NOT NULL DEFAULT 4370")
    if "biometric_auto_sync_enabled" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN biometric_auto_sync_enabled INTEGER NOT NULL DEFAULT 1")
    if "biometric_auto_sync_interval" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN biometric_auto_sync_interval INTEGER NOT NULL DEFAULT 60")
    if "biometric_last_sync_time" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN biometric_last_sync_time TEXT")
    if "biometric_last_sync_status" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN biometric_last_sync_status TEXT NOT NULL DEFAULT 'IDLE'")
    if "biometric_last_sync_message" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN biometric_last_sync_message TEXT")
    if "biometric_auto_sync_count" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN biometric_auto_sync_count INTEGER NOT NULL DEFAULT 0")

    # Company Profile, Address, Contact & SMTP Alert Settings migrations
    if "address" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN address TEXT NOT NULL DEFAULT '124, Press Colony, Sivakasi Road, Virudhunagar Dist, Tamil Nadu - 626123'")
    if "phone" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN phone TEXT NOT NULL DEFAULT '+91 94431 23456'")
    if "email" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN email TEXT NOT NULL DEFAULT 'admin@vasanthamprinters.com'")
    if "gstin" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN gstin TEXT NOT NULL DEFAULT '33AAAAA0000A1Z5'")
    if "factory_license_no" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN factory_license_no TEXT NOT NULL DEFAULT 'FAC/TN/VNR/2021/489'")
    if "supervisor_name" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN supervisor_name TEXT NOT NULL DEFAULT 'Authorized Press Supervisor'")
    if "smtp_host" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN smtp_host TEXT NOT NULL DEFAULT 'smtp.gmail.com'")
    if "smtp_port" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN smtp_port INTEGER NOT NULL DEFAULT 587")
    if "smtp_user" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN smtp_user TEXT NOT NULL DEFAULT ''")
    if "smtp_password" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN smtp_password TEXT NOT NULL DEFAULT ''")
    if "smtp_use_tls" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN smtp_use_tls INTEGER NOT NULL DEFAULT 1")
    if "alert_recipient_email" not in cs_cols:
        cursor.execute("ALTER TABLE company_settings ADD COLUMN alert_recipient_email TEXT NOT NULL DEFAULT 'supervisor@vasanthamprinters.com'")

    # Users Table for Login Authentication
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'ADMIN',
        full_name TEXT NOT NULL DEFAULT 'HR Administrator',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Seed default admin user if empty
    cursor.execute("SELECT COUNT(*) as cnt FROM users")
    if cursor.fetchone()["cnt"] == 0:
        from werkzeug.security import generate_password_hash
        default_pwd = generate_password_hash("admin123")
        cursor.execute("""
            INSERT INTO users (username, password_hash, role, full_name)
            VALUES ('admin', ?, 'ADMIN', 'Vasantham Press Admin')
        """, (default_pwd,))

    # Employees simplified salary & statutory compliance migrations
    cursor.execute("PRAGMA table_info(employees)")
    emp_cols = [r["name"] for r in cursor.fetchall()]
    if "pf_enabled" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN pf_enabled INTEGER NOT NULL DEFAULT 0")
    if "esi_enabled" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN esi_enabled INTEGER NOT NULL DEFAULT 0")
    if "tax_deduction_pct" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN tax_deduction_pct REAL NOT NULL DEFAULT 0.0")
    if "shift_salary" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN shift_salary REAL NOT NULL DEFAULT 0.0")
    if "ot_hourly_rate" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN ot_hourly_rate REAL NOT NULL DEFAULT 0.0")
    if "safety_training_completed" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN safety_training_completed INTEGER NOT NULL DEFAULT 1")
    if "safety_training_date" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN safety_training_date TEXT")
    if "safety_training_approved_by" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN safety_training_approved_by TEXT NOT NULL DEFAULT 'Press Safety Officer'")
    if "employment_status" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN employment_status TEXT NOT NULL DEFAULT 'ACTIVE'")
    if "status_date" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN status_date TEXT")
    if "status_reason" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN status_reason TEXT")

    # Sync employment_status based on is_active for existing records
    cursor.execute("""
        UPDATE employees
        SET employment_status = CASE 
            WHEN is_active = 1 THEN 'ACTIVE'
            ELSE 'RELIEVED'
        END
        WHERE employment_status IS NULL OR employment_status = ''
    """)

    # Sync existing employees to have valid shift_salary and ot_hourly_rate if empty
    cursor.execute("""
        UPDATE employees 
        SET shift_salary = CASE WHEN base_salary > 0 THEN ROUND(base_salary / 26.0, 2) ELSE 500.0 END
        WHERE shift_salary = 0.0
    """)
    cursor.execute("""
        UPDATE employees 
        SET ot_hourly_rate = CASE WHEN hourly_rate > 0 THEN hourly_rate ELSE 75.0 END
        WHERE ot_hourly_rate = 0.0
    """)

    # Statutory Diwali Bonus Records Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS diwali_bonus_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year INTEGER NOT NULL,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        annual_earned_wages REAL NOT NULL DEFAULT 0.0,
        bonus_percentage REAL NOT NULL DEFAULT 8.33,
        bonus_amount REAL NOT NULL DEFAULT 0.0,
        payout_status TEXT NOT NULL DEFAULT 'PENDING',
        payout_date TEXT,
        payout_reference TEXT,
        remarks TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(year, employee_id)
    );
    """)

    # Statutory Final Settlement Records Table (Full & Final)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS final_settlement_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        settlement_date TEXT NOT NULL,
        service_years REAL NOT NULL DEFAULT 0.0,
        last_drawn_wage REAL NOT NULL DEFAULT 0.0,
        gratuity_amount REAL NOT NULL DEFAULT 0.0,
        leave_encashment_days REAL NOT NULL DEFAULT 0.0,
        leave_encashment_amount REAL NOT NULL DEFAULT 0.0,
        unpaid_wages REAL NOT NULL DEFAULT 0.0,
        advance_recovery REAL NOT NULL DEFAULT 0.0,
        net_settlement_amount REAL NOT NULL DEFAULT 0.0,
        settlement_status TEXT NOT NULL DEFAULT 'SETTLED',
        approved_by TEXT,
        payout_reference TEXT,
        remarks TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Statutory Pongal Festival Holiday Leave Salary Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS pongal_holiday_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year INTEGER NOT NULL,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        holiday_date TEXT NOT NULL,
        festival_name TEXT NOT NULL,
        attended_preceding_day INTEGER NOT NULL DEFAULT 1,
        leave_salary_rate REAL NOT NULL DEFAULT 0.0,
        paid_status TEXT NOT NULL DEFAULT 'PAID',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(year, employee_id, holiday_date)
    );
    """)

    # Employee Permanent Deletion Audit Logs Table
    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS employee_deletion_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_emp_id INTEGER,
        emp_no TEXT NOT NULL,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        department_name TEXT,
        designation TEXT,
        join_date TEXT,
        salary_type TEXT,
        pay_frequency TEXT,
        base_salary REAL DEFAULT 0.0,
        shift_salary REAL DEFAULT 0.0,
        ot_hourly_rate REAL DEFAULT 0.0,
        employment_status TEXT,
        status_date TEXT,
        status_reason TEXT,
        pf_enabled INTEGER DEFAULT 0,
        esi_enabled INTEGER DEFAULT 0,
        is_admin INTEGER DEFAULT 0,
        deletion_reason TEXT,
        deleted_by TEXT NOT NULL DEFAULT 'Administrator',
        deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        snapshot_json TEXT
    );

    -- 24. Missing Attendance Punch Alerts & Manager Approvals
    CREATE TABLE IF NOT EXISTS missing_punch_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
        date TEXT NOT NULL,               -- 'YYYY-MM-DD'
        gap_type TEXT NOT NULL,           -- 'MISSING_CLOCK_IN', 'MISSING_CLOCK_OUT', 'MISSING_LUNCH_OUT', 'MISSING_LUNCH_RETURN'
        expected_time TEXT NOT NULL,      -- 'HH:MM:SS'
        suggested_punch_type TEXT NOT NULL, -- 'IN' or 'OUT'
        suggested_timestamp TEXT NOT NULL, -- 'YYYY-MM-DD HH:MM:SS'
        description TEXT,
        status TEXT NOT NULL DEFAULT 'PENDING', -- 'PENDING', 'APPROVED', 'DISMISSED'
        approved_by TEXT,
        approved_at TIMESTAMP,
        inserted_punch_id INTEGER REFERENCES attendance_punches(id) ON DELETE SET NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(employee_id, date, gap_type, suggested_punch_type)
    );
    """)

    # Check is_admin in employee_deletion_logs
    cursor.execute("PRAGMA table_info(employee_deletion_logs)")
    del_cols = [r["name"] for r in cursor.fetchall()]
    if "is_admin" not in del_cols:
        cursor.execute("ALTER TABLE employee_deletion_logs ADD COLUMN is_admin INTEGER DEFAULT 0")

    # Check is_admin in employees
    if "is_admin" not in emp_cols:
        cursor.execute("ALTER TABLE employees ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    # Auto-mark staff whose designation implies administrative duties as is_admin = 1
    cursor.execute("""
        UPDATE employees
        SET is_admin = 1
        WHERE LOWER(designation) LIKE '%admin%'
    """)

    # Check attendance_records mid punch columns
    cursor.execute("PRAGMA table_info(attendance_records)")
    att_cols = [r["name"] for r in cursor.fetchall()]
    if "has_missed_mid_punch" not in att_cols:
        cursor.execute("ALTER TABLE attendance_records ADD COLUMN has_missed_mid_punch INTEGER NOT NULL DEFAULT 0")
    if "mid_punch_status" not in att_cols:
        cursor.execute("ALTER TABLE attendance_records ADD COLUMN mid_punch_status TEXT NOT NULL DEFAULT 'NORMAL'")
    if "mid_punch_notes" not in att_cols:
        cursor.execute("ALTER TABLE attendance_records ADD COLUMN mid_punch_notes TEXT")
    if "mid_punch_approved_by" not in att_cols:
        cursor.execute("ALTER TABLE attendance_records ADD COLUMN mid_punch_approved_by TEXT")
    if "mid_punch_approved_at" not in att_cols:
        cursor.execute("ALTER TABLE attendance_records ADD COLUMN mid_punch_approved_at TEXT")
    if "gross_ot_hours" not in att_cols:
        cursor.execute("ALTER TABLE attendance_records ADD COLUMN gross_ot_hours REAL NOT NULL DEFAULT 0.0")
    if "late_deduction_mins" not in att_cols:
        cursor.execute("ALTER TABLE attendance_records ADD COLUMN late_deduction_mins INTEGER NOT NULL DEFAULT 0")

    conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized successfully at {DB_PATH}")
