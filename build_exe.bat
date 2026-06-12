@echo off
cd /d "%~dp0"
title Building DroneRID_Receiver.exe

echo ============================================================
echo   Build Standalone EXE - Drone RID Receiver
echo ============================================================
echo.

:: Find Python
for %%p in (python python3 py) do (
    where %%p >nul 2>&1 && set "P=%%p" && goto :found
)
echo [FAIL] Python not found
pause & exit /b 1

:found
echo [OK] Python: %P%
echo.

:: Install PyInstaller
echo [1/3] Installing PyInstaller...
%P% -m pip install -q pyinstaller
if errorlevel 1 (
    echo [FAIL] Cannot install PyInstaller
    pause & exit /b 1
)

:: Clean old builds
echo [2/3] Cleaning...
if exist build\ rmdir /s /q build
if exist dist\ rmdir /s /q dist

:: Build EXE
echo [3/3] Building EXE (this may take 1-2 minutes)...
%P% -m PyInstaller --clean --noconfirm ^
    --onefile ^
    --windowed ^
    --name DroneRID_Receiver ^
    --add-data "config\config.yaml;config" ^
    --add-data "config\power_lines.yaml;config" ^
    --hidden-import yaml ^
    --hidden-import src.db ^
    --hidden-import src.rid_parser ^
    --hidden-import src.rid_receiver ^
    --hidden-import src.powerline ^
    --hidden-import src.alert ^
    --hidden-import src.trajectory ^
    --hidden-import src.gui.main_window ^
    --hidden-import src.gui.powerline_dialog ^
    src\main_gui.py

if errorlevel 1 (
    echo.
    echo [FAIL] Build failed
    pause & exit /b 1
)

echo.
echo ============================================================
echo   SUCCESS!
echo   EXE: dist\DroneRID_Receiver.exe
echo ============================================================
echo.
echo Copy DroneRID_Receiver.exe anywhere and run it directly.
echo No Python or dependencies needed.
pause
