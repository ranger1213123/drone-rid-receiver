@echo off
cd /d "%~dp0"
title Drone RID Receiver - Setup

echo ============================================================
echo   Drone RID Receiver - One-Click Setup and Launch
echo ============================================================
echo.

:: Step 1: find Python
set "PY="
for %%c in (python python3 py) do (
    where %%c >nul 2>&1 && set "PY=%%c" && goto :found
)
echo [FAIL] No Python found. Please install Python 3.10+
echo        https://www.python.org/downloads/
echo        CHECK "Add Python to PATH" during install!
pause
exit /b 1

:found
echo [OK] Python: %PY%
%PY% --version
echo.

:: Step 2: create local venv
if not exist ".venv\" (
    echo [SETUP] Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [FAIL] Cannot create venv. Trying without venv...
        goto :novenv
    )
)

:: Step 3: install deps
echo [SETUP] Installing dependencies...
.venv\Scripts\python -m pip install --upgrade pip 2>nul
.venv\Scripts\python -m pip install pyyaml
if errorlevel 1 (
    echo [WARN] pip install had issues, continuing anyway...
)

:: Step 4: launch GUI
echo [START] Launching GUI...
echo ============================================================
.venv\Scripts\python src\main_gui.py
if errorlevel 1 (
    echo.
    echo [FAIL] GUI crashed. Trying without venv...
    goto :novenv
)
goto :end

:novenv
echo [INFO] Running with system Python...
%PY% -m pip install pyyaml 2>nul
%PY% src\main_gui.py
pause
goto :end

:end
