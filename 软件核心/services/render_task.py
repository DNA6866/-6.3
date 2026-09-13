from dataclasses import dataclass, field, replace
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from services.lut_service import normalize_lut_style


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "启用", "是"}
    return bool(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_target_resolution(resolution_text: str, default: Tuple[int, int] = (720, 1280)) -> Tuple[int, int]:
    text = str(resolution_text or "").split(" ")[0]
    match = re.match(r"^\s*(\d+)\s*[xX:]\s*(\d+)\s*$", text)
    if not match:
        return default
    width = max(16, _as_int(match.group(1), default[0]))
    height = max(16, _as_int(match.group(2), default[1]))
    return width, height


@dataclass(frozen=True)
class EncoderProfile:
    pix_fmt: str
    enc_args: List[str]
    final_enc_args: List[str]


@dataclass(frozen=True)
class AudioJobContext:
    audio_path: str
    audio_row: int
    track_index: int
    audio_basename: str
    audio_name: str
    output_stamp: str
    salt: str
    unique_id: str

    @classmethod
    def from_audio_dict(cls, audio_dict: Dict[str, Any], round_idx: int) -> "AudioJobContext":
        audio_path = str((audio_dict or {}).get("path", "") or "")
        track_index = _as_int((audio_dict or {}).get("track_index", -1), -1)
        audio_row = _as_int((audio_dict or {}).get("row", -1), -1)
        audio_basename = str((audio_dict or {}).get("source_basename") or os.path.splitext(os.path.basename(audio_path))[0])
        # 第一轨道模式用 track_index 做行号标识，保持输出文件名唯一。
        if track_index >= 0:
            audio_name = f"{audio_basename}_Track{track_index}"
            audio_row = track_index
        else:
            audio_name = audio_basename
        salt = f"{random.randint(10000, 99999)}"
        output_stamp = time.strftime("%Y%m%d_%H%M%S")
        unique_id = f"{audio_name}_Row{audio_row}_R{round_idx}_{salt}"
        return cls(
            audio_path=audio_path,
            audio_row=audio_row,
            track_index=track_index,
            audio_basename=audio_basename,
            audio_name=audio_name,
            output_stamp=output_stamp,
            salt=salt,
            unique_id=unique_id,
        )


@dataclass(frozen=True)
class RenderTaskConfig:
    mixer_mode: str = "traditional"
    gpu_mode: str = "CPU 软解"
    threads: int = 1
    auto_performance: bool = True
    loop_count: int = 1
    resolution: str = "720x1280 (竖屏)"
    target_w: int = 720
    target_h: int = 1280
    cache_dir: str = ""
    output_dir: str = ""
    min_clip: float = 2.0
    max_clip: float = 3.0
    trans_type: str = "不使用转场"
    trans_dur: float = 0.3
    main_vol: float = 1.0
    bgm_vol: float = 0.2
    smart_sfx_enabled: bool = False
    smart_sfx_strength: str = "standard"
    fast_render_enabled: bool = True
    ai_intro_mode: bool = False
    anti_dedup: bool = True
    lut_style: str = "none"
    preview_only: bool = False
    use_first_track_audio: bool = False
    strong_structure: bool = False
    hook_duration: float = 2.0
    strong_avatar_duration: float = 2.5
    strong_avatar_smart_tail: bool = True
    strong_product_duration: float = 3.0
    strong_avatar_count: int = 3
    clip_rhythm: str = "balanced"
    clip_jitter: float = 0.8
    overlay_gap_min: float = 1.5
    overlay_gap_max: float = 3.5
    overlay_zoom_min: float = 1.01
    overlay_zoom_max: float = 1.10
    overlay_zoom_mode: str = "slow_push_in"
    # 参数防重抖动：让每条成片的固定参数（开头时长/强结构节点/转场时长/音量）在
    # ±ratio 内随机浮动，避免同一批成片的时间轴骨架完全一致被平台判重复。
    # 0 表示关闭（保持原有固定值行为）。
    param_jitter: float = 0.15
    # 公平轮转配额：单个队列任务每轮最多产出该数量的成片后让位其它任务
    # （任务保持 waiting + 断点续传，由调度器轮转下一店铺）。0 = 关闭。
    fair_quota: int = 0
    overlay_offset_strength: Optional[float] = None
    legacy_overlay_offset: Optional[float] = None
    sub_config: Dict[str, Any] = field(default_factory=dict)
    cover_config: Dict[str, Any] = field(default_factory=dict)
    sys_fonts: Dict[str, str] = field(default_factory=dict)

    @property
    def target_res(self) -> str:
        return f"{self.target_w}:{self.target_h}"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RenderTaskConfig":
        data = data or {}
        mixer_mode = str(data.get("mixer_mode") or "traditional")
        # ordinary（原"普通视频拼接"）与 traditional 引擎行为完全一致，统一归并为 traditional
        if mixer_mode == "ordinary":
            mixer_mode = "traditional"
        if mixer_mode not in {
            "traditional",
            "first_track",
            "ai_intro_first_track",
            "ai_intro_audio",
        }:
            mixer_mode = "traditional"
        target_w, target_h = parse_target_resolution(data.get("resolution", "720x1280 (竖屏)"))
        overlay_gap_min = max(0.0, _as_float(data.get("overlay_gap_min"), 1.5))
        overlay_gap_max = max(overlay_gap_min, _as_float(data.get("overlay_gap_max"), 3.5))
        overlay_zoom_min = max(1.0, _as_float(data.get("overlay_zoom_min"), 1.01))
        overlay_zoom_max = max(overlay_zoom_min, _as_float(data.get("overlay_zoom_max"), 1.10))
        overlay_zoom_mode = str(data.get("overlay_zoom_mode") or "slow_push_in")
        if overlay_zoom_mode not in {"slow_push_in", "static"}:
            overlay_zoom_mode = "slow_push_in"
        smart_sfx_strength = str(
            data.get("smart_sfx_strength") or "standard"
        ).strip().lower()
        if smart_sfx_strength not in {"light", "standard", "strong"}:
            smart_sfx_strength = "standard"

        offset_strength = data.get("overlay_offset_strength")
        legacy_offset = None
        if offset_strength is None:
            legacy_offset = max(0.0, _as_float(data.get("overlay_offset"), 0.0))
        else:
            offset_strength = max(0.0, min(100.0, _as_float(offset_strength, 0.0)))

        sys_fonts = data.get("sys_fonts") if isinstance(data.get("sys_fonts"), dict) else {}
        sub_config = data.get("sub_config") if isinstance(data.get("sub_config"), dict) else {}
        cover_config = data.get("cover_config") if isinstance(data.get("cover_config"), dict) else {}

        return cls(
            mixer_mode=mixer_mode,
            gpu_mode=str(data.get("gpu_mode") or "CPU 软解"),
            threads=max(1, _as_int(data.get("threads"), 1)),
            auto_performance=_as_bool(data.get("auto_performance"), True),
            loop_count=max(1, _as_int(data.get("loop_count"), 1)),
            resolution=str(data.get("resolution") or "720x1280 (竖屏)"),
            target_w=target_w,
            target_h=target_h,
            cache_dir=str(data.get("cache_dir") or ""),
            output_dir=str(data.get("output_dir") or ""),
            min_clip=max(0.5, _as_float(data.get("min_clip"), 2.0)),
            max_clip=max(0.5, _as_float(data.get("max_clip"), 3.0)),
            trans_type=str(data.get("trans_type") or "不使用转场"),
            trans_dur=max(0.0, _as_float(data.get("trans_dur"), 0.3)),
            main_vol=max(0.0, _as_float(data.get("main_vol"), 1.0)),
            bgm_vol=max(0.0, _as_float(data.get("bgm_vol"), 0.2)),
            smart_sfx_enabled=_as_bool(data.get("smart_sfx_enabled"), False),
            smart_sfx_strength=smart_sfx_strength,
            fast_render_enabled=_as_bool(data.get("fast_render_enabled"), True),
            ai_intro_mode=_as_bool(data.get("ai_intro_mode"), False),
            anti_dedup=_as_bool(data.get("anti_dedup"), True),
            lut_style=normalize_lut_style(data.get("lut_style")),
            preview_only=_as_bool(data.get("preview_only"), False),
            use_first_track_audio=_as_bool(data.get("use_first_track_audio"), False),
            strong_structure=_as_bool(data.get("strong_structure"), False),
            hook_duration=max(0.0, _as_float(data.get("hook_duration"), 2.0)),
            strong_avatar_duration=max(0.8, _as_float(data.get("strong_avatar_duration"), 2.5)),
            strong_avatar_smart_tail=_as_bool(data.get("strong_avatar_smart_tail"), True),
            strong_product_duration=max(0.8, _as_float(data.get("strong_product_duration"), 3.0)),
            strong_avatar_count=max(1, _as_int(data.get("strong_avatar_count"), 3)),
            clip_rhythm=str(data.get("clip_rhythm") or "balanced"),
            clip_jitter=max(0.0, _as_float(data.get("clip_jitter"), 0.8)),
            overlay_gap_min=overlay_gap_min,
            overlay_gap_max=overlay_gap_max,
            overlay_zoom_min=overlay_zoom_min,
            overlay_zoom_max=overlay_zoom_max,
            overlay_zoom_mode=overlay_zoom_mode,
            param_jitter=max(0.0, min(0.5, _as_float(data.get("param_jitter"), 0.15))),
            fair_quota=max(0, _as_int(data.get("fair_quota"), 0)),
            overlay_offset_strength=offset_strength,
            legacy_overlay_offset=legacy_offset,
            sub_config=dict(sub_config),
            cover_config=dict(cover_config),
            sys_fonts=dict(sys_fonts),
        )


def _dict_items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


@dataclass
class RenderTaskData:
    """渲染任务的结构化入口，保留可变时长缓存供并发任务复用。"""

    config: RenderTaskConfig
    audios: List[Dict[str, Any]] = field(default_factory=list)
    videos: List[Dict[str, Any]] = field(default_factory=list)
    covers: List[Dict[str, Any]] = field(default_factory=list)
    bgms: List[Dict[str, Any]] = field(default_factory=list)
    ai_intro_videos: List[Dict[str, Any]] = field(default_factory=list)
    hook_videos: List[Dict[str, Any]] = field(default_factory=list)
    first_track_videos: List[Dict[str, Any]] = field(default_factory=list)
    second_segment_videos: List[Dict[str, Any]] = field(default_factory=list)
    watermarks: List[Dict[str, Any]] = field(default_factory=list)
    duration_cache: Dict[str, float] = field(default_factory=dict)
    enable_standard_video_cache: bool = False
    asset_scope: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RenderTaskData":
        source = data if isinstance(data, dict) else {}
        duration_cache = source.get("duration_cache")
        if not isinstance(duration_cache, dict):
            duration_cache = {}
            source["duration_cache"] = duration_cache
        return cls(
            config=RenderTaskConfig.from_dict(source),
            audios=_dict_items(source.get("audios")),
            videos=_dict_items(source.get("videos")),
            covers=_dict_items(source.get("covers")),
            bgms=_dict_items(source.get("bgms")),
            ai_intro_videos=_dict_items(source.get("ai_intro_videos")),
            hook_videos=_dict_items(source.get("hook_videos")),
            first_track_videos=_dict_items(source.get("first_track_videos")),
            second_segment_videos=_dict_items(
                source.get("second_segment_videos") or source.get("preferred_first_videos")
            ),
            watermarks=_dict_items(source.get("watermarks")),
            duration_cache=duration_cache,
            enable_standard_video_cache=_as_bool(
                source.get("enable_standard_video_cache"),
                False,
            ),
            asset_scope=str(source.get("asset_scope") or ""),
        )

    def with_runtime_profile(self, gpu_mode: str, threads: int) -> None:
        self.config = replace(
            self.config,
            gpu_mode=str(gpu_mode or self.config.gpu_mode),
            threads=max(1, _as_int(threads, self.config.threads)),
        )
