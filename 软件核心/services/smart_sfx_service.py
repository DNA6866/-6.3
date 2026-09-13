import hashlib
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import wave
from array import array
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from paths import core_path, runtime_path, workspace_path
from services.cache_service import touch_cache_entry


SFX_LIBRARY_VERSION = 5
SFX_VARIANTS_PER_FAMILY = 12
SFX_FAMILIES = (
    "impact",
    "rise",
    "bass",
    "pop",
    "chime",
    "sparkle",
    "click",
    "whoosh",
    "settle",
    "bell",
    "snap",
    "bubble",
    "coin",
    "camera",
    "glitch",
    "pulse",
)
SFX_LIBRARY_SIZE = len(SFX_FAMILIES) * SFX_VARIANTS_PER_FAMILY

_FAMILY_WEIGHTS_BY_EVENT = {
    "intro": (
        ("impact", 4.5),
        ("rise", 2.4),
        ("bass", 2.0),
        ("pulse", 1.2),
        ("glitch", 0.5),
    ),
    "hook": (
        ("impact", 3.8),
        ("whoosh", 3.2),
        ("rise", 2.0),
        ("snap", 1.4),
        ("pulse", 0.8),
    ),
    "product": (
        ("pop", 3.0),
        ("click", 2.5),
        ("sparkle", 2.2),
        ("chime", 1.5),
        ("bubble", 0.8),
        ("coin", 0.7),
        ("camera", 0.6),
    ),
    "transition": (
        ("whoosh", 4.2),
        ("snap", 2.0),
        ("rise", 1.8),
        ("click", 1.5),
        ("pulse", 0.8),
        ("glitch", 0.5),
    ),
    "avatar": (
        ("click", 2.8),
        ("pop", 2.0),
        ("sparkle", 1.2),
        ("chime", 0.8),
    ),
    "ending": (
        ("settle", 4.2),
        ("chime", 2.2),
        ("bass", 1.5),
        ("bell", 1.0),
        ("pulse", 0.8),
    ),
}
_KIND_PROBABILITY_SCALE = {
    "intro": 1.20,
    "hook": 1.15,
    "product": 0.90,
    "transition": 0.82,
    "avatar": 0.32,
    "ending": 0.78,
}
_STRENGTH_PROFILES = {
    "light": {
        "probability": 0.42,
        "min_gap": 3.0,
        "per_minute": 6,
        "volume_min": 0.88,
        "volume_max": 1.06,
    },
    "standard": {
        "probability": 0.68,
        "min_gap": 2.0,
        "per_minute": 9,
        "volume_min": 1.10,
        "volume_max": 1.36,
    },
    "strong": {
        "probability": 0.86,
        "min_gap": 1.25,
        "per_minute": 13,
        "volume_min": 1.34,
        "volume_max": 1.68,
    },
}
_STRENGTH_LABELS = {
    "light": "轻量",
    "standard": "标准",
    "strong": "强化",
}
_FAMILY_DURATION_RANGES = {
    "impact": (0.42, 0.68),
    "rise": (0.58, 0.94),
    "bass": (0.42, 0.70),
    "pop": (0.14, 0.26),
    "chime": (0.58, 0.96),
    "sparkle": (0.42, 0.78),
    "click": (0.07, 0.13),
    "whoosh": (0.48, 0.84),
    "settle": (0.52, 0.86),
    "bell": (0.62, 1.00),
    "snap": (0.09, 0.17),
    "bubble": (0.20, 0.36),
    "coin": (0.32, 0.54),
    "camera": (0.20, 0.34),
    "glitch": (0.26, 0.48),
    "pulse": (0.38, 0.68),
}
_FAMILY_KEYWORDS = {
    "impact": ("impact", "hit", "boom", "片头", "冲击", "重击"),
    "rise": ("rise", "riser", "uplift", "上升", "渐强"),
    "bass": ("bass", "sub", "低音", "低频"),
    "pop": ("pop", "弹出", "出现"),
    "chime": ("chime", "提示", "清脆"),
    "sparkle": ("sparkle", "shine", "闪光", "高光"),
    "click": ("click", "tap", "点击", "轻点"),
    "whoosh": ("whoosh", "swoosh", "swipe", "转场", "划过", "呼啸"),
    "settle": ("settle", "outro", "ending", "收尾", "结尾"),
    "bell": ("bell", "ding", "铃", "叮"),
    "snap": ("snap", "clap", "啪", "切换"),
    "bubble": ("bubble", "bloop", "气泡"),
    "coin": ("coin", "cash", "金币", "价格"),
    "camera": ("camera", "shutter", "快门", "拍照"),
    "glitch": ("glitch", "digital", "故障", "电子"),
    "pulse": ("pulse", "beat", "心跳", "脉冲"),
}
_SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}
_JIANYING_LIBRARY_KEY = "jianying"
_SFX_ORIGIN_LABELS = {
    "jianying": "剪映本地",
    "custom": "自定义",
    "builtin": "兼容兜底",
}
_asset_lock = threading.Lock()
_selection_lock = threading.RLock()
_library_lock = threading.RLock()
_recent_asset_keys = deque(maxlen=48)
_recent_families = deque(maxlen=16)
_family_usage = Counter()
_external_library_cache: Dict[str, Any] = {"scanned_at": 0.0, "assets": {}}


