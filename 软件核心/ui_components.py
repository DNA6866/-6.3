import os
import shutil
import subprocess
import time
import urllib.request

from PyQt5.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    Qt,
    QThread,
    pyqtSignal,
)
from PyQt5.QtGui import QColor, QBrush, QKeySequence, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QLabel,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from utils import get_system_fonts
from services.platform_service import resolve_network_path


WATERMARK_PREVIEW_FONT_SCALE = 1.16


def _forward_parameter_wheel(control, event):
    """Scroll the nearest page instead of changing the hovered parameter."""
    parent = control.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            QApplication.sendEvent(parent.viewport(), event)
            event.accept()
            return
        parent = parent.parentWidget()
    event.ignore()


class _ParameterWheelGuard(QObject):
    """Protect current and future parameter controls from accidental wheel edits."""

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Wheel:
            return False

        control = watched if isinstance(watched, (QAbstractSpinBox, QComboBox)) else None
        if control is None:
            try:
                parent = watched.parentWidget()
            except Exception:
                parent = None
            if isinstance(parent, (QAbstractSpinBox, QComboBox)):
                control = parent

        if control is None:
            return False
        _forward_parameter_wheel(control, event)
        return True


def _install_parameter_wheel_guard():
    app = QApplication.instance()
    if app is None or getattr(app, "_parameter_wheel_guard", None) is not None:
        return
    guard = _ParameterWheelGuard(app)
    app.installEventFilter(guard)
    app._parameter_wheel_guard = guard


def configure_spinbox_for_direct_input(widget):
    """Allow direct typing while preventing wheel-driven parameter changes."""
    if widget is None:
        return
    _install_parameter_wheel_guard()
    if isinstance(widget, QAbstractSpinBox):
        spinboxes = [widget]
    else:
        try:
            spinboxes = widget.findChildren(QAbstractSpinBox)
        except Exception:
            spinboxes = []

    for spinbox in spinboxes:
        try:
            spinbox.setReadOnly(False)
            spinbox.setFocusPolicy(Qt.StrongFocus)
            spinbox.setCorrectionMode(QAbstractSpinBox.CorrectToNearestValue)
            spinbox.setAccelerated(True)
            spinbox.wheelEvent = (
                lambda event, control=spinbox: _forward_parameter_wheel(control, event)
            )
            if spinbox.minimumWidth() < 82:
                spinbox.setMinimumWidth(82)
            line_edit = spinbox.lineEdit()
            if line_edit:
                line_edit.setReadOnly(False)
                line_edit.setFocusPolicy(Qt.StrongFocus)
                line_edit.setCursor(Qt.IBeamCursor)
        except Exception:
            continue

    if isinstance(widget, QComboBox):
        comboboxes = [widget]
    else:
        try:
            comboboxes = widget.findChildren(QComboBox)
        except Exception:
            comboboxes = []

    for combo in comboboxes:
        try:
            combo.setFocusPolicy(Qt.StrongFocus)
            combo.wheelEvent = (
                lambda event, control=combo: _forward_parameter_wheel(control, event)
            )
        except Exception:
            continue


class DragPreviewCanvas(QWidget):
    pointChanged = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self.design_w = 1080
        self.design_h = 1920
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    def set_design_size(self, width, height):
        self.design_w = max(1, int(width))
        self.design_h = max(1, int(height))

    def _emit_point(self, event):
        if self.width() <= 0 or self.height() <= 0:
            return
        x = round(event.x() / self.width() * self.design_w)
        y = round(event.y() / self.height() * self.design_h)
        x = max(0, min(self.design_w, x))
        y = max(0, min(self.design_h, y))
        self.pointChanged.emit(x, y)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._emit_point(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._emit_point(event)
        super().mouseMoveEvent(event)


class PlainPasteTextEdit(QPlainTextEdit):
    def insertFromMimeData(self, source):
        if source and source.hasText():
            self.insertPlainText(source.text())
            return
        super().insertFromMimeData(source)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Paste):
            self.paste()
            event.accept()
            return
        super().keyPressEvent(event)


