#!/usr/bin/env python3
"""
HRMS & Payroll System - Automated Setup & Instance Deployment Wizard
Usage:
    Interactive mode:
        python setup_wizard.py
    Silent / Non-interactive mode:
        python setup_wizard.py --non-interactive --target-dir "F:\\NEW_PLANT_HRMS" --company-name "My Company"
"""

import os
import sys
import shutil
import argparse
from werkzeug.security import generate_password_hash

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(SCRIPT_DIR, "core")

def print_banner():
    print("=" * 65)
    print("   Shift & Overtime Payroll HRMS - New Instance Setup Wizard")
    print("   Automated Deployment & Configuration Engine")
    print("=" * 65)
    print()

def prompt_with_default(prompt_text, default_val):
    resp = input(f"{prompt_text} [{default_val}]: ").strip()
    return resp if resp else default_val

def deploy_instance(config):
    target_dir = os.path.abspath(config["target_dir"])
    company_name = config.get("company_name", "My Factory / Enterprise")
    currency = config.get("currency", "₹")
    address = config.get("address", "Industrial Estate, Sivakasi Road, Tamil Nadu")
    phone = config.get("phone", "+91 94431 00000")
    email = config.get("email", "admin@company.com")
    gstin = config.get("gstin", "33AAAAA0000A1Z5")
    factory_license = config.get("factory_license", "FAC/TN/2026/001")
    biometric_ip = config.get("biometric_ip", "192.168.101.201")
    biometric_port = int(config.get("biometric_port", 4370))
    admin_user = config.get("admin_user", "admin")
    admin_pass = config.get("admin_pass", "admin123")
    server_port = int(config.get("server_port", 5000))
    db_mode = config.get("db_mode", "CLEAN").upper()

    print(f"\n[*] Preparing target directory: {target_dir}")
    os.makedirs(target_dir, exist_ok=True)

    # 1. Copy core files
    print("[*] Copying core application templates and modules...")
    if not os.path.exists(CORE_DIR):
        raise FileNotFoundError(f"Core template directory not found at: {CORE_DIR}")

    for item in os.listdir(CORE_DIR):
        s = os.path.join(CORE_DIR, item)
        d = os.path.join(target_dir, item)
        if os.path.isdir(s):
            if os.path.exists(d):
                shutil.rmtree(d)
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)

    print("[+] Application codebase successfully copied.")

    # 2. Configure Database
    db_file = os.path.join(target_dir, "hrms_payroll.db")
    print(f"[*] Configuring database at: {db_file}")

    # Set environment variable so imported database module uses the new target db
    os.environ["HRMS_DB_PATH"] = db_file
    if target_dir not in sys.path:
        sys.path.insert(0, target_dir)

    import database
    database.DB_PATH = db_file

    if db_mode == "DEMO":
        print("[*] Populating sample workforce, shifts, and attendance records...")
        import seed_data
        seed_data.seed_database()
        conn = database.get_db_connection()
        cursor = conn.cursor()
    else:
        print("[*] Clean production mode: initializing pristine database schema...")
        database.init_db()
        conn = database.get_db_connection()
        cursor = conn.cursor()

    # Configure Company Settings
    cursor.execute("SELECT id FROM company_settings LIMIT 1")
    existing_cs = cursor.fetchone()
    if existing_cs:
        cursor.execute("""
            UPDATE company_settings
            SET company_name = ?, currency_symbol = ?, address = ?, phone = ?,
                email = ?, gstin = ?, factory_license_no = ?,
                biometric_device_ip = ?, biometric_device_port = ?
            WHERE id = ?
        """, (company_name, currency, address, phone, email, gstin, factory_license,
              biometric_ip, biometric_port, existing_cs["id"]))
    else:
        cursor.execute("""
            INSERT INTO company_settings (
                company_name, currency_symbol, address, phone, email,
                gstin, factory_license_no, biometric_device_ip, biometric_device_port
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (company_name, currency, address, phone, email, gstin, factory_license,
              biometric_ip, biometric_port))

    # Configure Administrator Credentials
    pwd_hash = generate_password_hash(admin_pass)
    cursor.execute("DELETE FROM users WHERE username = ?", (admin_user,))
    cursor.execute("""
        INSERT INTO users (username, password_hash, role, full_name)
        VALUES (?, ?, 'ADMIN', ?)
    """, (admin_user, pwd_hash, f"{company_name} Administrator"))

    conn.commit()
    conn.close()
    database.checkpoint_db()
    print("[+] Database configured successfully.")

    # 3. Generate tailored run.bat and stop.bat
    run_bat_content = f"""@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title {company_name} - Shift & Overtime Payroll HRMS
cd /d "%~dp0"
echo ========================================================
echo   {company_name}
echo   Shift & Overtime Payroll HRMS (Port {server_port})
echo ========================================================

echo Checking Python environment...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in PATH.
    pause
    exit /b
)

