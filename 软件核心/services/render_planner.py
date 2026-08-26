from dataclasses import dataclass, field
import os
import random
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


FAST_XFADE_VALUES = frozenset({
    "fade", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "fadeblack", "fadewhite",
})

MAX_MEDIA_SELECTION_ATTEMPTS = 24
MIN_RENDERABLE_CLIP_DURATION = 0.5


def media_path(item: Any) -> str:
    return str(item.get("path", "") or "") if isinstance(item, dict) else str(item or "")


@dataclass(frozen=True)
class FastTransitionSelector:
    selected_name: str
    transition_map: Mapping[str, str]
    fast_transition_map: Mapping[str, str]

    @classmethod
    def from_map(cls, selected_name: str, transition_map: Mapping[str, str]) -> "FastTransitionSelector":
        fast_map = {
            key: value
            for key, value in transition_map.items()
            if "不使用" not in key and "随机" not in key and value in FAST_XFADE_VALUES
        }
        return cls(str(selected_name or ""), dict(transition_map), fast_map)

    def choose(self, rng=None) -> str:
        rng = rng or random
        if "随机" in self.selected_name and self.fast_transition_map:
            key = rng.choice(tuple(self.fast_transition_map))
            return self.fast_transition_map.get(key, "fade")
        selected = self.transition_map.get(self.selected_name, "fade")
        return selected if selected in FAST_XFADE_VALUES else "fade"


@dataclass(frozen=True)
class XFadePlan:
    filter_complex: str
    output_label: str
    duration: float
    transition_duration: float


def build_xfade_plan(
    clip_durations: Sequence[float],
    requested_transition_duration: float,
    transition_selector: FastTransitionSelector,
    rng=None,
) -> Optional[XFadePlan]:
    durations = [max(0.05, float(value or 0.0)) for value in clip_durations]
    if len(durations) <= 1:
        return None
    transition_duration = min(
        max(0.0, float(requested_transition_duration or 0.0)),
        max(0.08, min(durations) / 2.0 - 0.03),
    )
    if transition_duration <= 0:
        return None

    filter_parts = []
    current_offset = 0.0
    last_output = "0:v"
    for index in range(1, len(durations)):
        transition = transition_selector.choose(rng)
        if transition in {"none", "random"}:
            transition = "fade"
        current_offset += max(0.05, durations[index - 1] - transition_duration)
        first_input = last_output if index == 1 else f"v{index}"
        second_input = f"{index}:v"
        output = f"v{index + 1}"
        filter_parts.append(
            f"[{first_input}][{second_input}]xfade=transition={transition}:"
            f"duration={transition_duration:.3f}:offset={current_offset:.3f}[{output}]"
        )
        last_output = output

    duration = max(0.1, sum(durations) - transition_duration * max(0, len(durations) - 1))
    return XFadePlan(
        filter_complex=";".join(filter_parts),
        output_label=last_output,
        duration=duration,
        transition_duration=transition_duration,
    )


