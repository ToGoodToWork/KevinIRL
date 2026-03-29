@echo off
title KevinStream Receiver
echo.
echo Starting KevinStream Receiver...
echo.
python "%~dp0receive.py" %*
if errorlevel 1 (
    echo.
    echo If Python is not found, install it from https://python.org
    echo.
    pause
)
