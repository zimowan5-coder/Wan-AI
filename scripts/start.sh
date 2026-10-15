#!/bin/bash
# 开发机（Linux/macOS）启动脚本
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "控制台地址： http://127.0.0.1:8931"
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8931