@dataclass(frozen=True)
class SmartSfxEvent:
    time: float
    kind: str
    family: str
    variant: int
    path: str
    duration: float
    volume: float
    origin: str = "builtin"


def normalize_sfx_strength(value: Any) -> str:
    strength = str(value or "standard").strip().lower()
    return strength if strength in _STRENGTH_PROFILES else "standard"


def smart_sfx_strength_label(value: Any) -> str:
    return _STRENGTH_LABELS[normalize_sfx_strength(value)]


def smart_sfx_origin_summary(events: Sequence[SmartSfxEvent]) -> str:
    counts = Counter(str(event.origin or "builtin") for event in events or [])
    parts = []
    for origin in ("jianying", "custom", "builtin"):
        count = int(counts.get(origin) or 0)
        if count:
            parts.append(f"{_SFX_ORIGIN_LABELS[origin]} {count} 个")
    return "、".join(parts) or "未使用"


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _creation_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _seed_number(seed: Any) -> int:
    digest = hashlib.sha1(str(seed or "").encode("utf-8", errors="ignore")).hexdigest()
    return int(digest[:16], 16)


def _effect_duration(family: str, variant: int) -> float:
    minimum, maximum = _FAMILY_DURATION_RANGES[family]
    rng = random.Random(_seed_number(f"duration|{family}|{variant}|{SFX_LIBRARY_VERSION}"))
    return round(rng.uniform(minimum, maximum), 6)


def _chirp(t: float, duration: float, start_hz: float, end_hz: float) -> float:
    slope = (end_hz - start_hz) / max(0.001, duration)
    phase = 2.0 * math.pi * (start_hz * t + 0.5 * slope * t * t)
    return math.sin(phase)


def _attack_release(t: float, duration: float, attack: float, release: float) -> float:
    attack_gain = min(1.0, max(0.0, t / max(0.0001, attack)))
    release_gain = min(1.0, max(0.0, (duration - t) / max(0.0001, release)))
    return attack_gain * release_gain


