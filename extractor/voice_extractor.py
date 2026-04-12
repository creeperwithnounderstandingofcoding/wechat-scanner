"""微信语音消息提取与转写

WeChat 4.x 的语音消息（local_type=34）本地存储路径：
    ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<wxid>/msg/voice/

语音文件通常为 .aud 或 .silk 格式（SILK 编码）。

本模块负责：
1. 从 type=34 消息 XML 中解析语音元数据（voiceid、文件路径等）
2. 在本地文件目录中定位语音文件
3. 将 SILK 格式转换为 WAV
4. 使用 Whisper 模型进行语音转文字（可选依赖）
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BJT = timezone(timedelta(hours=8))

# 语音文件扩展名
VOICE_EXTENSIONS = {".aud", ".silk", ".slk", ".amr", ".mp3", ".wav"}


def parse_voice_metadata(xml_content: str) -> Optional[dict]:
    """从 type=34 消息 XML 中解析语音元数据。

    WeChat 语音消息的 raw_content 可能包含：
        <msg><voicemsg ... voicelength="..." ... />
    或
        <voiceid>...</voiceid>

    返回 dict 含 voiceid、voicelength 等，找不到返回 None。
    """
    if not xml_content:
        return None

    result = {}

    # voicelength（毫秒）
    length_match = re.search(r'voicelength="(\d+)"', xml_content)
    if length_match:
        result["voicelength"] = int(length_match.group(1))

    # clientmsgid / voiceid
    msgid_match = re.search(r'clientmsgid="([^"]+)"', xml_content)
    if msgid_match:
        result["clientmsgid"] = msgid_match.group(1)

    voiceid_match = re.search(r'<voiceid>([^<]+)</voiceid>', xml_content)
    if voiceid_match:
        result["voiceid"] = voiceid_match.group(1)

    # bufid
    bufid_match = re.search(r'bufid="(\d+)"', xml_content)
    if bufid_match:
        result["bufid"] = bufid_match.group(1)

    # fromusername (sender)
    from_match = re.search(r'fromusername="([^"]+)"', xml_content)
    if from_match:
        result["fromusername"] = from_match.group(1)

    return result if result else None


def find_voice_file(
    msg_raw_content: str,
    wechat_data_root: Path,
    create_time: int = 0,
) -> Optional[Path]:
    """在微信本地文件目录中查找语音文件。

    搜索策略：
    1. 解析 XML 中的 clientmsgid / voiceid / bufid 用于匹配文件名
    2. 根据消息时间定位 YYYY-MM 子目录
    3. 在 msg/voice/ 下搜索

    wechat_data_root: xwechat_files 根目录
    create_time: 消息时间戳（秒或毫秒）
    """
    if not wechat_data_root.exists():
        return None

    metadata = parse_voice_metadata(msg_raw_content)

    # 收集可用于匹配文件名的标识符
    identifiers = []
    if metadata:
        for key in ("clientmsgid", "voiceid", "bufid"):
            val = metadata.get(key, "")
            if val:
                identifiers.append(str(val).lower())

    # 解析时间 -> 候选月份
    candidate_months: list[str] = []
    if create_time:
        try:
            ts = create_time
            if ts > 1_000_000_000_000:
                ts //= 1000
            dt = datetime.fromtimestamp(ts, tz=BJT)
            candidate_months.append(dt.strftime("%Y-%m"))
            prev = dt.replace(day=1) - timedelta(days=1)
            candidate_months.append(prev.strftime("%Y-%m"))
        except (ValueError, OSError):
            pass

    # 语音可能存储的子目录
    voice_subdirs = ["msg/voice", "msg/audio", "msg/media"]

    for user_dir in wechat_data_root.iterdir():
        if not user_dir.is_dir():
            continue

        for subdir_name in voice_subdirs:
            voice_dir = user_dir / subdir_name
            if not voice_dir.exists():
                continue

            # 先查候选月份
            for month in candidate_months:
                month_dir = voice_dir / month
                if month_dir.exists():
                    hit = _find_voice_in_dir(month_dir, identifiers)
                    if hit:
                        return hit

            # 兜底：全月遍历（最近的先搜）
            for month_dir in sorted(voice_dir.iterdir(), reverse=True):
                if not month_dir.is_dir():
                    continue
                hit = _find_voice_in_dir(month_dir, identifiers)
                if hit:
                    return hit

            # 也检查 voice_dir 根目录
            hit = _find_voice_in_dir(voice_dir, identifiers)
            if hit:
                return hit

    return None


def _find_voice_in_dir(
    directory: Path,
    identifiers: list[str],
) -> Optional[Path]:
    """在目录中查找语音文件。

    匹配策略：文件名中包含任一 identifier。
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
        if child.suffix.lower() not in VOICE_EXTENSIONS:
            continue

        stem_lower = child.stem.lower()
        for ident in identifiers:
            if ident in stem_lower:
                return child

    return None


