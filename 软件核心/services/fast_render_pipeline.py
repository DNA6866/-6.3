from dataclasses import dataclass
import os
import random
from typing import List, Sequence


MAX_FAST_RENDER_CLIPS = 36
MAX_WINDOWS_COMMAND_CHARS = 28000


@dataclass(frozen=True)
class FastClipInput:
    source_path: str
    start: float
    input_duration: float
    output_duration: float
    video_filter: str
    loop: bool = False


@dataclass(frozen=True)
class FastRenderGraph:
    input_args: List[str]
    filter_complex: str
    output_label: str
    duration: float
    clip_count: int


@dataclass
class FastRenderRequest:
    ffmpeg_path: str
    clip_plan: Sequence[dict]
    output_path: str
    cache_dir: str
    unique_id: str
    render_sampler: object
    filter_builder: object
    anti_dedup: bool
    use_transitions: bool
    transition_duration: float
    transition_selector: object
    encoder_args: Sequence[str]
    pixel_format: str
    watermarks: Sequence[dict]
    target_width: int
    target_height: int
    watermark_filter_builder: object
    atomic_runner: object
    emit: object


def build_fast_render_graph(
    clips: Sequence[FastClipInput],
    pix_fmt: str,
    use_transitions: bool,
    transition_duration: float,
    transition_selector,
) -> FastRenderGraph:
    """把常规素材片段放进同一条 FFmpeg 图，避免逐片段重复启动编码器。"""
    normalized = [clip for clip in clips if clip.source_path and clip.output_duration > 0.05]
    if not normalized:
        raise ValueError("没有可用于快速渲染的片段")
    if len(normalized) > MAX_FAST_RENDER_CLIPS:
        raise ValueError(f"快速渲染最多支持 {MAX_FAST_RENDER_CLIPS} 个片段")

    input_args: List[str] = []
    filters: List[str] = []
    labels: List[str] = []
    durations: List[float] = []
    for index, clip in enumerate(normalized):
        if clip.loop:
            input_args.extend(["-stream_loop", "-1"])
        input_args.extend(
            [
                "-ss",
                f"{max(0.0, float(clip.start)):.6f}",
                "-t",
                f"{max(0.06, float(clip.input_duration)):.6f}",
                "-i",
                os.path.abspath(clip.source_path),
            ]
        )
        label = f"fast_clip_{index}"
        filters.append(
            f"[{index}:v:0]{clip.video_filter},"
            "fps=30,"
            f"format={pix_fmt},settb=AVTB,setpts=PTS-STARTPTS[{label}]"
        )
        labels.append(label)
        durations.append(max(0.06, float(clip.output_duration)))

    if len(labels) == 1:
        output_label = "fast_video"
        filters.append(f"[{labels[0]}]null[{output_label}]")
        total_duration = durations[0]
    elif not use_transitions or float(transition_duration or 0.0) <= 0.0:
        output_label = "fast_video"
        filters.append(
            "".join(f"[{label}]" for label in labels)
            + f"concat=n={len(labels)}:v=1:a=0[{output_label}]"
        )
        total_duration = sum(durations)
    else:
        actual_transition = min(
            max(0.0, float(transition_duration or 0.0)),
            max(0.08, min(durations) / 2.0 - 0.03),
        )
        if actual_transition <= 0.0:
            raise ValueError("转场时长无效")
        previous = labels[0]
        current_offset = 0.0
        for index in range(1, len(labels)):
            current_offset += max(0.05, durations[index - 1] - actual_transition)
            output = f"fast_xfade_{index}"
            transition = str(transition_selector.choose() or "fade")
            if transition in {"none", "random"}:
                transition = "fade"
            filters.append(
                f"[{previous}][{labels[index]}]"
                f"xfade=transition={transition}:duration={actual_transition:.3f}:"
                f"offset={current_offset:.3f}[{output}]"
            )
            previous = output
        output_label = previous
        total_duration = sum(durations) - actual_transition * (len(durations) - 1)

    return FastRenderGraph(
        input_args=input_args,
        filter_complex=";".join(filters),
        output_label=output_label,
        duration=max(0.1, total_duration),
        clip_count=len(normalized),
    )