@dataclass(frozen=True)
class RenderFilterBuilder:
    target_w: int
    target_h: int
    color_normalize_filter: str
    scale_quality: str
    is_standard_source: Callable[[str], bool]
    color_filter_for_source: Callable[[str], str]
    transition_enabled: bool
    transition_duration: float
    lut_filter: str = ""

    def _apply_lut(self, video_filter: str) -> str:
        if not self.lut_filter:
            return video_filter
        return f"{video_filter},{self.lut_filter}"

    def fit(
        self,
        zoom=1.0,
        offset_x=0.0,
        offset_y=0.0,
        duration=0.0,
        zoom_start=None,
        zoom_end=None,
        zoom_mode=None,
        source_path="",
    ) -> str:
        if self.is_standard_source(source_path):
            video_filter = f"{self.color_normalize_filter},setsar=1"
        else:
            video_filter = (
                f"{self.color_filter_for_source(source_path)},"
                f"scale={self.target_w}:{self.target_h}:force_original_aspect_ratio=increase:"
                f"{self.scale_quality},"
                f"crop={self.target_w}:{self.target_h}:(iw-ow)/2:(ih-oh)/2,setsar=1"
            )
        video_filter = self._apply_lut(video_filter)
        mode = zoom_mode or "static"
        if mode == "slow_push_in" and duration and (zoom_end or zoom) > 1.0:
            first_zoom = max(1.0, float(zoom_start if zoom_start is not None else 1.0))
            last_zoom = max(first_zoom, float(zoom_end if zoom_end is not None else zoom))
            safe_duration = max(0.1, float(duration or 0.1))
            zoom_expression = (
                f"({first_zoom:.5f}+({last_zoom:.5f}-{first_zoom:.5f})*"
                f"min(t/{safe_duration:.3f}\\,1))"
            )
            video_filter += (
                f",scale=w='trunc(iw*{zoom_expression}/2)*2':"
                f"h='trunc(ih*{zoom_expression}/2)*2':{self.scale_quality}:eval=frame"
                f",crop={self.target_w}:{self.target_h}:"
                f"(iw-ow)/2+({offset_x:.2f}):(ih-oh)/2+({offset_y:.2f})"
            )
        elif zoom > 1.0:
            video_filter += (
                f",scale=iw*{zoom}:ih*{zoom}:{self.scale_quality},"
                f"crop={self.target_w}:{self.target_h}:"
                f"(iw-ow)/2+({offset_x:.2f}):(ih-oh)/2+({offset_y:.2f})"
            )
        # 动态缩放会因偶数取整重新计算 SAR，裁剪结束后必须再次归一。
        return video_filter + ",setsar=1"

    def avatar_base_fit(self, windows, strong_structure: bool, source_path="") -> str:
        if not strong_structure or not windows:
            return self.fit(source_path=source_path)
        zoom_start = 1.01
        zoom_end = 1.10
        zoom_expression = f"{zoom_start:.5f}"
        for start_time, end_time in reversed(windows):
            duration = max(0.1, end_time - start_time)
            progress = f"min(max((t-{start_time:.3f})/{duration:.3f}\\,0)\\,1)"
            current_zoom = f"({zoom_start:.5f}+({zoom_end:.5f}-{zoom_start:.5f})*{progress})"
            zoom_expression = (
                f"if(between(t\\,{start_time:.3f}\\,{end_time:.3f})"
                f"\\,{current_zoom}\\,{zoom_expression})"
            )
        if self.is_standard_source(source_path):
            video_filter = f"{self.color_normalize_filter},setsar=1"
        else:
            video_filter = (
                f"{self.color_filter_for_source(source_path)},"
                f"scale={self.target_w}:{self.target_h}:force_original_aspect_ratio=increase:"
                f"{self.scale_quality},"
                f"crop={self.target_w}:{self.target_h}:(iw-ow)/2:(ih-oh)/2,setsar=1"
            )
        video_filter = self._apply_lut(video_filter)
        video_filter += (
            f",scale=w='trunc(iw*{zoom_expression}/2)*2':"
            f"h='trunc(ih*{zoom_expression}/2)*2':{self.scale_quality}:eval=frame"
            f",crop={self.target_w}:{self.target_h}:(iw-ow)/2:(ih-oh)/2,setsar=1"
        )
        return video_filter

    def overlay_transition(self, duration, fade_in=True, fade_out=True) -> str:
        if not self.transition_enabled or duration <= 0.35:
            return ""
        fade_duration = min(
            float(self.transition_duration or 0.0),
            max(0.05, duration / 2.0 - 0.03),
        )
        if fade_duration <= 0.05:
            return ""
        fade_out_start = max(0.0, duration - fade_duration)
        parts = [",format=rgba"]
        if fade_in:
            parts.append(f",fade=t=in:st=0:d={fade_duration:.3f}:alpha=1")
        if fade_out:
            parts.append(
                f",fade=t=out:st={fade_out_start:.3f}:d={fade_duration:.3f}:alpha=1"
            )
        return "".join(parts)


@dataclass(frozen=True)
class RenderSampler:
    target_w: int
    target_h: int
    zoom_min: float
    zoom_max: float
    zoom_mode: str
    offset_strength: float
    legacy_offset: Optional[float]
    clip_rhythm: str
    min_clip: float
    max_clip: float
    clip_jitter: float
    overlay_gap_min: float
    overlay_gap_max: float

    def sample_material_offset(self, zoom: float, rng=None) -> Tuple[float, float]:
        rng = rng or random
        if zoom <= 1.0:
            return 0.0, 0.0
        safe_x = max(0.0, self.target_w * (zoom - 1.0) / 2.0)
        safe_y = max(0.0, self.target_h * (zoom - 1.0) / 2.0)
        if self.legacy_offset is not None:
            max_x = min(self.legacy_offset, safe_x)
            max_y = min(self.legacy_offset, safe_y)
        else:
            max_x = safe_x * self.offset_strength
            max_y = safe_y * self.offset_strength
        offset_x = rng.uniform(-max_x, max_x) if max_x > 0 else 0.0
        offset_y = rng.uniform(-max_y, max_y) if max_y > 0 else 0.0
        return round(offset_x, 2), round(offset_y, 2)

    def sample_zoom_plan(self, rng=None) -> Dict[str, Any]:
        rng = rng or random
        if self.zoom_mode == "slow_push_in":
            zoom_start = round(max(1.0, self.zoom_min), 3)
            zoom_end = round(max(zoom_start, self.zoom_max), 3)
            return {
                "zoom": zoom_end,
                "zoom_start": zoom_start,
                "zoom_end": zoom_end,
                "zoom_mode": "slow_push_in",
            }
        zoom = round(rng.uniform(self.zoom_min, self.zoom_max), 3)
        return {
            "zoom": zoom,
            "zoom_start": zoom,
            "zoom_end": zoom,
            "zoom_mode": "static",
        }

    def sample_clip_duration(self, valid_duration: float, remaining: Optional[float] = None, rng=None):
        rng = rng or random
        hard_max = valid_duration
        if remaining is not None:
            hard_max = min(hard_max, remaining)
        if hard_max < 0.5:
            return None
        if self.clip_rhythm == "mixed":
            rhythm_min, rhythm_max = rng.choices(
                [(1.2, 2.4), (2.0, 4.5), (3.5, 7.0)],
                weights=[45, 40, 15],
                k=1,
            )[0]
        else:
            rhythm_min, rhythm_max = self.min_clip, self.max_clip
        low = max(0.5, rhythm_min - rng.uniform(0.0, self.clip_jitter))
        high = max(low, rhythm_max + rng.uniform(0.0, self.clip_jitter))
        high = min(high, hard_max)
        low = min(low, high)
        return rng.uniform(low, high)

    def sample_overlay_gap(self, rng=None) -> float:
        rng = rng or random
        return rng.uniform(self.overlay_gap_min, self.overlay_gap_max)


