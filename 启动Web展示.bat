@echo off
rem ============================================================
rem  mtzquant 回测结果 Web 展示 - 启动脚本
rem  启动 Web 服务（查看模式），浏览器打开实时可视页
rem  已入库的回测（含 ETF轮动V28.1 近似版 2025-01-01~2026-08-01）
rem  可在"监控/历史"页选择查看
rem  停止服务请运行: 停止Web展示.bat
rem  说明: 展示用纯 serve（不自动跑回测）。重跑回测请在命令行执行:
rem    .venv\Scripts\python -m mtzquant backtest -c configs\etf_rotation_v28_huber_ptrade.json
rem ============================================================
cd /d E:\programData\pythonProject\mtzQuant

echo ============================================
echo   mtzquant 回测结果 Web 展示 - 启动
echo   http://127.0.0.1:8501
echo ============================================
echo.

start "mtzquant-serve" cmd /k ".venv\Scripts\python.exe -m mtzquant serve"

timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:8501

echo 已启动，浏览器将打开 http://127.0.0.1:8501
echo 停止服务请运行: 停止Web展示.bat
echo 本窗口可关闭，但请保留 mtzquant-serve 服务窗口。
pause
