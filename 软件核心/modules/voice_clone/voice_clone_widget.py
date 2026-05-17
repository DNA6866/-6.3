import os
import wave

from PyQt5.QtCore import QByteArray, QBuffer, QIODevice, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QPainter, QPen
from PyQt5.QtMultimedia import QAudio, QAudioDeviceInfo, QAudioFormat, QAudioOutput
from PyQt5.QtWidgets import (
    QFileDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QCheckBox,
    QSpinBox,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from paths import tools_dir
from services.media_service import build_waveform_peaks

from .audio_tools import extract_audio_from_media, get_media_duration, make_output_path, open_output_dir, play_audio
from .config import APP_ROOT, ENV_REPORT_FILE, OUTPUT_DIR, ZIPENHANCER_MODEL_DIR, load_config, save_config
from .model_manager import check_model_path
from .task_worker import AudioPrepareWorker, BatchGenerateWorker, EnvCheckWorker, ReferenceTranscribeWorker, VoiceGenerateWorker


class ReferenceWaveform(QWidget):
    """参考音频波形展示，用于核对自动识别文字稿。"""

    selectionChanged = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.peaks = []
        self.progress = 0.0
        self.selection_start = 0.0
        self.selection_end = 1.0
        self._drag_anchor = None
        self._drag_mode = ""
        self._drag_start = 0.0
        self._drag_end = 1.0
        self._dragging = False
        self.setMinimumHeight(88)
        self.setMaximumHeight(96)
        self.setMouseTracking(True)

    def set_peaks(self, peaks):
        self.peaks = peaks or []
        self.progress = 0.0
        self.set_selection(0.0, 1.0, emit=False)
        self.update()

    def set_progress(self, progress):
        self.progress = max(0.0, min(1.0, float(progress)))
        self.update()

    def set_selection(self, start, end, emit=True):
        start = max(0.0, min(1.0, float(start)))
        end = max(0.0, min(1.0, float(end)))
        if end < start:
            start, end = end, start
        if abs(end - start) < 0.002:
            end = min(1.0, start + 0.002)
        changed = abs(self.selection_start - start) > 0.0005 or abs(self.selection_end - end) > 0.0005
        self.selection_start = start
        self.selection_end = end
        self.update()
        if emit and changed:
            self.selectionChanged.emit(self.selection_start, self.selection_end)

    def has_custom_selection(self):
        """全选是默认状态，不按蓝色框选样式绘制，避免误以为已经裁剪。"""
        return self.selection_start > 0.003 or self.selection_end < 0.997

    def _wave_rect(self):
        return self.rect().adjusted(8, 8, -8, -8)

    def _track_rect(self):
        return self._wave_rect().adjusted(7, 0, -7, 0)

    def _ratio_from_x(self, x):
        rect = self._track_rect()
        if rect.width() <= 0:
            return 0.0
        if x <= rect.left():
            return 0.0
        if x >= rect.right():
            return 1.0
        return max(0.0, min(1.0, (x - rect.left()) / rect.width()))

    def _edge_tolerance(self):
        rect = self._track_rect()
        if rect.width() <= 0:
            return 0.015
        return max(0.012, min(0.035, 10 / rect.width()))

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        ratio = self._ratio_from_x(event.x())
        tolerance = self._edge_tolerance()
        self._dragging = True
        self._drag_anchor = ratio
        self._drag_start = self.selection_start
        self._drag_end = self.selection_end
        custom_selection = self.has_custom_selection()
        if custom_selection and abs(ratio - self.selection_start) <= tolerance:
            self._drag_mode = "resize_start"
        elif custom_selection and abs(ratio - self.selection_end) <= tolerance:
            self._drag_mode = "resize_end"
        elif custom_selection and self.selection_start < ratio < self.selection_end:
            self._drag_mode = "move"
            self.setCursor(Qt.ClosedHandCursor)
        else:
            self._drag_mode = "create"
            self.set_selection(ratio, ratio)

    def mouseMoveEvent(self, event):
        ratio = self._ratio_from_x(event.x())
        if self._dragging and self._drag_anchor is not None:
            if self._drag_mode == "move":
                width = max(0.002, self._drag_end - self._drag_start)
                new_start = self._drag_start + (ratio - self._drag_anchor)
                new_start = max(0.0, min(1.0 - width, new_start))
                self.set_selection(new_start, new_start + width)
            elif self._drag_mode == "resize_start":
                self.set_selection(ratio, self._drag_end)
            elif self._drag_mode == "resize_end":
                self.set_selection(self._drag_start, ratio)
            else:
                self.set_selection(self._drag_anchor, ratio)
            return

        tolerance = self._edge_tolerance()
        custom_selection = self.has_custom_selection()
        if custom_selection and (abs(ratio - self.selection_start) <= tolerance or abs(ratio - self.selection_end) <= tolerance):
            self.setCursor(Qt.SizeHorCursor)
        elif custom_selection and self.selection_start < ratio < self.selection_end:
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self.mouseMoveEvent(event)
            self._dragging = False
            self._drag_anchor = None
            self._drag_mode = ""
            self.setCursor(Qt.ArrowCursor)
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self._wave_rect()
        track = self._track_rect()
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.setBrush(QBrush(QColor("#020617")))
        painter.drawRoundedRect(rect, 8, 8)

        custom_selection = self.has_custom_selection()
        sel_left = track.left() + int(track.width() * self.selection_start)
        sel_right = track.left() + int(track.width() * self.selection_end)
        if custom_selection:
            painter.setPen(QPen(QColor("#38BDF8"), 1))
            painter.setBrush(QBrush(QColor(37, 99, 235, 70)))
            painter.drawRoundedRect(sel_left, rect.top() + 4, max(2, sel_right - sel_left), rect.height() - 8, 6, 6)

        mid = rect.center().y()
        available_h = max(12, rect.height() - 18)
        if self.peaks:
            count = min(max(1, track.width()), len(self.peaks))
            step = len(self.peaks) / max(1, count)
            for i in range(count):
                x = track.left() + i
                peak = self.peaks[int(i * step)]
                bar_h = max(2, int(peak * available_h))
                ratio = i / max(1, count - 1)
                if custom_selection and self.selection_start <= ratio <= self.selection_end:
                    color = QColor("#38BDF8")
                elif ratio <= self.progress:
                    color = QColor("#93C5FD")
                else:
                    color = QColor("#475569")
                painter.setPen(QPen(color, 1))
                painter.drawLine(x, mid - bar_h // 2, x, mid + bar_h // 2)
        else:
            painter.setPen(QPen(QColor("#64748B"), 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(track.left(), mid, track.right(), mid)

        play_x = track.left() + int(track.width() * self.progress)
        painter.setPen(QPen(QColor("#FDE68A"), 2))
        painter.drawLine(play_x, rect.top() + 8, play_x, rect.bottom() - 8)
        if custom_selection:
            painter.setPen(QPen(QColor("#FDE68A"), 2))
            painter.drawLine(sel_left, rect.top() + 6, sel_left, rect.bottom() - 6)
            painter.drawLine(sel_right, rect.top() + 6, sel_right, rect.bottom() - 6)


class AudioPreviewPanel(QWidget):
    """统一音频预览、播放和框选裁剪控件。"""

    selectionChanged = pyqtSignal(float, float)

    def __init__(self, title="参考音频预览", parent=None):
        super().__init__(parent)
        self.audio_path = ""
        self.preview_wav_path = ""
        self.duration_ms = 0
        self._loop_selection = True
        self._audio_output = None
        self._audio_buffer = None
        self._audio_data = QByteArray()
        self._audio_bytes_per_ms = 0.0
        self._play_start_ms = 0
        self._paused = False
        self.setMinimumHeight(148)
        self.setMaximumHeight(158)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("mutedText")
        self.info_label = QLabel("尚未选择音频")
        self.info_label.setObjectName("mutedText")
        self.info_label.setMaximumHeight(20)
        self.waveform = ReferenceWaveform()
        self.waveform.selectionChanged.connect(self._on_waveform_selection_changed)

        self.timer = QTimer(self)
        self.timer.setInterval(15)
        self.timer.timeout.connect(self.refresh_preview)

        self.play_btn = QPushButton("播放选区")
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.clicked.connect(self.toggle_playback)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.stop_btn.clicked.connect(self.stop)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("mutedText")
        self.selection_label = QLabel("选区：全部")
        self.selection_label.setObjectName("mutedText")

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self.play_btn)
        controls.addWidget(self.stop_btn)
        controls.addStretch(1)
        controls.addWidget(self.time_label)
        controls.addWidget(self.selection_label)

        layout.addWidget(self.info_label)
        layout.addWidget(self.waveform)
        layout.addLayout(controls)

    def set_audio(self, audio_path):
        self.stop()
        self.audio_path = audio_path or ""
        self.preview_wav_path = ""
        self.duration_ms = 0
        self._audio_data = QByteArray()
        self._audio_bytes_per_ms = 0.0
        self.waveform.set_peaks([])
        self.waveform.set_progress(0)
        if not self.audio_path or not os.path.exists(self.audio_path):
            self.info_label.setText("尚未选择音频")
            self.time_label.setText("00:00 / 00:00")
            self.selection_label.setText("选区：全部")
            return
        name = os.path.basename(self.audio_path)
        size_mb = os.path.getsize(self.audio_path) / 1024 / 1024
        ffprobe = os.path.join(tools_dir(), "ffprobe.exe")
        duration = get_media_duration(ffprobe if os.path.exists(ffprobe) else "ffprobe", self.audio_path)
        if duration > 0:
            self.duration_ms = int(duration * 1000)
        self._prepare_pcm_preview()
        self.info_label.setText(f"{name}    {size_mb:.2f} MB")
        self._refresh_selection_label()
        self.refresh_preview(0)
        ffmpeg = os.path.join(tools_dir(), "ffmpeg.exe")
        try:
            self.waveform.set_peaks(build_waveform_peaks(ffmpeg, self.audio_path, points=900))
        except Exception:
            self.waveform.set_peaks([])

    def has_audio(self):
        return bool(self.audio_path and os.path.exists(self.audio_path))

    def has_custom_selection(self):
        return self.has_audio() and (self.waveform.selection_start > 0.003 or self.waveform.selection_end < 0.997)

    def selection_seconds(self):
        duration = self.duration_ms / 1000 if self.duration_ms else 0
        if duration <= 0:
            return 0.0, 0.0
        start = self.waveform.selection_start * duration
        end = self.waveform.selection_end * duration
        return max(0.0, start), max(0.0, end - start)

    def set_selection_seconds(self, start, duration):
        total = self.duration_ms / 1000 if self.duration_ms else 0
        if total <= 0:
            return
        end = total if duration <= 0 else min(total, start + duration)
        self.waveform.set_selection(start / total, end / total)

    def toggle_playback(self):
        if not self.has_audio():
            return
        if self._audio_output and self._audio_output.state() == QAudio.ActiveState:
            self._audio_output.suspend()
            self._paused = True
            self.timer.stop()
            self.play_btn.setText("继续")
            self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            return
        if self._audio_output and self._audio_output.state() == QAudio.SuspendedState:
            self._paused = False
            self._audio_output.resume()
            self.timer.start()
            self.play_btn.setText("暂停")
            self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
            return
        start_ms, _ = self._selection_ms()
        self._start_audio_output(start_ms)

    def stop(self):
        self.timer.stop()
        if self._audio_output:
            self._audio_output.stop()
            self._audio_output.deleteLater()
            self._audio_output = None
        if self._audio_buffer:
            self._audio_buffer.close()
            self._audio_buffer.deleteLater()
            self._audio_buffer = None
        self._play_start_ms = 0
        self._paused = False
        self.waveform.set_progress(0)
        self.time_label.setText(f"{self._fmt_ms(0)} / {self._fmt_ms(self._duration_ms())}")
        self.play_btn.setText("播放选区")
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def _selection_ms(self):
        duration = self._duration_ms()
        start = int(duration * self.waveform.selection_start)
        end = int(duration * self.waveform.selection_end)
        if end <= start:
            end = duration
        return start, end

    def _duration_ms(self):
        return self.duration_ms or 0

    def _prepare_pcm_preview(self):
        ffmpeg = os.path.join(tools_dir(), "ffmpeg.exe")
        ffmpeg = ffmpeg if os.path.exists(ffmpeg) else "ffmpeg"
        self.preview_wav_path = make_output_path(OUTPUT_DIR, "preview_audio", "wav")
        extract_audio_from_media(ffmpeg, self.audio_path, self.preview_wav_path, start=0, duration=0, sample_rate=48000)
        with wave.open(self.preview_wav_path, "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frames = wav_file.getnframes()
            pcm = wav_file.readframes(frames)
        if sample_width != 2:
            raise RuntimeError("预览音频格式不支持，请换用 wav/mp3/m4a 等常见格式。")
        self._audio_data = QByteArray(pcm)
        self._audio_bytes_per_ms = sample_rate * channels * sample_width / 1000
        self.duration_ms = int(frames / sample_rate * 1000) if sample_rate else self.duration_ms
        self._audio_format = QAudioFormat()
        self._audio_format.setSampleRate(sample_rate)
        self._audio_format.setChannelCount(channels)
        self._audio_format.setSampleSize(sample_width * 8)
        self._audio_format.setCodec("audio/pcm")
        self._audio_format.setByteOrder(QAudioFormat.LittleEndian)
        self._audio_format.setSampleType(QAudioFormat.SignedInt)
        device = QAudioDeviceInfo.defaultOutputDevice()
        if not device.isFormatSupported(self._audio_format):
            self._audio_format = device.nearestFormat(self._audio_format)

    def _start_audio_output(self, start_ms):
        if self._audio_data.isEmpty():
            self._prepare_pcm_preview()
        self.stop()
        start_ms = max(0, min(int(start_ms), self._duration_ms()))
        byte_pos = int(start_ms * self._audio_bytes_per_ms)
        if byte_pos % 2:
            byte_pos -= 1
        self._play_start_ms = start_ms
        self._audio_buffer = QBuffer(self)
        self._audio_buffer.setData(self._audio_data)
        self._audio_buffer.open(QIODevice.ReadOnly)
        self._audio_buffer.seek(max(0, min(byte_pos, self._audio_data.size())))
        self._audio_output = QAudioOutput(self._audio_format, self)
        self._audio_output.stateChanged.connect(self._on_audio_state_changed)
        self._audio_output.start(self._audio_buffer)
        self.timer.start()
        self.play_btn.setText("暂停")
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        self.refresh_preview(start_ms)

    def _on_waveform_selection_changed(self, start_ratio, end_ratio):
        if self._audio_output and self._audio_output.state() == QAudio.ActiveState:
            self._audio_output.suspend()
            self._paused = True
            self.timer.stop()
        start_ms, _ = self._selection_ms()
        self._play_start_ms = start_ms
        self.refresh_preview(start_ms)
        self._refresh_selection_label()
        start, duration = self.selection_seconds()
        self.selectionChanged.emit(start, duration)

    def _on_audio_state_changed(self, state):
        if state in (QAudio.IdleState, QAudio.StoppedState) and not self._paused:
            self.timer.stop()
            self.play_btn.setText("播放选区")
            self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def refresh_preview(self, position=None):
        duration = self._duration_ms()
        if position is None:
            if self._audio_output and self._audio_output.state() in (QAudio.ActiveState, QAudio.SuspendedState):
                position = self._play_start_ms + int(self._audio_output.processedUSecs() / 1000)
            else:
                position = self._play_start_ms
        if duration > 0 and self._audio_output and self._audio_output.state() == QAudio.ActiveState:
            start_ms, end_ms = self._selection_ms()
            if self._loop_selection and end_ms > start_ms and position >= end_ms:
                self._start_audio_output(start_ms)
                return
        position = max(0, min(int(position or 0), duration or int(position or 0)))
        if duration > 0:
            self.waveform.set_progress(position / duration)
        self.time_label.setText(f"{self._fmt_ms(position)} / {self._fmt_ms(duration)}")

    def _refresh_selection_label(self):
        start, duration = self.selection_seconds()
        if not self.has_audio() or not self.has_custom_selection():
            self.selection_label.setText("选区：全部")
        else:
            self.selection_label.setText(f"选区：{start:.2f}s - {start + duration:.2f}s")

    def _fmt_ms(self, value):
        total = max(0, int(value // 1000))
        return f"{total // 60:02d}:{total % 60:02d}"


class VoiceCloneWidget(QWidget):
    """AI音频克隆功能区，UI 与真实推理逻辑分离。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = load_config()
        self.env_worker = None
        self.generate_worker = None
        self.transcribe_worker = None
        self.audio_prepare_worker = None
        self.batch_worker = None
        self._build_ui()
        self._load_config_to_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("AI音频克隆")
        title.setObjectName("pageTitle")
        subtitle = QLabel("本地集成 VoxCPM2，用于文本转语音与参考音频克隆。")
        subtitle.setObjectName("mutedText")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)

        self.status_label = QLabel("当前状态：未检测")
        self.status_label.setObjectName("statusPill")
        self.env_button = QPushButton("检测运行环境")
        self.report_button = QPushButton("打开检测报告")
        self.env_button.clicked.connect(self.run_env_check)
        self.report_button.clicked.connect(self.open_report)
        header.addWidget(self.status_label)
        header.addWidget(self.env_button)
        header.addWidget(self.report_button)
        root.addLayout(header)

        root.addWidget(self._build_env_table())
        root.addWidget(self._build_model_group())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_tts_tab(), "文本转语音")
        self.tabs.addTab(self._build_audio_tools_tab(), "参考音频工具")
        self.tabs.addTab(self._build_clone_tab(), "参考音频克隆")
        self.tabs.addTab(self._build_ultimate_tab(), "高相似克隆")
        self.tabs.addTab(self._build_batch_tab(), "批量生成")
        root.addWidget(self.tabs, 1)

        notice = QLabel("请确保你拥有参考音频的合法使用权。禁止用于冒充他人、诈骗、虚假宣传或侵犯他人声音权益。AI生成音频请按平台要求进行标识。")
        notice.setObjectName("warningText")
        notice.setWordWrap(True)
        root.addWidget(notice)

    def _build_model_group(self):
        group = QGroupBox("模型路径设置")
        layout = QGridLayout(group)
        layout.setColumnStretch(1, 1)

        layout.addWidget(QLabel("当前模型路径"), 0, 0)
        self.model_path_edit = QLineEdit()
        self.model_path_edit.setReadOnly(True)
        layout.addWidget(self.model_path_edit, 0, 1)

        choose_btn = QPushButton("选择 VoxCPM2 模型目录")
        check_btn = QPushButton("检查模型")
        choose_btn.clicked.connect(self.choose_model_dir)
        check_btn.clicked.connect(self.check_model)
        layout.addWidget(choose_btn, 0, 2)
        layout.addWidget(check_btn, 0, 3)

        self.model_message = QLabel("默认检查 网盘资源包/models/VoxCPM2。")
        self.model_message.setObjectName("mutedText")
        layout.addWidget(self.model_message, 1, 1, 1, 3)
        return group

    def _build_env_table(self):
        group = QGroupBox("运行环境检测结果")
        layout = QVBoxLayout(group)
        self.env_table = QTableWidget(0, 4)
        self.env_table.setHorizontalHeaderLabels(["项目", "状态", "检测值", "客户提示"])
        self.env_table.horizontalHeader().setStretchLastSection(True)
        self.env_table.verticalHeader().setVisible(False)
        self.env_table.setAlternatingRowColors(True)
        self.env_table.setMinimumHeight(96)
        self.env_table.setMaximumHeight(126)
        layout.addWidget(self.env_table)
        return group

    def _row(self, label, widget, row, layout):
        layout.addWidget(QLabel(label), row, 0)
        layout.addWidget(widget, row, 1)

    def _build_param_line(self):
        cfg = QDoubleSpinBox()
        cfg.setRange(0.1, 10.0)
        cfg.setSingleStep(0.1)
        cfg.setValue(float(self.config.get("default_cfg_value", 2.0)))
        cfg.setToolTip("控制生成时对提示词和参考声音的约束强度；数值越大越贴近提示/参考，但过高可能更生硬。")
        steps = QSpinBox()
        steps.setRange(1, 100)
        steps.setValue(int(self.config.get("default_inference_timesteps", 10)))
        steps.setToolTip("控制推理迭代次数；数值越大通常更稳但更慢，数值越小速度更快但质量可能下降。")
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(QLabel("cfg_value"))
        line.addWidget(cfg)
        line.addSpacing(18)
        line.addWidget(QLabel("inference_timesteps"))
        line.addWidget(steps)
        line.addStretch(1)
        hint = QLabel("cfg_value：控制声音/提示词约束强度，越大越贴近但可能更生硬。inference_timesteps：控制推理步数，越大通常更稳但更慢。")
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addLayout(line)
        layout.addWidget(hint)
        return box, cfg, steps

    def _build_advanced_line(self, denoise=False):
        box = QWidget()
        line = QHBoxLayout(box)
        line.setContentsMargins(0, 0, 0, 0)
        normalize_chk = QCheckBox("文本规范化")
        normalize_chk.setToolTip("参考雨落版 VoxCPM2：生成前做文本规范化，适合长文本、英文/数字/符号较多的内容。")
        denoise_chk = QCheckBox("参考音频降噪增强")
        denoise_chk.setToolTip("需要 ZipEnhancer 模型；适合参考音频有底噪的情况，首次启用会增加加载时间。")
        denoise_chk.setEnabled(bool(denoise))
        line.addWidget(normalize_chk)
        line.addWidget(denoise_chk)
        line.addStretch(1)
        return box, normalize_chk, denoise_chk

    def _build_tts_tab(self):
        page = QWidget()
        layout = QGridLayout(page)
        layout.setColumnStretch(1, 1)

        self.tts_text = QTextEdit()
        self.tts_text.setPlaceholderText("输入要生成的中文或英文文本")
        self.tts_voice_desc = QLineEdit()
        self.tts_voice_desc.setPlaceholderText("例如：年轻女性，温柔，自然，适合电商直播")
        param_box, self.tts_cfg, self.tts_steps = self._build_param_line()
        advanced_box, self.tts_normalize, self.tts_denoise_unused = self._build_advanced_line(denoise=False)
        self.tts_generate_btn = QPushButton("生成文本转语音")
        self.tts_progress = QProgressBar()
        self.tts_output = QLineEdit()
        self.tts_output.setReadOnly(True)
        listen_btn = QPushButton("试听")
        open_btn = QPushButton("打开输出目录")

        self.tts_generate_btn.clicked.connect(self.generate_tts)
        listen_btn.clicked.connect(lambda: self.listen_audio(self.tts_output.text()))
        open_btn.clicked.connect(self.open_output)

        layout.addWidget(QLabel("文本输入"), 0, 0)
        layout.addWidget(self.tts_text, 0, 1, 1, 3)
        self._row("声音描述", self.tts_voice_desc, 1, layout)
        self._row("推理参数", param_box, 2, layout)
        self._row("高级选项", advanced_box, 3, layout)
        layout.addWidget(self.tts_generate_btn, 4, 1)
        layout.addWidget(self.tts_progress, 4, 2, 1, 2)
        self._row("输出文件", self.tts_output, 5, layout)
        layout.addWidget(listen_btn, 5, 2)
        layout.addWidget(open_btn, 5, 3)
        layout.setRowStretch(6, 1)
        return page

    def _build_audio_tools_tab(self):
        page = QWidget()
        layout = QGridLayout(page)
        layout.setColumnStretch(1, 1)

        self.prepare_input = QLineEdit()
        self.prepare_input.setReadOnly(True)
        choose_btn = QPushButton("选择音视频文件")
        choose_btn.clicked.connect(self.choose_prepare_input)

        self.prepare_start = QDoubleSpinBox()
        self.prepare_start.setRange(0, 999999)
        self.prepare_start.setDecimals(2)
        self.prepare_start.setSuffix(" 秒")
        self.prepare_duration = QDoubleSpinBox()
        self.prepare_duration.setRange(0, 3600)
        self.prepare_duration.setDecimals(2)
        self.prepare_duration.setValue(30)
        self.prepare_duration.setSuffix(" 秒")
        self.prepare_normalize = QCheckBox("统一音量")
        self.prepare_normalize.setChecked(True)
        self.prepare_preview = AudioPreviewPanel("输入音视频预览与裁剪")
        self.prepare_preview.selectionChanged.connect(self.on_prepare_selection_changed)
        self.prepare_start.valueChanged.connect(self.on_prepare_spin_changed)
        self.prepare_duration.valueChanged.connect(self.on_prepare_spin_changed)

        trim_box = QWidget()
        trim_line = QHBoxLayout(trim_box)
        trim_line.setContentsMargins(0, 0, 0, 0)
        trim_line.addWidget(QLabel("开始"))
        trim_line.addWidget(self.prepare_start)
        trim_line.addSpacing(18)
        trim_line.addWidget(QLabel("时长"))
        trim_line.addWidget(self.prepare_duration)
        trim_line.addSpacing(18)
        trim_line.addWidget(self.prepare_normalize)
        trim_line.addStretch(1)

        self.prepare_status = QLabel("建议选择 10-30 秒清晰人声片段；可以从视频里直接提取音频。")
        self.prepare_status.setObjectName("mutedText")
        self.prepare_progress = QProgressBar()
        self.prepare_output = QLineEdit()
        self.prepare_output.setReadOnly(True)
        self.prepare_btn = QPushButton("提取/裁剪为参考音频")
        self.prepare_btn.clicked.connect(self.prepare_reference_audio)
        to_clone_btn = QPushButton("设为普通克隆参考音频")
        to_clone_btn.clicked.connect(self.set_prepared_audio_to_clone)
        to_ultimate_btn = QPushButton("设为高相似参考音频")
        to_ultimate_btn.clicked.connect(self.set_prepared_audio_to_ultimate)
        listen_btn = QPushButton("试听处理结果")
        listen_btn.clicked.connect(lambda: self.listen_audio(self.prepare_output.text()))
        open_btn = QPushButton("打开输出目录")
        open_btn.clicked.connect(self.open_output)

        tip = QLabel("这个工具参考雨落版的工作流：先从视频/音频里截取干净的人声，再送去普通克隆或高相似克隆。时长填 0 表示从开始位置截到结尾。")
        tip.setObjectName("mutedText")
        tip.setWordWrap(True)

        layout.addWidget(QLabel("输入文件"), 0, 0)
        layout.addWidget(self.prepare_input, 0, 1, 1, 2)
        layout.addWidget(choose_btn, 0, 3)
        layout.addWidget(QLabel("预览裁剪"), 1, 0)
        layout.addWidget(self.prepare_preview, 1, 1, 1, 3)
        layout.setRowMinimumHeight(1, 150)
        self._row("截取参数", trim_box, 2, layout)
        layout.addWidget(self.prepare_btn, 3, 1)
        layout.addWidget(self.prepare_progress, 3, 2, 1, 2)
        self._row("处理状态", self.prepare_status, 4, layout)
        self._row("输出音频", self.prepare_output, 5, layout)
        layout.addWidget(listen_btn, 5, 2)
        layout.addWidget(open_btn, 5, 3)
        layout.addWidget(to_clone_btn, 6, 1)
        layout.addWidget(to_ultimate_btn, 6, 2)
        layout.addWidget(tip, 7, 1, 1, 3)
        layout.setRowStretch(8, 1)
        return page

    def _build_clone_tab(self):
        page = QWidget()
        layout = QGridLayout(page)
        layout.setColumnStretch(1, 1)

        self.clone_ref_audio = QLineEdit()
        self.clone_ref_audio.setReadOnly(True)
        choose_ref_btn = QPushButton("选择参考音频")
        choose_ref_btn.clicked.connect(self.choose_reference_audio)
        self.clone_preview = AudioPreviewPanel("参考音频预览与裁剪")
        self.clone_text = QTextEdit()
        self.clone_text.setPlaceholderText("输入要用参考声音生成的新文本")
        self.clone_prompt = QLineEdit()
        self.clone_prompt.setPlaceholderText("例如：自然、清晰、带一点直播讲解感")
        param_box, self.clone_cfg, self.clone_steps = self._build_param_line()
        advanced_box, self.clone_normalize, self.clone_denoise = self._build_advanced_line(denoise=True)
        self.clone_generate_btn = QPushButton("生成参考音频克隆")
        self.clone_progress = QProgressBar()
        self.clone_output = QLineEdit()
        self.clone_output.setReadOnly(True)
        listen_btn = QPushButton("试听")
        open_btn = QPushButton("打开输出目录")

        self.clone_generate_btn.clicked.connect(self.generate_clone)
        listen_btn.clicked.connect(lambda: self.listen_audio(self.clone_output.text()))
        open_btn.clicked.connect(self.open_output)

        layout.addWidget(QLabel("参考音频"), 0, 0)
        layout.addWidget(self.clone_ref_audio, 0, 1, 1, 2)
        layout.addWidget(choose_ref_btn, 0, 3)
        layout.addWidget(QLabel("预览裁剪"), 1, 0)
        layout.addWidget(self.clone_preview, 1, 1, 1, 3)
        layout.setRowMinimumHeight(1, 150)
        layout.addWidget(QLabel("生成文本"), 2, 0)
        layout.addWidget(self.clone_text, 2, 1, 1, 3)
        self._row("风格提示词", self.clone_prompt, 3, layout)
        self._row("推理参数", param_box, 4, layout)
        self._row("高级选项", advanced_box, 5, layout)
        layout.addWidget(self.clone_generate_btn, 6, 1)
        layout.addWidget(self.clone_progress, 6, 2, 1, 2)
        self._row("输出文件", self.clone_output, 7, layout)
        layout.addWidget(listen_btn, 7, 2)
        layout.addWidget(open_btn, 7, 3)
        layout.setRowStretch(8, 1)
        return page

    def _build_ultimate_tab(self):
        page = QWidget()
        layout = QGridLayout(page)
        layout.setColumnStretch(1, 1)
        self.ultimate_ref_audio = QLineEdit()
        self.ultimate_ref_audio.setReadOnly(True)
        choose_btn = QPushButton("选择参考音频")
        choose_btn.clicked.connect(self.choose_ultimate_reference_audio)
        transcribe_btn = QPushButton("重新识别文字稿")
        transcribe_btn.clicked.connect(self.start_ultimate_transcribe)
        self.ultimate_preview = AudioPreviewPanel("参考音频预览与裁剪")
        self.ultimate_preview.selectionChanged.connect(self.on_ultimate_preview_selection_changed)
        self.ultimate_transcript = QTextEdit()
        self.ultimate_transcript.setPlaceholderText("选择参考音频后会自动识别文字稿，识别完成后可在这里校对微调。")
        self.ultimate_transcript.setMinimumHeight(86)
        self.ultimate_transcript.setMaximumHeight(110)
        self.ultimate_text = QTextEdit()
        self.ultimate_text.setPlaceholderText("输入要生成的新文本")
        self.ultimate_text.setMinimumHeight(72)
        self.ultimate_text.setMaximumHeight(92)
        param_box, self.ultimate_cfg, self.ultimate_steps = self._build_param_line()
        advanced_box, self.ultimate_normalize, self.ultimate_denoise = self._build_advanced_line(denoise=True)
        self.ultimate_generate_btn = QPushButton("生成高相似克隆")
        self.ultimate_generate_btn.clicked.connect(self.generate_ultimate_clone)
        self.ultimate_progress = QProgressBar()
        self.ultimate_output = QLineEdit()
        self.ultimate_output.setReadOnly(True)
        listen_btn = QPushButton("试听")
        open_btn = QPushButton("打开输出目录")
        listen_btn.clicked.connect(lambda: self.listen_audio(self.ultimate_output.text()))
        open_btn.clicked.connect(self.open_output)
        self.ultimate_transcribe_status = QLabel("选择参考音频后自动识别文字稿。")
        self.ultimate_transcribe_status.setObjectName("mutedText")
        self.ultimate_transcribe_status.setWordWrap(True)
        self.ultimate_transcribe_status.setMinimumHeight(34)

        tip = QLabel("高相似克隆会自动识别参考音频文字稿，但建议生成前人工快速校对一次，文字稿越准，相似度越稳定。")
        tip.setObjectName("mutedText")
        tip.setWordWrap(True)

        layout.addWidget(QLabel("参考音频"), 0, 0)
        layout.addWidget(self.ultimate_ref_audio, 0, 1)
        layout.addWidget(choose_btn, 0, 2)
        layout.addWidget(transcribe_btn, 0, 3)
        layout.addWidget(QLabel("参考音频预览"), 1, 0)
        layout.addWidget(self.ultimate_preview, 1, 1, 1, 3)
        layout.setRowMinimumHeight(1, 150)
        layout.addWidget(QLabel("识别状态"), 2, 0)
        layout.addWidget(self.ultimate_transcribe_status, 2, 1, 1, 3)
        layout.setRowMinimumHeight(2, 34)
        layout.addWidget(QLabel("参考音频文字稿"), 3, 0)
        layout.addWidget(self.ultimate_transcript, 3, 1, 1, 3)
        layout.setRowMinimumHeight(3, 92)
        layout.addWidget(QLabel("生成文本"), 4, 0)
        layout.addWidget(self.ultimate_text, 4, 1, 1, 3)
        layout.setRowMinimumHeight(4, 78)
        self._row("推理参数", param_box, 5, layout)
        self._row("高级选项", advanced_box, 6, layout)
        layout.addWidget(self.ultimate_generate_btn, 7, 1)
        layout.addWidget(self.ultimate_progress, 7, 2, 1, 2)
        self._row("输出文件", self.ultimate_output, 8, layout)
        layout.addWidget(listen_btn, 8, 2)
        layout.addWidget(open_btn, 8, 3)
        layout.addWidget(tip, 9, 1, 1, 3)
        layout.setRowStretch(10, 1)
        return page

    def _build_batch_tab(self):
        page = QWidget()
        layout = QGridLayout(page)
        layout.setColumnStretch(1, 1)

        self.batch_txt = QLineEdit()
        self.batch_txt.setReadOnly(True)
        txt_btn = QPushButton("导入 txt")
        txt_btn.clicked.connect(self.choose_batch_txt)
        self.batch_ref_audio = QLineEdit()
        self.batch_ref_audio.setReadOnly(True)
        ref_btn = QPushButton("选择参考音频")
        ref_btn.clicked.connect(self.choose_batch_reference_audio)
        self.batch_preview = AudioPreviewPanel("批量参考音频预览与裁剪")
        self.batch_output_dir = QLineEdit(self.config.get("output_dir", OUTPUT_DIR))
        output_btn = QPushButton("选择输出目录")
        output_btn.clicked.connect(self.choose_output_dir)
        self.batch_prompt = QLineEdit()
        self.batch_prompt.setPlaceholderText("无参考音频时作为声音描述；有参考音频时作为风格提示词")
        self.batch_prompt_text = QTextEdit()
        self.batch_prompt_text.setPlaceholderText("可选：填写参考音频文字稿后使用高相似批量克隆；为空则使用普通参考音频克隆。")
        self.batch_prompt_text.setMinimumHeight(72)
        self.batch_prompt_text.setMaximumHeight(94)
        param_box, self.batch_cfg, self.batch_steps = self._build_param_line()
        advanced_box, self.batch_normalize, self.batch_denoise = self._build_advanced_line(denoise=True)
        start_btn = QPushButton("开始批量生成")
        start_btn.clicked.connect(self.start_batch_generate)
        self.batch_progress = QProgressBar()
        self.batch_list = QListWidget()
        self.batch_counts = QLabel("成功：0    失败：0")

        layout.addWidget(QLabel("txt 文件"), 0, 0)
        layout.addWidget(self.batch_txt, 0, 1)
        layout.addWidget(txt_btn, 0, 2)
        layout.addWidget(QLabel("参考音频"), 1, 0)
        layout.addWidget(self.batch_ref_audio, 1, 1)
        layout.addWidget(ref_btn, 1, 2)
        layout.addWidget(QLabel("预览裁剪"), 2, 0)
        layout.addWidget(self.batch_preview, 2, 1, 1, 3)
        layout.setRowMinimumHeight(2, 150)
        layout.addWidget(QLabel("输出目录"), 3, 0)
        layout.addWidget(self.batch_output_dir, 3, 1)
        layout.addWidget(output_btn, 3, 2)
        self._row("风格/声音描述", self.batch_prompt, 4, layout)
        layout.addWidget(QLabel("参考音频文字稿"), 5, 0)
        layout.addWidget(self.batch_prompt_text, 5, 1, 1, 2)
        self._row("推理参数", param_box, 6, layout)
        self._row("高级选项", advanced_box, 7, layout)
        layout.addWidget(start_btn, 8, 1)
        layout.addWidget(self.batch_progress, 8, 2)
        layout.addWidget(self.batch_counts, 8, 3)
        layout.addWidget(QLabel("任务列表"), 9, 0)
        layout.addWidget(self.batch_list, 9, 1, 1, 3)
        return page

    def _load_config_to_ui(self):
        self.model_path_edit.setText(self.config.get("model_path", ""))
        if hasattr(self, "clone_ref_audio"):
            last_audio = self.config.get("last_reference_audio", "")
            self.clone_ref_audio.setText(last_audio)
            if last_audio and os.path.exists(last_audio):
                self.clone_preview.set_audio(last_audio)
        if hasattr(self, "ultimate_ref_audio"):
            last_audio = self.config.get("last_reference_audio", "")
            self.ultimate_ref_audio.setText(last_audio)
            if last_audio and os.path.exists(last_audio):
                self.load_ultimate_reference_preview(last_audio)

    def _save_current_config(self):
        self.config["model_path"] = self.model_path_edit.text().strip()
        self.config["output_dir"] = self.batch_output_dir.text().strip() if hasattr(self, "batch_output_dir") else self.config.get("output_dir", OUTPUT_DIR)
        self.config["last_reference_audio"] = self.clone_ref_audio.text().strip() if hasattr(self, "clone_ref_audio") else ""
        self.config["default_cfg_value"] = float(self.tts_cfg.value()) if hasattr(self, "tts_cfg") else 2.0
        self.config["default_inference_timesteps"] = int(self.tts_steps.value()) if hasattr(self, "tts_steps") else 10
        self.config["zipenhancer_model_path"] = self.config.get("zipenhancer_model_path") or ZIPENHANCER_MODEL_DIR
        save_config(self.config)

    def choose_model_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择 VoxCPM2 模型目录", self.model_path_edit.text() or os.getcwd())
        if not path:
            return
        self.model_path_edit.setText(path)
        self._save_current_config()
        self.check_model()

    def check_model(self):
        ok, msg, files = check_model_path(self.model_path_edit.text().strip())
        self.model_message.setText(f"{msg} 文件数：{files.get('total_files', 0)}")
        color = "#22C55E" if ok else "#EF4444"
        self.model_message.setStyleSheet(f"color: {color};")
        QMessageBox.information(self, "模型检查", msg if ok else f"{msg}\n请确认模型文件夹选择正确。")

    def run_env_check(self):
        if self.env_worker and self.env_worker.isRunning():
            QMessageBox.information(self, "检测中", "环境检测正在进行，请稍等。")
            return
        self._save_current_config()
        self.env_button.setEnabled(False)
        self.status_label.setText("当前状态：检测中")
        self.env_worker = EnvCheckWorker(self.model_path_edit.text().strip(), self.config.get("output_dir", OUTPUT_DIR))
        self.env_worker.finished.connect(self.on_env_finished)
        self.env_worker.failed.connect(self.on_env_failed)
        self.env_worker.start()

    def on_env_finished(self, results, report_path, passed):
        self.env_button.setEnabled(True)
        self.status_label.setText("当前状态：环境通过" if passed else "当前状态：环境异常")
        self.status_label.setStyleSheet(f"color: {'#22C55E' if passed else '#EF4444'};")
        self._fill_env_table(results)
        QMessageBox.information(self, "环境检测完成", f"检测报告已生成：\n{report_path}")

    def on_env_failed(self, message):
        self.env_button.setEnabled(True)
        self.status_label.setText("当前状态：检测失败")
        self.status_label.setStyleSheet("color: #EF4444;")
        QMessageBox.warning(self, "环境检测失败", message)

    def _fill_env_table(self, results):
        self.env_table.setRowCount(len(results))
        colors = {"pass": QColor("#22C55E"), "warn": QColor("#F59E0B"), "fail": QColor("#EF4444")}
        labels = {"pass": "通过", "warn": "警告", "fail": "失败"}
        for row, item in enumerate(results):
            values = [
                item.get("name", ""),
                labels.get(item.get("status"), item.get("status", "")),
                item.get("value", ""),
                item.get("suggestion", ""),
            ]
            for col, value in enumerate(values):
                table_item = QTableWidgetItem(value)
                table_item.setForeground(colors.get(item.get("status"), QColor("#E5E7EB")) if col == 1 else QColor("#E5E7EB"))
                self.env_table.setItem(row, col, table_item)
        self.env_table.resizeColumnsToContents()

    def open_report(self):
        if not os.path.exists(ENV_REPORT_FILE):
            QMessageBox.information(self, "暂无报告", "还没有检测报告，请先点击“检测运行环境”。")
            return
        os.startfile(os.path.abspath(ENV_REPORT_FILE))

    def choose_reference_audio(self):
        path = self._choose_audio_to(self.clone_ref_audio)
        if path:
            self.clone_preview.set_audio(path)
            self.config["last_reference_audio"] = path
            save_config(self.config)

    def choose_ultimate_reference_audio(self):
        path = self._choose_audio_to(self.ultimate_ref_audio)
        if not path:
            return
        self.config["last_reference_audio"] = path
        save_config(self.config)
        self.load_ultimate_reference_preview(path)
        self.start_ultimate_transcribe()

    def choose_batch_reference_audio(self):
        path = self._choose_audio_to(self.batch_ref_audio)
        if path:
            self.batch_preview.set_audio(path)

    def load_ultimate_reference_preview(self, audio_path):
        self.ultimate_preview.set_audio(audio_path)

    def on_prepare_selection_changed(self, start, duration):
        self.prepare_start.blockSignals(True)
        self.prepare_duration.blockSignals(True)
        self.prepare_start.setValue(start)
        self.prepare_duration.setValue(duration)
        self.prepare_start.blockSignals(False)
        self.prepare_duration.blockSignals(False)
        self.prepare_status.setText(f"已框选 {start:.2f}s - {start + duration:.2f}s，可直接提取为参考音频。")

    def on_prepare_spin_changed(self):
        if hasattr(self, "prepare_preview") and self.prepare_preview.has_audio():
            self.prepare_preview.set_selection_seconds(self.prepare_start.value(), self.prepare_duration.value())

    def on_ultimate_preview_selection_changed(self, start, duration):
        self.ultimate_transcribe_status.setText("参考音频选区已变化，请点击“重新识别文字稿”后再生成高相似克隆。")

    def _ffmpeg_path(self):
        bundled = os.path.join(tools_dir(), "ffmpeg.exe")
        return bundled if os.path.exists(bundled) else "ffmpeg"

    def _reference_audio_for_task(self, line_edit, preview_panel, prefix):
        path = line_edit.text().strip()
        if not path or not os.path.exists(path):
            return path
        if not preview_panel or not preview_panel.has_custom_selection():
            return path
        if os.path.abspath(preview_panel.audio_path) != os.path.abspath(path):
            return path
        start, duration = preview_panel.selection_seconds()
        output_path = make_output_path(self.config.get("output_dir", OUTPUT_DIR), prefix, "wav")
        extract_audio_from_media(self._ffmpeg_path(), path, output_path, start=start, duration=duration, sample_rate=16000)
        line_edit.setText(output_path)
        preview_panel.set_audio(output_path)
        return output_path

    def start_ultimate_transcribe(self):
        audio_path = self.ultimate_ref_audio.text().strip()
        if not audio_path or not os.path.exists(audio_path):
            QMessageBox.warning(self, "缺少参考音频", "请先选择可读取的参考音频。")
            return
        if self.transcribe_worker and self.transcribe_worker.isRunning():
            QMessageBox.information(self, "识别中", "参考音频文字稿正在识别，请稍等。")
            return
        try:
            audio_path = self._reference_audio_for_task(self.ultimate_ref_audio, self.ultimate_preview, "ultimate_ref_clip")
        except Exception as exc:
            QMessageBox.warning(self, "裁剪失败", f"参考音频选区裁剪失败：{exc}")
            return
        self.ultimate_transcribe_status.setText("正在自动识别参考音频文字稿...")
        self.ultimate_transcript.setPlainText("")
        self.transcribe_worker = ReferenceTranscribeWorker(
            audio_path,
            self.config.get("output_dir", OUTPUT_DIR),
            self.config.get("asr_model_path"),
        )
        self.transcribe_worker.progress.connect(self.ultimate_progress.setValue)
        self.transcribe_worker.finished.connect(self.on_ultimate_transcribe_finished)
        self.transcribe_worker.failed.connect(self.on_ultimate_transcribe_failed)
        self.transcribe_worker.start()

    def on_ultimate_transcribe_finished(self, text):
        self.ultimate_progress.setValue(100)
        self.ultimate_transcript.setPlainText(text)
        self.ultimate_transcribe_status.setText("文字稿已自动识别完成，可直接生成，也可以先校对微调。")

    def on_ultimate_transcribe_failed(self, message):
        self.ultimate_progress.setValue(0)
        self.ultimate_transcribe_status.setText(message)
        QMessageBox.warning(self, "自动识别失败", message)

    def _choose_audio_to(self, line_edit):
        path, _ = QFileDialog.getOpenFileName(self, "选择参考音频", os.getcwd(), "音频文件 (*.wav *.mp3 *.m4a *.flac *.aac);;所有文件 (*.*)")
        if path:
            line_edit.setText(path)
        return path

    def choose_batch_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 txt", os.getcwd(), "文本文件 (*.txt);;所有文件 (*.*)")
        if path:
            self.batch_txt.setText(path)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = [x.strip() for x in f.readlines() if x.strip()]
                self.batch_list.clear()
                self.batch_list.addItems(lines[:200])
            except Exception as exc:
                QMessageBox.warning(self, "读取失败", f"txt 文件读取失败：{exc}")

    def choose_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.batch_output_dir.text() or OUTPUT_DIR)
        if path:
            self.batch_output_dir.setText(path)
            self._save_current_config()

    def choose_prepare_input(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择音视频文件",
            os.getcwd(),
            "音视频文件 (*.mp4 *.mov *.mkv *.avi *.flv *.webm *.wav *.mp3 *.m4a *.flac *.aac);;所有文件 (*.*)",
        )
        if not path:
            return
        self.prepare_input.setText(path)
        self.prepare_preview.set_audio(path)
        ffprobe = os.path.join(tools_dir(), "ffprobe.exe")
        duration = get_media_duration(ffprobe if os.path.exists(ffprobe) else "ffprobe", path)
        if duration > 0:
            self.prepare_status.setText(f"已读取文件，总时长约 {duration:.2f} 秒。建议截取 10-30 秒清晰人声。")
            if self.prepare_duration.value() <= 0 or self.prepare_duration.value() > duration:
                self.prepare_duration.setValue(min(30.0, duration))
        else:
            self.prepare_status.setText("已选择文件。未能读取总时长，但仍可直接提取或裁剪。")

    def prepare_reference_audio(self):
        if self.audio_prepare_worker and self.audio_prepare_worker.isRunning():
            QMessageBox.information(self, "处理中", "参考音频正在处理，请稍等。")
            return
        input_path = self.prepare_input.text().strip()
        if not input_path or not os.path.exists(input_path):
            QMessageBox.warning(self, "缺少文件", "请先选择要提取/裁剪的音视频文件。")
            return
        self._save_current_config()
        self.prepare_btn.setEnabled(False)
        self.prepare_progress.setValue(0)
        self.prepare_output.clear()
        self.prepare_status.setText("正在处理参考音频...")
        task = {
            "input_path": input_path,
            "output_dir": self.config.get("output_dir", OUTPUT_DIR),
            "prefix": "prepared_reference",
            "start": self.prepare_start.value(),
            "duration": self.prepare_duration.value(),
            "normalize_audio": self.prepare_normalize.isChecked(),
        }
        self.audio_prepare_worker = AudioPrepareWorker(task)
        self.audio_prepare_worker.progress.connect(self.prepare_progress.setValue)
        self.audio_prepare_worker.message.connect(self.prepare_status.setText)
        self.audio_prepare_worker.finished.connect(self.on_prepare_finished)
        self.audio_prepare_worker.failed.connect(self.on_prepare_failed)
        self.audio_prepare_worker.start()

    def on_prepare_finished(self, output_path):
        self.prepare_btn.setEnabled(True)
        self.prepare_output.setText(output_path)
        self.prepare_status.setText("参考音频已处理完成，可设为普通克隆或高相似克隆的参考音频。")

    def on_prepare_failed(self, message):
        self.prepare_btn.setEnabled(True)
        self.prepare_progress.setValue(0)
        self.prepare_status.setText(message)
        QMessageBox.warning(self, "参考音频处理失败", message)

    def _prepared_audio_path(self):
        path = self.prepare_output.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.information(self, "暂无处理结果", "请先完成参考音频提取/裁剪。")
            return ""
        return path

    def set_prepared_audio_to_clone(self):
        path = self._prepared_audio_path()
        if not path:
            return
        self.clone_ref_audio.setText(path)
        self.config["last_reference_audio"] = path
        save_config(self.config)
        self.tabs.setCurrentIndex(2)

    def set_prepared_audio_to_ultimate(self):
        path = self._prepared_audio_path()
        if not path:
            return
        self.ultimate_ref_audio.setText(path)
        self.config["last_reference_audio"] = path
        save_config(self.config)
        self.load_ultimate_reference_preview(path)
        self.tabs.setCurrentIndex(3)
        self.start_ultimate_transcribe()

    def start_batch_generate(self):
        if self.batch_worker and self.batch_worker.isRunning():
            QMessageBox.information(self, "批量生成中", "当前批量生成任务还在运行，请稍等。")
            return
        txt_path = self.batch_txt.text().strip()
        if not txt_path or not os.path.exists(txt_path):
            QMessageBox.warning(self, "缺少 txt", "请先导入 txt 文件，每一行生成一个音频。")
            return
        model_path = self.model_path_edit.text().strip()
        ok, msg, _ = check_model_path(model_path)
        if not ok:
            QMessageBox.warning(self, "模型路径错误", f"{msg}\n请先选择正确的 VoxCPM2 模型目录。")
            return
        denoise = self.batch_denoise.isChecked()
        zipenhancer_path = self.config.get("zipenhancer_model_path", ZIPENHANCER_MODEL_DIR)
        if denoise and not os.path.isdir(zipenhancer_path):
            QMessageBox.warning(self, "缺少降噪模型", "参考音频降噪增强需要 ZipEnhancer 模型，请先把该模型放入网盘资源包/models/speech_zipenhancer_ans_multiloss_16k_base。")
            return
        self._save_current_config()
        self.batch_progress.setValue(0)
        self.batch_counts.setText("正在生成...")
        try:
            batch_ref_audio = self._reference_audio_for_task(self.batch_ref_audio, self.batch_preview, "batch_ref_clip")
        except Exception as exc:
            QMessageBox.warning(self, "裁剪失败", f"批量参考音频选区裁剪失败：{exc}")
            return
        task = {
            "model_path": model_path,
            "input_file": txt_path,
            "reference_audio": batch_ref_audio,
            "prompt_text": self.batch_prompt_text.toPlainText().strip(),
            "control_prompt": self.batch_prompt.text().strip(),
            "output_dir": self.batch_output_dir.text().strip() or self.config.get("output_dir", OUTPUT_DIR),
            "cfg_value": self.batch_cfg.value(),
            "inference_timesteps": self.batch_steps.value(),
            "normalize": self.batch_normalize.isChecked(),
            "denoise": denoise,
            "zipenhancer_model_path": zipenhancer_path,
            "enable_denoiser": denoise,
            "optimize": True,
        }
        self.batch_worker = BatchGenerateWorker(task)
        self.batch_worker.progress.connect(self.batch_progress.setValue)
        self.batch_worker.message.connect(lambda text: self.batch_list.addItem(text))
        self.batch_worker.finished.connect(self.on_batch_finished)
        self.batch_worker.failed.connect(self.on_batch_failed)
        self.batch_worker.start()

    def on_batch_finished(self, message):
        self.batch_progress.setValue(100)
        self.batch_counts.setText(message.split("，输出目录：")[0])
        self.batch_list.addItem(message)
        QMessageBox.information(self, "批量生成完成", message)

    def on_batch_failed(self, message):
        self.batch_progress.setValue(0)
        self.batch_counts.setText("批量生成失败")
        self.batch_list.addItem(message)
        QMessageBox.warning(self, "批量生成失败", message)

    def generate_tts(self):
        text = self.tts_text.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "缺少文本", "请先输入要生成的文本。")
            return
        task = {
            "kind": "tts",
            "model_path": self.model_path_edit.text().strip(),
            "text": text,
            "voice_description": self.tts_voice_desc.text().strip(),
            "cfg_value": self.tts_cfg.value(),
            "inference_timesteps": self.tts_steps.value(),
            "normalize": self.tts_normalize.isChecked(),
            "zipenhancer_model_path": self.config.get("zipenhancer_model_path", ZIPENHANCER_MODEL_DIR),
            "enable_denoiser": False,
            "optimize": True,
            "output_path": make_output_path(self.config.get("output_dir", OUTPUT_DIR), "voice"),
        }
        self._start_generate(task, self.tts_progress, self.tts_output, self.tts_generate_btn)

    def generate_clone(self):
        text = self.clone_text.toPlainText().strip()
        ref_audio = self.clone_ref_audio.text().strip()
        if not ref_audio or not os.path.exists(ref_audio):
            QMessageBox.warning(self, "缺少参考音频", "请先选择可读取的参考音频，建议 wav 或 mp3 格式。")
            return
        if not text:
            QMessageBox.warning(self, "缺少文本", "请先输入要生成的新文本。")
            return
        try:
            ref_audio = self._reference_audio_for_task(self.clone_ref_audio, self.clone_preview, "clone_ref_clip")
        except Exception as exc:
            QMessageBox.warning(self, "裁剪失败", f"参考音频选区裁剪失败：{exc}")
            return
        task = {
            "kind": "clone",
            "model_path": self.model_path_edit.text().strip(),
            "text": text,
            "reference_audio": ref_audio,
            "control_prompt": self.clone_prompt.text().strip(),
            "cfg_value": self.clone_cfg.value(),
            "inference_timesteps": self.clone_steps.value(),
            "normalize": self.clone_normalize.isChecked(),
            "denoise": self.clone_denoise.isChecked(),
            "zipenhancer_model_path": self.config.get("zipenhancer_model_path", ZIPENHANCER_MODEL_DIR),
            "enable_denoiser": self.clone_denoise.isChecked(),
            "optimize": True,
            "output_path": make_output_path(self.config.get("output_dir", OUTPUT_DIR), "clone"),
        }
        self._start_generate(task, self.clone_progress, self.clone_output, self.clone_generate_btn)

    def generate_ultimate_clone(self):
        text = self.ultimate_text.toPlainText().strip()
        ref_audio = self.ultimate_ref_audio.text().strip()
        prompt_text = self.ultimate_transcript.toPlainText().strip()
        if not ref_audio or not os.path.exists(ref_audio):
            QMessageBox.warning(self, "缺少参考音频", "请先选择可读取的参考音频。")
            return
        if not prompt_text:
            QMessageBox.warning(self, "缺少参考音频文字稿", "请先自动识别或手动填写参考音频文字稿。")
            return
        if not text:
            QMessageBox.warning(self, "缺少生成文本", "请先输入要生成的新文本。")
            return
        try:
            ref_audio = self._reference_audio_for_task(self.ultimate_ref_audio, self.ultimate_preview, "ultimate_ref_clip")
        except Exception as exc:
            QMessageBox.warning(self, "裁剪失败", f"参考音频选区裁剪失败：{exc}")
            return
        task = {
            "kind": "ultimate",
            "model_path": self.model_path_edit.text().strip(),
            "text": text,
            "reference_audio": ref_audio,
            "prompt_text": prompt_text,
            "cfg_value": self.ultimate_cfg.value(),
            "inference_timesteps": self.ultimate_steps.value(),
            "normalize": self.ultimate_normalize.isChecked(),
            "denoise": self.ultimate_denoise.isChecked(),
            "zipenhancer_model_path": self.config.get("zipenhancer_model_path", ZIPENHANCER_MODEL_DIR),
            "enable_denoiser": self.ultimate_denoise.isChecked(),
            "optimize": True,
            "output_path": make_output_path(self.config.get("output_dir", OUTPUT_DIR), "ultimate_clone"),
        }
        self._start_generate(task, self.ultimate_progress, self.ultimate_output, self.ultimate_generate_btn)

    def _start_generate(self, task, progress_bar, output_edit, button):
        if self.generate_worker and self.generate_worker.isRunning():
            QMessageBox.information(self, "生成中", "当前已有音频生成任务在运行，请稍等。")
            return
        ok, msg, _ = check_model_path(task["model_path"])
        if not ok:
            QMessageBox.warning(self, "模型路径错误", f"{msg}\n请先选择正确的 VoxCPM2 模型目录。")
            return
        if task.get("denoise") and not os.path.isdir(task.get("zipenhancer_model_path", "")):
            QMessageBox.warning(self, "缺少降噪模型", "参考音频降噪增强需要 ZipEnhancer 模型，请先把该模型放入网盘资源包/models/speech_zipenhancer_ans_multiloss_16k_base。")
            return
        self._save_current_config()
        progress_bar.setValue(0)
        output_edit.clear()
        button.setEnabled(False)
        self.generate_worker = VoiceGenerateWorker(task)
        self.generate_worker.progress.connect(progress_bar.setValue)
        self.generate_worker.finished.connect(lambda path: self._on_generate_finished(path, output_edit, button))
        self.generate_worker.failed.connect(lambda message: self._on_generate_failed(message, button))
        self.generate_worker.start()

    def _on_generate_finished(self, output_path, output_edit, button):
        button.setEnabled(True)
        output_edit.setText(output_path)
        QMessageBox.information(self, "生成完成", f"音频已生成：\n{output_path}")

    def _on_generate_failed(self, message, button):
        button.setEnabled(True)
        QMessageBox.warning(self, "生成失败", message)

    def listen_audio(self, path):
        if not path or not os.path.exists(path):
            QMessageBox.information(self, "暂无音频", "还没有可试听的生成音频。")
            return
        play_audio(path)

    def open_output(self):
        open_output_dir(self.config.get("output_dir", OUTPUT_DIR))
