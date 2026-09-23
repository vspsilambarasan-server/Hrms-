# ApexHRMS: Shift & Overtime (OT) Payroll Software

A comprehensive, production-ready Web HRMS & Payroll solution built with Python (Flask, SQLite, Tailwind CSS, Chart.js) specifically engineered for complex shift scheduling, cross-midnight punches, overtime computation rules, and shift/OT-integrated payroll generation.

---

## 🌟 Key Features

### 1. Shift & Roster Engine
- **Cross-Midnight & 2-Day Shifts**: Full support for night shifts spanning across midnight (e.g. `22:00` to `06:00` next day).
- **Custom Shift Definitions**: Configure start/end times, break deductions, late arrival grace tolerance (e.g. 15 mins), early leave grace, and half-day/full-day thresholds.
- **Shift Allowances**: Automatic per-shift bonus/incentive (e.g. ₹350 night shift allowance) credited directly to payroll.
- **Weekly Visual Roster Scheduler**: Matrix calendar mapping staff members against the 7 days of the week, with single-click shift reassignment and weekly off/rest day management.

### 2. Shift-Aware Attendance & Biometric Engine
- **Intelligent Punch Pairing**: Automatically matches punch in and punch out logs against scheduled shifts.
- **Automatic Status Tagging**: `PRESENT`, `LATE` (with exact minutes), `HALF_DAY`, `ABSENT`, and `WEEKLY_OFF`.
- **Live Web Punch Simulator**: Simulate employee biometric punches in real-time right from the dashboard or attendance screen.
- **Biometric Log Importer**: Upload CSV files or paste raw biometric device logs (`EMP_NO, YYYY-MM-DD HH:MM:SS, IN/OUT`) with automatic shift detection.

### 3. Overtime (OT) Engine & Authorization Workflow
- **Pre-Shift & Post-Shift OT**: Captures early check-ins and late check-outs with configurable qualification thresholds.
- **Policy Multipliers**:
  - Normal Working Day OT: **1.5x** standard hourly wage.
  - Weekend / Rest Day OT: **2.0x** standard hourly wage.
  - Public Holiday OT: **2.5x** standard hourly wage.
- **Supervisor Review & Approval Workflow**:
  - Filter by `PENDING`, `APPROVED`, `REJECTED`.
  - Supervisor can adjust approved hours before authorization.
  - Single-click **Batch Approve** for fast monthly processing.
  - **Strict Payroll Guarantee**: Only approved OT hours are paid out.

### 4. Shift & OT-Driven Payroll Engine
- **Exact Earnings Formula**:
  $$\text{Gross Earnings} = \text{Earned Base} + \text{Shift Allowances} + \text{Approved OT Pay} + \text{HRA} + \text{Special Allowance}$$
- **Exact Deductions Formula**:
  $$\text{Total Deductions} = \text{PF (12\%)} + \text{ESI (0.75\%)} + \text{Tax/TDS (5\%)} + \text{Loss of Pay (LOP)}$$
- **Net Disbursed**:
  $$\text{Net Payable} = \text{Gross Earnings} - \text{Total Deductions}$$
- **Batch Pay Runs**: Run payroll for any month/year in seconds with live status tracking (`DRAFT` vs `FINALIZED`).
- **Professional Printable Payslip**: Complete company letterhead, shift & attendance summary, OT hours & multipliers breakdown, dual earnings & deductions tables, and clean `@media print` layout ready for paper or PDF export.
- **Payroll Register CSV Export**: One-click export of the entire payroll cycle into CSV.

---

## 🚀 Getting Started

### 1. Launch with One Click (Windows)
Double-click `run.bat` in the project directory:
```
C:\Users\vspsi\.gemini\antigravity\scratch\payroll-shift-hrms\run.bat
```
This automatically installs dependencies, opens your default browser at `http://127.0.0.1:5000`, and starts the server.

### 2. Manual Command Line
```powershell
cd C:\Users\vspsi\.gemini\antigravity\scratch\payroll-shift-hrms
python -m pip install -r requirements.txt
python seed_data.py   # (Optional) Re-seeds sample employees, shifts, punches, and payroll
python app.py
```
Open **http://127.0.0.1:5000** in your browser.

---

## 🧪 Automated Verification & Tests

Run the complete test suite:
```powershell
python -m unittest discover tests
```
- `tests/test_shift_engine.py`: Verifies cross-midnight shift time calculations, late grace detection, half-day status, and night shift allowances.
- `tests/test_payroll_engine.py`: Verifies hourly rate calculations, OT multipliers, gross pay, and statutory deductions.
- `tests/test_routes.py`: Verifies all web routes, roster views, attendance register, and API punch clock endpoints.

---

## 📂 Project Structure

```
payroll-shift-hrms/
├── app.py                      # Flask entry point & blueprint registration
├── database.py                 # SQLite schema & connection helpers
├── seed_data.py                # Realistic seed generator (12 employees, 5 shifts, 30-day logs)
├── run.bat                     # Windows one-click launcher
├── requirements.txt            # Python dependencies (Flask)
│
├── models/
│   ├── shift_engine.py         # Cross-midnight shift evaluator & grace periods
│   ├── ot_engine.py            # OT multipliers, hourly rates, & approval sync
│   └── payroll_engine.py       # Shift & OT-integrated monthly salary calculator
│
├── routes/
│   ├── dashboard.py            # KPI cards, shift distribution, pending OT alerts
│   ├── employees.py            # Staff directory, profiles, & compensation CRUD
│   ├── shifts.py               # Shift templates & visual roster scheduler
│   ├── attendance.py           # Timesheet, punch simulator, biometric importer
│   ├── overtime.py             # Overtime approval inbox & policy settings
│   ├── payroll.py              # Batch pay runs, payslip views, & CSV export
│   └── api.py                  # Live punch API & chart data
│
├── templates/
│   ├── base.html               # Enterprise layout, responsive sidebar & clock
│   ├── dashboard.html          # Operations & payroll dashboard
│   ├── employees/              # Staff directory & employee profile view
│   ├── shifts/                 # Shift definitions & weekly roster matrix
│   ├── attendance/             # Timesheet & punch simulator
│   ├── overtime/               # Overtime approvals & multiplier policy modal
│   └── payroll/                # Pay run register & printable payslip
│
├── static/
│   ├── css/custom.css          # Print stylesheets (@media print) & badge design
│   └── js/main.js              # Live clock, modal manager, async punch client
│
└── tests/
    ├── test_shift_engine.py    # Unit tests for shift calculations
    ├── test_payroll_engine.py  # Unit tests for salary & deduction math
    └── test_routes.py          # Integration tests for Flask routes & endpoints
```
