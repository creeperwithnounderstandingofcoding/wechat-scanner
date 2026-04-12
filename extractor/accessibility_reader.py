"""微信消息读取器 — 基于 macOS Accessibility API

不需要关闭 SIP，不需要解密数据库。
只需要在系统设置中授权「辅助功能」权限。

原理：
1. 通过 Accessibility API 获取微信窗口的 UI 元素树
2. 从聊天列表读取会话名称
3. 从聊天窗口读取消息内容
4. 通过模拟滚动加载更多历史消息

前置条件：
- 系统设置 → 隐私与安全性 → 辅助功能 → 勾选本程序（或终端）
"""

from __future__ import annotations

import logging
import subprocess
import time
import json
import re
from typing import Optional, Callable

logger = logging.getLogger(__name__)


def check_accessibility_permission() -> bool:
    """检查是否有辅助功能权限"""
    # 尝试读取任意窗口，如果没权限会报错
    script = '''
    tell application "System Events"
        set frontApp to name of first application process whose frontmost is true
        return frontApp
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_wechat_running() -> bool:
    """检查微信是否在运行"""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "WeChat"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_chat_list() -> list[dict]:
    """
    获取微信左侧聊天列表（最近会话）。

    返回 [{"name": str, "index": int}, ...]
    """
    script = '''
    tell application "System Events"
        tell process "WeChat"
            set chatList to {}
            try
                -- 微信主窗口的聊天列表通常在第一个 scroll area 中
                set mainWindow to window 1
                set allGroups to every group of mainWindow
                set idx to 1
                repeat with g in allGroups
                    try
                        set cellName to value of static text 1 of g
                        set end of chatList to {idx, cellName}
                    end try
                    set idx to idx + 1
                end repeat
            end try

            -- 如果上面没拿到，尝试另一种 UI 结构
            if (count of chatList) is 0 then
                try
                    set scrollArea to scroll area 1 of mainWindow
                    set allRows to every row of table 1 of scrollArea
                    set idx to 1
                    repeat with r in allRows
                        try
                            set cellName to value of static text 1 of r
                            set end of chatList to {idx, cellName}
                        end try
                        set idx to idx + 1
                    end repeat
                end try
            end if

            -- 再尝试直接遍历所有 static text
            if (count of chatList) is 0 then
                try
                    set allTexts to every static text of mainWindow
                    set idx to 1
                    repeat with t in allTexts
                        try
                            set tVal to value of t
                            if length of tVal > 0 and length of tVal < 50 then
                                set end of chatList to {idx, tVal}
                            end if
                        end try
                        set idx to idx + 1
                    end repeat
                end try
            end if

            return chatList
        end tell
    end tell
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.error(f"获取聊天列表失败: {result.stderr}")
            return []

        # 解析 AppleScript 输出
        return _parse_chat_list(result.stdout.strip())

    except subprocess.TimeoutExpired:
        logger.error("获取聊天列表超时")
        return []
    except Exception as e:
        logger.error(f"获取聊天列表出错: {e}")
        return []


def _parse_chat_list(raw: str) -> list[dict]:
    """解析 AppleScript 返回的聊天列表"""
    chats = []
    if not raw:
        return chats

    # AppleScript 返回格式可能是: {1, "名字1"}, {2, "名字2"}, ...
    # 或者直接是文本列表
    parts = re.findall(r'(?:(\d+),\s*)?["\u201c]([^"\u201d]+)["\u201d]', raw)
    for idx_str, name in parts:
        idx = int(idx_str) if idx_str else len(chats) + 1
        chats.append({"name": name, "index": idx})

    if not chats:
        # fallback: 按行分割
        for i, line in enumerate(raw.split(","), 1):
            name = line.strip().strip('"').strip("'").strip()
            if name and len(name) < 50:
                chats.append({"name": name, "index": i})

    return chats