def _synthesize_original_sfx(family: str, variant: int, output_path: str) -> float:
    """生成可自由分发的原创分层音效，避免简单单音正弦波的廉价听感。"""
    duration = _effect_duration(family, variant)
    sample_rate = 48000
    frame_count = max(1, int(math.ceil(duration * sample_rate)))
    rng = random.Random(
        _seed_number(f"pcm|{family}|{variant}|{SFX_LIBRARY_VERSION}")
    )
    pitch = rng.uniform(0.88, 1.14)
    pan = rng.uniform(-0.28, 0.28)
    left = [0.0] * frame_count
    right = [0.0] * frame_count
    low_l = 0.0
    low_r = 0.0
    hold = 0.0
    hold_frames = max(20, int(sample_rate / rng.uniform(80.0, 150.0)))

    for index in range(frame_count):
        t = index / sample_rate
        progress = min(1.0, t / max(0.001, duration))
        white_l = rng.uniform(-1.0, 1.0)
        white_r = rng.uniform(-1.0, 1.0)
        cutoff = 180.0 + 6200.0 * progress
        alpha = min(0.88, 2.0 * math.pi * cutoff / sample_rate)
        low_l += alpha * (white_l - low_l)
        low_r += alpha * (white_r - low_r)
        noise = (low_l + low_r) * 0.5
        width = (low_l - low_r) * 0.20

        if family == "impact":
            envelope = (
                _attack_release(t, duration, 0.003, 0.10) * math.exp(-5.6 * t)
            )
            tone = _chirp(t, duration, 112.0 * pitch, 44.0 * pitch)
            harmonic = _chirp(t, duration, 245.0 * pitch, 78.0 * pitch)
            value = (0.76 * tone + 0.18 * harmonic) * envelope
            value += 0.30 * (white_l + white_r) * 0.5 * math.exp(-32.0 * t)
        elif family == "rise":
            envelope = math.sin(math.pi * progress) ** 1.35
            value = 0.44 * noise * envelope
            value += (
                0.34
                * _chirp(t, duration, 180.0 * pitch, 1760.0 * pitch)
                * envelope
            )
        elif family == "bass":
            envelope = (
                _attack_release(t, duration, 0.004, 0.12) * math.exp(-4.6 * t)
            )
            tone = _chirp(t, duration, 82.0 * pitch, 38.0 * pitch)
            value = (
                0.82 * tone
                + 0.17 * math.sin(2.0 * math.pi * 48.0 * pitch * t)
            ) * envelope
        elif family == "pop":
            envelope = (
                _attack_release(t, duration, 0.0015, 0.04) * math.exp(-17.0 * t)
            )
            value = (
                0.78
                * _chirp(t, duration, 720.0 * pitch, 145.0 * pitch)
                * envelope
            )
            value += 0.20 * white_l * math.exp(-50.0 * t)
        elif family == "chime":
            base = (590.0 + variant * 31.0) * pitch
            envelope = (
                _attack_release(t, duration, 0.002, 0.10) * math.exp(-3.8 * t)
            )
            value = envelope * (
                0.48 * math.sin(2.0 * math.pi * base * t)
                + 0.29 * math.sin(2.0 * math.pi * base * 1.502 * t)
                + 0.15 * math.sin(2.0 * math.pi * base * 2.017 * t)
            )
        elif family == "sparkle":
            value = 0.0
            base = (1050.0 + variant * 43.0) * pitch
            for hit_index, hit_time in enumerate(
                (0.0, duration * 0.24, duration * 0.48)
            ):
                local_t = t - hit_time
                if local_t < 0.0:
                    continue
                hit_env = math.exp(-(8.5 + hit_index) * local_t)
                value += hit_env * (
                    0.31
                    * math.sin(
                        2.0
                        * math.pi
                        * base
                        * (1.0 + hit_index * 0.18)
                        * local_t
                    )
                    + 0.17
                    * math.sin(2.0 * math.pi * base * 1.69 * local_t)
                )
        elif family == "click":
            envelope = math.exp(-58.0 * t)
            value = 0.52 * white_l * envelope
            value += (
                0.45
                * math.sin(2.0 * math.pi * 2100.0 * pitch * t)
                * envelope
            )
        elif family == "whoosh":
            envelope = math.sin(math.pi * progress) ** 1.15
            value = 0.66 * noise * envelope
            value += (
                0.20
                * _chirp(t, duration, 260.0 * pitch, 1280.0 * pitch)
                * envelope
            )
        elif family == "settle":
            envelope = (
                _attack_release(t, duration, 0.006, 0.16) * math.exp(-3.8 * t)
            )
            value = (
                0.56
                * _chirp(t, duration, 980.0 * pitch, 390.0 * pitch)
                * envelope
            )
            value += (
                0.22
                * math.sin(2.0 * math.pi * 310.0 * pitch * t)
                * envelope
            )
        elif family == "bell":
            base = (480.0 + variant * 24.0) * pitch
            envelope = (
                _attack_release(t, duration, 0.002, 0.14) * math.exp(-3.0 * t)
            )
            value = envelope * (
                0.42 * math.sin(2.0 * math.pi * base * t)
                + 0.28 * math.sin(2.0 * math.pi * base * 2.41 * t)
                + 0.17 * math.sin(2.0 * math.pi * base * 3.77 * t)
            )
        elif family == "snap":
            first = math.exp(-72.0 * t)
            second_start = 0.014 + variant * 0.0004
            local_t = max(0.0, t - second_start)
            second = math.exp(-88.0 * local_t) if t >= second_start else 0.0
            value = 0.58 * white_l * first + 0.34 * white_r * second
            value += (
                0.25
                * math.sin(2.0 * math.pi * 1350.0 * pitch * t)
                * first
            )
        elif family == "bubble":
            envelope = (
                _attack_release(t, duration, 0.002, 0.05) * math.exp(-8.0 * t)
            )
            value = (
                0.78
                * _chirp(t, duration, 250.0 * pitch, 1480.0 * pitch)
                * envelope
            )
        elif family == "coin":
            base = (1180.0 + variant * 39.0) * pitch
            envelope = (
                _attack_release(t, duration, 0.0015, 0.08) * math.exp(-6.4 * t)
            )
            value = envelope * (
                0.40 * math.sin(2.0 * math.pi * base * t)
                + 0.35 * math.sin(2.0 * math.pi * base * 1.73 * t)
                + 0.18 * math.sin(2.0 * math.pi * base * 2.31 * t)
            )
        elif family == "camera":
            first = math.exp(-64.0 * t)
            second_start = 0.075 + variant * 0.0018
            local_t = max(0.0, t - second_start)
            second = math.exp(-72.0 * local_t) if t >= second_start else 0.0
            value = 0.48 * white_l * first + 0.40 * white_r * second
            value += (
                0.20
                * math.sin(2.0 * math.pi * 840.0 * pitch * t)
                * first
            )
        elif family == "glitch":
            if index % hold_frames == 0:
                hold = rng.uniform(-1.0, 1.0)
            gate = 1.0 if int(t * (24.0 + variant)) % 3 != 1 else 0.12
            envelope = _attack_release(t, duration, 0.003, 0.05)
            value = gate * envelope * (
                0.44 * hold
                + 0.30
                * math.sin(2.0 * math.pi * (180.0 + variant * 27.0) * t)
            )
        else:
            envelope = (
                _attack_release(t, duration, 0.004, 0.12) * math.exp(-4.8 * t)
            )
            pulse_gate = 0.45 + 0.55 * max(
                0.0, math.sin(2.0 * math.pi * 7.0 * t)
            )
            value = pulse_gate * envelope * (
                0.72 * math.sin(2.0 * math.pi * 74.0 * pitch * t)
                + 0.20 * math.sin(2.0 * math.pi * 148.0 * pitch * t)
            )

        left_gain = 1.0 - max(0.0, pan) * 0.42
        right_gain = 1.0 + min(0.0, pan) * 0.42
        left[index] = value * left_gain + width
        right[index] = value * right_gain - width

    if family in {"chime", "sparkle", "settle", "bell", "coin"}:
        delay = int(sample_rate * rng.uniform(0.030, 0.052))
        echo_gain = rng.uniform(0.10, 0.17)
        for index in range(delay, frame_count):
            left[index] += right[index - delay] * echo_gain
            right[index] += left[index - delay] * echo_gain

    peak = max(
        0.0001,
        max(abs(value) for value in left),
        max(abs(value) for value in right),
    )
    rms = math.sqrt(
        sum(value * value for value in left + right) / max(1, frame_count * 2)
    )
    gain = min(0.92 / peak, 0.24 / max(0.001, rms))
    pcm = array("h")
    for left_value, right_value in zip(left, right):
        for value in (left_value, right_value):
            normalized = math.tanh(value * gain * 1.18) / math.tanh(1.18)
            pcm.append(int(max(-1.0, min(1.0, normalized)) * 32767.0))
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(output_path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return duration


def _asset_path(cache_dir: str, family: str, variant: int) -> str:
    filename = f"sfx_v{SFX_LIBRARY_VERSION}_{family}_{int(variant):02d}.wav"
    return os.path.join(cache_dir, filename)


def ensure_sfx_asset(
    ffmpeg_path: str,
    cache_dir: str,
    family: str,
    variant: int,
) -> Tuple[str, float]:
    if family not in SFX_FAMILIES:
        raise ValueError(f"未知智能音效类型：{family}")
    if not os.path.isfile(ffmpeg_path):
        raise FileNotFoundError(f"FFmpeg 不存在：{ffmpeg_path}")
    os.makedirs(cache_dir, exist_ok=True)
    output_path = _asset_path(cache_dir, family, variant)
    duration = _effect_duration(family, variant)
    if os.path.isfile(output_path) and os.path.getsize(output_path) > 2048:
        touch_cache_entry(output_path)
        return output_path, duration

    with _asset_lock:
        if os.path.isfile(output_path) and os.path.getsize(output_path) > 2048:
            touch_cache_entry(output_path)
            return output_path, duration
        temporary_path = (
            output_path + f".{os.getpid()}.{threading.get_ident()}.partial.wav"
        )
        try:
            duration = _synthesize_original_sfx(family, variant, temporary_path)
            if (
                not os.path.isfile(temporary_path)
                or os.path.getsize(temporary_path) <= 2048
            ):
                raise RuntimeError("未生成有效音效文件")
            os.replace(temporary_path, output_path)
            touch_cache_entry(output_path)
            return output_path, duration
        except (OSError, ValueError, wave.Error) as exc:
            raise RuntimeError(f"智能音效生成失败：{exc}") from exc
        finally:
            try:
                if os.path.isfile(temporary_path):
                    os.remove(temporary_path)
            except OSError:
                pass


def _external_library_roots() -> List[str]:
    custom_root = workspace_path("素材", "智能音效")
    try:
        os.makedirs(custom_root, exist_ok=True)
    except OSError:
        pass
    candidates = (
        custom_root,
        runtime_path("音效库", "智能音效"),
        runtime_path("音效库"),
        core_path("assets", "smart_sfx"),
    )
    roots = []
    seen = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(candidate))
        if normalized in seen or not os.path.isdir(candidate):
            continue
        seen.add(normalized)
        roots.append(os.path.abspath(candidate))
    return roots


