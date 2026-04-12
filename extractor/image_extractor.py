"""微信图片消息提取器

WeChat 4.x 的图片消息（local_type=3）在本地存储路径：
    ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/msg/img/YYYY-MM/

本模块负责：
1. 从 type=3 消息 XML 中解析图片元数据（文件路径、md5 等）
2. 在本地文件目录中定位图片文件
3. 通过 Claude CLI 的 vision 能力提取图片中的文字和关键信息
"""

from __future__ import annotations

import base64
import logging
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BJT = timezone(timedelta(hours=8))

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}


def parse_image_metadata(xml_content: str) -> Optional[dict]:
    """从 type=3 消息 XML / raw_content 中解析图片元数据。

    WeChat 图片消息的 raw_content 可能包含 XML 形如：
        <img ...  cdnbigimgurl="..." ... />
    或直接含路径引用。

    返回 dict 含 md5、length 等信息，找不到有用信息也返回基础 dict。
    """
    if not xml_content:
        return None

    result = {}

    # 尝试提取 md5
    md5_match = re.search(r'md5="([a-fA-F0-9]+)"', xml_content)
    if md5_match:
        result["md5"] = md5_match.group(1)

    # 尝试提取长度/大小
    length_match = re.search(r'length="(\d+)"', xml_content)
    if length_match:
        result["length"] = int(length_match.group(1))

    # 尝试提取 hdlength
    hd_match = re.search(r'hdlength="(\d+)"', xml_content)
    if hd_match:
        result["hdlength"] = int(hd_match.group(1))

    # 尝试提取 cdnbigimgurl（CDN 高清图 URL）
    cdn_match = re.search(r'cdnbigimgurl="([^"]+)"', xml_content)
    if cdn_match:
        result["cdn_url"] = cdn_match.group(1)

    # 尝试提取 cdnmidimgurl（CDN 中等图 URL）
    cdnmid_match = re.search(r'cdnmidimgurl="([^"]+)"', xml_content)
    if cdnmid_match:
        result["cdn_mid_url"] = cdnmid_match.group(1)

    return result if result else None


def find_local_image(
    msg_raw_content: str,
    wechat_data_root: Path,
    create_time: int = 0,
    msg_server_id: int = 0,
) -> Optional[Path]:
    """在微信本地文件目录中查找图片文件。

    搜索策略：
    1. 根据消息时间定位 YYYY-MM 子目录
    2. 在 msg/img/ 下搜索图片文件
    3. WeChat 4.x 也可能在 msg/media/ 或 Image/ 目录下

    wechat_data_root: xwechat_files 根目录（内含多个 wxid 子目录）
    create_time: 消息时间戳（秒或毫秒），用于定位 YYYY-MM 子目录
    msg_server_id: 消息服务端 ID，有些图片以此命名
    """
    if not wechat_data_root.exists():
        return None

    # 解析时间 → 候选月份
    candidate_months: list[str] = []
    if create_time:
        try:
            ts = create_time
            if ts > 1_000_000_000_000:
                ts //= 1000
            dt = datetime.fromtimestamp(ts, tz=BJT)
            candidate_months.append(dt.strftime("%Y-%m"))
            # 前后一个月兜底
            prev = dt.replace(day=1) - timedelta(days=1)
            candidate_months.append(prev.strftime("%Y-%m"))
        except (ValueError, OSError):
            pass

    # 从 raw_content 中提取 md5 用于匹配
    md5 = ""
    md5_match = re.search(r'md5="([a-fA-F0-9]+)"', msg_raw_content or "")
    if md5_match:
        md5 = md5_match.group(1).lower()

    # 图片可能存储的子目录
    img_subdirs = ["msg/img", "msg/media", "msg/image", "Image"]

    for user_dir in wechat_data_root.iterdir():
        if not user_dir.is_dir():
            continue

        for subdir_name in img_subdirs:
            img_dir = user_dir / subdir_name
            if not img_dir.exists():
                continue

            # 先查候选月份
            for month in candidate_months:
                month_dir = img_dir / month
                if month_dir.exists():
                    hit = _find_image_in_dir(month_dir, md5, msg_server_id)
                    if hit:
                        return hit

            # 兜底：全月遍历（最近的先搜）
            for month_dir in sorted(img_dir.iterdir(), reverse=True):
                if not month_dir.is_dir():
                    continue
                hit = _find_image_in_dir(month_dir, md5, msg_server_id)
                if hit:
                    return hit

            # 也检查 img_dir 根目录（有些版本不分月份子目录）
            hit = _find_image_in_dir(img_dir, md5, msg_server_id)
            if hit:
                return hit

    return None


def _find_image_in_dir(
    directory: Path,
    md5: str = "",
    server_id: int = 0,
) -> Optional[Path]:
    """在目录中查找图片文件。

    匹配策略：
    1. 文件名含 md5
    2. 文件名含 server_id
    3. 如果都没有匹配，返回 None（不做盲猜）
    """
    if not directory.is_dir():
        return None

    try:
        children = list(directory.iterdir())
    except PermissionError:
        return None

    for child in children:
        if not child.is_file():
            continue
        if child.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        stem_lower = child.stem.lower()

        # md5 匹配
        if md5 and md5 in stem_lower:
            return child

        # server_id 匹配
        if server_id and str(server_id) in stem_lower:
            return child

    return None


def analyze_image_with_claude(
    image_path: Path,
    claude_cli: str,
    timeout: int = 60,
) -> str:
    """使用 Claude CLI 的 vision 能力分析图片。

    Claude CLI 在 --print 模式下不直接支持 --image 参数,
    因此使用 Anthropic API 的方式通过 base64 传递图片。

    返回提取的文字内容和关键信息。
    """
    if not image_path.exists():
        return ""

    # 检查文件大小（跳过过大的图片，>20MB）
    file_size = image_path.stat().st_size
    if file_size > 20 * 1024 * 1024:
        logger.warning(f"[图片分析] 文件过大，跳过: {image_path} ({file_size // 1024 // 1024}MB)")
        return ""

    # 读取并 base64 编码
    try:
        image_data = image_path.read_bytes()
        b64_data = base64.b64encode(image_data).decode("ascii")
    except Exception as e:
        logger.error(f"[图片分析] 读取文件失败: {image_path}: {e}")
        return ""

    # 判断 media type
    suffix = image_path.suffix.lower()
    media_type_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    media_type = media_type_map.get(suffix, "image/jpeg")

    return _analyze_image_via_api(b64_data, media_type, timeout)


def _analyze_image_via_api(
    b64_data: str,
    media_type: str,
    timeout: int = 60,
) -> str:
    """通过 Anthropic API 使用 Claude vision 分析图片。"""
    try:
        import anthropic
    except ImportError:
        logger.error("[图片分析] anthropic 库未安装。pip install anthropic")
        return ""

    import os
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("[图片分析] ANTHROPIC_API_KEY 未设置")
        return ""

    prompt = (
        "这是微信聊天中分享的图片。请提取其中的所有文字内容和关键信息。"
        "重点关注：公司名、项目名、融资信息、联系方式、行业/赛道信息。"
        "如果图片是截图（如文章截图、聊天截图），请完整提取文字。"
        "如果图片不含文字或与投资无关，简短描述图片内容即可。"
        "请直接输出提取的内容，不要加额外格式。"
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                }
            ],
        )
        result = response.content[0].text.strip()
        logger.info(f"[图片分析] 提取成功: {len(result)} 字符")
        return result
    except Exception as e:
        logger.error(f"[图片分析] API 调用失败: {e}")
        return ""
