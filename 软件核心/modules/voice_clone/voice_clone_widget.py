import hashlib
import os
import subprocess
import time
import traceback

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QPainter, QPen
from PyQt5.QtWidgets import (
    QFileDialog,
    QDoubleSpinBox,
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
    QComboBox,
    QSpinBox,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from paths import tools_dir, workspace_path
from services.media_service import build_waveform_peaks
from services.platform_service import open_path
from ui_components import configure_spinbox_for_direct_input

from .audio_tools import extract_audio_from_media, get_media_duration, make_output_path, open_output_dir, play_audio
from .config import ENV_REPORT_FILE, OUTPUT_DIR, ZIPENHANCER_MODEL_DIR, load_config, save_config
from .model_manager import check_model_path
from .task_worker import BatchGenerateWorker, EnvCheckWorker, ReferenceTranscribeWorker, VoiceGenerateWorker


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
        self.setFixedHeight(82)
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
            # 波形点数量可能少于控件宽度，绘制时必须重新采样铺满整条轨道。
            count = max(1, track.width())
            step = len(self.peaks) / max(1, count)
            for i in range(count):
                x = track.left() + i
                peak_index = min(len(self.peaks) - 1, int(i * step))
                peak = self.peaks[peak_index]
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
    """音频预览、播放和框选裁剪控件。使用 ffplay 后台播放，确保音频与进度精准同步。"""

    selectionChanged = pyqtSignal(float, float)

    def __init__(self, title="参考音频预览", parent=None):
        super().__init__(parent)
        self.audio_path = ""
        self.duration_ms = 0
        self._ffplay_process = None
        self._loop_selection = True
        self._play_start_ms = 0
        self._play_end_ms = 0
        self._is_playing = False
        self._tick_start = 0.0
        self.setMinimumHeight(130)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("mutedText")
        self.info_label = QLabel("尚未选择音频")
        self.info_label.setObjectName("mutedText")
        self.info_label.setMaximumHeight(26)
        self.waveform = ReferenceWaveform()
        self.waveform.selectionChanged.connect(self._on_waveform_selection_changed)

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick_progress)

        self.play_btn = QPushButton("播放选区")
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_btn.setMinimumHeight(34)
        self.play_btn.clicked.connect(self.toggle_playback)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.stop_btn.setMinimumHeight(34)
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

        layout.addWidget(self.waveform)
        layout.addLayout(controls)

    # ───── 音频加载 ─────
    def set_audio(self, audio_path):
        self.stop()
        self.audio_path = audio_path or ""
        self.duration_ms = 0
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
        duration = get_media_duration(ffprobe, self.audio_path) if os.path.exists(ffprobe) else 0
        if duration > 0:
            self.duration_ms = int(duration * 1000)
        self.info_label.setText(f"{name}    {size_mb:.2f} MB")
        self._refresh_selection_label()
        self.refresh_preview(0)
        ffmpeg = os.path.join(tools_dir(), "ffmpeg.exe")
        try:
            self.waveform.set_peaks(build_waveform_peaks(ffmpeg, self.audio_path, points=900))
        except Exception:
            self.waveform.set_peaks([])

    # ───── 公共接口 ─────
    def has_audio(self):
        return bool(self.audio_path and os.path.exists(self.audio_path))

    def has_custom_selection(self):
        return self.has_audio() and (self.waveform.selection_start > 0.003 or self.waveform.selection_end < 0.997)

    def selection_seconds(self):
        dur = self.duration_ms / 1000 if self.duration_ms else 0
        if dur <= 0:
            return 0.0, 0.0
        start = self.waveform.selection_start * dur
        end = self.waveform.selection_end * dur
        return max(0.0, start), max(0.0, end - start)

    def set_selection_seconds(self, start, duration):
        total = self.duration_ms / 1000 if self.duration_ms else 0
        if total <= 0:
            return
        end = total if duration <= 0 else min(total, start + duration)
        self.waveform.set_selection(start / total, end / total)

    # ───── ffplay 播放器（解决 QAudioOutput 进度不同步问题）─────
    @staticmethod
    def _startupinfo():
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return si
        return None

    def _kill_ffplay(self):
        if self._ffplay_process:
            try:
                self._ffplay_process.terminate()
            except Exception:
                try:
                    self._ffplay_process.kill()
                except Exception:
                    pass
            self._ffplay_process = None

    def _start_ffplay(self, start_sec, end_sec):
        self._kill_ffplay()
        ffplay = os.path.join(tools_dir(), "ffplay.exe")
        if not os.path.exists(ffplay):
            ffplay = "ffplay"
        dur = end_sec - start_sec
        if dur < 0.05:
            dur = 0.1
        cmd = [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
               "-ss", f"{start_sec:.3f}", "-t", f"{dur:.3f}", self.audio_path]
        try:
            self._ffplay_process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                startupinfo=self._startupinfo()
            )
        except Exception:
            self._ffplay_process = None
            return False
        return True

    def toggle_playback(self):
        if not self.has_audio():
            return
        if self._is_playing:
            self.stop()
            return
        start_ms, end_ms = self._selection_ms()
        start_sec = start_ms / 1000
        end_sec = end_ms / 1000
        self._play_start_ms = start_ms
        self._play_end_ms = end_ms
        if self._start_ffplay(start_sec, end_sec):
            self._is_playing = True
            self._tick_start = time.time()
            self.timer.start()
            self.play_btn.setText("暂停")
            self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
            self.refresh_preview(start_ms)

    def stop(self):
        self.timer.stop()
        self._kill_ffplay()
        self._is_playing = False
        self.waveform.set_progress(0)
        self.time_label.setText(f"{self._fmt_ms(0)} / {self._fmt_ms(self._duration_ms())}")
        self.play_btn.setText("播放选区")
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def _tick_progress(self):
        """定时器回调：基于播放开始时间和系统时钟推算进度（不依赖音频 API）"""
        if not self._is_playing:
            return
        elapsed = int((time.time() - self._tick_start) * 1000)
        position = self._play_start_ms + elapsed
        if self._loop_selection and position >= self._play_end_ms:
            # 循环播放：重启 ffplay
            start_ms = self._play_start_ms
            end_ms = self._play_end_ms
            self._restart_ffplay(start_ms / 1000, end_ms / 1000, start_ms, end_ms)
            return
        # 检查 ffplay 是否仍在运行
        if self._ffplay_process and self._ffplay_process.poll() is not None:
            self.stop()
            return
        if position >= self._duration_ms():
            self.stop()
            return
        self.refresh_preview(position)

    def _restart_ffplay(self, start_sec, end_sec, start_ms, end_ms):
        self._kill_ffplay()
        self._play_start_ms = start_ms
        self._play_end_ms = end_ms
        self._tick_start = time.time()
        if self._start_ffplay(start_sec, end_sec):
            self.refresh_preview(start_ms)
        else:
            self.stop()

    # ───── 波形选择与进度 ─────
    def _on_waveform_selection_changed(self, start_ratio, end_ratio):
        if self._is_playing:
            self.stop()
        start_ms, _ = self._selection_ms()
        self._play_start_ms = start_ms
        self.refresh_preview(start_ms)
        self._refresh_selection_label()
        start, duration = self.selection_seconds()
        self.selectionChanged.emit(start, duration)

    def _selection_ms(self):
        duration = self._duration_ms()
        start = int(duration * self.waveform.selection_start)
        end = int(duration * self.waveform.selection_end)
        if end <= start:
            end = duration
        return start, end

    def _duration_ms(self):
        return self.duration_ms or 0

    def refresh_preview(self, position=None):
        duration = self._duration_ms()
        if position is None:
            position = 0
        position = max(0, min(int(position), duration))
        if duration > 0:
            self.waveform.set_progress(position / duration)
        self.time_label.setText(f"{self._fmt_ms(position)} / {self._fmt_ms(duration)}")

    def _refresh_selection_label(self):
        start, dur = self.selection_seconds()
        if not self.has_audio() or not self.has_custom_selection():
            self.selection_label.setText("选区：全部")
        else:
            self.selection_label.setText(f"选区：{start:.2f}s - {start + dur:.2f}s")

    def _fmt_ms(self, value):
        total = max(0, int(value // 1000))
        return f"{total // 60:02d}:{total % 60:02d}"


class VoiceCloneWidget(QWidget):
    """AI音频克隆功能区，UI 与真实推理逻辑分离。"""

    log_signal = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = load_config()
        self.env_worker = None
        self.generate_worker = None
        self.transcribe_worker = None
        self.batch_worker = None
        self._ultimate_transcribe_signature = None
        self._ultimate_transcript_signature = None
        self._ultimate_transcribe_used_selection = False
        self._build_ui()
        configure_spinbox_for_direct_input(self)
        self._load_config_to_ui()

    def _log(self, message):
        self.log_signal.emit(f"[AI音频克隆] {message}")

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

    def _voice_presets(self):
        return [
            ("自定义输入", ""),
            ("电商带货女声", "年轻女性，清晰有亲和力，语速自然，适合电商直播带货"),
            ("电商带货男声", "年轻男性，干净有活力，表达利落，适合产品讲解和促单"),
            ("自然年轻女性", "年轻女性，自然温柔，声音干净，情绪轻松"),
            ("甜美种草女声", "甜美女声，亲切有感染力，适合种草分享和好物推荐"),
            ("专业广告旁白", "专业广告旁白，吐字清晰，节奏稳定，有高级感"),
            ("沉稳男声讲解", "成熟男性，沉稳可信，适合品牌介绍和产品参数讲解"),
            ("客服说明女声", "成熟女性，耐心清楚，语气温和，适合售后说明和教程"),
            ("直播促单快节奏", "有活力的直播口播，节奏稍快，情绪积极，适合限时促销"),
        ]

    def _on_tts_preset_changed(self, index):
        preset = self.tts_voice_preset.itemData(index)
        if preset:
            self.tts_voice_desc.setText(preset)

    def _build_tts_tab(self):
        page = QWidget()
        layout = QGridLayout(page)
        layout.setColumnStretch(1, 1)

        self.tts_text = QTextEdit()
        self.tts_text.setPlaceholderText("输入要生成的中文或英文文本")
        self.tts_voice_preset = QComboBox()
        for name, prompt in self._voice_presets():
            self.tts_voice_preset.addItem(name, prompt)
        self.tts_voice_desc = QLineEdit()
        self.tts_voice_desc.setPlaceholderText("可手动输入，也可以先从左侧选择预设")
        self.tts_voice_preset.currentIndexChanged.connect(self._on_tts_preset_changed)
        voice_box = QWidget()
        voice_layout = QHBoxLayout(voice_box)
        voice_layout.setContentsMargins(0, 0, 0, 0)
        voice_layout.addWidget(self.tts_voice_preset)
        voice_layout.addWidget(self.tts_voice_desc, 1)
        self.tts_rounds = QSpinBox()
        self.tts_rounds.setRange(1, 100)
        self.tts_rounds.setValue(1)
        self.tts_rounds.setToolTip("一次点击连续生成多少条音频。适合需要多版声音供挑选时使用。")
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
        self._row("声音描述", voice_box, 1, layout)
        self._row("生成轮数", self.tts_rounds, 2, layout)
        self._row("推理参数", param_box, 3, layout)
        self._row("高级选项", advanced_box, 4, layout)
        layout.addWidget(self.tts_generate_btn, 5, 1)
        layout.addWidget(self.tts_progress, 5, 2, 1, 2)
        self._row("输出文件", self.tts_output, 6, layout)
        layout.addWidget(listen_btn, 6, 2)
        layout.addWidget(open_btn, 6, 3)
        layout.setRowStretch(7, 1)
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
        layout.setRowMinimumHeight(1, 126)
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
        self.ultimate_transcript.textChanged.connect(self._on_ultimate_transcript_edited)
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
        self.ultimate_transcribe_status = QLabel("")
        self.ultimate_transcribe_status.setObjectName("mutedText")

        tip = QLabel(
            "拖动波形蓝色选区后，重新识别和高相似克隆都会使用该选区的临时裁剪；"
            "原始参考音频不会被替换或修改。文字稿越准确，相似度越稳定。"
        )
        tip.setObjectName("mutedText")
        tip.setWordWrap(True)

        layout.addWidget(QLabel("参考音频"), 0, 0)
        layout.addWidget(self.ultimate_ref_audio, 0, 1)
        layout.addWidget(choose_btn, 0, 2)
        layout.addWidget(transcribe_btn, 0, 3)
        layout.addWidget(QLabel("参考音频预览"), 1, 0)
        layout.addWidget(self.ultimate_preview, 1, 1, 1, 3)
        layout.setRowMinimumHeight(1, 126)
        layout.addWidget(QLabel("参考音频文字稿"), 2, 0)
        layout.addWidget(self.ultimate_transcript, 2, 1, 1, 3)
        layout.setRowMinimumHeight(2, 92)
        layout.addWidget(QLabel("生成文本"), 3, 0)
        layout.addWidget(self.ultimate_text, 3, 1, 1, 3)
        layout.setRowMinimumHeight(3, 78)
        self._row("推理参数", param_box, 4, layout)
        self._row("高级选项", advanced_box, 5, layout)
        layout.addWidget(self.ultimate_generate_btn, 6, 1)
        layout.addWidget(self.ultimate_progress, 6, 2, 1, 2)
        self._row("输出文件", self.ultimate_output, 7, layout)
        layout.addWidget(listen_btn, 7, 2)
        layout.addWidget(open_btn, 7, 3)
        layout.addWidget(tip, 8, 1, 1, 3)
        layout.setRowStretch(9, 1)
        return page

    def _build_batch_tab(self):
        page = QWidget()
        layout = QGridLayout(page)
        layout.setVerticalSpacing(8)
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
        layout.setRowMinimumHeight(2, 126)
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
        path = QFileDialog.getExistingDirectory(self, "选择 VoxCPM2 模型目录", self.model_path_edit.text() or workspace_path())
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
        self.env_worker.message.connect(self._log)
        self.env_worker.finished.connect(self.on_env_finished)
        self.env_worker.failed.connect(self.on_env_failed)
        self.env_worker.start()

    def on_env_finished(self, results, report_path, passed):
        self.env_button.setEnabled(True)
        self.status_label.setText("当前状态：环境通过" if passed else "当前状态：环境异常")
        self.status_label.setStyleSheet(f"color: {'#22C55E' if passed else '#EF4444'};")
        self._fill_env_table(results)
        self._log(f"环境检测完成：{'通过' if passed else '未通过'}；报告：{report_path}")
        QMessageBox.information(self, "环境检测完成", f"检测报告已生成：\n{report_path}")

    def on_env_failed(self, message):
        self.env_button.setEnabled(True)
        self.status_label.setText("当前状态：检测失败")
        self.status_label.setStyleSheet("color: #EF4444;")
        self._log(message)
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
        open_path(ENV_REPORT_FILE)

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
        try:
            self.start_ultimate_transcribe()
        except Exception:
            traceback.print_exc()
            print('[音频克隆] 自动识别启动失败，可手动点击"重新识别文字稿"', flush=True)

    def choose_batch_reference_audio(self):
        path = self._choose_audio_to(self.batch_ref_audio)
        if path:
            self.batch_preview.set_audio(path)

    def load_ultimate_reference_preview(self, audio_path):
        self.ultimate_preview.set_audio(audio_path)

    def on_ultimate_preview_selection_changed(self, start, duration):
        message = (
            "当前蓝色选区将同时用于文字稿识别和高相似克隆；"
            "请重新识别文字稿，原始音频不会被修改。"
        )
        self.ultimate_transcribe_status.setText(message)
        if not self.ultimate_transcript.toPlainText().strip():
            self.ultimate_transcript.setPlaceholderText(message)

    def _current_ultimate_reference_signature(self):
        if not hasattr(self, 'ultimate_ref_audio'):
            return None
        path = (self.ultimate_ref_audio.text() or "").strip()
        if not path:
            return None
        start = 0.0
        duration = 0.0
        try:
            if self.ultimate_preview.has_custom_selection():
                start, duration = self.ultimate_preview.selection_seconds()
        except Exception:
            pass
        return (
            os.path.normcase(os.path.abspath(path)),
            round(float(start or 0.0), 3),
            round(float(duration or 0.0), 3),
        )

    def _on_ultimate_transcript_edited(self):
        self._ultimate_transcript_signature = self._current_ultimate_reference_signature()

    def _ffmpeg_path(self):
        bundled = os.path.join(tools_dir(), "ffmpeg.exe")
        return bundled if os.path.exists(bundled) else ""

    def _reference_audio_for_task(self, line_edit, preview_panel, prefix):
        path = (line_edit.text() or "").strip() if line_edit else ""
        if not path or not os.path.exists(path):
            return path if path else ""
        if not preview_panel:
            return path
        try:
            has_sel = preview_panel.has_custom_selection()
        except Exception:
            return path
        if not has_sel:
            return path
        try:
            preview_audio = getattr(preview_panel, 'audio_path', '') or ''
            if not preview_audio or os.path.abspath(str(preview_audio)) != os.path.abspath(path):
                return path
        except Exception:
            return path
        try:
            start, duration = preview_panel.selection_seconds()
        except Exception:
            return path
        if duration <= 0.1:
            return path
        ffmpeg_path = self._ffmpeg_path()
        if not ffmpeg_path:
            raise RuntimeError("未找到 ffmpeg，无法裁剪参考音频选区。")
        try:
            stat = os.stat(path)
            identity = (
                f"{os.path.normcase(os.path.abspath(path))}|{stat.st_size}|"
                f"{stat.st_mtime_ns}|{start:.3f}|{duration:.3f}"
            )
        except OSError as exc:
            raise RuntimeError(f"无法读取参考音频：{exc}") from exc
        cache_dir = workspace_path("缓存", "音频克隆参考片段")
        os.makedirs(cache_dir, exist_ok=True)
        digest = hashlib.sha1(identity.encode("utf-8", errors="ignore")).hexdigest()[:20]
        output_path = os.path.join(cache_dir, f"{prefix}_{digest}.wav")
        if os.path.exists(output_path) and os.path.getsize(output_path) >= 512:
            return output_path
        temp_path = output_path + f".{os.getpid()}.tmp.wav"
        try:
            extract_audio_from_media(
                ffmpeg_path, path, temp_path,
                start=start, duration=duration, sample_rate=16000
            )
            if not os.path.exists(temp_path) or os.path.getsize(temp_path) < 512:
                raise RuntimeError("裁剪结果为空或损坏。")
            os.replace(temp_path, output_path)
        except Exception as exc:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            raise RuntimeError(f"参考音频选区裁剪失败：{exc}") from exc
        return output_path

    def start_ultimate_transcribe(self):
        """重新识别参考音频文字稿——带完整异常保护、状态重置与资源释放。"""
        # 1. 输入校验
        if not hasattr(self, 'ultimate_ref_audio'):
            return
        audio_path = (self.ultimate_ref_audio.text() or "").strip()
        if not audio_path or not os.path.exists(audio_path):
            QMessageBox.warning(self, "缺少参考音频", "请先选择可读取的参考音频。")
            return

        # 2. 异步任务冲突防护：终止上一个识别 worker
        if self.transcribe_worker and self.transcribe_worker.isRunning():
            try:
                self.transcribe_worker.cancel()
                self.transcribe_worker.wait(5000)
            except Exception:
                pass
            if self.transcribe_worker.isRunning():
                self.ultimate_transcribe_status.setText("上一次识别任务正在停止，请稍后再试。")
                return
            self.transcribe_worker = None

        # 3. 停止音频预览播放器，释放 ffplay 进程
        try:
            if hasattr(self, 'ultimate_preview') and self.ultimate_preview is not None:
                self.ultimate_preview.stop()
        except Exception:
            pass

        # 4. 安全裁剪选区，但不替换界面中的原始参考音频路径
        use_selection = False
        try:
            if hasattr(self, 'ultimate_preview') and self.ultimate_preview is not None:
                use_selection = self.ultimate_preview.has_custom_selection()
                self._ultimate_transcribe_signature = self._current_ultimate_reference_signature()
                audio_path = self._reference_audio_for_task(
                    self.ultimate_ref_audio, self.ultimate_preview, "ultimate_ref_clip"
                )
        except Exception as exc:
            traceback.print_exc()
            message = str(exc or "参考音频选区裁剪失败")
            self.ultimate_transcribe_status.setText(message)
            QMessageBox.warning(self, "选区裁剪失败", message)
            return

        # 5. 二次校验音频路径
        if not audio_path or not os.path.exists(audio_path):
            QMessageBox.warning(self, "音频不可用", "参考音频文件不可用，请重新选择音频。")
            return
        try:
            if os.path.getsize(audio_path) < 256:
                QMessageBox.warning(self, "音频无效", "参考音频文件过小或损坏，请更换音频文件。")
                return
        except OSError:
            QMessageBox.warning(self, "音频不可读", "无法读取参考音频文件。")
            return

        # 6. 重置 UI 状态
        self.ultimate_transcript.setPlainText("")
        context = "选区" if use_selection else "整段参考音频"
        self._ultimate_transcribe_used_selection = use_selection
        self.ultimate_transcript.setPlaceholderText(f"正在自动识别{context}文字稿，请稍等...")
        self.ultimate_progress.setValue(0)
        self.ultimate_transcribe_status.setText(f"正在自动识别{context}文字稿...")

        # 7. 创建并启动识别 worker
        try:
            # 断开旧 worker 信号，防止重复触发
            old = self.transcribe_worker
            if old:
                try:
                    old.progress.disconnect()
                    old.finished.disconnect()
                    old.failed.disconnect()
                except Exception:
                    pass
            worker = ReferenceTranscribeWorker(
                audio_path,
                self.config.get("output_dir", OUTPUT_DIR),
                self.config.get("asr_model_path"),
            )
            worker.progress.connect(self.ultimate_progress.setValue)
            worker.message.connect(self._log)
            worker.finished.connect(self.on_ultimate_transcribe_finished)
            worker.failed.connect(self.on_ultimate_transcribe_failed)
            self.transcribe_worker = worker
            worker.start()
        except Exception:
            traceback.print_exc()
            self.ultimate_transcribe_status.setText("启动识别失败，请检查模型与依赖环境。")
            QMessageBox.warning(self, "启动失败",
                '无法启动文字稿识别。\n请先点击"检测运行环境"，确认 SenseVoiceSmall 模型可用。')

    def on_ultimate_transcribe_finished(self, text):
        try:
            if self._ultimate_transcribe_signature != self._current_ultimate_reference_signature():
                self.ultimate_progress.setValue(0)
                self.ultimate_transcribe_status.setText(
                    "识别期间参考音频或选区发生变化，本次旧结果已忽略，请重新识别。"
                )
                return
            self.ultimate_progress.setValue(100)
            self.ultimate_transcript.setPlainText(str(text or ""))
            self.ultimate_transcript.setPlaceholderText(
                "选择参考音频后会自动识别文字稿，识别完成后可在这里校对微调。")
            context = "选区" if self._ultimate_transcribe_used_selection else "整段音频"
            self.ultimate_transcribe_status.setText(
                f"{context}文字稿已识别完成，可直接生成，也可以先校对微调。")
            self._log("参考音频文字稿识别完成。")
        except Exception:
            print("[音频克隆] on_ultimate_transcribe_finished 异常，但识别已完成", flush=True)

    def on_ultimate_transcribe_failed(self, message):
        try:
            self.ultimate_progress.setValue(0)
            msg = str(message or "识别失败")
            self.ultimate_transcript.setPlaceholderText(msg)
            self.ultimate_transcribe_status.setText(msg)
            self._log(msg)
            QMessageBox.warning(self, "自动识别失败", msg)
        except Exception:
            traceback.print_exc()
            print(f"[音频克隆] 识别失败回调异常，原始消息：{message}", flush=True)

    def _choose_audio_to(self, line_edit):
        path, _ = QFileDialog.getOpenFileName(self, "选择参考音频", workspace_path('素材', '音频素材'), "音频文件 (*.wav *.mp3 *.m4a *.flac *.aac);;所有文件 (*.*)")
        if path:
            line_edit.setText(path)
        return path

    def choose_batch_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 txt", workspace_path(), "文本文件 (*.txt);;所有文件 (*.*)")
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
        self.batch_worker.message.connect(self._log)
        self.batch_worker.finished.connect(self.on_batch_finished)
        self.batch_worker.failed.connect(self.on_batch_failed)
        self.batch_worker.start()

    def on_batch_finished(self, message):
        self.batch_progress.setValue(100)
        self.batch_counts.setText(message.split("，输出目录：")[0])
        self.batch_list.addItem(message)
        self._log(message)
        QMessageBox.information(self, "批量生成完成", message)

    def on_batch_failed(self, message):
        self.batch_progress.setValue(0)
        self.batch_counts.setText("批量生成失败")
        self.batch_list.addItem(message)
        self._log(message)
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
        if self._ultimate_transcript_signature != self._current_ultimate_reference_signature():
            QMessageBox.warning(
                self,
                "文字稿与选区不一致",
                "参考音频或蓝色选区已变化，请重新识别文字稿，或者按当前选区重新手动校对文字稿。",
            )
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
        self.generate_worker.message.connect(self._log)
        self.generate_worker.finished.connect(lambda path: self._on_generate_finished(path, output_edit, button))
        self.generate_worker.failed.connect(lambda message: self._on_generate_failed(message, button))
        self.generate_worker.start()

    def _on_generate_finished(self, output_path, output_edit, button):
        button.setEnabled(True)
        output_edit.setText(output_path)
        self._log(f"音频生成完成：{output_path}")
        QMessageBox.information(self, "生成完成", f"音频已生成：\n{output_path}")

    def _on_generate_failed(self, message, button):
        button.setEnabled(True)
        self._log(message)
        QMessageBox.warning(self, "生成失败", message)

    def prepare_app_close(self):
        for preview_name in ("clone_preview", "ultimate_preview", "batch_preview"):
            preview = getattr(self, preview_name, None)
            if preview:
                try:
                    preview.stop()
                except Exception:
                    pass
        for worker_name in ("generate_worker", "batch_worker", "transcribe_worker"):
            worker = getattr(self, worker_name, None)
            if worker and worker.isRunning():
                try:
                    worker.cancel()
                    worker.wait(8000)
                except Exception:
                    pass

    def listen_audio(self, path):
        if not path or not os.path.exists(path):
            QMessageBox.information(self, "暂无音频", "还没有可试听的生成音频。")
            return
        play_audio(path)

    def open_output(self):
        open_output_dir(self.config.get("output_dir", OUTPUT_DIR))