def _jianying_music_cache_dir(local_appdata: str = "") -> str:
    base_dir = str(local_appdata or os.environ.get("LOCALAPPDATA") or "").strip()
    if not base_dir:
        base_dir = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(
        base_dir,
        "JianyingPro",
        "User Data",
        "Cache",
        "music",
    )


def _load_jianying_downloaded_sfx(cache_dir: str = "") -> List[str]:
    """只读取剪映下载清单中的本地文件，不扫描项目原声和识别缓存。"""
    root = os.path.abspath(cache_dir or _jianying_music_cache_dir())
    config_path = os.path.join(root, "downLoadcfg")
    try:
        if (
            not os.path.isfile(config_path)
            or os.path.getsize(config_path) > 4 * 1024 * 1024
        ):
            return []
        with open(config_path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []

    raw_items = payload.get("list") if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        return []
    candidates = []
    for index, raw in enumerate(raw_items[:500]):
        if not isinstance(raw, dict):
            continue
        relative_path = str(raw.get("path") or "").strip()
        if not relative_path:
            continue
        candidate = os.path.abspath(os.path.join(root, relative_path))
        try:
            if os.path.normcase(os.path.commonpath((root, candidate))) != os.path.normcase(
                root
            ):
                continue
        except ValueError:
            continue
        try:
            valid_file = (
                os.path.splitext(candidate)[1].lower()
                in _SUPPORTED_AUDIO_EXTENSIONS
                and os.path.isfile(candidate)
                and not os.path.islink(candidate)
                and os.path.getsize(candidate) > 2048
            )
        except OSError:
            valid_file = False
        if not valid_file:
            continue
        try:
            downloaded_at = int(raw.get("date") or 0)
        except (TypeError, ValueError):
            downloaded_at = 0
        candidates.append((downloaded_at, index, candidate))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    discovered = []
    seen = set()
    for _downloaded_at, _index, candidate in candidates:
        normalized = os.path.normcase(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        discovered.append(candidate)
    return discovered


def _classify_external_asset(path: str) -> str:
    lowered = os.path.normcase(path).lower()
    for family, keywords in _FAMILY_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return family
    return "general"


def _scan_external_library(force: bool = False) -> Dict[str, List[str]]:
    now = time.monotonic()
    with _library_lock:
        scanned_at = float(_external_library_cache.get("scanned_at") or 0.0)
        cached = _external_library_cache.get("assets")
        if (
            not force
            and scanned_at > 0.0
            and now - scanned_at < 60.0
            and isinstance(cached, dict)
        ):
            return cached
        assets: Dict[str, List[str]] = {family: [] for family in SFX_FAMILIES}
        assets["general"] = []
        assets[_JIANYING_LIBRARY_KEY] = _load_jianying_downloaded_sfx()
        for root in _external_library_roots():
            for current, directories, files in os.walk(root, followlinks=False):
                directories[:] = [
                    name
                    for name in directories
                    if not os.path.islink(os.path.join(current, name))
                ]
                for name in files:
                    path = os.path.join(current, name)
                    if (
                        os.path.splitext(name)[1].lower()
                        not in _SUPPORTED_AUDIO_EXTENSIONS
                        or os.path.islink(path)
                    ):
                        continue
                    assets[_classify_external_asset(path)].append(path)
        for values in assets.values():
            values.sort(key=os.path.normcase)
        _external_library_cache["scanned_at"] = now
        _external_library_cache["assets"] = assets
        return assets


def _run_hidden(
    command: Sequence[str],
    timeout: float,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        startupinfo=_hidden_startupinfo(),
        creationflags=_creation_flags(),
        check=False,
    )


def _probe_audio_duration(ffmpeg_path: str, path: str) -> float:
    ffprobe_path = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe.exe")
    if not os.path.isfile(ffprobe_path):
        return 0.0
    try:
        completed = _run_hidden(
            (
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ),
            timeout=8,
        )
        if completed.returncode == 0:
            return max(0.0, float((completed.stdout or "0").strip() or 0.0))
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        pass
    return 0.0


def _prepare_external_asset(
    ffmpeg_path: str,
    cache_dir: str,
    family: str,
    source_path: str,
) -> Tuple[str, float]:
    try:
        stat = os.stat(source_path)
    except OSError as exc:
        raise RuntimeError(f"自定义音效不可读取：{exc}") from exc
    fingerprint = hashlib.sha1(
        f"{os.path.abspath(source_path)}|{stat.st_size}|{stat.st_mtime_ns}".encode(
            "utf-8", errors="ignore"
        )
    ).hexdigest()[:16]
    os.makedirs(cache_dir, exist_ok=True)
    output_path = os.path.join(
        cache_dir,
        f"sfx_v{SFX_LIBRARY_VERSION}_custom_{family}_{fingerprint}.wav",
    )
    duration = min(2.5, _probe_audio_duration(ffmpeg_path, source_path) or 0.8)
    if os.path.isfile(output_path) and os.path.getsize(output_path) > 2048:
        touch_cache_entry(output_path)
        return output_path, duration

    with _asset_lock:
        if os.path.isfile(output_path) and os.path.getsize(output_path) > 2048:
            touch_cache_entry(output_path)
            return output_path, duration
        temporary_path = (
            output_path + f".{os.getpid()}.{threading.get_ident()}.partial.wav"
        )
        command = (
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source_path,
            "-vn",
            "-t",
            "2.5",
            "-af",
            (
                "highpass=f=28,"
                "loudnorm=I=-14:TP=-1.0:LRA=6,"
                "aresample=48000,"
                "aformat=sample_fmts=s16:sample_rates=48000:"
                "channel_layouts=stereo,"
                "alimiter=limit=0.94:level=false"
            ),
            "-c:a",
            "pcm_s16le",
            temporary_path,
        )
        try:
            completed = _run_hidden(command, timeout=30)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(detail[-600:] or "FFmpeg未返回错误详情")
            if (
                not os.path.isfile(temporary_path)
                or os.path.getsize(temporary_path) <= 2048
            ):
                raise RuntimeError("FFmpeg未生成有效的标准化音效")
            os.replace(temporary_path, output_path)
            touch_cache_entry(output_path)
            duration = min(
                2.5,
                _probe_audio_duration(ffmpeg_path, output_path) or duration,
            )
            return output_path, duration
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"自定义音效标准化失败：{exc}") from exc
        finally:
            try:
                if os.path.isfile(temporary_path):
                    os.remove(temporary_path)
            except OSError:
                pass


def _normalized_candidates(
    candidates: Iterable[Dict[str, Any]],
    duration: float,
) -> List[Dict[str, Any]]:
    normalized = []
    for raw in candidates or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "transition").strip().lower()
        if kind not in _FAMILY_WEIGHTS_BY_EVENT:
            kind = "transition"
        try:
            event_time = float(raw.get("time") or 0.0)
            priority = int(raw.get("priority") or 1)
        except (TypeError, ValueError):
            continue
        event_time = max(0.0, min(max(0.0, duration - 0.02), event_time))
        normalized.append(
            {
                "time": event_time,
                "kind": kind,
                "priority": max(1, priority),
                "style": str(raw.get("style") or "").strip().lower(),
            }
        )
    normalized.sort(
        key=lambda item: (
            float(item["time"]),
            -int(item["priority"]),
            str(item["kind"]),
        )
    )
    deduplicated = []
    for item in normalized:
        if (
            deduplicated
            and abs(float(item["time"]) - float(deduplicated[-1]["time"])) < 0.12
        ):
            if int(item["priority"]) > int(deduplicated[-1]["priority"]):
                deduplicated[-1] = item
            continue
        deduplicated.append(item)
    return deduplicated


