@echo off
chcp 65001 >nul
cd /d %~dp0..
echo ============================================
echo  Wan's AI Business OS v0.2
echo ============================================
echo [1/4] 检查 Python ...
python --version >nul 2>&1
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.11 或更高版本：
  echo   https://www.python.org/downloads/ （安装时勾选 Add to PATH）
  pause
  exit /b 1
)
if not exist .venv (
  echo [2/4] 创建虚拟环境 ...
  python -m venv .venv
) else (
  echo [2/4] 虚拟环境已存在，跳过
)
call .venv\Scripts\activate.bat
echo [3/4] 安装依赖 ...
pip install -q -r requirements.txt
if errorlevel 1 (
  echo [错误] 依赖安装失败，请检查网络后重试
  pause
  exit /b 1
)
python scripts\bootstrap.py
echo [4/4] 启动经营控制台 ...
echo.
echo   控制台地址： http://127.0.0.1:8931
echo   按 Ctrl+C 可停止。数据保存在 data\wanai.db
echo.
start "" http://127.0.0.1:8931
python -m uvicorn app.main:app --host 127.0.0.1 --port 8931
pause
