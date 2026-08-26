import bisect
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from PyQt5.QtCore import QEvent, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPixmap
from PyQt5.QtMultimedia import QAudioFormat, QAudioOutput
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
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
    QShortcut,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from paths import tools_dir, workspace_path
from services.cache_service import touch_cache_entry
from services.platform_service import is_remote_path, open_path
from ui_components import configure_spinbox_for_direct_input
from utils import TRANS_MAP

from modules.quick_video_trim.quick_video_trim_engine import (
    PreviewBuildWorker,
    format_time,
    hidden_startupinfo,
)
from modules.quick_video_trim.quick_video_trim_widget import (
    FFmpegAudioDecodeWorker,
    FFmpegVideoDecodeWorker,
    HighlightTimeline,
    VideoPreviewArea,
    decode_single_frame,
    frame_step_time,
)

from .mixer_v2_engine import ReplacementRenderWorker
from .replacement_project import (
    ReplacementProjectStore,
    VIDEO_EXTENSIONS,
    scan_video_files,
    source_signature,
)
from .smart_split import SceneDetectWorker


LEGACY_TRANSITION_MODES = {
    "fade_black": "fadeblack",
    "fade_white": "fadewhite",
    "fade_random": "random",
}


@dataclass
class RenderQueueTask:
    queue_id: str
    project_path: str
    project_id: str
    project_name: str
    loops: int
    encoder: str
    replacement_text: str
    state: str = "queued"
    message: str = ""


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class ProjectDropList(QListWidget):
    filesDropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setStyleSheet(
            """
            QListWidget {
                background:#0B1120;
                border:1px solid #334155;
                border-radius:7px;
                color:#CBD5E1;
                outline:none;
            }
            QListWidget::item {
                min-height:38px;
                padding:8px;
                border-bottom:1px solid #1E293B;
            }
            QListWidget::item:selected {
                background:#2563EB;
                color:#FFFFFF;
                border-left:5px solid #FDE047;
                font-weight:700;
            }
            """
        )

    @staticmethod
    def _video_paths(urls):
        paths = []
        seen = set()
        for url in urls or []:
            raw_path = url.toLocalFile()
            if not raw_path:
                continue
            path = os.path.abspath(raw_path)
            candidates = [path] if os.path.isfile(path) else scan_video_files(path)
            for candidate in candidates:
                key = os.path.normcase(candidate)
                if (
                    key not in seen
                    and os.path.isfile(candidate)
                    and candidate.lower().endswith(VIDEO_EXTENSIONS)
                ):
                    seen.add(key)
                    paths.append(candidate)
        return paths

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
        paths = self._video_paths(event.mimeData().urls())
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class FrameTimelineWorker(QThread):
    ready = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)

    CACHE_VERSION = 1

    def __init__(self, ffprobe_path, source, fingerprint, cache_path, parent=None):
        super().__init__(parent)
        self.ffprobe_path = os.path.abspath(ffprobe_path)
        self.source = os.path.abspath(source)
        self.fingerprint = str(fingerprint or "")
        self.cache_path = os.path.abspath(cache_path)
        self._process = None

    def cancel(self):
        self.requestInterruption()
        process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

    def _load_cache(self):
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if int(payload.get("version", 0) or 0) != self.CACHE_VERSION:
                return []
            if str(payload.get("fingerprint") or "") != self.fingerprint:
                return []
            return [float(value) for value in payload.get("timestamps", [])]
        except Exception:
            return []

    def _save_cache(self, timestamps):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        temp_path = f"{self.cache_path}.{os.getpid()}.tmp"
        payload = {
            "version": self.CACHE_VERSION,
            "fingerprint": self.fingerprint,
            "timestamps": timestamps,
        }
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_path, self.cache_path)

    @staticmethod
    def _parse_timestamps(output):
        values = []
        for line in str(output or "").splitlines():
            try:
                value = float(line.strip())
            except (TypeError, ValueError):
                continue
            if value >= 0:
                values.append(value)
        values = sorted(set(values))
        if len(values) < 2:
            return []
        origin = values[0]
        timestamps = []
        for value in values:
            normalized = max(0.0, value - origin)
            if not timestamps or normalized - timestamps[-1] > 0.0000001:
                timestamps.append(round(normalized, 9))
        return timestamps

    def run(self):
        try:
            timestamps = self._load_cache()
            if len(timestamps) >= 2:
                if not self.isInterruptionRequested():
                    self.ready.emit(self.source, timestamps)
                return
            command = [
                self.ffprobe_path,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_packets",
                "-show_entries",
                "packet=pts_time",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "--",
                self.source,
            ]
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=hidden_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                output, _ = self._process.communicate(timeout=45)
            except subprocess.TimeoutExpired as exc:
                self._process.kill()
                self._process.communicate(timeout=5)
                raise RuntimeError("建立逐帧索引超时") from exc
            return_code = int(self._process.returncode or 0)
            self._process = None
            if self.isInterruptionRequested():
                return
            if return_code != 0:
                raise RuntimeError((output or "ffprobe 无法读取帧时间").strip())
            timestamps = self._parse_timestamps(output)
            if len(timestamps) < 2:
                raise RuntimeError("没有读取到有效的视频帧时间")
            self._save_cache(timestamps)
            if not self.isInterruptionRequested():
                self.ready.emit(self.source, timestamps)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(self.source, str(exc))
        finally:
            self._process = None


class RemotePreviewCacheWorker(QThread):
    progress = pyqtSignal(int, str)
    ready = pyqtSignal(str, str, bool)
    failed = pyqtSignal(str, str)

    def __init__(self, source, target, expected_size=0, parent=None):
        super().__init__(parent)
        self.source = os.path.abspath(source)
        self.target = os.path.abspath(target)
        self.expected_size = max(0, int(expected_size or 0))
        self._process = None

    def cancel(self):
        self.requestInterruption()
        process = self._process
        if not process or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    startupinfo=hidden_startupinfo(),
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )
            else:
                process.terminate()
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def _target_ready(self):
        try:
            size = os.path.getsize(self.target)
        except OSError:
            return False
        return size > 0 and (not self.expected_size or size == self.expected_size)

    def run(self):
        stage_dir = ""
        try:
            if self._target_ready():
                touch_cache_entry(self.target)
                self.ready.emit(self.source, self.target, True)
                return

            target_dir = os.path.dirname(self.target)
            os.makedirs(target_dir, exist_ok=True)
            required = self.expected_size or os.path.getsize(self.source)
            if shutil.disk_usage(target_dir).free < required + 512 * 1024**2:
                raise RuntimeError("本机空间不足，无法建立 NAS 预览缓存")

            self.progress.emit(5, "正在把网络原片复制到本机预览缓存...")
            stage_dir = os.path.join(
                target_dir,
                f".copy_{os.getpid()}_{time.time_ns()}",
            )
            os.makedirs(stage_dir, exist_ok=True)
            source_dir = os.path.dirname(self.source)
            source_name = os.path.basename(self.source)
            staged_file = os.path.join(stage_dir, source_name)
            if os.name == "nt":
                command = [
                    "robocopy",
                    source_dir,
                    stage_dir,
                    source_name,
                    "/J",
                    "/R:2",
                    "/W:2",
                    "/NFL",
                    "/NDL",
                    "/NJH",
                    "/NJS",
                    "/NP",
                ]
                self._process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    startupinfo=hidden_startupinfo(),
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                started = time.monotonic()
                while self._process.poll() is None:
                    if self.isInterruptionRequested():
                        self.cancel()
                        return
                    if time.monotonic() - started > 600:
                        self.cancel()
                        raise RuntimeError("复制 NAS 原片超时")
                    self.msleep(100)
                if int(self._process.returncode or 0) > 7:
                    raise RuntimeError(
                        f"复制 NAS 原片失败，robocopy 错误码 {self._process.returncode}"
                    )
            else:
                shutil.copyfile(self.source, staged_file)

            if self.isInterruptionRequested():
                return
            if not os.path.isfile(staged_file):
                raise RuntimeError("NAS 预览缓存没有生成完整文件")
            copied_size = os.path.getsize(staged_file)
            if copied_size <= 0 or (self.expected_size and copied_size != self.expected_size):
                raise RuntimeError("NAS 预览缓存大小校验失败")
            os.replace(staged_file, self.target)
            touch_cache_entry(self.target)
            self.progress.emit(100, "网络原片已缓存到本机，逐帧预览已加速。")
            self.ready.emit(self.source, self.target, False)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(self.source, str(exc))
        finally:
            self._process = None
            if stage_dir:
                target_dir = os.path.realpath(os.path.dirname(self.target))
                stage_real = os.path.realpath(stage_dir)
                try:
                    inside_target = (
                        os.path.commonpath((target_dir, stage_real)) == target_dir
                    )
                except ValueError:
                    inside_target = False
                if inside_target and os.path.basename(stage_real).startswith(".copy_"):
                    shutil.rmtree(stage_real, ignore_errors=True)


