"""服务器端 OAuth token 存储 — 按 open_id keying。

为什么需要这个：Flask session 是 cookie，后台扫描线程更新不了。
当 deep_scan 在子线程里 refresh access_token 时，飞书会返回新的 refresh_token
（旧的同时被作废）。如果新值只存在子线程内存里，下次扫描从 session 重新构造
FeishuUserAPI 时拿到的还是旧 refresh_token，请求飞书会报 20038
"refresh token has been used".

解决：每次拿到新 token 都写到本地 JSON 文件（按 open_id 索引）。
读 token 时先看 store（最新），找不到再 fallback 到 session。

线程安全：用 threading.Lock + 原子 rename 替换文件。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 文件位置：<project>/data/user_tokens.json
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_STORE_PATH = _DATA_DIR / "user_tokens.json"
_LOCK = threading.Lock()


def _read_all() -> dict:
    if not _STORE_PATH.exists():
        return {}
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        logger.warning(f"[token_store] 读取失败 ({e})，从空 dict 开始")
        return {}


def _write_all(data: dict) -> None:
    """原子写：先写临时文件再 rename，避免半截文件。"""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".user_tokens_", suffix=".json", dir=str(_DATA_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STORE_PATH)
        try:
            os.chmod(_STORE_PATH, 0o600)
        except Exception:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def save(
    open_id: str,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: int = 7200,
    token_obtained_at: Optional[int] = None,
    user_name: str = "",
) -> None:
    """保存 token。覆盖式更新。从任意线程都可以调。"""
    if not open_id or not access_token:
        return
    with _LOCK:
        data = _read_all()
        data[open_id] = {
            "access_token": access_token,
            "refresh_token": refresh_token or "",
            "expires_in": int(expires_in or 7200),
            "token_obtained_at": int(token_obtained_at or time.time()),
            "user_name": user_name or data.get(open_id, {}).get("user_name", ""),
            "saved_at": int(time.time()),
        }
        _write_all(data)
    logger.info(f"[token_store] saved tokens for {open_id} ({user_name or '?'})")


def load(open_id: str) -> Optional[dict]:
    """读 token；找不到返 None。"""
    if not open_id:
        return None
    with _LOCK:
        data = _read_all()
    rec = data.get(open_id)
    if not rec:
        return None
    return dict(rec)


def clear(open_id: str) -> None:
    """登出时清掉。"""
    if not open_id:
        return
    with _LOCK:
        data = _read_all()
        if open_id in data:
            data.pop(open_id, None)
            _write_all(data)
            logger.info(f"[token_store] cleared {open_id}")
