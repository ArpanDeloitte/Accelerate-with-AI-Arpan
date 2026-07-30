@echo off
REM ============================================================
REM  QueryGenie - Live-DB bridge launcher (GitHub edition)
REM  Starts the local bridge (app.py) on http://localhost:8788
REM  so the app can browse YOUR Oracle tables.
REM
REM  First run only: downloads Oracle Instant Client (~60 MB) from
REM  Oracle's official site into instantclient\ so auto-login /
REM  SSO Autonomous DB wallets work (thick mode) - no wallet password
REM  needed, exactly like SQL Developer. If the download is blocked,
REM  the bridge still runs in thin mode (host/port connections and
REM  password-protected wallets work; SSO wallets need thick mode -
REM  copy the "instantclient" folder from QueryGenie.zip here).
REM ============================================================
title QueryGenie - DB Bridge (keep this window open)
cd /d "%~dp0"

echo ============================================================
echo   QueryGenie - Database Bridge
echo ============================================================
echo.

echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo       ERROR: Python not found in PATH. Install Python 3.9+ and re-run.
    pause
    exit /b 1
)

echo [2/4] Checking dependencies...
python -c "import flask,flask_cors,oracledb,dotenv" 2>nul
if errorlevel 1 (
    echo       Installing dependencies ^(first run only^)...
    python -m pip install Flask flask-cors oracledb python-dotenv
)

echo [3/4] Checking Oracle Instant Client ^(for SSO wallet support^)...
if not exist "instantclient\" (
    echo       Downloading Oracle Instant Client Basic Light ~60 MB
    echo       from download.oracle.com ^(first run only^)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "try { Invoke-WebRequest -Uri 'https://download.oracle.com/otn_software/nt/instantclient/instantclient-basiclite-windows.zip' -OutFile '_ic.zip' -UseBasicParsing; Expand-Archive -LiteralPath '_ic.zip' -DestinationPath 'instantclient' -Force; Remove-Item '_ic.zip' -Force; exit 0 } catch { exit 1 }"
    if exist "instantclient\" (
        echo       Instant Client ready - SSO wallets supported ^(thick mode^).
    ) else (
        echo       WARNING: download failed ^(offline or blocked^).
        echo       Bridge will run in THIN mode: host/port connections and
        echo       password-protected wallets work; for SSO wallets copy the
        echo       "instantclient" folder from QueryGenie.zip here.
    )
) else (
    echo       Instant Client present.
)

echo [4/4] Releasing port 8788 if a stale bridge is holding it...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8788" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)

echo.
echo ------------------------------------------------------------
echo   Bridge starting on http://localhost:8788
echo   KEEP THIS WINDOW OPEN while you use the app.
echo   Then in the app: Source Tables -^> Live DB.
echo ------------------------------------------------------------
echo.
python app.py

echo.
echo Bridge stopped.
pause