class SingleFrameDecodeWorker(QThread):
    ready = pyqtSignal(object, float, float)
    failed = pyqtSignal(float, str)

    def __init__(
        self,
        ffmpeg_path,
        source,
        seconds,
        width,
        height,
        timeout=12,
        parent=None,
    ):
        super().__init__(parent)
        self.ffmpeg_path = os.path.abspath(ffmpeg_path)
        self.source = os.path.abspath(source)
        self.seconds = max(0.0, float(seconds or 0.0))
        self.width = max(2, int(width or 2))
        self.height = max(2, int(height or 2))
        self.timeout = max(2, int(timeout or 12))
        self._process = None

    def cancel(self):
        self.requestInterruption()
        process = self._process
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            pass

    def run(self):
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{self.seconds:.6f}",
            "-i",
            self.source,
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-vf",
            f"scale={self.width}:{self.height}",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        started = time.monotonic()
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(),
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
            try:
                stdout, stderr = self._process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                self.cancel()
                self._process.communicate(timeout=3)
                raise RuntimeError("读取当前帧超时") from exc
            if self.isInterruptionRequested():
                return
            frame_size = self.width * self.height * 3
            if self._process.returncode != 0 or len(stdout) < frame_size:
                detail = (stderr or b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                raise RuntimeError(detail or "无法解码当前画面")
            image = QImage(
                stdout[:frame_size],
                self.width,
                self.height,
                self.width * 3,
                QImage.Format_RGB888,
            ).copy()
            self.ready.emit(image, self.seconds, time.monotonic() - started)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(self.seconds, str(exc))
        finally:
            self._process = None


class MixerV2Widget(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.base_dir = workspace_path("混剪实验室V2")
        self.store = ReplacementProjectStore(self.base_dir)
        self.ffmpeg_path = os.path.join(tools_dir(), "ffmpeg.exe")
        self.ffprobe_path = os.path.join(tools_dir(), "ffprobe.exe")
        self.current_project = None
        self.preview_metadata = {}
        self.preview_worker = None
        self.frame_timeline_worker = None
        self.remote_cache_worker = None
        self.seek_frame_worker = None
        self.preview_source_path = ""
        self.frame_timestamps = []
        self.preview_frame_index = None
        self._frame_index_wait_logged = False
        self.decode_worker = None
        self.audio_worker = None
        self.audio_output = None
        self.audio_device = None
        self.render_worker = None
        self.render_queue = []
        self.render_task_records = []
        self.render_active_task = None
        self.render_queue_paused = False
        self.render_stop_requested = False
        self.scene_detect_worker = None
        self.render_project_id = ""
        self.render_project_name = ""
        self.render_project_path = ""
        self.preview_position_ms = 0
        self.preview_position_seconds = 0.0
        self.software_preview_playing = False
        self._pending_seek_seconds = None
        self._last_seek_decode_error = ""
        self._refreshing_slot_controls = False
        self._refreshing_segment_table = False
        self._refreshing_segment_overview = False
        self._segment_merge_rows = []
        self._active_preview_segment = None
        self.seek_preview_timer = QTimer(self)
        self.seek_preview_timer.setSingleShot(True)
        self.seek_preview_timer.timeout.connect(self._show_pending_seek_frame)
        self._build_ui()
        self._setup_shortcuts()
        self._install_keyboard_filter()
        configure_spinbox_for_direct_input(self)
        self.refresh_project_list()
        self._refresh_render_buttons()

    def _log(self, message):
        self.log_signal.emit(f"[混剪实验室 V2] {message}")

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(8)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("混剪实验室 V2 · 竞品局部替换")
        title.setObjectName("pageTitle")
        subtitle = QLabel(
            "标记区间用于切分完整时间线；片段路径留空时保留原片，填写文件夹后才会抽取素材替换。"
        )
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        import_button = QPushButton("导入竞品原片")
        import_button.setObjectName("toolbarPrimaryButton")
        import_button.clicked.connect(self.choose_source_videos)
        header.addWidget(import_button)
        root.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_project_panel())
        splitter.addWidget(self._build_workbench())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([285, 1250])
        root.addWidget(splitter, 1)

    def _build_project_panel(self):
        panel = QGroupBox("局部替换项目")
        panel.setMinimumWidth(250)
        panel.setMaximumWidth(360)
        layout = QVBoxLayout(panel)
        hint = QLabel("可直接拖入竞品视频。一个原片对应一个项目，标记和素材会自动保存。")
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.project_list = ProjectDropList()
        self.project_list.filesDropped.connect(self.add_source_videos)
        self.project_list.currentItemChanged.connect(self._on_project_selected)
        layout.addWidget(self.project_list, 1)
        actions = QHBoxLayout()
        add_button = QPushButton("添加原片")
        add_button.clicked.connect(self.choose_source_videos)
        open_button = QPushButton("打开项目")
        open_button.clicked.connect(self.open_project_dir)
        actions.addWidget(add_button)
        actions.addWidget(open_button)
        layout.addLayout(actions)

        manage_actions = QHBoxLayout()
        remove_button = QPushButton("移除当前")
        remove_button.setToolTip("移出项目列表，但不删除竞品原片")
        remove_button.clicked.connect(self.remove_current_project)
        clear_button = QPushButton("清空列表")
        clear_button.setToolTip("将全部项目移到“已删除项目”，不删除竞品原片")
        clear_button.clicked.connect(self.clear_project_list)
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh_project_list)
        manage_actions.addWidget(remove_button)
        manage_actions.addWidget(clear_button)
        manage_actions.addWidget(refresh_button)
        layout.addLayout(manage_actions)
        self.project_summary = QLabel("拖入视频后开始标记产品画面。")
        self.project_summary.setObjectName("mutedText")
        self.project_summary.setWordWrap(True)
        layout.addWidget(self.project_summary)
        return panel

    def _build_workbench(self):
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_mark_tab(), "1  标记与片段路径")
        self.tabs.addTab(self._build_render_tab(), "2  批量生成")
        return self.tabs

    def _build_mark_tab(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(6, 8, 6, 6)
        page_layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.mark_splitter = splitter
        editor_panel = QWidget()
        self.mark_editor_panel = editor_panel
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(6, 8, 6, 6)
        editor_layout.setSpacing(0)

        workspace_splitter = QSplitter(Qt.Horizontal)
        workspace_splitter.setChildrenCollapsible(False)
        self.mark_workspace_splitter = workspace_splitter

        preview_panel = QWidget()
        preview_panel.setMinimumWidth(275)
        preview_panel.setMaximumWidth(430)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 6, 0)
        preview_layout.setSpacing(6)

        tools_panel = QWidget()
        tools_panel.setMinimumWidth(390)
        tools_layout = QVBoxLayout(tools_panel)
        tools_layout.setContentsMargins(6, 0, 0, 0)
        tools_layout.setSpacing(6)

        preview_bar = QHBoxLayout()
        self.current_file_label = QLabel("尚未选择竞品原片")
        self.current_file_label.setStyleSheet("font-weight:700;color:#E2E8F0;")
        self.source_info_label = QLabel("")
        self.source_info_label.setObjectName("mutedText")
        self.source_info_label.setWordWrap(True)
        self.display_mode_combo = NoWheelComboBox()
        self.display_mode_combo.addItem("完整原比例", "fit")
        self.display_mode_combo.addItem("100%原始像素", "actual")
        self.display_mode_combo.currentIndexChanged.connect(
            lambda: self.preview_area.set_display_mode(
                self.display_mode_combo.currentData()
            )
        )
        preview_bar.addWidget(self.current_file_label, 1)
        preview_bar.addWidget(self.display_mode_combo)
        preview_layout.addLayout(preview_bar)
        preview_layout.addWidget(self.source_info_label)

        self.preview_area = VideoPreviewArea()
        self.preview_area.setMinimumHeight(390)
        self.preview_area.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding,
        )
        self.preview_area.filesDropped.connect(self.add_source_videos)
        preview_layout.addWidget(self.preview_area, 1)

        playback = QHBoxLayout()
        self.play_button = QPushButton("播放")
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_button.clicked.connect(self.toggle_playback)
        self.frame_back_button = QPushButton("上一帧")
        self.frame_back_button.clicked.connect(lambda: self.step_frame(-1))
        self.frame_forward_button = QPushButton("下一帧")
        self.frame_forward_button.clicked.connect(lambda: self.step_frame(1))
        for button in (self.frame_back_button, self.frame_forward_button):
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(350)
            button.setAutoRepeatInterval(45)
        self.frame_back_button.setEnabled(False)
        self.frame_forward_button.setEnabled(False)
        back_second = QPushButton("-1秒")
        back_second.clicked.connect(lambda: self.seek_relative(-1.0))
        forward_second = QPushButton("+1秒")
        forward_second.clicked.connect(lambda: self.seek_relative(1.0))
        self.time_label = QLabel("00:00:00.000 / 00:00:00.000")
        self.time_label.setMinimumWidth(165)
        self.time_label.setAlignment(Qt.AlignCenter)
        for widget in (
            self.play_button,
            self.frame_back_button,
            self.frame_forward_button,
        ):
            playback.addWidget(widget)
        playback.addStretch(1)
        preview_layout.addLayout(playback)

        seek_row = QHBoxLayout()
        seek_row.addWidget(back_second)
        seek_row.addWidget(self.time_label, 1)
        seek_row.addWidget(forward_second)
        preview_layout.addLayout(seek_row)

        tools_title = QLabel("完整时间轴与切分标记")
        tools_title.setStyleSheet("font-weight:700;color:#E2E8F0;")
        tools_layout.addWidget(tools_title)

        smart_split_box = QGroupBox("智能分割")
        smart_split_layout = QVBoxLayout(smart_split_box)
        smart_split_layout.setContentsMargins(8, 6, 8, 6)
        smart_split_layout.setSpacing(5)
        smart_options = QHBoxLayout()
        self.scene_sensitivity_combo = NoWheelComboBox()
        self.scene_sensitivity_combo.addItem("保守（少切）", 0.42)
        self.scene_sensitivity_combo.addItem("标准", 0.32)
        self.scene_sensitivity_combo.addItem("灵敏（多切）", 0.22)
        self.scene_sensitivity_combo.setCurrentIndex(1)
        self.minimum_segment_spin = QDoubleSpinBox()
        self.minimum_segment_spin.setRange(0.5, 8.0)
        self.minimum_segment_spin.setDecimals(1)
        self.minimum_segment_spin.setSingleStep(0.1)
        self.minimum_segment_spin.setValue(1.0)
        self.minimum_segment_spin.setSuffix(" 秒")
        smart_options.addWidget(QLabel("识别强度"))
        smart_options.addWidget(self.scene_sensitivity_combo, 1)
        smart_options.addWidget(QLabel("最短片段"))
        smart_options.addWidget(self.minimum_segment_spin)
        smart_split_layout.addLayout(smart_options)

        smart_actions = QHBoxLayout()
        self.smart_split_button = QPushButton("开始智能分割")
        self.smart_split_button.setObjectName("toolbarPrimaryButton")
        self.smart_split_button.clicked.connect(self.start_smart_split)
        self.stop_smart_split_button = QPushButton("停止分析")
        self.stop_smart_split_button.setEnabled(False)
        self.stop_smart_split_button.clicked.connect(self.stop_smart_split)
        self.smart_split_status = QLabel("会自动合并转场密集切点，并过滤过短片段")
        self.smart_split_status.setObjectName("mutedText")
        smart_actions.addWidget(self.smart_split_button)
        smart_actions.addWidget(self.stop_smart_split_button)
        smart_actions.addWidget(self.smart_split_status, 1)
        smart_split_layout.addLayout(smart_actions)
        tools_layout.addWidget(smart_split_box)

        self.timeline = HighlightTimeline()
        self.timeline.seekRequested.connect(self._seek_from_timeline)
        self.timeline.pointsChanged.connect(self._on_points_changed)
        tools_layout.addWidget(self.timeline)

        mark_row = QHBoxLayout()
        set_in_button = QPushButton("设置入点  I")
        set_in_button.clicked.connect(self.set_in_point)
        set_out_button = QPushButton("设置出点  O")
        set_out_button.clicked.connect(self.set_out_point)
        self.slot_label_edit = QLineEdit()
        self.slot_label_edit.setPlaceholderText("区间名称（可选）")
        self.slot_label_edit.setMaximumWidth(190)
        add_button = QPushButton("添加切分区间")
        add_button.setObjectName("toolbarPrimaryButton")
        add_button.clicked.connect(self.add_slot)
        update_button = QPushButton("更新选中区间")
        update_button.clicked.connect(self.update_slot)
        clear_button = QPushButton("清除入点/出点")
        clear_button.clicked.connect(self.clear_points)
        mark_row.addWidget(set_in_button)
        mark_row.addWidget(set_out_button)
        mark_row.addWidget(self.slot_label_edit)
        mark_row.addWidget(add_button)
        mark_row.addStretch(1)
        tools_layout.addLayout(mark_row)
        self.point_label = QLabel("入点：--    出点：--    时长：--")
        self.point_label.setObjectName("mutedText")
        mark_manage_row = QHBoxLayout()
        mark_manage_row.addWidget(update_button)
        mark_manage_row.addWidget(clear_button)
        mark_manage_row.addWidget(self.point_label, 1)
        tools_layout.addLayout(mark_manage_row)

        self.segment_tabs = QTabWidget()
        segment_page = QWidget()
        segment_layout = QVBoxLayout(segment_page)
        segment_layout.setContentsMargins(0, 4, 0, 0)
        segment_layout.setSpacing(5)
        self.segment_overview_table = QTableWidget(0, 4)
        self.segment_overview_table.setHorizontalHeaderLabels(
            ["编号", "时间范围", "时长", "处理"]
        )
        self.segment_overview_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.segment_overview_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.segment_overview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.segment_overview_table.verticalHeader().setVisible(False)
        self.segment_overview_table.verticalHeader().setDefaultSectionSize(32)
        for column in (0, 2, 3):
            self.segment_overview_table.horizontalHeader().setSectionResizeMode(
                column,
                QHeaderView.ResizeToContents,
            )
        self.segment_overview_table.horizontalHeader().setSectionResizeMode(
            1,
            QHeaderView.Stretch,
        )
        self.segment_overview_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAlwaysOff
        )
        self.segment_overview_table.itemSelectionChanged.connect(
            self._on_segment_overview_selected
        )
        self.segment_overview_table.installEventFilter(self)
        self.segment_overview_table.viewport().installEventFilter(self)
        segment_layout.addWidget(self.segment_overview_table, 1)
        segment_actions = QHBoxLayout()
        clear_segment_selection = QPushButton("取消片段选择 / 返回整条")
        clear_segment_selection.clicked.connect(self.clear_segment_preview_selection)
        self.segment_playback_hint = QLabel("选中片段后，播放键只播放该片段")
        self.segment_playback_hint.setObjectName("mutedText")
        segment_actions.addWidget(clear_segment_selection)
        segment_actions.addWidget(self.segment_playback_hint, 1)
        segment_layout.addLayout(segment_actions)
        self.segment_tabs.addTab(segment_page, "完整片段（0）")

        manual_page = QWidget()
        manual_layout = QVBoxLayout(manual_page)
        manual_layout.setContentsMargins(0, 4, 0, 0)
        manual_layout.setSpacing(5)
        self.slot_table = QTableWidget(0, 5)
        self.slot_table.setHorizontalHeaderLabels(
            ["标记编号", "区间名称", "入点", "出点", "时长"]
        )
        self.slot_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.slot_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.slot_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.slot_table.verticalHeader().setVisible(False)
        self.slot_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        for column in (0, 2, 3, 4):
            self.slot_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeToContents
            )
        self.slot_table.itemSelectionChanged.connect(self._on_slot_selected)
        manual_layout.addWidget(self.slot_table, 1)
        manual_actions = QHBoxLayout()
        full_timeline = QPushButton("返回完整轨道")
        full_timeline.setToolTip("取消当前编号的入点/出点选择，显示全部切分片段")
        full_timeline.clicked.connect(self.show_full_timeline)
        preview_slot = QPushButton("预览选中区间")
        preview_slot.clicked.connect(self.preview_selected_slot)
        delete_slot = QPushButton("删除选中区间")
        delete_slot.clicked.connect(self.delete_selected_slot)
        manual_actions.addWidget(full_timeline)
        manual_actions.addWidget(preview_slot)
        manual_actions.addWidget(delete_slot)
        manual_actions.addStretch(1)
        manual_layout.addLayout(manual_actions)
        self.segment_tabs.addTab(manual_page, "人工标记（0）")
        tools_layout.addWidget(self.segment_tabs, 1)

        self.preview_progress = QProgressBar()
        self.preview_progress.setRange(0, 100)
        self.preview_progress.setValue(0)
        self.preview_progress.setFormat("等待导入竞品原片")
        tools_layout.addWidget(self.preview_progress)

        workspace_splitter.addWidget(preview_panel)
        workspace_splitter.addWidget(tools_panel)
        workspace_splitter.setStretchFactor(0, 0)
        workspace_splitter.setStretchFactor(1, 1)
        workspace_splitter.setSizes([350, 620])
        editor_layout.addWidget(workspace_splitter, 1)

        splitter.addWidget(editor_panel)
        splitter.addWidget(self._build_segment_path_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([850, 500])
        page_layout.addWidget(splitter, 1)
        return page

    def _build_segment_path_panel(self):
        panel = QWidget()
        self.segment_path_panel = panel
        panel.setMinimumWidth(430)
        panel.setMaximumWidth(620)
        outer_root = QVBoxLayout(panel)
        outer_root.setContentsMargins(0, 0, 0, 0)
        outer_root.setSpacing(8)

        intro_group = QGroupBox("1. 独立片头（拼接在正文最前面）")
        root = QVBoxLayout(intro_group)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        intro_header = QHBoxLayout()
        self.intro_enabled_check = QCheckBox("启用片头")
        self.intro_enabled_check.setMinimumWidth(100)
        self.intro_enabled_check.toggled.connect(self._on_intro_settings_changed)
        intro_header.addWidget(self.intro_enabled_check)
        self.intro_full_play_check = QCheckBox("完整播放")
        self.intro_full_play_check.setMinimumWidth(100)
        self.intro_full_play_check.setToolTip(
            "勾选后随机选择一条片头视频并完整播放；取消勾选后按右侧时长截取"
        )
        self.intro_full_play_check.setChecked(True)
        self.intro_full_play_check.toggled.connect(self._on_intro_settings_changed)
        self.intro_duration_spin = QDoubleSpinBox()
        self.intro_duration_spin.setRange(0.20, 600.0)
        self.intro_duration_spin.setDecimals(2)
        self.intro_duration_spin.setSingleStep(0.10)
        self.intro_duration_spin.setValue(3.0)
        self.intro_duration_spin.setSuffix(" 秒")
        self.intro_duration_spin.setKeyboardTracking(False)
        self.intro_duration_spin.setEnabled(False)
        self.intro_duration_spin.valueChanged.connect(
            self._on_intro_settings_changed
        )
        intro_header.addWidget(self.intro_full_play_check)
        intro_header.addStretch(1)
        intro_header.addWidget(QLabel("自定义时长"))
        intro_header.addWidget(self.intro_duration_spin)
        root.addLayout(intro_header)

        self.intro_dir_edit = QLineEdit()
        self.intro_dir_edit.setReadOnly(True)
        self.intro_dir_edit.setPlaceholderText("当前项目的片头素材文件夹")

        intro_actions = QHBoxLayout()
        choose_intro_dir = QPushButton("选择")
        choose_intro_dir.setToolTip("选择外部片头素材目录")
        choose_intro_dir.clicked.connect(self._choose_intro_dir)
        use_project_intro_dir = QPushButton("项目")
        use_project_intro_dir.setToolTip("恢复使用当前项目自带的“片头素材”文件夹")
        use_project_intro_dir.clicked.connect(self._use_project_intro_dir)
        open_intro_dir = QPushButton("打开")
        open_intro_dir.setToolTip("打开当前片头素材目录")
        open_intro_dir.clicked.connect(self._open_intro_dir)
        intro_actions.addWidget(self.intro_dir_edit, 1)
        intro_actions.addWidget(choose_intro_dir)
        intro_actions.addWidget(use_project_intro_dir)
        intro_actions.addWidget(open_intro_dir)
        root.addLayout(intro_actions)

        self.intro_count_label = QLabel(
            "片头独立拼在最前面，正文原声或替换音频从片头结束后开始。"
        )
        self.intro_count_label.setObjectName("mutedText")
        self.intro_count_label.setWordWrap(True)
        root.addWidget(self.intro_count_label)

        outer_root.addWidget(intro_group)

        audio_group = QGroupBox("2. 正文音频（从片头结束后开始）")
        root = QVBoxLayout(audio_group)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        self.audio_mode_combo = NoWheelComboBox()
        self.audio_mode_combo.addItem("保留竞品原音频", "source")
        self.audio_mode_combo.addItem("从文件夹随机替换（音频决定成片时长）", "folder")
        self.audio_mode_combo.currentIndexChanged.connect(
            self._on_audio_settings_changed
        )
        root.addWidget(self.audio_mode_combo)

        self.audio_dir_edit = QLineEdit()
        self.audio_dir_edit.setReadOnly(True)
        self.audio_dir_edit.setPlaceholderText("当前项目的替换音频文件夹")

        audio_actions = QHBoxLayout()
        choose_audio_dir = QPushButton("选择")
        choose_audio_dir.setToolTip("选择外部正文音频目录")
        choose_audio_dir.clicked.connect(self._choose_audio_dir)
        use_project_audio_dir = QPushButton("项目")
        use_project_audio_dir.setToolTip("恢复使用当前项目自带的“替换音频”文件夹")
        use_project_audio_dir.clicked.connect(self._use_project_audio_dir)
        open_audio_dir = QPushButton("打开")
        open_audio_dir.setToolTip("打开当前正文音频目录")
        open_audio_dir.clicked.connect(self._open_audio_dir)
        audio_actions.addWidget(self.audio_dir_edit, 1)
        audio_actions.addWidget(choose_audio_dir)
        audio_actions.addWidget(use_project_audio_dir)
        audio_actions.addWidget(open_audio_dir)
        root.addLayout(audio_actions)

        self.audio_count_label = QLabel(
            "保留原音频；切换为文件夹替换后，成片时长以随机音频为准。"
        )
        self.audio_count_label.setObjectName("mutedText")
        self.audio_count_label.setWordWrap(True)
        root.addWidget(self.audio_count_label)

        outer_root.addWidget(audio_group)

        segment_group = QGroupBox("3. 完整片段路径（路径为空时保留原片）")
        root = QVBoxLayout(segment_group)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        hint = QLabel(
            "替换路径可直接输入或粘贴，按回车或点击别处自动保存；留空即保留原片。"
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.segment_mapping_table = QTableWidget(0, 5)
        self.segment_mapping_table.setHorizontalHeaderLabels(
            ["合并", "片段", "时间范围", "替换路径", "素材"]
        )
        header = self.segment_mapping_table.horizontalHeader()
        for column in (0, 1, 2, 4):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.segment_mapping_table.setColumnWidth(0, 40)
        self.segment_mapping_table.setColumnWidth(1, 50)
        self.segment_mapping_table.setColumnWidth(2, 112)
        self.segment_mapping_table.setColumnWidth(4, 52)
        self.segment_mapping_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.segment_mapping_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.segment_mapping_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.segment_mapping_table.verticalHeader().setVisible(False)
        self.segment_mapping_table.verticalHeader().setDefaultSectionSize(36)
        self.segment_mapping_table.setAlternatingRowColors(True)
        self.segment_mapping_table.setShowGrid(True)
        self.segment_mapping_table.setFrameShape(QFrame.StyledPanel)
        self.segment_mapping_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.segment_mapping_table.cellDoubleClicked.connect(
            lambda _row, _column: self._locate_selected_segment()
        )
        self.segment_mapping_table.itemSelectionChanged.connect(
            self._on_segment_mapping_selected
        )
        root.addWidget(self.segment_mapping_table, 1)

        table_actions = QHBoxLayout()
        self.merge_segments_button = QPushButton("合并勾选")
        self.merge_segments_button.setToolTip(
            "例如勾选片段002和003，合并后保留为一个片段"
        )
        self.merge_segments_button.clicked.connect(self.merge_selected_segments)
        clear_selection = QPushButton("取消")
        clear_selection.setToolTip("取消表格中所有待合并片段的勾选")
        clear_selection.clicked.connect(self.clear_segment_merge_selection)
        refresh_materials = QPushButton("刷新")
        refresh_materials.setToolTip("重新扫描各替换目录中的视频数量")
        refresh_materials.clicked.connect(self.refresh_material_status)
        open_project = QPushButton("项目")
        open_project.setToolTip("打开当前 V2 项目目录")
        open_project.clicked.connect(self.open_project_dir)
        table_actions.addWidget(self.merge_segments_button, 1)
        table_actions.addWidget(clear_selection)
        table_actions.addWidget(refresh_materials)
        table_actions.addWidget(open_project)
        root.addLayout(table_actions)
        self.material_status_label = QLabel("智能或手动切分后，这里会显示完整片段表。")
        self.material_status_label.setObjectName("mutedText")
        self.material_status_label.setWordWrap(True)
        root.addWidget(self.material_status_label)
        outer_root.addWidget(segment_group, 1)
        return panel

    def _build_render_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        settings = QGroupBox("生成设置")
        form = QFormLayout(settings)
        self.render_project_label = QLabel("尚未选择项目")
        self.render_summary_label = QLabel("--")
        self.render_summary_label.setWordWrap(True)
        self.loops_spin = QSpinBox()
        self.loops_spin.setRange(1, 9999)
        self.loops_spin.setValue(10)
        self.loops_spin.setKeyboardTracking(False)
        self.loops_spin.valueChanged.connect(self._on_loops_changed)
        self.encoder_combo = NoWheelComboBox()
        self.encoder_combo.addItem("自动（优先 NVIDIA，失败转 CPU）", "auto")
        self.encoder_combo.addItem("仅 CPU 精准编码", "cpu")
        transition_row = QWidget()
        transition_layout = QHBoxLayout(transition_row)
        transition_layout.setContentsMargins(0, 0, 0, 0)
        transition_layout.setSpacing(8)
        self.transition_mode_combo = NoWheelComboBox()
        for transition_name, transition_value in TRANS_MAP.items():
            self.transition_mode_combo.addItem(transition_name, transition_value)
        self.transition_duration_spin = QDoubleSpinBox()
        self.transition_duration_spin.setRange(0.10, 0.80)
        self.transition_duration_spin.setDecimals(2)
        self.transition_duration_spin.setSingleStep(0.05)
        self.transition_duration_spin.setValue(0.25)
        self.transition_duration_spin.setSuffix(" 秒")
        self.transition_mode_combo.currentIndexChanged.connect(
            self._on_transition_settings_changed
        )
        self.transition_duration_spin.valueChanged.connect(
            self._on_transition_settings_changed
        )
        transition_layout.addWidget(self.transition_mode_combo, 1)
        transition_layout.addWidget(QLabel("总时长"))
        transition_layout.addWidget(self.transition_duration_spin)
        smart_sfx_row = QWidget()
        smart_sfx_layout = QHBoxLayout(smart_sfx_row)
        smart_sfx_layout.setContentsMargins(0, 0, 0, 0)
        smart_sfx_layout.setSpacing(8)
        self.smart_sfx_check = QCheckBox("启用")
        self.smart_sfx_check.setChecked(False)
        self.smart_sfx_check.setToolTip(
            "根据片头、替换片段、转场和收尾自动加入结构音效。"
        )
        self.smart_sfx_strength_combo = NoWheelComboBox()
        self.smart_sfx_strength_combo.addItem("轻量（少而克制）", "light")
        self.smart_sfx_strength_combo.addItem("标准（推荐）", "standard")
        self.smart_sfx_strength_combo.addItem("强化（更密集）", "strong")
        self.smart_sfx_strength_combo.setCurrentIndex(1)
        self.smart_sfx_strength_combo.setEnabled(False)
        self.smart_sfx_strength_combo.setToolTip(
            "内置 16 类共 192 个音色，连续音效不会重复；首次使用后自动缓存。"
        )
        self.smart_sfx_check.toggled.connect(self._on_sfx_settings_changed)
        self.smart_sfx_strength_combo.currentIndexChanged.connect(
            self._on_sfx_settings_changed
        )
        smart_sfx_layout.addWidget(self.smart_sfx_check)
        smart_sfx_layout.addWidget(self.smart_sfx_strength_combo, 1)
        form.addRow("当前项目：", self.render_project_label)
        form.addRow("项目检查：", self.render_summary_label)
        form.addRow("生成数量：", self.loops_spin)
        form.addRow("编码方式：", self.encoder_combo)
        form.addRow("片段转场：", transition_row)
        form.addRow("智能音效：", smart_sfx_row)
        duration_hint = QLabel(
            "整条音频轨道在上一页右侧设置。替换音频较短时截短画面；"
            "较长时循环完整视觉结构，画面始终以音频终点结束。"
        )
        duration_hint.setObjectName("mutedText")
        duration_hint.setWordWrap(True)
        form.addRow("", duration_hint)
        layout.addWidget(settings)

        actions = QHBoxLayout()
        self.start_render_button = QPushButton("开始 / 继续生成")
        self.start_render_button.setObjectName("toolbarPrimaryButton")
        self.start_render_button.clicked.connect(self.start_render)
        self.stop_render_button = QPushButton("暂停任务")
        self.stop_render_button.setEnabled(False)
        self.stop_render_button.clicked.connect(self.stop_render)
        self.clear_render_queue_button = QPushButton("清空等待队列")
        self.clear_render_queue_button.setEnabled(False)
        self.clear_render_queue_button.clicked.connect(self.clear_render_queue)
        open_output = QPushButton("打开当前项目输出")
        open_output.clicked.connect(self.open_output_dir)
        validate_button = QPushButton("重新检查项目")
        validate_button.clicked.connect(self.refresh_material_status)
        actions.addWidget(self.start_render_button)
        actions.addWidget(self.stop_render_button)
        actions.addWidget(self.clear_render_queue_button)
        actions.addWidget(open_output)
        actions.addWidget(validate_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.render_task_owner_label = QLabel("任务列表所属：尚未开始")
        self.render_task_owner_label.setStyleSheet(
            "font-weight:700;color:#93C5FD;"
        )
        layout.addWidget(self.render_task_owner_label)

        self.render_queue_table = QTableWidget(0, 3)
        self.render_queue_table.setHorizontalHeaderLabels(
            ["队列顺序", "项目", "状态"]
        )
        self.render_queue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.render_queue_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.render_queue_table.verticalHeader().setVisible(False)
        self.render_queue_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.render_queue_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        self.render_queue_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents
        )
        self.render_queue_table.setMinimumHeight(86)
        self.render_queue_table.setMaximumHeight(142)
        layout.addWidget(self.render_queue_table)

        self.render_table = QTableWidget(0, 3)
        self.render_table.setHorizontalHeaderLabels(
            ["成片任务", "状态", "计划输出文件"]
        )
        self.render_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.render_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.render_table.verticalHeader().setVisible(False)
        self.render_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.render_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents
        )
        self.render_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        layout.addWidget(self.render_table, 1)
        self.render_progress = QProgressBar()
        self.render_progress.setRange(0, 1)
        self.render_progress.setValue(0)
        self.render_progress.setFormat("等待开始")
        layout.addWidget(self.render_progress)
        self.render_status = QLabel(
            "每轮只替换已填写文件夹路径的片段；启用替换音频后，成片时长由音频决定。"
        )
        self.render_status.setObjectName("mutedText")
        self.render_status.setWordWrap(True)
        layout.addWidget(self.render_status)
        return page

    def _setup_shortcuts(self):
        self._shortcuts = []
        for key, callback in (
            ("I", self.set_in_point),
            ("O", self.set_out_point),
            ("Left", lambda: self.step_frame(-1)),
            ("Right", lambda: self.step_frame(1)),
        ):
            shortcut = QShortcut(key, self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(key in {"Left", "Right"})
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
            event.type() == QEvent.MouseButtonPress
            and self._active_preview_segment
            and isinstance(watched, QWidget)
        ):
            inside_segment_table = (
                watched is self.segment_overview_table
                or self.segment_overview_table.isAncestorOf(watched)
                or watched is self.segment_mapping_table
                or self.segment_mapping_table.isAncestorOf(watched)
            )
            if not inside_segment_table and watched is not self.play_button:
                QTimer.singleShot(0, self.clear_segment_preview_selection)
        if (
            event.type() == QEvent.MouseButtonPress
            and isinstance(watched, QLineEdit)
            and watched.property("segment_mapping_row") is not None
        ):
            row = int(watched.property("segment_mapping_row"))
            QTimer.singleShot(
                0,
                lambda target_row=row: self._activate_segment_mapping_row(
                    target_row
                ),
            )
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
            if isinstance(focus, (QLineEdit, QComboBox, QSpinBox)):
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
        paths = ProjectDropList._video_paths(event.mimeData().urls())
        if paths:
            self.add_source_videos(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def choose_source_videos(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "导入竞品原片",
            workspace_path("素材"),
            "视频文件 (*.mp4 *.mov *.m4v *.avi *.mkv *.webm *.ts *.mts *.m2ts)",
        )
        self.add_source_videos(paths)

    def add_source_videos(self, paths):
        first_project_path = ""
        added = 0
        for path in paths or []:
            path = os.path.abspath(path)
            if not os.path.isfile(path) or not path.lower().endswith(VIDEO_EXTENSIONS):
                continue
            try:
                project = self.store.create_or_open(path)
            except Exception as exc:
                self._log(f"无法创建项目：{exc}")
                continue
            first_project_path = first_project_path or project["_project_path"]
            added += 1
        if not added:
            return
        self.refresh_project_list(select_project_path=first_project_path)
        self._log(f"已导入 {added} 个竞品原片项目")

    def remove_current_project(self):
        if (
            self.render_worker
            and self.render_worker.isRunning()
            or self.render_queue
        ):
            QMessageBox.warning(
                self,
                "生成队列未结束",
                "请先暂停当前任务并清空等待队列，再移除项目。",
            )
            return
        if not self.current_project:
            QMessageBox.information(self, "没有项目", "当前没有可移除的项目。")
            return
        project_name = str(self.current_project.get("name") or "未命名项目")
        answer = QMessageBox.question(
            self,
            "移除当前项目",
            f"确定从列表中移除“{project_name}”吗？\n\n"
            "项目标记、项目素材和生成结果会移到“已删除项目”，"
            "竞品原始视频不会被删除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._cancel_preview_worker()
        self._stop_preview(reset_position=True)
        try:
            archived_dir = self.store.archive_project(self.current_project)
        except Exception as exc:
            QMessageBox.warning(self, "移除失败", str(exc))
            return
        self._clear_current_project()
        self.refresh_project_list()
        self._log(f"已移除项目“{project_name}”，备份位置：{archived_dir}")

    def clear_project_list(self):
        if (
            self.render_worker
            and self.render_worker.isRunning()
            or self.render_queue
        ):
            QMessageBox.warning(
                self,
                "生成队列未结束",
                "请先暂停当前任务并清空等待队列，再清空项目列表。",
            )
            return
        projects = self.store.list_projects()
        if not projects:
            QMessageBox.information(self, "列表为空", "当前项目列表已经是空的。")
            return
        answer = QMessageBox.question(
            self,
            "清空项目列表",
            f"确定清空当前 {len(projects)} 个项目吗？\n\n"
            "全部项目会移到“已删除项目”，竞品原始视频不会被删除。"
            "之后可以直接导入新项目。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._cancel_preview_worker()
        self._stop_preview(reset_position=True)
        archived_count = 0
        failures = []
        for project in projects:
            try:
                self.store.archive_project(project)
                archived_count += 1
            except Exception as exc:
                failures.append(f"{project.get('name', '未命名项目')}：{exc}")
        self._clear_current_project()
        self.refresh_project_list()
        self._log(f"已清空项目列表，共移除 {archived_count} 个项目")
        if failures:
            QMessageBox.warning(
                self,
                "部分项目未移除",
                "以下项目处理失败：\n" + "\n".join(failures[:8]),
            )

    def refresh_project_list(self, select_project_path=""):
        current_path = select_project_path or str(
            (self.current_project or {}).get("_project_path") or ""
        )
        self.project_list.blockSignals(True)
        self.project_list.clear()
        target_row = -1
        projects = self.store.list_projects()
        for row, project in enumerate(projects):
            slots = [
                item
                for item in project.get("slots", [])
                if isinstance(item, dict) and item.get("enabled", True)
            ]
            issues, status = self.store.validate(project)
            replacements = [item for item in status if item.get("folder")]
            ready_count = sum(1 for item in replacements if item.get("ready"))
            source_exists = os.path.isfile(str(project.get("source_path") or ""))
            state = (
                f"{len(slots)}个人工标记 · "
                f"{len(project.get('cut_points', []))}个智能切点 · "
                f"{len(status)}个片段 · "
                f"{ready_count}/{len(replacements)}个替换就绪"
                if source_exists
                else "原片已移动或丢失"
            )
            item = QListWidgetItem(f"{project.get('name', '未命名')}\n{state}")
            item.setData(Qt.UserRole, project["_project_path"])
            item.setToolTip(
                f"{project.get('source_path', '')}\n"
                + ("项目已就绪" if not issues else "；".join(issues))
            )
            self.project_list.addItem(item)
            if (
                current_path
                and os.path.normcase(current_path)
                == os.path.normcase(project["_project_path"])
            ):
                target_row = row
        self.project_list.blockSignals(False)
        if target_row < 0 and projects:
            target_row = 0
        if target_row >= 0:
            self.project_list.setCurrentRow(target_row)
            self._on_project_selected(self.project_list.currentItem(), None)
        elif not projects:
            self._clear_current_project()

    def _on_project_selected(self, current, previous):
        if not current:
            return
        project_path = str(current.data(Qt.UserRole) or "")
        if (
            self.current_project
            and os.path.normcase(str(self.current_project.get("_project_path") or ""))
            == os.path.normcase(project_path)
        ):
            self._refresh_all_project_views()
            return
        try:
            project = self.store.load(project_path)
            project = self.store.sync_segment_mappings(project, save=True)
        except Exception as exc:
            QMessageBox.warning(self, "项目无法打开", str(exc))
            return
        self._stop_preview(reset_position=True)
        self.clear_segment_preview_selection()
        self._cancel_scene_detect_worker()
        self._cancel_preview_worker()
        self._cancel_frame_timeline_worker()
        self._cancel_remote_cache_worker()
        self._cancel_seek_frame_worker()
        self._reset_frame_timeline()
        self.preview_source_path = ""
        self.current_project = project
        self.preview_metadata = dict(project.get("source_metadata") or {})
        source_path = str(project.get("source_path") or "")
        self.current_file_label.setText(os.path.basename(source_path) or "竞品原片丢失")
        self.preview_area.show_message("正在准备原片预览...")
        self._refresh_all_project_views()
        if not os.path.isfile(source_path):
            self.preview_area.show_message("竞品原片已移动或删除，请恢复原路径后再使用。")
            self.preview_progress.setFormat("竞品原片不存在")
            return
        self._prepare_project_preview(source_path)

    def _clear_current_project(self):
        self._cancel_scene_detect_worker()
        self._cancel_remote_cache_worker()
        self._cancel_seek_frame_worker()
        self._cancel_frame_timeline_worker()
        self._reset_frame_timeline()
        self.preview_source_path = ""
        self.current_project = None
        self.preview_metadata = {}
        self.current_file_label.setText("尚未选择竞品原片")
        self.source_info_label.setText("")
        self.preview_area.show_message("拖入竞品原片后开始标记")
        self.timeline.set_media(0)
        self.timeline.set_segments([])
        self._active_preview_segment = None
        self.segment_overview_table.setRowCount(0)
        self.slot_table.setRowCount(0)
        self.segment_mapping_table.setRowCount(0)
        self.segment_tabs.setTabText(0, "完整片段（0）")
        self.segment_tabs.setTabText(1, "人工标记（0）")
        self.segment_playback_hint.setText("选中片段后，播放键只播放该片段")
        self._segment_merge_rows = []
        self.merge_segments_button.setEnabled(False)
        self.material_status_label.setText("智能或手动切分后，这里会显示完整片段表。")
        self.project_summary.setText("拖入视频后开始标记产品画面。")
        self._refresh_render_summary()
        self._refresh_render_buttons()

    def _preview_source(self):
        source = str(self.preview_source_path or "")
        if source and os.path.isfile(source):
            return source
        return str((self.current_project or {}).get("source_path") or "")

    def _remote_cache_target(self, source_path):
        signature = dict(
            ((self.current_project or {}).get("source_signature") or {})
        )
        fingerprint = str(signature.get("fingerprint") or "")
        if not fingerprint:
            try:
                fingerprint = str(
                    source_signature(source_path).get("fingerprint") or ""
                )
            except OSError:
                fingerprint = ""
        if not fingerprint:
            return ""
        extension = os.path.splitext(source_path)[1].lower() or ".mp4"
        return os.path.join(
            self.store.cache_dir,
            "NAS素材缓存",
            fingerprint,
            f"source{extension}",
        )

    def _start_project_preview(self, source_path):
        self.preview_source_path = os.path.abspath(source_path)
        if os.path.normcase(self.preview_source_path) != os.path.normcase(
            str((self.current_project or {}).get("source_path") or "")
        ):
            touch_cache_entry(self.preview_source_path)
        self._start_frame_timeline_build(self.preview_source_path)
        self._start_preview_build(self.preview_source_path)

    def _prepare_project_preview(self, source_path):
        self._cancel_remote_cache_worker()
        target = self._remote_cache_target(source_path)
        if not is_remote_path(source_path) or not target:
            self._start_project_preview(source_path)
            return

        expected_size = int(
            ((self.current_project or {}).get("source_signature") or {}).get(
                "size"
            )
            or 0
        )
        try:
            cached_size = os.path.getsize(target)
        except OSError:
            cached_size = 0
        if cached_size > 0 and (not expected_size or cached_size == expected_size):
            touch_cache_entry(target)
            self._log("已复用本机 NAS 原片预览缓存。")
            self._start_project_preview(target)
            return

        self.preview_area.show_message("正在把 NAS 原片缓存到本机，请稍候...")
        self.preview_progress.setValue(2)
        self.preview_progress.setFormat("正在建立本机预览缓存...")
        worker = RemotePreviewCacheWorker(
            source_path,
            target,
            expected_size=expected_size,
            parent=self,
        )
        worker.progress.connect(self._on_remote_cache_progress)
        worker.ready.connect(self._on_remote_cache_ready)
        worker.failed.connect(self._on_remote_cache_failed)
        worker.finished.connect(lambda: self._clear_remote_cache_worker(worker))
        self.remote_cache_worker = worker
        worker.start()

    def _cancel_remote_cache_worker(self):
        worker = self.remote_cache_worker
        self.remote_cache_worker = None
        if worker and worker.isRunning():
            worker.cancel()
            worker.wait(5000)

    def _clear_remote_cache_worker(self, worker):
        if self.remote_cache_worker is worker:
            self.remote_cache_worker = None

    def _on_remote_cache_progress(self, value, message):
        self.preview_progress.setValue(int(value))
        self.preview_progress.setFormat(str(message))

    def _on_remote_cache_ready(self, original_source, cached_source, reused):
        current_source = str((self.current_project or {}).get("source_path") or "")
        if os.path.normcase(original_source) != os.path.normcase(current_source):
            return
        self._log(
            "已复用本机 NAS 原片预览缓存。"
            if reused
            else "NAS 原片已复制到本机，后续逐帧不再重复读取网盘。"
        )
        self._start_project_preview(cached_source)

    def _on_remote_cache_failed(self, original_source, message):
        current_source = str((self.current_project or {}).get("source_path") or "")
        if os.path.normcase(original_source) != os.path.normcase(current_source):
            return
        self._log(f"NAS 本机预览缓存失败，已回退后台直读：{message}")
        self._start_project_preview(original_source)

    def _start_preview_build(self, source_path):
        self.preview_progress.setValue(5)
        self.preview_progress.setFormat("正在准备完整时间轴...")
        worker = PreviewBuildWorker(
            source_path,
            self.ffmpeg_path,
            self.ffprobe_path,
            self.store.cache_dir,
            cache_identity=str(
                ((self.current_project or {}).get("source_signature") or {}).get(
                    "fingerprint"
                )
                or ""
            ),
        )
        worker.progress.connect(self._on_preview_progress)
        worker.finished.connect(self._on_preview_ready)
        worker.failed.connect(self._on_preview_failed)
        worker.finished.connect(lambda *_: self._clear_preview_worker(worker))
        worker.failed.connect(lambda *_: self._clear_preview_worker(worker))
        self.preview_worker = worker
        worker.start()

    def _cancel_preview_worker(self):
        worker = self.preview_worker
        self.preview_worker = None
        if worker and worker.isRunning():
            worker.cancel()
            worker.wait(5000)

    def _set_frame_step_enabled(self, enabled):
        self.frame_back_button.setEnabled(bool(enabled))
        self.frame_forward_button.setEnabled(bool(enabled))

    def _reset_frame_timeline(self):
        self.frame_timestamps = []
        self.preview_frame_index = None
        self._frame_index_wait_logged = False
        self._set_frame_step_enabled(False)

    def _frame_timeline_cache_path(self, source):
        fingerprint = str(
            ((self.current_project or {}).get("source_signature") or {}).get(
                "fingerprint"
            )
            or ""
        )
        if not fingerprint:
            try:
                fingerprint = str(
                    source_signature(source).get("fingerprint") or ""
                )
            except Exception:
                fingerprint = ""
        if not fingerprint:
            return "", ""
        cache_path = os.path.join(
            self.store.cache_dir, "帧索引", f"{fingerprint}.json"
        )
        return fingerprint, cache_path

    def _start_frame_timeline_build(self, source):
        self._cancel_frame_timeline_worker()
        self._reset_frame_timeline()
        fingerprint, cache_path = self._frame_timeline_cache_path(source)
        if not fingerprint or not os.path.isfile(source):
            self._set_frame_step_enabled(True)
            return
        worker = FrameTimelineWorker(
            self.ffprobe_path,
            source,
            fingerprint,
            cache_path,
            self,
        )
        worker.ready.connect(self._on_frame_timeline_ready)
        worker.failed.connect(self._on_frame_timeline_failed)
        worker.finished.connect(lambda: self._clear_frame_timeline_worker(worker))
        self.frame_timeline_worker = worker
        worker.start()

    def _cancel_frame_timeline_worker(self):
        worker = self.frame_timeline_worker
        self.frame_timeline_worker = None
        if worker and worker.isRunning():
            worker.cancel()
            worker.wait(5000)

    def _clear_frame_timeline_worker(self, worker):
        if self.frame_timeline_worker is worker:
            self.frame_timeline_worker = None

    def _on_frame_timeline_ready(self, source, timestamps):
        current_source = self._preview_source()
        if os.path.normcase(source) != os.path.normcase(current_source):
            return
        self.frame_timestamps = [float(value) for value in timestamps]
        self.preview_frame_index = self._frame_index_at_or_after(
            self._current_position_seconds()
        )
        self._frame_index_wait_logged = False
        self._set_frame_step_enabled(True)
        self._update_time_label()
        self._log(f"逐帧索引已就绪：共 {len(self.frame_timestamps)} 帧")

    def _on_frame_timeline_failed(self, source, message):
        current_source = self._preview_source()
        if os.path.normcase(source) != os.path.normcase(current_source):
            return
        self._set_frame_step_enabled(True)
        self._log(f"逐帧索引失败，将按固定帧率步进：{message}")

    def _frame_index_at_or_after(self, seconds):
        if not self.frame_timestamps:
            return None
        index = bisect.bisect_left(
            self.frame_timestamps, max(0.0, float(seconds or 0.0)) - 0.0000001
        )
        return min(max(0, index), len(self.frame_timestamps) - 1)

    def _clear_preview_worker(self, worker):
        if self.preview_worker is worker:
            self.preview_worker = None

    def _on_preview_progress(self, value, text):
        self.preview_progress.setValue(int(value))
        self.preview_progress.setFormat(str(text))

    def _on_preview_ready(self, source, metadata):
        if not self.current_project:
            return
        if os.path.normcase(source) != os.path.normcase(
            self._preview_source()
        ):
            return
        self.preview_metadata = dict(metadata or {})
        self.current_project["source_metadata"] = {
            key: self.preview_metadata.get(key)
            for key in ("duration", "width", "height", "fps", "audio_codec", "video_codec")
        }
        self.current_project = self.store.sync_segment_mappings(
            self.current_project, save=True
        )
        self._apply_preview_metadata()
        self._refresh_all_project_views()
        original_source = str(self.current_project.get("source_path") or source)
        self._log(f"原片预览已就绪：{os.path.basename(original_source)}")

    def _on_preview_failed(self, source, message):
        self.preview_progress.setValue(0)
        self.preview_progress.setFormat("时间轴准备失败")
        self.preview_area.show_message(f"预览准备失败：{message}")
        self._log(f"原片预览准备失败：{message}")

    def _apply_preview_metadata(self):
        metadata = self.preview_metadata
        duration = float(metadata.get("duration") or 0.0)
        self.timeline.set_media(
            duration,
            metadata.get("thumbnails") or [],
            metadata.get("waveform") or [],
        )
        self.timeline.set_segments(self._timeline_slots())
        self.preview_area.set_source_size(
            metadata.get("display_width") or metadata.get("width") or 1280,
            metadata.get("display_height") or metadata.get("height") or 720,
        )
        thumbnails = metadata.get("thumbnails") or []
        if thumbnails:
            self.preview_area.set_poster_image(thumbnails[0])
        self.preview_position_ms = 0
        self.preview_position_seconds = 0.0
        self.preview_frame_index = 0 if self.frame_timestamps else None
        self._queue_seek_frame(0)
        self.preview_progress.setValue(100)
        self.preview_progress.setFormat("原片画面、声音和完整时间轴已就绪")
        self.source_info_label.setText(
            f"{format_time(duration)} · "
            f"{int(metadata.get('display_width') or metadata.get('width') or 0)}×"
            f"{int(metadata.get('display_height') or metadata.get('height') or 0)} · "
            f"{float(metadata.get('fps') or 0):.2f}fps"
        )
        self._update_time_label()

    def _timeline_slots(self):
        return [
            {
                "start": float(segment.get("start") or 0.0),
                "end": float(segment.get("end") or 0.0),
                "label": f"{int(segment.get('id') or 0):03d}",
                "border_color": (
                    "#22C55E"
                    if str(segment.get("folder") or "").strip()
                    else "#94A3B8"
                ),
                "fill_color": (
                    "#16A34A"
                    if str(segment.get("folder") or "").strip()
                    else (
                        "#334155"
                        if int(segment.get("id") or 0) % 2
                        else "#1E293B"
                    )
                ),
                "fill_alpha": 38,
                "status_bar_height": (
                    5 if str(segment.get("folder") or "").strip() else 3
                ),
                "text_color": (
                    "#DCFCE7"
                    if str(segment.get("folder") or "").strip()
                    else "#E2E8F0"
                ),
            }
            for segment in self.store.timeline_segments(self.current_project or {})
        ]

    def _duration(self):
        return float(
            self.preview_metadata.get("duration")
            or ((self.current_project or {}).get("source_metadata") or {}).get(
                "duration"
            )
            or 0.0
        )

    def start_smart_split(self):
        if self.scene_detect_worker and self.scene_detect_worker.isRunning():
            return
        if not self.current_project:
            QMessageBox.warning(self, "未选择项目", "请先导入竞品原片。")
            return
        source = self._preview_source()
        duration = self._duration()
        if not os.path.isfile(source):
            QMessageBox.warning(self, "原片不存在", "竞品原片已经移动或删除。")
            return
        if not os.path.isfile(self.ffmpeg_path):
            QMessageBox.warning(self, "缺少工具", "网盘资源包/tools 中缺少 ffmpeg.exe。")
            return
        if duration <= 0:
            QMessageBox.warning(self, "尚未就绪", "请等待原片预览信息加载完成后再分析。")
            return
        if (self.current_project or {}).get("cut_points"):
            answer = QMessageBox.question(
                self,
                "重新智能分割",
                "这会替换上一次的智能切点，并恢复曾经人工合并的边界。\n"
                "已经填写的路径会按原时间范围尽量自动继承，是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        worker = SceneDetectWorker(
            self.ffmpeg_path,
            source,
            duration,
            threshold=float(self.scene_sensitivity_combo.currentData() or 0.32),
            minimum_segment=self.minimum_segment_spin.value(),
            parent=self,
        )
        worker.progress.connect(self._on_scene_split_progress)
        worker.completed.connect(self._on_scene_split_completed)
        worker.failed.connect(self._on_scene_split_failed)
        worker.cancelled.connect(self._on_scene_split_cancelled)
        worker.finished.connect(lambda: self._clear_scene_detect_worker(worker))
        self.scene_detect_worker = worker
        self.smart_split_button.setEnabled(False)
        self.stop_smart_split_button.setEnabled(True)
        self.preview_progress.setRange(0, 100)
        self.preview_progress.setValue(0)
        self.preview_progress.setFormat("正在智能分析画面变化...")
        self.smart_split_status.setText("正在分析，界面仍可正常预览")
        self._log(
            f"开始智能分割：{self.scene_sensitivity_combo.currentText()}，"
            f"最短片段 {self.minimum_segment_spin.value():.1f} 秒"
        )
        worker.start()

    def stop_smart_split(self):
        worker = self.scene_detect_worker
        if worker and worker.isRunning():
            self.stop_smart_split_button.setEnabled(False)
            self.smart_split_status.setText("正在安全停止分析...")
            worker.stop()

    def _cancel_scene_detect_worker(self):
        worker = self.scene_detect_worker
        self.scene_detect_worker = None
        if worker and worker.isRunning():
            worker.stop()
            worker.wait(8000)
        if hasattr(self, "smart_split_button"):
            self.smart_split_button.setEnabled(True)
            self.stop_smart_split_button.setEnabled(False)

    def _clear_scene_detect_worker(self, worker):
        if self.scene_detect_worker is worker:
            self.scene_detect_worker = None
        self.smart_split_button.setEnabled(True)
        self.stop_smart_split_button.setEnabled(False)

    def _on_scene_split_progress(self, value, text):
        self.preview_progress.setValue(int(value))
        self.preview_progress.setFormat(str(text))

    def _on_scene_split_completed(self, cut_points, raw_count):
        if not self.current_project:
            return
        if not cut_points:
            self.preview_progress.setValue(0)
            self.preview_progress.setFormat("未识别到可靠切点")
            self.smart_split_status.setText("未发现满足条件的明显画面切换")
            QMessageBox.information(
                self,
                "未识别到切点",
                "没有发现满足当前条件的明显画面切换。\n"
                "可以改用“灵敏（多切）”，或降低最短片段时长后再试。",
            )
            return
        try:
            self.current_project = self.store.set_smart_cut_points(
                self.current_project,
                cut_points,
            )
        except Exception as exc:
            self._on_scene_split_failed(str(exc))
            return
        segments = self.store.timeline_segments(self.current_project)
        self._refresh_all_project_views()
        self.segment_tabs.setCurrentIndex(0)
        self.preview_progress.setValue(100)
        self.preview_progress.setFormat(f"智能分割完成：{len(segments)} 个片段")
        self.smart_split_status.setText(
            f"候选切点 {int(raw_count)} 个，整理后保留 {len(cut_points)} 个"
        )
        self._log(
            f"智能分割完成：候选 {int(raw_count)} 个，过滤后 {len(cut_points)} 个切点，"
            f"最终 {len(segments)} 个片段"
        )

    def _on_scene_split_failed(self, message):
        self.preview_progress.setValue(0)
        self.preview_progress.setFormat("智能分割失败")
        self.smart_split_status.setText("分析失败，可调整参数后重试")
        self._log(f"智能分割失败：{message}")
        QMessageBox.warning(self, "智能分割失败", str(message))

    def _on_scene_split_cancelled(self):
        self.preview_progress.setValue(0)
        self.preview_progress.setFormat("智能分割已停止")
        self.smart_split_status.setText("分析已停止，原有切分结果未改变")
        self._log("智能分割已由用户停止")

    def _source_fps(self):
        return max(
            1.0,
            float(
                self.preview_metadata.get("fps")
                or ((self.current_project or {}).get("source_metadata") or {}).get(
                    "fps"
                )
                or 25.0
            ),
        )

    def _current_position_seconds(self):
        return max(0.0, float(self.preview_position_seconds or 0.0))

    def _decode_dimensions(self):
        size = self.preview_area.preview_container.size()
        width = size.width() if size.width() > 32 else self.preview_area.source_width
        height = size.height() if size.height() > 32 else self.preview_area.source_height
        maximum = max(width, height, 1)
        scale = min(1.0, 1280.0 / maximum)
        return max(2, int(width * scale)), max(2, int(height * scale))

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
                worker.wait(4000)

    def _cancel_seek_frame_worker(self, clear_pending=True):
        if clear_pending:
            self._pending_seek_seconds = None
            self.seek_preview_timer.stop()
        worker = self.seek_frame_worker
        self.seek_frame_worker = None
        if worker and worker.isRunning():
            worker.cancel()
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
            self._log(f"预览音频初始化失败：{exc}")
            return False
        return self.audio_device is not None

    def _stop_audio_worker(self, wait=True):
        worker = self.audio_worker
        self.audio_worker = None
        if worker and worker.isRunning():
            worker.cancel()
            if wait:
                worker.wait(4000)
        if self.audio_output:
            try:
                self.audio_output.stop()
            except Exception:
                pass
        self.audio_output = None
        self.audio_device = None

    def _start_audio_preview(self, source, start_seconds):
        self._stop_audio_worker(wait=True)
        if not self._ensure_audio_output():
            return
        worker = FFmpegAudioDecodeWorker(
            self.ffmpeg_path, source, start_seconds, self
        )
        worker.chunkReady.connect(self._on_audio_chunk_ready)
        worker.failed.connect(
            lambda message: self._log(f"预览音频提示：{message}") if message else None
        )
        worker.finished.connect(lambda: self._clear_audio_worker(worker))
        self.audio_worker = worker
        worker.start()

    def _clear_audio_worker(self, worker):
        if self.audio_worker is worker:
            self.audio_worker = None

    def _on_audio_chunk_ready(self, chunk):
        if chunk and self.audio_device:
            try:
                self.audio_device.write(chunk)
            except Exception as exc:
                self._log(f"预览音频写入失败：{exc}")

    def _start_software_preview(self, start_seconds=None):
        source = self._preview_source()
        if not os.path.isfile(source):
            return
        start = (
            self._current_position_seconds()
            if start_seconds is None
            else max(0.0, float(start_seconds))
        )
        if self._duration() > 0 and start >= self._duration():
            start = max(0.0, self._duration() - 0.05)
        self.seek_preview_timer.stop()
        self._stop_decode_worker()
        self._stop_audio_worker()
        width, height = self._decode_dimensions()
        worker = FFmpegVideoDecodeWorker(
            self.ffmpeg_path,
            source,
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
        worker.finished.connect(lambda: self._on_decode_finished(worker))
        self.decode_worker = worker
        self.preview_position_seconds = start
        self.preview_position_ms = int(round(start * 1000))
        self._set_preview_play_state(True)
        self._start_audio_preview(source, start)
        worker.start()

    def _on_decode_finished(self, worker):
        if self.decode_worker is worker:
            self.decode_worker = None
            self._stop_audio_worker(wait=False)
            self._set_preview_play_state(False)

    def _pause_preview(self):
        self._stop_decode_worker()
        self._stop_audio_worker()
        self._set_preview_play_state(False)

    def _stop_preview(self, reset_position=False):
        self.seek_preview_timer.stop()
        self._stop_decode_worker()
        self._stop_audio_worker()
        self._set_preview_play_state(False)
        if reset_position:
            self.preview_position_ms = 0
            self.preview_position_seconds = 0.0
            self.timeline.set_position(0)
            self._update_time_label()

    def toggle_playback(self):
        if not self.current_project:
            return
        if self.software_preview_playing or (
            self.decode_worker and self.decode_worker.isRunning()
        ):
            self._pause_preview()
        else:
            start = self._current_position_seconds()
            active = dict(self._active_preview_segment or {})
            if active:
                segment_start = float(active.get("start") or 0.0)
                segment_end = float(active.get("end") or 0.0)
                if start < segment_start - 0.002 or start >= segment_end - 0.02:
                    start = segment_start
            self._start_software_preview(start)

    def _on_software_frame_ready(self, worker, image, position_seconds):
        if self.decode_worker is not worker:
            return
        self.preview_area.show_frame(image)
        self.preview_position_seconds = max(0.0, float(position_seconds or 0.0))
        self.preview_position_ms = int(round(self.preview_position_seconds * 1000))
        self.preview_frame_index = self._frame_index_at_or_after(
            self.preview_position_seconds
        )
        self.timeline.set_position(self.preview_position_seconds)
        self._update_time_label()
        active = dict(self._active_preview_segment or {})
        if active and self._current_position_seconds() >= float(
            active.get("end") or 0.0
        ) - max(0.02, 0.5 / self._source_fps()):
            self._pause_preview()
            return
        if self._duration() and self._current_position_seconds() >= self._duration() - 0.02:
            self._pause_preview()

    def _on_software_preview_failed(self, message):
        self._set_preview_play_state(False)
        self.preview_area.show_message(f"内置解码预览失败：{message}")
        self._log(f"内置解码预览失败：{message}")

    def seek_to_seconds(self, seconds, exact_frame_index=None):
        if not self.current_project:
            return
        seconds = max(0.0, min(self._duration(), float(seconds or 0.0)))
        if exact_frame_index is None:
            self.preview_frame_index = self._frame_index_at_or_after(seconds)
        else:
            self.preview_frame_index = min(
                max(0, int(exact_frame_index)), len(self.frame_timestamps) - 1
            )
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
        if self.frame_timestamps:
            current_index = self.preview_frame_index
            if current_index is None:
                current_index = self._frame_index_at_or_after(
                    self._current_position_seconds()
                )
            target_index = min(
                max(0, int(current_index or 0) + (-1 if int(direction) < 0 else 1)),
                len(self.frame_timestamps) - 1,
            )
            self.seek_to_seconds(
                self.frame_timestamps[target_index],
                exact_frame_index=target_index,
            )
            return
        worker = self.frame_timeline_worker
        if worker and worker.isRunning():
            if not self._frame_index_wait_logged:
                self._log("正在建立真实逐帧索引，请稍候再使用左右键。")
                self._frame_index_wait_logged = True
            return
        self.seek_to_seconds(
            frame_step_time(
                self._current_position_seconds(),
                self._source_fps(),
                direction,
                self._duration(),
            )
        )

    def _queue_seek_frame(self, seconds):
        self._pending_seek_seconds = max(0.0, float(seconds or 0.0))
        if not self.seek_preview_timer.isActive():
            self.seek_preview_timer.start(35)

    def _show_pending_seek_frame(self):
        worker = self.seek_frame_worker
        if worker and worker.isRunning():
            return
        source = self._preview_source()
        if self._pending_seek_seconds is None or not os.path.isfile(source):
            return
        seconds = self._pending_seek_seconds
        self._pending_seek_seconds = None
        width, height = self._decode_dimensions()
        worker = SingleFrameDecodeWorker(
            self.ffmpeg_path,
            source,
            seconds,
            width,
            height,
            timeout=12,
            parent=self,
        )
        worker.ready.connect(
            lambda image, decoded_seconds, elapsed, active=worker:
            self._on_seek_frame_ready(
                active,
                image,
                decoded_seconds,
                elapsed,
            )
        )
        worker.failed.connect(
            lambda decoded_seconds, message, active=worker:
            self._on_seek_frame_failed(active, decoded_seconds, message)
        )
        worker.finished.connect(
            lambda active=worker: self._on_seek_frame_finished(active)
        )
        self.seek_frame_worker = worker
        worker.start()

    def _on_seek_frame_ready(self, worker, image, seconds, elapsed):
        if self.seek_frame_worker is not worker:
            return
        tolerance = max(0.002, 0.75 / self._source_fps())
        if abs(float(seconds) - self._current_position_seconds()) <= tolerance:
            self.preview_area.show_frame(image)
        self._last_seek_decode_error = ""
        if elapsed > 2.0:
            self._log(f"当前帧后台解码耗时 {elapsed:.2f} 秒，正在优先追赶最新位置。")

    def _on_seek_frame_failed(self, worker, seconds, message):
        if self.seek_frame_worker is not worker:
            return
        text = str(message or "无法解码当前画面")
        if text != self._last_seek_decode_error:
            self._last_seek_decode_error = text
            self._log(f"当前帧后台预览失败：{text}")

    def _on_seek_frame_finished(self, worker):
        if self.seek_frame_worker is worker:
            self.seek_frame_worker = None
        if self._pending_seek_seconds is not None:
            QTimer.singleShot(0, self._show_pending_seek_frame)

    def _update_time_label(self):
        frame_text = ""
        if self.frame_timestamps and self.preview_frame_index is not None:
            frame_text = (
                f" · 帧 {int(self.preview_frame_index) + 1}/"
                f"{len(self.frame_timestamps)}"
            )
        self.time_label.setText(
            f"{format_time(self._current_position_seconds())} / "
            f"{format_time(self._duration())}{frame_text}"
        )

    def set_in_point(self):
        if not self.current_project:
            return
        self.timeline.in_point = self._current_position_seconds()
        self.timeline.update()
        self._on_points_changed(self.timeline.in_point, self.timeline.out_point)

    def set_out_point(self):
        if not self.current_project:
            return
        self.timeline.out_point = self._current_position_seconds()
        self.timeline.update()
        self._on_points_changed(self.timeline.in_point, self.timeline.out_point)

    def clear_points(self):
        self.slot_table.clearSelection()
        self.slot_table.setCurrentCell(-1, -1)
        self.slot_table.clearFocus()
        self.timeline.set_points(None, None)
        self.slot_label_edit.clear()

    def _selected_overview_segment(self):
        rows = self.segment_overview_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.segment_overview_table.item(rows[0].row(), 0)
        value = item.data(Qt.UserRole) if item else None
        return dict(value) if isinstance(value, dict) else None

    def _on_segment_overview_selected(self):
        if self._refreshing_segment_overview:
            return
        segment = self._selected_overview_segment()
        if not segment:
            self.clear_segment_preview_selection()
            return
        self._pause_preview()
        self._active_preview_segment = dict(segment)
        segment_id = int(segment.get("id") or 0)
        self._select_segment_mapping_row(segment_id)
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or 0.0)
        self.timeline.set_selected_range(start, end, f"{segment_id:03d}")
        self.segment_playback_hint.setText(
            f"已选择片段 {segment_id:03d}，播放范围 "
            f"{format_time(start)} - {format_time(end)}"
        )
        self.seek_to_seconds(start)

    def clear_segment_preview_selection(self):
        had_selection = bool(self._active_preview_segment)
        if had_selection and self.software_preview_playing:
            self._pause_preview()
        self._active_preview_segment = None
        self.segment_overview_table.blockSignals(True)
        self.segment_overview_table.clearSelection()
        self.segment_overview_table.setCurrentCell(-1, -1)
        self.segment_overview_table.blockSignals(False)
        self.timeline.set_selected_range()
        self.segment_playback_hint.setText("选中片段后，播放键只播放该片段")

    def show_full_timeline(self):
        self.clear_points()
        self.clear_segment_preview_selection()
        self.timeline.set_segments(self._timeline_slots())
        self._log("已返回完整轨道，当前显示全部切分片段。")

    def _seek_from_timeline(self, seconds):
        self.clear_segment_preview_selection()
        if self._selected_slot_id():
            self.clear_points()
        self.seek_to_seconds(seconds)

    def _on_points_changed(self, start, end):
        if start >= 0 and end >= 0:
            low, high = sorted((start, end))
            self.point_label.setText(
                f"入点：{format_time(low)}    出点：{format_time(high)}    "
                f"替换时长：{high - low:.3f} 秒"
            )
        else:
            start_text = format_time(start) if start >= 0 else "--"
            end_text = format_time(end) if end >= 0 else "--"
            self.point_label.setText(
                f"入点：{start_text}    出点：{end_text}    替换时长：--"
            )

    def _candidate_range(self):
        start = float(self.timeline.in_point)
        end = float(self.timeline.out_point)
        if start < 0 or end < 0:
            return None
        start, end = sorted((start, end))
        end = min(self._duration(), end)
        return (start, end) if end - start >= 0.10 else None

    def _selected_slot_id(self):
        rows = self.slot_table.selectionModel().selectedRows()
        if not rows:
            return 0
        item = self.slot_table.item(rows[0].row(), 0)
        return int(item.data(Qt.UserRole) or 0) if item else 0

    def _slot_by_id(self, slot_id):
        return next(
            (
                slot
                for slot in (self.current_project or {}).get("slots", [])
                if isinstance(slot, dict) and int(slot.get("id") or 0) == int(slot_id)
            ),
            None,
        )

    def add_slot(self):
        if not self.current_project:
            QMessageBox.warning(self, "未选择项目", "请先导入竞品原片。")
            return
        candidate = self._candidate_range()
        if not candidate:
            QMessageBox.warning(self, "标记不完整", "请设置有效的入点和出点。")
            return
        try:
            self.current_project, slot = self.store.add_slot(
                self.current_project,
                candidate[0],
                candidate[1],
                self.slot_label_edit.text().strip(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "无法添加区间", str(exc))
            return
        self._refresh_all_project_views(select_slot_id=int(slot.get("id") or 0))
        self.segment_tabs.setCurrentIndex(1)
        self.clear_points()
        self._log(
            f"已添加切分区间{int(slot.get('id') or 0):03d}："
            f"{format_time(slot.get('start'))} - {format_time(slot.get('end'))}"
        )

    def update_slot(self):
        slot_id = self._selected_slot_id()
        candidate = self._candidate_range()
        if not slot_id:
            QMessageBox.information(self, "未选择区间", "请先在表格中选择一个切分区间。")
            return
        if not candidate:
            QMessageBox.warning(self, "标记不完整", "请设置新的入点和出点。")
            return
        try:
            self.current_project, slot = self.store.update_slot(
                self.current_project,
                slot_id,
                candidate[0],
                candidate[1],
                label=self.slot_label_edit.text().strip() or None,
            )
        except Exception as exc:
            QMessageBox.warning(self, "无法更新区间", str(exc))
            return
        self._refresh_all_project_views(select_slot_id=slot_id)
        self.segment_tabs.setCurrentIndex(1)
        self._log(f"已更新切分区间{slot_id:03d}")

    def _save_slot_reference(self, slot):
        source = str((self.current_project or {}).get("source_path") or "")
        target = os.path.join(
            self.store.project_dir(self.current_project),
            str(slot.get("reference_image") or ""),
        )
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            image = decode_single_frame(
                self.ffmpeg_path,
                source,
                (float(slot.get("start") or 0.0) + float(slot.get("end") or 0.0))
                / 2.0,
                720,
                1280,
                timeout=12,
            )
            image.save(target, "JPG", 90)
        except Exception as exc:
            self._log(f"槽位参考帧保存失败，不影响生成：{exc}")

    def delete_selected_slot(self):
        slot_id = self._selected_slot_id()
        if not slot_id or not self.current_project:
            return
        answer = QMessageBox.question(
            self,
            "删除切分区间",
            f"确定删除切分区间{slot_id:03d}吗？已填写的片段路径不会删除路径中的原素材。",
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.current_project = self.store.archive_slot(
                self.current_project, slot_id
            )
        except Exception as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self.clear_points()
        self._refresh_all_project_views()
        self._log(f"已删除切分区间{slot_id:03d}")

    def _on_slot_selected(self):
        slot = self._slot_by_id(self._selected_slot_id())
        if not slot:
            return
        self.timeline.set_points(slot.get("start"), slot.get("end"))
        self.slot_label_edit.setText(str(slot.get("label") or ""))
        self.seek_to_seconds(float(slot.get("start") or 0.0))

    def preview_selected_slot(self):
        slot = self._slot_by_id(self._selected_slot_id())
        if not slot:
            return
        self.seek_to_seconds(float(slot.get("start") or 0.0))
        self._start_software_preview(float(slot.get("start") or 0.0))
        QTimer.singleShot(
            max(100, int(float(slot.get("duration") or 0.0) * 1000)),
            self._pause_preview,
        )

    def _refresh_all_project_views(self, select_slot_id=0):
        if not self.current_project:
            return
        self._refresh_slot_table(select_slot_id)
        self._refresh_segment_mapping_table()
        self._refresh_segment_overview_table()
        self.timeline.set_segments(self._timeline_slots())
        self._refresh_project_summary()
        self._refresh_render_summary()
        self._refresh_render_buttons()

    def _refresh_segment_overview_table(self):
        segments = self.store.timeline_segments(self.current_project or {})
        active = dict(self._active_preview_segment or {})
        self._refreshing_segment_overview = True
        self.segment_overview_table.blockSignals(True)
        self.segment_overview_table.setRowCount(len(segments))
        selected_row = -1
        for row, segment in enumerate(segments):
            segment_id = int(segment.get("id") or 0)
            folder = str(segment.get("folder") or "").strip()
            start_text = format_time(segment.get("start"))
            end_text = format_time(segment.get("end"))
            if start_text.startswith("00:") and end_text.startswith("00:"):
                start_text = start_text[3:]
                end_text = end_text[3:]
            values = [
                f"{segment_id:03d}",
                f"{start_text} - {end_text}",
                f"{float(segment.get('duration') or 0.0):.3f} 秒",
                "路径素材替换" if folder else "保留原片",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignLeft | Qt.AlignVCenter
                    if column == 3
                    else Qt.AlignCenter
                )
                if column == 0:
                    item.setData(Qt.UserRole, dict(segment))
                    item.setForeground(QColor("#F8FAFC"))
                    item.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
                elif column == 3:
                    item.setForeground(
                        QColor("#86EFAC") if folder else QColor("#CBD5E1")
                    )
                self.segment_overview_table.setItem(row, column, item)
            if (
                active
                and abs(
                    float(active.get("start") or 0.0)
                    - float(segment.get("start") or 0.0)
                )
                <= 0.002
                and abs(
                    float(active.get("end") or 0.0)
                    - float(segment.get("end") or 0.0)
                )
                <= 0.002
            ):
                selected_row = row
        self.segment_overview_table.blockSignals(False)
        self._refreshing_segment_overview = False
        self.segment_tabs.setTabText(0, f"完整片段（{len(segments)}）")
        if selected_row >= 0:
            self.segment_overview_table.selectRow(selected_row)
        elif active:
            self.clear_segment_preview_selection()

    def _refresh_slot_table(self, select_slot_id=0):
        slots = sorted(
            [
                item
                for item in (self.current_project or {}).get("slots", [])
                if isinstance(item, dict) and item.get("enabled", True)
            ],
            key=lambda item: float(item.get("start") or 0.0),
        )
        self.slot_table.blockSignals(True)
        self.slot_table.setRowCount(len(slots))
        selected_row = -1
        for row, slot in enumerate(slots):
            values = [
                f"{int(slot.get('id') or 0):03d}",
                str(slot.get("label") or ""),
                format_time(slot.get("start")),
                format_time(slot.get("end")),
                f"{float(slot.get('duration') or 0.0):.3f}秒",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.UserRole, int(slot.get("id") or 0))
                self.slot_table.setItem(row, column, item)
            if int(slot.get("id") or 0) == int(select_slot_id):
                selected_row = row
        self.slot_table.blockSignals(False)
        self.segment_tabs.setTabText(1, f"人工标记（{len(slots)}）")
        if selected_row >= 0:
            self.slot_table.selectRow(selected_row)

    def _refresh_segment_mapping_table(self):
        if not self.current_project:
            self.segment_mapping_table.setRowCount(0)
            return
        selected_segment = self._selected_segment_mapping()
        selected_id = int((selected_segment or {}).get("id") or 0)
        segments = self.store.timeline_segments(self.current_project)
        self.current_project["segment_mappings"] = segments
        self._refreshing_segment_table = True
        self._segment_merge_rows = []
        self.segment_mapping_table.setRowCount(len(segments))
        material_counts = {}
        replacement_count = 0
        ready_count = 0
        for row, segment in enumerate(segments):
            start = float(segment.get("start") or 0.0)
            end = float(segment.get("end") or 0.0)
            folder = str(segment.get("folder") or "")
            if not folder:
                status_text = "保留原片"
                status_color = "#94A3B8"
                material_text = "原片"
            elif not os.path.isdir(folder):
                replacement_count += 1
                status_text = "路径不存在"
                status_color = "#FCA5A5"
                material_text = "缺失"
            else:
                replacement_count += 1
                key = os.path.normcase(folder)
                if key not in material_counts:
                    material_counts[key] = len(scan_video_files(folder))
                count = material_counts[key]
                if count:
                    ready_count += 1
                    status_text = f"替换：{count}个素材"
                    status_color = "#86EFAC"
                    material_text = f"{count}个"
                else:
                    status_text = "目录内没有视频"
                    status_color = "#FCA5A5"
                    material_text = "0个"

            check_cell = QWidget()
            check_layout = QHBoxLayout(check_cell)
            check_layout.setContentsMargins(0, 0, 0, 0)
            check_layout.setAlignment(Qt.AlignCenter)
            merge_check = QCheckBox()
            merge_check.setToolTip("勾选两个或更多连续片段后，可在下方合并")
            check_layout.addWidget(merge_check)
            self._segment_merge_rows.append((merge_check, dict(segment)))

            segment_item = QTableWidgetItem(
                f"{int(segment.get('id') or 0):03d}"
            )
            segment_item.setData(Qt.UserRole, dict(segment))
            segment_item.setTextAlignment(Qt.AlignCenter)
            segment_item.setForeground(QColor(status_color))
            segment_item.setToolTip(
                f"片段{int(segment.get('id') or 0):03d}，时长 {end - start:.3f} 秒"
            )
            time_item = QTableWidgetItem(
                f"{format_time(start)} - {format_time(end)}"
            )
            time_item.setTextAlignment(Qt.AlignCenter)
            time_item.setToolTip(f"时长 {end - start:.3f} 秒")
            path_editor = QLineEdit(folder)
            path_editor.setObjectName("segmentPathEditor")
            path_editor.setPlaceholderText("留空=保留原片")
            path_editor.setClearButtonEnabled(True)
            path_editor.setProperty("segment_mapping_row", row)
            path_editor.setProperty("segment_start", start)
            path_editor.setProperty("segment_end", end)
            path_editor.setProperty("original_path", folder)
            path_editor.setToolTip(
                f"{status_text}\n{folder or '路径为空，生成时保留这一段原片'}"
            )
            path_editor.editingFinished.connect(
                lambda editor=path_editor: self._save_segment_path_editor(editor)
            )
            path_editor.installEventFilter(self)
            material_item = QTableWidgetItem(material_text)
            material_item.setTextAlignment(Qt.AlignCenter)
            material_item.setForeground(QColor(status_color))
            material_item.setToolTip(status_text)

            self.segment_mapping_table.setRowHeight(row, 36)
            self.segment_mapping_table.setCellWidget(row, 0, check_cell)
            self.segment_mapping_table.setItem(row, 1, segment_item)
            self.segment_mapping_table.setItem(row, 2, time_item)
            self.segment_mapping_table.setCellWidget(row, 3, path_editor)
            self.segment_mapping_table.setItem(row, 4, material_item)
        if segments:
            selected_row = next(
                (
                    row
                    for row, segment in enumerate(segments)
                    if int(segment.get("id") or 0) == selected_id
                ),
                0,
            )
            self.segment_mapping_table.selectRow(selected_row)
        self._refreshing_segment_table = False
        self.merge_segments_button.setEnabled(len(segments) >= 2)
        if not segments:
            self.material_status_label.setText(
                "当前没有分割片段。请使用智能分割，或设置入点和出点后添加切分区间。"
            )
        else:
            self.material_status_label.setText(
                f"完整时间线共 {len(segments)} 个片段；"
                f"填写路径 {replacement_count} 个，其中 {ready_count} 个素材就绪；"
                f"其余 {len(segments) - replacement_count} 个保持原片。"
            )

    def _selected_segment_mapping(self):
        row = self.segment_mapping_table.currentRow()
        if row < 0:
            return None
        item = self.segment_mapping_table.item(row, 1)
        segment = item.data(Qt.UserRole) if item else None
        return dict(segment) if isinstance(segment, dict) else None

    def _select_segment_mapping_row(self, segment_id):
        segment_id = int(segment_id or 0)
        if segment_id <= 0:
            return
        for row in range(self.segment_mapping_table.rowCount()):
            item = self.segment_mapping_table.item(row, 1)
            segment = item.data(Qt.UserRole) if item else None
            if int((segment or {}).get("id") or 0) != segment_id:
                continue
            previous = self.segment_mapping_table.blockSignals(True)
            self.segment_mapping_table.setCurrentCell(row, 1)
            self.segment_mapping_table.selectRow(row)
            self.segment_mapping_table.scrollToItem(
                item,
                QAbstractItemView.PositionAtCenter,
            )
            self.segment_mapping_table.blockSignals(previous)
            return

    def _select_segment_overview_row(self, segment_id):
        segment_id = int(segment_id or 0)
        if segment_id <= 0:
            return
        for row in range(self.segment_overview_table.rowCount()):
            item = self.segment_overview_table.item(row, 0)
            segment = item.data(Qt.UserRole) if item else None
            if int((segment or {}).get("id") or 0) != segment_id:
                continue
            current = self._selected_overview_segment()
            if int((current or {}).get("id") or 0) == segment_id:
                if int((self._active_preview_segment or {}).get("id") or 0) != segment_id:
                    self._on_segment_overview_selected()
                return
            self.segment_overview_table.selectRow(row)
            self.segment_overview_table.scrollToItem(
                item,
                QAbstractItemView.PositionAtCenter,
            )
            return

    def _activate_segment_mapping_row(self, row):
        if row < 0 or row >= self.segment_mapping_table.rowCount():
            return
        self.segment_mapping_table.setCurrentCell(row, 1)
        self.segment_mapping_table.selectRow(row)
        self._on_segment_mapping_selected()

    def _on_segment_mapping_selected(self):
        if self._refreshing_segment_table:
            return
        segment = self._selected_segment_mapping()
        if segment:
            self._select_segment_overview_row(segment.get("id"))

    def _save_segment_path_editor(self, editor):
        if self._refreshing_segment_table or not self.current_project:
            return
        start = float(editor.property("segment_start") or 0.0)
        end = float(editor.property("segment_end") or 0.0)
        original = str(editor.property("original_path") or "").strip()
        folder = str(editor.text() or "").strip().strip('"')
        if folder == original:
            return
        self._save_segment_folder(start, end, folder)

    def _require_selected_segment(self):
        segment = self._selected_segment_mapping()
        if segment:
            return segment
        QMessageBox.information(self, "请选择片段", "请先在完整片段表格中选中一行。")
        return None

    def _locate_selected_segment(self):
        segment = self._require_selected_segment()
        if segment:
            self._locate_segment(segment.get("start"), segment.get("end"))

    def clear_segment_merge_selection(self):
        for checkbox, _segment in self._segment_merge_rows:
            checkbox.setChecked(False)

    def merge_selected_segments(self):
        if not self.current_project:
            return
        selected = [
            segment
            for checkbox, segment in self._segment_merge_rows
            if checkbox.isChecked()
        ]
        if len(selected) < 2:
            QMessageBox.information(
                self,
                "请选择片段",
                "请勾选两个或更多编号连续的片段后再合并。",
            )
            return
        selected.sort(key=lambda item: int(item.get("id") or 0))
        selected_ids = [int(item.get("id") or 0) for item in selected]
        if selected_ids != list(range(selected_ids[0], selected_ids[-1] + 1)):
            QMessageBox.warning(self, "不能合并", "只能合并编号连续的相邻片段。")
            return

        folders = [
            str(item.get("folder") or "").strip()
            for item in selected
            if str(item.get("folder") or "").strip()
        ]
        unique_folders = {
            os.path.normcase(os.path.abspath(folder))
            for folder in folders
        }
        path_note = "所选片段没有填写路径，合并后继续保留原片。"
        if len(unique_folders) == 1:
            path_note = "所选片段使用同一路径，合并后会自动保留该路径。"
        elif len(unique_folders) > 1:
            path_note = (
                "所选片段填写了不同路径，合并后将保留最前面片段的有效路径：\n"
                f"{folders[0]}"
            )
        answer = QMessageBox.question(
            self,
            "合并相邻片段",
            f"确定合并片段{selected_ids[0]:03d}至片段{selected_ids[-1]:03d}吗？\n\n"
            f"{path_note}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.current_project, merged, _folders = self.store.merge_segments(
                self.current_project,
                selected_ids,
            )
        except Exception as exc:
            QMessageBox.warning(self, "合并失败", str(exc))
            return
        self.clear_points()
        self._refresh_all_project_views()
        self._log(
            f"已合并片段{selected_ids[0]:03d}至{selected_ids[-1]:03d}："
            f"{format_time(merged.get('start'))} - {format_time(merged.get('end'))}"
        )

    def _save_segment_folder(self, start, end, folder):
        if self._refreshing_segment_table or not self.current_project:
            return
        try:
            self.current_project, _ = self.store.update_segment_folder(
                self.current_project, start, end, folder
            )
        except Exception as exc:
            QMessageBox.warning(self, "路径保存失败", str(exc))
            return False
        self._refresh_segment_mapping_table()
        self._refresh_segment_overview_table()
        self.timeline.set_segments(self._timeline_slots())
        self._refresh_project_summary()
        self._refresh_render_summary()
        return True

    def _locate_segment(self, start, end):
        if self._selected_slot_id():
            self.clear_points()
        self.seek_to_seconds(float(start))
        self._log(
            f"已定位片段：{format_time(start)} - {format_time(end)}"
        )

    def _refresh_material_slot_list(self, select_slot_id=0):
        current_id = select_slot_id
        if not current_id and self.material_slot_list.currentItem():
            current_id = int(
                self.material_slot_list.currentItem().data(Qt.UserRole) or 0
            )
        self.material_slot_list.blockSignals(True)
        self.material_slot_list.clear()
        target_row = -1
        for row, slot in enumerate((self.current_project or {}).get("slots", [])):
            if not isinstance(slot, dict) or not slot.get("enabled", True):
                continue
            materials = self.store.slot_materials(self.current_project, slot)
            item = QListWidgetItem(
                f"槽位{int(slot.get('id') or 0):03d}  {slot.get('label', '')}\n"
                f"{format_time(slot.get('start'))} - {format_time(slot.get('end'))}  "
                f"· {len(materials)}个素材"
            )
            item.setData(Qt.UserRole, int(slot.get("id") or 0))
            self.material_slot_list.addItem(item)
            if int(slot.get("id") or 0) == int(current_id):
                target_row = self.material_slot_list.count() - 1
        self.material_slot_list.blockSignals(False)
        if target_row < 0 and self.material_slot_list.count() > 0:
            target_row = 0
        if target_row >= 0:
            self.material_slot_list.setCurrentRow(target_row)
            self._on_material_slot_selected(
                self.material_slot_list.currentItem(), None
            )
        else:
            self._clear_material_slot_details()

    def _select_material_slot(self, slot_id):
        for row in range(self.material_slot_list.count()):
            item = self.material_slot_list.item(row)
            if item and int(item.data(Qt.UserRole) or 0) == int(slot_id):
                self.material_slot_list.setCurrentRow(row)
                return

    def _material_selected_slot(self):
        item = self.material_slot_list.currentItem()
        return self._slot_by_id(int(item.data(Qt.UserRole) or 0)) if item else None

    def _on_material_slot_selected(self, current, previous):
        slot = self._material_selected_slot()
        if not slot:
            self._clear_material_slot_details()
            return
        self._refreshing_slot_controls = True
        self.material_slot_name.setText(
            f"槽位{int(slot.get('id') or 0):03d} · {slot.get('label', '')}"
        )
        self.material_slot_range.setText(
            f"{format_time(slot.get('start'))} - {format_time(slot.get('end'))} "
            f"（{float(slot.get('duration') or 0.0):.3f}秒）"
        )
        index = self.slot_mode_combo.findData(
            str(slot.get("mode") or "dedicated_fallback")
        )
        self.slot_mode_combo.setCurrentIndex(max(0, index))
        self.external_dir_edit.setText(str(slot.get("external_dir") or ""))
        reference_path = os.path.join(
            self.store.project_dir(self.current_project),
            str(slot.get("reference_image") or ""),
        )
        pixmap = QPixmap(reference_path)
        if pixmap.isNull():
            self.reference_preview.setPixmap(QPixmap())
            self.reference_preview.setText("暂无原片参考帧")
        else:
            self.reference_preview.setText("")
            self.reference_preview.setPixmap(
                pixmap.scaled(
                    max(240, self.reference_preview.width() - 12),
                    max(180, self.reference_preview.height() - 12),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
        self._refreshing_slot_controls = False
        self._refresh_material_counts(slot)

    def _clear_material_slot_details(self):
        self.material_slot_name.setText("--")
        self.material_slot_range.setText("--")
        self.external_dir_edit.clear()
        self.reference_preview.setPixmap(QPixmap())
        self.reference_preview.setText("添加替换区间后显示原片参考帧")
        self.material_status_label.setText("选择一个槽位后查看素材状态。")

    def _refresh_material_counts(self, slot=None):
        if not self.current_project:
            self.shared_count_label.setText("产品公共素材池：0 个视频")
            return
        shared_count = len(scan_video_files(self.store.shared_dir(self.current_project)))
        self.shared_count_label.setText(f"产品公共素材池：{shared_count} 个视频")
        slot = slot or self._material_selected_slot()
        if not slot:
            return
        dedicated_count = len(
            scan_video_files(self.store.slot_dir(self.current_project, slot))
        )
        external_count = len(scan_video_files(str(slot.get("external_dir") or "")))
        usable_count = len(self.store.slot_materials(self.current_project, slot))
        state = "可生成" if usable_count else "缺少可用产品视频"
        self.material_status_label.setText(
            f"专属文件夹 {dedicated_count} 个，外部目录 {external_count} 个，"
            f"公共池 {shared_count} 个；当前规则实际可用 {usable_count} 个。状态：{state}。"
        )

    def _save_slot_material_mode(self):
        if self._refreshing_slot_controls or not self.current_project:
            return
        slot = self._material_selected_slot()
        if not slot:
            return
        try:
            self.current_project, _ = self.store.update_slot(
                self.current_project,
                int(slot.get("id") or 0),
                slot.get("start"),
                slot.get("end"),
                mode=self.slot_mode_combo.currentData(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._refresh_all_project_views(select_slot_id=int(slot.get("id") or 0))

    def bind_external_dir(self):
        slot = self._material_selected_slot()
        if not slot or not self.current_project:
            return
        directory = QFileDialog.getExistingDirectory(
            self, "绑定当前槽位已有素材目录", workspace_path("素材")
        )
        if not directory:
            return
        try:
            self.current_project, _ = self.store.update_slot(
                self.current_project,
                int(slot.get("id") or 0),
                slot.get("start"),
                slot.get("end"),
                external_dir=directory,
            )
        except Exception as exc:
            QMessageBox.warning(self, "绑定失败", str(exc))
            return
        self._refresh_all_project_views(select_slot_id=int(slot.get("id") or 0))

    def clear_external_dir(self):
        slot = self._material_selected_slot()
        if not slot or not self.current_project:
            return
        self.current_project, _ = self.store.update_slot(
            self.current_project,
            int(slot.get("id") or 0),
            slot.get("start"),
            slot.get("end"),
            external_dir="",
        )
        self._refresh_all_project_views(select_slot_id=int(slot.get("id") or 0))

    @staticmethod
    def _copy_videos(paths, target_dir):
        os.makedirs(target_dir, exist_ok=True)
        copied = 0
        for source in paths or []:
            if not os.path.isfile(source) or not source.lower().endswith(VIDEO_EXTENSIONS):
                continue
            stem, extension = os.path.splitext(os.path.basename(source))
            target = os.path.join(target_dir, os.path.basename(source))
            suffix = 2
            while os.path.exists(target):
                target = os.path.join(target_dir, f"{stem}_{suffix}{extension}")
                suffix += 1
            shutil.copy2(source, target)
            copied += 1
        return copied

    def _choose_material_files(self, title):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            title,
            workspace_path("素材"),
            "视频文件 (*.mp4 *.mov *.m4v *.avi *.mkv *.webm *.ts *.mts *.m2ts)",
        )
        return paths

    def import_to_dedicated(self):
        slot = self._material_selected_slot()
        if not slot or not self.current_project:
            return
        paths = self._choose_material_files("导入当前槽位专属产品素材")
        if not paths:
            return
        try:
            copied = self._copy_videos(
                paths, self.store.slot_dir(self.current_project, slot)
            )
        except OSError as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self.refresh_material_status()
        self._log(f"已导入 {copied} 个槽位专属素材")

    def import_to_shared(self):
        if not self.current_project:
            return
        paths = self._choose_material_files("导入产品公共素材")
        if not paths:
            return
        try:
            copied = self._copy_videos(
                paths, self.store.shared_dir(self.current_project)
            )
        except OSError as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self.refresh_material_status()
        self._log(f"已导入 {copied} 个产品公共素材")

    def refresh_material_status(self):
        self._refresh_all_project_views()
        self.refresh_project_list(
            select_project_path=str(
                (self.current_project or {}).get("_project_path") or ""
            )
        )

    def open_project_dir(self):
        if self.current_project:
            open_path(self.store.project_dir(self.current_project))

    def open_selected_slot_dir(self):
        slot = self._material_selected_slot() or self._slot_by_id(
            self._selected_slot_id()
        )
        if slot and self.current_project:
            path = self.store.slot_dir(self.current_project, slot)
            os.makedirs(path, exist_ok=True)
            open_path(path)

    def open_shared_dir(self):
        if self.current_project:
            path = self.store.shared_dir(self.current_project)
            os.makedirs(path, exist_ok=True)
            open_path(path)

    def open_output_dir(self):
        if self.current_project:
            path = self.store.output_dir(self.current_project)
            os.makedirs(path, exist_ok=True)
            open_path(path)

    def _refresh_project_summary(self):
        if not self.current_project:
            return
        issues, status = self.store.validate(self.current_project)
        mark_count = len(
            [
                item
                for item in self.current_project.get("slots", [])
                if isinstance(item, dict) and item.get("enabled", True)
            ]
        )
        replacements = [item for item in status if item.get("folder")]
        ready_count = sum(1 for item in replacements if item.get("ready"))
        smart_count = len(
            [
                value
                for value in self.current_project.get("cut_points", [])
                if isinstance(value, (int, float))
            ]
        )
        self.project_summary.setText(
            f"原片：{os.path.basename(str(self.current_project.get('source_path') or ''))}\n"
            f"人工标记：{mark_count} 个 · 智能切点：{smart_count} 个 · "
            f"完整片段：{len(status)} 个\n"
            f"路径替换：{ready_count}/{len(replacements)} 个就绪\n"
            f"{'项目可生成' if not issues else '待处理：' + '；'.join(issues)}"
        )

    def _refresh_render_summary(self):
        if not self.current_project:
            self.render_project_label.setText("尚未选择项目")
            self.render_summary_label.setText("--")
            self.intro_dir_edit.clear()
            self.intro_count_label.setText(
                "选择项目后可设置独立片头；片头结束后正文和正文音频才开始。"
            )
            self.audio_dir_edit.clear()
            self.audio_count_label.setText(
                "选择项目后可设置整条替换音频；成片时长由替换音频决定。"
            )
            return
        settings = dict(self.current_project.get("settings") or {})
        intro_enabled = bool(settings.get("intro_enabled", False))
        intro_full_play = bool(settings.get("intro_full_play", True))
        intro_duration = float(settings.get("intro_duration") or 3.0)
        self.intro_enabled_check.blockSignals(True)
        self.intro_enabled_check.setChecked(intro_enabled)
        self.intro_enabled_check.blockSignals(False)
        self.intro_full_play_check.blockSignals(True)
        self.intro_full_play_check.setChecked(intro_full_play)
        self.intro_full_play_check.blockSignals(False)
        self.intro_duration_spin.blockSignals(True)
        self.intro_duration_spin.setValue(intro_duration)
        self.intro_duration_spin.blockSignals(False)
        self.intro_duration_spin.setEnabled(intro_enabled and not intro_full_play)
        intro_dir = self.store.intro_dir(self.current_project)
        intro_count = len(self.store.intro_materials(self.current_project))
        self.intro_dir_edit.setText(intro_dir)
        if intro_enabled:
            play_mode = (
                "完整播放"
                if intro_full_play
                else f"截取前 {intro_duration:.2f} 秒"
            )
            self.intro_count_label.setText(
                f"已启用独立片头，共扫描到 {intro_count} 个视频；{play_mode}。"
            )
        else:
            self.intro_count_label.setText(
                f"当前未启用独立片头；片头目录内已有 {intro_count} 个视频。"
            )
        saved_audio_mode = str(settings.get("audio_mode") or "source")
        audio_index = self.audio_mode_combo.findData(saved_audio_mode)
        self.audio_mode_combo.blockSignals(True)
        self.audio_mode_combo.setCurrentIndex(max(0, audio_index))
        self.audio_mode_combo.blockSignals(False)
        audio_dir = self.store.audio_dir(self.current_project)
        self.audio_dir_edit.setText(audio_dir)
        audio_count = len(self.store.audio_materials(self.current_project))
        if saved_audio_mode == "folder":
            self.audio_count_label.setText(
                f"已启用文件夹随机替换，共扫描到 {audio_count} 条音频。"
                "音频短则截短画面，音频长则循环完整视觉结构。"
            )
        else:
            self.audio_count_label.setText(
                f"当前保留竞品原音频；替换目录内已有 {audio_count} 条音频。"
            )
        issues, status = self.store.validate(self.current_project)
        self.render_project_label.setText(str(self.current_project.get("name") or ""))
        if issues:
            self.render_summary_label.setText("暂不能生成：" + "；".join(issues))
            self.render_summary_label.setStyleSheet("color:#FCA5A5;")
        else:
            replacements = [item for item in status if item.get("folder")]
            self.render_summary_label.setText(
                f"检查通过：完整时间线 {len(status)} 段，其中 "
                f"{len(replacements)} 段使用路径素材替换；"
                f"{'启用独立片头（' + str(intro_count) + ' 个候选）' if intro_enabled else '不添加片头'}；"
                f"{'随机使用 ' + str(audio_count) + ' 条替换音频' if saved_audio_mode == 'folder' else '保留竞品原音频'}；"
                f"将生成 {self.loops_spin.value()} 个视频。"
            )
            self.render_summary_label.setStyleSheet("color:#86EFAC;")
        saved_loops = int(settings.get("loops") or 0)
        if saved_loops > 0 and not self.loops_spin.hasFocus():
            self.loops_spin.blockSignals(True)
            self.loops_spin.setValue(saved_loops)
            self.loops_spin.blockSignals(False)
        saved_transition = LEGACY_TRANSITION_MODES.get(
            str(settings.get("transition_mode") or "none"),
            str(settings.get("transition_mode") or "none"),
        )
        transition_index = self.transition_mode_combo.findData(saved_transition)
        self.transition_mode_combo.blockSignals(True)
        self.transition_mode_combo.setCurrentIndex(max(0, transition_index))
        self.transition_mode_combo.blockSignals(False)
        self.transition_duration_spin.blockSignals(True)
        self.transition_duration_spin.setValue(
            float(settings.get("transition_duration") or 0.25)
        )
        self.transition_duration_spin.blockSignals(False)
        self.transition_duration_spin.setEnabled(saved_transition != "none")
        smart_sfx_enabled = bool(settings.get("smart_sfx_enabled", False))
        smart_sfx_strength = str(
            settings.get("smart_sfx_strength") or "standard"
        )
        smart_sfx_index = self.smart_sfx_strength_combo.findData(
            smart_sfx_strength
        )
        self.smart_sfx_check.blockSignals(True)
        self.smart_sfx_check.setChecked(smart_sfx_enabled)
        self.smart_sfx_check.blockSignals(False)
        self.smart_sfx_strength_combo.blockSignals(True)
        self.smart_sfx_strength_combo.setCurrentIndex(
            smart_sfx_index if smart_sfx_index >= 0 else 1
        )
        self.smart_sfx_strength_combo.blockSignals(False)
        self.smart_sfx_strength_combo.setEnabled(smart_sfx_enabled)
        self._prepare_render_rows()

    def _on_intro_settings_changed(self, *_args):
        enabled = self.intro_enabled_check.isChecked()
        full_play = self.intro_full_play_check.isChecked()
        self.intro_duration_spin.setEnabled(enabled and not full_play)
        if not self.current_project:
            return
        settings = dict(self.current_project.get("settings") or {})
        settings["intro_enabled"] = enabled
        settings["intro_full_play"] = full_play
        settings["intro_duration"] = round(self.intro_duration_spin.value(), 3)
        self.current_project["settings"] = settings
        try:
            self.current_project = self.store.save(self.current_project)
        except Exception as exc:
            QMessageBox.warning(self, "片头设置保存失败", str(exc))
            return
        self._refresh_render_summary()

    def _choose_intro_dir(self):
        if not self.current_project:
            QMessageBox.information(self, "尚未选择项目", "请先导入竞品原片。")
            return
        initial = self.store.intro_dir(self.current_project)
        folder = QFileDialog.getExistingDirectory(self, "选择片头素材文件夹", initial)
        if not folder:
            return
        settings = dict(self.current_project.get("settings") or {})
        settings["intro_external_dir"] = os.path.abspath(folder)
        settings["intro_enabled"] = True
        self.current_project["settings"] = settings
        self.current_project = self.store.save(self.current_project)
        self._refresh_render_summary()

    def _use_project_intro_dir(self):
        if not self.current_project:
            return
        settings = dict(self.current_project.get("settings") or {})
        settings["intro_external_dir"] = ""
        self.current_project["settings"] = settings
        self.current_project = self.store.save(self.current_project)
        path = self.store.intro_dir(self.current_project)
        os.makedirs(path, exist_ok=True)
        self._refresh_render_summary()

    def _open_intro_dir(self):
        if not self.current_project:
            QMessageBox.information(self, "尚未选择项目", "请先导入竞品原片。")
            return
        path = self.store.intro_dir(self.current_project)
        os.makedirs(path, exist_ok=True)
        open_path(path)

    def _on_audio_settings_changed(self, *_args):
        if not self.current_project:
            return
        settings = dict(self.current_project.get("settings") or {})
        settings["audio_mode"] = str(self.audio_mode_combo.currentData() or "source")
        self.current_project["settings"] = settings
        try:
            self.current_project = self.store.save(self.current_project)
        except Exception as exc:
            QMessageBox.warning(self, "音频设置保存失败", str(exc))
            return
        self._refresh_render_summary()

    def _choose_audio_dir(self):
        if not self.current_project:
            QMessageBox.information(self, "尚未选择项目", "请先导入竞品原片。")
            return
        initial = self.store.audio_dir(self.current_project)
        folder = QFileDialog.getExistingDirectory(self, "选择替换音频文件夹", initial)
        if not folder:
            return
        settings = dict(self.current_project.get("settings") or {})
        settings["audio_external_dir"] = os.path.abspath(folder)
        settings["audio_mode"] = "folder"
        self.current_project["settings"] = settings
        self.current_project = self.store.save(self.current_project)
        self._refresh_render_summary()

    def _use_project_audio_dir(self):
        if not self.current_project:
            return
        settings = dict(self.current_project.get("settings") or {})
        settings["audio_external_dir"] = ""
        self.current_project["settings"] = settings
        self.current_project = self.store.save(self.current_project)
        path = self.store.audio_dir(self.current_project)
        os.makedirs(path, exist_ok=True)
        self._refresh_render_summary()

    def _open_audio_dir(self):
        if not self.current_project:
            QMessageBox.information(self, "尚未选择项目", "请先导入竞品原片。")
            return
        path = self.store.audio_dir(self.current_project)
        os.makedirs(path, exist_ok=True)
        open_path(path)

    def _on_transition_settings_changed(self, *_args):
        mode = str(self.transition_mode_combo.currentData() or "none")
        self.transition_duration_spin.setEnabled(mode != "none")
        if not self.current_project:
            return
        settings = dict(self.current_project.get("settings") or {})
        settings["transition_mode"] = mode
        settings["transition_duration"] = round(
            self.transition_duration_spin.value(),
            3,
        )
        self.current_project["settings"] = settings
        try:
            self.current_project = self.store.save(self.current_project)
        except Exception as exc:
            QMessageBox.warning(self, "转场设置保存失败", str(exc))
            return
        self._refresh_render_summary()

    def _on_sfx_settings_changed(self, *_args):
        enabled = self.smart_sfx_check.isChecked()
        self.smart_sfx_strength_combo.setEnabled(enabled)
        if not self.current_project:
            return
        settings = dict(self.current_project.get("settings") or {})
        settings["smart_sfx_enabled"] = enabled
        settings["smart_sfx_strength"] = str(
            self.smart_sfx_strength_combo.currentData() or "standard"
        )
        self.current_project["settings"] = settings
        try:
            self.current_project = self.store.save(self.current_project)
        except Exception as exc:
            QMessageBox.warning(self, "智能音效设置保存失败", str(exc))
            return
        self._refresh_render_summary()

    def _on_loops_changed(self, value):
        if self.current_project:
            settings = dict(self.current_project.get("settings") or {})
            settings["loops"] = int(value)
            self.current_project["settings"] = settings
            self.current_project = self.store.save(self.current_project)
        self._prepare_render_rows()
        if self.current_project:
            issues, status = self.store.validate(self.current_project)
            if not issues:
                replacements = [item for item in status if item.get("folder")]
                self.render_summary_label.setText(
                    f"检查通过：完整时间线 {len(status)} 段，其中 "
                    f"{len(replacements)} 段使用路径素材替换；"
                    f"将生成 {value} 个视频。"
                )

    def _prepare_render_rows(self, planning=False, project=None, loops=None):
        if self.render_worker and self.render_worker.isRunning():
            return
        if self.render_queue_paused and self.render_queue:
            return
        target_project = project or self.current_project
        loops = max(
            1,
            int(loops if loops is not None else self.loops_spin.value()),
        )
        self.render_table.setRowCount(loops)
        planned_directory = ""
        if planning and target_project:
            planned_directory = os.path.join(
                self.store.output_dir(target_project),
                time.strftime("%Y-%m-%d"),
            )
        for row in range(loops):
            index_item = QTableWidgetItem(f"成片{row + 1:03d}")
            status_item = QTableWidgetItem("等待")
            if planned_directory:
                output_text = os.path.join(
                    planned_directory,
                    f"正在规划第{row + 1:03d}个文件名...",
                )
            else:
                output_text = "点击开始后显示计划路径"
            output_item = QTableWidgetItem(output_text)
            output_item.setToolTip(output_text)
            self.render_table.setItem(row, 0, index_item)
            self.render_table.setItem(row, 1, status_item)
            self.render_table.setItem(row, 2, output_item)
        self.render_progress.setRange(0, max(1, loops))
        if not self.render_worker:
            self.render_progress.setValue(0)
            self.render_progress.setFormat("等待开始")
        project_name = str((target_project or {}).get("name") or "")
        self.render_task_owner_label.setText(
            f"任务列表所属：{project_name or '尚未选择项目'}"
            "（尚未开始）"
        )

    @staticmethod
    def _render_project_key(project_path):
        return os.path.normcase(os.path.abspath(str(project_path or "")))

    def _refresh_render_buttons(self):
        running = bool(self.render_worker and self.render_worker.isRunning())
        self.start_render_button.setEnabled(self.current_project is not None)
        if running:
            self.start_render_button.setText("加入生成队列")
        elif self.render_queue_paused and self.render_queue:
            self.start_render_button.setText("继续生成队列")
        else:
            self.start_render_button.setText("开始 / 继续生成")
        self.stop_render_button.setEnabled(running)
        self.clear_render_queue_button.setEnabled(bool(self.render_queue))

    def _refresh_render_queue_table(self):
        state_labels = {
            "queued": "等待执行",
            "running": "正在生成",
            "paused": "已暂停，等待继续",
            "completed": "已完成",
            "failed": "失败，已继续后续",
            "cancelled": "已取消",
        }
        records = self.render_task_records[-50:]
        self.render_queue_table.setRowCount(len(records))
        pending_ids = {
            task.queue_id: index
            for index, task in enumerate(self.render_queue, 1)
        }
        for row, task in enumerate(records):
            if task.state == "running":
                order_text = "当前"
            elif task.queue_id in pending_ids:
                order_text = f"等待 {pending_ids[task.queue_id]}"
            else:
                order_text = "-"
            project_text = f"{task.project_name}（{task.loops} 个）"
            state_text = task.message or state_labels.get(task.state, task.state)
            order_item = QTableWidgetItem(order_text)
            project_item = QTableWidgetItem(project_text)
            project_item.setToolTip(task.project_path)
            state_item = QTableWidgetItem(state_text)
            self.render_queue_table.setItem(row, 0, order_item)
            self.render_queue_table.setItem(row, 1, project_item)
            self.render_queue_table.setItem(row, 2, state_item)
        self._refresh_render_buttons()

    def _update_active_task_label(self, state_text="后台生成中"):
        task = self.render_active_task
        if not task:
            return
        waiting_count = len(self.render_queue)
        suffix = f"，另有 {waiting_count} 个项目排队" if waiting_count else ""
        self.render_task_owner_label.setText(
            f"任务列表所属：{task.project_name}（{state_text}{suffix}）"
        )

    def _create_render_task(self, status):
        project = self.current_project or {}
        replacements = [
            item for item in status if str(item.get("folder") or "").strip()
        ]
        replacement_text = "、".join(
            f"{int(item.get('segment_id') or 0):03d}"
            f"（{int(item.get('material_count') or 0)}个素材）"
            for item in replacements
        )
        return RenderQueueTask(
            queue_id=str(time.time_ns()),
            project_path=str(project.get("_project_path") or ""),
            project_id=str(project.get("project_id") or ""),
            project_name=str(project.get("name") or "未命名项目"),
            loops=max(1, int(self.loops_spin.value())),
            encoder=str(self.encoder_combo.currentData() or "auto"),
            replacement_text=replacement_text,
        )

    def start_render(self):
        if not self.current_project:
            QMessageBox.warning(self, "未选择项目", "请先导入竞品原片。")
            return
        issues, status = self.store.validate(self.current_project)
        if issues:
            QMessageBox.warning(self, "项目尚未就绪", "\n".join(issues))
            self.tabs.setCurrentIndex(0)
            return
        if not os.path.isfile(self.ffmpeg_path) or not os.path.isfile(
            self.ffprobe_path
        ):
            QMessageBox.warning(
                self, "缺少工具", "网盘资源包/tools 中缺少 ffmpeg.exe 或 ffprobe.exe。"
            )
            return

        project_path = str(self.current_project.get("_project_path") or "")
        project_key = self._render_project_key(project_path)
        active_task = self.render_active_task
        if (
            active_task
            and active_task.state == "running"
            and self._render_project_key(active_task.project_path) == project_key
        ):
            self.render_status.setText(
                f"“{active_task.project_name}”正在生成，无需重复加入队列。"
            )
            return
        queued_task = next(
            (
                task
                for task in self.render_queue
                if self._render_project_key(task.project_path) == project_key
            ),
            None,
        )
        if queued_task:
            if self.render_queue_paused and not (
                self.render_worker and self.render_worker.isRunning()
            ):
                self.render_queue_paused = False
                self.render_status.setText("生成队列已继续执行。")
                self._start_next_render_task()
            else:
                position = self.render_queue.index(queued_task) + 1
                self.render_status.setText(
                    f"“{queued_task.project_name}”已在等待队列第 {position} 位。"
                )
            self._refresh_render_queue_table()
            return

        task = self._create_render_task(status)
        self.render_queue.append(task)
        self.render_task_records.append(task)
        self._refresh_render_queue_table()
        if self.render_worker and self.render_worker.isRunning():
            self.render_status.setText(
                f"“{task.project_name}”已加入队列，前面还有 "
                f"{max(0, len(self.render_queue) - 1)} 个等待项目。"
            )
            self._update_active_task_label()
            return
        self.render_queue_paused = False
        self._start_next_render_task()

    def _start_next_render_task(self):
        if self.render_worker and self.render_worker.isRunning():
            return
        if self.render_queue_paused or not self.render_queue:
            self._refresh_render_queue_table()
            return
        task = self.render_queue.pop(0)
        try:
            project = self.store.load(task.project_path)
            project = self.store.sync_segment_mappings(project, save=True)
            issues, _status = self.store.validate(project)
            if issues:
                raise RuntimeError("；".join(issues))
        except Exception as exc:
            task.state = "failed"
            task.message = f"项目检查失败：{exc}"
            self._log(f"队列项目“{task.project_name}”已跳过：{exc}")
            self._refresh_render_queue_table()
            QTimer.singleShot(0, self._start_next_render_task)
            return

        self.render_active_task = task
        task.state = "running"
        task.message = ""
        self.render_stop_requested = False
        self._prepare_render_rows(
            planning=True,
            project=project,
            loops=task.loops,
        )
        self.render_project_id = task.project_id
        self.render_project_name = task.project_name
        self.render_project_path = task.project_path
        self._update_active_task_label()
        worker = ReplacementRenderWorker(
            task.project_path,
            task.loops,
            self.ffmpeg_path,
            self.ffprobe_path,
            encoder=task.encoder,
            resume=True,
            parent=self,
        )
        worker.log.connect(self._log)
        worker.progress.connect(self._on_render_progress)
        worker.jobs_prepared.connect(self._on_render_jobs_prepared)
        worker.job_status.connect(self._on_render_job_status)
        worker.completed.connect(self._on_render_completed)
        worker.finished.connect(lambda: self._clear_render_worker(worker))
        self.render_worker = worker
        self.render_status.setText(
            f"正在生成“{task.project_name}”；"
            f"已确认替换片段：{task.replacement_text or '无'}。"
        )
        worker.start()
        self._refresh_render_queue_table()

    def stop_render(self):
        worker = self.render_worker
        if worker and worker.isRunning():
            self.render_stop_requested = True
            self.render_queue_paused = True
            self.stop_render_button.setEnabled(False)
            self.render_status.setText(
                "正在安全停止当前 ffmpeg 进程，等待队列将保留。"
            )
            worker.stop()

    def clear_render_queue(self):
        if not self.render_queue:
            return
        for task in self.render_queue:
            task.state = "cancelled"
            task.message = "已从等待队列移除"
        removed = len(self.render_queue)
        self.render_queue.clear()
        if not (self.render_worker and self.render_worker.isRunning()):
            self.render_queue_paused = False
        self.render_status.setText(
            f"已清空 {removed} 个等待任务，当前生成任务不受影响。"
        )
        self._refresh_render_queue_table()
        self._update_active_task_label()

    def _on_render_progress(self, completed, total, message):
        self.render_progress.setRange(0, max(1, int(total)))
        self.render_progress.setValue(int(completed))
        self.render_progress.setFormat(f"{message}（{completed}/{total}）")

    def _on_render_jobs_prepared(self, jobs):
        prepared_jobs = [
            dict(job) for job in (jobs or []) if isinstance(job, dict)
        ]
        if not prepared_jobs:
            return
        self.render_table.setRowCount(len(prepared_jobs))
        status_labels = {
            "completed": "已完成（跳过）",
            "failed": "等待重试",
            "running": "等待继续",
            "pending": "等待",
        }
        for row, job in enumerate(prepared_jobs):
            index = int(job.get("index") or row + 1)
            status = str(job.get("status") or "pending")
            output_path = os.path.normpath(str(job.get("output") or ""))
            index_item = QTableWidgetItem(f"成片{index:03d}")
            status_item = QTableWidgetItem(status_labels.get(status, "等待"))
            output_item = QTableWidgetItem(output_path)
            output_item.setToolTip(output_path)
            output_item.setData(Qt.UserRole, output_path)
            self.render_table.setItem(row, 0, index_item)
            self.render_table.setItem(row, 1, status_item)
            self.render_table.setItem(row, 2, output_item)

    def _on_render_job_status(self, index, status, color):
        row = int(index) - 1
        if row < 0 or row >= self.render_table.rowCount():
            return
        item = self.render_table.item(row, 1) or QTableWidgetItem()
        item.setText(str(status))
        item.setForeground(Qt.white)
        item.setBackground(
            self.palette().highlight().color()
            if not color
            else QColor(color)
        )
        self.render_table.setItem(row, 1, item)

    def _on_render_completed(self, success, message):
        self.render_status.setText(str(message))
        task = self.render_active_task
        if task:
            if success:
                task.state = "completed"
                task.message = "已完成"
            elif self.render_stop_requested:
                task.state = "paused"
                task.message = "已暂停，等待继续"
                if task not in self.render_queue:
                    self.render_queue.insert(0, task)
            else:
                task.state = "failed"
                task.message = str(message)
        if success:
            state_text = "已完成"
        elif self.render_stop_requested:
            state_text = "已暂停"
        else:
            state_text = "失败，准备继续后续队列"
        self.render_task_owner_label.setText(
            f"任务列表所属：{self.render_project_name or '未知项目'}"
            f"（{state_text}）"
        )
        self.stop_render_button.setEnabled(False)
        self._refresh_render_queue_table()
        self._log(str(message))

    def _clear_render_worker(self, worker):
        if self.render_worker is worker:
            self.render_worker = None
            self.render_active_task = None
            self.render_stop_requested = False
            self._refresh_render_queue_table()
            if self.render_queue and not self.render_queue_paused:
                QTimer.singleShot(0, self._start_next_render_task)

    def prepare_app_close(self):
        self._cancel_scene_detect_worker()
        self._cancel_preview_worker()
        self._cancel_frame_timeline_worker()
        self._cancel_remote_cache_worker()
        self._cancel_seek_frame_worker()
        self._stop_preview()
        self.render_queue_paused = True
        worker = self.render_worker
        if worker and worker.isRunning():
            worker.stop()
            worker.wait(8000)
        app = QApplication.instance()
        if app and self._keyboard_filter_installed:
            app.removeEventFilter(self)
            self._keyboard_filter_installed = False

    def closeEvent(self, event):
        self.prepare_app_close()
        super().closeEvent(event)
