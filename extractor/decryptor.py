"""微信数据库解密器

流程：
1. 从运行中的微信进程内存提取 SQLCipher 密钥
2. 用密钥解密 .db 文件到指定输出目录
3. 解密后的 DB 可用标准 sqlite3 读取

前置条件：
- macOS SIP 已关闭（csrutil disable）
- 微信已登录并在运行
- 安装了 sqlcipher（brew install sqlcipher）
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)


def check_prerequisites() -> list[str]:
    """检查前置条件，返回问题列表（空列表 = 全部通过）"""
    issues = []

    # 1. macOS only
    if platform.system() != "Darwin":
        issues.append("当前仅支持 macOS")

    # 2. SIP 状态
    try:
        result = subprocess.run(
            ["csrutil", "status"],
            capture_output=True, text=True, timeout=5,
        )
        if "enabled" in result.stdout.lower():
            issues.append(
                "SIP 已开启。需要关闭 SIP 才能读取微信进程内存。\n"
                "  步骤：重启 → 按住 Cmd+R 进入恢复模式 → 终端输入 csrutil disable → 重启"
            )
    except Exception:
        issues.append("无法检查 SIP 状态")

    # 3. sqlcipher
    if not shutil.which("sqlcipher"):
        issues.append("sqlcipher 未安装。运行: brew install sqlcipher")

    # 4. 微信是否在运行
    try:
        result = subprocess.run(
            ["pgrep", "-x", "WeChat"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            issues.append("微信未运行。请先登录微信。")
    except Exception:
        issues.append("无法检查微信进程")

    # 5. 数据目录存在
    if not config.WECHAT_DATA_ROOT.exists():
        issues.append(f"微信数据目录不存在: {config.WECHAT_DATA_ROOT}")

    return issues


def get_wechat_pid() -> Optional[int]:
    """获取微信进程 PID"""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "WeChat"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split("\n")
            return int(pids[0])
    except Exception as e:
        logger.error(f"获取微信 PID 失败: {e}")
    return None


def extract_key_from_memory(pid: int) -> Optional[str]:
    """
    从微信进程内存中提取 SQLCipher 密钥。

    原理：微信在内存中保存 SQLCipher 的 raw key (32 bytes)。
    通过 lldb attach 到进程，扫描内存区域找到密钥。

    返回 hex 格式的密钥字符串（64个hex字符）。
    """
    logger.info(f"正在从微信进程 (PID={pid}) 提取密钥...")

    # 方法 1: 尝试用 lldb 搜索内存
    key = _extract_key_via_lldb(pid)
    if key:
        return key

    # 方法 2: 尝试用外部工具
    key = _extract_key_via_tool()
    if key:
        return key

    logger.error("无法提取密钥。请确认 SIP 已关闭且微信在运行。")
    return None


def _extract_key_via_lldb(pid: int) -> Optional[str]:
    """通过 lldb attach 微信进程并搜索 SQLCipher 密钥"""
    try:
        # lldb 脚本：attach → 搜索 sqlite3_key 调用 → 提取参数
        lldb_script = f"""
