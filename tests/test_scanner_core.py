"""ScannerCore 单元测试

只测业务门面，不碰真实微信 DB / 飞书 API。使用临时 SQLite Store，
并 monkeypatch write_to_bitable 为假函数。
"""

from __future__ import annotations

import pytest

from core import ScannerCore
from core.models import (
    RefreshResult,
    ScanRunResult,
    WriteBatchResult,
)
from storage.store import Store


@pytest.fixture
def tmp_store(tmp_path):
    db = tmp_path / "scanner.db"
    return Store(db)


@pytest.fixture
def core(tmp_store):
    # 不提供 reader_factory → 它只会在 init_reader/scan 时才被调用
    return ScannerCore(store=tmp_store)


def _make_result(session_id, chat, analysis, store):
    return store.add_result(session_id, chat, analysis)


# ------------------------------------------------------------
# write_results
# ------------------------------------------------------------

def test_write_results_success(monkeypatch, core, tmp_store):
    session_id = tmp_store.create_session("smart", [{"wxid": "x", "name": "投资群"}])
    rid = _make_result(
        session_id, "投资群",
        {"company_name": "TestCo", "record_type": "会议"},
        tmp_store,
    )

    import core.scanner_core as sc
    monkeypatch.setattr(
        sc, "write_to_bitable",
        lambda analysis, chat: {"ok": True, "record_id": "rec_xyz", "company_name": "TestCo"},
    )

    batch = core.write_results([rid])
    assert isinstance(batch, WriteBatchResult)
    assert batch.success == 1
    assert batch.total == 1
    assert batch.items[0].ok is True
    assert batch.items[0].record_id == "rec_xyz"

    row = tmp_store.get_result(rid)
    assert row["status"] == "written"
    assert row["bitable_record_id"] == "rec_xyz"


def test_write_results_failure(monkeypatch, core, tmp_store):
    session_id = tmp_store.create_session("smart", [])
    rid = _make_result(session_id, "群", {"company_name": "BadCo"}, tmp_store)

    import core.scanner_core as sc
    monkeypatch.setattr(
        sc, "write_to_bitable",
        lambda a, c: {"ok": False, "error": "bitable down"},
    )

    batch = core.write_results([rid])
    assert batch.success == 0
    assert batch.total == 1
    assert batch.items[0].ok is False
    assert batch.items[0].error == "bitable down"

    row = tmp_store.get_result(rid)
    assert row["status"] == "failed"


def test_write_results_exception_is_caught(monkeypatch, core, tmp_store):
    session_id = tmp_store.create_session("smart", [])
    rid = _make_result(session_id, "群", {"company_name": "BoomCo"}, tmp_store)

    import core.scanner_core as sc
    def boom(a, c):
        raise RuntimeError("network explode")
    monkeypatch.setattr(sc, "write_to_bitable", boom)

    batch = core.write_results([rid])
    assert batch.success == 0
    assert batch.items[0].ok is False
    assert "network explode" in batch.items[0].error


def test_write_results_skips_missing_ids(core):
    batch = core.write_results(["res_doesnotexist"])
    assert batch.total == 0
    assert batch.success == 0
    assert batch.items == []


# ------------------------------------------------------------
# edit_result
# ------------------------------------------------------------

def test_edit_result_updates_fields(core, tmp_store):
    session_id = tmp_store.create_session("smart", [])
    rid = _make_result(
        session_id, "群",
        {"company_name": "OldName", "amount": 100},
        tmp_store,
    )

    row = core.edit_result(rid, {"company_name": "NewName", "amount": 999})
    assert row is not None
    assert row["company_name"] == "NewName"
    assert row["data"]["amount"] == 999


def test_edit_result_missing_returns_none(core):
    assert core.edit_result("res_nope", {"x": 1}) is None


# ------------------------------------------------------------
# dismiss_result
# ------------------------------------------------------------

def test_dismiss_result_blacklists_company(core, tmp_store):
    session_id = tmp_store.create_session("smart", [])
    rid = _make_result(
        session_id, "群",
        {"company_name": "SpamCo"},
        tmp_store,
    )

    ok = core.dismiss_result(rid, blacklist=True)
    assert ok is True

    row = tmp_store.get_result(rid)
    assert row["status"] == "dismissed"

    bl = tmp_store.list_blacklist()
    assert any("SpamCo" in (b.get("key_value") or "") or
               "spamco" in (b.get("key_value") or "").lower()
               for b in bl)


def test_dismiss_result_blacklists_pdf_md5(core, tmp_store):
    session_id = tmp_store.create_session("smart", [])
    rid = _make_result(
        session_id, "群",
        {"company_name": "", "_pdf_md5": "abc123", "file_name": "deck.pdf"},
        tmp_store,
    )

    ok = core.dismiss_result(rid, blacklist=True)
    assert ok is True

    bl = tmp_store.list_blacklist()
    assert any(b.get("key_value") == "abc123" for b in bl)


def test_dismiss_result_no_blacklist(core, tmp_store):
    session_id = tmp_store.create_session("smart", [])
    rid = _make_result(session_id, "群", {"company_name": "KeepCo"}, tmp_store)

    ok = core.dismiss_result(rid, blacklist=False)
    assert ok is True

    bl = tmp_store.list_blacklist()
    assert not any("KeepCo" in (b.get("key_value") or "") for b in bl)


def test_dismiss_result_missing_returns_false(core):
    assert core.dismiss_result("res_nope") is False


# ------------------------------------------------------------
# get_session_results
# ------------------------------------------------------------

def test_get_session_results_filters_by_status(core, tmp_store):
    session_id = tmp_store.create_session("smart", [])
    rid_pending = _make_result(session_id, "群", {"company_name": "A"}, tmp_store)
    rid_written = _make_result(session_id, "群", {"company_name": "B"}, tmp_store)
    tmp_store.update_result_status(rid_written, "written")

    pending = core.get_session_results(session_id, status="pending")
    assert len(pending) == 1
    assert pending[0]["result_id"] == rid_pending

    all_rows = core.get_session_results(session_id, status="all")
    assert len(all_rows) == 2


# ------------------------------------------------------------
# save_local
# ------------------------------------------------------------

def test_save_local_writes_json(core, tmp_store, tmp_path):
    session_id = tmp_store.create_session("smart", [])
    rid = _make_result(session_id, "群", {"company_name": "JsonCo"}, tmp_store)

    out_dir = tmp_path / "out"
    path, count = core.save_local([rid], output_dir=out_dir)

    assert count == 1
    assert path.exists()
    import json
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[0]["chat"] == "群"
    assert payload[0]["result"]["company_name"] == "JsonCo"


# ------------------------------------------------------------
# scan requires reader
# ------------------------------------------------------------

def test_scan_without_reader_raises(core):
    with pytest.raises(RuntimeError, match="reader 未初始化"):
        core.scan(mode="smart", chats=[])


# ------------------------------------------------------------
# list_history / blacklist passthrough
# ------------------------------------------------------------

def test_list_history_returns_view(core, tmp_store):
    tmp_store.create_session("smart", [{"wxid": "x", "name": "n"}])
    view = core.list_history(limit=5)
    assert len(view.sessions) >= 1
    assert isinstance(view.pdf_stats, dict)


def test_blacklist_passthrough(core, tmp_store):
    tmp_store.blacklist_name("Foo Inc", reason="test")
    items = core.list_blacklist()
    assert any("Foo" in (b.get("key_value") or "") or
               "foo" in (b.get("key_value") or "").lower()
               for b in items)
