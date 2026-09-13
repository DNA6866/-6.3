import json
import math
import os
import subprocess
import time
import uuid

from PyQt5.QtCore import QEvent, QRect, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt5.QtMultimedia import QAudioFormat, QAudioOutput, QMediaPlayer
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QShortcut,
    QSplitter,
    QStackedLayout,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from paths import config_dir, tools_dir, workspace_path
from services.platform_service import open_path
from ui_components import configure_spinbox_for_direct_input
from utils import detect_performance_profile

from .quick_video_trim_engine import (
    ClipExportWorker,
    PreviewBuildWorker,
    VIDEO_EXTENSIONS,
    format_time,
    hidden_startupinfo,
)


def collect_video_paths_from_urls(urls):
    paths = []
    seen = set()
    for url in urls or []:
        raw_path = url.toLocalFile()
        if not raw_path:
            continue
        path = os.path.abspath(raw_path)
        candidates = []
        if os.path.isfile(path):
            candidates = [path]
        elif os.path.isdir(path):
            for root, _, files in os.walk(path):
                for filename in files:
                    candidates.append(os.path.join(root, filename))
        for candidate in candidates:
            candidate = os.path.abspath(candidate)
            key = os.path.normcase(candidate)
            if key in seen:
                continue
            if os.path.isfile(candidate) and candidate.lower().endswith(VIDEO_EXTENSIONS):
                seen.add(key)
                paths.append(candidate)
    return paths


def _safe_decode_size(width, height, max_side=1280):
    width = max(2, int(width or 1280))
    height = max(2, int(height or 720))
    scale = min(1.0, float(max_side) / max(width, height))
    width = max(2, int(width * scale))
    height = max(2, int(height * scale))
    return width, height


def frame_step_time(current_seconds, fps, direction, duration=0.0):
    """按源视频帧率返回相邻一帧的精确时间，避免整数毫秒累计误差。"""
    safe_fps = max(1.0, min(240.0, float(fps or 25.0)))
    current_index = max(0, int(math.floor(max(0.0, float(current_seconds or 0.0)) * safe_fps + 0.5)))
    target_index = max(0, current_index + (-1 if int(direction) < 0 else 1))
    safe_duration = max(0.0, float(duration or 0.0))
    if safe_duration > 0:
        last_index = max(0, int(math.ceil(safe_duration * safe_fps)) - 1)
        target_index = min(target_index, last_index)
    return target_index / safe_fps


def decode_single_frame(ffmpeg_path, source, seconds, width, height, timeout=8):
    width, height = _safe_decode_size(width, height)
    command = [
        ffmpeg_path,
        "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, float(seconds or 0)):.6f}",
        "-i", source,
        "-frames:v", "1",
        "-vf", f"scale={width}:{height}",
        "-pix_fmt", "rgb24",
        "-f", "rawvideo",
        "pipe:1",
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        startupinfo=hidden_startupinfo(),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    frame_size = width * height * 3
    if result.returncode != 0 or len(result.stdout) < frame_size:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "无法解码当前画面")
    image = QImage(result.stdout[:frame_size], width, height, width * 3, QImage.Format_RGB888)
    return image.copy()


class FFmpegVideoDecodeWorker(QThread):
    frameReady = pyqtSignal(QImage, float)
    failed = pyqtSignal(str)

    def __init__(self, ffmpeg_path, source, start_seconds, width, height, fps, parent=None):
        super().__init__(parent)
        self.ffmpeg_path = ffmpeg_path
        self.source = source
        self.start_seconds = max(0.0, float(start_seconds or 0))
        self.width, self.height = _safe_decode_size(width, height)
        self.fps = max(1.0, min(30.0, float(fps or 25.0)))
        self._process = None

    def cancel(self):
        self.requestInterruption()
        process = self._process
        if process and process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        startupinfo=hidden_startupinfo(),
                        timeout=5,
                        check=False,
                    )
                else:
                    process.terminate()
            except Exception:
                pass

    def _read_exact(self, size):
        chunks = []
        remaining = size
        while remaining > 0 and not self.isInterruptionRequested():
            chunk = self._process.stdout.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def run(self):
        command = [
            self.ffmpeg_path,
            "-hide_banner", "-loglevel", "error",
            "-ss", f"{self.start_seconds:.6f}",
            "-i", self.source,
            "-an",
            "-vf", f"scale={self.width}:{self.height},fps={self.fps:.6f}",
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            "pipe:1",
        ]
        frame_size = self.width * self.height * 3
        frame_interval = 1.0 / self.fps
        frame_index = 0
        clock_start = time.monotonic()
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            while not self.isInterruptionRequested():
                frame = self._read_exact(frame_size)
                if len(frame) < frame_size:
                    break
                image = QImage(frame, self.width, self.height, self.width * 3, QImage.Format_RGB888).copy()
                position_seconds = self.start_seconds + frame_index * frame_interval
                self.frameReady.emit(image, position_seconds)
                frame_index += 1
                target_time = clock_start + frame_index * frame_interval
                delay = target_time - time.monotonic()
                if delay > 0:
                    self.msleep(int(delay * 1000))
            if self._process and self._process.poll() is None:
                self.cancel()
            if self._process:
                self._process.wait(timeout=3)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))
        finally:
            self._process = None


class FFmpegAudioDecodeWorker(QThread):
    chunkReady = pyqtSignal(object)
    failed = pyqtSignal(str)

    SAMPLE_RATE = 44100
    CHANNELS = 2
    BYTES_PER_SAMPLE = 2

    def __init__(self, ffmpeg_path, source, start_seconds, parent=None):
        super().__init__(parent)
        self.ffmpeg_path = ffmpeg_path
        self.source = source
        self.start_seconds = max(0.0, float(start_seconds or 0))
        self._process = None

    def cancel(self):
        self.requestInterruption()
        process = self._process
        if process and process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        startupinfo=hidden_startupinfo(),
                        timeout=5,
                        check=False,
                    )
                else:
                    process.terminate()
            except Exception:
                pass

    def run(self):
        command = [
            self.ffmpeg_path,
            "-hide_banner", "-loglevel", "error",
            "-ss", f"{self.start_seconds:.6f}",
            "-i", self.source,
            "-vn",
            "-ac", str(self.CHANNELS),
            "-ar", str(self.SAMPLE_RATE),
            "-f", "s16le",
            "pipe:1",
        ]
        bytes_per_second = self.SAMPLE_RATE * self.CHANNELS * self.BYTES_PER_SAMPLE
        chunk_size = max(4096, bytes_per_second // 25)
        clock_start = time.monotonic()
        chunks_sent = 0
        output_seen = False
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            while not self.isInterruptionRequested():
                chunk = self._process.stdout.read(chunk_size)
                if not chunk:
                    break
                output_seen = True
                self.chunkReady.emit(chunk)
                chunks_sent += 1
                target_time = clock_start + chunks_sent * chunk_size / bytes_per_second
                delay = target_time - time.monotonic()
                if delay > 0:
                    self.msleep(int(delay * 1000))
            if self._process and self._process.poll() is None:
                self.cancel()
            stderr = b""
            if self._process:
                try:
                    _, stderr = self._process.communicate(timeout=3)
                except Exception:
                    stderr = b""
            if not output_seen and not self.isInterruptionRequested():
                detail = (stderr or b"").decode("utf-8", errors="replace").strip()
                self.failed.emit(detail or "没有可播放的音频轨道")
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))
        finally:
            self._process = None


class HardwareDetectWorker(QThread):
    profileReady = pyqtSignal(object)

    def run(self):
        try:
            self.profileReady.emit(detect_performance_profile(tools_dir()))
        except Exception:
            self.profileReady.emit({
                "level": "cpu",
                "summary": "CPU 精准编码",
                "details": ["硬件编码检测失败，已使用 CPU。"],
            })


