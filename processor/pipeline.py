"""分析流水线：微信消息 → Claude 分析 → bitable 写入

两种扫描模式：
1. 区域扫描（area scan）— 用户指定一段区域，抓 ~30 条消息的快照分析
2. 全局扫描（full scan）— 扫描整个聊天历史，分批分析

复用 feishu-deal-bot 的 Claude 分析和 bitable 写入能力。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

import config
from extractor.db_reader import WeChatDBReader
from extractor.pdf_extractor import (
    parse_file_metadata,
    find_local_pdf,
    extract_pdf_text,
)
from extractor.image_extractor import (
    find_local_image,
    analyze_image_with_claude,
)
from extractor.voice_extractor import (
    find_voice_file,
    transcribe_voice,
)
from storage import get_store

logger = logging.getLogger(__name__)

BJT = timezone(timedelta(hours=8))


# ===== Claude 调用（独立版，不依赖 feishu-deal-bot）=====

def _find_claude_cli() -> str:
    """查找 Claude CLI"""
    if config.CLAUDE_CLI_PATH and os.path.exists(config.CLAUDE_CLI_PATH):
        return config.CLAUDE_CLI_PATH
    base = os.path.expanduser("~/Library/Application Support/Claude/claude-code")
    if os.path.isdir(base):
        versions = sorted(os.listdir(base), reverse=True)
        for v in versions:
            candidate = os.path.join(base, v, "claude.app/Contents/MacOS/claude")
            if os.path.exists(candidate):
                return candidate
    return "claude"


CLAUDE_CLI = _find_claude_cli()


def _call_claude(prompt: str, system: str = "", timeout: int = 180) -> str:
    full_prompt = (system + "\n\n" + prompt) if system else prompt
    result = subprocess.run(
        [CLAUDE_CLI, "--print", "--output-format", "text"],
        input=full_prompt,
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI 失败: {result.stderr[:300]}")
    return result.stdout.strip()


def _parse_json_response(text: str) -> dict:
    import re
    if "```" in text:
        parts = text.split("```")
        for part in parts[1:]:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"无法解析 JSON: {text[:300]}")


# ===== lark-cli 调用（写入 bitable）=====

def _run_lark_cli(args: list[str], timeout: int = 30) -> str:
    env = os.environ.copy()
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    result = subprocess.run(
        [config.LARK_CLI_PATH] + args,
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli 失败: {result.stderr[:300]}")
    return result.stdout.strip()


# ===== 被投名单（用于 Validator Agent）=====

def _portfolio_list_str() -> str:
    """格式化被投名单供 prompt 使用"""
    if not config.PORTFOLIO_COMPANIES:
        return "（暂无被投名单）"
    return "、".join(config.PORTFOLIO_COMPANIES)


# ===== 双 Agent 架构 =====

def _agent_extract(snapshot: str, chat_name: str) -> list[dict]:
    """Agent 1: 提取器 — 从聊天记录中粗提取所有公司/项目提及

    职责：宁多勿漏，把所有看起来像项目的信息都提取出来
    """
    system = f"""你是投资机构的聊天分析助手（提取模式）。

你的唯一任务：从微信聊天记录中提取所有被提及的**公司/项目/创业团队**。

核心规则：
1. 宁多勿漏 — 只要聊天中出现了公司名、产品名、团队名，都提取
2. 每个公司/项目单独一条记录
3. 不需要判断是否值得入库，只管提取
4. 如果聊天完全没有提到任何公司/项目（纯闲聊），返回空数组 []
5. 尽量从上下文推断：创始人、赛道、融资阶段等

内部员工名单：{', '.join(config.STAFF_NAMES)}"""

    prompt = f"""从以下微信聊天记录中提取所有提到的公司/项目。每个公司一条。

## 聊天记录
{snapshot}

## 返回 JSON 数组（只返回 JSON）：
[
  {{
    "company_name": "公司/项目名称",
    "founder_name": "创始人/关键人（如能识别）",
    "internal_attendees": "参与讨论的内部同事",
    "industry": "赛道/行业",
    "funding_stage": "融资阶段（如提及）",
    "context_summary": "该公司在聊天中的讨论要点（2-3句话）",
    "raw_mentions": "原文中提到该公司的关键句子（直接引用）"
  }}
]

无公司提及则返回: []"""

    try:
        text = _call_claude(prompt, system, timeout=180)
        import re
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            items = json.loads(match.group(0))
            return items if items else []
        return []
    except Exception as e:
        logger.error(f"Agent 1 提取失败 ({chat_name}): {e}")
        return []


def _agent_validate(extractions: list[dict], chat_name: str) -> list[dict]:
    """Agent 2: 验证器 — 审核 Agent 1 的提取结果

    职责：
    - 过滤噪音（非公司的误提取）
    - 标记 record_type: 新项目 vs 被投跟踪
    - 合并重复项
    - 判定置信度
    - 决定是否值得入库
    """
    if not extractions:
        return []

    portfolio_str = _portfolio_list_str()
    extractions_json = json.dumps(extractions, ensure_ascii=False, indent=2)

    system = f"""你是投资机构的分析验证专家（验证模式）。

你的任务：审核另一个 AI 提取的公司/项目信息，做质量把关。

## 你知道的关键信息

### 被投企业名单（已投资的公司）：
{portfolio_str}

### 内部员工名单：
{', '.join(config.STAFF_NAMES)}

## 验证规则

1. **过滤噪音**：如果提取的"公司"其实不是公司/项目（如个人名字、地名、产品功能名），标记 action="丢弃"
2. **被投 vs 新项目**：
   - 如果公司名在被投名单中 → record_type="被投跟踪"
   - 如果公司名不在被投名单中 → record_type="新项目"
   - 注意模糊匹配（如 "Simular AI" 匹配 "Simular"）
3. **合并重复**：同一家公司被多次提取的，合并为一条
4. **是否入库**：
   - 新项目：只要有实质性讨论（不是随口提一嘴），建议入库
   - 被投跟踪：有重要进展更新的建议入库，日常闲聊不入库
5. **置信度**：
   - high: 公司名明确、有实质讨论内容
   - medium: 公司名明确但信息有限
   - low: 不确定是否是独立公司/项目

## 输出格式
对每个提取项做判定，只保留 action="入库" 的记录。"""

    prompt = f"""以下是 AI 从微信群「{chat_name}」中提取的公司/项目信息。
请逐一审核，返回最终应入库的记录。

## AI 提取结果
{extractions_json}

## 请返回 JSON 数组（只返回 JSON）：
[
  {{
    "action": "入库|丢弃",
    "action_reason": "简短说明为什么入库/丢弃",
    "record_type": "新项目|被投跟踪",
    "company_name": "公司名（如需修正，用修正后的名字）",
    "meeting_type": "微信讨论|项目推荐|BP分享|投后跟踪|其他",
    "founder_name": "创始人",
    "internal_attendees": "内部同事",
    "industry": "赛道",
    "funding_stage": "融资阶段",
    "summary": "2-3句话专业摘要",
    "key_insights": "关键观点",
    "confidence": "high|medium|low",
    "confidence_reason": "依据"
  }}
]

