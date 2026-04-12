"""SQLite 持久化层

统一管理：
- 扫描 session（一次扫描是一个 session，记录哪些聊天、哪些结果）
- 扫描结果（per-item 状态：pending / written / dismissed / failed）
- 黑名单（被用户忽略的公司名或 PDF md5，下次扫描自动跳过）
- PDF md5 缓存（避免重复提取和重复分析）

所有时间戳统一用毫秒整数。JSON 字段存 TEXT。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------- 工具函数 ----------

_SUFFIX_PATTERNS = [
    "有限责任公司", "股份有限公司", "有限公司", "科技有限公司",
    "科技", "公司",
    " incorporated", " inc.", " inc", " ltd.", " ltd", " llc",
    " co., ltd.", " co.,ltd.", " co., ltd", " co.ltd", " co.", " co",
    " corp.", " corp", " corporation", " gmbh", " s.a.",
]
_PUNCT_RE = re.compile(r"[\s\.\-_/\\,，。！？!?\"'“”‘’()（）\[\]【】<>《》]+")


def normalize_company_name(name: str) -> str:
    """归一化公司名用于去重和黑名单匹配。

    规则：小写 → 去常见公司后缀 → 去所有标点空白 → 返回。
    空字符串 / None → 返回空串。
    """
    if not name:
        return ""
    n = str(name).lower().strip()
    # 反复剥离后缀（处理 "XX 科技有限公司" 两次）
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIX_PATTERNS:
            if n.endswith(suf):
                n = n[: -len(suf)].strip()
                changed = True
    n = _PUNCT_RE.sub("", n)
    return n


def _now_ms() -> int:
    return int(time.time() * 1000)


def _jdumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _jloads(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


# ---------- Schema ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_sessions (
    session_id   TEXT PRIMARY KEY,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    mode         TEXT NOT NULL,
    chats        TEXT,          -- JSON: [{wxid, name}]
    total_items  INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'completed',  -- running | completed | failed
    note         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON scan_sessions(created_at DESC);

CREATE TABLE IF NOT EXISTS scan_results (
    result_id         TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    chat              TEXT,
    company_name      TEXT,
    company_key       TEXT,   -- normalized
    record_type       TEXT,
    confidence        TEXT,
    data              TEXT NOT NULL,   -- JSON: full analysis dict
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending | written | dismissed | failed
    bitable_record_id TEXT,
    error             TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES scan_sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_results_session ON scan_results(session_id);
CREATE INDEX IF NOT EXISTS idx_results_status  ON scan_results(status);
CREATE INDEX IF NOT EXISTS idx_results_key     ON scan_results(company_key);

CREATE TABLE IF NOT EXISTS blacklist (
    key_type   TEXT NOT NULL,   -- 'name' | 'pdf_md5'
    key_value  TEXT NOT NULL,
    label      TEXT,             -- 人类可读的原始名字
    reason     TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (key_type, key_value)
);

CREATE TABLE IF NOT EXISTS pdf_cache (
    md5             TEXT PRIMARY KEY,
    title           TEXT,
    text_length     INTEGER,
    extracted_text  TEXT,            -- 可为空（文本很大可只存分析结果）
    is_bp           INTEGER,         -- 1 / 0 / NULL
    classification  TEXT,            -- JSON
    analysis        TEXT,            -- JSON
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
"""


# ---------- Store 类 ----------