echo Installing dependencies if needed...
python -m pip install -r requirements.txt --quiet

echo.
echo Starting Production WSGI Server on http://127.0.0.1:{server_port} ...
echo (Press Ctrl+C to stop the server)
echo.

start "" "http://127.0.0.1:{server_port}"
python app.py

pause
"""

    stop_bat_content = f"""@echo off
chcp 65001 >nul
title Stop {company_name} Server
cd /d "%~dp0"
echo ========================================================
echo   Stopping HRMS Server on port {server_port}...
echo ========================================================

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :{server_port} ^| findstr LISTENING') do (
    echo Stopping process PID: %%a
    taskkill /PID %%a /F >nul 2>&1
)

echo.
echo Checkpointing database to ensure all edits are safely saved...
python -c "from database import checkpoint_db; checkpoint_db(); print('Database checkpoint completed.')"

echo.
echo ========================================================
echo   Server on port {server_port} stopped safely.
echo ========================================================
pause
"""

    # If server_port != 5000, adapt app.py / wsgi.py default port in target
    if server_port != 5000:
        target_app_path = os.path.join(target_dir, "app.py")
        with open(target_app_path, "r", encoding="utf-8") as f:
            app_code = f.read()
        app_code = app_code.replace("port=5000", f"port={server_port}").replace(":5000", f":{server_port}")
        with open(target_app_path, "w", encoding="utf-8") as f:
            f.write(app_code)

        target_wsgi_path = os.path.join(target_dir, "wsgi.py")
        if os.path.exists(target_wsgi_path):
            with open(target_wsgi_path, "r", encoding="utf-8") as f:
                wsgi_code = f.read()
            wsgi_code = wsgi_code.replace("port=5000", f"port={server_port}").replace(":5000", f":{server_port}")
            with open(target_wsgi_path, "w", encoding="utf-8") as f:
                f.write(wsgi_code)

    with open(os.path.join(target_dir, "run.bat"), "w", encoding="utf-8") as f:
        f.write(run_bat_content)

    with open(os.path.join(target_dir, "stop.bat"), "w", encoding="utf-8") as f:
        f.write(stop_bat_content)

    print("[+] Generated optimized run.bat and stop.bat launchers.")

    print("\n" + "=" * 65)
    print("   [SUCCESS] New HRMS Instance Deployed Successfully!")
    print("=" * 65)
    print(f" * Target Directory : {target_dir}")
    print(f" * Company Name     : {company_name}")
    print(f" * Web Address      : http://127.0.0.1:{server_port}")
    print(f" * Biometric Device : {biometric_ip}:{biometric_port}")
    print(f" * Admin Username   : {admin_user}")
    print(f" * Admin Password   : {admin_pass}")
    print(f" * Database Mode    : {db_mode}")
    print("=" * 65)
    print(f"\nTo launch your new instance, run:\n   cd /d \"{target_dir}\" && run.bat\n")
    return True

def main():
    parser = argparse.ArgumentParser(description="HRMS & Payroll System Deployment Wizard")
    parser.add_argument("--non-interactive", action="store_true", help="Run without interactive prompts")
    parser.add_argument("--target-dir", help="Destination folder for the new instance")
    parser.add_argument("--company-name", default="My Factory / Enterprise", help="Legal Entity Name")
    parser.add_argument("--currency", default="₹", help="Currency symbol (default: ₹)")
    parser.add_argument("--address", default="Industrial Estate, Sivakasi Road, Tamil Nadu", help="Factory address")
    parser.add_argument("--phone", default="+91 94431 00000", help="Primary contact number")
    parser.add_argument("--email", default="admin@company.com", help="Factory admin email")
    parser.add_argument("--gstin", default="33AAAAA0000A1Z5", help="GSTIN Tax ID")
    parser.add_argument("--license", default="FAC/TN/2026/001", help="Factory License Number")
    parser.add_argument("--biometric-ip", default="192.168.101.201", help="ZKTeco Terminal IP")
    parser.add_argument("--biometric-port", default=4370, type=int, help="ZKTeco Port (default: 4370)")
    parser.add_argument("--admin-user", default="admin", help="Admin username")
    parser.add_argument("--admin-pass", default="admin123", help="Admin password")
    parser.add_argument("--server-port", default=5000, type=int, help="Local web port (default: 5000)")
    parser.add_argument("--db-mode", default="CLEAN", choices=["CLEAN", "DEMO"], help="Database mode (CLEAN or DEMO)")

    args = parser.parse_args()

    if args.non_interactive:
        if not args.target_dir:
            print("[!] Error: --target-dir is required in non-interactive mode.")
            sys.exit(1)
        config = {
            "target_dir": args.target_dir,
            "company_name": args.company_name,
            "currency": args.currency,
            "address": args.address,
            "phone": args.phone,
            "email": args.email,
            "gstin": args.gstin,
            "factory_license": args.license,
            "biometric_ip": args.biometric_ip,
            "biometric_port": args.biometric_port,
            "admin_user": args.admin_user,
            "admin_pass": args.admin_pass,
            "server_port": args.server_port,
            "db_mode": args.db_mode
        }
        deploy_instance(config)
        return

    # Interactive Mode
    print_banner()
    default_dir = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "NEW_HRMS_INSTANCE"))
    target_dir = prompt_with_default("1. Target Deployment Directory", default_dir)
    company_name = prompt_with_default("2. Company / Factory Name", "Vasantham Packaging Division")
    currency = prompt_with_default("3. Currency Symbol", "₹")
    address = prompt_with_default("4. Factory Address", "124, Press Colony, Sivakasi Road, Virudhunagar Dist, Tamil Nadu - 626123")
    phone = prompt_with_default("5. Contact Phone Number", "+91 94431 23456")
    email = prompt_with_default("6. Contact Email", "admin@company.com")
    gstin = prompt_with_default("7. GSTIN Tax ID", "33AAAAA0000A1Z5")
    license_no = prompt_with_default("8. Factory License Number", "FAC/TN/VNR/2026/102")
    biometric_ip = prompt_with_default("9. Biometric Terminal IP", "192.168.101.201")
    biometric_port = int(prompt_with_default("10. Biometric Terminal Port", "4370"))
    server_port = int(prompt_with_default("11. Web Server Port (Localhost)", "5000"))
    admin_user = prompt_with_default("12. Initial Admin Username", "admin")
    admin_pass = prompt_with_default("13. Initial Admin Password", "admin123")
    
    print("\nSelect Database Mode:")
    print("  [1] CLEAN - Fresh production database (zero dummy employees, clean slate)")
    print("  [2] DEMO  - Seeded database (includes 12 sample employees, shifts & attendance)")
    mode_choice = prompt_with_default("Choose option (1 or 2)", "1")
    db_mode = "DEMO" if mode_choice == "2" else "CLEAN"

    config = {
        "target_dir": target_dir,
        "company_name": company_name,
        "currency": currency,
        "address": address,
        "phone": phone,
        "email": email,
        "gstin": gstin,
        "factory_license": license_no,
        "biometric_ip": biometric_ip,
        "biometric_port": biometric_port,
        "admin_user": admin_user,
        "admin_pass": admin_pass,
        "server_port": server_port,
        "db_mode": db_mode
    }

    print("\nDeploying with configuration:")
    for k, v in config.items():
        print(f"  {k:18}: {v}")

    confirm = input("\nProceed with deployment? [Y/n]: ").strip().lower()
    if confirm and confirm not in ("y", "yes"):
        print("[!] Deployment cancelled by user.")
        sys.exit(0)

    deploy_instance(config)

if __name__ == "__main__":
    main()
