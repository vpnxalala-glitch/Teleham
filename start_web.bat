@echo off
cd /d "%~dp0"
echo Installing dependencies...
python -m pip install -r requirements.txt
echo.
echo Starting Teleham Web...
echo Open in browser: http://127.0.0.1:8080
echo.
python server.py
pause
