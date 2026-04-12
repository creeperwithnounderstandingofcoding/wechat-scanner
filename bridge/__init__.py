"""Bridge package — thin adapters to other projects' services.

目前只有一个桥接：bot_services —— 借 feishu-deal-bot 的 services/ 来做飞书扫描，
而不把它们物理搬进 wechat-scanner。这样 bot 零风险，随时可回滚，
等 Phase 4 架构清理时再统一迁到 shared 包。
"""
