import hashlib
import math
import os
import random
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from paths import workspace_path
from services.cache_service import touch_cache_entry


SFX_LIBRARY_VERSION = 3
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

_FAMILY_BY_EVENT = {
    "intro": ("impact", "rise", "bass", "pulse", "glitch"),
    "hook": ("impact", "rise", "whoosh", "snap", "glitch"),
    "product": (
        "pop",
        "chime",
        "sparkle",
        "click",
        "bell",
        "bubble",
        "coin",
        "camera",
    ),
    "transition": ("whoosh", "rise", "click", "snap", "glitch", "pulse"),
    "avatar": ("pop", "sparkle", "chime", "bell", "bubble", "coin"),
    "ending": ("settle", "chime", "bass", "bell", "pulse"),
}
_STRENGTH_PROFILES = {
    "light": {
        "probability": 0.44,
        "min_gap": 2.8,
        "per_minute": 7,
        "volume_min": 0.48,
        "volume_max": 0.62,
    },
    "standard": {
        "probability": 0.72,
        "min_gap": 1.5,
        "per_minute": 12,
        "volume_min": 0.72,
        "volume_max": 0.92,
    },
    "strong": {
        "probability": 0.92,
        "min_gap": 0.9,
        "per_minute": 18,
        "volume_min": 0.92,
        "volume_max": 1.16,
    },
}
_STRENGTH_LABELS = {
    "light": "轻量",
    "standard": "标准",
    "strong": "强化",
}
_asset_lock = threading.Lock()


@dataclass(frozen=True)
class SmartSfxEvent:
    time: float
    kind: str
    family: str
    variant: int
    path: str
    duration: float
    volume: float


def normalize_sfx_strength(value: Any) -> str:
    strength = str(value or "standard").strip().lower()
    return strength if strength in _STRENGTH_PROFILES else "standard"


def smart_sfx_strength_label(value: Any) -> str:
    return _STRENGTH_LABELS[normalize_sfx_strength(value)]


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