RECOMMENDED_CHINESE_FONTS = [
    {
        "name": "推荐字体: 霞鹜文楷",
        "file": "LXGWWenKai-Regular.ttf",
        "family_hint": "LXGW WenKai",
        "min_bytes": 1024 * 1024,
        "urls": [
            "https://github.com/lxgw/LxgwWenKai/releases/latest/download/LXGWWenKai-Regular.ttf",
        ],
    },
    {
        "name": "推荐字体: 思源黑体",
        "file": "NotoSansCJKsc-Regular.otf",
        "family_hint": "Noto Sans CJK SC",
        "min_bytes": 1024 * 1024,
        "urls": [
            "https://github.com/notofonts/noto-cjk/raw/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
        ],
    },
]


class FontDownloadWorker(QThread):
    message_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(object, object)

    def __init__(self, font_dir, font_defs):
        super().__init__()
        self.font_dir = font_dir
        self.font_defs = font_defs

    def run(self):
        ready = []
        failed = []
        os.makedirs(self.font_dir, exist_ok=True)
        for font_def in self.font_defs:
            name = font_def["name"]
            target_path = os.path.join(self.font_dir, font_def["file"])
            min_bytes = int(font_def.get("min_bytes", 1) or 1)
            if os.path.exists(target_path) and os.path.getsize(target_path) >= min_bytes:
                ready.append(target_path)
                continue

            ok = False
            for url in font_def.get("urls", []):
                temp_path = target_path + ".download"
                try:
                    self.message_signal.emit(f"[字体] 正在补齐 {name}...")
                    request = urllib.request.Request(url, headers={"User-Agent": "NiuYeYeVideoToolbox/1.0"})
                    with urllib.request.urlopen(request, timeout=45) as response, open(temp_path, "wb") as f:
                        shutil.copyfileobj(response, f)
                    if os.path.getsize(temp_path) < min_bytes:
                        raise RuntimeError("字体文件下载不完整")
                    os.replace(temp_path, target_path)
                    ready.append(target_path)
                    ok = True
                    break
                except Exception as exc:
                    failed.append(f"{name}: {exc}")
                    try:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                    except Exception:
                        pass
            if not ok and not os.path.exists(target_path):
                self.message_signal.emit(f"[字体] {name} 暂时未能自动补齐，继续使用本机中文字体。")
        self.finished_signal.emit(ready, failed)