class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        with self._lock:
            self._conn.executescript(SCHEMA)
        logger.info(f"[Store] Opened {self.db_path}")

    # ---------- 扫描 Session ----------

    def create_session(self, mode: str, chats: list[dict], note: str = "") -> str:
        session_id = f"scan_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        now = _now_ms()
        with self._lock:
            self._conn.execute(
                "INSERT INTO scan_sessions (session_id, created_at, updated_at, mode, chats, status, note) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                (session_id, now, now, mode, _jdumps(chats), note),
            )
        return session_id

    def finalize_session(self, session_id: str, status: str = "completed"):
        with self._lock:
            cnt = self._conn.execute(
                "SELECT COUNT(*) FROM scan_results WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            self._conn.execute(
                "UPDATE scan_sessions SET status=?, total_items=?, updated_at=? WHERE session_id=?",
                (status, cnt, _now_ms(), session_id),
            )

    def get_session(self, session_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scan_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, "
                "  (SELECT COUNT(*) FROM scan_results r WHERE r.session_id=s.session_id AND r.status='pending') AS pending_count, "
                "  (SELECT COUNT(*) FROM scan_results r WHERE r.session_id=s.session_id AND r.status='written') AS written_count, "
                "  (SELECT COUNT(*) FROM scan_results r WHERE r.session_id=s.session_id AND r.status='dismissed') AS dismissed_count "
                "FROM scan_sessions s ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = self._row_to_session(r)
            d["pending_count"] = r["pending_count"]
            d["written_count"] = r["written_count"]
            d["dismissed_count"] = r["dismissed_count"]
            out.append(d)
        return out

    def delete_session(self, session_id: str):
        with self._lock:
            self._conn.execute("DELETE FROM scan_results WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM scan_sessions WHERE session_id=?", (session_id,))

    # ---------- 扫描结果 ----------

    def add_result(
        self,
        session_id: str,
        chat: str,
        analysis: dict,
    ) -> str:
        result_id = f"res_{uuid.uuid4().hex[:12]}"
        now = _now_ms()
        company = analysis.get("company_name", "") or ""
        with self._lock:
            self._conn.execute(
                "INSERT INTO scan_results "
                "(result_id, session_id, chat, company_name, company_key, record_type, confidence, data, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    result_id, session_id, chat, company,
                    normalize_company_name(company),
                    analysis.get("record_type", ""),
                    analysis.get("confidence", ""),
                    _jdumps(analysis),
                    now, now,
                ),
            )
        return result_id

    def get_result(self, result_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM scan_results WHERE result_id=?", (result_id,)
            ).fetchone()
        return self._row_to_result(row) if row else None

    def get_results(
        self,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict]:
        sql = "SELECT * FROM scan_results WHERE 1=1"
        args: list[Any] = []
        if session_id:
            sql += " AND session_id=?"
            args.append(session_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_result(r) for r in rows]

    def update_result_status(
        self,
        result_id: str,
        status: str,
        bitable_record_id: Optional[str] = None,
        error: Optional[str] = None,
    ):
        with self._lock:
            self._conn.execute(
                "UPDATE scan_results SET status=?, bitable_record_id=COALESCE(?, bitable_record_id), "
                "error=?, updated_at=? WHERE result_id=?",
                (status, bitable_record_id, error, _now_ms(), result_id),
            )

    def update_result_fields(self, result_id: str, fields: dict):
        """inline 编辑：merge 到 data JSON 里。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM scan_results WHERE result_id=?", (result_id,)
            ).fetchone()
            if not row:
                return False
            data = _jloads(row["data"]) or {}
            data.update(fields)
            company = data.get("company_name", "")
            self._conn.execute(
                "UPDATE scan_results SET data=?, company_name=?, company_key=?, "
                "record_type=?, confidence=?, updated_at=? WHERE result_id=?",
                (
                    _jdumps(data), company, normalize_company_name(company),
                    data.get("record_type", ""), data.get("confidence", ""),
                    _now_ms(), result_id,
                ),
            )
        return True

    def delete_result(self, result_id: str):
        with self._lock:
            self._conn.execute("DELETE FROM scan_results WHERE result_id=?", (result_id,))

    # ---------- 黑名单 ----------

    def add_blacklist(self, key_type: str, key_value: str, label: str = "", reason: str = ""):
        if not key_value:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO blacklist (key_type, key_value, label, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_type, key_value, label, reason, _now_ms()),
            )

    def blacklist_name(self, company_name: str, reason: str = ""):
        key = normalize_company_name(company_name)
        if key:
            self.add_blacklist("name", key, label=company_name, reason=reason)

    def blacklist_pdf(self, md5: str, title: str = "", reason: str = ""):
        if md5:
            self.add_blacklist("pdf_md5", md5, label=title, reason=reason)

    def is_name_blacklisted(self, company_name: str) -> bool:
        key = normalize_company_name(company_name)
        if not key:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM blacklist WHERE key_type='name' AND key_value=?", (key,)
            ).fetchone()
        return row is not None

    def is_pdf_blacklisted(self, md5: str) -> bool:
        if not md5:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM blacklist WHERE key_type='pdf_md5' AND key_value=?", (md5,)
            ).fetchone()
        return row is not None

    def list_blacklist(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM blacklist ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_blacklist(self, key_type: str, key_value: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM blacklist WHERE key_type=? AND key_value=?",
                (key_type, key_value),
            )

    # ---------- PDF 缓存 ----------

    def get_pdf_cache(self, md5: str) -> Optional[dict]:
        if not md5:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pdf_cache WHERE md5=?", (md5,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["classification"] = _jloads(d.get("classification"))
        d["analysis"] = _jloads(d.get("analysis"))
        return d

    def set_pdf_cache(
        self,
        md5: str,
        title: str,
        text: Optional[str] = None,
        is_bp: Optional[bool] = None,
        classification: Optional[dict] = None,
        analysis: Optional[dict] = None,
    ):
        if not md5:
            return
        now = _now_ms()
        text_len = len(text) if text else 0
        is_bp_int = None if is_bp is None else (1 if is_bp else 0)
        with self._lock:
            existing = self._conn.execute(
                "SELECT md5 FROM pdf_cache WHERE md5=?", (md5,)
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE pdf_cache SET title=COALESCE(?,title), "
                    "extracted_text=COALESCE(?, extracted_text), "
                    "text_length=COALESCE(?, text_length), "
                    "is_bp=COALESCE(?, is_bp), "
                    "classification=COALESCE(?, classification), "
                    "analysis=COALESCE(?, analysis), "
                    "updated_at=? WHERE md5=?",
                    (
                        title or None,
                        text,
                        text_len if text else None,
                        is_bp_int,
                        _jdumps(classification) if classification is not None else None,
                        _jdumps(analysis) if analysis is not None else None,
                        now, md5,
                    ),
                )
            else:
                self._conn.execute(
                    "INSERT INTO pdf_cache (md5, title, extracted_text, text_length, is_bp, classification, analysis, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        md5, title, text, text_len, is_bp_int,
                        _jdumps(classification) if classification is not None else None,
                        _jdumps(analysis) if analysis is not None else None,
                        now, now,
                    ),
                )

    def pdf_cache_stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM pdf_cache").fetchone()[0]
            bp = self._conn.execute("SELECT COUNT(*) FROM pdf_cache WHERE is_bp=1").fetchone()[0]
        return {"total": total, "bp": bp}

    # ---------- Row 转 dict ----------

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["chats"] = _jloads(d.get("chats")) or []
        return d

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["data"] = _jloads(d.get("data")) or {}
        return d

    def close(self):
        with self._lock:
            self._conn.close()


# ---------- 全局单例 ----------

_STORE: Optional[Store] = None
_STORE_LOCK = threading.Lock()


def get_store(db_path: Optional[str | Path] = None) -> Store:
    """获取全局 Store 单例。首次调用时用 db_path 初始化。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            if db_path is None:
                db_path = Path(__file__).resolve().parent.parent / "data" / "scanner.db"
            _STORE = Store(db_path)
        return _STORE