@dataclass(frozen=True)
class MediaSegmentPlanner:
    duration_getter: Callable[[str], float]
    cached_source_getter: Callable[[str], str]
    render_sampler: RenderSampler
    asset_selector: Optional[Callable[[Sequence[str], str], str]] = None
    plan_recorder: Optional[Callable[[Dict[str, Any]], None]] = None
    invalid_paths: set = field(default_factory=set, compare=False, repr=False)

    def _candidate_paths(self, pool):
        return [
            path
            for path in (media_path(item) for item in (pool or []))
            if path and path not in self.invalid_paths
        ]

    def _mark_invalid(self, path):
        if path:
            self.invalid_paths.add(path)

    def _select_candidate(self, candidates, source_type, rng):
        if self.asset_selector:
            try:
                selected = self.asset_selector(candidates, source_type)
                if selected in candidates:
                    return selected
            except Exception:
                pass
        return rng.choice(candidates)

    def _record_plan(self, plan):
        if not self.plan_recorder:
            return
        try:
            self.plan_recorder(plan)
        except Exception:
            pass

    def choose_media_segment(
        self,
        pool,
        desired_duration,
        source_type="product",
        allow_loop=False,
        rng=None,
    ):
        rng = rng or random
        candidates = self._candidate_paths(pool)
        if not candidates or desired_duration <= 0:
            return None
        attempts = min(MAX_MEDIA_SELECTION_ATTEMPTS, max(8, len(candidates)))
        for _ in range(attempts):
            path = self._select_candidate(candidates, source_type, rng)
            if not os.path.exists(path):
                self._mark_invalid(path)
                candidates = [item for item in candidates if item != path]
                if not candidates:
                    break
                continue
            raw_duration = self.duration_getter(path)
            if raw_duration <= 0.3:
                self._mark_invalid(path)
                candidates = [item for item in candidates if item != path]
                if not candidates:
                    break
                continue
            usable_duration = max(0.2, raw_duration - 0.25)
            loop_input = False
            if usable_duration < desired_duration:
                if not allow_loop:
                    continue
                start_at = rng.uniform(0.0, max(0.0, usable_duration - 0.15))
                loop_input = True
            else:
                start_at = rng.uniform(0, max(0.0, usable_duration - desired_duration))
            plan = {
                "video": self.cached_source_getter(path),
                "source_video": path,
                "start": start_at,
                "duration": desired_duration,
                "source_type": source_type,
                "loop": loop_input,
                **self.render_sampler.sample_zoom_plan(rng),
            }
            self._record_plan(plan)
            return plan
        return None

    def choose_random_product_segment(
        self,
        video_pool,
        start_time,
        remaining,
        source_type="product",
        rng=None,
    ):
        rng = rng or random
        if remaining < MIN_RENDERABLE_CLIP_DURATION or not video_pool:
            return None
        candidates = self._candidate_paths(video_pool)
        if not candidates:
            return None
        attempts = min(MAX_MEDIA_SELECTION_ATTEMPTS, max(10, len(candidates)))
        for _ in range(attempts):
            video_path = self._select_candidate(candidates, source_type, rng)
            if not os.path.exists(video_path):
                self._mark_invalid(video_path)
                candidates = [item for item in candidates if item != video_path]
                if not candidates:
                    break
                continue
            raw_duration = self.duration_getter(video_path)
            valid_duration = raw_duration - 0.8
            if valid_duration < 1.0:
                self._mark_invalid(video_path)
                candidates = [item for item in candidates if item != video_path]
                if not candidates:
                    break
                continue
            clip_duration = self.render_sampler.sample_clip_duration(
                valid_duration,
                remaining,
                rng,
            )
            if not clip_duration:
                continue
            plan = {
                "video": self.cached_source_getter(video_path),
                "source_video": video_path,
                "start": rng.uniform(0, max(0.0, valid_duration - clip_duration)),
                "duration": clip_duration,
                "global_start": start_time,
                "source_type": source_type,
                "loop": False,
                **self.render_sampler.sample_zoom_plan(rng),
            }
            self._record_plan(plan)
            return plan
        return None
