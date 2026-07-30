@echo off
:: ============================================================
::  YouTube 24/7 Live Streamer — Windows Run Script
::  Usage: run.bat
:: ============================================================

title YT Live Streamer

echo.
echo  =============================================
echo   YouTube 24/7 Live Streamer - Starting...
echo  =============================================
echo.

:: Check Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo  [ERROR] Python not found. Please install Python 3.9+
    pause
    exit /b 1
)

:: Create venv if not exists
if not exist ".venv" (
    echo  [INFO] Creating virtual environment...
    python -m venv .venv
)

:: Activate venv
echo  [INFO] Activating virtual environment...
call .venv\Scripts\activate.bat

:: Install dependencies
echo  [INFO] Installing dependencies...
pip install -r requirements.txt --quiet

:: Load .env file if it exists
if exist ".env" (
    echo  [INFO] Loading .env file...
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not "%%A:~0,1%"=="#" set "%%A=%%B"
    )
)

:: Start the app
echo.
echo  [INFO] Starting Flask app on http://localhost:7860
echo  [INFO] Press Ctrl+C to stop
echo.
python app.py

pause
