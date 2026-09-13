import json
import os
import subprocess
import threading
import uuid
from typing import Sequence, Tuple


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def hidden_creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def atomic_media_path(output_path: str) -> str:
    """生成与目标文件扩展名一致的临时路径，确保 FFmpeg 能判断封装格式。"""
    absolute = os.path.abspath(str(output_path or ""))
    stem, extension = os.path.splitext(absolute)
    token = f"{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
    return f"{stem}.partial_{token}{extension}"


def replace_command_output(
    command: Sequence[str],
    expected_output: str,
    temporary_output: str,
) -> list:
    updated = list(command)
    expected = os.path.normcase(os.path.abspath(expected_output))
    replaced = False
    for index in range(len(updated) - 1, -1, -1):
        value = str(updated[index])
        try:
            candidate = os.path.normcase(os.path.abspath(value))
        except (OSError, ValueError):
            continue
        if candidate == expected:
            updated[index] = temporary_output
            replaced = True
            break
    if not replaced:
        raise ValueError("FFmpeg 命令中未找到预期输出路径")
    return updated


def validate_media_output(
    ffprobe_path: str,
    path: str,
    require_video: bool = True,
    minimum_bytes: int = 1024,
    timeout: float = 12.0,
) -> Tuple[bool, str]:
    """校验中间媒体是否可解码，防止损坏 TS 被下一个转场节点读取。"""
    try:
        if not os.path.isfile(path):
            return False, "输出文件不存在"
        if os.path.getsize(path) <= max(0, int(minimum_bytes)):
            return False, "输出文件过小"
        command = [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            path,
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(2.0, float(timeout or 12.0)),
            startupinfo=hidden_startupinfo(),
            creationflags=hidden_creation_flags(),
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return False, detail[-500:] or f"ffprobe 返回码 {result.returncode}"
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") if isinstance(payload, dict) else []
        if require_video and not any(
            isinstance(item, dict) and item.get("codec_type") == "video"
            for item in streams or []
        ):
            return False, "未检测到视频流"
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
        if duration <= 0.01:
            return False, "媒体时长无效"
        return True, ""
    except (
        OSError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return False, str(exc)
