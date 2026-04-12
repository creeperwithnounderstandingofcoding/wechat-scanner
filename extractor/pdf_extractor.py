"""微信 PDF 附件提取器

WeChat 4.x 的文件消息（local_type=49，子类型 app.type=6）会在本地保存到：
    ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/msg/file/YYYY-MM/<filename>

本模块负责：
1. 从消息 XML 中解析文件元数据（文件名、md5、大小、收到时间）
2. 在本地文件目录中定位 PDF 文件
3. 提取 PDF 正文文本（PyPDF2，pdftotext 兜底）
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BJT = timezone(timedelta(hours=8))


def parse_file_metadata(xml_content: str) -> Optional[dict]:
    """从 type=49 消息 XML 中解析文件元数据。

    只返回 PDF 类型的文件（通过 title 扩展名判断）。
    非文件消息（链接、小程序等）返回 None。
    """
    if not xml_content or "<title>" not in xml_content:
        return None

    title_match = re.search(r"<title>(.*?)</title>", xml_content, re.DOTALL)
    if not title_match:
        return None
    title = title_match.group(1).strip()

    # 只处理 PDF
    if not title.lower().endswith(".pdf"):
        return None

    # app.type=6 表示文件消息（区别于链接 type=5 等）
    type_match = re.search(r"<type>(\d+)</type>", xml_content)
    app_type = int(type_match.group(1)) if type_match else 0
    if app_type and app_type != 6:
        return None

    md5 = _extract_tag(xml_content, "md5")
    size_str = _extract_tag(xml_content, "totallen") or _extract_tag(xml_content, "filesize")
    try:
        size = int(size_str) if size_str else 0
    except ValueError:
        size = 0

    return {
        "title": title,
        "md5": md5,
        "size": size,
    }


def _extract_tag(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.DOTALL)
    return m.group(1).strip() if m else ""


def find_local_pdf(
    filename: str,
    wechat_data_root: Path,
    create_time: int = 0,
) -> Optional[Path]:
    """在微信本地文件目录中查找 PDF 文件。

    wechat_data_root: xwechat_files 根目录（内含多个 wxid 子目录）
    create_time: 消息时间戳（秒或毫秒），用于定位 YYYY-MM 子目录
    """
    if not wechat_data_root.exists():
        return None

    # 优先搜索对应月份目录，找不到再全量扫描
    candidate_months = []
    if create_time:
        try:
            ts = create_time
            if ts > 1_000_000_000_000:
                ts //= 1000
            dt = datetime.fromtimestamp(ts, tz=BJT)
            candidate_months.append(dt.strftime("%Y-%m"))
            # 前后各一个月作为兜底（时区误差 / 消息时间与文件落地时间差）
            prev = (dt.replace(day=1) - timedelta(days=1))
            candidate_months.append(prev.strftime("%Y-%m"))
        except (ValueError, OSError):
            pass

    # 构建搜索路径：所有 user wxid 目录
    for user_dir in wechat_data_root.iterdir():
        if not user_dir.is_dir():
            continue
        file_dir = user_dir / "msg" / "file"
        if not file_dir.exists():
            continue

        # 先查候选月份
        for month in candidate_months:
            month_dir = file_dir / month
            if month_dir.exists():
                hit = _match_in_dir(month_dir, filename)
                if hit:
                    return hit

        # 兜底：全月遍历
        for month_dir in sorted(file_dir.iterdir(), reverse=True):
            if not month_dir.is_dir():
                continue
            hit = _match_in_dir(month_dir, filename)
            if hit:
                return hit

    return None


def _match_in_dir(directory: Path, filename: str) -> Optional[Path]:
    """在目录中查找文件：先精确，再去掉扩展名和 (N) 后缀的模糊匹配。"""
    exact = directory / filename
    if exact.exists():
        return exact

    # 微信会在重复文件后加 (1)(2) 等，模糊匹配
    stem = Path(filename).stem
    for child in directory.iterdir():
        if not child.is_file():
            continue
        if child.stem == stem:
            return child
        # 匹配 "xxx (1)" 形式
        if re.match(rf"^{re.escape(stem)} \(\d+\)$", child.stem):
            return child
    return None


def extract_pdf_text(file_path: Path | str) -> str:
    """提取 PDF 文本。优先 PyPDF2，失败则 pdftotext。"""
    file_path = str(file_path)
    text = ""

    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(file_path)
        pages = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                pages.append(f"--- 第 {i+1} 页 ---\n{page_text}")
        text = "\n\n".join(pages)
        if text.strip():
            logger.info(f"[PDF] PyPDF2 提取成功: {len(text)} 字符, {len(reader.pages)} 页")
            return text
    except ImportError:
        logger.warning("[PDF] PyPDF2 未安装，尝试 pdftotext")
    except Exception as e:
        logger.warning(f"[PDF] PyPDF2 提取失败: {e}")

    try:
        result = subprocess.run(
            ["pdftotext", "-layout", file_path, "-"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.info(f"[PDF] pdftotext 提取成功: {len(result.stdout)} 字符")
            return result.stdout
    except FileNotFoundError:
        logger.debug("[PDF] pdftotext 未安装")
    except Exception as e:
        logger.warning(f"[PDF] pdftotext 失败: {e}")

    if not text.strip():
        raise RuntimeError("无法从 PDF 提取文本（可能是扫描件）")
    return text
