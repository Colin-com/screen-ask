@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   SCR-01  SCREEN ASK
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
