@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   锚点校对台  http://127.0.0.1:8765
echo   关闭本窗口即停止服务
echo.
start "" http://127.0.0.1:8765
py -3.12 server.py
pause
