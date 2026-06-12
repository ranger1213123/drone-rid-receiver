@echo off
cd /d "%~dp0"
title Environment Check

for %%p in (python python3 py) do (
    where %%p >nul 2>&1 && set "P=%%p" && goto :found
)
echo [ERROR] Python not found
pause & exit /b 1

:found
%P% check_env.py
pause
