@echo off
title Secure Local RAG - Sync Agent

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python virtual environment not found.
    echo Please complete the setup instructions first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" sync_agent.py

echo.
echo Sync Agent has stopped.
pause
