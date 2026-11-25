@echo off
REM Start the Switz Group Talent Acquisition app (production server).
REM Colleagues on the same network can open it at the address shown below.
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python app.py
pause