def _effect_spec(family: str, variant: int) -> Tuple[float, str]:
    """返回音效时长与波形表达式，不同家族使用不同的听感结构。"""
    variant = max(0, min(SFX_VARIANTS_PER_FAMILY - 1, int(variant)))
    shift = variant / max(1, SFX_VARIANTS_PER_FAMILY - 1)
    if family == "impact":
        duration = 0.38 + shift * 0.22
        frequency = 66.0 + variant * 4.2
        expression = (
            f"0.68*sin(2*PI*({frequency:.3f}*t+30*t*t))*exp(-6.2*t)"
            f"+0.20*sin(2*PI*{frequency * 3.1:.3f}*t)*exp(-17*t)"
            "+0.12*(2*random(0)-1)*exp(-32*t)"
        )
    elif family == "rise":
        duration = 0.48 + shift * 0.28
        start = 170.0 + variant * 21.0
        sweep = 680.0 + variant * 64.0
        expression = (
            f"0.43*sin(2*PI*({start:.3f}*t+{sweep:.3f}*t*t))"
            f"*sin(PI*t/{duration:.6f})*sin(PI*t/{duration:.6f})"
            f"+0.11*(2*random(0)-1)*sin(PI*t/{duration:.6f})"
        )
    elif family == "bass":
        duration = 0.44 + shift * 0.24
        frequency = 52.0 + variant * 3.4
        expression = (
            f"0.72*sin(2*PI*({frequency:.3f}*t-9*t*t))*exp(-5.4*t)"
            f"+0.17*sin(2*PI*{frequency * 2.0:.3f}*t)*exp(-9*t)"
        )
    elif family == "pop":
        duration = 0.13 + shift * 0.10
        frequency = 390.0 + variant * 52.0
        expression = (
            f"0.64*sin(2*PI*({frequency:.3f}*t-460*t*t))*exp(-20*t)"
            "+0.10*(2*random(0)-1)*exp(-48*t)"
        )
    elif family == "chime":
        duration = 0.52 + shift * 0.30
        frequency = 680.0 + variant * 58.0
        expression = (
            f"0.40*sin(2*PI*{frequency:.3f}*t)*exp(-4.4*t)"
            f"+0.23*sin(2*PI*{frequency * 1.5:.3f}*t)*exp(-6.2*t)"
            f"+0.11*sin(2*PI*{frequency * 2.03:.3f}*t)*exp(-8.0*t)"
        )
    elif family == "sparkle":
        duration = 0.34 + shift * 0.22
        frequency = 1180.0 + variant * 102.0
        expression = (
            f"0.36*sin(2*PI*{frequency:.3f}*t)*exp(-7.2*t)"
            f"+0.22*sin(2*PI*{frequency * 1.68:.3f}*t)*exp(-9.8*t)"
            f"+0.08*sin(2*PI*({frequency * 0.75:.3f}*t+900*t*t))*exp(-8*t)"
        )
    elif family == "click":
        duration = 0.065 + shift * 0.045
        frequency = 1750.0 + variant * 185.0
        expression = (
            f"0.53*sin(2*PI*{frequency:.3f}*t)*exp(-54*t)"
            "+0.18*(2*random(0)-1)*exp(-82*t)"
        )
    elif family == "whoosh":
        duration = 0.42 + shift * 0.28
        start = 280.0 + variant * 29.0
        sweep = 960.0 + variant * 84.0
        expression = (
            f"0.34*sin(2*PI*({start:.3f}*t+{sweep:.3f}*t*t))"
            f"*sin(PI*t/{duration:.6f})"
            f"+0.24*(2*random(0)-1)*sin(PI*t/{duration:.6f})"
        )
    elif family == "settle":
        duration = 0.46 + shift * 0.24
        start = 820.0 + variant * 42.0
        sweep = 520.0 + variant * 35.0
        expression = (
            f"0.46*sin(2*PI*({start:.3f}*t-{sweep:.3f}*t*t))"
            f"*exp(-4.8*t)"
        )
    elif family == "bell":
        duration = 0.58 + shift * 0.34
        frequency = 540.0 + variant * 47.0
        expression = (
            f"0.36*sin(2*PI*{frequency:.3f}*t)*exp(-3.8*t)"
            f"+0.25*sin(2*PI*{frequency * 2.41:.3f}*t)*exp(-5.0*t)"
            f"+0.13*sin(2*PI*{frequency * 3.77:.3f}*t)*exp(-6.4*t)"
        )
    elif family == "snap":
        duration = 0.085 + shift * 0.055
        frequency = 1150.0 + variant * 92.0
        expression = (
            "0.42*(2*random(0)-1)*exp(-58*t)"
            f"+0.31*sin(2*PI*{frequency:.3f}*t)*exp(-44*t)"
        )
    elif family == "bubble":
        duration = 0.18 + shift * 0.12
        start = 260.0 + variant * 31.0
        sweep = 1450.0 + variant * 95.0
        expression = (
            f"0.58*sin(2*PI*({start:.3f}*t+{sweep:.3f}*t*t))"
            "*exp(-10*t)"
        )
    elif family == "coin":
        duration = 0.30 + shift * 0.18
        frequency = 1320.0 + variant * 83.0
        expression = (
            f"0.41*sin(2*PI*{frequency:.3f}*t)*exp(-7.0*t)"
            f"+0.31*sin(2*PI*{frequency * 1.73:.3f}*t)*exp(-9.5*t)"
            f"+0.13*sin(2*PI*{frequency * 2.31:.3f}*t)*exp(-12*t)"
        )
    elif family == "camera":
        duration = 0.20 + shift * 0.10
        frequency = 820.0 + variant * 70.0
        expression = (
            "0.34*(2*random(0)-1)*exp(-55*t)"
            "+0.24*(2*random(1)-1)*exp(-75*abs(t-0.085))"
            f"+0.20*sin(2*PI*{frequency:.3f}*t)*exp(-28*t)"
        )
    elif family == "glitch":
        duration = 0.24 + shift * 0.18
        frequency = 190.0 + variant * 37.0
        expression = (
            f"0.34*sin(2*PI*{frequency:.3f}*t)"
            f"*sin(2*PI*{23 + variant:.3f}*t)*exp(-4.5*t)"
            "+0.16*(2*random(0)-1)*sin(2*PI*31*t)"
        )
    else:
        duration = 0.34 + shift * 0.20
        frequency = 92.0 + variant * 8.0
        expression = (
            f"0.58*sin(2*PI*{frequency:.3f}*t)*exp(-5.2*t)"
            f"+0.26*sin(2*PI*{frequency * 2.5:.3f}*t)*exp(-8*t)"
        )
    return duration, expression