def _family_weights(kind: str, style: str) -> List[Tuple[str, float]]:
    weights = dict(_FAMILY_WEIGHTS_BY_EVENT[kind])
    if kind == "transition":
        if any(
            token in style
            for token in (
                "wipe",
                "slide",
                "cover",
                "reveal",
                "wind",
                "left",
                "right",
                "up",
                "down",
            )
        ):
            weights.update({"whoosh": 7.0, "snap": 1.6, "rise": 1.1})
        elif any(
            token in style for token in ("zoom", "circle", "distance", "radial")
        ):
            weights.update({"rise": 4.8, "pulse": 2.7, "whoosh": 2.0})
        elif any(token in style for token in ("pixel", "slice", "glitch")):
            weights.update({"glitch": 4.8, "snap": 2.6, "click": 1.7})
        elif any(token in style for token in ("fade", "dissolve", "blur")):
            weights.update(
                {"settle": 3.0, "rise": 2.4, "chime": 1.5, "whoosh": 1.0}
            )
    return [(family, max(0.01, weight)) for family, weight in weights.items()]


def _weighted_choice(
    rng: random.Random,
    weighted_values: Sequence[Tuple[Any, float]],
):
    total = sum(max(0.0, float(weight)) for _value, weight in weighted_values)
    if total <= 0.0:
        return weighted_values[0][0]
    marker = rng.uniform(0.0, total)
    accumulated = 0.0
    for value, weight in weighted_values:
        accumulated += max(0.0, float(weight))
        if marker <= accumulated:
            return value
    return weighted_values[-1][0]


