@echo off
rem ============================================================
rem  mtzquant 回测实时展示 - 停止脚本
rem  按窗口标题 + 端口 8501 双重定位并终止 Web 服务
rem ============================================================

echo 正在停止 mtzquant Web 展示服务...

rem 1) 按窗口标题停止（启动脚本打开的 mtzquant-serve 窗口; /T 连带杀掉 python 子进程, 防残留）
taskkill /F /T /FI "WINDOWTITLE eq mtzquant-serve" >nul 2>&1

rem 2) 按端口 8501 停止残留监听进程（兜底，最可靠）
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8501" ^| findstr "LISTENING"') do (
    taskkill /F /T /PID %%p >nul 2>&1
)

echo 已停止 mtzquant Web 展示服务。
pause
