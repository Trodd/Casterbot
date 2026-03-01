@echo off
title CasterBot
echo Starting CasterBot...
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m casterbot
pause
