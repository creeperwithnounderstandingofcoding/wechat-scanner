"""WAL 合并器

从加密的 SQLite WAL 文件中解密有效帧，合并到已解密的主 DB 中。

背景：
- WeChat 4.x 用 SQLCipher 存聊天记录，最新消息常常还在 WAL 里没 checkpoint
- 纯解密主 DB 会丢最新数据；需要同时处理 WAL
- WAL 是环形缓冲区：checkpoint 后旧帧仍留在文件里，但 salt 和当前 header 不匹配
- SQLite 官方 WAL 恢复逻辑：从头读帧，第一个 salt 不匹配的地方就停止
- 如果不做 salt 校验，会把陈旧帧覆盖到已解密的新 main.db 上，导致 "刷新后看到旧消息"

本模块是 web_ui.py 里 _merge_wal_pages 的抽离版本，纯函数 + 可测。
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 常量
PAGE_SIZE = 4096
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_MAGIC_LE = 0x377F0682
WAL_MAGIC_BE = 0x377F0683

# 调用方提供：传入 (enc_key, page_data, pgno) 返回明文页（SQLCipher 解密）
DecryptPageFn = Callable[[bytes, bytes, int], bytes]


@dataclass
class WalMergeResult:
    db_rel_path: str
    frames_total: int         # 成功解密的帧数
    frames_applied: int       # 实际写入 DB 的不同页数
    decrypt_failures: int
    stopped_on_stale: bool    # 是否因 salt 不匹配而提前停止
    skipped_reason: str = ""  # 如果整个文件被跳过，原因

    @property
    def ok(self) -> bool:
        return not self.skipped_reason and self.frames_applied > 0


def merge_wal_into_db(
    wal_path: Path,
    decrypted_db_path: Path,
    enc_key: bytes,
    decrypt_page: DecryptPageFn,
    page_size: int = PAGE_SIZE,
) -> WalMergeResult:
    """把单个 WAL 文件中的有效帧解密并合并到已解密的主 DB。

    wal_path: 源 -wal 文件（加密）
    decrypted_db_path: 目标已解密 DB 文件（会被修改）
    enc_key: SQLCipher 原始加密 key
    decrypt_page: 解密函数
    page_size: DB 页大小（SQLCipher WeChat 用 4096）

    行为：
    - 不存在 / header 损坏 / magic 不对 / 已解密 DB 不存在 → 返回 skipped_reason，不抛
    - 遇到第一个 salt 不匹配的帧 → 停止读取（和 SQLite 自身 WAL 恢复一致）
    - 同一页多次出现时取最后一次（最新版本）
    - 超出原 DB 末尾的页以 0 填充空洞后追加
    """
    rel = decrypted_db_path.name
    if not wal_path.exists():
        return WalMergeResult(rel, 0, 0, 0, False, "wal 文件不存在")
    if not decrypted_db_path.exists():
        return WalMergeResult(rel, 0, 0, 0, False, "目标 DB 未解密")
    if wal_path.stat().st_size <= WAL_HEADER_SIZE:
        return WalMergeResult(rel, 0, 0, 0, False, "wal 只有 header")

    with open(wal_path, "rb") as wf:
        header = wf.read(WAL_HEADER_SIZE)
        if len(header) < WAL_HEADER_SIZE:
            return WalMergeResult(rel, 0, 0, 0, False, "wal header 损坏")

        magic = struct.unpack(">I", header[:4])[0]
        if magic not in (WAL_MAGIC_LE, WAL_MAGIC_BE):
            return WalMergeResult(rel, 0, 0, 0, False, f"非 SQLite WAL (magic=0x{magic:08x})")

        wal_page_size = struct.unpack(">I", header[8:12])[0]
        if wal_page_size != page_size:
            return WalMergeResult(
                rel, 0, 0, 0, False,
                f"wal page_size={wal_page_size} 与 {page_size} 不符",
            )

        # header 中的 salt1+salt2（共 8 字节），每次 checkpoint 后 WAL 重置会更新
        header_salt = header[16:24]

        frames: dict[int, bytes] = {}  # pgno -> decrypted page
        total_decrypted = 0
        decrypt_fail = 0
        stopped_on_stale = False

        while True:
            frame_header = wf.read(WAL_FRAME_HEADER_SIZE)
            if len(frame_header) < WAL_FRAME_HEADER_SIZE:
                break
            pgno = struct.unpack(">I", frame_header[:4])[0]
            frame_salt = frame_header[8:16]
            page_data = wf.read(page_size)
            if len(page_data) < page_size:
                break

            # 核心保护：salt 不匹配 → 陈旧帧，停止
            if frame_salt != header_salt:
                stopped_on_stale = True
                break

            if pgno == 0 or pgno > 10_000_000:  # 损坏保护
                break

            try:
                decrypted = decrypt_page(enc_key, page_data, pgno)
                frames[pgno] = decrypted
                total_decrypted += 1
            except Exception as e:
                decrypt_fail += 1
                if decrypt_fail <= 2:
                    logger.debug(f"[WAL] {rel} 解密 pgno={pgno} 失败: {e}")

    if not frames:
        # 没有可应用的帧。两种典型情况：
        # (a) 第一帧 salt 就不匹配 → stopped_on_stale=True，说明 WAL 刚被
        #     checkpoint 过、header 已更新但还没写新帧（或 WeChat 没有 flush）。
        # (b) 所有帧解密都失败 → 通常是 key 错了。
        if stopped_on_stale:
            reason = "第一帧 salt 不匹配（WAL 刚 checkpoint，无新帧）"
        elif decrypt_fail > 0 and total_decrypted == 0:
            reason = f"所有帧解密失败 ({decrypt_fail})"
        else:
            reason = "无可应用帧"
        return WalMergeResult(
            rel, total_decrypted, 0, decrypt_fail, stopped_on_stale,
            skipped_reason=reason,
        )

    # 写回 DB
    db_size = decrypted_db_path.stat().st_size
    max_existing_pgno = db_size // page_size
    applied = 0
    with open(decrypted_db_path, "r+b") as dbf:
        for pgno, page in sorted(frames.items()):
            offset = (pgno - 1) * page_size
            if pgno <= max_existing_pgno:
                dbf.seek(offset)
                dbf.write(page)
            else:
                dbf.seek(0, 2)  # 末尾
                current_end = dbf.tell()
                if current_end < offset:
                    dbf.write(b"\x00" * (offset - current_end))
                dbf.seek(offset)
                dbf.write(page)
            applied += 1

    return WalMergeResult(
        rel, total_decrypted, applied, decrypt_fail, stopped_on_stale,
    )


def merge_all_wals(
    db_pairs: list[tuple[Path, Path]],
    key_lookup: Callable[[str], Optional[bytes]],
    decrypt_page: DecryptPageFn,
    page_size: int = PAGE_SIZE,
) -> list[WalMergeResult]:
    """批量合并多个 DB 的 WAL。

    db_pairs: [(源加密 DB 路径（其 -wal 将被读取）, 已解密 DB 路径)]
    key_lookup: 传入相对路径或名称，返回 enc_key 或 None
    """
    results: list[WalMergeResult] = []
    for src_db, dst_db in db_pairs:
        wal_path = src_db.with_name(src_db.name + "-wal")
        rel = dst_db.name
        key = key_lookup(str(src_db))
        if key is None:
            results.append(WalMergeResult(rel, 0, 0, 0, False, "无 key"))
            continue
        try:
            res = merge_wal_into_db(wal_path, dst_db, key, decrypt_page, page_size)
        except Exception as e:
            logger.exception(f"[WAL] 合并 {rel} 出错: {e}")
            res = WalMergeResult(rel, 0, 0, 0, False, f"异常: {e}")
        results.append(res)
    return results
