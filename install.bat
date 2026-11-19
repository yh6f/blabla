@echo off
title guns.lol username checker - install
cd /d "%~dp0"

set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    py -3 --version >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo Python 3.8+ was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

echo Installing requirements with %PY% ...
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Installation failed. Check the messages above.
    pause
    exit /b 1
)

echo.
echo Installation complete! Start the checker with:  %PY% gunslol.py
pause
