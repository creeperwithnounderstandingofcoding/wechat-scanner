#!/usr/bin/env python3
"""微信聊天记录扫描器 v3 — Web UI

启动本地 Flask 服务，浏览器中操作，无 tkinter 渲染问题。

用法：
  python web_ui.py          # 启动后自动打开浏览器
  python web_ui.py --port 8080
"""

import logging
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import timedelta
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)

from core import ScannerCore

# 服务器模式：部署到远程服务器时开启。
# - 跳过微信 DB 初始化（服务器上没有 WeChat 本地数据）
# - 前端隐藏"微信扫描" tab，默认打开"飞书扫描"
SERVER_MODE = os.getenv("SERVER_MODE", "").lower() in ("1", "true", "yes", "on")

# ============================================================
# 飞书 OAuth 登录墙配置（Phase C）
# ============================================================
# REQUIRE_LOGIN=1 后，所有请求都必须通过飞书 OAuth 才能访问。
# 本地 dev 留空即可跳过。
REQUIRE_LOGIN = os.getenv("REQUIRE_LOGIN", "").lower() in ("1", "true", "yes", "on")

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

# 重定向 URL，必须在飞书开发者后台的"安全设置 → 重定向 URL"里注册
OAUTH_REDIRECT_URI = os.getenv(
    "OAUTH_REDIRECT_URI",
    "https://scanner.helioratech.com/oauth/callback",
)

# 飞书 API base（国内飞书 = open.feishu.cn，国际 Lark = open.larksuite.com）
FEISHU_OPEN_BASE = os.getenv("FEISHU_OPEN_BASE", "https://open.feishu.cn")

# 白名单：逗号分隔的 open_id 列表。空 = 允许任何租户成员
ALLOWED_OPEN_IDS = {
    s.strip()
    for s in os.getenv("ALLOWED_OPEN_IDS", "").split(",")
    if s.strip()
}

# Flask session secret —— 生产环境必须从 env 读（固定值，否则重启后 session 失效）
SESSION_SECRET = os.getenv("SESSION_SECRET", "") or secrets.token_hex(32)