def _choose_asset(
    rng: random.Random,
    family_weights: Sequence[Tuple[str, float]],
    external_library: Dict[str, List[str]],
    recent_in_video: Sequence[str],
) -> Tuple[str, int, str, str, str]:
    with _selection_lock:
        adjusted_families = []
        previous_family = _recent_families[-1] if _recent_families else ""
        for family, weight in family_weights:
            if family == previous_family and len(family_weights) > 1:
                continue
            adjusted = float(weight) / (1.0 + _family_usage[family] * 0.035)
            if family in list(_recent_families)[-3:]:
                adjusted *= 0.18
            elif family in _recent_families:
                adjusted *= 0.62
            adjusted_families.append((family, max(0.01, adjusted)))
        family = str(_weighted_choice(rng, adjusted_families))

        external_paths = list(
            external_library.get(_JIANYING_LIBRARY_KEY) or []
        )
        origin = "jianying"
        if not external_paths:
            external_paths = list(external_library.get(family) or [])
            origin = "custom"
        if not external_paths:
            external_paths = list(external_library.get("general") or [])
            origin = "custom"
        external_choices = [
            path
            for path in external_paths
            if f"{origin}:{os.path.normcase(path)}" not in _recent_asset_keys
            and f"{origin}:{os.path.normcase(path)}" not in recent_in_video
        ]
        if external_paths:
            source_path = rng.choice(external_choices or external_paths)
            asset_key = f"{origin}:{os.path.normcase(source_path)}"
            variant = -1
        else:
            variants = [
                value
                for value in range(SFX_VARIANTS_PER_FAMILY)
                if f"builtin:{family}:{value}" not in _recent_asset_keys
                and f"builtin:{family}:{value}" not in recent_in_video
            ]
            variant = int(
                rng.choice(variants or list(range(SFX_VARIANTS_PER_FAMILY)))
            )
            source_path = ""
            asset_key = f"builtin:{family}:{variant}"
            origin = "builtin"

        _recent_asset_keys.append(asset_key)
        _recent_families.append(family)
        _family_usage[family] += 1
        return family, variant, source_path, asset_key, origin


