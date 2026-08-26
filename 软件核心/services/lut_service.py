from dataclasses import dataclass
import os
import random
import threading
from typing import Dict, Tuple

from paths import workspace_path
from services.cache_service import touch_cache_entry
from utils import path_to_ffmpeg


LUT_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("关闭 LUT（保持原色）", "none"),
    ("随机 LUT（每条成片固定）", "random"),
    ("自然通透", "natural_clear"),
    ("清新明亮", "clean_bright"),
    ("暖调直播", "warm_commerce"),
    ("清冷质感", "cool_premium"),
    ("产品增艳", "product_vivid"),
    ("柔和胶片", "soft_film"),
)

LUT_LABELS = dict((key, label) for label, key in LUT_OPTIONS)
BUILTIN_LUT_KEYS = tuple(
    key for _label, key in LUT_OPTIONS if key not in {"none", "random"}
)
VALID_LUT_KEYS = frozenset(LUT_LABELS)
_LUT_GRID_SIZE = 17
_LUT_CACHE_VERSION = 1
_LUT_WRITE_LOCK = threading.Lock()


_PROFILES: Dict[str, Dict[str, float]] = {
    "natural_clear": {
        "contrast": 1.035,
        "saturation": 1.050,
        "gamma": 0.985,
        "red_gain": 1.004,
        "green_gain": 1.000,
        "blue_gain": 0.995,
        "black_lift": 0.000,
    },
    "clean_bright": {
        "contrast": 1.015,
        "saturation": 1.035,
        "gamma": 0.950,
        "red_gain": 1.000,
        "green_gain": 1.004,
        "blue_gain": 1.008,
        "black_lift": 0.000,
    },
    "warm_commerce": {
        "contrast": 1.025,
        "saturation": 1.060,
        "gamma": 0.985,
        "red_gain": 1.025,
        "green_gain": 1.005,
        "blue_gain": 0.976,
        "black_lift": 0.000,
    },
    "cool_premium": {
        "contrast": 1.040,
        "saturation": 0.985,
        "gamma": 1.000,
        "red_gain": 0.985,
        "green_gain": 1.000,
        "blue_gain": 1.025,
        "black_lift": 0.000,
    },
    "product_vivid": {
        "contrast": 1.040,
        "saturation": 1.120,
        "gamma": 0.990,
        "red_gain": 1.000,
        "green_gain": 1.000,
        "blue_gain": 1.000,
        "black_lift": 0.000,
    },
    "soft_film": {
        "contrast": 0.950,
        "saturation": 0.950,
        "gamma": 0.965,
        "red_gain": 1.012,
        "green_gain": 1.000,
        "blue_gain": 0.986,
        "black_lift": 0.012,
    },
}


@dataclass(frozen=True)
class LutSelection:
    key: str
    label: str
    path: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    @property
    def ffmpeg_filter(self) -> str:
        if not self.path:
            return ""
        return f"lut3d=file='{path_to_ffmpeg(self.path)}':interp=trilinear"


def normalize_lut_style(value: object) -> str:
    key = str(value or "none").strip().lower()
    return key if key in VALID_LUT_KEYS else "none"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _grade_channel(value: float, profile: Dict[str, float], gain_key: str) -> float:
    contrast = profile.get("contrast", 1.0)
    gamma = max(0.1, profile.get("gamma", 1.0))
    graded = _clamp((value - 0.5) * contrast + 0.5)
    graded = _clamp(graded ** gamma)
    graded = _clamp(graded * profile.get(gain_key, 1.0))
    black_lift = max(0.0, profile.get("black_lift", 0.0))
    if black_lift:
        graded = _clamp(graded + black_lift * (1.0 - graded) ** 2)
    return graded


def _grade_rgb(red: float, green: float, blue: float, profile: Dict[str, float]):
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    saturation = profile.get("saturation", 1.0)
    red = luminance + (red - luminance) * saturation
    green = luminance + (green - luminance) * saturation
    blue = luminance + (blue - luminance) * saturation
    return (
        _grade_channel(red, profile, "red_gain"),
        _grade_channel(green, profile, "green_gain"),
        _grade_channel(blue, profile, "blue_gain"),
    )


def _lut_cache_path(style_key: str) -> str:
    filename = (
        f"nyy_lut_v{_LUT_CACHE_VERSION}_{style_key}_{_LUT_GRID_SIZE}.cube"
    )
    return workspace_path("缓存", "内置LUT", filename)


def _write_lut_file(style_key: str, output_path: str) -> None:
    profile = _PROFILES[style_key]
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    temp_path = (
        f"{output_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with open(temp_path, "w", encoding="ascii", newline="\n") as handle:
            handle.write(f'TITLE "NYY {style_key} v{_LUT_CACHE_VERSION}"\n')
            handle.write(f"LUT_3D_SIZE {_LUT_GRID_SIZE}\n")
            handle.write("DOMAIN_MIN 0.0 0.0 0.0\n")
            handle.write("DOMAIN_MAX 1.0 1.0 1.0\n")
            divisor = float(_LUT_GRID_SIZE - 1)
            # .cube 规范中红色变化最快，其次绿色，最后蓝色。
            for blue_index in range(_LUT_GRID_SIZE):
                blue = blue_index / divisor
                for green_index in range(_LUT_GRID_SIZE):
                    green = green_index / divisor
                    for red_index in range(_LUT_GRID_SIZE):
                        red = red_index / divisor
                        out_red, out_green, out_blue = _grade_rgb(
                            red,
                            green,
                            blue,
                            profile,
                        )
                        handle.write(
                            f"{out_red:.7f} {out_green:.7f} {out_blue:.7f}\n"
                        )
        os.replace(temp_path, output_path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def ensure_builtin_lut(style_key: str) -> str:
    key = normalize_lut_style(style_key)
    if key not in BUILTIN_LUT_KEYS:
        return ""
    output_path = _lut_cache_path(key)
    with _LUT_WRITE_LOCK:
        if not os.path.isfile(output_path) or os.path.getsize(output_path) < 20000:
            _write_lut_file(key, output_path)
    touch_cache_entry(output_path)
    return output_path


def resolve_lut_selection(style_key: object, rng=None) -> LutSelection:
    key = normalize_lut_style(style_key)
    if key == "none":
        return LutSelection(key="none", label=LUT_LABELS["none"])
    if key == "random":
        rng = rng or random
        key = rng.choice(BUILTIN_LUT_KEYS)
    path = ensure_builtin_lut(key)
    return LutSelection(key=key, label=LUT_LABELS.get(key, key), path=path)
