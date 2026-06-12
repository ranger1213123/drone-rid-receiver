@echo off
cd /d "%~dp0"
title DJI Drone RID Scanner

for %%p in (python python3 py) do (
    where %%p >nul 2>&1 && set "P=%%p" && goto :found
)
echo [ERROR] Python not found
pause & exit /b 1

:found
echo ============================================================
echo   DJI Drone WiFi Scanner
echo ============================================================
%P% scan_wifi.py
echo.
pause
