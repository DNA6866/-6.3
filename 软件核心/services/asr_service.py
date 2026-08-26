import json
import os
import re
import subprocess
import uuid

_SENSEVOICE_MODEL = None
_SENSEVOICE_MODEL_PATH = None
_SENSEVOICE_DEVICE = None
_COMMON_TRAD_TO_SIMP = str.maketrans(
    "說這們麼會個裡後為來對過去發機與樣學當覺點實種聲網著經視聽畫貝車買賣嗎無體國問動萬東風"
    "臺台廣開關選擇標題視頻頻頁數據錄製錄制下載下載雲雜訊聲音參考輸出輸入啟動檢測異常處理測試",
    "说这们么会个里后为来对过去发机与样学当觉点实种声网着经视听画贝车买卖吗无体国问动万东风"
    "台台广开关选择标题视频频页数据录制录制下载下载云杂讯声音参考输出输入启动检测异常处理测试",
)
_STRONG_SENTENCE_MARKS = set("。！？!?；;")
_SOFT_SENTENCE_MARKS = set("，,、：:")


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def normalize_to_simplified_zh(text):
    return (text or "").translate(_COMMON_TRAD_TO_SIMP)


def clean_asr_text(text):
    text = normalize_to_simplified_zh(text)
    text = re.sub(r"<\|[^>]+?\|>", "", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sensevoice_model_ready(model_path):
    return bool(model_path) and os.path.isdir(model_path) and os.path.exists(os.path.join(model_path, "model.pt"))


def sensevoice_runtime_info():
    device = _SENSEVOICE_DEVICE or "独立AI环境（GPU）"
    loaded = bool(_SENSEVOICE_MODEL)
    return device, loaded


def _run_external_asr_task(task, event_callback=None, process_holder=None):
    from modules.voice_clone.runtime_support import run_voice_task

    result = run_voice_task(
        task,
        event_callback=event_callback,
        process_holder=process_holder,
    )
    global _SENSEVOICE_MODEL, _SENSEVOICE_MODEL_PATH, _SENSEVOICE_DEVICE
    _SENSEVOICE_MODEL = True
    _SENSEVOICE_MODEL_PATH = os.path.abspath(task.get("asr_model_path") or "")
    _SENSEVOICE_DEVICE = result.get("device") or _SENSEVOICE_DEVICE
    return result


def preload_sensevoice_model(model_path, event_callback=None):
    if not sensevoice_model_ready(model_path):
        raise FileNotFoundError("SenseVoiceSmall 模型目录不完整")
    was_loaded = bool(
        _SENSEVOICE_MODEL
        and _SENSEVOICE_MODEL_PATH == os.path.abspath(model_path)
    )
    result = _run_external_asr_task(
        {
            "operation": "asr_preload",
            "kind": "asr_preload",
            "asr_model_path": model_path,
            "allow_cpu_fallback": False,
        },
        event_callback=event_callback,
    )
    return result.get("device") or "独立AI环境", was_loaded


def transcribe_with_sensevoice(audio_path, model_path, event_callback=None, context="音频"):
    if not os.path.exists(audio_path):
        raise FileNotFoundError("音频文件不存在")
    if not sensevoice_model_ready(model_path):
        raise FileNotFoundError("SenseVoiceSmall 模型目录不完整")
    result = _run_external_asr_task(
        {
            "operation": "transcribe",
            "kind": "transcribe",
            "audio_path": audio_path,
            "asr_model_path": model_path,
            "context": context,
            "allow_cpu_fallback": False,
        },
        event_callback=event_callback,
    )
    text = clean_asr_text(result.get("text") or "")
    if not text:
        raise RuntimeError("SenseVoice 没有识别到有效文字")
    return text


class SenseVoiceIsolatedSession:
    """Compatibility wrapper backed by the independent AI Python runtime."""

    def __init__(self, model_path, timeout_sec=300):
        if not sensevoice_model_ready(model_path):
            raise FileNotFoundError("SenseVoiceSmall 模型目录不完整")
        self.model_path = model_path
        self.timeout_sec = timeout_sec
        self.device = ""
        self._closed = False
        self.device, _loaded = preload_sensevoice_model(model_path)

    def is_alive(self):
        return not self._closed

    def transcribe(self, audio_path, timeout_sec=None):
        if not os.path.exists(audio_path):
            raise FileNotFoundError("音频文件不存在")
        if self._closed:
            raise RuntimeError("SenseVoice 独立运行服务已关闭")
        text = transcribe_with_sensevoice(
            audio_path,
            self.model_path,
            context="音视频",
        )
        self.device = _SENSEVOICE_DEVICE or self.device
        return text, self.device

    def close(self, force=False):
        self._closed = True


def transcribe_with_sensevoice_isolated(
    audio_path,
    model_path,
    timeout_sec=300,
    event_callback=None,
    context="音频",
):
    """Run SenseVoice outside the GUI process to avoid native DLL teardown crashes."""
    text = transcribe_with_sensevoice(
        audio_path,
        model_path,
        event_callback=event_callback,
        context=context,
    )
    return text, _SENSEVOICE_DEVICE or "独立AI环境"


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
            timeout=30,
        )
        return max(0.0, float((res.stdout or "0").strip()))
    except Exception:
        return 0.0


