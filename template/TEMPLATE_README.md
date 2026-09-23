# Shift & Overtime Payroll HRMS - Instance Deployment Template

This directory contains the master deployment template and automated setup engine for the **Shift & Overtime Payroll HRMS**. Use this template to spin up new, independent instances for new factories, branches, sister concerns, or staging environments in seconds.

---

## 🚀 Quick Start: Deploy a New Instance

### Option 1: 1-Click Interactive Wizard (Windows)
Double-click `INSTALL_NEW_INSTANCE.bat` in this folder:
```text
F:\HRMS_TEMPLATE\INSTALL_NEW_INSTANCE.bat
```
The wizard will guide you through entering:
1. **Target Directory**: Where to install (e.g. `F:\NEW_PLANT_HRMS` or `C:\HRMS_PLANT2`).
2. **Company Profile**: Legal Name, Address, Phone, Email, GSTIN, Factory License.
3. **Hardware Config**: ZKTeco Biometric Terminal IP (`192.168.101.201`) and Port (`4370`).
4. **Local Web Port**: Default port `5000` (or `5001`, `5002` for running multiple plants on the same server).
5. **Admin Account**: Custom username and password.
6. **Database Mode**:
   * `[1] CLEAN`: Completely empty employee database ready for immediate live staff enrollment.
   * `[2] DEMO`: Populated with 12 sample workers, rotational shifts, and test punches.

---

### Option 2: Command Line (Silent / Automated Deployment)

Run `setup_wizard.py` directly with arguments:

```powershell
python setup_wizard.py --non-interactive `
  --target-dir "F:\SIVAKASI_PACKAGING_HRMS" `
  --company-name "Sivakasi Packaging Industries Pvt Ltd" `
  --address "Plot 45, SIDCO Industrial Estate, Sivakasi" `
  --phone "+91 94431 88888" `
  --email "hr@sivakasipackaging.com" `
  --gstin "33BBBBB1111B1Z2" `
  --license "FAC/TN/VNR/2026/890" `
  --biometric-ip "192.168.1.210" `
  --biometric-port 4370 `
  --server-port 5001 `
  --admin-user "admin" `
  --admin-pass "securepass123" `
  --db-mode "CLEAN"
```

---

## 📂 Template Directory Layout

```
HRMS_TEMPLATE/
├── INSTALL_NEW_INSTANCE.bat     # Windows 1-click wizard launcher
├── setup_wizard.py              # Automated deployment engine
├── TEMPLATE_README.md           # This documentation
│
└── core/                        # Master clean codebase (no stale DB or logs)
    ├── app.py                   # Flask entrypoint
    ├── wsgi.py                  # Production Waitress WSGI server
    ├── database.py              # SQLite schema, pragmas & migrations
    ├── requirements.txt         # Dependencies
    ├── run.bat                  # Base launcher
    ├── stop.bat                 # Base shutdown script
    ├── models/                  # Shift, OT, Biometric, Payroll engines
    ├── routes/                  # All blueprint endpoints
    ├── templates/               # Complete responsive HTML templates & print layouts
    ├── static/                  # CSS styles & JavaScript assets
    └── tests/                   # Complete automated test suite
```

---

## ⚙️ Post-Deployment Operations

Every newly deployed instance is completely self-contained:
* **To start the instance:** Double-click `run.bat` inside the new folder.
* **To stop the instance:** Double-click `stop.bat` inside the new folder.
* **To run health check:** Run `python -m unittest discover tests` inside the new folder.
