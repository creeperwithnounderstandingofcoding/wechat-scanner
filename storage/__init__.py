"""本地持久化层：扫描会话、结果、黑名单、PDF 缓存。"""

from .store import Store, get_store, normalize_company_name

__all__ = ["Store", "get_store", "normalize_company_name"]
