@echo off
setlocal enabledelayedexpansion
title Drone RID Receiver v2.0

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

:: Check Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.10+ from https://www.python.org/
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo [INFO] Python %%v

:: Check deps
python -c "import yaml" >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Installing pyyaml...
    python -m pip install -q pyyaml
)

:MENU
cls
echo ==============================================================
echo    Drone RID Receiver + Power Line Collision Avoidance  v2.0
echo ==============================================================
echo.
echo    [1] GUI - Mock Data (test mode)
echo    [2] GUI - BLE Scan (Bluetooth)
echo    [3] GUI - WiFi Scan (scapy + Npcap)
echo    [4] CLI - Terminal mode
echo    [5] Run Unit Tests
echo    [6] Install All Dependencies
echo    [0] Exit
echo.
echo ==============================================================
set /p CHOICE="Select [0-6]: "

if "%CHOICE%"=="1" goto GUI_MOCK
if "%CHOICE%"=="2" goto GUI_BLE
if "%CHOICE%"=="3" goto GUI_WIFI
if "%CHOICE%"=="4" goto CLI
if "%CHOICE%"=="5" goto TEST
if "%CHOICE%"=="6" goto INSTALL
if "%CHOICE%"=="0" goto END
goto MENU

:GUI_MOCK
echo [START] GUI (Mock Mode)
python -B src\main_gui.py
goto MENU

:GUI_BLE
echo [START] GUI (BLE Mode)
python -c "import bleak" >nul 2>&1 || (
    echo [INSTALL] bleak...
    python -m pip install -q bleak
)
python -B src\main_gui.py
goto MENU

:GUI_WIFI
echo [START] GUI (WiFi Mode)
echo [HINT] WiFi mode requires Npcap: https://npcap.com
python -c "import scapy" >nul 2>&1 || (
    echo [INSTALL] scapy...
    python -m pip install -q scapy
)
python -B src\main_gui.py
goto MENU

:CLI
echo [START] CLI Mode
python -B src\main.py --mode mock
goto MENU

:TEST
echo [TEST] Running unit tests...
python -c "import pytest" >nul 2>&1 || (
    python -m pip install -q pytest
)
python -m pytest tests\test_system.py -v
pause
goto MENU

:INSTALL
echo [INSTALL] Installing all dependencies...
python -m pip install -r requirements.txt
echo Done!
pause
goto MENU

:END
exit /b 0
