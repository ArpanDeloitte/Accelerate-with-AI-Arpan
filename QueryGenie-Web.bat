@echo off
REM ============================================================
REM  QueryGenie - APP launcher
REM  Starts the web app on http://localhost:8000 and opens your browser.
REM  (Needed for AI + voice. For plain SQL building you can also just
REM   double-click index.html. For Live DB, also run start-bridge.bat.)
REM  Keep this window open; close it to stop the app.
REM ============================================================
title QueryGenie - App (keep this window open)
cd /d "%~dp0"

echo [1/2] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo       ERROR: Python not found in PATH. Install Python 3.9+ and re-run.
    pause
    exit /b 1
)

echo [2/2] Checking dependencies...
python -c "import fastapi,uvicorn" 2>nul
if errorlevel 1 (
    echo       Installing dependencies ^(first run only^)...
    python -m pip install -r requirements.txt
)

echo.
echo ------------------------------------------------------------
echo   QueryGenie starting on http://localhost:8000
echo   KEEP THIS WINDOW OPEN while you use the app.
echo ------------------------------------------------------------
echo.
python web_app.py

echo.
echo App stopped.
pause