def _silk_to_wav(silk_path: Path) -> Optional[Path]:
    """将 SILK 格式音频转换为 WAV。

    尝试以下方式（按优先级）：
    1. pilk 库 (pip install pilk)
    2. silk-v3-decoder 命令行工具
    3. ffmpeg（某些版本支持 silk）

    返回 WAV 文件路径（临时文件），失败返回 None。
    """
    wav_path = Path(tempfile.mktemp(suffix=".wav"))

    # 方式 1: pilk
    try:
        import pilk
        pcm_path = Path(tempfile.mktemp(suffix=".pcm"))
        try:
            pilk.decode(str(silk_path), str(pcm_path))
            # PCM -> WAV: pilk 解码为 24000Hz 16bit mono PCM
            import wave
            import struct
            pcm_data = pcm_path.read_bytes()
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(24000)
                wf.writeframes(pcm_data)
            logger.debug(f"[语音转换] pilk 转换成功: {silk_path} -> {wav_path}")
            return wav_path
        except Exception as e:
            logger.debug(f"[语音转换] pilk 解码失败: {e}")
        finally:
            if pcm_path.exists():
                pcm_path.unlink(missing_ok=True)
    except ImportError:
        logger.debug("[语音转换] pilk 未安装")

    # 方式 2: silk-v3-decoder CLI
    try:
        result = subprocess.run(
            ["silk_v3_decoder", str(silk_path), str(wav_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and wav_path.exists():
            logger.debug(f"[语音转换] silk_v3_decoder 转换成功")
            return wav_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 方式 3: ffmpeg
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(silk_path), "-ar", "16000", "-ac", "1", str(wav_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and wav_path.exists():
            logger.debug(f"[语音转换] ffmpeg 转换成功")
            return wav_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 清理
    if wav_path.exists():
        wav_path.unlink(missing_ok=True)

    logger.warning(f"[语音转换] 所有转换方式均失败: {silk_path}")
    return None


def transcribe_voice(audio_path: Path) -> str:
    """对语音文件进行转写。

    如果文件是 SILK 格式（.aud/.silk/.slk），先转为 WAV。
    然后使用 Whisper 模型转写。

    Whisper 是可选依赖 — 未安装时返回空字符串并打 warning。

    返回转写文本，失败返回空字符串。
    """
    if not audio_path.exists():
        return ""

    wav_path: Optional[Path] = None
    need_cleanup = False

    # 如果是 SILK 格式，先转换
    if audio_path.suffix.lower() in (".aud", ".silk", ".slk"):
        wav_path = _silk_to_wav(audio_path)
        if not wav_path:
            logger.warning(f"[语音转写] SILK 转换失败，无法转写: {audio_path}")
            return ""
        need_cleanup = True
    elif audio_path.suffix.lower() == ".wav":
        wav_path = audio_path
    elif audio_path.suffix.lower() in (".mp3", ".amr"):
        # ffmpeg 转换为 WAV
        wav_path = Path(tempfile.mktemp(suffix=".wav"))
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(audio_path), "-ar", "16000", "-ac", "1", str(wav_path)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0 or not wav_path.exists():
                logger.warning(f"[语音转写] ffmpeg 转换失败: {audio_path}")
                return ""
            need_cleanup = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning(f"[语音转写] ffmpeg 不可用或超时: {audio_path}")
            return ""
    else:
        logger.warning(f"[语音转写] 不支持的音频格式: {audio_path.suffix}")
        return ""

    try:
        return _whisper_transcribe(wav_path)
    finally:
        if need_cleanup and wav_path and wav_path.exists():
            try:
                wav_path.unlink()
            except OSError:
                pass


def _whisper_transcribe(wav_path: Path) -> str:
    """使用 OpenAI Whisper 模型转写 WAV 文件。

    需要安装 openai-whisper: pip install openai-whisper
    首次运行会下载模型文件。
    """
    try:
        import whisper
    except ImportError:
        logger.warning(
            "[语音转写] whisper 未安装，跳过语音转写。"
            "安装方式: pip install openai-whisper"
        )
        return ""

    try:
        # 使用 base 模型（平衡速度和质量）
        model = whisper.load_model("base")
        result = model.transcribe(
            str(wav_path),
            language="zh",  # 默认中文
            fp16=False,  # CPU 兼容
        )
        text = result.get("text", "").strip()
        if text:
            logger.info(f"[语音转写] Whisper 转写成功: {len(text)} 字符")
        return text
    except Exception as e:
        logger.error(f"[语音转写] Whisper 转写失败: {e}")
        return ""