def _relative_volume(
    profile: Dict[str, float],
    rng: random.Random,
    base_gain: float,
) -> float:
    try:
        base_gain = max(0.25, min(3.0, float(base_gain or 1.0)))
    except (TypeError, ValueError):
        base_gain = 1.0
    compensation = 1.0 + max(0.0, base_gain - 1.0) * 0.24
    return rng.uniform(
        float(profile["volume_min"]),
        float(profile["volume_max"]),
    ) * compensation


def plan_smart_sfx(
    ffmpeg_path: str,
    duration: float,
    candidates: Iterable[Dict[str, Any]],
    strength: str = "standard",
    seed: Any = "",
    cache_dir: str = "",
    base_gain: float = 1.0,
) -> List[SmartSfxEvent]:
    duration = max(0.0, float(duration or 0.0))
    if duration < 0.15:
        return []
    strength = normalize_sfx_strength(strength)
    profile = _STRENGTH_PROFILES[strength]
    normalized = _normalized_candidates(candidates, duration)
    if not normalized:
        return []

    rng = random.Random(_seed_number(seed))
    max_events = max(
        1,
        int(math.ceil(duration / 60.0 * int(profile["per_minute"]))),
    )
    selected = []
    last_time = -999.0
    for item in normalized:
        event_time = float(item["time"])
        mandatory = (
            not selected
            and item["kind"] in {"intro", "hook"}
            and event_time <= 0.15
        )
        if event_time - last_time < float(profile["min_gap"]):
            continue
        probability = min(
            0.97,
            (
                float(profile["probability"])
                + max(0, int(item["priority"]) - 1) * 0.08
            )
            * float(_KIND_PROBABILITY_SCALE.get(str(item["kind"]), 1.0)),
        )
        if not mandatory and rng.random() > probability:
            continue
        selected.append(item)
        last_time = event_time
    if not selected:
        selected.append(normalized[0])
    if len(selected) > max_events:
        protected = []
        for item in selected:
            if (
                not protected
                and item["kind"] in {"intro", "hook"}
                and float(item["time"]) <= 0.15
            ):
                protected.append(item)
                break
        ending = next(
            (item for item in reversed(selected) if item["kind"] == "ending"),
            None,
        )
        if ending is not None and ending not in protected and len(protected) < max_events:
            protected.append(ending)
        pool = [item for item in selected if item not in protected]
        slots = max(0, max_events - len(protected))
        ranked = sorted(
            pool,
            key=lambda item: rng.random() ** (
                1.0 / max(1.0, float(item.get("priority") or 1))
            ),
            reverse=True,
        )
        selected = sorted(
            protected + ranked[:slots],
            key=lambda item: float(item["time"]),
        )

    asset_dir = cache_dir or workspace_path("缓存", "智能音效库")
    external_library = _scan_external_library()
    events = []
    recent_in_video: List[str] = []
    for item in selected:
        family, variant, source_path, asset_key, origin = _choose_asset(
            rng,
            _family_weights(str(item["kind"]), str(item.get("style") or "")),
            external_library,
            recent_in_video,
        )
        try:
            if source_path:
                path, asset_duration = _prepare_external_asset(
                    ffmpeg_path,
                    asset_dir,
                    family,
                    source_path,
                )
            else:
                path, asset_duration = ensure_sfx_asset(
                    ffmpeg_path,
                    asset_dir,
                    family,
                    variant,
                )
        except Exception:
            variant = rng.randrange(SFX_VARIANTS_PER_FAMILY)
            asset_key = f"builtin:{family}:{variant}"
            origin = "builtin"
            path, asset_duration = ensure_sfx_asset(
                ffmpeg_path,
                asset_dir,
                family,
                variant,
            )
        volume = _relative_volume(profile, rng, base_gain)
        events.append(
            SmartSfxEvent(
                time=round(float(item["time"]), 6),
                kind=str(item["kind"]),
                family=family,
                variant=variant,
                path=path,
                duration=asset_duration,
                volume=round(volume, 4),
                origin=origin,
            )
        )
        recent_in_video.append(asset_key)
        recent_in_video = recent_in_video[-12:]
    return events