def command_length_is_safe(parts: Sequence[str]) -> bool:
    if os.name != "nt":
        return True
    estimated = sum(len(str(part)) + 3 for part in parts)
    return estimated <= MAX_WINDOWS_COMMAND_CHARS


def execute_fast_render(request: FastRenderRequest) -> bool:
    """执行常规混剪单进程管线，失败由调用方切回旧稳定管线。"""
    inputs = []
    for plan in request.clip_plan:
        zoom_plan = request.render_sampler.sample_zoom_plan()
        offset_x, offset_y = request.render_sampler.sample_material_offset(
            zoom_plan.get("zoom_start", zoom_plan["zoom"])
        )
        video_filter = request.filter_builder.fit(
            zoom_plan["zoom"],
            offset_x,
            offset_y,
            duration=plan["duration"],
            zoom_start=zoom_plan.get("zoom_start"),
            zoom_end=zoom_plan.get("zoom_end"),
            zoom_mode=zoom_plan.get("zoom_mode"),
            source_path=plan.get("video", ""),
        )
        speed = 1.0
        if request.anti_dedup:
            speed = round(random.uniform(0.95, 1.05), 3)
            video_filter += f",setpts={1.0 / speed}*PTS"
            brightness = round(random.uniform(-0.012, 0.012), 3)
            contrast = round(random.uniform(0.99, 1.02), 3)
            saturation = round(random.uniform(1.00, 1.04), 3)
            video_filter += (
                f",eq=brightness={brightness}:contrast={contrast}:"
                f"saturation={saturation}"
            )
        if (
            plan.get("fade_out")
            and not request.use_transitions
            and float(plan["duration"]) > 1.5
        ):
            video_filter += (
                f",fade=t=out:st={float(plan['duration']) - 0.5:.3f}:d=0.5"
            )
        inputs.append(
            FastClipInput(
                source_path=str(plan.get("video") or ""),
                start=max(0.0, float(plan.get("start") or 0.0)),
                input_duration=max(
                    0.06,
                    float(plan.get("duration") or 0.0) * speed,
                ),
                output_duration=max(
                    0.06,
                    float(plan.get("duration") or 0.0),
                ),
                video_filter=video_filter,
                loop=bool(plan.get("loop")),
            )
        )

    graph = build_fast_render_graph(
        inputs,
        request.pixel_format,
        request.use_transitions,
        request.transition_duration,
        request.transition_selector,
    )
    filter_complex = graph.filter_complex
    output_label = graph.output_label
    if request.watermarks:
        watermarked_label = "fast_video_watermarked"
        watermark_filter = (
            f"[{output_label}]format={request.pixel_format},fps=30,"
            "settb=1/90000"
            + request.watermark_filter_builder(
                request.watermarks,
                request.cache_dir,
                request.unique_id,
                time_offset=0.0,
                is_first_clip=True,
                target_w=int(request.target_width),
                target_h=int(request.target_height),
            )
            + f"[{watermarked_label}]"
        )
        filter_complex += ";" + watermark_filter
        output_label = watermarked_label

    command = [
        request.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *graph.input_args,
        "-filter_complex",
        filter_complex,
        "-map",
        f"[{output_label}]",
        "-t",
        f"{graph.duration:.6f}",
        *request.encoder_args,
        "-an",
        "-r",
        "30",
        "-pix_fmt",
        request.pixel_format,
        request.output_path,
    ]
    if not command_length_is_safe(command):
        request.emit(
            f"[{request.unique_id}] [快速管线] 命令长度超过 Windows 安全范围，"
            "当前条自动使用稳定管线。"
        )
        return False
    request.emit(
        f"[{request.unique_id}] [快速管线] {graph.clip_count} 个片段将一次解码、"
        "一次转场并只编码一次。"
    )
    return bool(
        request.atomic_runner(
            command,
            request.output_path,
            cwd=request.cache_dir,
            minimum_bytes=1024,
            require_video=True,
            retries=0,
        )
    )
