@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 用 32 位 Python 打包(可用环境变量 PY32 覆盖)
if "%PY32%"=="" set "PY32=D:\Program Files (x86)\ZCANPRO\python38\python.exe"
"%PY32%" -c "import struct,sys;assert struct.calcsize('P')==4, '需要 32 位 Python'" || (echo 需要32位Python & pause & exit /b 1)
"%PY32%" -m PyInstaller --noconfirm --clean --windowed --name myzcanpro --onedir run_app.py ^
  --hidden-import zdiag --hidden-import zdiag_ui ^
  --exclude-module PySide2 --exclude-module tkinter
if errorlevel 1 (echo 打包失败 & pause & exit /b 1)
rem 复制厂商文件(如有): vendor\ 目录下的 zlgcan.dll、kerneldlls\、密钥 dll
if exist vendor xcopy /e /i /y vendor dist\myzcanpro >nul
echo.
echo 完成: dist\myzcanpro\myzcanpro.exe
echo 请把 zlgcan.dll 和 kerneldlls\ 放到该目录(与exe同级)。
pause