process attach --pid {pid}
# 搜索内存中的 SQLCipher key pattern
# SQLCipher key 是 32 bytes，通常在调用 sqlite3_key 时传入
# 我们搜索 "PRAGMA key" 字符串附近的内存
memory find --count 1 --expr "(uint8_t[10]){{0x50,0x52,0x41,0x47,0x4d,0x41,0x20,0x6b,0x65,0x79}}" -- 0x0 0x7fffffffffff
process detach
quit
"""
        result = subprocess.run(
            ["lldb", "--batch", "--one-line-on-crash", "quit"],
            input=lldb_script,
            capture_output=True, text=True, timeout=30,
        )
        # 解析 lldb 输出找密钥
        # 这个方法可能不稳定，优先用工具方法
        logger.debug(f"lldb 输出: {result.stdout[:500]}")
    except Exception as e:
        logger.debug(f"lldb 方法失败: {e}")
    return None


def _extract_key_via_tool() -> Optional[str]:
    """尝试用 wechat-db-decrypt-macos 等工具提取密钥"""
    # 检查是否安装了 wechat-dump-rs
    for tool in ["wechat-dump-rs", "wechat-db-decrypt"]:
        path = shutil.which(tool)
        if path:
            try:
                result = subprocess.run(
                    [path, "key"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    # 尝试从输出提取 hex key
                    match = re.search(r'[0-9a-fA-F]{64}', result.stdout)
                    if match:
                        key = match.group(0)
                        logger.info(f"成功提取密钥 (via {tool})")
                        return key
            except Exception as e:
                logger.debug(f"{tool} 失败: {e}")
    return None


def decrypt_database(
    encrypted_db: Path,
    output_dir: Path,
    key: str,
) -> Optional[Path]:
    """
    用 sqlcipher 解密单个数据库文件。

    encrypted_db: 加密的 .db 文件路径
    output_dir: 输出目录
    key: hex 格式密钥
    返回解密后的文件路径
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / encrypted_db.name

    if output_path.exists():
        # 检查是否比源文件新（已解密过）
        if output_path.stat().st_mtime >= encrypted_db.stat().st_mtime:
            logger.info(f"已解密，跳过: {encrypted_db.name}")
            return output_path

    logger.info(f"解密: {encrypted_db.name} -> {output_path}")

    try:
        # sqlcipher 解密命令
        sql_commands = f"""
PRAGMA key = "x'{key}'";
PRAGMA cipher_compatibility = 4;
ATTACH DATABASE '{output_path}' AS decrypted KEY '';
SELECT sqlcipher_export('decrypted');
DETACH DATABASE decrypted;
.quit
"""
        result = subprocess.run(
            ["sqlcipher", str(encrypted_db)],
            input=sql_commands,
            capture_output=True, text=True, timeout=120,
        )

        if result.returncode != 0:
            logger.error(f"sqlcipher 解密失败: {result.stderr[:300]}")
            # 清理失败的输出
            output_path.unlink(missing_ok=True)
            return None

        # 验证解密结果
        try:
            conn = sqlite3.connect(str(output_path))
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            conn.close()
            if tables:
                logger.info(f"解密成功: {encrypted_db.name} ({len(tables)} 个表)")
                return output_path
            else:
                logger.warning(f"解密后无表: {encrypted_db.name}")
                output_path.unlink(missing_ok=True)
                return None
        except sqlite3.DatabaseError:
            logger.error(f"解密后文件无效: {encrypted_db.name}")
            output_path.unlink(missing_ok=True)
            return None

    except subprocess.TimeoutExpired:
        logger.error(f"解密超时: {encrypted_db.name}")
        output_path.unlink(missing_ok=True)
        return None
    except Exception as e:
        logger.error(f"解密出错: {encrypted_db.name}: {e}")
        output_path.unlink(missing_ok=True)
        return None


def find_wechat_accounts() -> list[dict]:
    """
    发现本机所有微信账号的数据目录。

    返回 [{"wxid": str, "data_path": Path, "db_storage": Path}, ...]
    """
    accounts = []
    root = config.WECHAT_DATA_ROOT
    if not root.exists():
        return accounts

    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith(".") or entry.name == "Backup":
            continue
        # 新格式目录名: wxid_xxxxx_xxxx
        if entry.name.startswith("wxid_"):
            wxid = entry.name.rsplit("_", 1)[0]  # 去掉最后的 hash 后缀
            db_storage = entry / "db_storage"
            if db_storage.exists():
                accounts.append({
                    "wxid": wxid,
                    "data_path": entry,
                    "db_storage": db_storage,
                    "display_name": wxid,
                })

    return accounts


def decrypt_account_dbs(
    account: dict,
    key: str,
    output_dir: Path | None = None,
) -> dict[str, Path]:
    """
    解密一个微信账号的核心数据库。

    返回 {db_name: decrypted_path}
    """
    if output_dir is None:
        output_dir = config.DECRYPTED_DB_DIR / account["wxid"]

    db_storage = account["db_storage"]
    results = {}

    # 核心数据库列表
    target_dbs = [
        "contact/contact.db",
        "session/session.db",
        "message/message_0.db",
        "message/message_1.db",
        "message/message_2.db",
        "message/message_3.db",
    ]

    for db_rel in target_dbs:
        db_path = db_storage / db_rel
        if db_path.exists():
            db_output_dir = output_dir / Path(db_rel).parent
            decrypted = decrypt_database(db_path, db_output_dir, key)
            if decrypted:
                results[db_rel] = decrypted

    logger.info(f"账号 {account['wxid']}: 解密 {len(results)}/{len(target_dbs)} 个数据库")
    return results
