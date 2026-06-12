@echo off
cd /d "%~dp0"
title Drone RID Receiver

:: Find Python
for %%p in (python python3 py) do (
    where %%p >nul 2>&1 && set "P=%%p" && goto :found
)
echo ============================================================
echo  ERROR: Python not found
echo  Download: https://www.python.org/downloads/
echo  IMPORTANT: Check "Add Python to PATH" during install!
echo ============================================================
pause
exit /b 1

:found
echo Python: %P%
echo.

:: Install deps
%P% -c "import flask" 2>nul || (
    echo Installing flask + pyyaml...
    %P% -m pip install flask pyyaml
)

:: Launch web server + open browser
echo ============================================================
echo   Starting Drone RID Receiver...
echo   Open http://localhost:5000 in your browser
echo   Press Ctrl+C to stop
echo ============================================================
start http://localhost:5000
%P% src\main_web.py
pause
