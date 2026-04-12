"""聊天选择器 — CLI 交互界面

让用户选择要扫描的聊天/群组：
1. 显示最近会话列表
2. 支持搜索联系人/群名
3. 支持多选
4. 确认后返回选中的 wxid 列表

隐私保护：必须用户主动选择，不会自动扫描全部聊天。
"""

from __future__ import annotations

import logging
from typing import Optional

from extractor.db_reader import WeChatDBReader

logger = logging.getLogger(__name__)


def select_chats_cli(reader: WeChatDBReader) -> list[dict]:
    """
    CLI 交互式选择聊天。

    返回选中的聊天列表 [{"wxid": str, "display_name": str, "is_group": bool}, ...]
    """
    print("\n" + "=" * 60)
    print("  微信聊天选择器")
    print("  隐私保护：只会扫描你选择的聊天")
    print("=" * 60)

    # 加载联系人（建立缓存）
    print("\n正在加载联系人...")
    contacts = reader.get_contacts()
    print(f"已加载 {len(contacts)} 个联系人")

    # 获取会话列表
    print("正在加载最近会话...")
    sessions = reader.get_sessions()

    if not sessions:
        print("⚠️ 无法读取会话列表。请尝试手动输入 wxid。")
        return _manual_input()

    # 分离群聊和个人聊天
    groups = [s for s in sessions if s.get("is_group")]
    privates = [s for s in sessions if not s.get("is_group")]

    selected = []

    while True:
        print("\n--- 操作菜单 ---")
        print("1. 查看最近群聊")
        print("2. 查看最近私聊")
        print("3. 搜索聊天")
        print(f"4. 查看已选择 ({len(selected)} 个)")
        print("5. 确认开始扫描")
        print("0. 退出")

        choice = input("\n请输入选项: ").strip()

        if choice == "1":
            selected = _show_and_select(groups, "群聊", selected, reader)
        elif choice == "2":
            selected = _show_and_select(privates, "私聊", selected, reader)
        elif choice == "3":
            keyword = input("输入搜索关键词: ").strip()
            if keyword:
                matched = _search_sessions(sessions, keyword)
                if matched:
                    selected = _show_and_select(matched, f"搜索「{keyword}」", selected, reader)
                else:
                    print("未找到匹配的聊天。")
        elif choice == "4":
            if selected:
                print(f"\n已选择 {len(selected)} 个聊天:")
                for i, s in enumerate(selected, 1):
                    print(f"  {i}. {s['display_name']} ({'群' if s.get('is_group') else '私'})")
                remove = input("\n输入编号取消选择（直接回车跳过）: ").strip()
                if remove.isdigit():
                    idx = int(remove) - 1
                    if 0 <= idx < len(selected):
                        removed = selected.pop(idx)
                        print(f"已取消选择: {removed['display_name']}")
            else:
                print("尚未选择任何聊天。")
        elif choice == "5":
            if selected:
                print(f"\n将扫描以下 {len(selected)} 个聊天:")
                for s in selected:
                    msg_count = reader.get_message_count(s["wxid"])
                    print(f"  - {s['display_name']} ({msg_count} 条消息)")
                confirm = input("\n确认开始？(y/n): ").strip().lower()
                if confirm in ("y", "yes", "是"):
                    return selected
            else:
                print("请先选择要扫描的聊天。")
        elif choice == "0":
            return []

    return selected


def _show_and_select(
    sessions: list[dict],
    title: str,
    current_selected: list[dict],
    reader: WeChatDBReader,
) -> list[dict]:
    """显示会话列表并让用户选择"""
    if not sessions:
        print(f"没有{title}记录。")
        return current_selected

    # 按最后消息时间排序
    sorted_sessions = sorted(sessions, key=lambda s: s.get("last_msg_time", 0), reverse=True)

    page_size = 20
    page = 0
    total_pages = (len(sorted_sessions) + page_size - 1) // page_size

    while True:
        start = page * page_size
        end = min(start + page_size, len(sorted_sessions))
        page_items = sorted_sessions[start:end]

        selected_wxids = {s["wxid"] for s in current_selected}

        print(f"\n--- {title} (第 {page + 1}/{total_pages} 页) ---")
        for i, s in enumerate(page_items, start + 1):
            mark = "✅" if s["wxid"] in selected_wxids else "  "
            name = s["display_name"]
            time_str = s.get("last_msg_time_str", "")
            print(f"{mark} {i:3d}. {name}  [{time_str}]")

        print(f"\n输入编号选择（如 1,3,5），n 下一页，p 上一页，q 返回")
        inp = input("> ").strip().lower()

        if inp == "q":
            break
        elif inp == "n" and page < total_pages - 1:
            page += 1
        elif inp == "p" and page > 0:
            page -= 1
        else:
            # 解析编号
            for part in inp.replace("，", ",").split(","):
                part = part.strip()
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < len(sorted_sessions):
                        session = sorted_sessions[idx]
                        if session["wxid"] not in selected_wxids:
                            current_selected.append(session)
                            selected_wxids.add(session["wxid"])
                            print(f"  ✅ 已选择: {session['display_name']}")
                        else:
                            # 取消选择
                            current_selected = [
                                s for s in current_selected if s["wxid"] != session["wxid"]
                            ]
                            selected_wxids.discard(session["wxid"])
                            print(f"  ❌ 已取消: {session['display_name']}")

    return current_selected


def _search_sessions(sessions: list[dict], keyword: str) -> list[dict]:
    """搜索会话"""
    kw = keyword.lower()
    return [
        s for s in sessions
        if kw in s.get("display_name", "").lower()
        or kw in s.get("wxid", "").lower()
    ]


def _manual_input() -> list[dict]:
    """手动输入 wxid（会话列表不可用时的 fallback）"""
    print("\n请手动输入要扫描的 wxid（每行一个，输入空行结束）:")
    selected = []
    while True:
        wxid = input("> ").strip()
        if not wxid:
            break
        selected.append({
            "wxid": wxid,
            "display_name": wxid,
            "is_group": wxid.endswith("@chatroom"),
        })
        print(f"  已添加: {wxid}")
    return selected
