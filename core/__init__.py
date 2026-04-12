"""Core service layer

提供与具体 UI（Flask Web / 飞书 Bot / CLI）解耦的业务门面。

典型用法：
    from core import ScannerCore
    core = ScannerCore()
    core.init_reader()
    run = core.scan(mode="smart", chats=[{"wxid": "x", "name": "投资群"}])
    batch = core.write_results([r.result_id for r in run.results])
"""

from .models import (
    RefreshResult,
    ScanRunResult,
    ScanRunItem,
    WriteItemResult,
    WriteBatchResult,
    HistoryView,
)
from .scanner_core import ScannerCore

__all__ = [
    "ScannerCore",
    "RefreshResult",
    "ScanRunResult",
    "ScanRunItem",
    "WriteItemResult",
    "WriteBatchResult",
    "HistoryView",
]
