"""Feishu bot services bridge.

本模块从 feishu-deal-bot 项目里复用以下能力，但 **不搬代码**：
  - 创建/持有一个 lark.Client（app_id/app_secret 来自 bot 的 .env）
  - 列出 bot 能访问的所有群聊 (im.v1.chat.list)
  - 触发 services.history_scanner.process_history_links(client, chat_id, cb)

为什么用 bridge 而不是 import？因为两个项目各有自己的 `config.py`，
naive import 会被 Python 的 sys.modules 缓存污染——wechat-scanner/config.py
已经被缓存为 `sys.modules['config']`，bot/config.py 一 import 就拿错。
解决方式和 scanner_core.py 里的 WAL import 完全一样：临时 pop / 恢复。

使用：
    from bridge.bot_services import BotBridge
    bridge = BotBridge()                   # 懒加载 client + env
    chats = bridge.list_chats()            # [{chat_id, name, ...}]
    bridge.run_history_scan(chat_id, cb)   # cb(msg_str) 流式回调

并发：同一进程内只维护一个 client，多次 list/scan 共享。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

import config as scanner_config

logger = logging.getLogger(__name__)


# 默认 bot 根目录——可用 .env 里的 FEISHU_DEAL_BOT_DIR 覆盖
_DEFAULT_BOT_DIR = Path.home() / "Desktop" / "feishu" / "feishu-deal-bot"


def _bot_dir() -> Path:
    custom = os.getenv("FEISHU_DEAL_BOT_DIR")
    if custom:
        return Path(custom).expanduser().resolve()
    return _DEFAULT_BOT_DIR


class BridgeError(Exception):
    pass


# scanner 和 bot 都有的模块名，在 import bot services 时需要隔离
_CONFLICT_MODULE_NAMES = ("config",)


@contextmanager
def _bot_import_context():
    """临时切到 bot 项目的 import 路径 + 切换 cwd + 隔离 config 模块。

    步骤：
      1. 把 bot_dir 插到 sys.path[0]
      2. pop 掉 scanner 的 'config' 等重名模块（保存下来）
      3. 切 cwd 到 bot_dir，好让 bot 的 load_dotenv() 找到 .env
      4. yield
      5. finally: 恢复 cwd / sys.path / sys.modules
    """
    bot_dir = _bot_dir()
    if not bot_dir.exists():
        raise BridgeError(f"feishu-deal-bot 目录不存在: {bot_dir}")

    bot_dir_str = str(bot_dir)
    saved = {name: sys.modules.pop(name, None) for name in _CONFLICT_MODULE_NAMES}
    # 也 pop 掉 bot 自己的子包（万一之前 bridge 别处 import 过会缓存）
    bot_pkg_prefixes = ("services.", "utils.", "handlers.")
    saved_bot_pkgs: dict[str, Any] = {}
    for key in list(sys.modules.keys()):
        if key in ("services", "utils", "handlers") or any(
            key.startswith(p) for p in bot_pkg_prefixes
        ):
            saved_bot_pkgs[key] = sys.modules.pop(key)

    # 强制把 bot 目录放到 sys.path 最前（关键修复）。
    # 旧逻辑只在 bot_dir 不在 path 时才 insert(0)，但如果 bot_dir 已经在 path 里
    # （排在 scanner 目录之后，例如别处 import 过 bot 模块留下的），insert 被跳过
    # → `import services` 先命中 scanner 自己的 services 包 → 报
    # "No module named 'services.deep_scan_pipeline'"（以及 feishu_user_read 等）。
    had_bot_dir = bot_dir_str in sys.path
    if had_bot_dir:
        try:
            sys.path.remove(bot_dir_str)
        except ValueError:
            pass
    sys.path.insert(0, bot_dir_str)

    original_cwd = os.getcwd()
    try:
        os.chdir(bot_dir_str)
        yield
    finally:
        try:
            os.chdir(original_cwd)
        except Exception:
            pass
        # 撤掉我们插到最前的 bot_dir；若它本来就在 path 里，按原样补回末尾
        try:
            sys.path.remove(bot_dir_str)
        except ValueError:
            pass
        if had_bot_dir and bot_dir_str not in sys.path:
            sys.path.append(bot_dir_str)
        # 恢复 scanner 的 config（重要——后续 scanner 代码还要用）
        for name, mod in saved.items():
            if mod is not None:
                sys.modules[name] = mod
            else:
                sys.modules.pop(name, None)
        # 关键：清掉刚加载的 bot 版 services/utils/handlers，恢复 scanner 原本的，
        # 否则 context 退出后 scanner 代码 import services.token_store / wiki_service
        # 会拿到 bot 的包而失败。
        for key in list(sys.modules.keys()):
            if key in ("services", "utils", "handlers") or any(
                key.startswith(p) for p in bot_pkg_prefixes
            ):
                sys.modules.pop(key, None)
        for name, mod in saved_bot_pkgs.items():
            sys.modules[name] = mod


class BotBridge:
    """bot 服务的单例桥接器。线程安全——list/scan 可并发。"""

    _instance: Optional["BotBridge"] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "BotBridge":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._client = None  # lark.Client, lazy
        self._client_lock = threading.Lock()
        self._bot_config_snapshot: dict[str, Any] = {}
        self._initialized = True

    # ------------------------------------------------------------
    # Client
    # ------------------------------------------------------------

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client

            # 优先用 scanner 自己的 env（和 bot 共享 .env 文件或者单独覆盖）
            app_id = scanner_config.FEISHU_APP_ID if hasattr(scanner_config, "FEISHU_APP_ID") else ""
            app_secret = scanner_config.FEISHU_APP_SECRET if hasattr(scanner_config, "FEISHU_APP_SECRET") else ""

            if not app_id or not app_secret:
                # 退而求其次——从 bot 的 .env 读
                app_id, app_secret = self._read_bot_env_credentials()

            if not app_id or not app_secret:
                raise BridgeError(
                    "FEISHU_APP_ID/FEISHU_APP_SECRET 未配置。"
                    "请在 wechat-scanner/.env 或 feishu-deal-bot/.env 中设置。"
                )

            import lark_oapi as lark  # noqa: E402

            self._client = (
                lark.Client.builder()
                .app_id(app_id)
                .app_secret(app_secret)
                .log_level(lark.LogLevel.INFO)
                .build()
            )
            logger.info(f"[Bridge] Created lark.Client app_id={app_id[:8]}...")
            return self._client

    def _read_bot_env_credentials(self) -> tuple[str, str]:
        """从 bot 的 .env 文件里读 APP_ID/SECRET。不污染当前进程 env。"""
        env_path = _bot_dir() / ".env"
        if not env_path.exists():
            return "", ""
        app_id = app_secret = ""
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k == "FEISHU_APP_ID":
                    app_id = v
                elif k == "FEISHU_APP_SECRET":
                    app_secret = v
        except Exception as e:
            logger.warning(f"[Bridge] 读 bot .env 失败: {e}")
        return app_id, app_secret

    # ------------------------------------------------------------
    # Public: list chats
    # ------------------------------------------------------------

    def list_chats(self) -> list[dict]:
        """列出 bot 能访问的所有群聊。返回 [{chat_id, name, description, chat_mode, owner_id}]"""
        client = self._ensure_client()
        from lark_oapi.api.im.v1 import ListChatRequest  # noqa: E402

        chats: list[dict] = []
        page_token = ""
        while True:
            builder = ListChatRequest.builder().page_size(100)
            if page_token:
                builder = builder.page_token(page_token)
            resp = client.im.v1.chat.list(builder.build())
            if not resp.success():
                raise BridgeError(
                    f"飞书 chat.list 失败: code={resp.code} msg={resp.msg}"
                )
            items = resp.data.items or []
            for c in items:
                chats.append({
                    "chat_id": c.chat_id or "",
                    "name": c.name or "(未命名)",
                    "description": c.description or "",
                    "external": bool(getattr(c, "external", False)),
                    "owner_id": c.owner_id or "",
                    "avatar": getattr(c, "avatar", "") or "",
                })
            if not resp.data.has_more:
                break
            page_token = resp.data.page_token or ""
            if not page_token:
                break
        # 排序：有名字的在前
        chats.sort(key=lambda x: (not x["name"] or x["name"] == "(未命名)", x["name"]))
        logger.info(f"[Bridge] list_chats: {len(chats)} 个群聊")
        return chats

    # ------------------------------------------------------------
    # Public: run history scan
    # ------------------------------------------------------------

    def run_history_scan(
        self,
        chat_id: str,
        progress_callback: Callable[[str], None],
        user_api=None,
    ) -> None:
        """调用 bot 的深度扫描 pipeline (streaming + memory)。

        优先用 user_api（OAuth user_access_token）静默拉群消息，不需要 bot
        在群里。bot client 仅用于 PDF 下载等需要租户凭证的操作。

        所有进度/错误通过 progress_callback (DM owner)，从不 spam 群。
        """
        client = self._ensure_client()
        with _bot_import_context():
            from services.deep_scan_pipeline import run_deep_scan  # noqa: E402
            from services.minutes_service import set_user_api, clear_user_api  # noqa: E402

            # 单一 token holder：把 web_ui 传来的 user_api 注入线程内，拉群消息 + 妙记逐字稿
            # 都用同一个它（同一条 refresh 链路），避免之前两个对象各自刷新把单次有效的
            # refresh_token 用废（飞书 20038 invalid refresh token）。
            if user_api is not None:
                set_user_api(user_api)
                logger.info("[Bridge] 启用 user_api 静默读群 + 妙记（不需要 bot 在群里）")

            try:
                run_deep_scan(chat_id, progress_callback, bot_client=client)
            except Exception as e:
                logger.exception(f"[Bridge] 深度扫描 {chat_id} 异常: {e}")
                progress_callback(f"❌ 扫描异常: {e}")
                raise
            finally:
                if user_api:
                    clear_user_api()

    def cancel_scan(self) -> None:
        """取消正在进行的扫描。"""
        with _bot_import_context():
            from services.history_scanner import cancel_scan  # noqa: E402
            cancel_scan()
