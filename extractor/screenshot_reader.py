"""微信消息读取器 — 基于截图 + Claude Vision OCR

最可靠的方案：
- 不需要关闭 SIP
- 不需要辅助功能权限
- 只需要屏幕录制权限（首次会弹窗授权）

原理：
1. screencapture 截取微信窗口
2. Claude Vision 识别聊天内容
3. 结构化输出供分析

支持两种模式：
- 自动截图：screencapture -x 全屏截图
- 手动截图：用户自己截图粘贴/拖入
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# Claude CLI 路径
def _find_claude_cli() -> str:
    base = os.path.expanduser("~/Library/Application Support/Claude/claude-code")
    if os.path.isdir(base):
        versions = sorted(os.listdir(base), reverse=True)
        for v in versions:
            candidate = os.path.join(base, v, "claude.app/Contents/MacOS/claude")
            if os.path.exists(candidate):
                return candidate
    return "claude"

CLAUDE_CLI = _find_claude_cli()


def capture_screen(output_path: str = "/tmp/wechat_capture.png") -> Optional[str]:
    """全屏截图，返回文件路径"""
    try:
        subprocess.run(
            ["screencapture", "-x", output_path],
            timeout=10,
        )
        if os.path.exists(output_path):
            return output_path
    except Exception as e:
        logger.error(f"截图失败: {e}")
    return None


def capture_wechat_window(output_path: str = "/tmp/wechat_capture.png") -> Optional[str]:
    """
    截取微信窗口。
    先激活微信，然后用 screencapture -x 截全屏。
    """
    try:
        # 激活微信
        subprocess.run(
            ["osascript", "-e", 'tell application "WeChat" to activate'],
            capture_output=True, text=True, timeout=5,
        )
        time.sleep(1)

        # 截图
        subprocess.run(
            ["screencapture", "-x", output_path],
            timeout=10,
        )
        if os.path.exists(output_path):
            logger.info(f"截图成功: {output_path}")
            return output_path
    except Exception as e:
        logger.error(f"截图失败: {e}")
    return None


def capture_interactive(output_path: str = "/tmp/wechat_capture.png") -> Optional[str]:
    """
    交互式截图：弹出十字准心让用户框选微信聊天区域。
    -i = 交互模式（用户框选），-x = 无声音
    """
    try:
        print("📸 请用鼠标框选微信聊天区域...")
        subprocess.run(
            ["screencapture", "-i", "-x", output_path],
            timeout=60,
        )
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info(f"交互式截图成功: {output_path}")
            return output_path
        else:
            logger.warning("用户取消了截图")
            return None
    except Exception as e:
        logger.error(f"交互式截图失败: {e}")
    return None


def read_chat_list_from_screenshot(image_path: str) -> list[dict]:
    """
    用 Claude Vision 从截图中识别微信左侧的聊天列表。
    返回 [{"name": str, "index": int}, ...]
    """
    prompt = """请从这张微信截图中识别左侧的聊天列表。
只列出聊天名称，按从上到下的顺序。

只返回 JSON 数组，格式：
[{"name": "聊天名称", "index": 1}, ...]

如果看不到聊天列表，返回空数组 []"""

    result = _call_claude_vision(image_path, prompt)
    if not result:
        return []

    try:
        # 尝试解析 JSON
        import re
        match = re.search(r'\[[\s\S]*\]', result)
        if match:
            return json.loads(match.group(0))
    except (json.JSONDecodeError, AttributeError):
        pass

    return []


def read_messages_from_screenshot(image_path: str) -> str:
    """
    用 Claude Vision 从截图中识别微信聊天消息。
    返回格式化的聊天文本。
    """
    prompt = """请从这张微信截图中提取所有可见的聊天消息。

格式要求：
- 每条消息一行
- 格式：发送者: 消息内容
- 如果能看到时间，加上时间：[时间] 发送者: 消息内容
- 如果是图片/视频/文件，标注 [图片]/[视频]/[文件: 文件名]
- 如果是链接，提取链接文字和URL
- 保持原始顺序（从上到下）
- 忽略微信UI元素（按钮、图标等），只提取消息内容

请直接输出聊天记录文本，不要加任何说明。"""

    result = _call_claude_vision(image_path, prompt)
    return result or ""


def read_messages_from_multiple_screenshots(image_paths: list[str]) -> str:
    """
    从多张截图中提取消息（用于滚动截图场景）。
    自动去重合并。
    """
    all_text = []
    seen_lines = set()

    for path in image_paths:
        text = read_messages_from_screenshot(path)
        if text:
            for line in text.strip().split("\n"):
                line = line.strip()
                if line and line not in seen_lines:
                    seen_lines.add(line)
                    all_text.append(line)

    return "\n".join(all_text)


def _call_claude_vision(image_path: str, prompt: str, timeout: int = 120) -> Optional[str]:
    """调用 Claude CLI 处理图片（利用 claude --print 支持图片输入）"""
    if not os.path.exists(image_path):
        logger.error(f"图片不存在: {image_path}")
        return None

    try:
        # Claude CLI 支持通过 stdin 传入图片路径
        # 格式: 在 prompt 中引用图片路径
        full_prompt = f"请分析这张图片: {image_path}\n\n{prompt}"

        result = subprocess.run(
            [CLAUDE_CLI, "--print", "--output-format", "text", image_path],
            input=prompt,
            capture_output=True, text=True, timeout=timeout,
        )

        if result.returncode != 0:
            logger.error(f"Claude Vision 失败: {result.stderr[:300]}")
            return None

        return result.stdout.strip()

    except subprocess.TimeoutExpired:
        logger.error("Claude Vision 超时")
        return None
    except Exception as e:
        logger.error(f"Claude Vision 出错: {e}")
        return None


def scroll_and_capture(
    num_pages: int = 5,
    output_dir: str = "/tmp/wechat_captures",
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """
    在微信中滚动并连续截图，捕获更多历史消息。

    使用 AppleScript 模拟键盘 Page Up 滚动（需要辅助功能权限）。
    如果没有权限，只返回当前页面截图。
    """
    os.makedirs(output_dir, exist_ok=True)
    captured = []

    # 先截当前页面
    path = os.path.join(output_dir, "page_0.png")
    if capture_wechat_window(path):
        captured.append(path)
        if progress_cb:
            progress_cb("已截取当前页面")

    # 尝试滚动截更多页
    for i in range(1, num_pages + 1):
        try:
            # 尝试用 AppleScript 滚动
            subprocess.run(
                ["osascript", "-e", '''
                tell application "System Events"
                    tell process "WeChat"
                        key code 116 -- Page Up
                    end tell
                end tell
                '''],
                capture_output=True, text=True, timeout=5,
            )
            time.sleep(1)

            path = os.path.join(output_dir, f"page_{i}.png")
            if capture_wechat_window(path):
                captured.append(path)
                if progress_cb:
                    progress_cb(f"已截取第 {i+1} 页")
            else:
                break
        except Exception as e:
            logger.debug(f"滚动截图失败: {e}")
            break

    if len(captured) <= 1 and progress_cb:
        progress_cb("提示: 如需更多历史，请手动滚动微信后重新截图")

    return captured
