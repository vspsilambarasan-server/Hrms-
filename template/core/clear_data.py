import sqlite3
import os
from database import DB_PATH, init_db, get_db_connection

def clear_all_data():
    """Wipes all transactional and master records from the database, leaving clean empty tables."""
    conn = get_db_connection()
    cursor = conn.cursor()

    tables = [
        "payslips",
        "payroll_runs",
        "overtime_records",
        "attendance_records",
        "attendance_punches",
        "roster_schedules",
        "employees",
        "shifts",
        "departments"
    ]

    print("Clearing tables...")
    for table in tables:
        cursor.execute(f"DELETE FROM {table};")
        # Reset sqlite autoincrement sequence
        cursor.execute(f"DELETE FROM sqlite_sequence WHERE name='{table}';")
        print(f"  [OK] Cleared {table}")

    # Reset company settings to clean defaults
    cursor.execute("""
        INSERT OR REPLACE INTO company_settings (
            id, company_name, currency_symbol, default_ot_multiplier,
            weekend_ot_multiplier, holiday_ot_multiplier, standard_month_days, standard_day_hours
        ) VALUES (
            1, 'Vasantham Printers', '₹', 1.5, 2.0, 2.5, 26, 8.0
        );

    """)
    print("  [OK] Reset company_settings to default")

    conn.commit()
    conn.close()
    print("\nAll database data cleared successfully! System is in a clean fresh state.")

if __name__ == "__main__":
    clear_all_data()
