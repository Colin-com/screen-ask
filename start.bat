@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul || (echo Python not found in PATH & pause & exit /b 1)
start "" pythonw "screen_ask.py"
