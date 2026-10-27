@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 用 32 位 Python 运行(zlgcan.dll 为 32 位)。可用环境变量 PY32 指定 python.exe
if "%PY32%"=="" set "PY32=D:\Program Files (x86)\ZCANPRO\python38\python.exe"
"%PY32%" run_app.py
if errorlevel 1 pause
