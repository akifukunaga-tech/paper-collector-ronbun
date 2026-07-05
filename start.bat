@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Paper Collector を起動します...
python server.py
pause
