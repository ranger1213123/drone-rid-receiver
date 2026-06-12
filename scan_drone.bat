@echo off
cd /d "%~dp0"
title Drone RID Scanner

:: Find Python
for %%p in (python python3 py) do (
    where %%p >nul 2>&1 && set "P=%%p" && goto :found
)
echo [ERROR] Python not found
pause & exit /b 1

:found
echo.
echo ============================================================
echo   Drone RID Scanner
echo ============================================================
echo   1. BLE scan (Bluetooth drones)
echo   2. WiFi scan (DJI drones - needs Npcap + scapy)
echo   3. WiFi passive scan (netsh, no extra software)
echo ============================================================
set /p M="Choice [1-3]: "

if "%M%"=="1" goto :ble
if "%M%"=="2" goto :wifi
if "%M%"=="3" goto :passive
echo Invalid choice.
pause & exit /b 1

:ble
echo.
echo Starting BLE scan for drones...
echo Press Ctrl+C to stop
echo.
%P% -c "import bleak" 2>nul || (
    echo Installing bleak...
    %P% -m pip install bleak
)
%P% scan_drone.py --mode ble
goto :end

:wifi
echo.
echo Checking scapy...
%P% -c "import scapy" 2>nul || (
    echo Installing scapy...
    %P% -m pip install scapy
)
echo.
echo WARNING: WiFi Beacon scanning requires Npcap!
echo Download: https://npcap.com
echo Check "Support raw 802.11 traffic" during install!
echo.
echo Starting WiFi scan...
echo Press Ctrl+C to stop
echo.
%P% scan_drone.py --mode wifi
goto :end

:passive
echo.
echo Running passive WiFi scan (netsh)...
%P% scan_wifi.py
goto :end

:end
pause
