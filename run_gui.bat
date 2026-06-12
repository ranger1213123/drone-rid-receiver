@echo off
cd /d "%~dp0"
title Drone RID Receiver

:: Find Python
set "P="
for %%c in (python python3 py) do (
    where %%c >nul 2>&1 && set "P=%%c" && goto :found
)
echo [ERROR] Python not found
echo Install from https://www.python.org/downloads/
pause
exit /b 1

:found
echo Python: %P%
%P% --version
echo.

:: Install pyyaml
%P% -c "import yaml" 2>nul || (
    echo Installing pyyaml...
    %P% -m pip install pyyaml
)

:: Launch
echo Starting GUI...
%P% -B src\main_gui.py
if errorlevel 1 pause
