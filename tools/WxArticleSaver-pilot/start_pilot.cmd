@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -u "%~dp0coexist_launcher.py" --seconds 300 --auto-export-pilot