def split_text_for_subtitle(text, max_chars=None):
    """按完整语句切分字幕；max_chars 仅为兼容旧配置，不再限制句长。"""
    text = clean_asr_text(text)
    raw_parts = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?；;])", text)
        if item.strip()
    ]
    if not raw_parts:
        raw_parts = [text] if text else []

    return [part for part in raw_parts if part]


def _subtitle_display_units(text):
    units = 0.0
    for char in str(text or ""):
        if char.isspace():
            units += 0.35
        elif char.isascii() and (char.isalnum() or char in "_-/"):
            units += 0.58
        elif char in "。，、！？!?；;：:,.()（）[]【】“”\"'":
            units += 0.52
        else:
            units += 1.0
    return units


def _balanced_subtitle_cut(text):
    total_units = _subtitle_display_units(text)
    target = total_units / 2.0
    best_cut = max(1, len(text) // 2)
    best_score = float("inf")
    left_units = 0.0
    for cut, char in enumerate(text[:-1], 1):
        left_units += _subtitle_display_units(char)
        right_units = total_units - left_units
        if min(left_units, right_units) < total_units * 0.22:
            continue
        score = abs(left_units - target)
        if char in _SOFT_SENTENCE_MARKS:
            score -= 2.8
        elif char in _STRONG_SENTENCE_MARKS or char.isspace():
            score -= 4.0
        if score < best_score:
            best_cut = cut
            best_score = score
    return best_cut


def layout_subtitle_text(
    text,
    font_size=85,
    play_res_x=1080,
    margin_side=80,
    min_font_size=30,
):
    """保持一句字幕为一个事件，宽度不足时平衡成两行并避免横向溢出。"""
    text = clean_asr_text(text)
    if not text:
        return "", max(1, int(font_size or 85))

    font_size = max(1, int(font_size or 85))
    available_width = max(120.0, float(play_res_x) - 2.0 * float(margin_side))
    glyph_width_factor = 0.96
    one_line_units = available_width / (font_size * glyph_width_factor)
    if _subtitle_display_units(text) <= one_line_units:
        return text, font_size

    cut = _balanced_subtitle_cut(text)
    left = text[:cut].strip()
    right = text[cut:].strip()
    if not left or not right:
        return text, font_size

    widest_units = max(_subtitle_display_units(left), _subtitle_display_units(right))
    fitted_size = int(available_width / max(1.0, widest_units * glyph_width_factor))
    fitted_size = max(min(int(min_font_size or 30), font_size), min(font_size, fitted_size))
    return rf"{left}\N{right}", fitted_size


def _srt_time(seconds):
    seconds = max(0.0, float(seconds))
    ms = int(round((seconds - int(seconds)) * 1000))
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt_from_text(text, duration, output_path, max_chars=None):
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


def _token_offset(token, key):
    offsets = token.get("offsets") if isinstance(token, dict) else {}
    try:
        return max(0.0, float((offsets or {}).get(key, 0)) / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def _segment_character_timings(segment):
    text = clean_asr_text((segment or {}).get("text") or "")
    characters = [char for char in text if not char.isspace()]
    if not characters:
        return []
    raw_tokens = (segment or {}).get("tokens") or []
    tokens = []
    for token in raw_tokens:
        token_text = str((token or {}).get("text") or "")
        if token_text.startswith("[_") or token_text.endswith("_]"):
            continue
        start = _token_offset(token, "from")
        end = _token_offset(token, "to")
        if end < start:
            end = start
        tokens.append((start, end, float((token or {}).get("p") or 0.0)))

    segment_offsets = (segment or {}).get("offsets") or {}
    try:
        segment_start = max(0.0, float(segment_offsets.get("from", 0)) / 1000.0)
        segment_end = max(segment_start, float(segment_offsets.get("to", 0)) / 1000.0)
    except (TypeError, ValueError):
        segment_start, segment_end = 0.0, 0.0
    if not tokens:
        span = max(0.2, segment_end - segment_start)
        tokens = [
            (
                segment_start + span * index / len(characters),
                segment_start + span * (index + 1) / len(characters),
                0.0,
            )
            for index in range(len(characters))
        ]

    timings = []
    token_count = len(tokens)
    char_count = len(characters)
    for index, char in enumerate(characters):
        first_index = min(token_count - 1, int(index * token_count / char_count))
        last_index = min(
            token_count - 1,
            max(first_index, int(((index + 1) * token_count - 1) / char_count)),
        )
        start = tokens[first_index][0]
        end = max(start + 0.02, tokens[last_index][1])
        confidence_values = [tokens[pos][2] for pos in range(first_index, last_index + 1)]
        confidence = sum(confidence_values) / max(1, len(confidence_values))
        timings.append({
            "text": char,
            "start": start,
            "end": end,
            "confidence": confidence,
        })
    return timings


def parse_whisper_word_segments(payload, max_chars=None, pause_threshold=0.42):
    """按句末标点或真实停顿整理词级时间戳，不再按固定字数截断。"""
    character_timings = []
    for segment in (payload or {}).get("transcription") or []:
        character_timings.extend(_segment_character_timings(segment))
    if not character_timings:
        return []

    result = []
    previous_end = 0.0
    current = []
    for index, item in enumerate(character_timings):
        current.append(item)
        next_item = character_timings[index + 1] if index + 1 < len(character_timings) else None
        pause = (
            max(0.0, next_item["start"] - item["end"])
            if next_item is not None else 0.0
        )
        sentence_end = item["text"] in _STRONG_SENTENCE_MARKS
        if next_item is not None and not sentence_end and pause < pause_threshold:
            continue

        text = "".join(char["text"] for char in current).strip()
        if text:
            start = max(previous_end, current[0]["start"])
            end = max(start + 0.2, current[-1]["end"])
            result.append({"start": start, "end": end, "text": text})
            previous_end = end
        current = []
    return result


def write_srt_from_segments(segments, output_path):
    lines = []
    for index, segment in enumerate(segments or [], 1):
        text = clean_asr_text((segment or {}).get("text") or "")
        if not text:
            continue
        start = max(0.0, float((segment or {}).get("start") or 0.0))
        end = max(start + 0.2, float((segment or {}).get("end") or start + 0.2))
        lines.extend([
            str(len(lines) // 4 + 1),
            f"{_srt_time(start)} --> {_srt_time(end)}",
            text,
            "",
        ])
    if not lines:
        raise RuntimeError("词级时间戳没有生成有效字幕")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return output_path


def transcribe_with_whisper_word_timestamps(
    whisper_exe,
    model_path,
    ffmpeg_path,
    audio_path,
    output_path,
    max_chars=None,
    timeout_sec=900,
):
    """使用已有 whisper.cpp 生成词级时间轴；全程使用 ASCII 临时文件名。"""
    for path, label in (
        (whisper_exe, "Whisper 程序"),
        (model_path, "Whisper 模型"),
        (ffmpeg_path, "FFmpeg 程序"),
        (audio_path, "待识别音频"),
    ):
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(f"{label}不存在：{path}")

    work_dir = os.path.dirname(os.path.abspath(whisper_exe))
    token = f"word_ts_{uuid.uuid4().hex[:12]}"
    wav_name = token + ".wav"
    wav_path = os.path.join(work_dir, wav_name)
    json_path = os.path.join(work_dir, token + ".json")
    model_arg = os.path.relpath(os.path.abspath(model_path), work_dir)
    try:
        convert = subprocess.run(
            [
                ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
                "-i", audio_path, "-ar", "16000", "-ac", "1",
                "-c:a", "pcm_s16le", wav_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=hidden_startupinfo(),
            timeout=120,
        )
        if convert.returncode != 0 or not os.path.isfile(wav_path):
            detail = (convert.stderr or convert.stdout or "音频转换失败").strip()
            raise RuntimeError(detail[-1200:])

        command = [
            whisper_exe,
            "-m", model_arg,
            "-f", wav_name,
            "-ojf",
            "-of", token,
            "-l", "zh",
            "-sow",
            "-np",
        ]
        result = subprocess.run(
            command,
            cwd=work_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=hidden_startupinfo(),
            timeout=max(60, int(timeout_sec or 900)),
        )
        if result.returncode != 0 or not os.path.isfile(json_path):
            detail = (result.stdout or "Whisper 没有输出词级时间戳").strip()
            raise RuntimeError(detail[-1600:])
        with open(json_path, "r", encoding="utf-8", errors="replace") as handle:
            payload = json.load(handle)
        segments = parse_whisper_word_segments(payload, max_chars=max_chars)
        write_srt_from_segments(segments, output_path)
        return output_path, segments
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Whisper 词级识别超时（{exc.timeout}秒）") from exc
    finally:
        for path in (wav_path, json_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


def transcribe_to_txt_with_sensevoice(
    audio_path,
    model_path,
    output_path,
    isolated=True,
    event_callback=None,
    context="音频",
):
    if isolated:
        text, _device = transcribe_with_sensevoice_isolated(
            audio_path,
            model_path,
            event_callback=event_callback,
            context=context,
        )
    else:
        text = transcribe_with_sensevoice(
            audio_path,
            model_path,
            event_callback=event_callback,
            context=context,
        )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)
    return output_path
