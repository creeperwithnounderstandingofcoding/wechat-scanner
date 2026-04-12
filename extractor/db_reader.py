"""微信消息数据库读取器 v2 — 适配 WeChat 4.x 解密后的 DB

数据库结构（解密后）：
- contact.db — 联系人（字段: username, nick_name, remark）
- session.db — 会话列表（字段: username, summary, last_timestamp, sort_timestamp）
- message_N.db — 消息（表名: Msg_{hash}，字段: message_content, local_type, create_time, WCDB_CT_message_content）
  - message_content 可能是 zstd 压缩的（WCDB_CT_message_content=4）
  - local_type: 1=文本, 3=图片, 34=语音, 43=视频, 49=文件/链接
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BJT = timezone(timedelta(hours=8))

# zstd 解压
try:
    import zstandard
    _dctx = zstandard.ZstdDecompressor()
except ImportError:
    _dctx = None
    logger.warning("zstandard 未安装，压缩消息将无法解码。pip install zstandard")


class WeChatDBReader:
    """微信解密数据库读取器（适配 WeChat 4.x）"""

    def __init__(self, decrypted_dir: str | Path):
        self._dir = Path(decrypted_dir)
        self._contact_cache: dict[str, dict] = {}
        # Name2Id 映射: id → username（每个消息 DB 有自己的 Name2Id 表）
        self._name2id_cache: dict[str, dict[int, str]] = {}

    def _conn(self, rel_path: str) -> Optional[sqlite3.Connection]:
        path = self._dir / rel_path
        if path.exists():
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            return conn
        return None

    # ===== 联系人 =====

    def get_contacts(self) -> list[dict]:
        conn = self._conn("contact/contact.db")
        if not conn:
            return []

        contacts = []
        try:
            rows = conn.execute(
                "SELECT username, nick_name, remark FROM contact"
            ).fetchall()
            for r in rows:
                contacts.append({
                    "wxid": r["username"],
                    "nickname": r["nick_name"] or "",
                    "remark": r["remark"] or "",
                    "display_name": r["remark"] or r["nick_name"] or r["username"],
                    "is_group": "@chatroom" in r["username"],
                })
        except Exception as e:
            logger.error(f"读取联系人失败: {e}")
        finally:
            conn.close()

        self._contact_cache = {c["wxid"]: c for c in contacts}
        logger.info(f"加载 {len(contacts)} 个联系人")
        return contacts

    def get_contact_name(self, wxid: str) -> str:
        if not self._contact_cache:
            self.get_contacts()
        c = self._contact_cache.get(wxid, {})
        return c.get("display_name", wxid)

    # ===== 会话列表 =====

    def get_sessions(self, limit: int = 100) -> list[dict]:
        conn = self._conn("session/session.db")
        if not conn:
            return []

        sessions = []
        try:
            if limit > 0:
                rows = conn.execute(
                    "SELECT username, summary, last_timestamp, sort_timestamp "
                    "FROM SessionTable ORDER BY sort_timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                # limit=0 → 全部加载
                rows = conn.execute(
                    "SELECT username, summary, last_timestamp, sort_timestamp "
                    "FROM SessionTable ORDER BY sort_timestamp DESC",
                ).fetchall()
            for r in rows:
                wxid = r["username"]
                sessions.append({
                    "wxid": wxid,
                    "display_name": self.get_contact_name(wxid),
                    "is_group": "@chatroom" in wxid,
                    "last_msg_time": r["sort_timestamp"] or 0,
                    "last_msg_time_str": _ts_to_str(r["sort_timestamp"]),
                    "summary": r["summary"] or "",
                })
        except Exception as e:
            logger.error(f"读取会话列表失败: {e}")
        finally:
            conn.close()

        return sessions

    # ===== Name2Id 映射 =====

    def _load_name2id(self, conn: sqlite3.Connection, db_key: str) -> dict[int, str]:
        if db_key in self._name2id_cache:
            return self._name2id_cache[db_key]

        mapping = {}
        try:
            rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
            for r in rows:
                mapping[r["rowid"]] = r["user_name"]
        except Exception:
            pass
        self._name2id_cache[db_key] = mapping
        return mapping

    # ===== 消息读取 =====

    @staticmethod
    def _wxid_to_table(wxid: str) -> str:
        """wxid → Msg_{MD5(wxid)} 表名"""
        import hashlib
        md5 = hashlib.md5(wxid.encode()).hexdigest()
        return f"Msg_{md5}"

    def get_messages(
        self,
        wxid: str,
        limit: int = 100,
        offset: int = 0,
        after_ts: int = 0,
    ) -> list[dict]:
        """获取与某个聊天的消息，按时间正序"""
        messages = []
        table = self._wxid_to_table(wxid)

        # WeChat 4.x: 每个聊天一个 Msg_{MD5(wxid)} 表，分布在 message_0~3.db 中
        for i in range(4):
            db_path = f"message/message_{i}.db"
            conn = self._conn(db_path)
            if not conn:
                continue

            try:
                # 检查表是否存在
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()

                if not exists:
                    conn.close()
                    continue

                name2id = self._load_name2id(conn, db_path)

                where = "1=1"
                params: list = []
                if after_ts:
                    where = "create_time > ?"
                    params.append(after_ts)

                rows = conn.execute(
                    f'SELECT * FROM "{table}" WHERE {where} ORDER BY sort_seq ASC',
                    params,
                ).fetchall()

                for row in rows:
                    msg = self._parse_message(dict(row), name2id, wxid)
                    if msg:
                        messages.append(msg)

            except Exception as e:
                logger.error(f"读取 {db_path}/{table} 失败: {e}")
            finally:
                conn.close()

        messages.sort(key=lambda m: m.get("timestamp", 0))
        return messages[offset:offset + limit]

    def _parse_message(self, row: dict, name2id: dict, chat_wxid: str) -> Optional[dict]:
        """解析单条消息"""
        content_raw = row.get("message_content", b"")
        ct = row.get("WCDB_CT_message_content", 0)
        msg_type = row.get("local_type", 0)
        create_time = row.get("create_time", 0)

        # 解压 / 解码 content
        content = ""
        if content_raw:
            if ct == 4 and isinstance(content_raw, bytes) and _dctx:
                try:
                    content = _dctx.decompress(content_raw).decode("utf-8", errors="replace")
                except Exception:
                    content = ""
            elif isinstance(content_raw, bytes):
                content = content_raw.decode("utf-8", errors="replace")
            elif isinstance(content_raw, str):
                content = content_raw

        if not content:
            return None

        # 解析发送者
        sender_id = row.get("real_sender_id", 0)
        sender_wxid = name2id.get(sender_id, "")
        sender_name = self.get_contact_name(sender_wxid) if sender_wxid else ""

        # 群聊中提取实际发送者
        if chat_wxid.endswith("@chatroom") and not sender_name:
            # 群消息格式可能在 content 里: "wxid:\n实际内容"
            if ":\n" in content and content.index(":\n") < 60:
                parts = content.split(":\n", 1)
                sender_name = self.get_contact_name(parts[0])
                content = parts[1]

        # 处理消息类型
        type_name = _msg_type_name(msg_type)
        display_content = content

        if msg_type != 1:  # 非文本
            display_content = _extract_rich_content(content, msg_type)

        return {
            "wxid": chat_wxid,
            "timestamp": create_time,
            "time_str": _ts_to_str(create_time),
            "sender": sender_name,
            "sender_wxid": sender_wxid,
            "content": display_content,
            "raw_content": content,
            "msg_type": msg_type,
            "type_name": type_name,
        }

    def get_message_count(self, wxid: str) -> int:
        """获取某聊天的消息总数"""
        total = 0
        table = self._wxid_to_table(wxid)

        for i in range(4):
            db_path = f"message/message_{i}.db"
            conn = self._conn(db_path)
            if not conn:
                continue
            try:
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    conn.close()
                    continue

                c = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
                if c:
                    total += c[0]
            except Exception:
                pass
            finally:
                conn.close()
        return total

    def search_messages(self, keyword: str, wxid: str = "", limit: int = 50) -> list[dict]:
        """搜索消息内容。如果指定 wxid 则只搜该聊天，否则搜所有聊天。"""
        results = []

        for i in range(4):
            db_path = f"message/message_{i}.db"
            conn = self._conn(db_path)
            if not conn:
                continue
            try:
                name2id = self._load_name2id(conn, db_path)

                # 确定要搜索的表
                if wxid:
                    tables = [self._wxid_to_table(wxid)]
                else:
                    tables = [
                        t[0] for t in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                        ).fetchall()
                    ]

                for table in tables:
                    # 检查表是否存在
                    exists = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()
                    if not exists:
                        continue

                    try:
                        rows = conn.execute(
                            f'SELECT * FROM "{table}" WHERE local_type = 1 '
                            f'ORDER BY sort_seq DESC LIMIT ?',
                            (limit * 3,),
                        ).fetchall()
                        for row in rows:
                            msg = self._parse_message(dict(row), name2id, wxid or "")
                            if msg and keyword.lower() in msg["content"].lower():
                                results.append(msg)
                                if len(results) >= limit:
                                    break
                    except sqlite3.OperationalError:
                        continue
                    if len(results) >= limit:
                        break
            except Exception:
                pass
            finally:
                conn.close()
            if len(results) >= limit:
                break
        return results[:limit]


def _ts_to_str(ts) -> str:
    if not ts:
        return ""
    try:
        ts = int(ts)
        if ts > 1_000_000_000_000:
            ts = ts // 1000
        dt = datetime.fromtimestamp(ts, tz=BJT)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _msg_type_name(msg_type) -> str:
    # WeChat 4.x 的类型可能是大数字（高位有额外信息）
    base_type = msg_type & 0xFF if msg_type > 255 else msg_type
    type_map = {
        1: "文本",
        3: "图片",
        34: "语音",
        42: "名片",
        43: "视频",
        47: "表情",
        48: "位置",
        49: "文件/链接",
    }
    return type_map.get(base_type, f"其他({msg_type})")


def _extract_rich_content(content: str, msg_type: int) -> str:
    """从 XML 消息中提取有用信息"""
    base = msg_type & 0xFF if msg_type > 255 else msg_type

    if base == 49:  # 文件/链接/小程序
        title = re.search(r"<title>(.*?)</title>", content)
        des = re.search(r"<des>(.*?)</des>", content, re.DOTALL)
        url = re.search(r"<url>(.*?)</url>", content)
        parts = []
        if title:
            parts.append(title.group(1))
        if des and des.group(1).strip():
            parts.append(des.group(1).strip()[:200])
        result = " | ".join(parts) if parts else "[链接/文件]"
        if url and url.group(1).strip():
            result += f" [链接: {url.group(1).strip()}]"
        return result

    if base == 3:
        # 尝试从 XML 中提取 md5 用于后续图片定位
        md5_match = re.search(r'md5="([a-fA-F0-9]+)"', content)
        if md5_match:
            return f"[图片:md5={md5_match.group(1)}]"
        return "[图片]"
    if base == 34:
        # 尝试从 XML 中提取微信内置语音转文字
        trans_match = re.search(r'<voicetrans[^>]*>(.*?)</voicetrans>', content, re.DOTALL)
        if not trans_match:
            trans_match = re.search(r'<transtext[^>]*>(.*?)</transtext>', content, re.DOTALL)
        if not trans_match:
            trans_match = re.search(r'<voicetranslateinfo>.*?<translatecontent><!\[CDATA\[(.*?)\]\]></translatecontent>', content, re.DOTALL)
        if trans_match:
            trans_text = trans_match.group(1).strip()
            if trans_text:
                return f"[语音转文字: {trans_text}]"
        return "[语音]"
    if base == 43:
        return "[视频]"
    if base == 42:
        nick = re.search(r'nickname="(.*?)"', content)
        return f"[名片: {nick.group(1)}]" if nick else "[名片]"
    if base == 47:
        return "[表情]"

    # 对于含 XML 的消息，尝试提取 title
    title = re.search(r"<title>(.*?)</title>", content)
    if title:
        return title.group(1)

    return content[:200] if len(content) > 200 else content