如果全部应丢弃，返回: []"""

    try:
        text = _call_claude(prompt, system, timeout=120)
        import re
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            items = json.loads(match.group(0))
            # 只保留 action="入库" 的
            validated = [
                item for item in items
                if item.get("action", "").strip() == "入库"
            ]
            for v in validated:
                v["source_type"] = "wechat"
                v.pop("action", None)
                v.pop("action_reason", None)
            return validated
        return []
    except Exception as e:
        logger.error(f"Agent 2 验证失败 ({chat_name}): {e}")
        # 验证失败时降级：返回原始提取结果（标记 low confidence）
        for item in extractions:
            item["source_type"] = "wechat"
            item["confidence"] = "low"
            item["confidence_reason"] = "验证 Agent 未能运行，使用原始提取"
            item["record_type"] = "新项目"
        return extractions


def _code_validate(extractions: list[dict], chat_name: str, progress_cb=None) -> list[dict]:
    """代码验证：替代 Agent 2，用规则做 portfolio matching + confidence scoring + 去重。

    比 Agent 2 快（无 Claude 调用），且行为确定性更强。
    """
    if not extractions:
        return []

    # 获取被投名单用于 portfolio matching
    portfolio_set_lower = {p.lower() for p in config.PORTFOLIO_COMPANIES}

    def _normalize(name: str) -> str:
        s = re.sub(r'[（(].*?[）)]', '', name)
        return s.replace(" ", "").lower()

    # 去重：按归一化名称分组，保留信息最丰富的
    seen: dict[str, dict] = {}
    for item in extractions:
        name = item.get("company_name", "").strip()
        if not name:
            continue
        norm = _normalize(name)
        if not norm:
            continue

        # 检查是否与已有条目重复（包含关系）
        matched_key = None
        for key in seen:
            if norm == key or (len(norm) >= 2 and (norm in key or key in norm)):
                matched_key = key
                break

        if matched_key:
            # 保留字段更多的那个
            existing = seen[matched_key]
            existing_fields = sum(1 for v in existing.values() if v)
            new_fields = sum(1 for v in item.values() if v)
            if new_fields > existing_fields:
                seen[matched_key] = item
        else:
            seen[norm] = item

    validated = []
    for norm_name, item in seen.items():
        company_name = item.get("company_name", "").strip()

        # 1. 过滤垃圾名
        if _is_junk_company_name(company_name):
            if progress_cb:
                progress_cb(f"  [代码验证] 丢弃垃圾名: {company_name}")
            continue

        # 2. Portfolio matching: 新项目 vs 被投跟踪
        is_portfolio = False
        norm_lower = _normalize(company_name)
        for p in config.PORTFOLIO_COMPANIES:
            p_norm = _normalize(p)
            if norm_lower == p_norm or (len(norm_lower) >= 2 and (norm_lower in p_norm or p_norm in norm_lower)):
                is_portfolio = True
                break

        item["record_type"] = "被投跟踪" if is_portfolio else "新项目"

        # 3. Confidence scoring: 基于字段填充率 + 内容质量
        filled_fields = 0
        quality_fields = ["founder_name", "industry", "funding_stage", "context_summary", "raw_mentions"]
        for f in quality_fields:
            val = item.get(f, "")
            if val and len(str(val).strip()) > 1:
                filled_fields += 1

        # context_summary 的内容质量（长度）
        summary_len = len(item.get("context_summary", ""))

        if filled_fields >= 3 and summary_len > 30:
            item["confidence"] = "high"
            item["confidence_reason"] = f"字段填充 {filled_fields}/{len(quality_fields)}，摘要 {summary_len} 字符"
        elif filled_fields >= 2 or summary_len > 20:
            item["confidence"] = "medium"
            item["confidence_reason"] = f"字段填充 {filled_fields}/{len(quality_fields)}，摘要 {summary_len} 字符"
        else:
            item["confidence"] = "low"
            item["confidence_reason"] = f"字段填充 {filled_fields}/{len(quality_fields)}，信息有限"

        # 4. 入库决策
        # 新项目: 有实质讨论（至少有 context_summary）就入库
        # 被投跟踪: 需要有重要进展（至少 medium confidence）
        if item["record_type"] == "被投跟踪" and item["confidence"] == "low":
            if progress_cb:
                progress_cb(f"  [代码验证] 跳过被投日常提及: {company_name}")
            continue

        if item["confidence"] == "low" and not item.get("context_summary"):
            if progress_cb:
                progress_cb(f"  [代码验证] 跳过低置信无摘要: {company_name}")
            continue

        # 设置 meeting_type
        if not item.get("meeting_type"):
            if is_portfolio:
                item["meeting_type"] = "投后跟踪"
            elif item.get("funding_stage"):
                item["meeting_type"] = "项目推荐"
            else:
                item["meeting_type"] = "微信讨论"

        # 将 context_summary 映射为 summary（Agent 2 输出格式兼容）
        if item.get("context_summary") and not item.get("summary"):
            item["summary"] = item["context_summary"]

        # 将 raw_mentions 映射为 key_insights
        if item.get("raw_mentions") and not item.get("key_insights"):
            item["key_insights"] = item["raw_mentions"]

        item["source_type"] = "wechat"
        validated.append(item)

    return validated


def _dual_agent_analyze(snapshot: str, chat_name: str, progress_cb=None) -> list[dict]:
    """分析流程：提取 → 验证（代码验证或 Agent 2）"""
    # Agent 1: 提取
    if progress_cb:
        progress_cb(f"[Agent 1] 提取中...")
    extractions = _agent_extract(snapshot, chat_name)

    if not extractions:
        if progress_cb:
            progress_cb(f"[Agent 1] 未发现公司/项目提及")
        return []

    if progress_cb:
        names = ", ".join(e.get("company_name", "?") for e in extractions)
        progress_cb(f"[Agent 1] 提取到 {len(extractions)} 个: {names}")

    # 验证：代码验证 or Agent 2
    if config.SKIP_VALIDATION_AGENT:
        if progress_cb:
            progress_cb(f"[代码验证] 验证中（无需额外 AI 调用）...")
        validated = _code_validate(extractions, chat_name, progress_cb)
    else:
        # 原有 Agent 2 流程
        if progress_cb:
            progress_cb(f"[Agent 2] 验证中...")
        validated = _agent_validate(extractions, chat_name)

    if progress_cb:
        if validated:
            for v in validated:
                tag = "被投" if v.get("record_type") == "被投跟踪" else "新"
                progress_cb(f"  ✅ [{tag}] {v.get('company_name', '?')} [{v.get('confidence', '')}]")
        else:
            progress_cb(f"[验证] 验证后无需入库的项目")

    return validated


# ===== 消息快照格式化 =====

def format_messages_for_ai(
    messages: list[dict],
    chat_name: str = "",
) -> str:
    """将微信消息列表格式化为 Claude 可分析的文本快照

    对于图片消息（type=3），如果已通过 _process_images_in_messages 提取了文字，
    会在 [图片] 标记后附带提取的内容。
    """
    if not messages:
        return "（无消息）"

    lines = []
    if chat_name:
        lines.append(f"=== 微信聊天: {chat_name} ===\n")

    for msg in messages:
        time_str = msg.get("time_str", "")
        sender_name = msg.get("sender", "") or "未知"
        content = msg.get("content", "")

        # 如果图片已经被分析过，附带提取的内容
        image_text = msg.get("_image_extracted_text", "")
        if image_text:
            content = f"[图片内容: {image_text}]"

        # 如果语音已经被转写，替换 [语音] 占位符
        voice_text = msg.get("_voice_text", "")
        if voice_text:
            content = f"[语音转文字: {voice_text}]"

        lines.append(f"[{time_str}] {sender_name}: {content}")

    return "\n".join(lines)


def _extract_link_info(content: str) -> str:
    """从微信消息 XML 中提取链接/文件信息"""
    import re
    title_match = re.search(r"<title>(.+?)</title>", content)
    url_match = re.search(r"<url>(.+?)</url>", content)
    des_match = re.search(r"<des>(.+?)</des>", content)

    parts = []
    if title_match:
        parts.append(title_match.group(1))
    if des_match:
        parts.append(des_match.group(1)[:100])
    if url_match:
        parts.append(f"链接: {url_match.group(1)}")

    return " | ".join(parts) if parts else f"[链接/文件] {content[:100]}"


def _extract_card_info(content: str) -> str:
    """从名片消息中提取信息"""
    import re
    nick = re.search(r"nickname=\"(.+?)\"", content)
    return f"[名片: {nick.group(1)}]" if nick else "[名片]"


# ===== 图片处理 =====

MAX_IMAGES_PER_CHUNK = 10  # 每个 chunk 最多处理的图片数，避免拖慢分析


def _process_images_in_messages(
    messages: list[dict],
    wechat_data_root: Path | None = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """预处理消息列表中的图片消息：定位本地文件并用 Claude vision 提取文字。

    修改 messages 列表中的 dict（原地更新），为图片消息添加 _image_extracted_text 字段。
    返回处理后的 messages（同一引用）。

    限制：每个 chunk 最多处理 MAX_IMAGES_PER_CHUNK 张图片。
    """
    if wechat_data_root is None:
        wechat_data_root = config.WECHAT_DATA_ROOT

    if not wechat_data_root.exists():
        logger.warning(f"[图片处理] 微信数据目录不存在: {wechat_data_root}")
        return messages

    # 筛选图片消息
    image_msgs = [
        m for m in messages
        if (m.get("msg_type", 0) & 0xFF if m.get("msg_type", 0) > 255 else m.get("msg_type", 0)) == 3
    ]

    if not image_msgs:
        return messages

    if len(image_msgs) > MAX_IMAGES_PER_CHUNK:
        if progress_cb:
            progress_cb(
                f"[图片处理] 发现 {len(image_msgs)} 张图片，"
                f"仅处理前 {MAX_IMAGES_PER_CHUNK} 张"
            )
        image_msgs = image_msgs[:MAX_IMAGES_PER_CHUNK]

    if progress_cb:
        progress_cb(f"[图片处理] 正在分析 {len(image_msgs)} 张图片...")

    processed_count = 0
    for msg in image_msgs:
        raw_content = msg.get("raw_content", "")
        create_time = msg.get("timestamp", 0)

        # 尝试定位本地图片文件
        local_path = find_local_image(
            msg_raw_content=raw_content,
            wechat_data_root=wechat_data_root,
            create_time=create_time,
        )

        if not local_path:
            logger.debug(f"[图片处理] 未找到本地图片文件: ts={create_time}")
            continue

        logger.info(f"[图片处理] 找到图片: {local_path}")

        # 使用 Claude vision 分析图片
        try:
            extracted_text = analyze_image_with_claude(
                image_path=local_path,
                claude_cli=CLAUDE_CLI,
                timeout=60,
            )
            if extracted_text:
                msg["_image_extracted_text"] = extracted_text
                msg["_image_local_path"] = str(local_path)
                processed_count += 1
                if progress_cb:
                    preview = extracted_text[:80].replace("\n", " ")
                    progress_cb(f"  [图片 {processed_count}] {preview}...")
        except Exception as e:
            logger.error(f"[图片处理] 分析失败 {local_path}: {e}")

    if progress_cb:
        progress_cb(f"[图片处理] 完成: {processed_count}/{len(image_msgs)} 张图片提取成功")

    return messages


# ===== 语音处理 =====

MAX_VOICES_PER_CHUNK = 10  # 每个 chunk 最多处理的语音数


def _process_voice_in_messages(
    messages: list[dict],
    wechat_data_root: Path | None = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """预处理消息列表中的语音消息：尝试转写语音内容。

    转写来源（按优先级）：
    1. 消息 XML 中已有的微信内置转文字（在 db_reader 中已提取为 [语音转文字: ...]）
    2. 定位本地语音文件 + Whisper 转写

    修改 messages 列表中的 dict（原地更新），为语音消息添加 _voice_text 字段。
    返回处理后的 messages（同一引用）。
    """
    if wechat_data_root is None:
        wechat_data_root = config.WECHAT_DATA_ROOT

    # 筛选语音消息（type=34）且尚未有内置转文字的
    voice_msgs = []
    for m in messages:
        msg_type = m.get("msg_type", 0)
        base = msg_type & 0xFF if msg_type > 255 else msg_type
        if base != 34:
            continue
        # 如果 db_reader 已经提取了内置转文字，记录到 _voice_text 并跳过
        content = m.get("content", "")
        if content.startswith("[语音转文字:"):
            m["_voice_text"] = content.replace("[语音转文字: ", "").rstrip("]")
            continue
        voice_msgs.append(m)

    if not voice_msgs:
        return messages

    if not wechat_data_root.exists():
        logger.warning(f"[语音处理] 微信数据目录不存在: {wechat_data_root}")
        return messages

    if len(voice_msgs) > MAX_VOICES_PER_CHUNK:
        if progress_cb:
            progress_cb(
                f"[语音处理] 发现 {len(voice_msgs)} 条语音，"
                f"仅处理前 {MAX_VOICES_PER_CHUNK} 条"
            )
        voice_msgs = voice_msgs[:MAX_VOICES_PER_CHUNK]

    if progress_cb:
        progress_cb(f"[语音处理] 正在转写 {len(voice_msgs)} 条语音...")

    processed_count = 0
    for msg in voice_msgs:
        raw_content = msg.get("raw_content", "")
        create_time = msg.get("timestamp", 0)

        # 尝试定位本地语音文件
        local_path = find_voice_file(
            msg_raw_content=raw_content,
            wechat_data_root=wechat_data_root,
            create_time=create_time,
        )

        if not local_path:
            logger.debug(f"[语音处理] 未找到本地语音文件: ts={create_time}")
            continue

        logger.info(f"[语音处理] 找到语音: {local_path}")

        # Whisper 转写
        try:
            text = transcribe_voice(local_path)
            if text:
                msg["_voice_text"] = text
                msg["_voice_local_path"] = str(local_path)
                processed_count += 1
                if progress_cb:
                    preview = text[:80].replace("\n", " ")
                    progress_cb(f"  [语音 {processed_count}] {preview}...")
        except Exception as e:
            logger.error(f"[语音处理] 转写失败 {local_path}: {e}")

    if progress_cb:
        progress_cb(f"[语音处理] 完成: {processed_count}/{len(voice_msgs)} 条语音转写成功")

    return messages


# ===== URL 内容抓取 =====

_URL_PATTERN = re.compile(r'\[链接: (https?://[^\]\s]+)\]')


def _fetch_url_content(url: str, timeout: int = 10) -> str:
    """抓取 URL 页面的正文文本，返回截断后的纯文本（最多 3000 字符）。

    - 对微信公众号文章 (mp.weixin.qq.com) 做专门解析
    - 优先使用 BeautifulSoup，不可用时回退到正则剥离 HTML
    - 所有异常静默处理，返回空字符串
    """
    try:
        import requests
    except ImportError:
        logger.debug("requests 未安装，跳过 URL 抓取")
        return ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text
    except Exception as e:
        logger.debug(f"URL 抓取失败 ({url}): {e}")
        return ""

    text = _html_to_text(html, url)
    # 截断到 3000 字符
    if len(text) > 3000:
        text = text[:3000] + "…"
    return text.strip()


def _html_to_text(html: str, url: str = "") -> str:
    """将 HTML 转为可读纯文本。优先用 BeautifulSoup，否则用正则。"""
    try:
        from bs4 import BeautifulSoup
        return _bs4_extract(html, url)
    except ImportError:
        return _regex_strip_html(html)


def _bs4_extract(html: str, url: str = "") -> str:
    """用 BeautifulSoup 提取正文"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # 移除无用标签
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    # 微信公众号文章：正文在 div#js_content
    if "mp.weixin.qq.com" in url:
        article = soup.find("div", id="js_content")
        if article:
            return article.get_text(separator="\n", strip=True)

    # 通用：尝试 <article> 标签
    article = soup.find("article")
    if article:
        return article.get_text(separator="\n", strip=True)

    # 回退：取 body 文本
    body = soup.find("body")
    if body:
        return body.get_text(separator="\n", strip=True)

    return soup.get_text(separator="\n", strip=True)


