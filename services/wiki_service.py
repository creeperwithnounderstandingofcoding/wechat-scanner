"""知识库 Wiki 服务：为每个公司累积 deal 知识，存储为 Markdown 文件。

每次 deal 分析结果写入 Bitable 后，同步生成/更新对应公司的 Wiki 页面。
支持搜索、浏览、为 memo 生成上下文。
"""

import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BJT = timezone(timedelta(hours=8))

# 单例锁，防止并发写同一文件
_write_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()


def _get_file_lock(filepath: str) -> threading.Lock:
    """获取文件级别的锁"""
    with _global_lock:
        if filepath not in _write_locks:
            _write_locks[filepath] = threading.Lock()
        return _write_locks[filepath]


def _normalize_name(name: str) -> str:
    """将公司名规范化为安全的文件名（小写，去特殊字符）"""
    if not name:
        return "unknown"
    # 保留中文、字母、数字
    cleaned = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", name.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return "unknown"
    return cleaned.lower()


class WikiService:
    """公司知识库服务"""

    def __init__(self, wiki_dir: str = ""):
        """
        Args:
            wiki_dir: Wiki 文件存储目录。优先级：
                1. 传入参数
                2. 环境变量 WIKI_DIR
                3. 生产默认 /opt/wiki/companies
                4. 本地开发回退 ./data/wiki/
        """
        if wiki_dir:
            self.wiki_dir = Path(wiki_dir)
        else:
            env_dir = os.getenv("WIKI_DIR", "")
            if env_dir:
                self.wiki_dir = Path(env_dir)
            elif os.path.isdir("/opt/wiki") or os.access("/opt", os.W_OK):
                self.wiki_dir = Path("/opt/wiki/companies")
            else:
                self.wiki_dir = Path(__file__).parent.parent / "data" / "wiki"

        # 确保目录存在
        try:
            self.wiki_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[Wiki] 知识库目录: {self.wiki_dir}")
        except Exception as e:
            # 权限不够时回退到本地目录
            logger.warning(f"[Wiki] 无法创建 {self.wiki_dir}: {e}，回退到本地目录")
            self.wiki_dir = Path(__file__).parent.parent / "data" / "wiki"
            self.wiki_dir.mkdir(parents=True, exist_ok=True)

    def _get_filepath(self, company_name: str) -> Path:
        """获取公司 wiki 文件路径"""
        normalized = _normalize_name(company_name)
        return self.wiki_dir / f"{normalized}.md"

    def save_deal_knowledge(self, analysis: dict, source: str = "未知来源") -> str:
        """保存/更新公司的 wiki 页面

        Args:
            analysis: 分析结果字典，包含 company_name, summary, industry 等字段
            source: 来源标识，如 "微信扫描" / "飞书扫描" / "手动录入"

        Returns:
            wiki 文件路径
        """
        company_name = analysis.get("company_name", "").strip()
        if not company_name or company_name == "待补充":
            logger.debug("[Wiki] 公司名无效，跳过 wiki 写入")
            return ""

        filepath = self._get_filepath(company_name)
        file_lock = _get_file_lock(str(filepath))

        with file_lock:
            try:
                if filepath.exists():
                    content = self._update_existing(filepath, analysis, source)
                else:
                    content = self._create_new(analysis, source)

                # 原子写入：先写临时文件，再 rename
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self.wiki_dir), suffix=".md.tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(content)
                    os.replace(tmp_path, str(filepath))
                except Exception:
                    # 清理临时文件
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise

                logger.info(f"[Wiki] 已保存: {company_name} -> {filepath}")
                return str(filepath)

            except Exception as e:
                logger.error(f"[Wiki] 保存失败 ({company_name}): {e}")
                return ""

    def _create_new(self, analysis: dict, source: str) -> str:
        """创建新的 wiki 页面"""
        now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")
        company_name = analysis.get("company_name", "未知公司")
        industry = analysis.get("industry", "")
        funding_stage = analysis.get("funding_stage", "")
        founder_name = analysis.get("founder_name", "")
        meeting_type = analysis.get("meeting_type", "")
        summary = analysis.get("summary", "")
        key_insights = analysis.get("key_insights", "")
        attendees = analysis.get("internal_attendees", "")
        confidence = analysis.get("confidence", "")

        lines = [
            f"# {company_name}",
            "",
            "## 基本信息",
            f"- 赛道: {industry or '待补充'}",
            f"- 融资阶段: {funding_stage or '待补充'}",
            f"- 创始人: {founder_name or '待补充'}",
            f"- 首次记录: {now}",
            f"- 最近更新: {now}",
            "",
            "## 接触时间线",
            "",
        ]

        # 添加第一条时间线条目
        lines.extend(self._format_timeline_entry(
            date=now,
            meeting_type=meeting_type,
            source=source,
            summary=summary,
            key_insights=key_insights,
            attendees=attendees,
        ))

        # 累计分析标签
        confidence_map = {}
        if confidence:
            confidence_map[confidence] = 1
        source_map = {source: 1}

        lines.extend([
            "",
            "## 累计分析标签",
            f"- 提及次数: 1",
            f"- 置信度分布: {self._format_confidence_map(confidence_map)}",
            f"- 来源: {self._format_source_map(source_map)}",
            "",
        ])

        return "\n".join(lines)

    def _update_existing(self, filepath: Path, analysis: dict, source: str) -> str:
        """更新已有的 wiki 页面：追加时间线条目，更新基本信息和统计"""
        existing = filepath.read_text(encoding="utf-8")
        now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")

        company_name = analysis.get("company_name", "")
        industry = analysis.get("industry", "")
        funding_stage = analysis.get("funding_stage", "")
        founder_name = analysis.get("founder_name", "")
        meeting_type = analysis.get("meeting_type", "")
        summary = analysis.get("summary", "")
        key_insights = analysis.get("key_insights", "")
        attendees = analysis.get("internal_attendees", "")
        confidence = analysis.get("confidence", "")

        # --- 更新基本信息（只在新数据更完整时覆盖） ---
        existing = self._update_basic_info(existing, {
            "赛道": industry,
            "融资阶段": funding_stage,
            "创始人": founder_name,
        })
        # 始终更新"最近更新"
        existing = re.sub(
            r"- 最近更新: .+",
            f"- 最近更新: {now}",
            existing,
        )

        # --- 在"接触时间线"下方插入新条目（逆序，最新在前） ---
        new_entry_lines = self._format_timeline_entry(
            date=now,
            meeting_type=meeting_type,
            source=source,
            summary=summary,
            key_insights=key_insights,
            attendees=attendees,
        )
        new_entry = "\n".join(new_entry_lines)

        # 找到 "## 接触时间线" 后面插入
        timeline_marker = "## 接触时间线"
        idx = existing.find(timeline_marker)
        if idx != -1:
            insert_pos = idx + len(timeline_marker)
            # 跳过紧跟的空行
            rest = existing[insert_pos:]
            leading_newlines = len(rest) - len(rest.lstrip("\n"))
            insert_pos += leading_newlines
            existing = (
                existing[:insert_pos]
                + "\n"
                + new_entry
                + "\n"
                + existing[insert_pos:]
            )
        else:
            # 没有时间线区域，追加到末尾
            existing += f"\n## 接触时间线\n\n{new_entry}\n"

        # --- 更新累计分析标签 ---
        existing = self._update_stats(existing, confidence, source)

        return existing

    def _format_timeline_entry(
        self,
        date: str,
        meeting_type: str,
        source: str,
        summary: str,
        key_insights: str,
        attendees: str,
    ) -> list[str]:
        """格式化一条时间线条目"""
        lines = [
            f"### {date} -- {meeting_type or '会议'} ({source})",
            "",
        ]
        if summary:
            lines.append(summary)
            lines.append("")
        if key_insights:
            lines.append("**关键观点:**")
            lines.append(key_insights)
            lines.append("")
        if attendees:
            lines.append(f"**参会人:** {attendees}")
            lines.append("")
        lines.append("---")
        return lines

    def _update_basic_info(self, content: str, updates: dict) -> str:
        """更新基本信息区域的字段（只在现有值为"待补充"或空时更新）"""
        for field, new_val in updates.items():
            if not new_val:
                continue
            pattern = rf"(- {re.escape(field)}: )(.*)"
            match = re.search(pattern, content)
            if match:
                old_val = match.group(2).strip()
                if old_val in ("", "待补充") or (len(new_val) > len(old_val) and old_val != new_val):
                    content = content[:match.start()] + f"- {field}: {new_val}" + content[match.end():]
        return content

    def _update_stats(self, content: str, confidence: str, source: str) -> str:
        """更新累计分析标签区域"""
        # 提及次数 +1
        mention_match = re.search(r"- 提及次数: (\d+)", content)
        if mention_match:
            old_count = int(mention_match.group(1))
            content = content[:mention_match.start()] + f"- 提及次数: {old_count + 1}" + content[mention_match.end():]

        # 置信度分布
        if confidence:
            conf_match = re.search(r"- 置信度分布: (.+)", content)
            if conf_match:
                conf_map = self._parse_kv_string(conf_match.group(1))
                conf_map[confidence] = conf_map.get(confidence, 0) + 1
                new_conf = self._format_confidence_map(conf_map)
                content = content[:conf_match.start()] + f"- 置信度分布: {new_conf}" + content[conf_match.end():]

        # 来源分布
        source_match = re.search(r"- 来源: (.+)", content)
        if source_match:
            src_map = self._parse_source_string(source_match.group(1))
            src_map[source] = src_map.get(source, 0) + 1
            new_src = self._format_source_map(src_map)
            content = content[:source_match.start()] + f"- 来源: {new_src}" + content[source_match.end():]

        return content

    @staticmethod
    def _format_confidence_map(conf_map: dict) -> str:
        """格式化置信度分布: high:2 medium:1"""
        if not conf_map:
            return "无"
        return " ".join(f"{k}:{v}" for k, v in sorted(conf_map.items()))

    @staticmethod
    def _format_source_map(src_map: dict) -> str:
        """格式化来源分布: 微信(2), 飞书(1)"""
        if not src_map:
            return "无"
        return ", ".join(f"{k}({v})" for k, v in sorted(src_map.items()))

    @staticmethod
    def _parse_kv_string(s: str) -> dict:
        """解析 'high:2 medium:1' 格式"""
        result = {}
        for pair in s.strip().split():
            if ":" in pair:
                k, v = pair.split(":", 1)
                try:
                    result[k.strip()] = int(v.strip())
                except ValueError:
                    pass
        return result

    @staticmethod
    def _parse_source_string(s: str) -> dict:
        """解析 '微信扫描(2), 飞书扫描(1)' 格式"""
        result = {}
        for item in s.strip().split(","):
            item = item.strip()
            match = re.match(r"(.+?)\((\d+)\)", item)
            if match:
                result[match.group(1).strip()] = int(match.group(2))
        return result

    def get_company_page(self, company_name: str) -> Optional[str]:
        """读取公司的 wiki 页面内容

        Returns:
            Markdown 内容字符串，不存在返回 None
        """
        filepath = self._get_filepath(company_name)
        if not filepath.exists():
            return None
        try:
            return filepath.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"[Wiki] 读取失败 ({company_name}): {e}")
            return None

    def search_wiki(self, keyword: str) -> list[dict]:
        """搜索所有 wiki 页面中包含关键词的条目

        Returns:
            [{"company": "公司名", "file": "文件名", "matches": ["匹配行..."]}]
        """
        if not keyword:
            return []

        keyword_lower = keyword.lower()
        results = []

        try:
            for md_file in sorted(self.wiki_dir.glob("*.md")):
                try:
                    content = md_file.read_text(encoding="utf-8")
                except Exception:
                    continue

                if keyword_lower not in content.lower():
                    continue

                # 提取公司名（第一行 # 标题）
                company = md_file.stem
                first_line = content.split("\n", 1)[0]
                if first_line.startswith("# "):
                    company = first_line[2:].strip()

                # 收集匹配行（上下文）
                matches = []
                for line in content.split("\n"):
                    if keyword_lower in line.lower():
                        matches.append(line.strip())
                        if len(matches) >= 5:
                            break

                results.append({
                    "company": company,
                    "file": md_file.name,
                    "matches": matches,
                })

        except Exception as e:
            logger.error(f"[Wiki] 搜索失败: {e}")

        return results

    def list_companies(self) -> list[dict]:
        """列出所有有 wiki 页面的公司

        Returns:
            [{"company": "公司名", "file": "文件名", "updated": "最近更新时间",
              "industry": "赛道", "mentions": 提及次数}]
        """
        companies = []
        try:
            for md_file in sorted(self.wiki_dir.glob("*.md")):
                try:
                    content = md_file.read_text(encoding="utf-8")
                except Exception:
                    continue

                info = self._extract_meta(content, md_file)
                companies.append(info)

        except Exception as e:
            logger.error(f"[Wiki] 列表失败: {e}")

        # 按最近更新时间倒序
        companies.sort(key=lambda x: x.get("updated", ""), reverse=True)
        return companies

    def _extract_meta(self, content: str, md_file: Path) -> dict:
        """从 wiki 内容中提取元信息"""
        company = md_file.stem
        first_line = content.split("\n", 1)[0]
        if first_line.startswith("# "):
            company = first_line[2:].strip()

        updated = ""
        match = re.search(r"- 最近更新: (.+)", content)
        if match:
            updated = match.group(1).strip()

        industry = ""
        match = re.search(r"- 赛道: (.+)", content)
        if match:
            industry = match.group(1).strip()

        mentions = 0
        match = re.search(r"- 提及次数: (\d+)", content)
        if match:
            mentions = int(match.group(1))

        funding = ""
        match = re.search(r"- 融资阶段: (.+)", content)
        if match:
            funding = match.group(1).strip()

        return {
            "company": company,
            "file": md_file.name,
            "updated": updated,
            "industry": industry,
            "funding": funding,
            "mentions": mentions,
        }

    def get_context_for_memo(self, company_name: str) -> str:
        """获取公司累积知识，用于 memo 生成的上下文

        Returns:
            格式化的知识上下文字符串
        """
        content = self.get_company_page(company_name)
        if not content:
            return ""

        return (
            f"=== 知识库: {company_name} ===\n"
            f"以下是该公司的历史接触记录和累积知识：\n\n"
            f"{content}\n"
            f"=== 知识库结束 ===\n"
        )

    def get_stats(self) -> dict:
        """获取知识库整体统计"""
        companies = self.list_companies()
        total_mentions = sum(c.get("mentions", 0) for c in companies)
        latest_update = companies[0].get("updated", "") if companies else ""

        return {
            "total_companies": len(companies),
            "total_entries": total_mentions,
            "last_updated": latest_update,
        }


# ===== 全局单例 =====
_wiki_instance: Optional[WikiService] = None
_wiki_init_lock = threading.Lock()


def get_wiki_service() -> WikiService:
    """获取 WikiService 全局单例"""
    global _wiki_instance
    if _wiki_instance is None:
        with _wiki_init_lock:
            if _wiki_instance is None:
                _wiki_instance = WikiService()
    return _wiki_instance
