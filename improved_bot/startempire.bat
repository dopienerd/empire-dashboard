@echo off
title EMPIRE v44 — IMPROVED

:: ── CRYPTO.COM API KEYS ──
set CRYPTOCOM_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set CRYPTOCOM_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

:: ── EVM PRIVATE KEY ──
set EVM_PRIVATE_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

:: ── SOLANA MULTI-WALLET (ADD AS MANY AS YOU WANT) ──
set SOL_PRIVATE_KEY_1=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set SOL_PRIVATE_KEY_2=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set SOL_PRIVATE_KEY_3=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set SOL_PRIVATE_KEY_4=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set SOL_PRIVATE_KEY_5=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set SOL_PRIVATE_KEY_6=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set SOL_PRIVATE_KEY_7=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set SOL_PRIVATE_KEY_8=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
set SOL_PRIVATE_KEY_9=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
:: keep adding — bot will use ALL of them automatically

echo.
echo ================================================
echo   EMPIRE v44 — Starting...
echo ================================================
echo.

python main.py

echo.
echo Bot stopped. You can close this window now.
pause
