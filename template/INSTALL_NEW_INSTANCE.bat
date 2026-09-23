@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Shift & Overtime Payroll HRMS - New Instance Deployment Wizard
cd /d "%~dp0"

echo ========================================================
echo   Shift & Overtime Payroll HRMS
echo   Automated Setup & Instance Deployment Wizard
echo ========================================================
echo.

echo Checking Python environment...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in your system PATH.
    echo Please install Python 3.10+ from https://www.python.org/
    pause
    exit /b 1
)

python setup_wizard.py

pause
