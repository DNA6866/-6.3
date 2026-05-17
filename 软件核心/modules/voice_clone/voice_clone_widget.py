import os

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QBrush, QColor, QPainter, QPen
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
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

from .audio_tools import get_media_duration, make_output_path, open_output_dir, play_audio
from .config import APP_ROOT, ENV_REPORT_FILE, OUTPUT_DIR, ZIPENHANCER_MODEL_DIR, load_config, save_config
from .model_manager import check_model_path
from .task_worker import AudioPrepareWorker, BatchGenerateWorker, EnvCheckWorker, ReferenceTranscribeWorker, VoiceGenerateWorker


class ReferenceWaveform(QWidget):
    """参考音频波形展示，用于核对自动识别文字稿。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.peaks = []
        self.progress = 0.0
        self.setMinimumHeight(92)

    def set_peaks(self, peaks):
        self.peaks = peaks or []
        self.progress = 0.0
        self.update()

    def set_progress(self, progress):
        self.progress = max(0.0, min(1.0, float(progress)))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(8, 8, -8, -8)
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.setBrush(QBrush(QColor("#020617")))
        painter.drawRoundedRect(rect, 8, 8)

        mid = rect.center().y()
        available_h = max(12, rect.height() - 18)
        if self.peaks:
            count = min(max(1, rect.width() - 14), len(self.peaks))
            step = len(self.peaks) / max(1, count)
            for i in range(count):
                x = rect.left() + 7 + i
                peak = self.peaks[int(i * step)]
                bar_h = max(2, int(peak * available_h))
                color = QColor("#38BDF8") if i / max(1, count) <= self.progress else QColor("#475569")
                painter.setPen(QPen(color, 1))
                painter.drawLine(x, mid - bar_h // 2, x, mid + bar_h // 2)
        else:
            painter.setPen(QPen(QColor("#64748B"), 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(rect.left() + 12, mid, rect.right() - 12, mid)

        play_x = rect.left() + int(rect.width() * self.progress)
        painter.setPen(QPen(QColor("#F8FAFC"), 2))
        painter.drawLine(play_x, rect.top() + 8, play_x, rect.bottom() - 8)


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
        self.ultimate_player = None
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

        root.addWidget(self._build_model_group())
        root.addWidget(self._build_env_table())

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
        self.env_table.setMinimumHeight(170)
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
        steps = QSpinBox()
        steps.setRange(1, 100)
        steps.setValue(int(self.config.get("default_inference_timesteps", 10)))
        box = QWidget()
        line = QHBoxLayout(box)
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(QLabel("cfg_value"))
        line.addWidget(cfg)
        line.addSpacing(18)
        line.addWidget(QLabel("inference_timesteps"))
        line.addWidget(steps)
        line.addStretch(1)
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
        self._row("截取参数", trim_box, 1, layout)
        layout.addWidget(self.prepare_btn, 2, 1)
        layout.addWidget(self.prepare_progress, 2, 2, 1, 2)
        self._row("处理状态", self.prepare_status, 3, layout)
        self._row("输出音频", self.prepare_output, 4, layout)
        layout.addWidget(listen_btn, 4, 2)
        layout.addWidget(open_btn, 4, 3)
        layout.addWidget(to_clone_btn, 5, 1)
        layout.addWidget(to_ultimate_btn, 5, 2)
        layout.addWidget(tip, 6, 1, 1, 3)
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
        layout.addWidget(QLabel("生成文本"), 1, 0)
        layout.addWidget(self.clone_text, 1, 1, 1, 3)
        self._row("风格提示词", self.clone_prompt, 2, layout)
        self._row("推理参数", param_box, 3, layout)
        self._row("高级选项", advanced_box, 4, layout)
        layout.addWidget(self.clone_generate_btn, 5, 1)
        layout.addWidget(self.clone_progress, 5, 2, 1, 2)
        self._row("输出文件", self.clone_output, 6, layout)
        layout.addWidget(listen_btn, 6, 2)
        layout.addWidget(open_btn, 6, 3)
        layout.setRowStretch(7, 1)
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
        self.ultimate_waveform = ReferenceWaveform()
        self.ultimate_audio_info = QLabel("尚未选择参考音频")
        self.ultimate_audio_info.setObjectName("mutedText")
        self.ultimate_play_btn = QPushButton("播放")
        self.ultimate_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.ultimate_play_btn.clicked.connect(self.toggle_ultimate_reference_playback)
        stop_ref_btn = QPushButton("停止")
        stop_ref_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        stop_ref_btn.clicked.connect(self.stop_ultimate_reference_playback)
        self.ultimate_position_label = QLabel("00:00 / 00:00")
        self.ultimate_position_label.setObjectName("mutedText")
        player_row = QHBoxLayout()
        player_row.setContentsMargins(0, 0, 0, 0)
        player_row.addWidget(self.ultimate_play_btn)
        player_row.addWidget(stop_ref_btn)
        player_row.addWidget(self.ultimate_position_label)
        player_row.addStretch(1)
        player_box = QWidget()
        player_box.setLayout(player_row)
        self.ultimate_transcript = QTextEdit()
        self.ultimate_transcript.setPlaceholderText("选择参考音频后会自动识别文字稿，识别完成后可在这里校对微调。")
        self.ultimate_text = QTextEdit()
        self.ultimate_text.setPlaceholderText("输入要生成的新文本")
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

        tip = QLabel("高相似克隆会自动识别参考音频文字稿，但建议生成前人工快速校对一次，文字稿越准，相似度越稳定。")
        tip.setObjectName("mutedText")
        tip.setWordWrap(True)

        layout.addWidget(QLabel("参考音频"), 0, 0)
        layout.addWidget(self.ultimate_ref_audio, 0, 1)
        layout.addWidget(choose_btn, 0, 2)
        layout.addWidget(transcribe_btn, 0, 3)
        layout.addWidget(QLabel("参考音频预览"), 1, 0)
        layout.addWidget(self.ultimate_audio_info, 1, 1, 1, 3)
        layout.addWidget(self.ultimate_waveform, 2, 1, 1, 3)
        layout.addWidget(player_box, 3, 1, 1, 3)
        layout.addWidget(QLabel("识别状态"), 4, 0)
        layout.addWidget(self.ultimate_transcribe_status, 4, 1, 1, 3)
        layout.addWidget(QLabel("参考音频文字稿"), 5, 0)
        layout.addWidget(self.ultimate_transcript, 5, 1, 1, 3)
        layout.addWidget(QLabel("生成文本"), 6, 0)
        layout.addWidget(self.ultimate_text, 6, 1, 1, 3)
        self._row("推理参数", param_box, 7, layout)
        self._row("高级选项", advanced_box, 8, layout)
        layout.addWidget(self.ultimate_generate_btn, 9, 1)
        layout.addWidget(self.ultimate_progress, 9, 2, 1, 2)
        self._row("输出文件", self.ultimate_output, 10, layout)
        layout.addWidget(listen_btn, 10, 2)
        layout.addWidget(open_btn, 10, 3)
        layout.addWidget(tip, 11, 1, 1, 3)
        layout.setRowStretch(12, 1)
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
        ref_btn.clicked.connect(lambda: self._choose_audio_to(self.batch_ref_audio))
        self.batch_output_dir = QLineEdit(self.config.get("output_dir", OUTPUT_DIR))
        output_btn = QPushButton("选择输出目录")
        output_btn.clicked.connect(self.choose_output_dir)
        self.batch_prompt = QLineEdit()
        self.batch_prompt.setPlaceholderText("无参考音频时作为声音描述；有参考音频时作为风格提示词")
        self.batch_prompt_text = QTextEdit()
        self.batch_prompt_text.setPlaceholderText("可选：填写参考音频文字稿后使用高相似批量克隆；为空则使用普通参考音频克隆。")
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
        layout.addWidget(QLabel("输出目录"), 2, 0)
        layout.addWidget(self.batch_output_dir, 2, 1)
        layout.addWidget(output_btn, 2, 2)
        self._row("风格/声音描述", self.batch_prompt, 3, layout)
        layout.addWidget(QLabel("参考音频文字稿"), 4, 0)
        layout.addWidget(self.batch_prompt_text, 4, 1, 1, 2)
        self._row("推理参数", param_box, 5, layout)
        self._row("高级选项", advanced_box, 6, layout)
        layout.addWidget(start_btn, 7, 1)
        layout.addWidget(self.batch_progress, 7, 2)
        layout.addWidget(self.batch_counts, 7, 3)
        layout.addWidget(QLabel("任务列表"), 8, 0)
        layout.addWidget(self.batch_list, 8, 1, 1, 3)
        return page

    def _load_config_to_ui(self):
        self.model_path_edit.setText(self.config.get("model_path", ""))
        if hasattr(self, "clone_ref_audio"):
            self.clone_ref_audio.setText(self.config.get("last_reference_audio", ""))
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

    def _ensure_ultimate_player(self):
        if self.ultimate_player:
            return
        self.ultimate_player = QMediaPlayer(self)
        self.ultimate_player.positionChanged.connect(self.on_ultimate_position_changed)
        self.ultimate_player.durationChanged.connect(self.on_ultimate_duration_changed)
        self.ultimate_player.stateChanged.connect(self.on_ultimate_player_state_changed)

    def load_ultimate_reference_preview(self, audio_path):
        self.stop_ultimate_reference_playback()
        self._ensure_ultimate_player()
        self.ultimate_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(audio_path))))
        name = os.path.basename(audio_path)
        size_mb = os.path.getsize(audio_path) / 1024 / 1024 if os.path.exists(audio_path) else 0
        self.ultimate_audio_info.setText(f"{name}    {size_mb:.2f} MB")
        self.ultimate_position_label.setText("00:00 / 00:00")
        self.ultimate_waveform.set_progress(0)
        ffmpeg = os.path.join(tools_dir(), "ffmpeg.exe")
        try:
            self.ultimate_waveform.set_peaks(build_waveform_peaks(ffmpeg, audio_path, points=900))
        except Exception:
            self.ultimate_waveform.set_peaks([])

    def toggle_ultimate_reference_playback(self):
        audio_path = self.ultimate_ref_audio.text().strip()
        if not audio_path or not os.path.exists(audio_path):
            QMessageBox.information(self, "暂无参考音频", "请先选择参考音频。")
            return
        self._ensure_ultimate_player()
        if self.ultimate_player.mediaStatus() == QMediaPlayer.NoMedia:
            self.ultimate_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(audio_path))))
        if self.ultimate_player.state() == QMediaPlayer.PlayingState:
            self.ultimate_player.pause()
        else:
            self.ultimate_player.play()

    def stop_ultimate_reference_playback(self):
        if self.ultimate_player:
            self.ultimate_player.stop()
        if hasattr(self, "ultimate_waveform"):
            self.ultimate_waveform.set_progress(0)

    def on_ultimate_position_changed(self, position):
        duration = self.ultimate_player.duration() if self.ultimate_player else 0
        if duration > 0:
            self.ultimate_waveform.set_progress(position / duration)
        self.ultimate_position_label.setText(f"{self._fmt_ms(position)} / {self._fmt_ms(duration)}")

    def on_ultimate_duration_changed(self, duration):
        position = self.ultimate_player.position() if self.ultimate_player else 0
        self.ultimate_position_label.setText(f"{self._fmt_ms(position)} / {self._fmt_ms(duration)}")

    def on_ultimate_player_state_changed(self, state):
        if state == QMediaPlayer.PlayingState:
            self.ultimate_play_btn.setText("暂停")
            self.ultimate_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        else:
            self.ultimate_play_btn.setText("播放")
            self.ultimate_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def _fmt_ms(self, value):
        total = max(0, int(value // 1000))
        return f"{total // 60:02d}:{total % 60:02d}"

    def start_ultimate_transcribe(self):
        audio_path = self.ultimate_ref_audio.text().strip()
        if not audio_path or not os.path.exists(audio_path):
            QMessageBox.warning(self, "缺少参考音频", "请先选择可读取的参考音频。")
            return
        if self.transcribe_worker and self.transcribe_worker.isRunning():
            QMessageBox.information(self, "识别中", "参考音频文字稿正在识别，请稍等。")
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
        task = {
            "model_path": model_path,
            "input_file": txt_path,
            "reference_audio": self.batch_ref_audio.text().strip(),
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