def _asset_path(cache_dir: str, family: str, variant: int) -> str:
    filename = (
        f"sfx_v{SFX_LIBRARY_VERSION}_{family}_"
        f"{int(variant):02d}.wav"
    )
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
    duration, expression = _effect_spec(family, variant)
    if os.path.isfile(output_path) and os.path.getsize(output_path) > 512:
        touch_cache_entry(output_path)
        return output_path, duration

    with _asset_lock:
        if os.path.isfile(output_path) and os.path.getsize(output_path) > 512:
            touch_cache_entry(output_path)
            return output_path, duration
        temporary_path = (
            output_path
            + f".{os.getpid()}.{threading.get_ident()}.partial.wav"
        )
        command = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={expression}:s=48000:d={duration:.6f}",
            "-af",
            (
                "pan=stereo|c0=c0|c1=c0,"
                "loudnorm=I=-14:TP=-1.2:LRA=5,"
                "aresample=48000,"
                "volume=2.0,"
                "alimiter=limit=0.92:level=false,"
                f"afade=t=out:st={max(0.01, duration - 0.06):.6f}:d=0.06"
            ),
            "-c:a",
            "pcm_s16le",
            temporary_path,
        ]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                startupinfo=_hidden_startupinfo(),
                creationflags=_creation_flags(),
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(detail[-600:] or "FFmpeg 未返回错误详情")
            if not os.path.isfile(temporary_path) or os.path.getsize(temporary_path) <= 512:
                raise RuntimeError("FFmpeg 未生成有效音效文件")
            os.replace(temporary_path, output_path)
            touch_cache_entry(output_path)
            return output_path, duration
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"智能音效生成失败：{exc}") from exc
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
        if kind not in _FAMILY_BY_EVENT:
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


def plan_smart_sfx(
    ffmpeg_path: str,
    duration: float,
    candidates: Iterable[Dict[str, Any]],
    strength: str = "standard",
    seed: Any = "",
    cache_dir: str = "",
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
        if len(selected) >= max_events:
            break
        if event_time - last_time < float(profile["min_gap"]):
            continue
        probability = min(
            0.96,
            float(profile["probability"])
            + max(0, int(item["priority"]) - 1) * 0.08,
        )
        if not mandatory and rng.random() > probability:
            continue
        selected.append(item)
        last_time = event_time
    if not selected:
        selected.append(normalized[0])

    asset_dir = cache_dir or workspace_path("缓存", "智能音效库")
    events = []
    previous_family = ""
    recent_assets = []
    for item in selected:
        family_choices = [
            value
            for value in _FAMILY_BY_EVENT[str(item["kind"])]
            if value != previous_family
        ]
        family = rng.choice(
            family_choices or _FAMILY_BY_EVENT[str(item["kind"])]
        )
        variant_choices = [
            value
            for value in range(SFX_VARIANTS_PER_FAMILY)
            if (family, value) not in recent_assets
        ]
        variant = rng.choice(
            variant_choices or list(range(SFX_VARIANTS_PER_FAMILY))
        )
        path, asset_duration = ensure_sfx_asset(
            ffmpeg_path,
            asset_dir,
            family,
            variant,
        )
        volume = rng.uniform(
            float(profile["volume_min"]),
            float(profile["volume_max"]),
        )
        events.append(
            SmartSfxEvent(
                time=round(float(item["time"]), 6),
                kind=str(item["kind"]),
                family=family,
                variant=variant,
                path=path,
                duration=asset_duration,
                volume=round(volume, 4),
            )
        )
        previous_family = family
        recent_assets.append((family, variant))
        recent_assets = recent_assets[-8:]
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
        filters.append(
            f"[{int(input_index)}:a:0]"
            f"aresample={sample_rate},"
            f"aformat=sample_fmts=fltp:sample_rates={sample_rate}:"
            f"channel_layouts=stereo,"
            f"volume={float(event.volume):.4f},"
            f"adelay={delay_ms}:all=1[{label}]"
        )
        sfx_labels.append(f"[{label}]")
    filters.append(
        f"[{base_label}]"
        + "".join(sfx_labels)
        + f"amix=inputs={len(sfx_labels) + 1}:duration=first:"
        "dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95:level=false[{output_label}]"
    )
    return filters
