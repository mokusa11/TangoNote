@echo off
setlocal
cd /d "%~dp0"
set "TEST_MODE=1"
if "%TEST_MODE%"=="1" (
  python app.py --test-mode %*
) else (
  python app.py %*
)
if errorlevel 1 (
  echo.
  echo 笔记程序启动失败。请确认已安装 Python 3.10 或更高版本。
  pause
)
