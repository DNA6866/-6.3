import os
import re
import subprocess
import threading

_SENSEVOICE_MODEL = None
_SENSEVOICE_MODEL_PATH = None
_SENSEVOICE_DEVICE = None
_SENSEVOICE_LOCK = threading.Lock()


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _is_ascii_path(path):
    try:
        str(path).encode("ascii")
        return True
    except Exception:
        return False


def _subst_ascii_dir(path):
    """SenseVoice 底层在 Windows 下可能读不到中文模型路径，临时映射成纯英文盘符。"""
    if os.name != "nt" or not path or _is_ascii_path(path):
        return path, ""
    abs_path = os.path.abspath(path)
    if not os.path.isdir(abs_path):
        return path, ""
    for letter in "ZYXWVUTSRQPONMLKJIHGFEDCBA":
        drive = f"{letter}:"
        if os.path.exists(drive + "\\"):
            continue
        result = subprocess.run(
            ["subst", drive, abs_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=hidden_startupinfo(),
        )
        if result.returncode == 0:
            return drive + "\\", drive
    return path, ""


def _release_subst_drive(drive):
    if os.name == "nt" and drive:
        subprocess.run(
            ["subst", drive, "/D"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=hidden_startupinfo(),
        )


def clean_asr_text(text):
    text = re.sub(r"<\|[^>]+?\|>", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sensevoice_model_ready(model_path):
    return bool(model_path) and os.path.isdir(model_path) and os.path.exists(os.path.join(model_path, "model.pt"))


def get_sensevoice_device():
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda:0"
    except Exception:
        pass
    return "cpu"


def sensevoice_runtime_info():
    device = _SENSEVOICE_DEVICE or get_sensevoice_device()
    loaded = _SENSEVOICE_MODEL is not None
    return device, loaded


def preload_sensevoice_model(model_path):
    if not sensevoice_model_ready(model_path):
        raise FileNotFoundError("SenseVoiceSmall 模型目录不完整")
    global _SENSEVOICE_MODEL, _SENSEVOICE_MODEL_PATH, _SENSEVOICE_DEVICE
    with _SENSEVOICE_LOCK:
        device = get_sensevoice_device()
        was_loaded = _SENSEVOICE_MODEL is not None and _SENSEVOICE_MODEL_PATH == os.path.abspath(model_path) and _SENSEVOICE_DEVICE == device
        if not was_loaded:
            from funasr import AutoModel

            safe_model_path, subst_drive = _subst_ascii_dir(model_path)
            try:
                _SENSEVOICE_MODEL = AutoModel(
                    model=safe_model_path,
                    trust_remote_code=False,
                    disable_update=True,
                    device=device,
                )
            finally:
                _release_subst_drive(subst_drive)
            _SENSEVOICE_MODEL_PATH = os.path.abspath(model_path)
            _SENSEVOICE_DEVICE = device
        return device, was_loaded


def transcribe_with_sensevoice(audio_path, model_path):
    if not os.path.exists(audio_path):
        raise FileNotFoundError("音频文件不存在")
    if not sensevoice_model_ready(model_path):
        raise FileNotFoundError("SenseVoiceSmall 模型目录不完整")

    preload_sensevoice_model(model_path)

    with _SENSEVOICE_LOCK:
        result = _SENSEVOICE_MODEL.generate(
            input=audio_path,
            language="zh",
            use_itn=True,
            batch_size_s=60,
        )

    if isinstance(result, list) and result:
        text = result[0].get("text", "")
    elif isinstance(result, dict):
        text = result.get("text", "")
    else:
        text = str(result)
    text = clean_asr_text(text)
    if not text:
        raise RuntimeError("SenseVoice 没有识别到有效文字")
    return text


def get_media_duration(ffprobe_path, file_path):
    try:
        res = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=hidden_startupinfo(),
        )
        return max(0.0, float((res.stdout or "0").strip()))
    except Exception:
        return 0.0


def _split_long_text(text, max_chars):
    parts = []
    text = text.strip()
    while len(text) > max_chars:
        cut = max_chars
        for mark in ["，", "、", "；", ",", " "]:
            pos = text.rfind(mark, 0, max_chars)
            if pos >= max(4, max_chars // 2):
                cut = pos + 1
                break
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def split_text_for_subtitle(text, max_chars=18):
    text = clean_asr_text(text)
    raw_parts = [x.strip() for x in re.split(r"(?<=[。！？!?])", text) if x.strip()]
    if not raw_parts:
        raw_parts = [text] if text else []

    parts = []
    for part in raw_parts:
        if len(part) <= max_chars:
            parts.append(part)
        else:
            parts.extend(_split_long_text(part, max_chars))
    return [x for x in parts if x]


def _srt_time(seconds):
    seconds = max(0.0, float(seconds))
    ms = int(round((seconds - int(seconds)) * 1000))
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt_from_text(text, duration, output_path, max_chars=18):
    parts = split_text_for_subtitle(text, max_chars=max_chars)
    if not parts:
        raise RuntimeError("没有可写入字幕的文本")

    duration = max(float(duration or 0), len(parts) * 0.8)
    weights = [max(1, len(re.sub(r"\s+", "", part))) for part in parts]
    total_weight = sum(weights)
    cursor = 0.0
    lines = []
    for idx, (part, weight) in enumerate(zip(parts, weights), 1):
        span = duration * weight / total_weight
        span = max(0.8, span)
        start = cursor
        end = min(duration, cursor + span)
        if idx == len(parts):
            end = duration
        lines.extend([
            str(idx),
            f"{_srt_time(start)} --> {_srt_time(end)}",
            part,
            "",
        ])
        cursor = end

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return output_path


def transcribe_to_txt_with_sensevoice(audio_path, model_path, output_path):
    text = transcribe_with_sensevoice(audio_path, model_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)
    return output_path
