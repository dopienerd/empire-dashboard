@echo off
title EMPIRE v44 — IMPROVED

:: ================================================================
:: Load keys from .env file (same folder as this script)
:: ================================================================
if not exist "%~dp0.env" (
    echo ERROR: .env file not found!
    echo Copy .env.example to .env and fill in your real keys.
    pause
    exit /b 1
)

for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%~dp0.env") do (
    if not "%%B"=="" set "%%A=%%B"
)

echo.
echo ================================================
echo   EMPIRE v44 — Starting...
echo ================================================
echo.

python "%~dp0main.py"

echo.
echo Bot stopped. You can close this window now.
pause
