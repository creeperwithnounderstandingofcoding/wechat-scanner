"""Core 层的纯数据模型。无依赖，可被任何前端引用。"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class RefreshResult:
    """数据库刷新结果。"""
    ok: bool
    error: str = ""
    session_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanRunItem:
    """单条扫描结果（扫完的某个项目）。"""
    result_id: str
    chat: str
    result: dict  # 完整的 analysis dict

    def to_dict(self) -> dict:
        return {"result_id": self.result_id, "chat": self.chat, "result": self.result}


@dataclass
class ScanRunResult:
    """一次扫描 session 的汇总。"""
    session_id: str
    results: list[ScanRunItem] = field(default_factory=list)
    status: str = "completed"  # completed | failed

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "results": [r.to_dict() for r in self.results],
        }


@dataclass
class WriteItemResult:
    """单条写入飞书的结果。"""
    result_id: str
    ok: bool
    record_id: str = ""
    company_name: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WriteBatchResult:
    """批量写入汇总。"""
    success: int
    total: int
    items: list[WriteItemResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "total": self.total,
            "items": [i.to_dict() for i in self.items],
        }


@dataclass
class HistoryView:
    """历史面板数据。"""
    sessions: list[dict] = field(default_factory=list)
    pdf_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"sessions": self.sessions, "pdf_stats": self.pdf_stats}
