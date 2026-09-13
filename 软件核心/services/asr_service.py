import json
import math
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


def _split_balanced_lines(text, n_lines, cap=None):
    """按显示单元均衡把文本切成 n 行，优先落在自然断点（标点/空格）。

    cap：单行显示单元容量上限（可用宽/字宽）。切分目标不会超过该容量，
    保证每一行都放得下（n 行不够时由调用方提高 n_lines）。
    """
    n_lines = max(1, int(n_lines))
    total_units = _subtitle_display_units(text)
    if n_lines <= 1 or total_units <= 0:
        return [text]
    lines = []
    pos = 0
    for line_idx in range(n_lines - 1):
        remaining_chars = len(text) - pos
        remaining_lines = n_lines - line_idx
        remaining_units = _subtitle_display_units(text[pos:])
        if remaining_units <= 0 or remaining_chars < 2:
            break
        target_here = remaining_units / remaining_lines
        if cap:
            target_here = min(target_here, float(cap))
        # 候选切点：局部累计首次达到 target 的位置及其后 4 字符窗口
        candidates = []
        local_acc = 0.0
        reached = False
        for i in range(pos + 1, len(text) + 1):
            if i > pos:
                local_acc = _subtitle_display_units(text[pos:i])
            if local_acc >= target_here:
                reached = True
            if reached:
                candidates.append(i)
                if len(candidates) >= 5:
                    break
        if not candidates:
            candidates = [min(len(text) - 1, pos + max(1, remaining_chars // remaining_lines))]
        best_i, best_score = None, float("inf")
        for j in candidates:
            if j <= pos or j > len(text):
                continue
            left_units = _subtitle_display_units(text[pos:j])
            score = abs(left_units - target_here)
            prev = text[j - 1] if j - 1 < len(text) else ""
            if prev in _SOFT_SENTENCE_MARKS:
                score -= 1.5
            elif prev in _STRONG_SENTENCE_MARKS or prev.isspace():
                score -= 2.0
            if score < best_score:
                best_score, best_i = score, j
        if not best_i or best_i <= pos or best_i >= len(text):
            best_i = min(len(text) - 1, pos + max(1, remaining_chars // remaining_lines))
        lines.append(text[pos:best_i].strip())
        pos = best_i
    lines.append(text[pos:].strip())
    return [line for line in lines if line]


def layout_subtitle_text(
    text,
    font_size=85,
    play_res_x=1080,
    margin_side=80,
    min_font_size=30,
    max_lines=2,
    allow_shrink=True,
):
    """保持一句字幕为一个事件，宽度不足时平衡换行。

    - allow_shrink=True（旧行为，批量混剪在用）：两行放不下则缩小字号（下限 min_font_size）；
    - allow_shrink=False（视频加字幕，客户要求字号恒定）：字号不动，
      行数自适应（1~max_lines 行），每行按显示单元均衡切分并优先自然断点。
    """
    text = clean_asr_text(text)
    if not text:
        return "", max(1, int(font_size or 85))

    font_size = max(1, int(font_size or 85))
    available_width = max(120.0, float(play_res_x) - 2.0 * float(margin_side))
    glyph_width_factor = 0.96
    one_line_units = available_width / (font_size * glyph_width_factor)
    units = _subtitle_display_units(text)
    if units <= one_line_units:
        return text, font_size

    if allow_shrink:
        cut = _balanced_subtitle_cut(text)
        left = text[:cut].strip()
        right = text[cut:].strip()
        if not left or not right:
            return text, font_size
        widest_units = max(_subtitle_display_units(left), _subtitle_display_units(right))
        fitted_size = int(available_width / max(1.0, widest_units * glyph_width_factor))
        fitted_size = max(min(int(min_font_size or 30), font_size), min(font_size, fitted_size))
        return rf"{left}\N{right}", fitted_size

    # 字号恒定模式：行数自适应，每行不超过可用宽度
    n_lines = max(2, min(int(max_lines or 2), math.ceil(units / max(1.0, one_line_units))))
    parts = _split_balanced_lines(text, n_lines, cap=one_line_units)
    if len(parts) <= 1:
        return text, font_size
    return r"\N".join(parts), font_size


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


def write_srt_aligned_from_text(text, duration, silence_points, output_path, max_chars=None):
    """将整段识别文本按语音停顿点对齐时间轴后写入 SRT。

    相比 write_srt_from_text 的按时长均分，此函数会把句子边界
    吸附到最近的语音停顿点（±15% 容差），显著减少字幕与语音的漂移。
    仍属估算时间轴，精准对齐请使用 Whisper 词级时间轴。
    max_chars：单条字幕字数上限（按画面容量动态传入）。
    """
    text = clean_asr_text(text)
    parts = split_text_for_subtitle(text, max_chars=max_chars)
    if not parts:
        raise RuntimeError("没有可写入字幕的文本")

    duration = max(float(duration or 0), len(parts) * 0.8)
    points = sorted(
        max(0.0, min(duration, float(p)))
        for p in (silence_points or [])
        if isinstance(p, (int, float))
    )

    # 理想均分切点 → 吸附最近停顿点（±15% 容差）
    cuts = []
    tolerance = duration * 0.15
    for i in range(1, len(parts)):
        ideal = duration * i / len(parts)
        chosen = ideal
        if points:
            nearest = min(points, key=lambda p: abs(p - ideal))
            if abs(nearest - ideal) <= tolerance:
                chosen = nearest
        cuts.append(chosen)

    # 去除过近/乱序切点
    filtered = []
    for c in sorted(cuts):
        if filtered and c - filtered[-1] < 0.25:
            continue
        filtered.append(c)
    cuts = filtered

    events = []
    cursor = 0.0
    for idx, part in enumerate(parts):
        end = cuts[idx] if idx < len(cuts) else duration
        end = max(cursor + 0.2, min(duration, end))
        if idx == len(parts) - 1:
            end = duration
        if end <= cursor:
            end = min(duration, cursor + 0.2)
        events.append((cursor, end, part))
        cursor = end

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    lines = []
    for idx, (start, end, part) in enumerate(events, 1):
        lines.extend([str(idx), f"{_srt_time(start)} --> {_srt_time(end)}", part, ""])
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


def detect_silence_intervals(ffmpeg_path, audio_path, noise="-32dB", min_dur=0.25, timeout=45):
    """用 ffmpeg silencedetect 检测静音区间，返回 [(start, end), ...]。

    供 Whisper 字幕时间轴的静音感知内插使用（视频加字幕与批量混剪共享）。
    """
    try:
        res = subprocess.run(
            [ffmpeg_path, '-hide_banner', '-nostdin', '-loglevel', 'info',
             '-i', audio_path, '-vn',
             '-af', f'silencedetect=noise={noise}:d={min_dur}',
             '-f', 'null', '-'],
            capture_output=True, text=True, encoding='utf-8', errors='ignore',
            timeout=max(8, int(timeout)),
        )
        text = (res.stderr or "") + "\n" + (res.stdout or "")
        starts = [float(m.group(1)) for m in re.finditer(r"silence_start:\s*([0-9.]+)", text)]
        ends = [float(m.group(1)) for m in re.finditer(r"silence_end:\s*([0-9.]+)", text)]
        intervals = []
        for i, s in enumerate(starts):
            e = ends[i] if i < len(ends) else s + 0.5
            intervals.append((max(0.0, s), max(0.0, e)))
        return intervals
    except Exception:
        return []


def parse_whisper_word_segments(payload, max_chars=None, pause_threshold=0.3, silence_intervals=None, max_duration=5.0):
    """按 segment 边界、句末标点、逗号或字数上限整理 whisper 时间戳。

    分段策略（针对中文口播字幕可读性）：
      - whisper segment 边界：最高优先级，可靠的语音段落边界，强制切段；
      - 句末强标点（。！？!?；;）立即切段；
      - 逗号处且当前段已接近字数上限 -> 切段；
      - 达到 max_chars 硬上限 -> 强制切段；
      - 单条预估时长超过 max_duration 秒 -> 强制细分（安全阈值：
        连续语速快、全程无静音无标点的长句，避免一条字幕挂 7-8 秒）。

    时间轴（静音感知内插）：
      每条字幕的时间在其所属 segment 区间内按字符比例分配，但只分配到
      「语音活动区间」上——silencedetect 检出的静音段不占字幕时间。
      不使用词级时间戳做条目起止：whisper.cpp 中文词级时间戳存在伪停顿，
      会把"散步"这类词撕开并产生整体跳变；segment 级 offsets 可靠得多。
    """
    max_chars = int(max_chars or 20)  # 调用方可能显式传 None（旧签名兼容）
    silences = [
        (float(s), float(e))
        for (s, e) in (silence_intervals or [])
        if e > s
    ]

    def _speech_spans(a, b):
        """[a, b] 内扣除静音后的语音活动区间列表（过滤 <0.35s 的边缘残渣）。"""
        spans = []
        cur = a
        for (s, e) in sorted(silences):
            s, e = max(a, s), min(b, e)
            if s >= e:
                continue
            if s > cur:
                spans.append((cur, s))
            cur = max(cur, e)
        if cur < b:
            spans.append((cur, b))
        # 残渣过滤：segment 边缘可能留下极短的"语音"碎片（TTS 尾音/检测毛刺），
        # 会把字幕起点钳在静音中间；过短的一律并入静音。
        return [(x, y) for x, y in spans if y - x >= 0.35]

    def _end_from(start_abs, duration):
        """（已弃用：end 改为连续内插）保留占位以防外部引用，直接返回内插值。"""
        return start_abs + duration

    groups = []
    for segment in (payload or {}).get("transcription") or []:
        chars = _segment_character_timings(segment)
        if not chars:
            continue
        offsets = (segment or {}).get("offsets") or {}
        try:
            seg_start = max(0.0, float(offsets.get("from", 0)) / 1000.0)
            seg_end = max(seg_start, float(offsets.get("to", 0)) / 1000.0)
        except (TypeError, ValueError):
            seg_start = float(chars[0].get("start") or 0.0)
            seg_end = max(seg_start, float(chars[-1].get("end") or seg_start))
        if seg_end <= seg_start:
            seg_end = seg_start + 0.2 * max(1, len(chars))
        groups.append({"chars": chars, "start": seg_start, "end": seg_end})
    if not groups:
        return []

    soft_marks = "，,、：:"
    result = []
    previous_end = 0.0

    for group in groups:
        chars = group["chars"]
        seg_start, seg_end = group["start"], group["end"]
        unit_list = [max(1, _subtitle_display_units(c.get("text") or "")) for c in chars]
        total_units = sum(unit_list)

        # ── 组内切分：segment/标点/字数（不信词级伪停顿）──
        cuts = []
        a = 0
        units = 0
        for i, item in enumerate(chars):
            char = item.get("text") or ""
            units += unit_list[i]
            is_last = i == len(chars) - 1
            sentence_end = char in _STRONG_SENTENCE_MARKS
            soft_end = char in soft_marks
            if (
                is_last
                or sentence_end
                or (soft_end and units >= max_chars * 0.6)
                or units >= max_chars
            ):
                cuts.append((a, i + 1))
                a = i + 1
                units = 0
        if a < len(chars):
            cuts.append((a, len(chars)))

        # ── 时间：静音感知内插 ──
        spans = _speech_spans(seg_start, seg_end)
        if not spans:
            spans = [(seg_start, seg_end)]
        voice_total = sum(b - x for x, b in spans)

        def _to_abs(voice_pos):
            """语音时间轴位置 -> 绝对时间（跳过静音区间）。"""
            acc = 0.0
            for (x, b) in spans:
                length = b - x
                if voice_pos <= acc + length:
                    return x + (voice_pos - acc)
                acc += length
            return spans[-1][1]

        voice_cursor = 0.0
        for (s, e) in cuts:
            text = "".join(str(c.get("text") or "") for c in chars[s:e]).strip()
            if not text:
                continue
            seg_units = sum(unit_list[s:e]) or 1
            v0 = voice_cursor
            v1 = voice_cursor + voice_total * (seg_units / total_units)
            # 安全阈值：无静音无标点的连续长句，预估时长超限时强制细分，
            # 让字幕切换快一点，避免一条两行长字幕在屏幕上挂 7-8 秒。
            n_splits = 1
            if max_duration and max_duration > 0:
                n_splits = max(1, math.ceil((v1 - v0) / float(max_duration)))
            if n_splits > 1 and (e - s) >= n_splits:
                step_chars = math.ceil((e - s) / n_splits)
                sub_bounds = []
                k = s
                while k < e:
                    sub_bounds.append((k, min(k + step_chars, e)))
                    k += step_chars
                step_v = (v1 - v0) / len(sub_bounds)
                for i, (ss, se) in enumerate(sub_bounds):
                    sub_text = "".join(
                        str(c.get("text") or "") for c in chars[ss:se]
                    ).strip()
                    if not sub_text:
                        continue
                    vs = v0 + i * step_v
                    start = max(previous_end, _to_abs(vs))
                    end = max(start + 0.2, _to_abs(vs + step_v))
                    result.append({"start": start, "end": end, "text": sub_text})
                    previous_end = end
            else:
                start = max(previous_end, _to_abs(v0))
                # end 连续内插（可跨静音）：保证字幕时间轴无空档、无跳跃累积。
                # 之前"end 停在静音起点"会制造 2 秒级的无字幕空档，后续条目
                # 全部与语音脱节（客户反馈"有一段不出字幕、后面跟不上"）。
                end = max(start + 0.2, _to_abs(v1))
                result.append({"start": start, "end": end, "text": text})
                previous_end = end
            voice_cursor = v1

    # 句间静音期字幕适度延续（最多 +1s，且不超过下一句开始），
    # 避免"说话停顿期间屏幕上完全没有字幕"的空档观感。
    for i in range(len(result) - 1):
        nxt_start = result[i + 1]["start"]
        cur_end = result[i]["end"]
        if nxt_start > cur_end:
            result[i]["end"] = min(nxt_start - 0.05, cur_end + 1.0)
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
    silence_intervals=None,
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
        segments = parse_whisper_word_segments(
            payload, max_chars=max_chars, silence_intervals=silence_intervals,
        )
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
