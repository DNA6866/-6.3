from dataclasses import dataclass


@dataclass(frozen=True)
class WindowLayoutProfile:
    window_width: int
    window_height: int
    minimum_width: int
    minimum_height: int
    sidebar_width: int
    content_margin: int
    log_minimum_height: int
    log_maximum_height: int
    compact_header: bool


def choose_window_layout(
    available_width: int,
    available_height: int,
) -> WindowLayoutProfile:
    """按 Qt 的逻辑像素选择布局，兼容 768p、1080p 和 4K 缩放屏。"""
    width = max(1024, int(available_width or 1920))
    height = max(640, int(available_height or 1080))
    compact = width < 1550 or height < 880
    spacious = width >= 2200 and height >= 1250

    outer_gap = 16 if compact else 28
    minimum_width = min(1180 if compact else 1320, width - 8)
    minimum_height = min(700 if compact else 800, height - 8)
    target_width = max(
        minimum_width,
        min(1890 if not spacious else 2040, width - outer_gap),
    )
    target_height = max(
        minimum_height,
        min(1185 if not spacious else 1280, height - outer_gap),
    )
    return WindowLayoutProfile(
        window_width=max(1000, target_width),
        window_height=max(620, target_height),
        minimum_width=max(1000, minimum_width),
        minimum_height=max(620, minimum_height),
        sidebar_width=170 if compact else 188,
        content_margin=10 if compact else 16,
        log_minimum_height=56 if compact else 68,
        log_maximum_height=84 if compact else 110,
        compact_header=compact,
    )