# 以下路径不需要登录
LOGIN_EXEMPT_PATHS = {
    "/login",
    "/oauth/callback",
    "/health",
    "/favicon.ico",
    "/api/wechat_script",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("wechat_scanner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("web_ui")

app = Flask(__name__)
app.secret_key = SESSION_SECRET
app.permanent_session_lifetime = timedelta(days=7)
# 通过 nginx 反代后，Flask 拿到的 scheme 是 http；设置下面这些让 session cookie 行为正确
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=REQUIRE_LOGIN,  # 生产开 HTTPS，本地 dev 关
)

# Core 业务门面单例
core = ScannerCore()


# ============================================================
# CORS — 本地模式下允许服务器页面跨域调用本地 API
# ============================================================

@app.after_request
def _cors_headers(response):
    """非服务器模式下，给所有 /api/* 和 /health 加 CORS header。
    这样 scanner.helioratech.com 的页面可以直接 fetch 127.0.0.1:5678 的 API。"""
    if not SERVER_MODE:
        path = request.path
        if path.startswith("/api/") or path == "/health":
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/api/<path:subpath>", methods=["OPTIONS"])
@app.route("/health", methods=["OPTIONS"])
def _cors_preflight(**kwargs):
    """处理 CORS 预检请求。"""
    from flask import Response
    resp = Response("", status=204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"] = "3600"
    return resp


# ============================================================
# 飞书 OAuth 登录墙 —— @before_request 守卫
# ============================================================

@app.before_request
def _auth_guard():
    """所有请求先过这里。未登录 → 跳 /login。"""
    if not REQUIRE_LOGIN:
        return None
    path = request.path
    if path in LOGIN_EXEMPT_PATHS or path.startswith("/static/"):
        return None
    if session.get("user_open_id"):
        return None
    # 记录用户想去的页面，登录后跳回
    if request.method == "GET":
        session["post_login_redirect"] = request.full_path.rstrip("?") or "/"
    return redirect(url_for("login"))


@app.route("/login")
def login():
    """跳到飞书授权页。"""
    if not REQUIRE_LOGIN:
        return redirect("/")
    if not FEISHU_APP_ID:
        return ("OAuth 未配置：缺少 FEISHU_APP_ID", 500)

    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    params = {
        "app_id": FEISHU_APP_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "state": state,
    }
    authorize_url = (
        f"{FEISHU_OPEN_BASE}/open-apis/authen/v1/index?"
        + urllib.parse.urlencode(params)
    )
    logger.info(f"[OAuth] 重定向到飞书授权页: state={state[:8]}...")
    return redirect(authorize_url)


@app.route("/oauth/callback")
def oauth_callback():
    """飞书授权后回到这里，拿 code 换 user_access_token 和身份。"""
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    expected_state = session.get("oauth_state", "")

    if not code:
        logger.warning("[OAuth] callback 缺少 code")
        return ("缺少授权码", 400)
    if not state or state != expected_state:
        logger.warning(
            f"[OAuth] state 不匹配: got={state[:8]} expected={expected_state[:8]}"
        )
        return ("state 校验失败（CSRF 防护）", 400)

    try:
        import requests  # lark_oapi 依赖，venv 里一定有
    except ImportError:
        return ("requests 库未安装", 500)

    # Step 1: app_access_token（自建应用 internal 模式）
    try:
        r1 = requests.post(
            f"{FEISHU_OPEN_BASE}/open-apis/auth/v3/app_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10,
        ).json()
    except Exception as e:
        logger.exception("[OAuth] app_access_token 请求异常")
        return (f"获取 app token 失败: {e}", 500)
    if r1.get("code") != 0:
        logger.error(f"[OAuth] app_access_token 返回错误: {r1}")
        return (f"app token 失败: {r1.get('msg')}", 500)
    app_token = r1.get("app_access_token", "")

    # Step 2: code → user_access_token + 身份
    try:
        r2 = requests.post(
            f"{FEISHU_OPEN_BASE}/open-apis/authen/v1/oidc/access_token",
            headers={"Authorization": f"Bearer {app_token}"},
            json={"grant_type": "authorization_code", "code": code},
            timeout=10,
        ).json()
    except Exception as e:
        logger.exception("[OAuth] code 换 token 异常")
        return (f"交换 token 失败: {e}", 500)
    logger.info(f"[OAuth] r2 raw response: {r2}")
    if r2.get("code") != 0:
        logger.error(f"[OAuth] code 交换失败: {r2}")
        return (f"授权码无效: {r2.get('msg')}", 400)

    token_data = r2.get("data", {})
    user_access_token = token_data.get("access_token", "")
    if not user_access_token:
        logger.error(f"[OAuth] r2 数据里无 access_token: {r2}")
        return ("授权返回缺少 access_token", 500)

    # Step 2b: 用 user_access_token 拉用户信息
    try:
        r3 = requests.get(
            f"{FEISHU_OPEN_BASE}/open-apis/authen/v1/user_info",
            headers={"Authorization": f"Bearer {user_access_token}"},
            timeout=10,
        ).json()
    except Exception as e:
        logger.exception("[OAuth] user_info 请求异常")
        return (f"获取用户信息失败: {e}", 500)
    logger.info(f"[OAuth] user_info response: code={r3.get('code')} name={r3.get('data', {}).get('name')} open_id={r3.get('data', {}).get('open_id')}")
    if r3.get("code") != 0:
        logger.error(f"[OAuth] user_info 失败: {r3}")
        return (f"获取用户信息失败: {r3.get('msg')}", 500)

    data = r3.get("data", {})
    open_id = data.get("open_id", "")
    name = data.get("name", "<未知>")
    tenant_key = data.get("tenant_key", "")

    # Step 3: 白名单检查
    if ALLOWED_OPEN_IDS and open_id not in ALLOWED_OPEN_IDS:
        logger.warning(
            f"[OAuth] 拒绝非白名单用户: name={name} open_id={open_id}"
        )
        return (
            f"<h2>访问被拒绝</h2>"
            f"<p>用户 <b>{name}</b> ({open_id}) 不在白名单内。</p>"
            f"<p>请联系管理员把你的 open_id 加入 ALLOWED_OPEN_IDS。</p>",
            403,
        )

    # Step 4: 写入 session（含 access_token / refresh_token 供后续 API 调用）
    session.permanent = True
    session["user_open_id"] = open_id
    session["user_name"] = name
    session["tenant_key"] = tenant_key
    session["user_access_token"] = user_access_token
    session["user_refresh_token"] = token_data.get("refresh_token", "")
    session["token_expires_in"] = token_data.get("expires_in", 7200)
    session["refresh_expires_in"] = token_data.get("refresh_expires_in", 2592000)
    import time
    session["token_obtained_at"] = int(time.time())
    session.pop("oauth_state", None)
    next_url = session.pop("post_login_redirect", "/")
    if not next_url.startswith("/"):
        next_url = "/"
    logger.info(f"[OAuth] 登录成功: {name} ({open_id}) → {next_url}")
    return redirect(next_url)


@app.route("/logout")
def logout():
    user = session.get("user_name", "?")
    session.clear()
    logger.info(f"[OAuth] 登出: {user}")
    return redirect(url_for("login"))


@app.route("/health")
def health():
    resp = jsonify({"ok": True, "server_mode": SERVER_MODE, "require_login": REQUIRE_LOGIN})
    # 允许远程服务器页面检测本地服务（CORS）
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ===== 微信解锁向导 API =====

_WECHAT_DECRYPT_DIR = Path(os.getenv(
    "WECHAT_DECRYPT_DIR_ROOT",
    Path(__file__).resolve().parent.parent / "wechat-decrypt",
))
_KEYS_FILE = _WECHAT_DECRYPT_DIR / "all_keys.json"
_KEY_EXTRACTOR = _WECHAT_DECRYPT_DIR / "find_all_keys_macos"


@app.route("/api/wechat_setup/status")
def wechat_setup_status():
    """检测微信解锁各步骤的状态"""
    import platform, subprocess as sp
    import config as scanner_config

    is_mac = platform.system() == "Darwin"

    # SIP 状态
    sip_disabled = False
    if is_mac:
        try:
            r = sp.run(["csrutil", "status"], capture_output=True, text=True, timeout=5)
            sip_disabled = "disabled" in r.stdout.lower()
        except Exception:
            pass

    # 微信是否运行
    wechat_running = False
    if is_mac:
        try:
            r = sp.run(["pgrep", "-x", "WeChat"], capture_output=True, timeout=5)
            wechat_running = r.returncode == 0
        except Exception:
            pass

    # 密钥是否存在
    keys_exist = _KEYS_FILE.exists()
    keys_age_hours = None
    if keys_exist:
        keys_age_hours = round((time.time() - _KEYS_FILE.stat().st_mtime) / 3600, 1)

    # 解密数据是否存在
    decrypted_dir = Path(scanner_config.DECRYPTED_DB_DIR) if hasattr(scanner_config, "DECRYPTED_DB_DIR") else None
    has_decrypted = False
    db_count = 0
    if decrypted_dir and (decrypted_dir / "message").exists():
        dbs = list((decrypted_dir / "message").glob("*.db"))
        db_count = len(dbs)
        has_decrypted = db_count > 0

    # reader 是否已加载
    reader_loaded = getattr(core, "reader", None) is not None

    # extractor 是否存在
    extractor_ready = _KEY_EXTRACTOR.exists() and os.access(_KEY_EXTRACTOR, os.X_OK)

    return jsonify({
        "is_mac": is_mac,
        "sip_disabled": sip_disabled,
        "wechat_running": wechat_running,
        "keys_exist": keys_exist,
        "keys_age_hours": keys_age_hours,
        "extractor_ready": extractor_ready,
        "has_decrypted": has_decrypted,
        "db_count": db_count,
        "reader_loaded": reader_loaded,
        "extract_command": f"sudo {_KEY_EXTRACTOR}",
        "decrypt_dir": str(_WECHAT_DECRYPT_DIR),
    })


@app.route("/api/wechat_setup/decrypt", methods=["POST"])
def wechat_setup_decrypt():
    """用现有密钥重新解密 + 加载 reader。不需要 sudo。"""
    if SERVER_MODE:
        return jsonify({"ok": False, "error": "服务器模式不支持微信解密"}), 400

    if not _KEYS_FILE.exists():
        return jsonify({"ok": False, "error": "密钥文件不存在，请先在终端运行密钥提取命令"}), 400

    try:
        result = core.refresh_db()
        if result.ok:
            return jsonify({"ok": True, "session_count": result.session_count})
        else:
            return jsonify({"ok": False, "error": result.error}), 500
    except Exception as e:
        logger.exception("[Setup] 解密失败")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/wechat_launcher")
def wechat_launcher():
    """生成一键启动 .command 脚本，用户下载后双击即可。"""
    from flask import Response
    script = '''#!/bin/bash
# 微信扫描工作台 — 一键启动
# 双击打开此文件即可（首次可能需要右键→打开）

# 确保自身可执行（下次双击就不需要右键了）
chmod +x "$0" 2>/dev/null

echo ""
echo "========================================"
echo "  微信扫描工作台 — 启动中..."
echo "========================================"
echo ""

# 查找项目目录
SCAN_DIR="$HOME/Desktop/feishu/wechat-scanner"
if [ ! -d "$SCAN_DIR" ]; then
    SCAN_DIR="$(find "$HOME" -maxdepth 3 -name "wechat-scanner" -type d 2>/dev/null | head -1)"
fi

# 如果项目不存在，尝试克隆
if [ ! -d "$SCAN_DIR" ] || [ ! -f "$SCAN_DIR/web_ui.py" ]; then
    echo "[!] 未找到项目目录，正在下载..."
    mkdir -p "$HOME/Desktop/feishu" 2>/dev/null
    SCAN_DIR="$HOME/Desktop/feishu/wechat-scanner"
    if command -v git &>/dev/null; then
        git clone https://github.com/helioratech/wechat-scanner.git "$SCAN_DIR" 2>/dev/null
    fi
    if [ ! -f "$SCAN_DIR/web_ui.py" ]; then
        echo "[错误] 无法下载项目。请联系管理员获取项目文件。"
        echo ""
        echo "按回车键关闭..."
        read
        exit 1
    fi
fi

cd "$SCAN_DIR"
echo "[+] 项目目录: $SCAN_DIR"

# 检查 Python
PY=$(command -v python3 2>/dev/null)
if [ -z "$PY" ]; then
    echo ""
    echo "[错误] 未找到 python3"
    echo ""
    echo "请安装 Python 3.9+:"
    echo "  下载: https://www.python.org/downloads/"
    echo "  或: brew install python3"
    echo ""
    echo "安装后再次运行此文件即可。"
    echo ""
    echo "按回车键关闭..."
    read
    exit 1
fi
echo "[+] Python: $($PY --version)"

# 检查/安装依赖
$PY -c "import flask" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "[!] 首次运行，安装依赖中（约30秒）..."
    pip3 install flask python-dotenv lark-oapi anthropic pycryptodome requests 2>&1 | tail -3
    echo "[+] 依赖安装完成"
fi

echo ""
echo "[+] 启动本地服务: http://127.0.0.1:5678"
echo "[+] 关闭此窗口即可停止"
echo ""

# 自动打开浏览器
sleep 2 && open "http://127.0.0.1:5678" &

$PY web_ui.py --port 5678 --no-browser
'''
    resp = Response(script, mimetype='application/octet-stream')
    # HTTP headers 只能 ASCII，中文文件名用 RFC 5987 编码
    encoded_name = urllib.parse.quote('启动微信扫描.command')
    resp.headers['Content-Disposition'] = (
        f"attachment; filename=\"wechat-scanner.command\"; filename*=UTF-8''{encoded_name}"
    )
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@app.route("/api/wechat_script")
def wechat_script():
    """供 curl | bash 使用的启动脚本，纯文本，无需下载文件。"""
    from flask import Response
    script = '''#!/bin/bash
# 微信扫描工作台 — 一键启动
set -e

echo ""
echo "========================================"
echo "  微信扫描工作台 — 启动中..."
echo "========================================"
echo ""

# 查找项目目录
SCAN_DIR="$HOME/Desktop/feishu/wechat-scanner"
if [ ! -d "$SCAN_DIR" ]; then
    SCAN_DIR="$(find "$HOME" -maxdepth 3 -name "wechat-scanner" -type d 2>/dev/null | head -1)"
fi

# 如果项目不存在，尝试克隆
if [ ! -d "$SCAN_DIR" ] || [ ! -f "$SCAN_DIR/web_ui.py" ]; then
    echo "[!] 未找到项目目录，正在下载..."
    mkdir -p "$HOME/Desktop/feishu" 2>/dev/null
    SCAN_DIR="$HOME/Desktop/feishu/wechat-scanner"
    if command -v git &>/dev/null; then
        git clone https://github.com/helioratech/wechat-scanner.git "$SCAN_DIR" 2>/dev/null || true
    fi
    if [ ! -f "$SCAN_DIR/web_ui.py" ]; then
        echo "[错误] 无法下载项目。请联系管理员获取项目文件。"
        exit 1
    fi
fi

cd "$SCAN_DIR"
echo "[+] 项目目录: $SCAN_DIR"

# 检查 Python
PY=$(command -v python3 2>/dev/null)
if [ -z "$PY" ]; then
    echo ""
    echo "[错误] 未找到 python3"
    echo "  请安装: https://www.python.org/downloads/"
    echo "  或运行: brew install python3"
    exit 1
fi
echo "[+] Python: $($PY --version)"

# 检查/安装依赖
if ! $PY -c "import flask" 2>/dev/null; then
    echo "[!] 首次运行，安装依赖中..."
    pip3 install flask python-dotenv lark-oapi anthropic pycryptodome requests 2>&1 | tail -3
    echo "[+] 依赖安装完成"
fi

echo ""
echo "[+] 启动本地服务: http://127.0.0.1:5678"
echo "[+] 按 Ctrl+C 停止"
echo ""

# 等本地服务就绪后，打开服务器页面（自动切换到微信扫描 tab）
(sleep 3 && open "https://scanner.helioratech.com?tab=wechat") &

$PY web_ui.py --port 5678 --no-browser
'''
    return Response(script, mimetype='text/plain; charset=utf-8')


@app.route("/api/wechat_setup/reload", methods=["POST"])
def wechat_setup_reload():
    """仅重新加载已解密的数据（不重新解密）"""
    if SERVER_MODE:
        return jsonify({"ok": False, "error": "服务器模式不支持"}), 400
    try:
        count = core.init_reader()
        return jsonify({"ok": True, "session_count": count})
    except Exception as e:
        logger.exception("[Setup] 加载失败")
        return jsonify({"ok": False, "error": str(e)}), 500

# 仅 view-layer 状态（log 缓冲用于前端轮询）
import threading as _threading
_scan_logs_lock = _threading.Lock()
scan_logs: list[str] = []
scan_running = False


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微信扫描器 — Deal Flow</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #FAFAF8; color: #1A1A1A; font-size: 14px; line-height: 1.6; }
.container { max-width: 1500px; margin: 0 auto; padding: 20px; position: relative; }
h1 { text-align: center; font-size: 26px; font-weight: 600; margin: 20px 0 5px; color: #1A1A1A; }
h1::before { content: ""; display: block; width: 40px; height: 3px; background: #D97706; border-radius: 2px; margin: 0 auto 14px; }
.subtitle { text-align: center; color: #6B6B6B; margin-bottom: 20px; font-size: 14px; }

/* 左右两栏 */
.layout { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: 20px; align-items: start; }
@media (max-width: 1000px) { .layout { grid-template-columns: 1fr; } }

/* ===== 手机适配 ===== */
@media (max-width: 600px) {
  .container { padding: 12px; }
  h1 { font-size: 20px; margin: 12px 0 4px; }
  h1::before { margin-bottom: 10px; }
  .subtitle { font-size: 12px; margin-bottom: 14px; }

  /* Tabs 紧凑 */
  .tabs { gap: 0; }
  .tab { padding: 10px 16px; font-size: 14px; }

  /* 卡片 */
  .card { padding: 14px; border-radius: 10px; margin-bottom: 12px; }
  .card h3 { font-size: 15px; }

  /* 按钮 */
  .btn { padding: 10px 16px; font-size: 13px; width: 100%; }
  .actions { flex-direction: column; gap: 8px; }

  /* 表格：隐藏次要列 */
  table { font-size: 13px; }
  th:nth-child(3), td:nth-child(3),
  th:nth-child(4), td:nth-child(4) { display: none; }
  th, td { padding: 8px 6px; }

  /* 日志 */
  .log-box { font-size: 11px; padding: 10px; height: 160px; border-radius: 8px; }

  /* 结果卡片 */
  .result-item { padding: 12px; }
  .result-item h4 { padding-right: 0; font-size: 14px; }
  .result-item .meta { padding-right: 0; font-size: 12px; }
  .result-item .item-actions { position: static; margin-top: 8px; justify-content: flex-start; }
  .btn-sm { padding: 6px 14px; }

  /* 历史 */
  .history-item .h-meta { gap: 6px; }

  /* 用户信息 */
  .container > div[style*="position:absolute"] { position: static !important; text-align: center; margin-bottom: 10px; }

  /* 搜索栏 */
  .select-all-bar { font-size: 12px; gap: 6px; }

  /* Modal */
  .modal { padding: 16px; border-radius: 10px; }

  /* 微信远程面板 */
  #wechat-remote-panel .card { padding: 24px 16px; margin: 20px auto; }

  /* 手机端：服务器模式隐藏微信远程 tab（手机无法启动本地 Mac 服务） */
  .tab[data-tab="wechat-remote"] { display: none; }
}
.col { min-width: 0; }
.status { text-align: center; padding: 10px 14px; background: #ECFDF5; color: #166534; border-radius: 8px; margin-bottom: 15px; font-size: 14px; border: 1px solid #BBF7D0; }
.status.error { background: #FEF2F2; color: #991B1B; border-color: #FECACA; }
.status.loading { background: #FFFBEB; color: #92400E; border-color: #FDE68A; }

/* 会话列表 */
.card { background: #FFFFFF; border-radius: 12px; padding: 20px; margin-bottom: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04); border: 1px solid #E8E5E0; }
.card h3 { font-size: 16px; font-weight: 600; margin-bottom: 10px; color: #1A1A1A; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; padding: 8px 10px; background: #F9F7F4; color: #6B6B6B; font-weight: 500; border-bottom: 1px solid #E8E5E0; }
td { padding: 8px 10px; border-bottom: 1px solid #F0EDE8; }
tr.selected { background: #FEF3C7; }
tr:hover { background: #F9F7F4; cursor: pointer; }
tr.selected:hover { background: #FDE68A; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.tag.group { background: #FEF3C7; color: #92400E; }
.tag.private { background: #F3E8FF; color: #7C3AED; }
td.num { text-align: center; }

/* 按钮 */
.actions { display: flex; gap: 10px; justify-content: center; margin: 15px 0; flex-wrap: wrap; }
.btn { padding: 10px 22px; border: none; border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer; transition: all 0.2s ease; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-primary { background: #D97706; color: white; }
.btn-primary:hover:not(:disabled) { background: #B45309; }
.btn-secondary { background: #F5F0EB; color: #1A1A1A; }
.btn-secondary:hover:not(:disabled) { background: #EDE8E2; }
.btn-success { background: #16A34A; color: white; }
.btn-success:hover:not(:disabled) { background: #15803D; }
.btn-danger { background: #DC2626; color: white; }
.btn-danger:hover:not(:disabled) { background: #B91C1C; }

/* 日志 */
.log-box { background: #1A1A1A; color: #A3E635; border-radius: 10px; padding: 16px; font-family: "Menlo", "SF Mono", monospace; font-size: 12px; height: 200px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }

/* 结果 */
.result-item { border-left: 4px solid #D97706; padding: 14px 18px; margin: 10px 0; background: #FAFAF8; border-radius: 0 10px 10px 0; position: relative; transition: opacity 0.4s, transform 0.4s; border: 1px solid #E8E5E0; border-left: 4px solid #D97706; }
.result-item.writing { opacity: 0.6; }
.result-item.written { opacity: 0; transform: translateX(20px); pointer-events: none; }
.result-item h4 { color: #D97706; margin-bottom: 6px; padding-right: 90px; font-weight: 600; }
.result-item .meta { color: #6B6B6B; font-size: 13px; padding-right: 90px; }
.result-item .summary { margin-top: 6px; font-size: 14px; line-height: 1.6; }
.result-item .item-actions { position: absolute; top: 14px; right: 14px; display: flex; gap: 6px; }
.btn-sm { padding: 5px 12px; font-size: 12px; border-radius: 6px; border: none; cursor: pointer; font-weight: 500; transition: all 0.2s ease; }
.btn-sm.btn-success { background: #16A34A; color: white; }
.btn-sm.btn-success:hover:not(:disabled) { background: #15803D; }
.btn-sm.btn-danger { background: #DC2626; color: white; }
.btn-sm.btn-danger:hover:not(:disabled) { background: #B91C1C; }
.btn-sm:disabled { opacity: 0.5; cursor: not-allowed; }
.empty-results { text-align: center; color: #6B6B6B; padding: 20px; font-size: 13px; }

.select-all-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; font-size: 13px; color: #6B6B6B; }

/* 结果卡片内字段行 */
.result-item .field-row { display: flex; gap: 6px; align-items: center; margin-top: 6px; font-size: 13px; flex-wrap: wrap; }
.result-item .field-row label { color: #6B6B6B; min-width: 44px; }
.result-item .edit-input { flex: 1; min-width: 120px; padding: 4px 8px; border: 1px solid #E8E5E0; border-radius: 6px; font-size: 13px; background: white; }
.result-item .edit-input:focus { outline: none; border-color: #D97706; box-shadow: 0 0 0 2px rgba(217,119,6,0.15); }
.result-item .summary-edit { width: 100%; min-height: 60px; padding: 6px 8px; border: 1px solid #E8E5E0; border-radius: 6px; font-family: inherit; font-size: 13px; resize: vertical; margin-top: 6px; }
.result-item.editing { background: #FFFBEB; border-left-color: #FBBF24; }

.result-item .summary-preview { margin-top: 6px; font-size: 14px; line-height: 1.6; max-height: 72px; overflow: hidden; position: relative; }
.result-item .summary-preview.expanded { max-height: none; }
.result-item .summary-preview.collapsed::after { content: ""; position: absolute; bottom: 0; left: 0; right: 0; height: 24px; background: linear-gradient(transparent, #FAFAF8); }
.result-item .summary-toggle { font-size: 12px; color: #D97706; cursor: pointer; margin-top: 4px; display: inline-block; }

.result-item.status-written { border-left-color: #16A34A; background: #F0FDF4; }
.result-item.status-failed { border-left-color: #DC2626; }
.result-item.status-dismissed { border-left-color: #8E8E93; opacity: 0.6; }

/* 历史面板 */
.history-item { padding: 10px 12px; border: 1px solid #E8E5E0; border-radius: 8px; margin-bottom: 8px; cursor: pointer; transition: all 0.2s; }
.history-item:hover { background: #F9F7F4; border-color: #D97706; }
.history-item .h-title { font-size: 13px; font-weight: 500; }
.history-item .h-meta { font-size: 11px; color: #6B6B6B; margin-top: 3px; display: flex; gap: 10px; flex-wrap: wrap; }
.history-item .h-badge { display: inline-block; padding: 1px 6px; border-radius: 8px; font-size: 10px; }
.h-badge.pending { background: #FEF3C7; color: #92400E; }
.h-badge.written { background: #ECFDF5; color: #166534; }
.h-badge.dismissed { background: #F5F0EB; color: #6B6B6B; }

/* Modal */
.modal-backdrop { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 100; align-items: center; justify-content: center; }
.modal-backdrop.show { display: flex; }
.modal { background: white; border-radius: 12px; padding: 24px; max-width: 520px; width: 90%; max-height: 80vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.15); border: 1px solid #E8E5E0; }
.modal h3 { margin-bottom: 12px; font-size: 16px; font-weight: 600; }
.modal .bl-item { display: flex; justify-content: space-between; align-items: center; padding: 8px; border-bottom: 1px solid #F0EDE8; font-size: 13px; }
.modal .bl-item .remove-btn { background: none; border: none; color: #DC2626; cursor: pointer; font-size: 12px; }
.modal-close { margin-top: 12px; text-align: right; }

/* Tabs */
.tabs { display: flex; gap: 4px; border-bottom: 2px solid #E8E5E0; margin-bottom: 20px; padding: 0 4px; }
.tab { background: none; border: none; padding: 12px 24px; font-size: 15px; font-weight: 500; color: #6B6B6B; cursor: pointer; border-bottom: 3px solid transparent; margin-bottom: -2px; transition: all 0.2s; }
.tab:hover { color: #1A1A1A; }
.tab.tab-active { color: #D97706; border-bottom-color: #D97706; }
.tab-panel { display: none; }
.tab-panel.tab-panel-active { display: block; }
/* 微信解锁向导样式 */
.setup-step { border:1px solid #E8E5E0; border-radius:12px; margin-bottom:12px; overflow:hidden; }
.step-header { display:flex; align-items:center; gap:10px; padding:14px 16px; background:#F9F7F4; cursor:pointer; }
.step-num { width:26px; height:26px; border-radius:50%; background:#E8E5E0; color:#1A1A1A; display:flex; align-items:center; justify-content:center; font-weight:600; font-size:13px; flex-shrink:0; }
.step-title { flex:1; font-weight:500; font-size:14px; }
.step-badge { font-size:11px; padding:3px 10px; border-radius:20px; font-weight:500; }
.badge-pending { background:#E8E5E0; color:#6B6B6B; }
.badge-ok { background:#DCFCE7; color:#166534; }
.badge-warn { background:#FEF3C7; color:#92400E; }
.badge-action { background:#DBEAFE; color:#1E40AF; }
.badge-running { background:#FEF3C7; color:#92400E; animation:pulse 1.5s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }
@keyframes spin { to { transform:rotate(360deg); } }
.step-body { padding:0 16px 14px; font-size:13px; line-height:1.6; color:#1A1A1A; }
.step-done .step-num { background:#16A34A; color:#fff; }
.step-active .step-num { background:#D97706; color:#fff; }
</style>
</head>
<body>
<div class="container">
  {% if current_user %}
  <div style="position:absolute;top:20px;right:28px;display:flex;align-items:center;gap:10px;font-size:13px;color:#6B6B6B;">
    <span>👤 {{ current_user }}</span>
    <a href="/logout" style="color:#D97706;text-decoration:none;">登出</a>
  </div>
  {% endif %}
  <h1 id="pageTitle">扫描工作台</h1>
  <p class="subtitle" id="pageSubtitle">微信 DB 解密 + 飞书历史扫描 | 选择来源 → AI 分析 → 入库</p>

  <div class="tabs">
    {% if not server_mode %}
    <button class="tab tab-active" data-tab="wechat" onclick="switchTab('wechat')">微信扫描</button>
    <button class="tab" data-tab="feishu" onclick="switchTab('feishu')">飞书扫描</button>
    {% else %}
    <button class="tab tab-active" data-tab="feishu" onclick="switchTab('feishu')">飞书扫描</button>
    <button class="tab" data-tab="wechat-remote" onclick="switchTab('wechat-remote')">微信扫描</button>
    {% endif %}
    {% if server_mode %}
    <button class="tab" data-tab="wiki" onclick="switchTab('wiki')">知识库</button>
    {% endif %}
  </div>

  <!-- ============ 微信扫描 tab ============ -->
  {% if not server_mode %}
  <div id="wechat-panel" class="tab-panel tab-panel-active">

  <!-- ===== 微信解锁向导（未就绪时显示） ===== -->
  <div id="wechat-setup-wizard" style="display:none;">
    <div class="card" style="max-width:700px;margin:30px auto;">
      <h3 style="text-align:center;margin-bottom:20px;">🔓 解锁微信扫描</h3>
      <p style="color:#6B6B6B;text-align:center;margin-bottom:24px;">
        微信消息数据库已加密，需要以下步骤解锁。完成后即可在工作台中扫描微信会话。
      </p>

      <div id="setup-steps">
        <!-- Step 1: SIP -->
        <div class="setup-step" id="step-sip">
          <div class="step-header">
            <span class="step-num">1</span>
            <span class="step-title">关闭 macOS SIP（系统完整性保护）</span>
            <span class="step-badge" id="badge-sip">检测中...</span>
          </div>
          <div class="step-body" id="body-sip">
            <p>SIP 阻止读取微信进程内存。需要一次性关闭：</p>
            <ol style="margin:8px 0;padding-left:20px;line-height:1.8;">
              <li>关闭 Mac</li>
              <li>长按电源键 → 进入"选项"</li>
              <li>菜单栏 → 实用工具 → 终端</li>
              <li>输入 <code style="background:#F5F0EB;padding:2px 6px;border-radius:4px;">csrutil disable</code> 回车</li>
              <li>重启 Mac</li>
            </ol>
            <p style="color:#6B6B6B;font-size:12px;">只需操作一次，后续不再需要。</p>
          </div>
        </div>

        <!-- Step 2: WeChat -->
        <div class="setup-step" id="step-wechat">
          <div class="step-header">
            <span class="step-num">2</span>
            <span class="step-title">启动并登录微信</span>
            <span class="step-badge" id="badge-wechat">检测中...</span>
          </div>
          <div class="step-body" id="body-wechat">
            <p>请打开微信 App 并确保已登录。密钥提取需要微信正在运行。</p>
          </div>
        </div>

        <!-- Step 3: Extract Keys -->
        <div class="setup-step" id="step-keys">
          <div class="step-header">
            <span class="step-num">3</span>
            <span class="step-title">提取加密密钥</span>
            <span class="step-badge" id="badge-keys">检测中...</span>
          </div>
          <div class="step-body" id="body-keys">
            <p>打开「终端 App」，粘贴以下命令并回车：</p>
            <div style="display:flex;align-items:center;gap:8px;margin:10px 0;">
              <code id="extract-cmd" style="flex:1;background:#1A1A1A;color:#A3E635;padding:10px 14px;border-radius:8px;font-size:13px;word-break:break-all;"></code>
              <button onclick="copyCmd()" class="btn-sm" style="background:#D97706;color:#fff;white-space:nowrap;">复制</button>
            </div>
            <p style="color:#6B6B6B;font-size:12px;">需要输入 Mac 开机密码。提取完成后回到这里点击"检查状态"。</p>
            <button onclick="checkSetupStatus()" class="btn btn-secondary" style="margin-top:8px;">🔄 检查状态</button>
          </div>
        </div>

        <!-- Step 4: Decrypt -->
        <div class="setup-step" id="step-decrypt">
          <div class="step-header">
            <span class="step-num">4</span>
            <span class="step-title">解密数据库</span>
            <span class="step-badge" id="badge-decrypt">检测中...</span>
          </div>
          <div class="step-body" id="body-decrypt">
            <p>密钥已就位，点击下方按钮自动解密：</p>
            <button onclick="runDecrypt()" class="btn btn-primary" id="btn-decrypt" disabled style="margin-top:8px;">
              🔐 解密微信数据库
            </button>
            <div id="decrypt-progress" style="display:none;margin-top:10px;color:#6B6B6B;">
              ⏳ 正在解密，可能需要 1-2 分钟...
            </div>
          </div>
        </div>

        <!-- Step 5: Done -->
        <div class="setup-step" id="step-done">
          <div class="step-header">
            <span class="step-num">5</span>
            <span class="step-title">完成</span>
            <span class="step-badge" id="badge-done">等待</span>
          </div>
          <div class="step-body" id="body-done">
            <p>解密完成后，点击下方按钮加载数据并开始使用：</p>
            <button onclick="reloadAndSwitch()" class="btn btn-primary" id="btn-reload" disabled style="margin-top:8px;">
              🚀 加载数据 & 开始扫描
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ===== 正常扫描 UI（就绪后显示） ===== -->
  <div id="wechat-scanner-ui" style="{% if not wechat_ready %}display:none;{% endif %}">
  <div id="status" class="status">正在加载数据库...</div>

  <div class="layout">
    <!-- 左栏：会话列表 + 操作按钮 -->
    <div class="col">
      <div class="card">
        <h3>会话列表</h3>
        <div style="display:flex; gap:10px; align-items:center; margin-bottom:10px;">
          <input type="text" id="searchBox" placeholder="搜索会话名称..." oninput="filterSessions()" style="flex:1; padding:8px 12px; border:1px solid #E8E5E0; border-radius:8px; font-size:14px; outline:none;">
          <label style="font-size:13px; white-space:nowrap;"><input type="checkbox" id="groupOnly" onchange="filterSessions()"> 只看群聊</label>
        </div>
        <div class="select-all-bar">
          <label><input type="checkbox" id="selectAll" onchange="toggleSelectAll()"> 全选(当前筛选)</label>
          <span id="selectedCount">已选 0 个</span>
          <span id="totalCount" style="margin-left:auto;"></span>
        </div>
        <div style="max-height: 500px; overflow-y: auto;">
          <table>
            <thead><tr><th style="width:40px"></th><th>名称</th><th style="width:120px">最后消息</th><th style="width:80px">消息数</th><th style="width:60px">类型</th></tr></thead>
            <tbody id="sessionList"><tr><td colspan="5" style="text-align:center;color:#6B6B6B;">加载中...</td></tr></tbody>
          </table>
        </div>
      </div>

      <div class="actions">
        <button class="btn btn-secondary" onclick="refreshDB()" id="btnRefresh">刷新数据库</button>
        <button class="btn btn-secondary" onclick="previewChat()" id="btnPreview" disabled>预览消息</button>
        <button class="btn btn-primary" onclick="smartScan()" id="btnSmart" disabled>智能扫描</button>
      </div>

      <div class="card">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
          <h3 style="margin:0;">历史扫描</h3>
          <div style="display:flex; gap:6px;">
            <button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="loadHistory()">刷新</button>
            <button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="showBlacklist()">黑名单</button>
          </div>
        </div>
        <div id="historyList" style="max-height: 280px; overflow-y: auto;">
          <div class="empty-results">暂无历史</div>
        </div>
        <div id="pdfStats" style="font-size:11px;color:#6B6B6B;margin-top:6px;text-align:center;"></div>
      </div>
    </div>

    <!-- 右栏：日志 + 扫描结果 -->
    <div class="col">
      <div class="card">
        <h3>日志</h3>
        <div class="log-box" id="logBox">等待操作...\n</div>
      </div>

      <div class="card" id="resultsCard" style="display:none;">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
          <h3 style="margin:0;">扫描结果 <span id="resultsCount" style="color:#6B6B6B;font-size:13px;font-weight:normal;"></span></h3>
          <div style="display:flex; gap:8px;">
            <button class="btn-sm btn-success" onclick="writeAll()">全部写入</button>
            <button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="saveLocal()">保存到本地</button>
          </div>
        </div>
        <div id="resultsList"></div>
      </div>
    </div>
  </div>

  </div><!-- /#wechat-scanner-ui -->
  </div><!-- /#wechat-panel -->
  {% endif %}

  <!-- ============ 微信扫描（服务器模式） ============ -->
  {% if server_mode %}
  <div id="wechat-remote-panel" class="tab-panel">

    <!-- ===== 安装引导（未连接时显示） ===== -->
    <div id="wx-setup-card" class="card" style="max-width:520px;margin:50px auto;text-align:center;padding:40px 30px;">

      <!-- 状态 A：检测中 -->
      <div id="wx-state-checking">
        <div style="font-size:36px;margin-bottom:12px;">⏳</div>
        <p style="color:#6B6B6B;">正在检测本地微信扫描服务...</p>
      </div>

      <!-- 状态 B：未启动 — 初始状态，显示大按钮 -->
      <div id="wx-state-offline" style="display:none;">
        <div style="font-size:48px;margin-bottom:16px;">📱</div>
        <h3 style="margin-bottom:8px;">微信扫描</h3>
        <p style="color:#6B6B6B;margin-bottom:28px;">微信消息存在你的 Mac 本地，一键启动扫描服务。</p>
        <button onclick="launchWx()" id="wx-launch-btn"
                style="padding:16px 48px;background:#D97706;color:#fff;border:none;border-radius:14px;font-size:17px;font-weight:600;cursor:pointer;transition:all .2s;">
          一键启动微信扫描
        </button>
      </div>

      <!-- 状态 B2：已复制，引导用户粘贴 -->
      <div id="wx-state-copied" style="display:none;">
        <!-- 成功提示 -->
        <div style="display:inline-flex;align-items:center;gap:8px;background:#ECFDF5;border:1px solid #BBF7D0;border-radius:20px;padding:6px 16px;margin-bottom:20px;">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="8" fill="#16A34A"/><path d="M5 8.5L7 10.5L11 6" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span style="color:#166534;font-size:13px;font-weight:500;">命令已复制到剪贴板</span>
        </div>

        <h3 style="margin-bottom:16px;font-size:17px;">在终端中粘贴运行</h3>

        <!-- 两步指引 — 紧凑横排 -->
        <div style="display:flex;gap:12px;margin-bottom:20px;">
          <div style="flex:1;background:#F9F7F4;border:1px solid #E8E5E0;border-radius:10px;padding:14px 12px;text-align:center;">
            <div style="font-size:13px;color:#6B6B6B;margin-bottom:4px;">第一步</div>
            <div style="font-size:14px;font-weight:500;">
              <kbd style="background:#fff;border:1px solid #E8E5E0;padding:2px 6px;border-radius:4px;font-size:12px;font-family:inherit;">&#8984; 空格</kbd>
              <span style="color:#6B6B6B;margin:0 4px;">→</span> 输入<strong>终端</strong>
            </div>
          </div>
          <div style="flex:1;background:#F9F7F4;border:1px solid #E8E5E0;border-radius:10px;padding:14px 12px;text-align:center;">
            <div style="font-size:13px;color:#6B6B6B;margin-bottom:4px;">第二步</div>
            <div style="font-size:14px;font-weight:500;">
              <kbd style="background:#fff;border:1px solid #E8E5E0;padding:2px 6px;border-radius:4px;font-size:12px;font-family:inherit;">&#8984;V</kbd>
              <span style="color:#6B6B6B;margin:0 4px;">→</span> <strong>回车</strong>
            </div>
          </div>
        </div>

        <!-- 命令预览 — 可折叠 -->
        <details style="margin-bottom:16px;text-align:left;">
          <summary style="font-size:12px;color:#6B6B6B;cursor:pointer;text-align:center;">查看命令内容</summary>
          <div style="background:#1A1A1A;border-radius:8px;padding:10px 14px;margin-top:8px;position:relative;">
            <code style="color:#A3E635;font-size:11px;font-family:'SF Mono',Menlo,monospace;word-break:break-all;">curl -sL https://scanner.helioratech.com/api/wechat_script | bash</code>
            <button onclick="reCopy()" id="wx-recopy"
                    style="position:absolute;top:7px;right:8px;background:#44403C;color:#fff;border:none;border-radius:5px;padding:3px 10px;font-size:11px;cursor:pointer;">
              复制
            </button>
          </div>
        </details>

        <div id="wx-polling" style="display:flex;align-items:center;justify-content:center;gap:8px;">
          <div style="width:14px;height:14px;border:2px solid #D97706;border-top-color:transparent;border-radius:50%;animation:spin 0.8s linear infinite;"></div>
          <span style="color:#6B6B6B;font-size:13px;">等待本地服务启动...</span>
        </div>
      </div>

    </div><!-- /#wx-setup-card -->

    <!-- ===== 操作面板（连接后直接显示，取代安装引导） ===== -->
    <div id="wx-embedded-ui" style="display:none;">
    <div style="background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;padding:10px 16px;margin-bottom:16px;display:flex;align-items:center;gap:10px;font-size:13px;">
      <span style="font-size:16px;">🔒</span>
      <span style="color:#92400E;flex:1;">微信数据仅在本地处理，不会上传到服务器。所有 API 调用直连你的 Mac (127.0.0.1)。</span>
      <span id="wx-local-status" style="color:#16A34A;font-weight:500;white-space:nowrap;">● 已连接</span>
    </div>

    <div id="wx-remote-status" class="status">正在加载本地数据...</div>

    <div class="layout">
      <div class="col">
        <div class="card">
          <h3>微信会话</h3>
          <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;">
            <input type="text" id="wxRemoteSearch" placeholder="搜索会话名称..." oninput="wxFilterSessions()" style="flex:1;padding:8px 12px;border:1px solid #E8E5E0;border-radius:8px;font-size:14px;outline:none;">
            <label style="font-size:13px;white-space:nowrap;"><input type="checkbox" id="wxRemoteGroupOnly" onchange="wxFilterSessions()"> 只看群聊</label>
          </div>
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:13px;color:#6B6B6B;">
            <label><input type="checkbox" id="wxRemoteSelectAll" onchange="wxToggleSelectAll()"> 全选</label>
            <span id="wxRemoteSelectedCount">已选 0 个</span>
            <span id="wxRemoteTotalCount" style="margin-left:auto;"></span>
          </div>
          <div style="max-height:450px;overflow-y:auto;">
            <table>
              <thead><tr><th style="width:36px"></th><th>名称</th><th style="width:100px">最后消息</th><th style="width:60px">消息数</th><th style="width:50px">类型</th></tr></thead>
              <tbody id="wxRemoteSessionList"><tr><td colspan="5" style="text-align:center;color:#6B6B6B;">加载中...</td></tr></tbody>
            </table>
          </div>
        </div>

        <div class="actions">
          <button class="btn btn-secondary" onclick="wxRefreshDB()">刷新数据库</button>
          <button class="btn btn-secondary" onclick="wxPreviewChat()" id="wxBtnPreview" disabled>预览消息</button>
          <button class="btn btn-primary" onclick="wxSmartScan()" id="wxBtnScan" disabled>智能扫描</button>
        </div>
      </div>

      <div class="col">
        <!-- 消息预览（点击预览后显示） -->
        <div class="card" id="wxPreviewCard" style="display:none;">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
            <h3 style="margin:0;">消息预览 <span id="wxPreviewTitle" style="color:#6B6B6B;font-size:13px;font-weight:normal;"></span></h3>
            <div style="display:flex;gap:6px;">
              <button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="wxPreviewMore()">加载更多</button>
              <button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="document.getElementById('wxPreviewCard').style.display='none'">关闭</button>
            </div>
          </div>
          <div id="wxPreviewMessages" style="max-height:400px;overflow-y:auto;font-size:13px;line-height:1.6;"></div>
        </div>

        <div class="card">
          <h3>扫描日志</h3>
          <div class="log-box" id="wxRemoteLog" style="height:300px;">等待操作...\n</div>
        </div>

        <div class="card" id="wxRemoteResultsCard" style="display:none;">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
            <h3 style="margin:0;">扫描结果 <span id="wxRemoteResultsCount" style="color:#6B6B6B;font-size:13px;font-weight:normal;"></span></h3>
            <button class="btn-sm btn-success" onclick="wxWriteAll()">全部写入飞书</button>
          </div>
          <div id="wxRemoteResultsList"></div>
        </div>
      </div>
    </div>
  </div><!-- /#wx-embedded-ui -->
  </div><!-- /#wechat-remote-panel -->
  {% endif %}

  <!-- ============ 飞书扫描 tab ============ -->
  <div id="feishu-panel" class="tab-panel {% if server_mode %}tab-panel-active{% endif %}">
    <div id="feishu-status" class="status">准备就绪</div>

    <div class="layout">
      <div class="col">
        <div class="card">
          <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
            <h3 style="margin:0;">飞书群聊</h3>
            <button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="loadFeishuChats()">刷新列表</button>
          </div>
          <div style="font-size:12px; color:#6B6B6B; margin-bottom:8px;">
            bot 能访问的群聊（来自 feishu-deal-bot 的 app 凭证）
          </div>
          <div style="max-height:500px; overflow-y:auto;">
            <table>
              <thead><tr><th style="width:40px"></th><th>群名称</th><th style="width:60px">外部</th></tr></thead>
              <tbody id="feishuChatList"><tr><td colspan="3" style="text-align:center; color:#6B6B6B;">点击「刷新列表」加载</td></tr></tbody>
            </table>
          </div>
        </div>

        <div class="actions">
          <button class="btn btn-primary" id="btnFeishuScan" onclick="startFeishuScan()" disabled>开始扫描选中群</button>
          <button class="btn btn-danger" id="btnFeishuCancel" onclick="cancelFeishuScan()" style="display:none;">取消扫描</button>
        </div>

        <div class="card">
          <h3 style="margin-bottom:8px;">扫描说明</h3>
          <div style="font-size:13px; color:#6B6B6B; line-height:1.7; background:#F9F7F4; padding:14px 16px; border-radius:8px; border-left:3px solid #D97706;">
            • 提取群内所有消息中的 <strong>妙记 / 文档 / 维基</strong> 链接和 <strong>PDF 附件</strong><br>
            • 对每个链接/PDF 用 Claude 做 AI 分析：提取公司、类型、总结<br>
            • 已录入过的 URL 会自动跳过（基于飞书多维表格去重）<br>
            • 结果直接写入 Deal Flow 表，无需二次确认
          </div>
        </div>
      </div>

      <div class="col">
        <div class="card">
          <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
            <h3 style="margin:0;">扫描进度</h3>
            <span id="feishuJobLabel" style="font-size:12px; color:#6B6B6B;"></span>
          </div>
          <div id="feishuLog" class="log-box" style="height:480px;">（尚未开始）</div>
        </div>
      </div>
    </div>
  </div><!-- /#feishu-panel -->

  <!-- ============ 知识库 tab (server_mode only) ============ -->
  {% if server_mode %}
  <div id="wiki-panel" class="tab-panel">
    <div class="layout" style="display:block;">
      <!-- 统计概览 -->
      <div class="card" style="margin-bottom:15px;">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <h3 style="margin:0;">知识库概览</h3>
          <button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="loadWikiList()">刷新</button>
        </div>
        <div id="wikiStats" style="display:flex;gap:30px;margin-top:12px;font-size:14px;color:#6B6B6B;">
          <span>公司总数: <strong id="wikiTotalCompanies">--</strong></span>
          <span>总条目: <strong id="wikiTotalEntries">--</strong></span>
          <span>最近更新: <strong id="wikiLastUpdated">--</strong></span>
        </div>
      </div>

      <!-- 搜索 -->
      <div class="card" style="margin-bottom:15px;">
        <div style="display:flex;gap:10px;align-items:center;">
          <input type="text" id="wikiSearchInput" placeholder="搜索公司、赛道、关键词..."
                 style="flex:1;padding:8px 12px;border:1px solid #E8E5E0;border-radius:8px;font-size:14px;"
                 onkeydown="if(event.key==='Enter')searchWiki()">
          <button class="btn-sm" onclick="searchWiki()">搜索</button>
          <button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="clearWikiSearch()">清除</button>
        </div>
      </div>

      <!-- 公司列表 -->
      <div class="card">
        <div style="max-height:500px;overflow-y:auto;">
          <table>
            <thead>
              <tr>
                <th>公司名</th>
                <th style="width:120px;">赛道</th>
                <th style="width:100px;">融资阶段</th>
                <th style="width:60px;">提及</th>
                <th style="width:140px;">最近更新</th>
              </tr>
            </thead>
            <tbody id="wikiCompanyList">
              <tr><td colspan="5" style="text-align:center;color:#6B6B6B;">点击「刷新」加载知识库</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div><!-- /#wiki-panel -->

  <!-- Wiki 详情弹窗 -->
  <div class="modal-backdrop" id="wikiDetailModal" onclick="if(event.target===this)closeWikiDetail()" style="display:none;">
    <div class="modal" style="max-width:800px;max-height:80vh;overflow-y:auto;">
      <h3 id="wikiDetailTitle">公司详情</h3>
      <div id="wikiDetailContent" style="font-size:14px;line-height:1.7;white-space:pre-wrap;font-family:inherit;"></div>
      <div class="modal-close"><button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="closeWikiDetail()">关闭</button></div>
    </div>
  </div>
  {% endif %}

</div>

<div class="modal-backdrop" id="blacklistModal" onclick="if(event.target===this)closeBlacklist()">
  <div class="modal">
    <h3>黑名单管理</h3>
    <div id="blacklistItems">加载中...</div>
    <div class="modal-close"><button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="closeBlacklist()">关闭</button></div>
  </div>
</div>

<script>
let sessions = [];
let filteredIndices = [];  // 当前筛选后可见的 session index
let selected = new Set();
let scanResults = [];
let currentSessionId = null;
let logPollTimer = null;

function filterSessions() {
  const q = document.getElementById('searchBox').value.toLowerCase().trim();
  const groupOnly = document.getElementById('groupOnly').checked;
  filteredIndices = [];
  sessions.forEach((s, i) => {
    const nameMatch = !q || s.display_name.toLowerCase().includes(q) || (s.summary || '').toLowerCase().includes(q);
    const groupMatch = !groupOnly || s.is_group;
    if (nameMatch && groupMatch) filteredIndices.push(i);
  });
  renderSessions();
  document.getElementById('totalCount').textContent = '显示 ' + filteredIndices.length + '/' + sessions.length;
}

async function loadSessions() {
  try {
    const resp = await fetch('/api/sessions');
    const data = await resp.json();
    sessions = data.sessions || [];
    filterSessions();
    setStatus(sessions.length + ' 个会话已加载', '');
    document.getElementById('btnPreview').disabled = false;
    document.getElementById('btnSmart').disabled = false;
  } catch(e) {
    setStatus('加载失败: ' + e.message, 'error');
  }
}

function renderSessions() {
  const tbody = document.getElementById('sessionList');
  if (!filteredIndices.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#6B6B6B;">无匹配会话</td></tr>';
    return;
  }
  tbody.innerHTML = filteredIndices.map(i => {
    const s = sessions[i];
    const cls = selected.has(i) ? 'selected' : '';
    const tag = s.is_group ? '<span class="tag group">群聊</span>' : '<span class="tag private">私聊</span>';
    // 如果名字是 wxid 格式（无群名），显示 summary 辅助识别
    let nameHtml = esc(s.display_name);
    if (s.display_name.includes('@chatroom') && s.summary) {
      nameHtml = '<span style="color:#6B6B6B;font-size:12px;">' + esc(s.display_name) + '</span><br><span style="font-size:12px;color:#1A1A1A;">' + esc(s.summary.substring(0, 50)) + '</span>';
    }
    return `<tr class="${cls}" onclick="toggleRow(${i})">
      <td><input type="checkbox" ${selected.has(i)?'checked':''} onclick="event.stopPropagation();toggleRow(${i})"></td>
      <td>${nameHtml}</td>
      <td>${s.last_msg_time_str || ''}</td>
      <td class="num">${s.msg_count || 0}</td>
      <td>${tag}</td>
    </tr>`;
  }).join('');
}

function toggleRow(i) {
  if (selected.has(i)) selected.delete(i); else selected.add(i);
  renderSessions();
  document.getElementById('selectedCount').textContent = '已选 ' + selected.size + ' 个';
}

function toggleSelectAll() {
  const all = document.getElementById('selectAll').checked;
  if (all) {
    filteredIndices.forEach(i => selected.add(i));
  } else {
    filteredIndices.forEach(i => selected.delete(i));
  }
  renderSessions();
  document.getElementById('selectedCount').textContent = '已选 ' + selected.size + ' 个';
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function setStatus(msg, type) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = 'status ' + (type || '');
}

function addLog(msg) {
  const el = document.getElementById('logBox');
  el.textContent += msg + '\\n';
  el.scrollTop = el.scrollHeight;
}

async function pollLogs() {
  try {
    const resp = await fetch('/api/logs');
    const data = await resp.json();
    if (data.logs && data.logs.length) {
      data.logs.forEach(m => addLog(m));
    }
    if (data.running) {
      logPollTimer = setTimeout(pollLogs, 1000);
    }
  } catch(e) {}
}

async function previewChat() {
  if (!selected.size) { alert('请先选择一个聊天'); return; }
  const idx = [...selected][0];
  const wxid = sessions[idx].wxid;
  addLog('\\n--- 预览: ' + sessions[idx].display_name + ' ---');
  try {
    const resp = await fetch('/api/preview?wxid=' + encodeURIComponent(wxid));
    const data = await resp.json();
    (data.messages || []).forEach(m => {
      addLog('[' + m.time_str + '] ' + (m.sender||'?') + ': ' + (m.content||'').substring(0,120));
    });
    addLog('--- 共 ' + (data.messages||[]).length + ' 条 ---');
  } catch(e) { addLog('预览失败: ' + e.message); }
}

async function refreshDB() {
  const btn = document.getElementById('btnRefresh');
  btn.disabled = true;
  btn.textContent = '解密中...';
  setStatus('正在重新解密并加载数据库（约30秒）...', 'loading');
  addLog('\\n重新解密微信数据库...');
  try {
    const resp = await fetch('/api/refresh', {method: 'POST'});
    const data = await resp.json();
    if (data.ok) {
      sessions = data.sessions || [];
      selected.clear();
      document.getElementById('selectedCount').textContent = '已选 0 个';
      document.getElementById('selectAll').checked = false;
      filterSessions();
      setStatus(sessions.length + ' 个会话已刷新', '');
      addLog('刷新完成: ' + sessions.length + ' 个会话');
    } else {
      setStatus('刷新失败: ' + (data.error || ''), 'error');
      addLog('刷新失败: ' + (data.error || ''));
    }
  } catch(e) {
    setStatus('刷新失败: ' + e.message, 'error');
    addLog('刷新出错: ' + e.message);
  }
  btn.disabled = false;
  btn.textContent = '刷新数据库';
}

async function smartScan() {
  if (!selected.size) { alert('请先选择要扫描的聊天'); return; }

  const wxids = [...selected].map(i => ({wxid: sessions[i].wxid, name: sessions[i].display_name}));
  setStatus('正在智能扫描 ' + wxids.length + ' 个聊天...', 'loading');
  document.getElementById('btnSmart').disabled = true;
  addLog('\\n开始智能扫描 ' + wxids.length + ' 个聊天...');

  // Start polling logs
  logPollTimer = setTimeout(pollLogs, 1000);

  try {
    const resp = await fetch('/api/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: 'smart', chats: wxids}),
    });
    const data = await resp.json();
    currentSessionId = data.session_id || null;
    scanResults = data.results || [];

    // Final log poll
    if (logPollTimer) clearTimeout(logPollTimer);
    await pollLogs();

    if (scanResults.length) {
      showResults(scanResults);
      setStatus('扫描完成! 发现 ' + scanResults.length + ' 个项目', '');
    } else {
      setStatus('扫描完成，未发现项目相关内容', '');
    }
    loadHistory();
  } catch(e) {
    setStatus('扫描失败: ' + e.message, 'error');
    addLog('扫描出错: ' + e.message);
  }
  document.getElementById('btnSmart').disabled = false;
}

function _findItem(resultId) {
  const idx = scanResults.findIndex(x => x.result_id === resultId);
  return { idx, item: idx >= 0 ? scanResults[idx] : null };
}

function _elForId(resultId) {
  return document.querySelector(`.result-item[data-id="${resultId}"]`);
}

function showResults(results) {
  const card = document.getElementById('resultsCard');
  const list = document.getElementById('resultsList');
  const countEl = document.getElementById('resultsCount');
  if (!results.length) {
    card.style.display = 'none';
    list.innerHTML = '';
    if (countEl) countEl.textContent = '';
    return;
  }
  card.style.display = 'block';
  if (countEl) countEl.textContent = results.length + ' 条';
  list.innerHTML = results.map((item, i) => renderCard(item, i)).join('');
}

function renderCard(item, i) {
  const r = item.result || {};
  const rid = item.result_id || '';
  const rtBadge = r.record_type === '被投跟踪'
    ? '<span style="background:#FEF3C7;color:#92400E;padding:2px 8px;border-radius:10px;font-size:12px;">被投跟踪</span>'
    : '<span style="background:#ECFDF5;color:#166534;padding:2px 8px;border-radius:10px;font-size:12px;">新项目</span>';
  const conf = r.confidence || 'low';
  const confColor = conf === 'high' ? '#16A34A' : (conf === 'medium' ? '#D97706' : '#8E8E93');
  const summary = r.summary || '';
  const longSummary = summary.length > 120;
  return `<div class="result-item" data-id="${rid}">
    <div class="item-actions">
      <button class="btn-sm btn-success" onclick="writeOne('${rid}')" title="写入飞书">入库</button>
      <button class="btn-sm" style="background:#FFFBEB;color:#92400E;" onclick="toggleEdit('${rid}')" title="编辑字段">编辑</button>
      <button class="btn-sm btn-danger" onclick="dismissOne('${rid}')" title="忽略并加入黑名单">忽略</button>
    </div>
    <h4>${i+1}. <span class="fname">${esc(r.company_name || '未知')}</span> ${rtBadge}</h4>
    <div class="meta">
      来源: ${esc(item.chat||'')} ·
      类型: <span class="fmt">${esc(r.meeting_type||'')}</span> ·
      赛道: <span class="find">${esc(r.industry||'')}</span> ·
      阶段: <span class="fstage">${esc(r.funding_stage||'')}</span> ·
      <span style="color:${confColor};font-weight:500;">● ${conf}</span>
    </div>
    <div class="summary-preview ${longSummary?'collapsed':''}" onclick="this.classList.toggle('expanded');this.classList.toggle('collapsed')">${esc(summary)}</div>
    ${longSummary ? '<span class="summary-toggle" onclick="this.previousElementSibling.classList.toggle(\\'expanded\\');this.previousElementSibling.classList.toggle(\\'collapsed\\')">展开/收起</span>' : ''}
  </div>`;
}

function toggleEdit(rid) {
  const el = _elForId(rid);
  const { item } = _findItem(rid);
  if (!el || !item) return;
  if (el.classList.contains('editing')) {
    // 保存
    saveEdit(rid);
    return;
  }
  const r = item.result || {};
  el.classList.add('editing');
  el.innerHTML = `
    <div class="item-actions">
      <button class="btn-sm btn-success" onclick="saveEdit('${rid}')">保存</button>
      <button class="btn-sm" style="background:#F5F0EB;color:#1A1A1A;" onclick="cancelEdit('${rid}')">取消</button>
    </div>
    <h4 style="padding-right:140px;">编辑项目</h4>
    <div class="field-row"><label>公司名</label><input class="edit-input" data-field="company_name" value="${esc(r.company_name||'')}"></div>
    <div class="field-row"><label>类型</label>
      <select class="edit-input" data-field="record_type">
        <option value="新项目" ${r.record_type==='新项目'?'selected':''}>新项目</option>
        <option value="被投跟踪" ${r.record_type==='被投跟踪'?'selected':''}>被投跟踪</option>
      </select>
    </div>
    <div class="field-row"><label>赛道</label><input class="edit-input" data-field="industry" value="${esc(r.industry||'')}"></div>
    <div class="field-row"><label>阶段</label><input class="edit-input" data-field="funding_stage" value="${esc(r.funding_stage||'')}"></div>
    <div class="field-row"><label>创始人</label><input class="edit-input" data-field="founder_name" value="${esc(r.founder_name||'')}"></div>
    <div class="field-row"><label>会议</label><input class="edit-input" data-field="meeting_type" value="${esc(r.meeting_type||'')}"></div>
    <div class="field-row"><label>置信度</label>
      <select class="edit-input" data-field="confidence">
        <option value="low" ${r.confidence==='low'?'selected':''}>low</option>
        <option value="medium" ${r.confidence==='medium'?'selected':''}>medium</option>
        <option value="high" ${r.confidence==='high'?'selected':''}>high</option>
      </select>
    </div>
    <textarea class="summary-edit" data-field="summary" placeholder="摘要">${esc(r.summary||'')}</textarea>
  `;
}

function cancelEdit(rid) {
  const { idx, item } = _findItem(rid);
  if (idx < 0) return;
  const el = _elForId(rid);
  el.classList.remove('editing');
  el.outerHTML = renderCard(item, idx);
}

async function saveEdit(rid) {
  const el = _elForId(rid);
  if (!el) return;
  const fields = {};
  el.querySelectorAll('[data-field]').forEach(n => {
    fields[n.dataset.field] = n.value;
  });
  try {
    const resp = await fetch('/api/edit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({result_id: rid, fields}),
    });
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'failed');
    // 更新本地数据
    const { idx } = _findItem(rid);
    if (idx >= 0) {
      scanResults[idx].result = data.result.data;
    }
    showResults(scanResults);
    setStatus('已保存编辑: ' + (fields.company_name || ''), '');
    addLog('✏️ 编辑: ' + (fields.company_name || rid));
  } catch(e) {
    alert('保存失败: ' + e.message);
  }
}

async function writeOne(rid) {
  const { idx, item } = _findItem(rid);
  if (!item) return;
  const el = _elForId(rid);
  if (!el) return;
  el.classList.add('writing');
  el.querySelectorAll('.btn-sm').forEach(b => b.disabled = true);
  setStatus('正在写入飞书...', 'loading');
  try {
    const resp = await fetch('/api/write', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({result_ids: [rid]}),
    });
    const data = await resp.json();
    const res = (data.items || [])[0] || {};
    if (res.ok) {
      el.classList.add('written');
      addLog('✅ 已写入: ' + (res.company_name || ''));
      setTimeout(() => {
        scanResults.splice(idx, 1);
        if (scanResults.length === 0) {
          showResults([]);
          setStatus('全部结果已处理', '');
        } else {
          showResults(scanResults);
          setStatus('写入成功，剩余 ' + scanResults.length + ' 条', '');
        }
        loadHistory();
      }, 420);
    } else {
      el.classList.remove('writing');
      el.classList.add('status-failed');
      el.querySelectorAll('.btn-sm').forEach(b => b.disabled = false);
      setStatus('写入失败: ' + (res.error || ''), 'error');
      addLog('❌ 写入失败: ' + (res.company_name || '') + ' - ' + (res.error || ''));
    }
  } catch(e) {
    el.classList.remove('writing');
    el.querySelectorAll('.btn-sm').forEach(b => b.disabled = false);
    setStatus('写入失败: ' + e.message, 'error');
  }
}

async function dismissOne(rid) {
  const { idx, item } = _findItem(rid);
  if (!item) return;
  const cname = (item.result || {}).company_name || '';
  if (!confirm('忽略 "' + cname + '" 并加入黑名单？\\n（下次扫描自动跳过同名项目）')) return;
  const el = _elForId(rid);
  el.classList.add('writing');
  try {
    const resp = await fetch('/api/dismiss', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({result_id: rid, blacklist: true}),
    });
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'failed');
    el.classList.add('written');
    addLog('🚫 已忽略: ' + cname);
    setTimeout(() => {
      scanResults.splice(idx, 1);
      if (scanResults.length === 0) {
        showResults([]);
        setStatus('全部结果已处理', '');
      } else {
        showResults(scanResults);
        setStatus('已忽略，剩余 ' + scanResults.length + ' 条', '');
      }
    }, 420);
  } catch(e) {
    el.classList.remove('writing');
    alert('忽略失败: ' + e.message);
  }
}

async function writeAll() {
  if (!scanResults.length) return;
  setStatus('正在批量写入飞书...', 'loading');
  document.querySelectorAll('.result-item').forEach(el => {
    el.classList.add('writing');
    el.querySelectorAll('.btn-sm').forEach(b => b.disabled = true);
  });
  const ids = scanResults.map(x => x.result_id).filter(Boolean);
  try {
    const resp = await fetch('/api/write', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({result_ids: ids}),
    });
    const data = await resp.json();
    addLog('批量写入完成: ' + data.success + '/' + data.total);
    // 按 per-item 结果处理
    const okIds = new Set((data.items || []).filter(x => x.ok).map(x => x.result_id));
    const failItems = (data.items || []).filter(x => !x.ok);

    // 成功的淡出
    okIds.forEach(rid => {
      const el = _elForId(rid);
      if (el) el.classList.add('written');
    });
    // 失败的保留 + 标红
    failItems.forEach(fi => {
      const el = _elForId(fi.result_id);
      if (el) {
        el.classList.remove('writing');
        el.classList.add('status-failed');
        el.querySelectorAll('.btn-sm').forEach(b => b.disabled = false);
        el.title = '写入失败: ' + (fi.error || '');
      }
      addLog('❌ ' + (fi.company_name || '') + ': ' + (fi.error || ''));
    });

    setTimeout(() => {
      scanResults = scanResults.filter(x => !okIds.has(x.result_id));
      showResults(scanResults);
      if (scanResults.length === 0) {
        setStatus('全部写入完成: ' + data.success + '/' + data.total, '');
      } else {
        setStatus('写入: ' + data.success + '/' + data.total + ' 成功，' + scanResults.length + ' 条待处理', data.success === data.total ? '' : 'error');
      }
      loadHistory();
    }, 420);
  } catch(e) {
    document.querySelectorAll('.result-item').forEach(el => {
      el.classList.remove('writing');
      el.querySelectorAll('.btn-sm').forEach(b => b.disabled = false);
    });
    setStatus('写入失败: ' + e.message, 'error');
  }
}

async function saveLocal() {
  if (!scanResults.length) return;
  try {
    const ids = scanResults.map(x => x.result_id).filter(Boolean);
    const resp = await fetch('/api/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({result_ids: ids}),
    });
    const data = await resp.json();
    alert('已保存到: ' + data.path + '\\n(' + data.count + ' 条)');
  } catch(e) { alert('保存失败: ' + e.message); }
}

// ----- 历史 -----
async function loadHistory() {
  try {
    const resp = await fetch('/api/history');
    const data = await resp.json();
    const box = document.getElementById('historyList');
    const sessions = data.sessions || [];
    if (!sessions.length) {
      box.innerHTML = '<div class="empty-results">暂无历史</div>';
    } else {
      box.innerHTML = sessions.map(s => {
        const dt = new Date(s.created_at).toLocaleString('zh-CN', {hour12: false});
        const chats = (s.chats || []).map(c => c.name).join(', ').substring(0, 40);
        const active = s.session_id === currentSessionId ? 'background:#FEF3C7;border-color:#D97706;' : '';
        return `<div class="history-item" style="${active}" onclick="loadSession('${s.session_id}')">
          <div class="h-title">${esc(chats || s.mode)}</div>
          <div class="h-meta">
            <span>${dt}</span>
            <span>${s.mode}</span>
            ${s.pending_count ? `<span class="h-badge pending">待处理 ${s.pending_count}</span>` : ''}
            ${s.written_count ? `<span class="h-badge written">已入库 ${s.written_count}</span>` : ''}
            ${s.dismissed_count ? `<span class="h-badge dismissed">忽略 ${s.dismissed_count}</span>` : ''}
          </div>
        </div>`;
      }).join('');
    }
    const stats = data.pdf_stats || {};
    document.getElementById('pdfStats').textContent =
      'PDF 缓存: ' + (stats.total || 0) + ' 份 (BP ' + (stats.bp || 0) + ')';
  } catch(e) {
    addLog('加载历史失败: ' + e.message);
  }
}

async function loadSession(sid) {
  try {
    const resp = await fetch('/api/results/' + sid + '?status=pending');
    const data = await resp.json();
    currentSessionId = sid;
    scanResults = data.results || [];
    if (scanResults.length === 0) {
      showResults([]);
      setStatus('该历史 session 无待处理项', '');
    } else {
      showResults(scanResults);
      setStatus('已载入历史 session，' + scanResults.length + ' 条待处理', '');
    }
    loadHistory();
  } catch(e) { alert('载入失败: ' + e.message); }
}

// ----- 黑名单 -----
async function showBlacklist() {
  const modal = document.getElementById('blacklistModal');
  modal.classList.add('show');
  const box = document.getElementById('blacklistItems');
  box.innerHTML = '加载中...';
  try {
    const resp = await fetch('/api/blacklist');
    const data = await resp.json();
    const items = data.items || [];
    if (!items.length) {
      box.innerHTML = '<div class="empty-results">黑名单为空</div>';
      return;
    }
    box.innerHTML = items.map(it => {
      const dt = new Date(it.created_at).toLocaleString('zh-CN', {hour12: false});
      const typeTag = it.key_type === 'name' ? '名称' : 'PDF';
      return `<div class="bl-item">
        <div>
          <span style="background:#F5F0EB;padding:1px 6px;border-radius:8px;font-size:11px;">${typeTag}</span>
          <strong>${esc(it.label || it.key_value)}</strong>
          <div style="font-size:11px;color:#6B6B6B;">${dt} · ${esc(it.reason||'')}</div>
        </div>
        <button class="remove-btn" onclick="removeBlacklist('${it.key_type}','${esc(it.key_value)}')">移除</button>
      </div>`;
    }).join('');
  } catch(e) {
    box.innerHTML = '加载失败: ' + e.message;
  }
}

function closeBlacklist() {
  document.getElementById('blacklistModal').classList.remove('show');
}

async function removeBlacklist(type, value) {
  try {
    await fetch('/api/blacklist/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key_type: type, key_value: value}),
    });
    showBlacklist();
  } catch(e) { alert('移除失败: ' + e.message); }
}

// ============ Tab 切换 ============
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('tab-active', t.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.classList.toggle('tab-panel-active', p.id === name + '-panel');
  });
  if (name === 'feishu' && feishuChats.length === 0) {
    loadFeishuChats();
  }
  if (name === 'wiki' && !wikiLoaded) {
    loadWikiList();
  }
}

// ============ 知识库 ============
let wikiLoaded = false;
let wikiCompanies = [];

async function loadWikiList() {
  try {
    const res = await fetch('/api/wiki/list');
    const data = await res.json();
    wikiCompanies = data.companies || [];
    wikiLoaded = true;

    // 更新统计
    const stats = data.stats || {};
    document.getElementById('wikiTotalCompanies').textContent = stats.total_companies || 0;
    document.getElementById('wikiTotalEntries').textContent = stats.total_entries || 0;
    document.getElementById('wikiLastUpdated').textContent = stats.last_updated || '--';

    renderWikiList(wikiCompanies);
  } catch(e) {
    console.error('加载知识库失败:', e);
  }
}

function renderWikiList(companies) {
  const tbody = document.getElementById('wikiCompanyList');
  if (!companies || companies.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#6B6B6B;">暂无数据</td></tr>';
    return;
  }
  tbody.innerHTML = companies.map(c => `
    <tr style="cursor:pointer;" onclick="viewWikiCompany('${c.company.replace(/'/g, "\\'")}')">
      <td style="font-weight:500;color:#D97706;">${c.company}</td>
      <td>${c.industry || '--'}</td>
      <td>${c.funding || '--'}</td>
      <td>${c.mentions || 0}</td>
      <td style="font-size:12px;color:#6B6B6B;">${c.updated || '--'}</td>
    </tr>
  `).join('');
}

async function viewWikiCompany(name) {
  try {
    const res = await fetch('/api/wiki/company?name=' + encodeURIComponent(name));
    const data = await res.json();
    if (data.content) {
      document.getElementById('wikiDetailTitle').textContent = name;
      // 简单 markdown 渲染
      document.getElementById('wikiDetailContent').innerHTML = simpleMarkdown(data.content);
      document.getElementById('wikiDetailModal').style.display = 'flex';
    } else {
      alert('未找到该公司的知识库页面');
    }
  } catch(e) {
    alert('加载失败: ' + e.message);
  }
}

function closeWikiDetail() {
  document.getElementById('wikiDetailModal').style.display = 'none';
}

async function searchWiki() {
  const q = document.getElementById('wikiSearchInput').value.trim();
  if (!q) { renderWikiList(wikiCompanies); return; }
  try {
    const res = await fetch('/api/wiki/search?q=' + encodeURIComponent(q));
    const data = await res.json();
    const results = data.results || [];
    if (results.length === 0) {
      document.getElementById('wikiCompanyList').innerHTML =
        '<tr><td colspan="5" style="text-align:center;color:#6B6B6B;">未找到匹配结果</td></tr>';
      return;
    }
    // 将搜索结果映射为列表格式
    const mapped = results.map(r => ({
      company: r.company,
      industry: '',
      funding: '',
      mentions: r.matches ? r.matches.length : 0,
      updated: '',
    }));
    renderWikiList(mapped);
  } catch(e) {
    alert('搜索失败: ' + e.message);
  }
}

function clearWikiSearch() {
  document.getElementById('wikiSearchInput').value = '';
  renderWikiList(wikiCompanies);
}

function simpleMarkdown(text) {
  // 基础 markdown → HTML 转换
  let html = text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/^### (.+)$/gm, '<h4 style="margin:16px 0 8px;color:#1A1A1A;">$1</h4>')
    .replace(/^## (.+)$/gm, '<h3 style="margin:20px 0 10px;color:#1A1A1A;border-bottom:1px solid #E8E5E0;padding-bottom:6px;">$1</h3>')
    .replace(/^# (.+)$/gm, '<h2 style="margin:0 0 12px;color:#1A1A1A;">$1</h2>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^- (.+)$/gm, '<div style="padding-left:16px;">&#8226; $1</div>')
    .replace(/^---$/gm, '<hr style="border:none;border-top:1px solid #E8E5E0;margin:16px 0;">')
    .replace(/\\n\\n/g, '<br><br>')
    .replace(/\\n/g, '<br>');
  return html;
}

// ============ 飞书扫描 ============
let feishuChats = [];
let selectedFeishuChatId = null;
let currentFeishuJob = null;
let feishuPollTimer = null;
let feishuLogsSeen = 0;

async function loadFeishuChats() {
  const status = document.getElementById('feishu-status');
  status.textContent = '正在获取群聊列表...';
  status.className = 'status loading';
  const list = document.getElementById('feishuChatList');
  list.innerHTML = '<tr><td colspan="3" style="text-align:center; color:#6B6B6B;">加载中...</td></tr>';
  try {
    const resp = await fetch('/api/feishu_chats');
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || '未知错误');
    feishuChats = data.chats || [];
    renderFeishuChats();
    status.textContent = '就绪：共 ' + feishuChats.length + ' 个群聊';
    status.className = 'status';
  } catch (e) {
    status.textContent = '加载失败: ' + e.message;
    status.className = 'status error';
    list.innerHTML = '<tr><td colspan="3" style="text-align:center; color:#DC2626;">加载失败</td></tr>';
  }
}

function renderFeishuChats() {
  const tbody = document.getElementById('feishuChatList');
  if (feishuChats.length === 0) {
    tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; color:#6B6B6B;">暂无可访问的群聊</td></tr>';
    return;
  }
  tbody.innerHTML = feishuChats.map((c, i) => `
    <tr data-chat-id="${c.chat_id}" onclick="selectFeishuChat('${c.chat_id}')">
      <td><input type="radio" name="feishuChat" ${selectedFeishuChatId === c.chat_id ? 'checked' : ''} onclick="event.stopPropagation(); selectFeishuChat('${c.chat_id}')"></td>
      <td>${escapeHtml(c.name)}${c.description ? '<div style="font-size:12px;color:#6B6B6B;">' + escapeHtml(c.description.slice(0, 60)) + '</div>' : ''}</td>
      <td>${c.external ? '<span class="tag" style="background:#fff3cd;color:#856404;">外部</span>' : ''}</td>
    </tr>
  `).join('');
  Array.from(tbody.querySelectorAll('tr')).forEach(tr => {
    tr.classList.toggle('selected', tr.dataset.chatId === selectedFeishuChatId);
  });
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function selectFeishuChat(chatId) {
  selectedFeishuChatId = chatId;
  renderFeishuChats();
  document.getElementById('btnFeishuScan').disabled = !chatId || !!currentFeishuJob;
}

async function startFeishuScan() {
  if (!selectedFeishuChatId) return;
  const chat = feishuChats.find(c => c.chat_id === selectedFeishuChatId);
  if (!chat) return;

  const logBox = document.getElementById('feishuLog');
  logBox.textContent = '正在启动扫描...\\n';
  feishuLogsSeen = 0;

  const status = document.getElementById('feishu-status');
  status.textContent = '启动中...';
  status.className = 'status loading';

  try {
    const resp = await fetch('/api/feishu_scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({chat_id: chat.chat_id, chat_name: chat.name}),
    });
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || '启动失败');
    currentFeishuJob = data.job_id;
    document.getElementById('feishuJobLabel').textContent = '任务 ' + data.job_id + ' | ' + chat.name;
    document.getElementById('btnFeishuScan').disabled = true;
    document.getElementById('btnFeishuCancel').style.display = '';
    status.textContent = '扫描进行中...';
    status.className = 'status';
    // 开始轮询
    pollFeishuJob();
  } catch (e) {
    status.textContent = '启动失败: ' + e.message;
    status.className = 'status error';
    logBox.textContent += '❌ ' + e.message + '\\n';
  }
}

async function pollFeishuJob() {
  if (!currentFeishuJob) return;
  try {
    const resp = await fetch('/api/feishu_scan/' + currentFeishuJob + '?since=' + feishuLogsSeen);
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'poll failed');

    const logBox = document.getElementById('feishuLog');
    if (data.new_logs && data.new_logs.length > 0) {
      if (feishuLogsSeen === 0) logBox.textContent = '';
      for (const line of data.new_logs) {
        const t = new Date(line.ts * 1000).toLocaleTimeString();
        logBox.textContent += '[' + t + '] ' + line.msg + '\\n';
      }
      feishuLogsSeen = data.total_logs;
      logBox.scrollTop = logBox.scrollHeight;
    }

    const status = document.getElementById('feishu-status');
    if (data.status === 'completed') {
      status.textContent = '✅ 扫描完成';
      status.className = 'status';
      cleanupFeishuJob();
    } else if (data.status === 'failed') {
      status.textContent = '❌ 扫描失败: ' + (data.error || '');
      status.className = 'status error';
      cleanupFeishuJob();
    } else {
      feishuPollTimer = setTimeout(pollFeishuJob, 1500);
    }
  } catch (e) {
    console.error('poll error', e);
    feishuPollTimer = setTimeout(pollFeishuJob, 3000);
  }
}

function cleanupFeishuJob() {
  currentFeishuJob = null;
  if (feishuPollTimer) { clearTimeout(feishuPollTimer); feishuPollTimer = null; }
  document.getElementById('btnFeishuScan').disabled = !selectedFeishuChatId;
  document.getElementById('btnFeishuCancel').style.display = 'none';
}

async function cancelFeishuScan() {
  if (!currentFeishuJob) return;
  try {
    await fetch('/api/feishu_scan/' + currentFeishuJob + '/cancel', {method: 'POST'});
  } catch (e) {
    console.error('cancel error', e);
  }
}

// 页面加载
// 手机端 + 服务器模式：微信远程 tab 被隐藏，确保飞书 tab 激活
{% if server_mode %}
if (window.innerWidth <= 600) {
  switchTab('feishu');
}
{% endif %}
loadSessions();
loadHistory();

// ===== 微信解锁向导 =====
{% if not server_mode %}
(function() {
  const wizardEl = document.getElementById('wechat-setup-wizard');
  const scannerEl = document.getElementById('wechat-scanner-ui');
  if (!wizardEl || !scannerEl) return;

  const wechatReady = {{ 'true' if wechat_ready else 'false' }};
  if (wechatReady) {
    wizardEl.style.display = 'none';
    scannerEl.style.display = '';
  } else {
    wizardEl.style.display = '';
    scannerEl.style.display = 'none';
    checkSetupStatus();
  }
})();

function checkSetupStatus() {
  fetch('/api/wechat_setup/status')
    .then(r => r.json())
    .then(s => {
      // Step 1: SIP
      setBadge('sip', s.sip_disabled ? 'ok' : 'warn',
               s.sip_disabled ? '已关闭' : '未关闭');
      setStepClass('step-sip', s.sip_disabled ? 'step-done' : 'step-active');

      // Step 2: WeChat running
      setBadge('wechat', s.wechat_running ? 'ok' : 'warn',
               s.wechat_running ? '已运行' : '未运行');
      setStepClass('step-wechat', s.wechat_running ? 'step-done' : (s.sip_disabled ? 'step-active' : ''));

      // Step 3: Keys
      const keysLabel = s.keys_exist
        ? (s.keys_age_hours < 24 ? '已就绪' : `已就绪 (${Math.round(s.keys_age_hours/24)}天前)`)
        : '需要提取';
      setBadge('keys', s.keys_exist ? 'ok' : 'action', keysLabel);
      setStepClass('step-keys', s.keys_exist ? 'step-done' : (s.sip_disabled && s.wechat_running ? 'step-active' : ''));
      document.getElementById('extract-cmd').textContent = s.extract_command || '';

      // Step 4: Decrypt
      const canDecrypt = s.keys_exist;
      setBadge('decrypt', s.has_decrypted ? 'ok' : (canDecrypt ? 'action' : 'pending'),
               s.has_decrypted ? `${s.db_count} 个库` : (canDecrypt ? '可解密' : '等待密钥'));
      setStepClass('step-decrypt', s.has_decrypted ? 'step-done' : (canDecrypt ? 'step-active' : ''));
      document.getElementById('btn-decrypt').disabled = !canDecrypt;

      // Step 5: Done
      const canReload = s.has_decrypted;
      setBadge('done', s.reader_loaded ? 'ok' : (canReload ? 'action' : 'pending'),
               s.reader_loaded ? '已加载' : (canReload ? '可加载' : '等待解密'));
      setStepClass('step-done', s.reader_loaded ? 'step-done' : (canReload ? 'step-active' : ''));
      document.getElementById('btn-reload').disabled = !canReload;

      // 如果已经全部完成，自动切换到扫描器
      if (s.reader_loaded) {
        setTimeout(() => {
          document.getElementById('wechat-setup-wizard').style.display = 'none';
          document.getElementById('wechat-scanner-ui').style.display = '';
          loadSessions();
        }, 500);
      }
    })
    .catch(e => console.error('setup status error:', e));
}

function setBadge(name, type, text) {
  const el = document.getElementById('badge-' + name);
  if (!el) return;
  el.textContent = text;
  el.className = 'step-badge badge-' + type;
}

function setStepClass(id, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('step-done', 'step-active');
  if (cls) el.classList.add(cls);
}

function copyCmd() {
  const cmd = document.getElementById('extract-cmd').textContent;
  navigator.clipboard.writeText(cmd).then(() => {
    const btn = event.target;
    btn.textContent = '已复制';
    setTimeout(() => btn.textContent = '复制', 2000);
  });
}

function runDecrypt() {
  const btn = document.getElementById('btn-decrypt');
  const prog = document.getElementById('decrypt-progress');
  btn.disabled = true;
  btn.textContent = '⏳ 解密中...';
  prog.style.display = '';
  setBadge('decrypt', 'running', '解密中...');

  fetch('/api/wechat_setup/decrypt', {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      prog.style.display = 'none';
      if (data.ok) {
        btn.textContent = '✅ 解密完成';
        setBadge('decrypt', 'ok', `${data.session_count} 个会话`);
        setStepClass('step-decrypt', 'step-done');
        // reader 也已加载了（refresh_db 内部调了 init_reader）
        setBadge('done', 'ok', '已加载');
        setStepClass('step-done', 'step-done');
        document.getElementById('btn-reload').disabled = false;
        document.getElementById('btn-reload').textContent = '🚀 进入微信扫描';
      } else {
        btn.textContent = '🔐 重试解密';
        btn.disabled = false;
        setBadge('decrypt', 'warn', '失败');
        alert('解密失败: ' + (data.error || '未知错误'));
      }
    })
    .catch(e => {
      prog.style.display = 'none';
      btn.textContent = '🔐 重试解密';
      btn.disabled = false;
      alert('请求失败: ' + e.message);
    });
}

function reloadAndSwitch() {
  const btn = document.getElementById('btn-reload');
  btn.disabled = true;
  btn.textContent = '⏳ 加载中...';

  fetch('/api/wechat_setup/reload', {method: 'POST'})
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        document.getElementById('wechat-setup-wizard').style.display = 'none';
        document.getElementById('wechat-scanner-ui').style.display = '';
        loadSessions();
      } else {
        btn.textContent = '🚀 重试加载';
        btn.disabled = false;
        alert('加载失败: ' + (data.error || '未知错误'));
      }
    })
    .catch(e => {
      btn.textContent = '🚀 重试加载';
      btn.disabled = false;
      alert('请求失败: ' + e.message);
    });
}
{% endif %}

// ===== 服务器模式：检测本地微信扫描服务 =====
{% if server_mode %}
let _wxPollTimer = null;

function detectLocal() {
  const c = new AbortController();
  setTimeout(() => c.abort(), 2000);
  return fetch('http://127.0.0.1:5678/health', {mode:'cors', signal:c.signal})
    .then(r => r.json())
    .then(d => d.ok === true)
    .catch(() => false);
}

function showWxState(state) {
  const ids = ['wx-state-checking','wx-state-offline','wx-state-copied'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = (id === 'wx-state-' + state) ? '' : 'none';
  });
  // 核心：online 时隐藏整个安装引导卡片，显示操作面板
  const setupCard = document.getElementById('wx-setup-card');
  const embeddedUI = document.getElementById('wx-embedded-ui');
  if (state === 'online') {
    if (setupCard) setupCard.style.display = 'none';
    if (embeddedUI) embeddedUI.style.display = '';
  } else {
    if (setupCard) setupCard.style.display = '';
    if (embeddedUI) embeddedUI.style.display = 'none';
  }
}

const _wxCmd = 'curl -sL https://scanner.helioratech.com/api/wechat_script | bash';

function launchWx() {
  // 1) 复制命令到剪贴板
  navigator.clipboard.writeText(_wxCmd).then(() => {
    // 2) 切换到「已复制」状态
    showWxState('copied');
    // 3) 开始轮询
    startPolling();
  }).catch(() => {
    // fallback: 选中文本让用户手动复制
    prompt('请复制以下命令到终端运行：', _wxCmd);
    showWxState('copied');
    startPolling();
  });
}

function reCopy() {
  navigator.clipboard.writeText(_wxCmd).then(() => {
    const btn = document.getElementById('wx-recopy');
    btn.textContent = '已复制 ✓';
    btn.style.background = '#16A34A';
    setTimeout(() => { btn.textContent = '再复制'; btn.style.background = '#333'; }, 1500);
  });
}

function startPolling() {
  if (_wxPollTimer) clearInterval(_wxPollTimer);
  _wxPollTimer = setInterval(async () => {
    if (await detectLocal()) {
      clearInterval(_wxPollTimer);
      showWxState('online');
      wxLoadSessions();
    }
  }, 3000);
}

async function initWxTab() {
  showWxState('checking');
  if (await detectLocal()) {
    showWxState('online');
    wxLoadSessions();
  } else {
    showWxState('offline');
    // 后台持续轮询，一旦本地服务启动就自动切换到操作面板
    startPolling();
  }
}

// 切换到微信 tab 时检测
const _origSwitchTab = switchTab;
switchTab = function(name) {
  _origSwitchTab(name);
  if (name === 'wechat-remote') initWxTab();
};

// 页面加载时：检查 URL 参数，自动切换到微信 tab
(function() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('tab') === 'wechat') {
    // 清除 URL 参数（不刷新）
    history.replaceState(null, '', window.location.pathname);
    // 自动切换到微信 tab
    switchTab('wechat-remote');
  }
})();

// ===== 内嵌本地微信扫描器 =====
const WX_LOCAL = 'http://127.0.0.1:5678';
let wxSessions = [];
let wxFiltered = [];
let wxSelected = new Set();
let wxScanResults = [];
let wxLogPollTimer = null;

async function wxLoadSessions() {
  const st = document.getElementById('wx-remote-status');
  try {
    const resp = await fetch(WX_LOCAL + '/api/sessions');
    const data = await resp.json();
    wxSessions = data.sessions || [];
    wxFilterSessions();
    st.textContent = wxSessions.length + ' 个微信会话已加载';
    st.className = 'status';
    document.getElementById('wxBtnScan').disabled = false;
    document.getElementById('wxBtnPreview').disabled = false;
  } catch(e) {
    st.textContent = '加载失败: ' + e.message;
    st.className = 'status error';
  }
}

function wxFilterSessions() {
  const q = (document.getElementById('wxRemoteSearch').value || '').toLowerCase();
  const groupOnly = document.getElementById('wxRemoteGroupOnly').checked;
  wxFiltered = [];
  wxSessions.forEach((s, i) => {
    const nm = !q || (s.display_name||'').toLowerCase().includes(q);
    const gm = !groupOnly || s.is_group;
    if (nm && gm) wxFiltered.push(i);
  });
  wxRenderSessions();
  document.getElementById('wxRemoteTotalCount').textContent = '显示 ' + wxFiltered.length + '/' + wxSessions.length;
}

function wxRenderSessions() {
  const tbody = document.getElementById('wxRemoteSessionList');
  if (!wxFiltered.length) { tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#6B6B6B;">无匹配</td></tr>'; return; }
  tbody.innerHTML = wxFiltered.map(i => {
    const s = wxSessions[i];
    const cls = wxSelected.has(i) ? 'selected' : '';
    const tag = s.is_group ? '<span class="tag group">群聊</span>' : '<span class="tag private">私聊</span>';
    return '<tr class="'+cls+'" onclick="wxToggleRow('+i+')"><td><input type="checkbox" '+(wxSelected.has(i)?'checked':'')+' onclick="event.stopPropagation();wxToggleRow('+i+')"></td><td>'+esc(s.display_name)+'</td><td>'+(s.last_msg_time_str||'')+'</td><td class="num">'+(s.msg_count||0)+'</td><td>'+tag+'</td></tr>';
  }).join('');
}

function wxToggleRow(i) {
  if (wxSelected.has(i)) wxSelected.delete(i); else wxSelected.add(i);
  wxRenderSessions();
  document.getElementById('wxRemoteSelectedCount').textContent = '已选 ' + wxSelected.size + ' 个';
}

function wxToggleSelectAll() {
  const all = document.getElementById('wxRemoteSelectAll').checked;
  wxFiltered.forEach(i => all ? wxSelected.add(i) : wxSelected.delete(i));
  wxRenderSessions();
  document.getElementById('wxRemoteSelectedCount').textContent = '已选 ' + wxSelected.size + ' 个';
}

// ===== 消息预览 =====
let wxPreviewOffset = 0;
let wxPreviewWxid = '';

async function wxPreviewChat() {
  if (!wxSelected.size) { alert('请先选择一个会话'); return; }
  // 取第一个选中的会话
  const idx = [...wxSelected][0];
  const s = wxSessions[idx];
  wxPreviewWxid = s.wxid;
  wxPreviewOffset = 0;

  const card = document.getElementById('wxPreviewCard');
  const title = document.getElementById('wxPreviewTitle');
  const list = document.getElementById('wxPreviewMessages');

  card.style.display = '';
  title.textContent = s.display_name;
  list.innerHTML = '<div style="text-align:center;color:#6B6B6B;padding:20px;">加载中...</div>';

  try {
    const resp = await fetch(WX_LOCAL + '/api/preview?wxid=' + encodeURIComponent(wxPreviewWxid) + '&limit=50&offset=0');
    const data = await resp.json();
    const msgs = data.messages || [];
    wxPreviewOffset = 50;
    list.innerHTML = msgs.length ? msgs.map(m => wxFmtMsg(m)).join('') : '<div style="text-align:center;color:#6B6B6B;padding:20px;">无消息</div>';
    list.scrollTop = list.scrollHeight;
  } catch(e) {
    list.innerHTML = '<div style="color:#DC2626;padding:10px;">加载失败: ' + e.message + '</div>';
  }
}

async function wxPreviewMore() {
  if (!wxPreviewWxid) return;
  const list = document.getElementById('wxPreviewMessages');
  try {
    const resp = await fetch(WX_LOCAL + '/api/preview?wxid=' + encodeURIComponent(wxPreviewWxid) + '&limit=50&offset=' + wxPreviewOffset);
    const data = await resp.json();
    const msgs = data.messages || [];
    if (!msgs.length) { wxAddLog('没有更多消息了'); return; }
    wxPreviewOffset += 50;
    // 旧消息插入顶部
    const oldHtml = list.innerHTML;
    list.innerHTML = msgs.map(m => wxFmtMsg(m)).join('') + oldHtml;
  } catch(e) {
    wxAddLog('加载失败: ' + e.message);
  }
}

function wxFmtMsg(m) {
  const typeTag = m.msg_type && m.msg_type !== '文本' ? ' <span style="background:#F5F0EB;color:#6B6B6B;padding:1px 6px;border-radius:4px;font-size:11px;">' + esc(m.msg_type) + '</span>' : '';
  return '<div style="padding:6px 0;border-bottom:1px solid #F0EEEB;">' +
    '<div style="display:flex;align-items:baseline;gap:8px;margin-bottom:2px;">' +
      '<span style="font-weight:500;color:#D97706;font-size:12px;white-space:nowrap;">' + esc(m.sender || '?') + '</span>' +
      '<span style="color:#999;font-size:11px;">' + esc(m.time_str || '') + '</span>' + typeTag +
    '</div>' +
    '<div style="color:#1A1A1A;word-break:break-all;">' + esc(m.content || '') + '</div>' +
  '</div>';
}

function wxAddLog(msg) {
  const el = document.getElementById('wxRemoteLog');
  el.textContent += msg + '\\n';
  el.scrollTop = el.scrollHeight;
}

async function wxRefreshDB() {
  const st = document.getElementById('wx-remote-status');
  st.textContent = '正在刷新本地微信数据库...';
  st.className = 'status loading';
  wxAddLog('\\n刷新微信数据库...');
  try {
    const resp = await fetch(WX_LOCAL + '/api/refresh', {method:'POST'});
    const data = await resp.json();
    if (data.ok) {
      wxSessions = data.sessions || [];
      wxSelected.clear();
      wxFilterSessions();
      st.textContent = wxSessions.length + ' 个会话已刷新';
      st.className = 'status';
      wxAddLog('刷新完成: ' + wxSessions.length + ' 个会话');
    } else {
      st.textContent = '刷新失败: ' + (data.error||'');
      st.className = 'status error';
    }
  } catch(e) {
    st.textContent = '连接本地服务失败';
    st.className = 'status error';
  }
}

async function wxSmartScan() {
  if (!wxSelected.size) { alert('请先选择要扫描的聊天'); return; }
  const wxids = [...wxSelected].map(i => ({wxid: wxSessions[i].wxid, name: wxSessions[i].display_name}));
  const st = document.getElementById('wx-remote-status');
  st.textContent = '正在扫描 ' + wxids.length + ' 个聊天...';
  st.className = 'status loading';
  document.getElementById('wxBtnScan').disabled = true;
  wxAddLog('\\n开始扫描 ' + wxids.length + ' 个聊天...');

  // 轮询日志
  async function pollWxLogs() {
    try {
      const r = await fetch(WX_LOCAL + '/api/logs');
      const d = await r.json();
      if (d.logs) d.logs.forEach(m => wxAddLog(m));
      if (d.running) wxLogPollTimer = setTimeout(pollWxLogs, 1000);
    } catch(e) {}
  }
  wxLogPollTimer = setTimeout(pollWxLogs, 1000);

  try {
    const resp = await fetch(WX_LOCAL + '/api/scan', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode:'smart', chats:wxids}),
    });
    const data = await resp.json();
    wxScanResults = data.results || [];
    if (wxLogPollTimer) clearTimeout(wxLogPollTimer);
    await pollWxLogs();
    if (wxScanResults.length) {
      wxShowResults(wxScanResults);
      st.textContent = '扫描完成! 发现 ' + wxScanResults.length + ' 个项目';
      st.className = 'status';
    } else {
      st.textContent = '扫描完成，未发现项目相关内容';
      st.className = 'status';
    }
  } catch(e) {
    st.textContent = '扫描失败: ' + e.message;
    st.className = 'status error';
  }
  document.getElementById('wxBtnScan').disabled = false;
}

function wxShowResults(results) {
  const card = document.getElementById('wxRemoteResultsCard');
  const list = document.getElementById('wxRemoteResultsList');
  const cnt = document.getElementById('wxRemoteResultsCount');
  if (!results.length) { card.style.display = 'none'; return; }
  card.style.display = 'block';
  cnt.textContent = results.length + ' 条';
  list.innerHTML = results.map((item, i) => {
    const r = item.result || {};
    const rid = item.result_id || '';
    return '<div class="result-item" data-wxrid="'+rid+'"><h4>'+(i+1)+'. '+esc(r.company_name||'未知')+'</h4><div class="meta">类型: '+esc(r.meeting_type||'')+' · 赛道: '+esc(r.industry||'')+' · 阶段: '+esc(r.funding_stage||'')+'</div><div class="summary-preview">'+esc(r.summary||'')+'</div><div style="margin-top:8px;display:flex;gap:6px;"><button class="btn-sm btn-success" onclick="wxWriteOne(\\''+rid+'\\')">入库</button><button class="btn-sm btn-danger" onclick="wxDismiss(\\''+rid+'\\')">忽略</button></div></div>';
  }).join('');
}

async function wxWriteOne(rid) {
  const el = document.querySelector('[data-wxrid="'+rid+'"]');
  if (el) el.classList.add('writing');
  try {
    const resp = await fetch(WX_LOCAL + '/api/write', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({result_ids:[rid]}),
    });
    const data = await resp.json();
    const res = (data.items||[])[0]||{};
    if (res.ok) {
      if (el) el.classList.add('written');
      wxAddLog('✅ 已写入: ' + (res.company_name||''));
      setTimeout(() => {
        wxScanResults = wxScanResults.filter(x => x.result_id !== rid);
        wxShowResults(wxScanResults);
      }, 400);
    } else {
      if (el) el.classList.remove('writing');
      alert('写入失败: ' + (res.error||''));
    }
  } catch(e) {
    if (el) el.classList.remove('writing');
    alert('写入失败: ' + e.message);
  }
}

async function wxWriteAll() {
  if (!wxScanResults.length) return;
  const ids = wxScanResults.map(x => x.result_id).filter(Boolean);
  try {
    const resp = await fetch(WX_LOCAL + '/api/write', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({result_ids:ids}),
    });
    const data = await resp.json();
    wxAddLog('批量写入: ' + data.success + '/' + data.total);
    wxScanResults = [];
    wxShowResults([]);
  } catch(e) { alert('写入失败: ' + e.message); }
}

async function wxDismiss(rid) {
  try {
    await fetch(WX_LOCAL + '/api/dismiss', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({result_id:rid, blacklist:true}),
    });
    wxScanResults = wxScanResults.filter(x => x.result_id !== rid);
    wxShowResults(wxScanResults);
    wxAddLog('🚫 已忽略');
  } catch(e) { alert('失败: ' + e.message); }
}

// 定期检查本地服务是否还在线
setInterval(async () => {
  const el = document.getElementById('wx-local-status');
  const embeddedUI = document.getElementById('wx-embedded-ui');
  if (!el || !embeddedUI || embeddedUI.style.display === 'none') return;
  const ok = await detectLocal();
  el.textContent = ok ? '● 已连接' : '● 已断开';
  el.style.color = ok ? '#16A34A' : '#DC2626';
  // 如果断开连接，切换回安装引导
  if (!ok) {
    showWxState('offline');
  }
}, 10000);

{% endif %}
</script>
</body>
</html>"""


@app.route("/")
def index():
    wechat_ready = getattr(core, "reader", None) is not None
    return render_template_string(
        HTML_TEMPLATE,
        server_mode=SERVER_MODE,
        wechat_ready=wechat_ready,
        current_user=session.get("user_name") if REQUIRE_LOGIN else None,
    )


@app.route("/api/sessions")
def api_sessions():
    return jsonify({"sessions": core.list_wechat_sessions()})


@app.route("/api/preview")
def api_preview():
    wxid = request.args.get("wxid", "")
    limit = int(request.args.get("limit", "50"))
    offset = int(request.args.get("offset", "0"))
    return jsonify({"messages": core.preview_chat(wxid, limit=limit, offset=offset)})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """重新解密微信数据库 + 重新加载，获取最新消息"""
    result = core.refresh_db()
    if not result.ok:
        return jsonify({"ok": False, "error": result.error})
    return jsonify({
        "ok": True,
        "sessions": core.list_wechat_sessions(),
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    global scan_logs, scan_running
    data = request.json or {}
    mode = data.get("mode", "smart")
    chats = data.get("chats", [])

    with _scan_logs_lock:
        scan_logs = []
    scan_running = True

    def _log(msg):
        with _scan_logs_lock:
            scan_logs.append(msg)

    try:
        run = core.scan(mode=mode, chats=chats, log_cb=_log)
    finally:
        scan_running = False

    return jsonify({
        "session_id": run.session_id,
        "results": [item.to_dict() for item in run.results],
    })


@app.route("/api/logs")
def api_logs():
    global scan_logs
    with _scan_logs_lock:
        logs = scan_logs[:]
        scan_logs = []
    return jsonify({"logs": logs, "running": scan_running})


@app.route("/api/write", methods=["POST"])
def api_write():
    """按 result_id 写入 bitable。返回 per-item 结果。"""
    data = request.json or {}
    result_ids = data.get("result_ids") or []
    if not result_ids and data.get("results"):
        # 兼容老格式
        result_ids = [r.get("result_id") for r in data["results"] if r.get("result_id")]

    batch = core.write_results(result_ids)
    return jsonify(batch.to_dict())


@app.route("/api/edit", methods=["POST"])
def api_edit():
    """Inline 编辑单条结果的字段。"""
    data = request.json or {}
    result_id = data.get("result_id")
    fields = data.get("fields") or {}
    if not result_id or not fields:
        return jsonify({"ok": False, "error": "missing result_id or fields"}), 400
    row = core.edit_result(result_id, fields)
    if row is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "result": row})


@app.route("/api/dismiss", methods=["POST"])
def api_dismiss():
    """忽略（丢弃）扫描结果。可选地加入黑名单，下次扫描自动跳过同名项目。"""
    data = request.json or {}
    result_id = data.get("result_id")
    blacklist = bool(data.get("blacklist", True))
    if not result_id:
        return jsonify({"ok": False, "error": "missing result_id"}), 400
    ok = core.dismiss_result(result_id, blacklist=blacklist)
    if not ok:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/results/<session_id>")
def api_results(session_id: str):
    """取某次 session 的所有 pending 结果（用于历史恢复）。"""
    status_filter = request.args.get("status", "pending")
    return jsonify({
        "session_id": session_id,
        "results": core.get_session_results(session_id, status=status_filter),
    })


@app.route("/api/history")
def api_history():
    """最近的扫描 session 列表。"""
    return jsonify(core.list_history(limit=30).to_dict())


@app.route("/api/blacklist", methods=["GET"])
def api_blacklist_list():
    return jsonify({"items": core.list_blacklist()})


@app.route("/api/blacklist/delete", methods=["POST"])
def api_blacklist_delete():
    data = request.json or {}
    key_type = data.get("key_type")
    key_value = data.get("key_value")
    if not key_type or not key_value:
        return jsonify({"ok": False, "error": "missing key"}), 400
    core.remove_blacklist(key_type, key_value)
    return jsonify({"ok": True})


@app.route("/api/save", methods=["POST"])
def api_save():
    """保存扫描结果到本地 JSON。"""
    data = request.json or {}
    result_ids = data.get("result_ids") or []
    output_file, count = core.save_local(result_ids)
    return jsonify({"path": str(output_file), "count": count})


# ============================================================
# 飞书扫描（Phase 3b Level 1，桥接 feishu-deal-bot 的 services）
# ============================================================


@app.route("/api/feishu_chats")
def api_feishu_chats():
    """列出 bot 能访问的飞书群聊。"""
    try:
        chats = core.list_feishu_chats()
        return jsonify({"ok": True, "chats": chats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/feishu_scan", methods=["POST"])
def api_feishu_scan():
    """启动一次飞书历史扫描。body: {chat_id, chat_name?} → {job_id}"""
    data = request.json or {}
    chat_id = (data.get("chat_id") or "").strip()
    chat_name = (data.get("chat_name") or "").strip()
    if not chat_id:
        return jsonify({"ok": False, "error": "chat_id 必填"}), 400

    # Phase E: 从 session 中构建当前登录用户的 API 实例
    user_api = None
    if session.get("user_access_token"):
        try:
            import sys, os
            bot_dir = os.getenv("FEISHU_DEAL_BOT_DIR", "")
            if bot_dir and bot_dir not in sys.path:
                sys.path.insert(0, bot_dir)
            from services.feishu_api import FeishuUserAPI
            user_api = FeishuUserAPI(
                access_token=session["user_access_token"],
                refresh_token=session.get("user_refresh_token", ""),
                token_obtained_at=session.get("token_obtained_at", 0),
                expires_in=session.get("token_expires_in", 7200),
                on_token_refreshed=lambda **kw: _update_session_tokens(kw),
            )
        except Exception as e:
            logger.warning(f"[Phase E] 创建 user_api 失败，回退 lark-cli: {e}")

    try:
        job_id = core.start_feishu_scan(chat_id, chat_name, user_api=user_api)
        return jsonify({"ok": True, "job_id": job_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _update_session_tokens(token_info: dict):
    """Token 刷新后更新 Flask session（跨线程需注意：session 只在请求上下文内有效）。
    如果不在请求上下文内，静默跳过——token 已在 FeishuUserAPI 对象内更新。"""
    try:
        from flask import has_request_context
        if not has_request_context():
            return
        session["user_access_token"] = token_info.get("access_token", "")
        session["user_refresh_token"] = token_info.get("refresh_token", "")
        session["token_expires_in"] = token_info.get("expires_in", 7200)
        session["token_obtained_at"] = token_info.get("token_obtained_at", 0)
    except Exception:
        pass


@app.route("/api/feishu_scan/<job_id>")
def api_feishu_scan_status(job_id: str):
    """轮询扫描状态。?since=N 返回第 N 条之后的增量日志。"""
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    snap = core.get_feishu_scan_status(job_id, since_index=since)
    if snap is None:
        return jsonify({"ok": False, "error": "job 不存在"}), 404
    return jsonify({"ok": True, **snap})


@app.route("/api/feishu_scan/<job_id>/cancel", methods=["POST"])
def api_feishu_scan_cancel(job_id: str):
    ok = core.cancel_feishu_scan(job_id)
    return jsonify({"ok": ok})


# ============================================================
# 知识库 Wiki API（Phase: Wiki Knowledge Base）
# ============================================================

@app.route("/api/wiki/list")
def api_wiki_list():
    """列出所有公司 wiki 页面，附统计信息。"""
    try:
        from services.wiki_service import get_wiki_service
        wiki = get_wiki_service()
        companies = wiki.list_companies()
        stats = wiki.get_stats()
        return jsonify({"companies": companies, "stats": stats})
    except Exception as e:
        return jsonify({"companies": [], "stats": {}, "error": str(e)}), 500


@app.route("/api/wiki/company")
def api_wiki_company():
    """获取指定公司的 wiki 页面内容。"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"content": None, "error": "缺少 name 参数"}), 400
    try:
        from services.wiki_service import get_wiki_service
        wiki = get_wiki_service()
        content = wiki.get_company_page(name)
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"content": None, "error": str(e)}), 500


@app.route("/api/wiki/search")
def api_wiki_search():
    """搜索 wiki 知识库。"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    try:
        from services.wiki_service import get_wiki_service
        wiki = get_wiki_service()
        results = wiki.search_wiki(q)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)}), 500


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5678)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    print("=" * 50)
    print("  扫描工作台 — Web UI")
    print(f"  SERVER_MODE={SERVER_MODE}")
    print("=" * 50)
    if not SERVER_MODE:
        print("正在加载微信数据库...")
        try:
            count = core.init_reader()
            print(f"加载完成: {count} 个会话")
        except Exception as e:
            print(f"⚠️ 微信 DB 加载失败（将禁用微信扫描 tab）: {e}")
    else:
        print("服务器模式：跳过微信 DB 初始化")

    host = os.getenv("BIND_HOST", "127.0.0.1")
    url = f"http://{host}:{args.port}"
    if not args.no_browser and not SERVER_MODE:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"\n监听: {url}")
    print("按 Ctrl+C 停止\n")
    app.run(host=host, port=args.port, debug=False)