def _regex_strip_html(html: str) -> str:
    """用正则粗略去除 HTML 标签，提取纯文本（回退方案）"""
    import re
    # 移除 script / style 块
    text = re.sub(r'<(script|style)[^>]*>[\s\S]*?</\1>', '', html, flags=re.IGNORECASE)
    # 移除所有 HTML 标签
    text = re.sub(r'<[^>]+>', ' ', text)
    # 合并空白
    text = re.sub(r'\s+', ' ', text)
    # 解码常见 HTML 实体
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    return text.strip()


def _enrich_snapshot_with_url_content(snapshot: str, max_urls: int = 5) -> str:
    """检测快照中的 [链接: URL] 标记，抓取内容并追加到快照末尾。

    最多抓取 max_urls 个链接，避免过多请求拖慢分析。
    """
    urls = _URL_PATTERN.findall(snapshot)
    if not urls:
        return snapshot

    # 去重并限制数量
    seen = set()
    unique_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)
    unique_urls = unique_urls[:max_urls]

    fetched_parts = []
    for url in unique_urls:
        content = _fetch_url_content(url)
        if content:
            fetched_parts.append(f"\n--- 链接内容: {url} ---\n{content}")
            logger.debug(f"成功抓取链接内容: {url} ({len(content)} 字符)")

    if fetched_parts:
        snapshot += "\n\n===== 以下是消息中分享链接的正文内容 =====\n"
        snapshot += "\n".join(fetched_parts)

    return snapshot


