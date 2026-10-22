@echo off
title CasterBot Web Test
echo Starting CasterBot web server (local test - no Discord bot)...
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python run_web_test.py
pause
