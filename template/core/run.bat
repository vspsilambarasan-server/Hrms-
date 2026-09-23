@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Vasantham Printers - Shift & Overtime Payroll HRMS (Production WSGI)
cd /d "%~dp0"
echo ========================================================
echo   Vasantham Printers: Shift & Overtime Payroll HRMS
echo   Production WSGI Server (Waitress)
echo ========================================================

echo.

echo Checking Python environment...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in your PATH.
    pause
    exit /b
)

echo Installing dependencies if needed...
python -m pip install -r requirements.txt --quiet

echo.
echo Starting Production WSGI Server on http://127.0.0.1:5000 ...
echo (Press Ctrl+C to stop the server)
echo.

start "" "http://127.0.0.1:5000"
python app.py

pause