class VideoDropList(QListWidget):
    filesDropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setStyleSheet("""
            QListWidget {
                background: #0B1120;
                border: 1px solid #334155;
                border-radius: 7px;
                color: #CBD5E1;
                outline: none;
            }
            QListWidget::item {
                min-height: 30px;
                padding: 7px 9px;
                border-bottom: 1px solid #1E293B;
            }
            QListWidget::item:selected {
                background: #2563EB;
                color: #FFFFFF;
                border-left: 5px solid #FDE047;
                font-weight: 800;
            }
            QListWidget::item:hover {
                background: #1E293B;
            }
        """)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = collect_video_paths_from_urls(event.mimeData().urls())
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class VideoPreviewArea(QScrollArea):
    filesDropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.preview_container = QWidget()
        self.preview_container.setObjectName("videoPreviewContainer")
        self.preview_container.setAcceptDrops(True)
        self.video_widget = QVideoWidget()
        self.video_widget.setAcceptDrops(True)
        self.video_widget.setAspectRatioMode(Qt.KeepAspectRatio)
        self.video_widget.setStyleSheet("background:#000000;border:none;")
        self.poster_label = QLabel("拖入视频或点击导入后显示预览")
        self.poster_label.setAcceptDrops(True)
        self.poster_label.setAlignment(Qt.AlignCenter)
        self.poster_label.setStyleSheet("background:#000000;color:#94A3B8;border:none;")
        self.poster_label.setWordWrap(True)
        self.frame_label = QLabel()
        self.frame_label.setAcceptDrops(True)
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setStyleSheet("background:#000000;border:none;")
        self.poster_pixmap = QPixmap()
        self.frame_pixmap = QPixmap()
        self.preview_stack = QStackedLayout(self.preview_container)
        self.preview_stack.setContentsMargins(0, 0, 0, 0)
        self.preview_stack.addWidget(self.poster_label)
        self.preview_stack.addWidget(self.frame_label)
        self.preview_stack.addWidget(self.video_widget)
        self.preview_stack.setCurrentWidget(self.poster_label)
        self.setWidget(self.preview_container)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignCenter)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("QScrollArea{background:#000000;border:1px solid #334155;}")
        self.source_width = 1280
        self.source_height = 720
        self.mode = "fit"
        self.setMinimumHeight(360)
        self.preview_container.installEventFilter(self)
        self.video_widget.installEventFilter(self)
        self.poster_label.installEventFilter(self)
        self.frame_label.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.DragEnter, QEvent.DragMove):
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return True
        if event.type() == QEvent.Drop:
            paths = collect_video_paths_from_urls(event.mimeData().urls())
            if paths:
                self.filesDropped.emit(paths)
                event.acceptProposedAction()
                return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = collect_video_paths_from_urls(event.mimeData().urls())
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def show_message(self, text):
        self.poster_pixmap = QPixmap()
        self.frame_pixmap = QPixmap()
        self.poster_label.setPixmap(QPixmap())
        self.frame_label.setPixmap(QPixmap())
        self.poster_label.setText(text)
        self.preview_stack.setCurrentWidget(self.poster_label)

    def set_poster_image(self, path):
        pixmap = QPixmap(path) if path and os.path.isfile(path) else QPixmap()
        if pixmap.isNull():
            return False
        self.poster_pixmap = pixmap
        self.poster_label.setText("")
        self._refresh_poster_pixmap()
        self.preview_stack.setCurrentWidget(self.poster_label)
        return True

    def show_frame(self, image):
        if image is None or image.isNull():
            return False
        self.frame_pixmap = QPixmap.fromImage(image)
        self._refresh_frame_pixmap()
        self.preview_stack.setCurrentWidget(self.frame_label)
        return True

    def show_video(self):
        self.preview_stack.setCurrentWidget(self.video_widget)

    def set_source_size(self, width, height):
        self.source_width = max(1, int(width or 1280))
        self.source_height = max(1, int(height or 720))
        self._update_video_size()

    def set_display_mode(self, mode):
        self.mode = mode
        self._update_video_size()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.mode == "fit":
            QTimer.singleShot(0, self._update_video_size)

    def _update_video_size(self):
        if self.mode == "actual":
            self.preview_container.setFixedSize(self.source_width, self.source_height)
            self._refresh_poster_pixmap()
            self._refresh_frame_pixmap()
            return
        viewport = self.viewport().size()
        available_w = max(160, viewport.width() - 4)
        available_h = max(120, viewport.height() - 4)
        scale = min(available_w / self.source_width, available_h / self.source_height)
        width = max(1, int(self.source_width * scale))
        height = max(1, int(self.source_height * scale))
        self.preview_container.setFixedSize(width, height)
        self._refresh_poster_pixmap()
        self._refresh_frame_pixmap()

    def _refresh_poster_pixmap(self):
        if self.poster_pixmap.isNull():
            return
        target_size = self.preview_container.size()
        if target_size.width() <= 0 or target_size.height() <= 0:
            return
        scaled = self.poster_pixmap.scaled(
            target_size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.poster_label.setPixmap(scaled)

    def _refresh_frame_pixmap(self):
        if self.frame_pixmap.isNull():
            return
        target_size = self.preview_container.size()
        if target_size.width() <= 0 or target_size.height() <= 0:
            return
        scaled = self.frame_pixmap.scaled(
            target_size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.frame_label.setPixmap(scaled)


class HighlightTimeline(QWidget):
    seekRequested = pyqtSignal(float)
    pointsChanged = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.duration = 0.0
        self.position = 0.0
        self.in_point = -1.0
        self.out_point = -1.0
        self.selected_range = None
        self.segments = []
        self.thumbnails = []
        self.waveform = []
        self._drag_target = ""
        self.setMinimumHeight(154)
        self.setMaximumHeight(180)
        self.setMouseTracking(True)

    def set_media(self, duration, thumbnails=None, waveform=None):
        self.duration = max(0.0, float(duration or 0))
        self.position = 0.0
        self.in_point = -1.0
        self.out_point = -1.0
        self.selected_range = None
        self.thumbnails = [
            QPixmap(path) for path in (thumbnails or []) if os.path.isfile(path)
        ]
        self.waveform = list(waveform or [])
        self.update()

    def set_segments(self, segments):
        self.segments = list(segments or [])
        self.update()

    def set_position(self, seconds):
        self.position = max(0.0, min(self.duration, float(seconds or 0)))
        self.update()

    def set_points(self, start, end):
        self.in_point = float(start) if start is not None else -1.0
        self.out_point = float(end) if end is not None else -1.0
        self.update()
        self.pointsChanged.emit(self.in_point, self.out_point)

    def set_selected_range(self, start=None, end=None, label=""):
        if start is None or end is None:
            self.selected_range = None
        else:
            low, high = sorted((float(start), float(end)))
            self.selected_range = {
                "start": low,
                "end": high,
                "label": str(label or ""),
            }
        self.update()

    def _content_rect(self):
        return self.rect().adjusted(10, 20, -10, -18)

    def _x_from_time(self, value):
        rect = self._content_rect()
        if self.duration <= 0:
            return rect.left()
        ratio = max(0.0, min(1.0, float(value) / self.duration))
        return rect.left() + int(rect.width() * ratio)

    def _time_from_x(self, x):
        rect = self._content_rect()
        if rect.width() <= 0 or self.duration <= 0:
            return 0.0
        ratio = (x - rect.left()) / rect.width()
        return max(0.0, min(self.duration, ratio * self.duration))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0B1120"))
        rect = self._content_rect()
        painter.setPen(QPen(QColor("#475569"), 1))
        painter.setBrush(QColor("#020617"))
        painter.drawRoundedRect(rect, 7, 7)

        thumb_height = max(62, int(rect.height() * 0.66))
        thumb_rect = QRect(rect.left() + 1, rect.top() + 1, rect.width() - 2, thumb_height)
        if self.thumbnails:
            cell_width = thumb_rect.width() / len(self.thumbnails)
            for index, pixmap in enumerate(self.thumbnails):
                target = QRect(
                    int(thumb_rect.left() + index * cell_width),
                    thumb_rect.top(),
                    max(1, int(cell_width + 1)),
                    thumb_rect.height(),
                )
                painter.drawPixmap(target, pixmap, pixmap.rect())
        else:
            painter.setPen(QColor("#64748B"))
            painter.drawText(thumb_rect, Qt.AlignCenter, "导入视频后生成完整缩略图时间轴")

        wave_top = thumb_rect.bottom() + 2
        wave_rect = QRect(rect.left() + 2, wave_top, rect.width() - 4, rect.bottom() - wave_top - 1)
        if self.waveform and wave_rect.height() > 4:
            middle = wave_rect.center().y()
            count = max(1, wave_rect.width())
            step = len(self.waveform) / count
            painter.setPen(QPen(QColor("#60A5FA"), 1))
            for index in range(count):
                peak = self.waveform[min(len(self.waveform) - 1, int(index * step))]
                height = max(1, int(peak * wave_rect.height() * 0.46))
                x = wave_rect.left() + index
                painter.drawLine(x, middle - height, x, middle + height)

        for index, segment in enumerate(self.segments, 1):
            left = self._x_from_time(segment["start"])
            right = self._x_from_time(segment["end"])
            segment_rect = QRect(left, rect.top(), max(2, right - left), rect.height())
            fill_color = QColor(str(segment.get("fill_color") or "#22C55E"))
            fill_alpha = segment.get("fill_alpha", 68)
            fill_alpha = 68 if fill_alpha is None else int(fill_alpha)
            fill_color.setAlpha(max(0, min(255, fill_alpha)))
            border_color = QColor(
                str(segment.get("border_color") or "#22C55E")
            )
            text_color = QColor(str(segment.get("text_color") or "#DCFCE7"))
            label = str(segment.get("label") or index)
            if fill_color.alpha() > 0:
                painter.fillRect(segment_rect, fill_color)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(border_color, 2))
            painter.drawRect(segment_rect.adjusted(0, 0, -1, -1))
            status_bar_height = max(
                0,
                min(
                    segment_rect.height(),
                    int(segment.get("status_bar_height", 0) or 0),
                ),
            )
            if status_bar_height:
                painter.fillRect(
                    QRect(
                        segment_rect.left(),
                        segment_rect.top(),
                        segment_rect.width(),
                        status_bar_height,
                    ),
                    border_color,
                )
            painter.setPen(text_color)
            if segment_rect.width() >= 20:
                font_size = 8 if segment_rect.width() < 44 else 9
                painter.setFont(
                    QFont("Microsoft YaHei UI", font_size, QFont.Bold)
                )
                text_width = painter.fontMetrics().horizontalAdvance(label) + 8
                badge_width = min(max(18, text_width), max(18, segment_rect.width() - 4))
                badge_rect = QRect(
                    segment_rect.center().x() - badge_width // 2,
                    segment_rect.top() + 3,
                    badge_width,
                    18,
                )
                painter.fillRect(badge_rect, QColor(2, 6, 23, 205))
                painter.setPen(text_color)
                painter.drawText(badge_rect, Qt.AlignCenter, label)

        if self.selected_range:
            left = self._x_from_time(self.selected_range.get("start") or 0.0)
            right = self._x_from_time(self.selected_range.get("end") or 0.0)
            selected_rect = QRect(
                left,
                rect.top(),
                max(2, right - left),
                rect.height(),
            )
            painter.fillRect(selected_rect, QColor(250, 204, 21, 42))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#FACC15"), 3))
            painter.drawRect(selected_rect.adjusted(1, 1, -2, -2))

        if self.in_point >= 0 and self.out_point >= 0:
            left = self._x_from_time(min(self.in_point, self.out_point))
            right = self._x_from_time(max(self.in_point, self.out_point))
            painter.fillRect(QRect(left, rect.top(), max(2, right - left), rect.height()), QColor(37, 99, 235, 70))

        marker_specs = [
            (self.in_point, QColor("#38BDF8"), "入"),
            (self.out_point, QColor("#F472B6"), "出"),
        ]
        for value, color, label in marker_specs:
            if value < 0:
                continue
            x = self._x_from_time(value)
            painter.setPen(QPen(color, 3))
            painter.drawLine(x, rect.top() - 5, x, rect.bottom() + 2)
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(QRect(x - 12, 1, 24, 18), 4, 4)
            painter.setPen(QColor("#020617"))
            painter.drawText(QRect(x - 12, 1, 24, 18), Qt.AlignCenter, label)

        play_x = self._x_from_time(self.position)
        painter.setPen(QPen(QColor("#FDE047"), 2))
        painter.drawLine(play_x, rect.top() - 7, play_x, rect.bottom() + 4)
        painter.setPen(QColor("#CBD5E1"))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(rect.left(), self.height() - 3, "00:00")
        total = format_time(self.duration, milliseconds=False)
        painter.drawText(rect.adjusted(0, 0, 0, 15), Qt.AlignRight | Qt.AlignBottom, total)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self.duration <= 0:
            return
        tolerance = 10
        if self.in_point >= 0 and abs(event.x() - self._x_from_time(self.in_point)) <= tolerance:
            self._drag_target = "in"
        elif self.out_point >= 0 and abs(event.x() - self._x_from_time(self.out_point)) <= tolerance:
            self._drag_target = "out"
        else:
            self._drag_target = "seek"
            self.seekRequested.emit(self._time_from_x(event.x()))

    def mouseMoveEvent(self, event):
        if not self._drag_target:
            return
        value = self._time_from_x(event.x())
        if self._drag_target == "in":
            self.in_point = value
            self.pointsChanged.emit(self.in_point, self.out_point)
            self.update()
        elif self._drag_target == "out":
            self.out_point = value
            self.pointsChanged.emit(self.in_point, self.out_point)
            self.update()
        elif self._drag_target == "seek":
            self.seekRequested.emit(value)

    def mouseReleaseEvent(self, event):
        self._drag_target = ""


class QuickVideoTrimWidget(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.entries = {}
        self.current_path = ""
        self.preview_workers = []
        self.export_worker = None
        self.hardware_worker = None
        self.hardware_profile = {"level": "cpu", "summary": "CPU 精准编码"}
        self.loop_range = None
        self._video_output_available = False
        self.decode_worker = None
        self.audio_worker = None
        self.audio_output = None
        self.audio_device = None
        self.software_preview_playing = False
        self.preview_position_ms = 0
        self.preview_position_seconds = 0.0
        self._suppress_player_updates = False
        self._pending_seek_seconds = None
        self._segment_table_refreshing = False
        self._pending_segment_selection = None
        self.seek_preview_timer = QTimer(self)
        self.seek_preview_timer.setSingleShot(True)
        self.seek_preview_timer.timeout.connect(self._show_pending_seek_frame)
        self.output_dir = workspace_path("输出", "视频快速剪辑")
        self.cache_dir = workspace_path("缓存", "视频快速剪辑")
        self.config_path = os.path.join(config_dir(), "quick_video_trim.json")
        self.ffmpeg_path = os.path.join(tools_dir(), "ffmpeg.exe")
        self.ffprobe_path = os.path.join(tools_dir(), "ffprobe.exe")
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)
        self._build_ui()
        self._load_config()
        self._setup_player()
        self._setup_shortcuts()
        self._install_keyboard_filter()
        configure_spinbox_for_direct_input(self)
        self._start_hardware_detection()

    def _log(self, text):
        self.log_signal.emit(f"[视频快速剪辑] {text}")

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(8)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("视频快速剪辑")
        title.setObjectName("pageTitle")
        subtitle = QLabel("原比例画面预览，使用入点和出点标记多个高潮片段；每个片段独立导出为一个视频。")
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        import_button = QPushButton("导入视频")
        import_button.setObjectName("toolbarPrimaryButton")
        import_button.clicked.connect(self.choose_videos)
        open_output_button = QPushButton("打开输出目录")
        open_output_button.clicked.connect(self.open_output_dir)
        header.addWidget(import_button)
        header.addWidget(open_output_button)
        root.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_source_panel())
        splitter.addWidget(self._build_editor_panel())
        splitter.addWidget(self._build_segment_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([270, 1050, 370])
        root.addWidget(splitter, stretch=1)

    def _build_source_panel(self):
        panel = QGroupBox("视频列表")
        panel.setMinimumWidth(230)
        panel.setMaximumWidth(330)
        layout = QVBoxLayout(panel)
        self.video_list = VideoDropList()
        self.video_list.setToolTip("可直接拖入一个或多个视频")
        self.video_list.filesDropped.connect(self.add_video_paths)
        self.video_list.currentItemChanged.connect(self._on_video_selected)
        layout.addWidget(self.video_list, stretch=1)

        action_row = QHBoxLayout()
        add_button = QPushButton("添加")
        add_button.clicked.connect(self.choose_videos)
        remove_button = QPushButton("移除")
        remove_button.clicked.connect(self.remove_current_video)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self.clear_videos)
        action_row.addWidget(add_button)
        action_row.addWidget(remove_button)
        action_row.addWidget(clear_button)
        layout.addLayout(action_row)

        self.video_info = QLabel("拖入视频或点击“导入视频”。")
        self.video_info.setObjectName("mutedText")
        self.video_info.setWordWrap(True)
        layout.addWidget(self.video_info)
        return panel

    def _build_editor_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        preview_bar = QHBoxLayout()
        self.current_file_label = QLabel("尚未选择视频")
        self.current_file_label.setStyleSheet("font-weight:700;color:#E2E8F0;")
        self.display_mode_combo = QComboBox()
        self.display_mode_combo.addItem("完整原比例", "fit")
        self.display_mode_combo.addItem("100%原始像素", "actual")
        self.display_mode_combo.currentIndexChanged.connect(
            lambda: self.preview_area.set_display_mode(self.display_mode_combo.currentData())
        )
        preview_bar.addWidget(self.current_file_label, 1)
        preview_bar.addWidget(QLabel("画面显示"))
        preview_bar.addWidget(self.display_mode_combo)
        layout.addLayout(preview_bar)

        self.preview_area = VideoPreviewArea()
        self.preview_area.filesDropped.connect(self.add_video_paths)
        layout.addWidget(self.preview_area, stretch=1)

        playback = QHBoxLayout()
        self.play_button = QPushButton("播放")
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_button.clicked.connect(self.toggle_playback)
        frame_back = QPushButton("上一帧")
        frame_back.clicked.connect(lambda: self.step_frame(-1))
        frame_forward = QPushButton("下一帧")
        frame_forward.clicked.connect(lambda: self.step_frame(1))
        back_second = QPushButton("-1秒")
        back_second.clicked.connect(lambda: self.seek_relative(-1.0))
        next_second = QPushButton("+1秒")
        next_second.clicked.connect(lambda: self.seek_relative(1.0))
        self.time_label = QLabel("00:00:00.000 / 00:00:00.000")
        self.time_label.setMinimumWidth(220)
        self.time_label.setAlignment(Qt.AlignCenter)
        playback.addWidget(self.play_button)
        playback.addWidget(frame_back)
        playback.addWidget(frame_forward)
        playback.addWidget(back_second)
        playback.addWidget(next_second)
        playback.addStretch(1)
        playback.addWidget(self.time_label)
        layout.addLayout(playback)

        self.timeline = HighlightTimeline()
        self.timeline.seekRequested.connect(self.seek_to_seconds)
        self.timeline.pointsChanged.connect(self._on_points_changed)
        layout.addWidget(self.timeline)

        mark_row = QHBoxLayout()
        self.set_in_button = QPushButton("设置入点  I")
        self.set_in_button.clicked.connect(self.set_in_point)
        self.set_out_button = QPushButton("设置出点  O")
        self.set_out_button.clicked.connect(self.set_out_point)
        add_segment = QPushButton("添加高潮片段")
        add_segment.setObjectName("toolbarPrimaryButton")
        add_segment.clicked.connect(self.add_segment)
        update_segment = QPushButton("更新选中片段")
        update_segment.clicked.connect(self.update_selected_segment)
        clear_points = QPushButton("清除入点/出点")
        clear_points.clicked.connect(self.clear_points)
        self.point_label = QLabel("入点：--    出点：--    时长：--")
        self.point_label.setObjectName("mutedText")
        mark_row.addWidget(self.set_in_button)
        mark_row.addWidget(self.set_out_button)
        mark_row.addWidget(add_segment)
        mark_row.addWidget(update_segment)
        mark_row.addWidget(clear_points)
        mark_row.addStretch(1)
        mark_row.addWidget(self.point_label)
        layout.addLayout(mark_row)

        self.preview_progress = QProgressBar()
        self.preview_progress.setRange(0, 100)
        self.preview_progress.setValue(0)
        self.preview_progress.setFormat("等待导入视频")
        self.preview_progress.setMaximumHeight(20)
        layout.addWidget(self.preview_progress)
        return panel

    def _build_segment_panel(self):
        panel = QGroupBox("全部高潮片段")
        panel.setMinimumWidth(420)
        panel.setMaximumWidth(560)
        layout = QVBoxLayout(panel)

        table_hint = QLabel("这里统一显示左侧所有视频已添加的高潮片段，可一次性导出。")
        table_hint.setObjectName("mutedText")
        table_hint.setWordWrap(True)
        layout.addWidget(table_hint)

        self.segment_table = QTableWidget(0, 5)
        self.segment_table.setHorizontalHeaderLabels(["视频", "序号", "入点", "出点", "时长"])
        self.segment_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.segment_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.segment_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.segment_table.itemSelectionChanged.connect(self._load_selected_segment)
        self.segment_table.itemDoubleClicked.connect(lambda *_: self.preview_selected_segment())
        header = self.segment_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.segment_table, stretch=1)

        segment_actions = QHBoxLayout()
        preview_button = QPushButton("预览选中")
        preview_button.clicked.connect(self.preview_selected_segment)
        delete_button = QPushButton("删除选中")
        delete_button.clicked.connect(self.delete_selected_segment)
        self.loop_checkbox = QCheckBox("循环预览")
        self.loop_checkbox.setChecked(True)
        segment_actions.addWidget(preview_button)
        segment_actions.addWidget(delete_button)
        segment_actions.addWidget(self.loop_checkbox)
        layout.addLayout(segment_actions)

        export_group = QGroupBox("独立导出")
        export_layout = QVBoxLayout(export_group)
        path_row = QHBoxLayout()
        self.output_edit = QLineEdit(self.output_dir)
        browse_output = QPushButton("选择")
        browse_output.clicked.connect(self.choose_output_dir)
        path_row.addWidget(self.output_edit, 1)
        path_row.addWidget(browse_output)
        export_layout.addLayout(path_row)

        self.export_mode_combo = QComboBox()
        self.export_mode_combo.addItem("精准裁剪（推荐，切点准确）", "precise")
        self.export_mode_combo.addItem("极速无损（按关键帧，切点可能有偏差）", "fast")
        export_layout.addWidget(self.export_mode_combo)

        self.hardware_label = QLabel("正在检测精准导出加速方式...")
        self.hardware_label.setObjectName("mutedText")
        self.hardware_label.setWordWrap(True)
        export_layout.addWidget(self.hardware_label)

        self.export_current_button = QPushButton("导出当前视频片段")
        self.export_current_button.setObjectName("startButton")
        self.export_current_button.clicked.connect(lambda: self.start_export(False))
        self.export_all_button = QPushButton("导出右侧全部片段")
        self.export_all_button.clicked.connect(lambda: self.start_export(True))
        self.stop_export_button = QPushButton("停止导出")
        self.stop_export_button.setObjectName("stopButton")
        self.stop_export_button.setEnabled(False)
        self.stop_export_button.clicked.connect(self.stop_export)
        export_layout.addWidget(self.export_current_button)
        export_layout.addWidget(self.export_all_button)
        export_layout.addWidget(self.stop_export_button)

        self.export_progress = QProgressBar()
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(0)
        export_layout.addWidget(self.export_progress)
        self.export_status = QLabel("每个入点+出点区间会单独生成一个视频，不会自动合并。")
        self.export_status.setObjectName("mutedText")
        self.export_status.setWordWrap(True)
        export_layout.addWidget(self.export_status)
        layout.addWidget(export_group)

        return panel

    def _setup_player(self):
        self.player = QMediaPlayer(self, QMediaPlayer.VideoSurface)
        self.player.setVideoOutput(self.preview_area.video_widget)
        self.player.positionChanged.connect(self._on_qt_position_changed)
        self.player.durationChanged.connect(self._on_player_duration)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.videoAvailableChanged.connect(self._on_video_available_changed)
        self.player.stateChanged.connect(self._on_player_state_changed)
        self.player.error.connect(self._on_player_error)

    def _setup_shortcuts(self):
        shortcuts = [
            ("I", self.set_in_point),
            ("O", self.set_out_point),
            ("Left", lambda: self.step_frame(-1)),
            ("Right", lambda: self.step_frame(1)),
            ("Delete", self.delete_selected_segment),
        ]
        self._shortcuts = []
        for key, callback in shortcuts:
            shortcut = QShortcut(key, self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    def _install_keyboard_filter(self):
        app = QApplication.instance()
        self._keyboard_filter_installed = False
        if app:
            app.installEventFilter(self)
            self._keyboard_filter_installed = True

    def eventFilter(self, watched, event):
        if (
            event.type() == QEvent.KeyPress
            and self.isVisible()
            and event.key() == Qt.Key_Space
            and event.modifiers() == Qt.NoModifier
        ):
            active_window = QApplication.activeWindow()
            if active_window is not None and active_window is not self.window():
                return False
            focus = QApplication.focusWidget()
            if isinstance(focus, (QLineEdit, QComboBox)):
                return False
            self.toggle_playback()
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space and event.modifiers() == Qt.NoModifier:
            self.toggle_playback()
            event.accept()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = collect_video_paths_from_urls(event.mimeData().urls())
        if paths:
            self.add_video_paths(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def _current_position_seconds(self):
        if self.current_path:
            return max(0.0, float(self.preview_position_seconds or 0.0))
        return 0.0

    def _source_fps(self):
        metadata = self.entries.get(self.current_path, {}).get("metadata", {})
        return float(metadata.get("fps") or 25.0)

    def _decode_dimensions(self):
        size = self.preview_area.preview_container.size()
        width = size.width() if size.width() > 32 else self.preview_area.source_width
        height = size.height() if size.height() > 32 else self.preview_area.source_height
        return _safe_decode_size(width, height)

    def _set_preview_play_state(self, playing):
        self.software_preview_playing = bool(playing)
        if playing:
            self.play_button.setText("暂停")
            self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        else:
            self.play_button.setText("播放")
            self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def _stop_decode_worker(self, wait=True):
        worker = self.decode_worker
        self.decode_worker = None
        if worker and worker.isRunning():
            worker.cancel()
            if wait:
                worker.wait(3000)

    def _ensure_audio_output(self):
        if self.audio_output and self.audio_device:
            return True
        audio_format = QAudioFormat()
        audio_format.setSampleRate(FFmpegAudioDecodeWorker.SAMPLE_RATE)
        audio_format.setChannelCount(FFmpegAudioDecodeWorker.CHANNELS)
        audio_format.setSampleSize(16)
        audio_format.setCodec("audio/pcm")
        audio_format.setByteOrder(QAudioFormat.LittleEndian)
        audio_format.setSampleType(QAudioFormat.SignedInt)
        try:
            self.audio_output = QAudioOutput(audio_format, self)
            self.audio_output.setBufferSize(
                FFmpegAudioDecodeWorker.SAMPLE_RATE
                * FFmpegAudioDecodeWorker.CHANNELS
                * FFmpegAudioDecodeWorker.BYTES_PER_SAMPLE
            )
            self.audio_device = self.audio_output.start()
        except Exception as exc:
            self.audio_output = None
            self.audio_device = None
            self._log(f"音频输出初始化失败：{exc}")
            return False
        return self.audio_device is not None

    def _stop_audio_worker(self, wait=True):
        worker = self.audio_worker
        self.audio_worker = None
        if worker and worker.isRunning():
            worker.cancel()
            if wait:
                worker.wait(3000)
        if self.audio_output:
            try:
                self.audio_output.stop()
            except Exception:
                pass
        self.audio_device = None
        self.audio_output = None

    def _start_audio_preview(self, start_seconds):
        self._stop_audio_worker(wait=True)
        if not self._ensure_audio_output():
            return
        worker = FFmpegAudioDecodeWorker(
            self.ffmpeg_path,
            self.current_path,
            start_seconds,
            self,
        )
        worker.chunkReady.connect(self._on_audio_chunk_ready)
        worker.failed.connect(self._on_audio_preview_failed)
        worker.finished.connect(lambda w=worker: self._on_audio_worker_finished(w))
        self.audio_worker = worker
        worker.start()

    def _on_audio_chunk_ready(self, chunk):
        if not chunk or not self.audio_device:
            return
        try:
            self.audio_device.write(chunk)
        except Exception as exc:
            self._log(f"音频播放写入失败：{exc}")

    def _on_audio_preview_failed(self, message):
        if message:
            self._log(f"内置音频预览提示：{message}")

    def _on_audio_worker_finished(self, worker):
        if self.audio_worker is worker:
            self.audio_worker = None

    def _on_decode_worker_finished(self, worker):
        if self.decode_worker is worker:
            self.decode_worker = None
            self._stop_audio_worker(wait=False)
            self._set_preview_play_state(False)

    def _start_software_preview(self, start_seconds=None):
        if not self.current_path:
            return
        if not os.path.isfile(self.ffmpeg_path):
            QMessageBox.warning(self, "缺少 ffmpeg", "网盘资源包/tools/ffmpeg.exe 不存在，无法解码预览。")
            return
        start = self._current_position_seconds() if start_seconds is None else max(0.0, float(start_seconds))
        duration = self._duration()
        if duration > 0 and start >= duration:
            start = max(0.0, duration - 0.05)
        self.seek_preview_timer.stop()
        self._stop_decode_worker(wait=True)
        self._stop_audio_worker(wait=True)
        self.player.pause()
        width, height = self._decode_dimensions()
        worker = FFmpegVideoDecodeWorker(
            self.ffmpeg_path,
            self.current_path,
            start,
            width,
            height,
            self._source_fps(),
            self,
        )
        worker.frameReady.connect(
            lambda image, seconds, active_worker=worker:
            self._on_software_frame_ready(active_worker, image, seconds)
        )
        worker.failed.connect(self._on_software_preview_failed)
        worker.finished.connect(lambda w=worker: self._on_decode_worker_finished(w))
        self.decode_worker = worker
        self.preview_position_seconds = start
        self.preview_position_ms = int(round(start * 1000))
        self._set_preview_play_state(True)
        self._start_audio_preview(start)
        worker.start()

    def _pause_preview(self):
        self._stop_decode_worker(wait=True)
        self._stop_audio_worker(wait=True)
        self.player.pause()
        self._set_preview_play_state(False)

    def _stop_preview(self, reset_position=False):
        self.seek_preview_timer.stop()
        self._stop_decode_worker(wait=True)
        self._stop_audio_worker(wait=True)
        self.player.stop()
        self._set_preview_play_state(False)
        if reset_position:
            self.preview_position_ms = 0
            self.preview_position_seconds = 0.0
            self.timeline.set_position(0)
            self._update_time_label()

    def _on_software_frame_ready(self, worker, image, position_seconds):
        if self.decode_worker is not worker:
            return
        self.preview_area.show_frame(image)
        self._on_position_changed(position_seconds)

    def _on_software_preview_failed(self, message):
        self._set_preview_play_state(False)
        self._log(f"内置解码预览失败：{message}")
        self.preview_area.show_message(f"内置解码预览失败：{message}")

    def _queue_seek_frame(self, seconds):
        self._pending_seek_seconds = max(0.0, float(seconds or 0))
        if not self.seek_preview_timer.isActive():
            self.seek_preview_timer.start(35)

    def _show_pending_seek_frame(self):
        if self._pending_seek_seconds is None or not self.current_path:
            return
        seconds = self._pending_seek_seconds
        self._pending_seek_seconds = None
        try:
            width, height = self._decode_dimensions()
            image = decode_single_frame(self.ffmpeg_path, self.current_path, seconds, width, height)
            self.preview_area.show_frame(image)
        except Exception as exc:
            self._log(f"当前帧预览失败：{exc}")

    def _load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                config = json.load(handle)
        except Exception:
            config = {}
        output_dir = str(config.get("output_dir") or self.output_dir)
        self.output_dir = output_dir
        self.output_edit.setText(output_dir)
        mode = str(config.get("export_mode") or "precise")
        index = self.export_mode_combo.findData(mode)
        if index >= 0:
            self.export_mode_combo.setCurrentIndex(index)

    def _save_config(self):
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        config = {
            "output_dir": self.output_edit.text().strip() or self.output_dir,
            "export_mode": self.export_mode_combo.currentData(),
        }
        try:
            with open(self.config_path, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _start_hardware_detection(self):
        self.hardware_worker = HardwareDetectWorker(self)
        self.hardware_worker.profileReady.connect(self._on_hardware_detected)
        self.hardware_worker.start()

    def _on_hardware_detected(self, profile):
        self.hardware_profile = profile or {"level": "cpu", "summary": "CPU 精准编码"}
        self.hardware_label.setText(
            f"精准导出：{self.hardware_profile.get('summary', 'CPU 编码')}"
        )

    def choose_videos(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "导入需要截取高潮的视频",
            workspace_path("素材"),
            "视频文件 (*.mp4 *.mov *.m4v *.avi *.mkv *.webm *.ts *.mts *.m2ts)",
        )
        self.add_video_paths(paths)

    def add_video_paths(self, paths):
        added = 0
        for path in paths or []:
            path = os.path.abspath(path)
            if not os.path.isfile(path) or not path.lower().endswith(VIDEO_EXTENSIONS):
                continue
            if path in self.entries:
                continue
            self.entries[path] = {"metadata": {}, "segments": []}
            item = QListWidgetItem(os.path.basename(path))
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            self.video_list.addItem(item)
            added += 1
        if added:
            self._refresh_video_list_labels()
            self._log(f"已导入 {added} 个视频。")
            if not self.video_list.currentItem():
                self.video_list.setCurrentRow(0)

    def remove_current_video(self):
        row = self.video_list.currentRow()
        item = self.video_list.item(row)
        if not item:
            return
        path = item.data(Qt.UserRole)
        self._stop_preview(reset_position=True)
        self.video_list.takeItem(row)
        self.entries.pop(path, None)
        self._refresh_video_list_labels()
        self._refresh_segment_table()
        if self.video_list.count() == 0:
            self.current_path = ""
            self.current_file_label.setText("尚未选择视频")
            self.video_info.setText("拖入视频或点击“导入视频”。")
            self.preview_area.show_message("拖入视频或点击导入后显示预览")
            self.timeline.set_media(0)
            self._refresh_segment_table()

    def clear_videos(self):
        if not self.entries:
            return
        answer = QMessageBox.question(self, "清空视频", "确定清空全部视频和未导出的标记吗？")
        if answer != QMessageBox.Yes:
            return
        self._stop_preview(reset_position=True)
        self.entries.clear()
        self.video_list.clear()
        self.current_path = ""
        self.current_file_label.setText("尚未选择视频")
        self.video_info.setText("拖入视频或点击“导入视频”。")
        self.preview_area.show_message("拖入视频或点击导入后显示预览")
        self.timeline.set_media(0)
        self._refresh_segment_table()

    def _refresh_video_list_labels(self):
        total = self.video_list.count()
        for index in range(total):
            item = self.video_list.item(index)
            if not item:
                continue
            path = item.data(Qt.UserRole)
            segment_count = len(self.entries.get(path, {}).get("segments", []))
            prefix = "▶ " if path == self.current_path else "  "
            item.setText(f"{prefix}{index + 1:02d}. {os.path.basename(path)}  ({segment_count}段)")
            item.setToolTip(path)

    def _select_video_path(self, path):
        path = os.path.abspath(path or "")
        for index in range(self.video_list.count()):
            item = self.video_list.item(index)
            if item and os.path.abspath(item.data(Qt.UserRole) or "") == path:
                if self.video_list.currentRow() != index:
                    self.video_list.setCurrentRow(index)
                return True
        return False

    def _on_video_selected(self, current, previous):
        if not current:
            return
        path = current.data(Qt.UserRole)
        if not path or path == self.current_path:
            return
        self._stop_preview(reset_position=True)
        self.loop_range = None
        self._video_output_available = False
        self.current_path = path
        self.current_file_label.setText(os.path.basename(path))
        self._refresh_video_list_labels()
        self.preview_area.show_message("正在加载视频预览...")
        entry = self.entries[path]
        metadata = entry.get("metadata") or {}
        if metadata:
            self._apply_preview_metadata(path, metadata)
        else:
            self.timeline.set_media(0)
            self.timeline.set_segments(entry["segments"])
            self.preview_progress.setValue(5)
            self.preview_progress.setFormat("正在准备预览时间轴...")
            worker = PreviewBuildWorker(
                path,
                self.ffmpeg_path,
                self.ffprobe_path,
                self.cache_dir,
            )
            worker.progress.connect(
                lambda value, text, source=path: self._on_preview_progress(source, value, text)
            )
            worker.finished.connect(self._on_preview_ready)
            worker.failed.connect(self._on_preview_failed)
            worker.finished.connect(lambda *_: self._cleanup_preview_workers())
            worker.failed.connect(lambda *_: self._cleanup_preview_workers())
            self.preview_workers.append(worker)
            registry = getattr(self, "_thread_registry", None)
            if registry is not None:
                registry.register(worker, "快速剪辑预览", worker.cancel)
            worker.start()
        self._refresh_segment_table()

    def _cleanup_preview_workers(self):
        self.preview_workers = [worker for worker in self.preview_workers if worker.isRunning()]

    def _on_preview_progress(self, source, value, text):
        if source == self.current_path:
            self.preview_progress.setValue(value)
            self.preview_progress.setFormat(text)

    def _on_preview_ready(self, source, metadata):
        if source not in self.entries:
            return
        self.entries[source]["metadata"] = metadata
        if source == self.current_path:
            self._apply_preview_metadata(source, metadata)
        self._log(f"预览时间轴准备完成：{os.path.basename(source)}")

    def _on_preview_failed(self, source, message):
        if source == self.current_path:
            self.preview_progress.setValue(0)
            self.preview_progress.setFormat("预览时间轴生成失败")
            self.preview_area.show_message("预览时间轴生成失败，可直接播放或更换视频编码后重试。")
        self._log(f"预览准备失败：{message}")

    def _apply_preview_metadata(self, source, metadata):
        duration = metadata.get("duration") or 0
        self.timeline.set_media(
            duration,
            metadata.get("thumbnails"),
            metadata.get("waveform"),
        )
        self.timeline.set_segments(self.entries[source]["segments"])
        self.preview_area.set_source_size(
            metadata.get("display_width") or metadata.get("width"),
            metadata.get("display_height") or metadata.get("height"),
        )
        thumbnails = metadata.get("thumbnails") or []
        if thumbnails and self.player.state() != QMediaPlayer.PlayingState:
            self.preview_area.set_poster_image(thumbnails[0])
        self.preview_position_ms = 0
        self.preview_position_seconds = 0.0
        self._queue_seek_frame(0)
        self.preview_progress.setValue(100)
        self.preview_progress.setFormat("完整时间轴已就绪")
        size_mb = (metadata.get("size") or 0) / 1024 / 1024
        self.video_info.setText(
            f"时长：{format_time(duration)}\n"
            f"原始分辨率：{metadata.get('width', 0)} × {metadata.get('height', 0)}\n"
            f"帧率：{metadata.get('fps', 0):.2f} fps\n"
            f"视频：{metadata.get('video_codec', '未知')}  音频：{metadata.get('audio_codec', '未知')}\n"
            f"文件大小：{size_mb:.1f} MB"
        )
        ref = self._selected_segment_ref()
        ref_path, _, segment = self._segment_by_ref(ref)
        if ref_path == source and segment:
            self.timeline.set_points(segment["start"], segment["end"])
        self._update_time_label()

    def toggle_playback(self):
        if not self.current_path:
            return
        if self.software_preview_playing or (self.decode_worker and self.decode_worker.isRunning()):
            self._pause_preview()
        else:
            self._start_software_preview(self._current_position_seconds())

    def seek_to_seconds(self, seconds):
        if not self.current_path:
            return
        self.loop_range = None
        seconds = max(0.0, min(self._duration(), float(seconds or 0)))
        self.preview_position_seconds = seconds
        self.preview_position_ms = int(round(seconds * 1000))
        self.timeline.set_position(seconds)
        self._update_time_label()
        if self.software_preview_playing:
            self._start_software_preview(seconds)
        else:
            self._queue_seek_frame(seconds)

    def seek_relative(self, delta):
        self.seek_to_seconds(self._current_position_seconds() + float(delta))

    def step_frame(self, direction):
        self._pause_preview()
        self.seek_to_seconds(
            frame_step_time(
                self._current_position_seconds(),
                self._source_fps(),
                direction,
                self._duration(),
            )
        )

    def _on_position_changed(self, position):
        seconds = max(0.0, float(position or 0.0))
        if isinstance(position, int):
            seconds /= 1000.0
        self.preview_position_seconds = seconds
        self.preview_position_ms = int(round(seconds * 1000))
        self.timeline.set_position(seconds)
        if self.loop_range and seconds >= self.loop_range[1] - 0.02:
            if self.loop_checkbox.isChecked():
                self._start_software_preview(self.loop_range[0])
            else:
                self._pause_preview()
                self.loop_range = None
        self._update_time_label()

    def _on_qt_position_changed(self, milliseconds):
        if self.software_preview_playing or self.decode_worker:
            return
        self._on_position_changed(milliseconds)

    def _on_player_duration(self, milliseconds):
        if not self.current_path or milliseconds <= 0:
            return
        entry = self.entries.get(self.current_path, {})
        metadata = entry.get("metadata") or {}
        if not metadata.get("duration"):
            metadata["duration"] = milliseconds / 1000
            entry["metadata"] = metadata
            self.timeline.duration = milliseconds / 1000
            self.timeline.update()
        self._update_time_label()

    def _on_player_state_changed(self, state):
        if self.software_preview_playing:
            return
        if state == QMediaPlayer.PlayingState:
            self.preview_area.show_video()
            self.play_button.setText("暂停")
            self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        else:
            self.play_button.setText("播放")
            self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def _on_media_status_changed(self, status):
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self._update_time_label()
        elif status == QMediaPlayer.InvalidMedia:
            self.preview_area.show_message("播放器无法加载该视频，可尝试转为 H.264 MP4 后再预览。")

    def _on_video_available_changed(self, available):
        self._video_output_available = bool(available)
        if available and self.player.state() == QMediaPlayer.PlayingState:
            self.preview_area.show_video()

    def _on_player_error(self, error):
        if error != QMediaPlayer.NoError:
            message = self.player.errorString() or str(error)
            self.preview_area.show_message(f"播放器提示：{message}")
            self._log(f"播放器提示：{message}")

    def _duration(self):
        metadata = self.entries.get(self.current_path, {}).get("metadata", {})
        return float(metadata.get("duration") or self.player.duration() / 1000 or 0)

    def _update_time_label(self):
        self.time_label.setText(
            f"{format_time(self._current_position_seconds())} / {format_time(self._duration())}"
        )

    def set_in_point(self):
        if not self.current_path:
            return
        self.timeline.in_point = self._current_position_seconds()
        self.timeline.update()
        self._on_points_changed(self.timeline.in_point, self.timeline.out_point)

    def set_out_point(self):
        if not self.current_path:
            return
        self.timeline.out_point = self._current_position_seconds()
        self.timeline.update()
        self._on_points_changed(self.timeline.in_point, self.timeline.out_point)

    def clear_points(self):
        self.timeline.set_points(None, None)
        self.loop_range = None

    def _on_points_changed(self, start, end):
        if start >= 0 and end >= 0:
            low, high = sorted((start, end))
            self.point_label.setText(
                f"入点：{format_time(low)}    出点：{format_time(high)}    "
                f"时长：{high - low:.3f} 秒"
            )
        else:
            start_text = format_time(start) if start >= 0 else "--"
            end_text = format_time(end) if end >= 0 else "--"
            self.point_label.setText(f"入点：{start_text}    出点：{end_text}    时长：--")

    def _candidate_range(self):
        start = self.timeline.in_point
        end = self.timeline.out_point
        if start < 0 or end < 0:
            return None
        start, end = sorted((start, end))
        if end - start < 0.08:
            return None
        return start, min(end, self._duration())

    def add_segment(self):
        if not self.current_path:
            QMessageBox.warning(self, "未选择视频", "请先导入并选择视频。")
            return
        candidate = self._candidate_range()
        if not candidate:
            QMessageBox.warning(self, "标记不完整", "请先设置入点和出点，且片段时长不能过短。")
            return
        start, end = candidate
        segments = self.entries[self.current_path]["segments"]
        if any(abs(item["start"] - start) < 0.02 and abs(item["end"] - end) < 0.02 for item in segments):
            QMessageBox.information(self, "片段已存在", "这一组入点和出点已经添加过。")
            return
        segment_id = uuid.uuid4().hex
        segments.append({"id": segment_id, "start": start, "end": end})
        segments.sort(key=lambda item: (item["start"], item["end"]))
        self.timeline.set_segments(segments)
        self._refresh_video_list_labels()
        self._refresh_segment_table((self.current_path, segment_id))
        self.clear_points()
        self._log(f"已添加高潮片段：{format_time(start)} - {format_time(end)}")

    def _selected_segment_ref(self):
        rows = self.segment_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.segment_table.item(rows[0].row(), 0)
        data = item.data(Qt.UserRole) if item else None
        if not isinstance(data, dict):
            return None
        return data.get("path"), data.get("id")

    def _segment_by_ref(self, ref):
        if not ref:
            return None, -1, None
        path, segment_id = ref
        segments = self.entries.get(path, {}).get("segments", [])
        for index, segment in enumerate(segments):
            if segment.get("id") == segment_id:
                return path, index, segment
        return path, -1, None

    def _activate_segment_ref(self, ref):
        path, index, segment = self._segment_by_ref(ref)
        if not path or index < 0 or not segment:
            return None
        if path != self.current_path:
            self._pending_segment_selection = ref
            self._select_video_path(path)
        if path == self.current_path:
            self.timeline.set_points(segment["start"], segment["end"])
            self.timeline.set_segments(self.entries[path].get("segments", []))
        return segment

    def _load_selected_segment(self):
        if self._segment_table_refreshing:
            return
        ref = self._selected_segment_ref()
        path, index, segment = self._segment_by_ref(ref)
        if not path or index < 0 or not segment:
            return
        if path != self.current_path:
            QTimer.singleShot(0, lambda r=ref: self._activate_segment_ref(r))
            return
        self.timeline.set_points(segment["start"], segment["end"])

    def update_selected_segment(self):
        ref = self._selected_segment_ref()
        path, index, segment = self._segment_by_ref(ref)
        candidate = self._candidate_range()
        if not path or index < 0 or not segment:
            QMessageBox.information(self, "未选择片段", "请先在右侧选择需要更新的高潮片段。")
            return
        if path != self.current_path:
            QMessageBox.information(self, "请等待预览切换", "请先切换到该片段所属视频，再调整入点和出点。")
            return
        if not candidate:
            QMessageBox.warning(self, "标记不完整", "请设置有效的入点和出点。")
            return
        segments = self.entries[path]["segments"]
        segments[index]["start"], segments[index]["end"] = candidate
        segments.sort(key=lambda item: (item["start"], item["end"]))
        if path == self.current_path:
            self.timeline.set_segments(segments)
        self._refresh_segment_table(ref)

    def delete_selected_segment(self):
        ref = self._selected_segment_ref()
        path, index, segment = self._segment_by_ref(ref)
        if not path or index < 0:
            return
        segments = self.entries[path]["segments"]
        segments.pop(index)
        if path == self.current_path:
            self.timeline.set_segments(segments)
            self.clear_points()
        self._refresh_video_list_labels()
        self._refresh_segment_table()

    def preview_selected_segment(self):
        ref = self._selected_segment_ref()
        path, index, segment = self._segment_by_ref(ref)
        if not path or index < 0 or not segment:
            return
        self._activate_segment_ref(ref)
        self.loop_range = (segment["start"], segment["end"])
        self.preview_position_seconds = float(segment["start"])
        self.preview_position_ms = int(round(self.preview_position_seconds * 1000))
        self._start_software_preview(segment["start"])

    def _refresh_segment_table(self, selected_ref=None):
        selected_ref = selected_ref or self._pending_segment_selection
        self._pending_segment_selection = None
        rows = []
        for path, entry in self.entries.items():
            for number, segment in enumerate(entry.get("segments", []), 1):
                rows.append((path, number, segment))

        self._segment_table_refreshing = True
        self.segment_table.setRowCount(len(rows))
        selected_row = -1
        try:
            for row, (path, number, segment) in enumerate(rows):
                values = [
                    os.path.basename(path),
                    str(number),
                    format_time(segment["start"]),
                    format_time(segment["end"]),
                    f"{segment['end'] - segment['start']:.3f}s",
                ]
                ref = {"path": path, "id": segment.get("id")}
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(path if column == 0 else value)
                    item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter if column == 0 else Qt.AlignCenter)
                    if column == 0:
                        item.setData(Qt.UserRole, ref)
                    self.segment_table.setItem(row, column, item)
                if selected_ref and path == selected_ref[0] and segment.get("id") == selected_ref[1]:
                    selected_row = row
        finally:
            self._segment_table_refreshing = False

        if selected_row >= 0:
            self.segment_table.selectRow(selected_row)
        if hasattr(self, "export_status"):
            self.export_status.setText(
                f"右侧共 {len(rows)} 个高潮片段；可一次性导出全部。"
                if rows else
                "每个入点+出点区间会单独生成一个视频，不会自动合并。"
            )

    def choose_output_dir(self):
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择高潮片段输出目录",
            self.output_edit.text().strip() or self.output_dir,
        )
        if selected:
            self.output_edit.setText(selected)
            self._save_config()

    def _export_tasks(self, all_videos):
        paths = list(self.entries) if all_videos else [self.current_path]
        tasks = []
        for path in paths:
            if not path or path not in self.entries:
                continue
            for number, segment in enumerate(self.entries[path]["segments"], 1):
                tasks.append({
                    "source": path,
                    "start": segment["start"],
                    "end": segment["end"],
                    "number": number,
                })
        return tasks

    def start_export(self, all_videos):
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "正在导出", "当前导出任务尚未结束。")
            return
        tasks = self._export_tasks(all_videos)
        if not tasks:
            QMessageBox.warning(self, "没有高潮片段", "请先用入点和出点添加至少一个高潮片段。")
            return
        if not os.path.isfile(self.ffmpeg_path):
            QMessageBox.warning(self, "缺少 ffmpeg", "网盘资源包/tools/ffmpeg.exe 不存在。")
            return
        output_dir = self.output_edit.text().strip() or self.output_dir
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as exc:
            QMessageBox.warning(self, "输出目录不可用", str(exc))
            return
        self._save_config()
        self._pause_preview()
        mode = self.export_mode_combo.currentData()
        profile = getattr(self, "hardware_profile", {"level": "cpu"})
        self.export_worker = ClipExportWorker(
            tasks,
            self.ffmpeg_path,
            output_dir,
            mode,
            profile.get("level", "cpu"),
        )
        self._export_total = len(tasks)
        self._export_stop_requested = False
        self.export_worker.progress.connect(
            lambda value, current, total: self.export_progress.setValue(value)
        )
        self.export_worker.message.connect(self._on_export_message)
        self.export_worker.file_finished.connect(
            lambda path: self._log(f"已导出：{path}")
        )
        self.export_worker.finished.connect(self._on_export_finished)
        self.export_worker.failed.connect(self._on_export_failed)
        registry = getattr(self, "_thread_registry", None)
        if registry is not None:
            registry.register(
                self.export_worker,
                "快速剪辑导出",
                self.export_worker.cancel,
            )
        self.export_current_button.setEnabled(False)
        self.export_all_button.setEnabled(False)
        self.stop_export_button.setEnabled(True)
        self.export_progress.setValue(0)
        self.export_status.setText(f"准备导出 {len(tasks)} 个独立高潮视频...")
        self.export_worker.start()

    def _on_export_message(self, message):
        self.export_status.setText(message)
        self._log(message)

    def _on_export_finished(self, success, failed, output_dir):
        self.export_current_button.setEnabled(True)
        self.export_all_button.setEnabled(True)
        self.stop_export_button.setEnabled(False)
        stopped = bool(getattr(self, '_export_stop_requested', False))
        if success and not failed and not stopped:
            self.export_progress.setValue(100)
        state = "导出已停止" if stopped else "导出结束"
        remaining = max(0, getattr(self, '_export_total', success + failed) - success - failed)
        self.export_status.setText(f"{state}：成功 {success} 个，失败 {failed} 个，未完成 {remaining} 个。")
        self._log(f"{state}：成功 {success} 个，失败 {failed} 个，未完成 {remaining} 个，目录：{output_dir}")
        QMessageBox.information(
            self,
            state,
            f"{state}。\n成功：{success}\n失败：{failed}\n未完成：{remaining}\n目录：{output_dir}",
        )

    def _on_export_failed(self, message):
        self.export_current_button.setEnabled(True)
        self.export_all_button.setEnabled(True)
        self.stop_export_button.setEnabled(False)
        self.export_status.setText("导出失败，详细原因已写入日志。")
        self._log(message)
        QMessageBox.warning(self, "导出失败", message)

    def stop_export(self):
        if self.export_worker and self.export_worker.isRunning():
            self._export_stop_requested = True
            self.export_status.setText("正在停止导出...")
            self.export_worker.cancel()

    def open_output_dir(self):
        path = self.output_edit.text().strip() or self.output_dir
        os.makedirs(path, exist_ok=True)
        open_path(path)

    def prepare_app_close(self):
        if getattr(self, "_keyboard_filter_installed", False):
            app = QApplication.instance()
            if app:
                app.removeEventFilter(self)
            self._keyboard_filter_installed = False
        self._stop_preview(reset_position=False)
        for worker in self.preview_workers:
            if worker.isRunning():
                worker.cancel()
        for worker in self.preview_workers:
            if worker.isRunning():
                worker.wait(5000)
        if self.export_worker and self.export_worker.isRunning():
            self.export_worker.cancel()
            self.export_worker.wait(10000)
        if self.hardware_worker and self.hardware_worker.isRunning():
            self.hardware_worker.wait(10000)
        self._save_config()
