@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_studio_windows.ps1" -OpenBrowser %*
if errorlevel 1 (
    pause
    exit /b 1
)
