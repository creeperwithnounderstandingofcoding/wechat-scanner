#!/bin/bash
# 双击此文件即可启动微信扫描工作台（本地）
# 启动后会自动打开浏览器

cd "$(dirname "$0")"
echo "========================================"
echo "  微信扫描工作台 — 启动中..."
echo "========================================"
echo ""

# 检查 Python
if ! command -v python3 &>/dev/null; then
    echo "[错误] 未找到 python3，请先安装 Python 3.9+"
    echo "       下载: https://www.python.org/downloads/"
    echo ""
    echo "按回车键关闭..."
    read
    exit 1
fi

# 检查依赖
python3 -c "import flask" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "[!] 正在安装依赖..."
    pip3 install flask python-dotenv lark-oapi anthropic pycryptodome requests 2>/dev/null
fi

echo "[+] 启动本地服务: http://127.0.0.1:5678"
echo "[+] 关闭此窗口即可停止服务"
echo ""

python3 web_ui.py --port 5678