def click_chat(chat_name: str) -> bool:
    """在微信中点击指定的聊天"""
    script = f'''
    tell application "System Events"
        tell process "WeChat"
            set mainWindow to window 1
            -- 尝试点击包含指定名称的元素
            try
                set allElements to every UI element of mainWindow
                repeat with elem in allElements
                    try
                        if value of static text 1 of elem contains "{chat_name}" then
                            click elem
                            return true
                        end if
                    end try
                end repeat
            end try
            return false
        end tell
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        return "true" in result.stdout.lower()
    except Exception as e:
        logger.error(f"点击聊天失败: {e}")
        return False


def read_visible_messages() -> list[dict]:
    """
    读取当前微信聊天窗口中可见的消息。

    返回 [{"sender": str, "content": str, "time": str}, ...]
    """
    script = '''
    tell application "System Events"
        tell process "WeChat"
            set msgList to {}
            try
                set mainWindow to window 1
                -- 聊天消息通常在一个 scroll area 中
                set chatArea to scroll area 1 of mainWindow

                -- 尝试读取所有文本元素
                set allTexts to {}
                try
                    set allTexts to every static text of chatArea
                end try

                -- 如果直接读不到，尝试深层遍历
                if (count of allTexts) is 0 then
                    try
                        set allGroups to every group of chatArea
                        repeat with g in allGroups
                            try
                                set gTexts to every static text of g
                                repeat with t in gTexts
                                    set end of allTexts to t
                                end repeat
                            end try
                        end repeat
                    end try
                end if

                repeat with t in allTexts
                    try
                        set tVal to value of t
                        if tVal is not "" then
                            set end of msgList to tVal
                        end if
                    end try
                end repeat
            end try

            -- 也尝试从 table/rows 读取
            if (count of msgList) is 0 then
                try
                    set chatArea to scroll area 2 of mainWindow
                    set allTexts to every static text of chatArea
                    repeat with t in allTexts
                        try
                            set tVal to value of t
                            if tVal is not "" then
                                set end of msgList to tVal
                            end if
                        end try
                    end repeat
                end try
            end if

            return msgList
        end tell
    end tell
    '''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.error(f"读取消息失败: {result.stderr}")
            return []

        return _parse_messages(result.stdout.strip())

    except subprocess.TimeoutExpired:
        logger.error("读取消息超时")
        return []
    except Exception as e:
        logger.error(f"读取消息出错: {e}")
        return []


def _parse_messages(raw: str) -> list[dict]:
    """解析消息文本，尝试识别发送者和内容"""
    if not raw:
        return []

    messages = []
    # 按换行或逗号分割
    lines = raw.replace("\\n", "\n").split(",")

    current_sender = ""
    time_pattern = re.compile(r'^\d{1,2}:\d{2}$|^\d{4}[/-]\d{1,2}[/-]\d{1,2}|^昨天|^前天|^星期')

    for line in lines:
        line = line.strip().strip('"').strip("'").strip()
        if not line:
            continue

        # 时间戳行
        if time_pattern.match(line):
            continue

        # 短文本可能是发送者名称（微信中名字通常在消息上方）
        if len(line) < 20 and not any(c in line for c in "。，！？.!?,"):
            current_sender = line
            continue

        messages.append({
            "sender": current_sender,
            "content": line,
            "time": "",
        })

    return messages


def scroll_up_chat(scroll_count: int = 5) -> bool:
    """在微信聊天窗口向上滚动加载更多历史消息"""
    script = f'''
    tell application "WeChat" to activate
    delay 0.3
    tell application "System Events"
        tell process "WeChat"
            -- 向上滚动
            repeat {scroll_count} times
                key code 126 using {{option down}} -- Option+Up arrow = page up
                delay 0.2
            end repeat
        end tell
    end tell
    return true
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
        time.sleep(1)  # 等待加载
        return result.returncode == 0
    except Exception as e:
        logger.error(f"滚动失败: {e}")
        return False


def read_chat_with_scroll(
    max_scrolls: int = 10,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """
    读取当前聊天的消息，通过向上滚动获取更多历史。

    max_scrolls: 最大滚动次数
    返回去重后的消息列表
    """
    all_messages = []
    seen_contents = set()

    for i in range(max_scrolls + 1):
        if progress_cb:
            progress_cb(f"读取第 {i+1} 页消息...")

        messages = read_visible_messages()
        new_count = 0
        for msg in messages:
            content = msg.get("content", "")
            if content and content not in seen_contents:
                seen_contents.add(content)
                all_messages.append(msg)
                new_count += 1

        if progress_cb:
            progress_cb(f"  获取 {new_count} 条新消息 (累计 {len(all_messages)})")

        if new_count == 0 and i > 0:
            # 没有新消息了，到顶了
            if progress_cb:
                progress_cb("已到达聊天历史顶部")
            break

        if i < max_scrolls:
            scroll_up_chat(scroll_count=3)

    return all_messages


def activate_wechat():
    """将微信窗口置前"""
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "WeChat" to activate'],
            capture_output=True, text=True, timeout=5,
        )
        time.sleep(0.5)
    except Exception as e:
        logger.error(f"激活微信失败: {e}")


def get_current_chat_name() -> str:
    """获取当前正在查看的聊天名称"""
    script = '''
    tell application "System Events"
        tell process "WeChat"
            try
                -- 聊天标题通常在窗口顶部
                set mainWindow to window 1
                set windowTitle to name of mainWindow
                return windowTitle
            end try
            return ""
        end tell
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""
