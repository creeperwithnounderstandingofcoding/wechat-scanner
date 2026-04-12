"""WAL merger 单测

用伪造的 WAL 文件 + identity 解密函数验证：
1. salt 匹配的帧会被应用
2. salt 不匹配的帧会被跳过并停止
3. 同一 pgno 多次出现取最后一次
4. 无效 magic / 损坏 header 被安全跳过
5. 目标 DB 页会被正确覆盖
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from extractor.wal_merger import (
    PAGE_SIZE,
    WAL_MAGIC_LE,
    merge_wal_into_db,
)


def _identity_decrypt(_key: bytes, page: bytes, _pgno: int) -> bytes:
    """Pass-through 解密——测试用，不做真实 SQLCipher 解密。"""
    return page


def _make_wal(salt: bytes, frames: list[tuple[int, bytes, bytes]]) -> bytes:
    """造一个最小 WAL：header + frames。

    frames: [(pgno, frame_salt, page_data)]
    """
    assert len(salt) == 8
    header = b""
    header += struct.pack(">I", WAL_MAGIC_LE)              # magic
    header += struct.pack(">I", 3007000)                    # file format
    header += struct.pack(">I", PAGE_SIZE)                  # page size
    header += struct.pack(">I", 0)                          # checkpoint seq
    header += salt                                          # salt1 + salt2
    header += b"\x00" * 8                                   # checksum
    assert len(header) == 32
    body = b""
    for pgno, frame_salt, page in frames:
        fh = b""
        fh += struct.pack(">I", pgno)
        fh += struct.pack(">I", 0)       # commit marker (0 = not commit)
        fh += frame_salt                  # 8 bytes
        fh += b"\x00" * 8                 # checksum
        assert len(fh) == 24
        body += fh + page
    return header + body


def _page(marker: int) -> bytes:
    """造一个 PAGE_SIZE 大小的 page，第一个字节是 marker 方便断言。"""
    return bytes([marker]) + b"\x00" * (PAGE_SIZE - 1)


def _initial_db(pages: int) -> bytes:
    """造一个初始 DB，每页第一字节是 0xFF。"""
    return (b"\xFF" + b"\x00" * (PAGE_SIZE - 1)) * pages


def test_valid_frames_applied(tmp_path: Path):
    salt = b"SALT1234"
    wal = _make_wal(salt, [
        (1, salt, _page(0xAA)),
        (2, salt, _page(0xBB)),
    ])
    wal_path = tmp_path / "main.db-wal"
    wal_path.write_bytes(wal)
    db_path = tmp_path / "main.db"
    db_path.write_bytes(_initial_db(3))

    res = merge_wal_into_db(wal_path, db_path, b"key", _identity_decrypt)
    assert res.ok
    assert res.frames_applied == 2
    assert not res.stopped_on_stale

    data = db_path.read_bytes()
    assert data[0] == 0xAA              # page 1 被覆盖
    assert data[PAGE_SIZE] == 0xBB      # page 2 被覆盖
    assert data[2 * PAGE_SIZE] == 0xFF  # page 3 未动


def test_stale_salt_stops_reading(tmp_path: Path):
    """遇到 salt 不匹配立即停止，之后的帧全部忽略。"""
    salt_curr = b"CURRENT1"
    salt_old = b"OLDSALT1"
    wal = _make_wal(salt_curr, [
        (1, salt_curr, _page(0xAA)),      # 有效
        (2, salt_old,  _page(0xCC)),      # 陈旧 → 停止
        (3, salt_curr, _page(0xDD)),      # 不应被读取
    ])
    wal_path = tmp_path / "main.db-wal"
    wal_path.write_bytes(wal)
    db_path = tmp_path / "main.db"
    db_path.write_bytes(_initial_db(4))

    res = merge_wal_into_db(wal_path, db_path, b"key", _identity_decrypt)
    assert res.stopped_on_stale
    assert res.frames_applied == 1

    data = db_path.read_bytes()
    assert data[0] == 0xAA
    assert data[PAGE_SIZE] == 0xFF      # page 2 未被污染
    assert data[2 * PAGE_SIZE] == 0xFF  # page 3 未被读取


def test_same_pgno_takes_last(tmp_path: Path):
    salt = b"SALT1234"
    wal = _make_wal(salt, [
        (1, salt, _page(0x11)),
        (1, salt, _page(0x22)),
        (1, salt, _page(0x33)),
    ])
    wal_path = tmp_path / "main.db-wal"
    wal_path.write_bytes(wal)
    db_path = tmp_path / "main.db"
    db_path.write_bytes(_initial_db(1))

    res = merge_wal_into_db(wal_path, db_path, b"key", _identity_decrypt)
    assert res.ok
    assert res.frames_applied == 1  # 去重后只有一页
    assert db_path.read_bytes()[0] == 0x33


def test_bad_magic_is_skipped(tmp_path: Path):
    wal_path = tmp_path / "main.db-wal"
    wal_path.write_bytes(b"\x00" * 64)  # 全 0 → magic 错
    db_path = tmp_path / "main.db"
    db_path.write_bytes(_initial_db(1))

    res = merge_wal_into_db(wal_path, db_path, b"key", _identity_decrypt)
    assert not res.ok
    assert "magic" in res.skipped_reason
    assert db_path.read_bytes()[0] == 0xFF  # 未被触碰


def test_append_beyond_existing_db(tmp_path: Path):
    """新 pgno 超出原 DB 末尾时会扩展文件。"""
    salt = b"SALT1234"
    wal = _make_wal(salt, [(5, salt, _page(0xEE))])
    wal_path = tmp_path / "main.db-wal"
    wal_path.write_bytes(wal)
    db_path = tmp_path / "main.db"
    db_path.write_bytes(_initial_db(2))  # 只有 2 页

    res = merge_wal_into_db(wal_path, db_path, b"key", _identity_decrypt)
    assert res.ok
    assert res.frames_applied == 1

    data = db_path.read_bytes()
    assert len(data) == 5 * PAGE_SIZE
    assert data[4 * PAGE_SIZE] == 0xEE


def test_missing_db_returns_skipped(tmp_path: Path):
    wal_path = tmp_path / "main.db-wal"
    wal_path.write_bytes(_make_wal(b"SALT1234", [(1, b"SALT1234", _page(1))]))
    db_path = tmp_path / "main.db"  # 不创建
    res = merge_wal_into_db(wal_path, db_path, b"key", _identity_decrypt)
    assert not res.ok
    assert "未解密" in res.skipped_reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
