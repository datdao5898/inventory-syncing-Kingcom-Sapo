@echo off
cd /d "%~dp0"
python sync.py --apply --loop 15
pause
