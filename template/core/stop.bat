@echo off
title Stop Shift & Overtime Payroll Server
cd /d "%~dp0"
echo ========================================================
echo   Stopping Production WSGI Server on port 5000...
echo ========================================================

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5000 ^| findstr LISTENING') do (
    echo Stopping process PID: %%a
    taskkill /PID %%a /F >nul 2>&1
)

echo.
echo Checkpointing database to ensure all edits are safely saved...
python -c "from database import checkpoint_db; checkpoint_db(); print('Database checkpoint completed.')"

echo.
echo ========================================================
echo   Server on port 5000 stopped safely.
echo   All employee alterations and attendance data are saved.
echo ========================================================
pause