def build_sfx_mix_filters(
    base_label: str,
    indexed_events: Sequence[Tuple[int, SmartSfxEvent]],
    sample_rate: int,
    output_label: str = "audio_with_sfx",
) -> List[str]:
    if not indexed_events:
        return []
    sample_rate = max(8000, int(sample_rate))
    filters = []
    sfx_labels = []
    for index, (input_index, event) in enumerate(indexed_events):
        label = f"smart_sfx_{index}"
        delay_ms = max(0, int(round(float(event.time) * 1000.0)))
        duration = max(0.04, min(2.5, float(event.duration or 0.8)))
        filters.append(
            f"[{int(input_index)}:a:0]"
            f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,"
            f"aresample={sample_rate},"
            f"aformat=sample_fmts=fltp:sample_rates={sample_rate}:"
            f"channel_layouts=stereo,"
            f"volume={float(event.volume):.4f},"
            f"adelay={delay_ms}:all=1[{label}]"
        )
        sfx_labels.append(f"[{label}]")

    if len(sfx_labels) == 1:
        filters.append(f"{sfx_labels[0]}anull[smart_sfx_bus]")
    else:
        filters.append(
            "".join(sfx_labels)
            + f"amix=inputs={len(sfx_labels)}:duration=longest:"
            "dropout_transition=0:normalize=0,"
            "alimiter=limit=0.92:level=false[smart_sfx_bus]"
        )
    filters.extend(
        (
            "[smart_sfx_bus]apad,asplit=2[smart_sfx_key][smart_sfx_mix]",
            f"[{base_label}][smart_sfx_key]"
            "sidechaincompress=threshold=0.025:ratio=2.2:attack=8:"
            "release=220:makeup=1:mix=0.82[smart_sfx_ducked]",
            "[smart_sfx_ducked][smart_sfx_mix]"
            "amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.95:level=false[{output_label}]",
        )
    )
    return filters
