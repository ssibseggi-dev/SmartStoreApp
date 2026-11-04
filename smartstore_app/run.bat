@echo off
rem 스마트스토어 앱 실행 (더블클릭 또는 터미널에서 run.bat)
cd /d "%~dp0"
"%~dp0..\.venv\Scripts\python.exe" main.py
if errorlevel 1 pause