class FileDropArea(QFrame):
    filesDropped = pyqtSignal(list)
    clicked = pyqtSignal()

    def __init__(self, title="拖拽文件到这里导入"):
        super().__init__()
        self.setObjectName("dropArea")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        label = QLabel(title)
        label.setAlignment(Qt.AlignCenter)
        label.setObjectName("dropAreaText")
        layout.addWidget(label)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        files = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                files.append(path)
        if files:
            self.filesDropped.emit(files)
        event.acceptProposedAction()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class AudioRangeSelector(QWidget):
    """音频波形选择器：支持左右边界拖拽、整体拖动、鼠标滚轮缩放。"""

    rangeChanged = pyqtSignal(float, float)
    HANDLE_WIDTH = 6

    def __init__(self):
        super().__init__()
        self.duration = 10.0
        self.start = 0.0
        self.end = 3.0
        self.peaks = []
        self.view_start = 0.0
        self.view_end = 10.0
        self._drag_mode = None
        self._drag_start_x = 0
        self._drag_body_start = 0.0
        self._drag_body_end = 0.0
        self._drag_anchor_time = 0.0
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def set_duration(self, duration):
        self.duration = max(0.1, float(duration))
        self.start = 0.0
        self.end = min(3.0, self.duration)
        self.view_start = 0.0
        self.view_end = self.duration
        self.update()
        self.rangeChanged.emit(self.start, self.end)

    def selected_range(self):
        return self.start, self.end

    def set_waveform(self, peaks):
        self.peaks = peaks or []
        self.update()

    def _track_rect(self):
        margin = 18
        track_h = max(60, self.height() - 56)
        top = max(8, (self.height() - track_h) // 2)
        return margin, top, max(1, self.width() - margin * 2), track_h

    def _visible_span(self):
        return max(0.001, self.view_end - self.view_start)

    def _x_from_time(self, value):
        x0, _, width, _ = self._track_rect()
        ratio = (value - self.view_start) / self._visible_span()
        return x0 + int(max(0.0, min(1.0, ratio)) * width)

    def _time_from_x(self, x):
        x0, _, width, _ = self._track_rect()
        ratio = max(0.0, min(1.0, (x - x0) / width))
        return self.view_start + ratio * self._visible_span()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        x0, top, width, height = self._track_rect()
        mid = top + height // 2

        painter.setPen(QPen(QColor("#334155"), 1))
        painter.setBrush(QBrush(QColor("#020617")))
        painter.drawRoundedRect(x0, top, width, height, 8, 8)

        if self.peaks:
            bar_count = min(width, len(self.peaks))
            step = len(self.peaks) / max(1, bar_count)
            painter.setPen(QPen(QColor("#60A5FA"), 1))
            for i in range(bar_count):
                idx = int(i * step)
                if idx >= len(self.peaks):
                    break
                peak = self.peaks[idx]
                bar_h = max(2, int(peak * (height - 16)))
                x = x0 + i
                painter.drawLine(x, mid - bar_h // 2, x, mid + bar_h // 2)
        else:
            painter.setPen(QPen(QColor("#475569"), 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(x0 + 8, mid, x0 + width - 8, mid)

        if self.view_start > 0.001 or self.view_end < self.duration - 0.001:
            vx0 = self._x_from_time(self.view_start)
            vx1 = self._x_from_time(self.view_end)
            painter.setPen(QPen(QColor("#475569"), 2))
            painter.drawLine(x0, top - 3, x0 + width, top - 3)
            painter.setPen(QPen(QColor("#60A5FA"), 2))
            painter.drawLine(vx0, top - 3, vx1, top - 3)

        sx = self._x_from_time(self.start)
        ex = self._x_from_time(self.end)
        painter.setPen(QPen(QColor("#F8FAFC"), 2))
        painter.setBrush(QBrush(QColor(37, 99, 235, 70)))
        painter.drawRect(sx, top + 4, max(4, ex - sx), height - 8)

        painter.setPen(QPen(QColor("#FFFFFF"), 2))
        painter.setBrush(QBrush(QColor("#2563EB")))
        hw = self.HANDLE_WIDTH // 2
        painter.drawRoundedRect(sx - hw, top, self.HANDLE_WIDTH, height, 3, 3)
        painter.drawRoundedRect(ex - hw, top, self.HANDLE_WIDTH, height, 3, 3)

        painter.setPen(QColor("#E2E8F0"))
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        start_label = self._fmt(self.start)
        end_label = self._fmt(self.end)
        dur_label = self._fmt(self.duration)
        painter.drawText(x0, top + height + 20, start_label)
        painter.drawText(ex - painter.fontMetrics().horizontalAdvance(end_label) - 4, top + height + 20, end_label)
        painter.drawText(x0 + width - painter.fontMetrics().horizontalAdvance(dur_label), top - 6, dur_label)

        if self.view_end - self.view_start < self.duration * 0.99:
            zoom_pct = int(100 * self.duration / self._visible_span())
            z_label = f"缩放 {zoom_pct}%"
            tw = painter.fontMetrics().horizontalAdvance(z_label)
            painter.setPen(QColor("#64748B"))
            painter.drawText(x0 + width - tw, top + height + 40, z_label)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        sx = self._x_from_time(self.start)
        ex = self._x_from_time(self.end)
        handle_range = self.HANDLE_WIDTH + 2

        if abs(event.x() - sx) <= handle_range:
            self._drag_mode = "start"
            self.setCursor(Qt.SizeHorCursor)
        elif abs(event.x() - ex) <= handle_range:
            self._drag_mode = "end"
            self.setCursor(Qt.SizeHorCursor)
        elif sx + handle_range < event.x() < ex - handle_range:
            self._drag_mode = "body"
            self._drag_start_x = event.x()
            self._drag_body_start = self.start
            self._drag_body_end = self.end
            self.setCursor(Qt.ClosedHandCursor)
        else:
            self._drag_mode = "start" if event.x() < sx else "end"
            self.setCursor(Qt.SizeHorCursor)
        self._drag_anchor_time = self._time_from_x(event.x())

    def mouseMoveEvent(self, event):
        if not self._drag_mode:
            sx = self._x_from_time(self.start)
            ex = self._x_from_time(self.end)
            handle_range = self.HANDLE_WIDTH + 2
            if abs(event.x() - sx) <= handle_range or abs(event.x() - ex) <= handle_range:
                self.setCursor(Qt.SizeHorCursor)
            elif sx + handle_range < event.x() < ex - handle_range:
                self.setCursor(Qt.OpenHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return

        if self._drag_mode == "body":
            current_time = self._time_from_x(event.x())
            delta = current_time - self._drag_anchor_time
            seg_len = self._drag_body_end - self._drag_body_start
            new_start = self._drag_body_start + delta
            new_end = self._drag_body_end + delta
            if new_start < 0.0:
                new_start = 0.0
                new_end = seg_len
            if new_end > self.duration:
                new_end = self.duration
                new_start = self.duration - seg_len
            self.start = max(0.0, new_start)
            self.end = min(self.duration, new_end)
        else:
            self._move_handle(event.x())

        self.update()
        self.rangeChanged.emit(self.start, self.end)

    def mouseReleaseEvent(self, event):
        self._drag_mode = None
        self.setCursor(Qt.ArrowCursor)

    def _move_handle(self, x):
        value = self._time_from_x(x)
        if self._drag_mode == "start":
            self.start = max(self.view_start, min(value, self.end - 0.05))
        elif self._drag_mode == "end":
            self.end = min(self.view_end, max(value, self.start + 0.05))

    def wheelEvent(self, event):
        cursor_x = event.pos().x()
        cursor_time = self._time_from_x(cursor_x)
        span = self._visible_span()
        factor = 1.2 if event.angleDelta().y() > 0 else 1.0 / 1.2
        new_span = max(0.3, min(self.duration * 2, span / factor))
        ratio = (cursor_time - self.view_start) / span if span > 0.001 else 0.5
        self.view_start = cursor_time - ratio * new_span
        self.view_end = cursor_time + (1.0 - ratio) * new_span
        if self.view_start < 0.0:
            self.view_end -= self.view_start
            self.view_start = 0.0
        if self.view_end > self.duration:
            self.view_start -= self.view_end - self.duration
            self.view_end = self.duration
        self.view_start = max(0.0, self.view_start)
        self.view_end = max(self.view_start + 0.3, min(self.duration, self.view_end))
        self.update()

    def _fmt(self, sec):
        m = int(sec // 60)
        s = sec - m * 60
        return f"{m:02d}:{s:05.2f}"


class FontScannerWorker(QThread):
    """在后台线程扫描系统字体，完成后信号通知主线程更新 combobox。"""

    finished = pyqtSignal(dict)

    def __init__(self, font_dir):
        super().__init__()
        self.font_dir = font_dir

    def run(self):
        try:
            raw_fonts = get_system_fonts()
            self.finished.emit(raw_fonts)
        except Exception:
            self.finished.emit({})


class DirectoryScanWorker(QThread):
    """Scan one material directory off the UI thread."""

    scan_finished = pyqtSignal(str, str, int, object)
    scan_failed = pyqtSignal(str, str, int, str)

    def __init__(self, tab_name, folder_path, allowed_exts, request_id):
        super().__init__()
        self.tab_name = str(tab_name)
        self.folder_path = os.path.abspath(folder_path)
        self.scan_path = resolve_network_path(self.folder_path)
        self.allowed_exts = tuple(str(ext).lower() for ext in (allowed_exts or []))
        self.request_id = int(request_id)

    def run(self):
        try:
            is_network = self.scan_path.startswith("\\\\")
            if not is_network:
                os.makedirs(self.scan_path, exist_ok=True)

            attempts = 4 if is_network else 1
            last_error = ""
            for attempt in range(attempts):
                if self.isInterruptionRequested():
                    return
                if not os.path.isdir(self.scan_path):
                    last_error = f"目录暂时不可访问：{self.scan_path}"
                    if attempt + 1 < attempts:
                        time.sleep(1.5)
                        continue
                    raise OSError(last_error)

                files = []
                walk_errors = []

                def on_walk_error(exc):
                    walk_errors.append(str(exc))

                for root, _, names in os.walk(self.scan_path, onerror=on_walk_error):
                    if self.isInterruptionRequested():
                        return
                    for filename in names:
                        if self.isInterruptionRequested():
                            return
                        if self.allowed_exts and not filename.lower().endswith(self.allowed_exts):
                            continue
                        full_path = os.path.abspath(os.path.join(root, filename))
                        if not os.path.isfile(full_path):
                            continue
                        files.append((os.path.relpath(full_path, self.scan_path), full_path))

                if walk_errors and not files and attempt + 1 < attempts:
                    last_error = walk_errors[0]
                    time.sleep(1.5)
                    continue
                if walk_errors and not files:
                    raise OSError(walk_errors[0])

                files.sort(key=lambda item: item[0].lower())
                self.scan_finished.emit(
                    self.tab_name,
                    self.folder_path,
                    self.request_id,
                    files,
                )
                return

            raise OSError(last_error or f"目录读取失败：{self.scan_path}")
        except Exception as exc:
            self.scan_failed.emit(
                self.tab_name,
                self.folder_path,
                self.request_id,
                str(exc),
            )


class ReferenceFrameWorker(QThread):
    """在后台线程提取视频参考帧，返回 (watermark_path, subtitle_path)。"""

    finished = pyqtSignal(str, str)
    error = pyqtSignal(str)

    def __init__(self, ffmpeg_path, source_video, watermark_out, subtitle_out):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.source = source_video
        self.watermark_out = watermark_out
        self.subtitle_out = subtitle_out

    def run(self):
        try:
            os.makedirs(os.path.dirname(self.watermark_out), exist_ok=True)
            watermark_ok = self._extract_one(self.watermark_out)
            subtitle_ok = self._extract_one(self.subtitle_out)
            if not watermark_ok and not subtitle_ok:
                self.error.emit("ffmpeg 提取参考帧失败")
                return
            self.finished.emit(
                self.watermark_out if watermark_ok else "",
                self.subtitle_out if subtitle_ok else "",
            )
        except Exception as exc:
            self.error.emit(str(exc))

    def _extract_one(self, output_path):
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "1",
                "-i",
                self.source,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                output_path,
            ]
            result = subprocess.run(cmd, startupinfo=si, capture_output=True, timeout=15)
            if result.returncode != 0 or not os.path.exists(output_path):
                cmd2 = [
                    self.ffmpeg_path,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    self.source,
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    output_path,
                ]
                subprocess.run(cmd2, startupinfo=si, capture_output=True, timeout=30, check=True)
            return os.path.exists(output_path) and os.path.getsize(output_path) > 256
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False


class SplashScreen(QWidget):
    def __init__(self, image_path):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QVBoxLayout()
        self.setLayout(layout)

        self.image_label = QLabel()
        pixmap = QPixmap(image_path)
        self.image_label.setPixmap(pixmap.scaled(600, 600, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(self.image_label)

        glow_effect = QGraphicsDropShadowEffect(self)
        glow_effect.setOffset(0, 0)
        glow_effect.setBlurRadius(60)
        glow_effect.setColor(QColor(255, 0, 0, 180))
        self.image_label.setGraphicsEffect(glow_effect)

        self.setWindowOpacity(0.0)
        self.animation = QPropertyAnimation(self, b"windowOpacity")
        self.animation.setDuration(1500)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.InOutQuad)
        self.animation.start()

    def close_splash(self):
        self.fade_out = QPropertyAnimation(self, b"windowOpacity")
        self.fade_out.setDuration(800)
        self.fade_out.setStartValue(1.0)
        self.fade_out.setEndValue(0.0)
        self.fade_out.finished.connect(self.close)
        self.fade_out.start()
