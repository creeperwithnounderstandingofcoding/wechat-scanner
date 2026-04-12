"""ScannerCore — 与 UI 无关的业务门面

封装：
- 微信 DB 加载 / 刷新
- 会话列表 / 预览
- 扫描执行（smart/pdf/full/area）+ 结果持久化
- 结果的写入 / 编辑 / 忽略
- 历史 / 黑名单 / 本地保存

所有方法返回 dataclass 或原生 dict；不引用 Flask / HTTP / Card 等 UI 概念。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import config
from extractor.db_reader import WeChatDBReader
from extractor.wal_merger import merge_wal_into_db
from processor.pipeline import (
    area_scan,
    full_scan,
    smart_scan,
    scan_pdfs_in_chat,
    write_to_bitable,
)
from storage import Store, get_store

from .models import (
    HistoryView,
    RefreshResult,
    ScanRunItem,
    ScanRunResult,
    WriteBatchResult,
    WriteItemResult,
)

logger = logging.getLogger(__name__)

LogCallback = Callable[[str], None]
ReaderFactory = Callable[[], WeChatDBReader]


class ScannerCore:
    """业务门面。线程安全不要求；调用方（Flask/Bot）自行保证并发。"""

    def __init__(
        self,
        store: Optional[Store] = None,
        reader_factory: Optional[ReaderFactory] = None,
    ):
        self.store: Store = store or get_store()
        self._reader_factory: ReaderFactory = (
            reader_factory or (lambda: WeChatDBReader(config.DECRYPTED_DB_DIR))
        )
        self.reader: Optional[WeChatDBReader] = None
        self.sessions_cache: list[dict] = []

        # 飞书扫描任务（见 scan_feishu_* 方法）。单例进程内的 in-memory 状态。
        # key: job_id -> FeishuScanJob
        self._feishu_jobs: dict[str, "FeishuScanJob"] = {}
        self._feishu_jobs_lock = threading.Lock()

    # ============================================================
    # 初始化 / 数据库刷新
    # ============================================================

    def init_reader(self) -> int:
        """首次加载 reader 和会话列表。返回会话数。"""
        self.reader = self._reader_factory()
        self.reader.get_contacts()
        self.sessions_cache = self.reader.get_sessions(limit=0)
        self._preload_message_counts(self.sessions_cache)
        logger.info(f"[Core] Loaded {len(self.sessions_cache)} sessions")
        return len(self.sessions_cache)

    def _preload_message_counts(self, sessions: list[dict]) -> None:
        """批量预加载消息数，避免逐个连接 DB。"""
        wxid_tables: dict[str, str] = {}
        for s in sessions:
            wxid = s["wxid"]
            md5 = hashlib.md5(wxid.encode()).hexdigest()
            wxid_tables[wxid] = f"Msg_{md5}"
            s["msg_count"] = 0

        for i in range(4):
            db_path = os.path.join(config.DECRYPTED_DB_DIR, f"message/message_{i}.db")
            if not os.path.exists(db_path):
                continue
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                existing_tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    ).fetchall()
                }
                for s in sessions:
                    table = wxid_tables[s["wxid"]]
                    if table in existing_tables:
                        try:
                            c = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
                            if c:
                                s["msg_count"] += c[0]
                        except Exception:
                            pass
                conn.close()
            except Exception as e:
                logger.warning(f"[Core] 预加载 message_{i}.db 失败: {e}")

    def refresh_db(self) -> RefreshResult:
        """重新解密微信数据库（含 WAL 合并）+ 重新加载 reader。"""
        try:
            err = self._re_decrypt_dbs()
            if err:
                return RefreshResult(ok=False, error=err)
            count = self.init_reader()
            return RefreshResult(ok=True, session_count=count)
        except Exception as e:
            logger.exception("[Core] refresh_db failed")
            return RefreshResult(ok=False, error=str(e))

    def _re_decrypt_dbs(self) -> str:
        """用现有密钥重新解密 DB + 合并 WAL。返回错误消息或空串。"""
        decrypt_dir = Path.home() / "Desktop/feishu/wechat-decrypt"
        keys_file = decrypt_dir / "all_keys.json"
        if not keys_file.exists():
            return "密钥文件不存在，请先运行 find_all_keys_macos"

        out_dir = config.DECRYPTED_DB_DIR
        for sub in ["message", "contact", "session"]:
            d = Path(out_dir) / sub
            if d.exists():
                for f in d.glob("*.db"):
                    f.unlink(missing_ok=True)
                    logger.info(f"[Core] 删除旧解密文件: {f}")

        result = subprocess.run(
            [sys.executable, "decrypt_db.py"],
            cwd=str(decrypt_dir),
            capture_output=True, text=True, timeout=300,
        )
        logger.info(f"[Core] decrypt_db.py stdout:\n{result.stdout[-500:]}")
        if result.returncode != 0:
            logger.error(f"[Core] decrypt_db.py failed: {result.stderr[-300:]}")
            return f"解密失败: {result.stderr[-200:]}"

        try:
            self._merge_wal_pages(decrypt_dir, keys_file, Path(out_dir))
        except Exception as e:
            logger.warning(f"[Core] WAL 合并失败（非致命）: {e}")
        return ""

    def _merge_wal_pages(self, decrypt_dir: Path, keys_file: Path, out_dir: Path) -> None:
        # Import helpers from wechat-decrypt/. Tricky: that project has its own
        # config.py (with load_config()) and our wechat-scanner/config.py is
        # already cached in sys.modules as 'config'. A naive sys.path.insert +
        # `from decrypt_db import ...` will resolve `from config import load_config`
        # inside decrypt_db.py to OUR config module and crash with
        #   ImportError: cannot import name 'load_config' from 'config'
        # → WAL merge silently fails → refresh looks like it did nothing.
        #
        # Fix: temporarily pop 'config' / 'key_utils' from sys.modules so the
        # subsequent import loads the ones next to decrypt_db.py, then restore
        # our originals so the rest of ScannerCore keeps working normally.
        decrypt_dir_str = str(decrypt_dir)
        saved_config = sys.modules.pop("config", None)
        saved_key_utils = sys.modules.pop("key_utils", None)
        saved_decrypt_db = sys.modules.pop("decrypt_db", None)
        path_inserted = False
        if decrypt_dir_str not in sys.path:
            sys.path.insert(0, decrypt_dir_str)
            path_inserted = True
        try:
            from key_utils import get_key_info, strip_key_metadata  # noqa: E402
            from decrypt_db import decrypt_page  # noqa: E402
        finally:
            # Restore our config / key_utils so the rest of the bot/scanner
            # continues to import the correct module. decrypt_db can stay
            # cached (it only exposes decrypt_page which takes explicit args).
            if path_inserted:
                try:
                    sys.path.remove(decrypt_dir_str)
                except ValueError:
                    pass
            if saved_config is not None:
                sys.modules["config"] = saved_config
            else:
                sys.modules.pop("config", None)
            if saved_key_utils is not None:
                sys.modules["key_utils"] = saved_key_utils
            # leave decrypt_db cached (harmless) or restore
            if saved_decrypt_db is not None:
                sys.modules["decrypt_db"] = saved_decrypt_db

        with open(keys_file, encoding="utf-8") as f:
            keys = strip_key_metadata(json.load(f))
        db_dir = Path(json.load(open(decrypt_dir / "config.json"))["db_dir"])

        target_dbs = [
            "message/message_0.db", "message/message_1.db",
            "message/message_2.db", "message/message_3.db",
            "contact/contact.db", "session/session.db",
        ]
        total_merged = 0
        for rel in target_dbs:
            src_db = db_dir / rel
            wal_path = src_db.with_name(src_db.name + "-wal")
            decrypted_db = out_dir / rel
            # Always log per-target visibility — a silent continue here was
            # exactly how the refresh bug hid for so long.
            if not wal_path.exists():
                logger.info(f"[Core] WAL {rel} 跳过: wal 文件不存在 ({wal_path})")
                continue
            if not decrypted_db.exists():
                logger.info(f"[Core] WAL {rel} 跳过: 解密 DB 不存在 ({decrypted_db})")
                continue
            wal_size = wal_path.stat().st_size
            key_info = get_key_info(keys, rel)
            if not key_info:
                logger.info(f"[Core] WAL {rel} 跳过: 无 key (wal={wal_size}B)")
                continue
            enc_key = bytes.fromhex(key_info["enc_key"])

            try:
                res = merge_wal_into_db(wal_path, decrypted_db, enc_key, decrypt_page)
            except Exception as e:
                logger.warning(f"[Core] WAL {rel} 合并异常: {e}")
                continue

            if res.skipped_reason:
                logger.info(
                    f"[Core] WAL {rel} 跳过: {res.skipped_reason} "
                    f"(wal={wal_size}B, 解密={res.frames_total}, 失败={res.decrypt_failures}, "
                    f"陈旧停止={res.stopped_on_stale})"
                )
                continue
            total_merged += res.frames_applied
            logger.info(
                f"[Core] WAL 合并: {rel} — 应用 {res.frames_applied} 页 "
                f"(wal={wal_size}B, 解密 {res.frames_total}, 失败 {res.decrypt_failures}, "
                f"陈旧停止={res.stopped_on_stale})"
            )
        logger.info(f"[Core] WAL 合并完成，共 {total_merged} 页")

    # ============================================================
    # 会话列表 / 预览
    # ============================================================

    def list_wechat_sessions(self) -> list[dict]:
        """对外的会话列表（精简字段，用于前端渲染）。"""
        return [
            {
                "wxid": s["wxid"],
                "display_name": s["display_name"],
                "is_group": s["is_group"],
                "last_msg_time_str": s.get("last_msg_time_str", ""),
                "msg_count": s.get("msg_count", 0),
                "summary": (s.get("summary") or "")[:80],
            }
            for s in self.sessions_cache
        ]

    def preview_chat(self, wxid: str, limit: int = 50, offset: int = 0) -> list[dict]:
        if not self.reader or not wxid:
            return []
        total = self.reader.get_message_count(wxid)
        # offset=0 表示"最新消息"，自动定位到末尾
        if offset == 0:
            actual_offset = max(0, total - limit)
        else:
            actual_offset = max(0, total - offset - limit)
        messages = self.reader.get_messages(wxid, limit=limit, offset=actual_offset)
        return [
            {
                "time_str": m.get("time_str", ""),
                "sender": m.get("sender", ""),
                "content": m.get("content", "")[:500],
                "msg_type": m.get("type_name", ""),
            }
            for m in messages
        ]

    # ============================================================
    # 扫描
    # ============================================================

    def scan(
        self,
        mode: str,
        chats: list[dict],
        log_cb: Optional[LogCallback] = None,
    ) -> ScanRunResult:
        """执行扫描。会自动创建 session、持久化所有结果。"""
        if not self.reader:
            raise RuntimeError("reader 未初始化，请先 init_reader()")

        def _log(msg: str) -> None:
            if log_cb:
                log_cb(msg)

        session_id = self.store.create_session(mode, chats)
        _log(f"📝 Session: {session_id}")

        def _persist(chat_name: str, analyses: list[dict]) -> list[ScanRunItem]:
            out: list[ScanRunItem] = []
            for r in analyses:
                try:
                    rid = self.store.add_result(session_id, chat_name, r)
                    out.append(ScanRunItem(result_id=rid, chat=chat_name, result=r))
                except Exception as e:
                    logger.error(f"[Core] add_result 失败: {e}")
            return out

        all_items: list[ScanRunItem] = []
        status = "completed"
        try:
            for chat in chats:
                wxid = chat["wxid"]
                name = chat["name"]

                if mode == "smart":
                    results = smart_scan(
                        self.reader, wxid, name,
                        chunk_size=config.BATCH_SIZE,
                        max_workers=3,
                        progress_cb=_log,
                    )
                    all_items.extend(_persist(name, results))
                    try:
                        pdf_results = scan_pdfs_in_chat(
                            self.reader, wxid, name,
                            max_workers=2,
                            progress_cb=_log,
                        )
                        all_items.extend(_persist(name, pdf_results))
                    except Exception as e:
                        _log(f"PDF 扫描出错: {e}")
                        logger.error(f"[Core] PDF scan error: {e}", exc_info=True)
                elif mode == "pdf":
                    results = scan_pdfs_in_chat(
                        self.reader, wxid, name,
                        max_workers=2,
                        progress_cb=_log,
                    )
                    all_items.extend(_persist(name, results))
                elif mode == "full":
                    results = full_scan(
                        self.reader, wxid, name,
                        batch_size=config.BATCH_SIZE,
                        progress_cb=_log,
                    )
                    all_items.extend(_persist(name, results))
                else:
                    result = area_scan(
                        self.reader, wxid, name,
                        window_size=config.CONTEXT_WINDOW_SIZE,
                        progress_cb=_log,
                    )
                    if result is None:
                        _log(f"{name}: 无项目相关内容")
                    elif isinstance(result, list):
                        all_items.extend(_persist(name, result))
                    elif isinstance(result, dict) and result.get("meeting_type") != "无关内容":
                        all_items.extend(_persist(name, [result]))
        except Exception as e:
            _log(f"扫描出错: {e}")
            logger.error(f"[Core] Scan error: {e}", exc_info=True)
            status = "failed"
        finally:
            self.store.finalize_session(session_id, status=status)

        return ScanRunResult(session_id=session_id, results=all_items, status=status)

    # ============================================================
    # 结果：写入 / 编辑 / 忽略 / 查询
    # ============================================================

    def write_results(self, result_ids: list[str]) -> WriteBatchResult:
        """把一批 result_id 对应的结果写入飞书 bitable。"""
        items: list[WriteItemResult] = []
        success = 0
        total = 0
        for rid in result_ids:
            row = self.store.get_result(rid)
            if not row:
                continue
            total += 1
            chat = row.get("chat", "")
            analysis = row.get("data") or {}
            try:
                res = write_to_bitable(analysis, chat)
            except Exception as e:
                res = {"ok": False, "error": str(e)[:200]}

            if res.get("ok"):
                self.store.update_result_status(
                    rid, "written",
                    bitable_record_id=res.get("record_id", ""),
                )
                success += 1
                items.append(WriteItemResult(
                    result_id=rid, ok=True,
                    record_id=res.get("record_id", ""),
                    company_name=res.get("company_name") or analysis.get("company_name", ""),
                ))
            else:
                self.store.update_result_status(rid, "failed", error=res.get("error", ""))
                items.append(WriteItemResult(
                    result_id=rid, ok=False,
                    error=res.get("error", "unknown"),
                    company_name=analysis.get("company_name", ""),
                ))
        return WriteBatchResult(success=success, total=total, items=items)

    def edit_result(self, result_id: str, fields: dict) -> Optional[dict]:
        """inline 编辑。返回更新后的 row（原生 dict）或 None。"""
        ok = self.store.update_result_fields(result_id, fields)
        if not ok:
            return None
        return self.store.get_result(result_id)

    def dismiss_result(self, result_id: str, blacklist: bool = True) -> bool:
        """忽略一条结果；可选地加入黑名单。"""
        row = self.store.get_result(result_id)
        if not row:
            return False
        self.store.update_result_status(result_id, "dismissed")
        if blacklist:
            cname = row.get("company_name", "")
            if cname:
                self.store.blacklist_name(
                    cname, reason=f"dismissed from {row.get('session_id', '')}"
                )
            analysis = row.get("data") or {}
            md5 = analysis.get("_pdf_md5")
            if md5:
                self.store.blacklist_pdf(
                    md5, title=analysis.get("file_name", ""), reason="dismissed"
                )
        return True

    def get_session_results(
        self,
        session_id: str,
        status: Optional[str] = "pending",
    ) -> list[dict]:
        """取 session 下的结果。status=None → 全部。"""
        if status == "all":
            status = None
        rows = self.store.get_results(session_id=session_id, status=status)
        return [
            {
                "result_id": r["result_id"],
                "chat": r["chat"],
                "status": r["status"],
                "result": r["data"],
            }
            for r in rows
        ]

    # ============================================================
    # 历史 / 黑名单 / 本地保存
    # ============================================================

    def list_history(self, limit: int = 30) -> HistoryView:
        return HistoryView(
            sessions=self.store.list_sessions(limit=limit),
            pdf_stats=self.store.pdf_cache_stats(),
        )

    def list_blacklist(self) -> list[dict]:
        return self.store.list_blacklist()

    def remove_blacklist(self, key_type: str, key_value: str) -> None:
        self.store.remove_blacklist(key_type, key_value)

    def save_local(
        self,
        result_ids: list[str],
        output_dir: Optional[Path] = None,
    ) -> tuple[Path, int]:
        """把一批 result 导出成本地 JSON。返回 (path, count)。"""
        output_dir = output_dir or Path("data")
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = []
        for rid in result_ids:
            row = self.store.get_result(rid)
            if row:
                payload.append({
                    "result_id": rid,
                    "chat": row.get("chat", ""),
                    "result": row.get("data", {}),
                })
        output_file = output_dir / f"scan_{int(time.time())}.json"
        output_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output_file, len(payload)

    # ============================================================
    # 飞书扫描（Phase 3b Level 1，通过 bridge.bot_services 复用 bot 的 services）
    # ============================================================

    def list_feishu_chats(self) -> list[dict]:
        """列出 bot 能访问的飞书群聊。返回 [{chat_id, name, description, ...}]"""
        from bridge.bot_services import BotBridge, BridgeError

        try:
            return BotBridge().list_chats()
        except BridgeError as e:
            logger.error(f"[Core] list_feishu_chats failed: {e}")
            raise

    def start_feishu_scan(self, chat_id: str, chat_name: str = "", user_api=None) -> str:
        """启动一次异步飞书历史扫描。返回 job_id。

        扫描跑在后台线程里，调用方通过 get_feishu_scan_status(job_id) 轮询/流式消费。
        同一个 chat_id 允许重复启动（bot 的 scanner 内部有 is_url_seen 去重）。

        user_api: 可选 FeishuUserAPI 实例（Phase E 多用户），当前登录用户的 OAuth token。
        """
        job_id = uuid.uuid4().hex[:12]
        job = FeishuScanJob(
            job_id=job_id,
            chat_id=chat_id,
            chat_name=chat_name,
            status="running",
            started_at=time.time(),
        )
        with self._feishu_jobs_lock:
            self._feishu_jobs[job_id] = job

        def _runner():
            # 外层包一层以便捕获所有异常进状态。
            try:
                self._feishu_scan_worker(job, user_api=user_api)
            except Exception as e:
                logger.exception(f"[Core] feishu scan job {job_id} crashed")
                job.append_log(f"❌ 任务崩溃: {e}")
                job.status = "failed"
                job.error = str(e)
                job.finished_at = time.time()

        t = threading.Thread(target=_runner, name=f"feishu-scan-{job_id}", daemon=True)
        t.start()
        job.thread = t
        return job_id

    def _feishu_scan_worker(self, job: "FeishuScanJob", user_api=None) -> None:
        from bridge.bot_services import BotBridge

        who = ""
        if user_api:
            who = " (使用登录用户 token)"
        job.append_log(f"🚀 启动飞书扫描：{job.chat_name or job.chat_id}{who}")

        def _cb(msg: str) -> None:
            # bot 的 services 回调。注意 msg 可能是多行。
            job.append_log(msg)

        bridge = BotBridge()
        try:
            bridge.run_history_scan(job.chat_id, _cb, user_api=user_api)
        except Exception as e:
            job.append_log(f"❌ 扫描失败: {e}")
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            return

        job.status = "completed"
        job.finished_at = time.time()
        job.append_log("✅ 任务已完成")

    def get_feishu_scan_status(
        self,
        job_id: str,
        since_index: int = 0,
    ) -> Optional[dict]:
        """轮询扫描状态。since_index 表示调用方已经看到的日志条数，
        本次只返回之后的增量日志，避免重复传输。"""
        with self._feishu_jobs_lock:
            job = self._feishu_jobs.get(job_id)
        if not job:
            return None
        return job.snapshot(since_index)

    def cancel_feishu_scan(self, job_id: str) -> bool:
        with self._feishu_jobs_lock:
            job = self._feishu_jobs.get(job_id)
        if not job:
            return False
        try:
            from bridge.bot_services import BotBridge
            BotBridge().cancel_scan()
            job.append_log("⏹ 已发出取消信号")
            return True
        except Exception as e:
            job.append_log(f"取消失败: {e}")
            return False


# ============================================================
# 飞书扫描任务数据结构（放在模块底部，避免前向引用循环）
# ============================================================


@dataclass
class FeishuScanJob:
    """一次异步飞书扫描的 in-memory 状态。

    log 是一个 append-only 列表；get_status(since_index) 返回增量切片。
    用 list + index 而不是 queue，是因为 Web UI 可能多次刷新页面重新拿日志。
    """
    job_id: str
    chat_id: str
    chat_name: str
    status: str  # running / completed / failed
    started_at: float
    finished_at: Optional[float] = None
    error: str = ""
    thread: Optional[threading.Thread] = None
    _log: list[tuple[float, str]] = field(default_factory=list)
    _log_lock: threading.Lock = field(default_factory=threading.Lock)

    def append_log(self, msg: str) -> None:
        with self._log_lock:
            # bot 的 callback 传来的字符串可能是多行，展开成单行方便前端渲染
            for line in str(msg).splitlines() or [str(msg)]:
                if line.strip():
                    self._log.append((time.time(), line))

    def snapshot(self, since_index: int = 0) -> dict:
        with self._log_lock:
            total = len(self._log)
            new_lines = [
                {"ts": ts, "msg": msg}
                for ts, msg in self._log[max(0, since_index):]
            ]
        return {
            "job_id": self.job_id,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "total_logs": total,
            "new_logs": new_lines,
        }
