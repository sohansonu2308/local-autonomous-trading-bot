@echo off
title Whale-Mindset Autonomous Crypto Trading System
cd /d "D:\Zeldris\GEMINI_TRADING_BOT"
if exist "C:\Users\Bochetti YogeshSagar\AppData\Roaming\uv\tools\free-claude-code\Scripts\python.exe" (
    "C:\Users\Bochetti YogeshSagar\AppData\Roaming\uv\tools\free-claude-code\Scripts\python.exe" app.py
) else (
    python app.py
)
pause