# ===== 区域扫描 =====

def area_scan(
    reader: WeChatDBReader,
    wxid: str,
    chat_name: str,
    center_offset: int = 0,
    window_size: int = 30,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """
    区域扫描：抓取指定区域的 ~N 条消息，分析是否有可入库的项目信息。

    wxid: 聊天对象 wxid
    center_offset: 中心偏移（0 = 最新消息）
    window_size: 窗口大小
    progress_cb: 进度回调

    返回分析结果 dict 或 None（无项目信息）
    """
    if progress_cb:
        progress_cb(f"正在读取 {chat_name} 的消息...")

    messages = reader.get_messages(wxid, limit=window_size, offset=center_offset)

    if not messages:
        if progress_cb:
            progress_cb("未找到消息。")
        return None

    if progress_cb:
        progress_cb(f"读取 {len(messages)} 条消息，正在分析...")

    # 预处理图片消息：定位本地文件并用 Claude vision 提取文字
    _process_images_in_messages(messages, progress_cb=progress_cb)
    # 预处理语音消息：尝试转写
    _process_voice_in_messages(messages, progress_cb=progress_cb)

    snapshot = format_messages_for_ai(messages, chat_name)

    # 抓取链接正文内容，丰富快照供 Claude 分析
    snapshot = _enrich_snapshot_with_url_content(snapshot, max_urls=5)

    # 双 Agent 分析
    try:
        validated = _dual_agent_analyze(snapshot, chat_name, progress_cb)
        return validated if validated else None
    except Exception as e:
        logger.error(f"区域扫描分析失败: {e}")
        if progress_cb:
            progress_cb(f"分析出错: {str(e)[:100]}")
        return None


# ===== 全局扫描 =====

def full_scan(
    reader: WeChatDBReader,
    wxid: str,
    chat_name: str,
    batch_size: int = 50,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """
    全局扫描：扫描整个聊天历史，分批分析。

    返回所有发现的项目信息列表。
    """
    total_count = reader.get_message_count(wxid)
    if total_count == 0:
        if progress_cb:
            progress_cb(f"{chat_name}: 无消息")
        return []

    if progress_cb:
        progress_cb(f"开始全局扫描 {chat_name}: 共 {total_count} 条消息")

    results = []
    offset = 0
    batch_num = 0

    while offset < total_count:
        batch_num += 1
        messages = reader.get_messages(wxid, limit=batch_size, offset=offset)

        if not messages:
            break

        if progress_cb:
            progress_cb(
                f"[{chat_name}] 扫描批次 {batch_num} "
                f"({offset+1}-{offset+len(messages)}/{total_count})"
            )

        # 快速预筛：检查是否有项目相关关键词
        combined_text = " ".join(m.get("content", "") for m in messages)
        if _has_deal_keywords(combined_text):
            # 预处理图片消息
            _process_images_in_messages(messages, progress_cb=progress_cb)
            # 预处理语音消息
            _process_voice_in_messages(messages, progress_cb=progress_cb)
            snapshot = format_messages_for_ai(messages, chat_name)
            # 抓取链接正文内容，丰富快照供 Claude 分析
            snapshot = _enrich_snapshot_with_url_content(snapshot, max_urls=5)
            batch_results = _analyze_batch(snapshot, chat_name)
            if batch_results:
                for r in batch_results:
                    r["batch_offset"] = offset
                    r["batch_size"] = len(messages)
                    results.append(r)
                    if progress_cb:
                        rtype = r.get("record_type", "")
                        progress_cb(
                            f"  ✅ 发现: {r.get('company_name', '未知')} "
                            f"[{rtype}] [{r.get('confidence', '')}]"
                        )
        else:
            logger.debug(f"批次 {batch_num} 无项目关键词，跳过分析")

        offset += batch_size
        time.sleep(1)  # 避免 Claude API 限流

    if progress_cb:
        progress_cb(
            f"[{chat_name}] 全局扫描完成: "
            f"共 {total_count} 条消息，发现 {len(results)} 个项目"
        )

    return results


def _has_deal_keywords(text: str) -> bool:
    """快速检查文本中是否包含项目/投资相关关键词"""
    keywords = [
        "融资", "天使", "轮", "估值", "BP", "pitch", "创始人",
        "CEO", "CTO", "赛道", "行业", "入库", "项目", "投资",
        "Pre-A", "Series", "seed", "商业模式", "市场规模",
        "ARR", "MRR", "GMV", "用户", "增长", "团队",
        "孵化", "加速", "LP", "GP", "基金",
    ]
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _dedup_results(results: list[dict]) -> list[dict]:
    """跨 chunk 去重：同一公司出现在多个 chunk 中时合并为一条，保留置信度最高的"""
    if not results:
        return []

    confidence_rank = {"high": 3, "medium": 2, "low": 1}

    def _normalize(name: str) -> str:
        import re
        s = re.sub(r'[（(].*?[）)]', '', name)
        return s.replace(" ", "").lower()

    groups: dict[str, list[dict]] = {}
    for r in results:
        name = r.get("company_name", "")
        norm = _normalize(name)
        if not norm:
            continue
        # Check if this normalised name matches an existing group (containment)
        matched_key = None
        for key in groups:
            if norm == key or (len(norm) >= 2 and (norm in key or key in norm)):
                matched_key = key
                break
        if matched_key:
            groups[matched_key].append(r)
        else:
            groups[norm] = [r]

    deduped = []
    for _key, items in groups.items():
        # Pick the one with highest confidence
        best = max(items, key=lambda x: confidence_rank.get(x.get("confidence", "low"), 0))
        # Merge summaries if multiple chunks found the same company
        if len(items) > 1:
            all_summaries = [it.get("summary", "") for it in items if it.get("summary")]
            if all_summaries:
                best["summary"] = " | ".join(all_summaries)
            best["chunk_appearances"] = len(items)
        deduped.append(best)

    return deduped


def smart_scan(
    reader: "WeChatDBReader",
    wxid: str,
    chat_name: str,
    chunk_size: int = 50,
    max_workers: int = 3,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """
    智能扫描：读取全部消息 → 分 chunk → 并发分析 → 去重合并。

    1. 读取选定聊天的全部消息
    2. 按 chunk_size 切分
    3. 用 _has_deal_keywords 预筛，跳过无关 chunk
    4. ThreadPoolExecutor 并发（max_workers）调用 _dual_agent_analyze
    5. 最终 _dedup_results 去重
    """
    total_count = reader.get_message_count(wxid)
    if total_count == 0:
        if progress_cb:
            progress_cb(f"{chat_name}: 无消息")
        return []

    if progress_cb:
        progress_cb(f"[智能扫描] {chat_name}: 共 {total_count} 条消息，读取中...")

    # Step 1: 读取全部消息
    all_messages = reader.get_messages(wxid, limit=total_count, offset=0)
    if not all_messages:
        if progress_cb:
            progress_cb(f"{chat_name}: 读取消息为空")
        return []

    if progress_cb:
        progress_cb(f"[智能扫描] 读取完成: {len(all_messages)} 条，切分 chunk...")

    # Step 2: 切分 chunks
    chunks = []
    for i in range(0, len(all_messages), chunk_size):
        chunk_msgs = all_messages[i : i + chunk_size]
        chunks.append((i, chunk_msgs))

    total_chunks = len(chunks)
    if progress_cb:
        progress_cb(f"[智能扫描] 共 {total_chunks} 个 chunk (每个 {chunk_size} 条)")

    # Step 3: 预筛 + 构建待分析列表
    analysis_tasks = []  # (chunk_idx, offset, snapshot, chunk_ts_ms)
    skipped = 0
    for chunk_idx, (offset, chunk_msgs) in enumerate(chunks):
        combined_text = " ".join(m.get("content", "") for m in chunk_msgs)
        if _has_deal_keywords(combined_text):
            # 预处理图片消息
            _process_images_in_messages(chunk_msgs, progress_cb=progress_cb)
            # 预处理语音消息
            _process_voice_in_messages(chunk_msgs, progress_cb=progress_cb)
            snapshot = format_messages_for_ai(chunk_msgs, chat_name)
            # Capture the latest message timestamp from this chunk (seconds → ms)
            timestamps = [m.get("timestamp", 0) for m in chunk_msgs if m.get("timestamp")]
            chunk_ts_ms = int(max(timestamps) * 1000) if timestamps else int(time.time() * 1000)
            analysis_tasks.append((chunk_idx, offset, snapshot, chunk_ts_ms))
        else:
            skipped += 1

    if progress_cb:
        progress_cb(
            f"[智能扫描] 预筛完成: {len(analysis_tasks)} 个 chunk 含关键词，"
            f"{skipped} 个跳过"
        )

    if not analysis_tasks:
        if progress_cb:
            progress_cb(f"[智能扫描] {chat_name}: 无项目相关内容")
        return []

    # Step 4: 并发分析
    all_results = []
    completed_count = 0
    lock = __import__("threading").Lock()

    def _analyze_one(task):
        chunk_idx, offset, snapshot, chunk_ts = task
        try:
            # 抓取链接正文内容，丰富快照供 Claude 分析
            snapshot = _enrich_snapshot_with_url_content(snapshot, max_urls=5)
            validated = _dual_agent_analyze(snapshot, chat_name)
            if validated:
                for r in validated:
                    r["chunk_offset"] = offset
                    r["message_timestamp"] = chunk_ts
                return validated
        except Exception as e:
            logger.error(f"Chunk {chunk_idx} 分析失败: {e}")
        return []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(_analyze_one, task): task
            for task in analysis_tasks
        }
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            chunk_idx = task[0]
            with lock:
                completed_count += 1
            try:
                chunk_results = future.result()
                if chunk_results:
                    all_results.extend(chunk_results)
                    if progress_cb:
                        for r in chunk_results:
                            rtype = r.get("record_type", "")
                            progress_cb(
                                f"  [Chunk {chunk_idx+1}] "
                                f"发现: {r.get('company_name', '未知')} "
                                f"[{rtype}] [{r.get('confidence', '')}]"
                            )
                if progress_cb:
                    progress_cb(
                        f"[智能扫描] 进度: {completed_count}/{len(analysis_tasks)} chunks 完成"
                    )
            except Exception as e:
                logger.error(f"Chunk {chunk_idx} future 异常: {e}")
                if progress_cb:
                    progress_cb(f"  [Chunk {chunk_idx+1}] 出错: {str(e)[:80]}")

    # Step 5: 去重
    if all_results:
        before_dedup = len(all_results)
        all_results = _dedup_results(all_results)
        if progress_cb and before_dedup != len(all_results):
            progress_cb(
                f"[智能扫描] 去重: {before_dedup} → {len(all_results)} 个项目"
            )

    # Step 6: 黑名单过滤（用户曾经忽略过的项目名）
    if all_results:
        store = get_store()
        filtered = []
        skipped_names = []
        for r in all_results:
            cname = r.get("company_name", "")
            if cname and store.is_name_blacklisted(cname):
                skipped_names.append(cname)
                continue
            filtered.append(r)
        if skipped_names and progress_cb:
            progress_cb(
                f"[智能扫描] 黑名单过滤: 跳过 {len(skipped_names)} 个项目 "
                f"({', '.join(skipped_names[:3])}{'...' if len(skipped_names)>3 else ''})"
            )
        all_results = filtered

    if progress_cb:
        progress_cb(
            f"[智能扫描] {chat_name} 完成: "
            f"共 {total_count} 条消息，发现 {len(all_results)} 个项目"
        )

    return all_results


def _analyze_batch(snapshot: str, chat_name: str) -> Optional[list]:
    """分析一个消息批次（使用双 Agent 架构）"""
    validated = _dual_agent_analyze(snapshot, chat_name)
    return validated if validated else None


# ===== 写入 bitable =====

def _get_existing_projects() -> dict[str, str]:
    """从 bitable 获取已有项目名 → record_id 映射（用于跨源去重）

    自动分页获取全部记录，每页 500 条。
    """
    result_map: dict[str, str] = {}
    page_token: str | None = None

    try:
        while True:
            params: dict = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token

            output = _run_lark_cli([
                "api", "GET",
                f"/open-apis/bitable/v1/apps/{config.BITABLE_APP_TOKEN}"
                f"/tables/{config.BITABLE_TABLE_ID}/records",
                "--params", json.dumps(params),
                "--as", "bot",
            ], timeout=30)
            data = json.loads(output)
            items = data.get("data", {}).get("items", [])
            for item in items:
                name = item.get("fields", {}).get("项目/公司名", "")
                rid = item.get("record_id", "")
                if name:
                    result_map[name] = rid

            # Check if there are more pages
            has_more = data.get("data", {}).get("has_more", False)
            page_token = data.get("data", {}).get("page_token")
            if not has_more or not page_token:
                break

    except Exception as e:
        logger.warning(f"获取已有项目失败: {e}")

    return result_map


def _match_project(company_name: str, existing: dict[str, str]) -> tuple[str, str]:
    """
    智能匹配项目名，返回 (matched_name, record_id)。
    支持模糊匹配（忽略空格、大小写、括号内容）。
    """
    if not company_name or not existing:
        return "", ""

    # 精确匹配
    if company_name in existing:
        return company_name, existing[company_name]

    # 归一化匹配
    def normalize(s: str) -> str:
        import re
        s = re.sub(r'[（(].*?[）)]', '', s)  # 去括号内容
        s = s.replace(" ", "").lower()
        return s

    norm_name = normalize(company_name)
    for proj_name, rid in existing.items():
        if normalize(proj_name) == norm_name:
            return proj_name, rid
        # 包含关系
        if len(norm_name) >= 2 and (norm_name in normalize(proj_name) or normalize(proj_name) in norm_name):
            return proj_name, rid

    return "", ""


def _is_junk_company_name(name: str) -> bool:
    """检查公司名是否为垃圾值（与 feishu-deal-bot 保持一致）"""
    import re
    if not name or not name.strip():
        return True
    name = name.strip()
    if name in ("待补充", "未知", "文档", "无", "N/A", "unknown"):
        return True
    if any(ext in name.lower() for ext in (".pdf", ".docx", ".pptx", ".xlsx")):
        return True
    if re.search(r'(20\d{2}[01]\d[0-3]\d|BP\d{4,}|纪要|会议记录)', name):
        return True
    if len(name) > 30:
        return True
    return False


def write_to_bitable(
    analysis: dict,
    source_chat: str = "",
) -> dict:
    """将分析结果写入飞书多维表格（带跨源去重 + 子记录）

    返回 dict：
      {"ok": True, "record_id": "...", "company_name": "..."} 写入成功
      {"ok": False, "error": "..."} 写入失败 / 被跳过
    """
    if not config.BITABLE_APP_TOKEN or not config.BITABLE_TABLE_ID:
        logger.warning("BITABLE 未配置，跳过写入")
        return {"ok": False, "error": "BITABLE 未配置"}

    # Use actual message timestamp if available, fall back to current time
    meeting_date_ms = analysis.get("message_timestamp", int(time.time() * 1000))
    company_name = analysis.get("company_name", "待补充")

    # 垃圾项目名检查：尝试从摘要中补全
    if _is_junk_company_name(company_name):
        summary = analysis.get("summary", "")
        if summary and len(summary) > 20:
            try:
                extraction_prompt = (
                    f"Given this info about a company:\n"
                    f"Summary: {summary}\n"
                    f"Industry: {analysis.get('industry', '')}\n"
                    f"Founder: {analysis.get('founder_name', '')}\n"
                    f"\nWhat is the company name? Return ONLY the company name.\n"
                    f'If you truly cannot determine it, return "待补充".'
                )
                extracted = _call_claude(extraction_prompt, timeout=30).strip().strip('"').strip("'")
                if extracted and not _is_junk_company_name(extracted):
                    logger.info(f"[微信写入] 项目名补全: '{company_name}' -> '{extracted}'")
                    company_name = extracted
                    analysis["company_name"] = extracted
            except Exception as e:
                logger.warning(f"[微信写入] 项目名补全失败: {e}")

    # 仍然是垃圾名 + low confidence → 跳过写入
    if _is_junk_company_name(company_name) and analysis.get("confidence") == "low":
        logger.info(f"[微信写入] 跳过垃圾记录: name='{company_name}' confidence=low")
        return {"ok": False, "error": f"项目名无效（{company_name}）且置信度低"}

    # 跨源去重：检查是否已有同名项目（无论来源是飞书还是微信）
    existing = _get_existing_projects()
    matched_name, parent_rid = _match_project(company_name, existing)

    if matched_name:
        company_name = matched_name  # 用已有的项目名保持一致
        logger.info(f"[跨源去重] 匹配到已有项目: {matched_name} (rid={parent_rid})")

    # 后处理：确保创始人不是内部员工
    founder_name = analysis.get("founder_name", "")
    if founder_name:
        staff_lower = {s.lower() for s in config.STAFF_NAMES}
        cleaned = []
        for name in founder_name.replace("、", ",").replace("，", ",").split(","):
            name = name.strip()
            if not name:
                continue
            is_staff = name.lower() in staff_lower or any(
                name.lower() in s.lower() or s.lower() in name.lower()
                for s in config.STAFF_NAMES if len(s) >= 2
            )
            if not is_staff:
                cleaned.append(name)
        founder_name = ", ".join(cleaned)

    fields = {
        "项目/公司名": company_name,
        "会议类型": analysis.get("meeting_type", "微信讨论"),
        "创始人/联系人": founder_name,
        "会议日期": meeting_date_ms,
        "参会同事": analysis.get("internal_attendees", ""),
        "AI摘要": analysis.get("summary", ""),
        "赛道/行业": analysis.get("industry", ""),
        "融资阶段": analysis.get("funding_stage", ""),
        "关键观点": analysis.get("key_insights", ""),
        # 妙记链接是 URL 类型(type=15)，微信来源无有效 URL，用录入人标记来源

        "识别置信度": analysis.get("confidence", "low"),
        "录入人": f"微信扫描器 - {source_chat}" if source_chat else "微信扫描器",
    }

    # 子记录：如果匹配到已有项目，作为子记录挂在父项目下
    if parent_rid and parent_rid != "unknown":
        fields["父记录"] = [parent_rid]

    # 去掉空值
    fields = {k: v for k, v in fields.items() if v not in (None, "", 0)}

    try:
        body = json.dumps({"fields": fields}, ensure_ascii=False)
        output = _run_lark_cli([
            "api", "POST",
            f"/open-apis/bitable/v1/apps/{config.BITABLE_APP_TOKEN}"
            f"/tables/{config.BITABLE_TABLE_ID}/records",
            "--data", body,
            "--as", "bot",
        ])
        record_id = ""
        try:
            resp = json.loads(output)
            record_id = (
                resp.get("data", {}).get("record", {}).get("record_id", "")
                or resp.get("data", {}).get("record_id", "")
            )
        except Exception:
            pass
        if matched_name:
            logger.info(f"写入子记录成功: {company_name} (父={matched_name}) rid={record_id}")
        else:
            logger.info(f"写入新项目成功: {company_name} rid={record_id}")

        # Wiki 知识库：写入成功后同步更新公司 wiki 页面
        try:
            from services.wiki_service import get_wiki_service
            wiki = get_wiki_service()
            wiki.save_deal_knowledge(analysis, source="微信扫描")
        except Exception as e:
            logger.warning(f"[Wiki] 微信扫描 wiki 写入失败（非致命）: {e}")

        return {"ok": True, "record_id": record_id, "company_name": company_name}
    except Exception as e:
        logger.error(f"写入 bitable 失败: {e}")
        return {"ok": False, "error": str(e)[:200]}


# =====================================================
# Accessibility API 版本（基于文本输入，不依赖 db_reader）
# =====================================================

def area_scan_from_text(
    msg_text: str,
    chat_name: str,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Optional[dict]:
    """
    区域扫描（文本版）：直接分析格式化的聊天文本。
    用于 Accessibility API 读取的消息。
    """
    if not msg_text or len(msg_text.strip()) < 20:
        if progress_cb:
            progress_cb("消息内容太短，跳过。")
        return None

    if progress_cb:
        progress_cb("正在用 AI 分析...")

    system = f"""你是一个投资机构的会议分析助手。你的任务是分析微信聊天记录，提取项目/投资相关的信息。

核心规则：
1. 仔细阅读整段聊天，识别是否有关于创业公司/投资项目的讨论
2. 如果聊天中提到了公司名、创始人、融资、赛道等信息，提取并结构化
3. 如果聊天与投资项目完全无关（闲聊、行政等），meeting_type 填 "无关内容"
4. 摘要要简洁专业

内部员工名单：{', '.join(config.STAFF_NAMES)}"""

    prompt = f"""分析以下微信聊天记录，以 JSON 格式返回。

## 聊天记录
{msg_text[:30000]}

## 返回 JSON：
{{
  "meeting_type": "微信讨论|项目推荐|BP分享|其他|无关内容",
  "company_name": "公司名称",
  "founder_name": "创始人",
  "internal_attendees": "内部同事",
  "industry": "赛道/行业",
  "funding_stage": "融资阶段",
  "summary": "3-5句话摘要",
  "key_insights": "关键观点",
  "confidence": "high|medium|low",
  "confidence_reason": "依据"
}}"""

    try:
        text = _call_claude(prompt, system)
        result = _parse_json_response(text)
        result["source_type"] = "wechat"

        if progress_cb:
            if result.get("meeting_type") == "无关内容":
                progress_cb("分析完成：该段聊天与项目无关。")
            else:
                progress_cb(
                    f"发现项目: {result.get('company_name', '未知')} "
                    f"[{result.get('confidence', 'low')}]"
                )

        return result
    except Exception as e:
        logger.error(f"区域扫描分析失败: {e}")
        if progress_cb:
            progress_cb(f"分析出错: {str(e)[:100]}")
        return None


def full_scan_from_text(
    msg_text: str,
    chat_name: str,
    batch_size: int = 3000,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """
    全局扫描（文本版）：将长文本分批分析。
    batch_size: 每批字符数
    """
    if not msg_text:
        return []

    results = []
    lines = msg_text.split("\n")
    batch_lines = []
    batch_chars = 0
    batch_num = 0

    for line in lines:
        batch_lines.append(line)
        batch_chars += len(line)

        if batch_chars >= batch_size:
            batch_num += 1
            batch_text = "\n".join(batch_lines)

            if _has_deal_keywords(batch_text):
                if progress_cb:
                    progress_cb(f"分析第 {batch_num} 批...")

                result = _analyze_batch(batch_text, chat_name)
                if result and result.get("meeting_type") != "无关内容":
                    results.append({"chat": chat_name, "result": result})
                    if progress_cb:
                        progress_cb(
                            f"  ✅ 发现: {result.get('company_name', '未知')}"
                        )
            batch_lines = []
            batch_chars = 0
            time.sleep(1)

    # 处理最后一批
    if batch_lines:
        batch_num += 1
        batch_text = "\n".join(batch_lines)
        if _has_deal_keywords(batch_text):
            if progress_cb:
                progress_cb(f"分析第 {batch_num} 批...")
            result = _analyze_batch(batch_text, chat_name)
            if result and result.get("meeting_type") != "无关内容":
                results.append({"chat": chat_name, "result": result})

    if progress_cb:
        progress_cb(f"全局扫描完成：{len(results)} 个项目")

    return results


# =====================================================
# PDF 扫描（微信文件附件 → BP 分析）
# =====================================================

def _classify_pdf_is_bp(text: str, filename: str) -> dict:
    """判断 PDF 是否是商业计划书"""
    preview = text[:5000]
    prompt = f"""请判断以下 PDF 是否是创业公司的商业计划书（BP / Pitch Deck）。

文件名：{filename}

文档内容（前 5000 字符）：
{preview}

判断标准：
- BP 通常包含：公司介绍、团队、产品、市场、商业模式、融资需求等
- 非 BP：研究报告、新闻文章、合同、技术文档、个人简历等

只返回 JSON：
{{"is_bp": true/false, "reason": "理由", "company_name": "如果是BP填公司名，否则空"}}"""
    try:
        text = _call_claude(prompt, timeout=60)
        return _parse_json_response(text)
    except Exception as e:
        logger.error(f"PDF 分类失败: {e}")
        return {"is_bp": False, "reason": f"分类出错: {e}", "company_name": ""}


def _analyze_pdf_bp(text: str, filename: str, chat_name: str) -> Optional[dict]:
    """分析 BP 内容，返回结构化信息"""
    truncated = text[:50000]
    if len(text) > 50000:
        truncated += f"\n\n[... 已截断，总长度 {len(text)} ...]"

    system = f"""你是投资机构的 BP 分析助手。

内部员工名单：{', '.join(config.STAFF_NAMES)}
被投企业：{_portfolio_list_str()}

任务：从商业计划书中提取结构化信息。注意：
1. 摘要要突出商业模式、市场机会、团队亮点
2. key_insights 留空（BP 只有创始人陈述，无投资团队评价）
3. 若公司在被投名单，record_type="被投跟踪"，否则"新项目" """

    prompt = f"""分析以下商业计划书，以 JSON 返回。

## 文件名
{filename}

## 来源微信聊天
{chat_name}

## 文档内容
{truncated}

只返回 JSON：
{{
  "record_type": "新项目|被投跟踪",
  "meeting_type": "BP",
  "company_name": "公司名",
  "founder_name": "创始人/CEO",
  "internal_attendees": "",
  "industry": "赛道/行业",
  "funding_stage": "融资阶段",
  "summary": "3-5句摘要：做什么、商业模式、优势、团队",
  "key_insights": "",
  "confidence": "high|medium|low",
  "confidence_reason": "依据"
}}"""

    try:
        text_out = _call_claude(prompt, system, timeout=180)
        result = _parse_json_response(text_out)
        result["source_type"] = "wechat_pdf"
        return result
    except Exception as e:
        logger.error(f"BP 分析失败: {e}")
        return None


def scan_pdfs_in_chat(
    reader: WeChatDBReader,
    wxid: str,
    chat_name: str,
    max_workers: int = 2,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """扫描某个聊天中收到的 PDF 附件，判定是否是 BP 并分析入库信息。

    流程：读取全部消息 → 筛选 type=49 的 PDF 附件 → 本地定位文件 →
    提取文本 → Claude 分类 → 若是 BP 则分析 → 返回待入库记录列表。
    """
    total = reader.get_message_count(wxid)
    if total == 0:
        return []

    if progress_cb:
        progress_cb(f"[PDF扫描] {chat_name}: 共 {total} 条消息，查找 PDF 附件...")

    all_messages = reader.get_messages(wxid, limit=total, offset=0)

    # 提取所有 PDF 附件（按 md5 去重）
    pdf_tasks = []
    seen_md5 = set()
    for msg in all_messages:
        base_type = msg.get("msg_type", 0)
        if base_type > 255:
            base_type &= 0xFF
        if base_type != 49:
            continue
        raw = msg.get("raw_content", "")
        meta = parse_file_metadata(raw)
        if not meta:
            continue
        md5 = meta.get("md5") or meta["title"]
        if md5 in seen_md5:
            continue
        seen_md5.add(md5)
        pdf_tasks.append({
            "title": meta["title"],
            "md5": meta["md5"],
            "size": meta["size"],
            "create_time": msg.get("timestamp", 0),
            "sender": msg.get("sender", ""),
        })

    if progress_cb:
        progress_cb(f"[PDF扫描] 发现 {len(pdf_tasks)} 个 PDF 附件")

    if not pdf_tasks:
        return []

    wechat_root = Path(config.WECHAT_DATA_ROOT)
    store = get_store()

    def _process_one(task: dict) -> Optional[dict]:
        title = task["title"]
        md5 = task.get("md5") or ""
        try:
            # 黑名单：用户之前忽略过这份 PDF
            if md5 and store.is_pdf_blacklisted(md5):
                if progress_cb:
                    progress_cb(f"  🚫 已忽略（黑名单）: {title}")
                return None

            # 缓存命中：分析结果在本地，直接复用
            cached = store.get_pdf_cache(md5) if md5 else None
            if cached and cached.get("is_bp") == 0:
                if progress_cb:
                    progress_cb(f"  ⏭️  缓存命中（非 BP，跳过）: {title}")
                return None
            if cached and cached.get("is_bp") == 1 and cached.get("analysis"):
                if progress_cb:
                    progress_cb(f"  ♻️  缓存命中（复用分析）: {title}")
                analysis = dict(cached["analysis"])
                analysis.setdefault("file_name", title)
                analysis.setdefault("sender", task.get("sender", ""))
                analysis["_pdf_md5"] = md5
                return analysis

            local = find_local_pdf(title, wechat_root, task.get("create_time", 0))
            if not local:
                if progress_cb:
                    progress_cb(f"  ⚠️  未找到本地文件: {title}")
                return None

            # 已提取过文本但没分析？复用文本，跳过提取
            if cached and cached.get("extracted_text"):
                text = cached["extracted_text"]
                if progress_cb:
                    progress_cb(f"  ♻️  复用缓存文本: {title}")
            else:
                if progress_cb:
                    progress_cb(f"  📄 提取文本: {title}")
                text = extract_pdf_text(local)
                if md5 and text:
                    store.set_pdf_cache(md5, title, text=text)

            if not text or len(text.strip()) < 50:
                if progress_cb:
                    progress_cb(f"  ⚠️  {title} 内容太短（可能是扫描件）")
                if md5:
                    store.set_pdf_cache(md5, title, is_bp=False)
                return None

            classification = _classify_pdf_is_bp(text, title)
            is_bp = bool(classification.get("is_bp"))
            if md5:
                store.set_pdf_cache(md5, title, is_bp=is_bp, classification=classification)

            if not is_bp:
                if progress_cb:
                    progress_cb(f"  ⏭️  {title} 非 BP: {classification.get('reason', '')[:60]}")
                return None

            if progress_cb:
                progress_cb(f"  ✅ {title} 是 BP，分析中...")
            analysis = _analyze_pdf_bp(text, title, chat_name)
            if not analysis:
                return None

            analysis["file_name"] = title
            analysis["sender"] = task.get("sender", "")
            analysis["_pdf_md5"] = md5

            # 存完整分析到缓存
            if md5:
                store.set_pdf_cache(md5, title, is_bp=True, analysis=analysis)

            # 名称黑名单：已经被用户忽略过同名项目，这次也跳过
            cname = analysis.get("company_name", "")
            if cname and store.is_name_blacklisted(cname):
                if progress_cb:
                    progress_cb(f"  🚫 项目名在黑名单: {cname}")
                return None

            if progress_cb:
                rtype = analysis.get("record_type", "")
                progress_cb(
                    f"  ✅ [{rtype}] {cname or '?'} "
                    f"[{analysis.get('confidence', '')}]"
                )
            return analysis
        except Exception as e:
            logger.error(f"处理 PDF 失败 {title}: {e}")
            if progress_cb:
                progress_cb(f"  ❌ {title} 出错: {str(e)[:80]}")
            return None

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_process_one, t) for t in pdf_tasks]
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                results.append(r)

    if progress_cb:
        progress_cb(f"[PDF扫描] {chat_name} 完成: {len(results)} 个 BP 待入库")

    return results
