import os
import subprocess
import time

from PyQt5.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from paths import models_dir, tools_dir, workspace_path
from services.media_service import build_waveform_peaks, extract_reference_frame
from services.platform_service import open_path
from ui_components import (
    AudioRangeSelector,
    DragPreviewCanvas,
    FileDropArea,
    PlainPasteTextEdit,
    WATERMARK_PREVIEW_FONT_SCALE,
)
from modules.short_video.task_worker import (
    ShortVideoDownloadWorker,
    ShortVideoParseWorker,
    ShortVideoTranscriptWorker,
)
from .workers import AVProcessWorker, ASRPreloadWorker


class AVProcessingMixin:
    def build_av_processing_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        nav_card = QFrame()
        nav_card.setObjectName("card")
        nav_card.setFixedWidth(230)
        nav_layout = QVBoxLayout(nav_card)
        nav_layout.setContentsMargins(14, 14, 14, 14)
        nav_title = QLabel("音视频处理")
        nav_title.setObjectName("appTitle")
        nav_layout.addWidget(nav_title)

        self.av_stack = QStackedWidget()
        self.av_btn_group = QButtonGroup()
        av_tools = [
            ('抖快视频解析', self.build_av_short_video_page,  None),
            ('视频分离音频', self.build_av_extract_audio_page, 'extract_audio'),
            ('音视频转换格式', self.build_av_convert_page, 'convert'),
            ('批量加封面', self.build_av_add_cover_page,  'add_cover'),
            ('视频加字幕', self.build_av_add_subtitle_page, 'add_subtitle'),
            ('视频加文字水印', self.build_av_text_watermark_page, 'text_watermark'),
            ('音视频转文本', self.build_av_transcribe_page, 'transcribe'),
            ('音频裁剪', self.build_av_trim_page, 'trim_audio'),
            ('保留人声', self.build_av_vocal_page, 'vocal_enhance'),
        ]

        for i, (name, builder, kind) in enumerate(av_tools):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setProperty("navButton", True)
            if i == 0:
                btn.setChecked(True)
            self.av_btn_group.addButton(btn, i)
            nav_layout.addWidget(btn)
            self.av_stack.addWidget(builder())
            if kind:
                self.av_page_kinds[i] = kind

        nav_layout.addStretch()
        self.av_btn_group.idClicked.connect(self._on_av_page_switched)
        layout.addWidget(nav_card)
        layout.addWidget(self.av_stack, stretch=1)
        return page

    # ───────── 音视频子功能目录串联 ─────────
    def _on_av_page_switched(self, index):
        """导航切换时触发：切换页面 + 尝试从上游自动填入输入文件"""
        self.av_stack.setCurrentIndex(index)
        kind = self.av_page_kinds.get(index)
        if not kind:
            return
        self._try_av_chain_fill(kind)

    def _try_av_chain_fill(self, kind):
        """检查是否存在上游输出，自动填入当前页面的输入框"""
        upstream_kind = self.AV_CHAIN_MAP.get(kind)
        if not upstream_kind:
            return
        # 上游无输出 → 尝试备用链
        if upstream_kind not in self.av_last_outputs or not self.av_last_outputs[upstream_kind]:
            fallback = self.AV_CHAIN_FALLBACK.get(kind)
            if fallback and fallback in self.av_last_outputs and self.av_last_outputs[fallback]:
                upstream_kind = fallback
            else:
                return
        outputs = self.av_last_outputs[upstream_kind]
        existing = [p for p in outputs if os.path.exists(p)]
        if not existing:
            return
        # 检查当前输入框是否已有内容（用户手动导入的文件不覆盖）
        input_line = self.av_input_lines.get(kind)
        if input_line and input_line.text().strip() and os.path.exists(input_line.text().split(';')[0].strip()):
            return
        # 自动填入上游输出
        fill_path = ";".join(existing)
        if input_line:
            input_line.setText(fill_path)
        chain_name = {'extract_audio': '视频分离音频', 'convert': '音视频转换格式',
                      'vocal_enhance': '保留人声', 'trim_audio': '音频裁剪'}.get(upstream_kind, upstream_kind)
        self.update_log(f'[目录串联] 自动填入{chain_name}的输出文件到当前输入框，总共 {len(existing)} 个文件。')

    def on_av_task_finished(self, ok, message):
        kind = getattr(self, 'current_av_kind', '')
        worker = getattr(self, 'av_worker', None)
        outputs = getattr(worker, 'output_paths', None)
        if outputs is not None and kind:
            self.av_last_outputs[kind] = [path for path in outputs if os.path.isfile(path)]
        if ok:
            self.task_progress.setValue(100)
            self.update_log(f"[音视频处理] 完成: {message}")
            # 记录输出文件路径，供下游子功能自动串联
            kind = getattr(self, 'current_av_kind', '')
            if kind:
                outputs = [p.strip() for p in str(message).split('；') if p.strip() and os.path.exists(p.strip())]
                if outputs:
                    self.av_last_outputs[kind] = outputs
            if kind == 'transcribe':
                self.load_transcribe_result(message)
        else:
            self.update_log(f"[音视频处理] 失败: {message}")
            QMessageBox.warning(self, "处理失败", message)

    def build_av_tool_page(self, title, desc, input_filter, allowed_exts, controls, run_callback, kind=None):
        page = QFrame()
        page.setObjectName("card")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("appTitle")
        desc_lbl = QLabel(desc)
        desc_lbl.setObjectName("appSubtitle")
        desc_lbl.setWordWrap(True)
        layout.addWidget(title_lbl)
        layout.addWidget(desc_lbl)

        input_line = QLineEdit()
        input_line.setPlaceholderText("选择或拖入文件 — 切换子功能时将自动匹配上游输出")
        if kind:
            self.av_input_lines[kind] = input_line
            QTimer.singleShot(100, lambda k=kind: self._try_av_chain_fill(k))
        drop = FileDropArea("拖拽音视频文件到这里，或点击选择一个/多个文件")
        drop.filesDropped.connect(lambda files, line=input_line: line.setText(";".join(files)))
        drop.clicked.connect(lambda line=input_line, filt=input_filter: self.choose_av_inputs(line, filt))
        layout.addWidget(drop)

        row = QHBoxLayout()
        row.addWidget(input_line, stretch=1)
        clear_btn = QPushButton("清空选择")
        clear_btn.clicked.connect(input_line.clear)
        row.addWidget(clear_btn)
        layout.addLayout(row)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        for label, widget in controls:
            form.addRow(label, widget)
        layout.addLayout(form)

        layout.addLayout(self.build_av_output_row())

        run_btn = QPushButton("开始处理")
        run_btn.setObjectName("startButton")
        run_btn.clicked.connect(lambda: run_callback(input_line))
        layout.addWidget(run_btn)
        layout.addStretch()
        return page

    def build_av_output_row(self):
        row = QHBoxLayout()
        output_hint = QLabel(f"输出目录: {os.path.abspath(self.av_output_dir)}")
        output_hint.setObjectName("appSubtitle")
        output_hint.setWordWrap(True)
        open_btn = QPushButton("打开输出目录")
        open_btn.clicked.connect(self.open_av_output_dir)
        row.addWidget(output_hint, stretch=1)
        row.addWidget(open_btn)
        return row

    def open_av_output_dir(self):
        os.makedirs(self.av_output_dir, exist_ok=True)
        try:
            open_path(self.av_output_dir)
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开输出目录：{exc}")

    def choose_av_inputs(self, line_edit, file_filter):
        files, _ = QFileDialog.getOpenFileNames(self, "选择音视频文件", self._file_dialog_default_dir(), file_filter)
        if files:
            self._record_file_dialog_dirs(files)
            line_edit.setText(";".join(files))

    def _split_input_paths(self, text):
        return [p.strip().strip('"') for p in text.split(';') if p.strip()]

    def _av_output_path(self, input_path, suffix, ext):
        os.makedirs(self.av_output_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(input_path))[0]
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stem = f"{base}_{suffix}_{stamp}"
        reserved = getattr(self, '_av_reserved_outputs', None)
        if reserved is None:
            self._av_reserved_outputs = reserved = set()
        path = os.path.abspath(os.path.join(self.av_output_dir, f"{stem}.{ext}"))
        number = 1
        while os.path.exists(path) or os.path.normcase(path) in reserved:
            number += 1
            path = os.path.abspath(os.path.join(self.av_output_dir, f"{stem}_{number}.{ext}"))
        reserved.add(os.path.normcase(path))
        return path

    def get_media_duration(self, input_path):
        ffprobe = os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe'))
        if not os.path.exists(ffprobe) or not os.path.exists(input_path):
            return 0.0
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self.get_si(), timeout=8,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            return max(0.0, float(res.stdout.strip()))
        except Exception:
            return 0.0

    def _start_av_task(self, task):
        if hasattr(self, 'av_worker') and self.av_worker.isRunning():
            QMessageBox.warning(self, "任务进行中", "当前已有音视频处理任务在运行。")
            return
        inputs = self._split_input_paths(task.pop('input_text', task.get('input', '')))
        if not inputs:
            inputs = [task.get('input', '')]
        inputs = [p for p in inputs if p and os.path.exists(p)]
        if not inputs:
            QMessageBox.warning(self, "缺少文件", "请先选择有效的音视频文件。")
            return
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        if not os.path.exists(ffmpeg):
            QMessageBox.warning(self, "缺少组件", "未找到 tools/ffmpeg.exe。")
            return

        jobs = []
        self._av_reserved_outputs = set()
        for input_path in inputs:
            job = task.copy()
            covers = job.pop('cover_paths', [])
            if covers:
                job['cover_path'] = covers[(len(jobs)) % len(covers)]
            job.update({
                'input': input_path,
                'output': self._av_output_path(input_path, job.get('suffix', '输出'), job.get('output_ext', job.get('format', 'mp4'))),
                'ffmpeg': ffmpeg,
                'ffprobe': os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe')),
                'tools_dir': os.path.abspath(tools_dir()),
                'models_dir': os.path.abspath(models_dir()),
                'cache_dir': os.path.abspath(getattr(self, 'cache_dir', workspace_path('缓存'))),
                'output_dir': os.path.abspath(self.av_output_dir),
                'startupinfo': self.get_si(),
            })
            jobs.append(job)
        self.task_progress.setValue(0)
        self.current_av_kind = task.get('kind', '')
        self.av_last_outputs.pop(self.current_av_kind, None)
        if task.get('kind') == 'add_subtitle':
            # 视频加字幕：使用独立 worker（含 ASR + ASS 烧录），支持多视频逐个处理
            from .workers import AddSubtitleWorker
            self.av_worker = AddSubtitleWorker({'jobs': jobs, 'startupinfo': self.get_si()})
        else:
            self.av_worker = AVProcessWorker({'jobs': jobs, 'startupinfo': self.get_si()})
        self.av_worker.log_signal.connect(lambda text: self.update_log(f"[音视频处理] {text}"))
        self.av_worker.progress_signal.connect(self.task_progress.setValue)
        self.av_worker.finished_signal.connect(self.on_av_task_finished)
        self.av_worker.start()
        self.update_log("[音视频处理] 任务已进入后台处理。")

    def load_transcribe_result(self, message):
        if not hasattr(self, 'transcribe_result_text'):
            return
        paths = [p for p in str(message).split('；') if p.strip()]
        if len(paths) != 1:
            self.transcribe_result_text.setPlainText(f"已完成 {len(paths)} 个文件转写，结果已保存到输出目录。")
            return
        path = paths[0]
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                self.transcribe_result_text.setPlainText(f.read().strip())
        except Exception as e:
            self.transcribe_result_text.setPlainText(f"读取转写结果失败: {e}")

    def copy_transcribe_result(self):
        if not hasattr(self, 'transcribe_result_text'):
            return
        text = self.transcribe_result_text.toPlainText().strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.update_log("[音视频处理] 转写文本已复制到剪贴板。")

    def build_av_short_video_page(self):
        page = QFrame()
        page.setObjectName("card")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        title = QLabel("抖音快手视频解析")
        title.setObjectName("appTitle")
        desc = QLabel("粘贴抖音或快手分享链接，先解析视频流，再自动识别视频里的口播文案；也可一键下载音频或未加平台水印的原视频。")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(desc)

        self.short_video_input = QPlainTextEdit()
        self.short_video_input.setPlaceholderText("粘贴手机分享出来的完整文本，例如：复制下方链接，打开抖音/快手观看...")
        self.short_video_input.setMaximumHeight(92)
        layout.addWidget(self.short_video_input)

        cookie_group = QGroupBox("Cookie 设置")
        cookie_layout = QGridLayout(cookie_group)
        self.short_video_cookie_mode = QComboBox()
        self.short_video_cookie_mode.addItem("不使用 Cookie", "none")
        self.short_video_cookie_mode.addItem("读取 Chrome Cookie", "chrome")
        self.short_video_cookie_mode.addItem("读取 Edge Cookie", "edge")
        self.short_video_cookie_mode.addItem("读取 Firefox Cookie", "firefox")
        self.short_video_cookie_mode.addItem("选择 cookies.txt", "file")
        self.short_video_cookie_mode.setCurrentIndex(1)
        self.short_video_cookie_file = QLineEdit()
        self.short_video_cookie_file.setPlaceholderText("选择 cookies.txt 时填写；浏览器 Cookie 模式可留空")
        cookie_file_btn = QPushButton("选择")
        cookie_file_btn.clicked.connect(self.choose_short_video_cookie_file)
        cookie_layout.addWidget(QLabel("Cookie 来源:"), 0, 0)
        cookie_layout.addWidget(self.short_video_cookie_mode, 0, 1)
        cookie_layout.addWidget(self.short_video_cookie_file, 1, 0, 1, 2)
        cookie_layout.addWidget(cookie_file_btn, 1, 2)
        cookie_tip = QLabel("抖音提示 Fresh cookies 时，先用对应浏览器打开一次抖音/快手再解析；如果提示 Cookie 数据库被占用，请完全关闭对应浏览器后重试，或改用 cookies.txt。Cookie 只在本机读取，不上传。")
        cookie_tip.setObjectName("appSubtitle")
        cookie_tip.setWordWrap(True)
        cookie_layout.addWidget(cookie_tip, 2, 0, 1, 3)
        layout.addWidget(cookie_group)

        action_row = QHBoxLayout()
        parse_btn = QPushButton("解析链接")
        parse_btn.setObjectName("startButton")
        parse_btn.clicked.connect(self.start_short_video_parse)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.clear_short_video_page)
        action_row.addWidget(parse_btn)
        action_row.addWidget(clear_btn)
        action_row.addStretch()
        layout.addLayout(action_row)

        info_row = QHBoxLayout()
        self.short_video_status = QLabel("状态：等待解析")
        self.short_video_status.setObjectName("appSubtitle")
        self.short_video_meta = QLabel("")
        self.short_video_meta.setObjectName("appSubtitle")
        self.short_video_meta.setWordWrap(True)
        info_row.addWidget(self.short_video_status, 1)
        info_row.addWidget(self.short_video_meta, 3)
        layout.addLayout(info_row)

        result_group = QGroupBox("口播文案")
        result_layout = QVBoxLayout(result_group)
        copy_row = QHBoxLayout()
        copy_hint = QLabel("这里显示的是视频声音识别出来的文案，不是平台标题。")
        copy_hint.setObjectName("appSubtitle")
        copy_row.addWidget(copy_hint)
        copy_row.addStretch()
        copy_btn = QPushButton("一键复制文案")
        copy_btn.clicked.connect(self.copy_short_video_caption)
        copy_row.addWidget(copy_btn)
        self.short_video_caption = QTextEdit()
        self.short_video_caption.setPlaceholderText("解析成功后会自动识别视频口播内容，并显示在这里。")
        self.short_video_caption.setMinimumHeight(170)
        result_layout.addLayout(copy_row)
        result_layout.addWidget(self.short_video_caption)
        layout.addWidget(result_group)

        download_row = QHBoxLayout()
        self.short_video_audio_btn = QPushButton("一键下载音频")
        self.short_video_audio_btn.setEnabled(False)
        self.short_video_audio_btn.clicked.connect(lambda: self.start_short_video_download("audio"))
        self.short_video_video_btn = QPushButton("一键下载无水印原视频")
        self.short_video_video_btn.setEnabled(False)
        self.short_video_video_btn.clicked.connect(lambda: self.start_short_video_download("video"))
        open_btn = QPushButton("打开输出目录")
        open_btn.clicked.connect(self.open_av_output_dir)
        download_row.addWidget(self.short_video_audio_btn)
        download_row.addWidget(self.short_video_video_btn)
        download_row.addStretch()
        download_row.addWidget(open_btn)
        layout.addLayout(download_row)

        tip = QLabel("提示：请确认你拥有视频内容的合法使用权，并遵守平台规则；下载功能会优先使用平台返回的原始视频流。")
        tip.setObjectName("appSubtitle")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        layout.addStretch()

        self.short_video_info = None
        self.short_video_transcript_path = ""
        return page

    def choose_short_video_cookie_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", self._file_dialog_default_dir(), "Cookie Files (*.txt);;All Files (*.*)")
        if path:
            self.short_video_cookie_file.setText(path)
            if hasattr(self, 'short_video_cookie_mode'):
                index = self.short_video_cookie_mode.findData("file")
                if index >= 0:
                    self.short_video_cookie_mode.setCurrentIndex(index)

    def current_short_video_cookie_options(self):
        mode = "none"
        if hasattr(self, 'short_video_cookie_mode'):
            mode = self.short_video_cookie_mode.currentData() or "none"
        cookie_file = self.short_video_cookie_file.text().strip() if hasattr(self, 'short_video_cookie_file') else ""
        return {"mode": mode, "cookie_file": cookie_file}

    def clear_short_video_page(self):
        if hasattr(self, 'short_video_input'):
            self.short_video_input.clear()
        if hasattr(self, 'short_video_caption'):
            self.short_video_caption.clear()
        if hasattr(self, 'short_video_status'):
            self.short_video_status.setText("状态：等待解析")
        if hasattr(self, 'short_video_meta'):
            self.short_video_meta.clear()
        self.short_video_info = None
        self.short_video_transcript_path = ""
        for btn_name in ('short_video_audio_btn', 'short_video_video_btn'):
            if hasattr(self, btn_name):
                getattr(self, btn_name).setEnabled(False)

    def start_short_video_parse(self):
        raw_text = self.short_video_input.toPlainText().strip() if hasattr(self, 'short_video_input') else ""
        if not raw_text:
            QMessageBox.warning(self, "缺少链接", "请先粘贴抖音或快手分享链接。")
            return
        if hasattr(self, 'short_video_parse_worker') and self.short_video_parse_worker.isRunning():
            QMessageBox.information(self, "解析中", "当前链接正在解析，请稍等。")
            return
        self.short_video_status.setText("状态：正在解析")
        self.short_video_caption.setPlainText("")
        self.short_video_audio_btn.setEnabled(False)
        self.short_video_video_btn.setEnabled(False)
        cookie_options = self.current_short_video_cookie_options()
        mode_text = self.short_video_cookie_mode.currentText() if hasattr(self, 'short_video_cookie_mode') else "不使用 Cookie"
        self.update_log(f"[短视频解析] Cookie 来源：{mode_text}")
        self.short_video_parse_worker = ShortVideoParseWorker(raw_text, cookie_options)
        self.short_video_parse_worker.message.connect(lambda text: self.update_log(f"[短视频解析] {text}"))
        self.short_video_parse_worker.finished.connect(self.on_short_video_parsed)
        self.short_video_parse_worker.failed.connect(self.on_short_video_parse_failed)
        self.short_video_parse_worker.start()
        self.update_log("[短视频解析] 任务已进入后台处理。")

    def on_short_video_parsed(self, info):
        self.short_video_info = info
        self.short_video_transcript_path = ""
        title = info.get("title") or info.get("description") or ""
        share_caption = info.get("share_caption") or info.get("caption") or ""
        meta = " | ".join([item for item in [
            info.get("platform", ""),
            info.get("uploader", ""),
            f"作品标题/描述：{title}" if title else "",
            info.get("format_summary", ""),
            info.get("parsed_at", ""),
        ] if item])
        self.short_video_status.setText("状态：解析成功")
        self.short_video_meta.setText(meta)
        partial = bool(info.get("partial"))
        self.short_video_audio_btn.setEnabled(not partial)
        self.short_video_video_btn.setEnabled(not partial)
        if partial:
            self.short_video_status.setText("状态：仅提取到分享文本")
            self.short_video_caption.setPlainText(share_caption)
            self.update_log(f"[短视频解析] 仅提取到分享文本，无法识别口播文案：{info.get('parse_error', '')}")
        else:
            self.update_log(f"[短视频解析] 解析成功：{info.get('title', '')}")
            self.start_short_video_transcript()

    def start_short_video_transcript(self):
        if not self.short_video_info or self.short_video_info.get("partial"):
            return
        if hasattr(self, 'short_video_transcript_worker') and self.short_video_transcript_worker.isRunning():
            self.update_log("[短视频解析] 口播文案识别正在进行中。")
            return
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        sensevoice_dir = os.path.abspath(os.path.join(models_dir(), 'SenseVoiceSmall'))
        if not os.path.exists(ffmpeg):
            self.short_video_status.setText("状态：等待识别组件")
            self.short_video_caption.setPlainText("未找到 ffmpeg.exe，无法从视频里提取音频识别口播文案。")
            self.update_log("[短视频解析] 未找到 ffmpeg.exe，跳过口播文案识别。")
            return
        self.short_video_status.setText("状态：正在识别口播文案")
        self.short_video_caption.setPlainText("正在识别视频里的口播文案，请稍等...")
        self.task_progress.setValue(5)
        temp_dir = os.path.abspath(os.path.join(self.cache_dir, '短视频口播识别'))
        self.short_video_transcript_worker = ShortVideoTranscriptWorker(
            self.short_video_info,
            temp_dir,
            os.path.abspath(self.av_output_dir),
            ffmpeg,
            sensevoice_dir,
            self.current_short_video_cookie_options(),
        )
        self.short_video_transcript_worker.message.connect(lambda text: self.update_log(f"[短视频解析] {text}"))
        self.short_video_transcript_worker.progress.connect(self.task_progress.setValue)
        self.short_video_transcript_worker.finished.connect(self.on_short_video_transcribed)
        self.short_video_transcript_worker.failed.connect(self.on_short_video_transcribe_failed)
        self.short_video_transcript_worker.start()

    def on_short_video_transcribed(self, result):
        text = result.get("text", "")
        self.short_video_transcript_path = result.get("txt_path", "")
        self.short_video_caption.setPlainText(text)
        self.short_video_status.setText("状态：口播文案识别完成")
        self.task_progress.setValue(100)
        self.update_log(f"[短视频解析] 口播文案识别完成：{self.short_video_transcript_path}")

    def on_short_video_transcribe_failed(self, message):
        self.task_progress.setValue(0)
        fallback = ""
        if self.short_video_info:
            fallback = self.short_video_info.get("share_caption") or ""
        if fallback:
            self.short_video_caption.setPlainText(fallback)
        else:
            self.short_video_caption.setPlainText("口播文案识别失败。请确认 SenseVoiceSmall 模型已放入网盘资源包/models/SenseVoiceSmall，并确认该视频有清晰人声。")
        self.short_video_status.setText("状态：口播文案识别失败")
        self.update_log(f"[短视频解析] 口播文案识别失败：{message}")

    def on_short_video_parse_failed(self, message):
        self.short_video_info = None
        self.short_video_status.setText("状态：解析失败")
        self.short_video_audio_btn.setEnabled(False)
        self.short_video_video_btn.setEnabled(False)
        self.update_log(f"[短视频解析] 解析失败：{message}")
        QMessageBox.warning(self, "解析失败", message)

    def copy_short_video_caption(self):
        text = self.short_video_caption.toPlainText().strip() if hasattr(self, 'short_video_caption') else ""
        if not text:
            QMessageBox.information(self, "暂无文案", "当前没有可复制的视频文案。")
            return
        QApplication.clipboard().setText(text)
        self.update_log("[短视频解析] 口播文案已复制到剪贴板。")

    def start_short_video_download(self, kind):
        if not self.short_video_info:
            QMessageBox.warning(self, "未解析", "请先解析视频链接。")
            return
        if hasattr(self, 'short_video_download_worker') and self.short_video_download_worker.isRunning():
            QMessageBox.information(self, "下载中", "当前下载任务正在进行，请稍等。")
            return
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        if not os.path.exists(ffmpeg):
            QMessageBox.warning(self, "缺少组件", "未找到 网盘资源包/tools/ffmpeg.exe，无法合并视频或提取音频。")
            return
        self.task_progress.setValue(10)
        self.short_video_status.setText("状态：正在下载音频" if kind == "audio" else "状态：正在下载原视频")
        self.short_video_download_worker = ShortVideoDownloadWorker(
            self.short_video_info,
            os.path.abspath(self.av_output_dir),
            ffmpeg,
            kind,
            self.current_short_video_cookie_options(),
        )
        self.short_video_download_worker.message.connect(lambda text: self.update_log(f"[短视频解析] {text}"))
        self.short_video_download_worker.finished.connect(lambda path, k=kind: self.on_short_video_downloaded(k, path))
        self.short_video_download_worker.failed.connect(self.on_short_video_download_failed)
        self.short_video_download_worker.start()

    def on_short_video_downloaded(self, kind, path):
        self.task_progress.setValue(100)
        self.short_video_status.setText("状态：下载完成")
        label = "音频" if kind == "audio" else "原视频"
        self.update_log(f"[短视频解析] {label}下载完成：{path}")
        QMessageBox.information(self, "下载完成", f"{label}已保存：\n{path}")

    def on_short_video_download_failed(self, message):
        self.task_progress.setValue(0)
        self.short_video_status.setText("状态：下载失败")
        self.update_log(f"[短视频解析] 下载失败：{message}")
        QMessageBox.warning(self, "下载失败", message)

    def build_av_extract_audio_page(self):
        fmt = QComboBox()
        fmt.addItems(["mp3", "wav"])
        return self.build_av_tool_page(
            "视频分离音频",
            "从视频文件中提取音频，输出到固定目录。",
            "Video Files (*.mp4 *.mov *.avi *.mkv *.webm)",
            ['.mp4', '.mov', '.avi', '.mkv', '.webm'],
            [("输出格式:", fmt)],
            lambda line: self._start_av_task({
                'kind': 'extract_audio',
                'input_text': line.text().strip(),
                'format': fmt.currentText(),
                'suffix': '提取音频',
                'output_ext': fmt.currentText()
            }),
            kind='extract_audio'
        )

    def build_av_convert_page(self):
        fmt = QComboBox()
        fmt.addItems(["mp4", "mov", "avi", "mp3", "wav", "m4a"])
        standard_sdr_chk = QCheckBox("iPhone HDR/MOV 转标准 MP4（防发白）")
        standard_sdr_chk.setToolTip("检测到 HDR/BT.2020 时自动转 SDR；普通素材输出为标准 BT.709 MP4。原文件不覆盖。")
        standard_sdr_hint = QLabel("用于 iPhone HDR、杜比视界、BT.2020 素材。会重新编码为 H.264 / yuv420p / BT.709，适合抖快平台。")
        standard_sdr_hint.setObjectName("appSubtitle")
        standard_sdr_hint.setWordWrap(True)

        def apply_standard_mode(enabled):
            if enabled:
                fmt.setCurrentText("mp4")
            fmt.setEnabled(not enabled)

        standard_sdr_chk.toggled.connect(apply_standard_mode)

        def start_convert(line):
            standard_mode = standard_sdr_chk.isChecked()
            self._start_av_task({
                'kind': 'convert',
                'input_text': line.text().strip(),
                'format': 'mp4' if standard_mode else fmt.currentText(),
                'standard_sdr_mp4': standard_mode,
                'suffix': '标准MP4' if standard_mode else '格式转换',
                'output_ext': 'mp4' if standard_mode else fmt.currentText()
            })

        return self.build_av_tool_page(
            "音视频转换格式",
            "转换常见音视频格式。视频格式会重新编码，音频格式会自动只保留音频流。",
            "Media Files (*.mp4 *.mov *.avi *.mkv *.webm *.mp3 *.wav *.m4a *.aac *.flac)",
            ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.aac', '.flac'],
            [("目标格式:", fmt), ("色彩修复:", standard_sdr_chk), ("说明:", standard_sdr_hint)],
            start_convert,
            kind='convert'
        )

    def build_av_add_subtitle_page(self):
        page = QFrame()
        page.setObjectName("card")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        title = QLabel("视频加字幕")
        title.setObjectName("appTitle")
        desc = QLabel("直接给完整视频添加识别字幕：自动识别语音并生成时间轴字幕，保留原画面与原音轨。")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(desc)

        # 左右分栏：左侧参数控件，右侧整栏大预览
        body = QHBoxLayout()
        body.setSpacing(18)
        left = QVBoxLayout()
        left.setSpacing(12)

        self.av_sub_video_line = QLineEdit()
        self.av_sub_video_line.setPlaceholderText("选择或拖入一个/多个视频 — 切换子功能时将自动匹配上游输出")
        self.av_input_lines['add_subtitle'] = self.av_sub_video_line
        QTimer.singleShot(100, lambda: self._try_av_chain_fill('add_subtitle'))
        video_drop = FileDropArea("拖拽完整视频到这里，或点击选择一个/多个视频")
        video_drop.filesDropped.connect(lambda files: self.av_sub_video_line.setText(";".join(files)))
        video_drop.clicked.connect(lambda: self.choose_av_inputs(self.av_sub_video_line, "Video Files (*.mp4 *.mov *.avi *.mkv *.webm)"))
        left.addWidget(video_drop)
        video_row = QHBoxLayout()
        video_row.addWidget(self.av_sub_video_line, stretch=1)
        clear_video_btn = QPushButton("清空视频")
        clear_video_btn.clicked.connect(self.av_sub_video_line.clear)
        video_row.addWidget(clear_video_btn)
        left.addLayout(video_row)

        # 字幕样式面板（花字预设 + 样式表单；预览放右侧大窗，与批量混剪共用一套预设体系）
        from subtitle_style_panel import SubtitleStylePanel, SubtitlePreviewWidget
        self.av_sub_style_panel = SubtitleStylePanel(
            system_fonts=dict(getattr(self, 'sys_fonts', {}) or {}),
            default_size=72,
            show_preview=False,
        )
        left.addWidget(self.av_sub_style_panel)

        hint = QLabel("识别引擎：优先 Whisper 生成精准时间轴（字幕与语音停顿对齐），缺失时回退 SenseVoice 快速模式（时间轴为估算）。输出到音视频处理输出目录，不覆盖原视频。")
        hint.setObjectName("warningText")
        hint.setWordWrap(True)
        left.addWidget(hint)

        # 仅导出字幕文件模式
        self.av_sub_export_chk = QCheckBox("仅导出字幕文件（不烧录到视频，生成 .srt / .ass）")
        self.av_sub_export_chk.setChecked(False)
        left.addWidget(self.av_sub_export_chk)

        left.addLayout(self.build_av_output_row())

        run_btn = QPushButton("开始添加字幕")
        run_btn.setObjectName("startButton")
        run_btn.clicked.connect(self.start_av_add_subtitle)
        left.addWidget(run_btn)
        left.addStretch()
        body.addLayout(left, stretch=5)

        # 右：整栏大预览（与面板样式实时同步）
        self.av_sub_preview_large = SubtitlePreviewWidget(width=400)
        self.av_sub_style_panel.styleChanged.connect(
            lambda: self.av_sub_preview_large.set_style(self.av_sub_style_panel.get_style())
        )
        self.av_sub_preview_large.set_style(self.av_sub_style_panel.get_style())
        body.addLayout(self._av_sub_preview_right_col(), stretch=4)
        layout.addLayout(body, stretch=1)
        return page

    def _av_sub_preview_right_col(self):
        """右侧大预览列（独立容器，预览随窗口内容自适应）。"""
        col = QVBoxLayout()
        col.addWidget(self.av_sub_preview_large)
        col.addStretch()
        return col

    def _apply_av_sub_style_preset(self, preset, clicked_btn=None):
        """兼容入口：预设应用已由 SubtitleStylePanel 内部处理。"""
        if hasattr(self, "av_sub_style_panel"):
            self.av_sub_style_panel.apply_preset(preset, clicked_btn)

    def _apply_av_random_sub_style(self):
        """兼容入口：随机已由 SubtitleStylePanel 内部处理。"""
        if hasattr(self, "av_sub_style_panel"):
            self.av_sub_style_panel.apply_random_style()

    def _on_av_sub_video_changed(self, text):
        """视频输入变化：后台抽中间帧 + 探测分辨率，更新预览。"""
        first = next(
            (p for p in self._split_input_paths(text.strip()) if os.path.exists(p)),
            None,
        )
        if not first:
            return

        from PyQt5.QtGui import QPixmap

        class _FrameThread(QThread):
            frameReady = pyqtSignal(QPixmap, int, int)

            def __init__(self, video, ffmpeg, ffprobe, parent=None):
                super().__init__(parent)
                self._video = video
                self._ffmpeg = ffmpeg
                self._ffprobe = ffprobe

            def run(self):
                try:
                    tmp = os.path.join(
                        os.environ.get("TEMP", "."), f"avsub_frame_{os.getpid()}.jpg"
                    )
                    dur_res = subprocess.run(
                        [self._ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                         '-of', 'csv=p=0', self._video],
                        capture_output=True, text=True, timeout=10,
                    )
                    try:
                        duration = float((dur_res.stdout or "").strip())
                    except ValueError:
                        duration = 0.0
                    seek = max(0.0, duration * 0.3)
                    subprocess.run(
                        [self._ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                         '-ss', f"{seek:.2f}", '-i', self._video,
                         '-frames:v', '1', '-q:v', '3', tmp],
                        capture_output=True, timeout=20,
                    )
                    pixmap = QPixmap(tmp) if os.path.exists(tmp) else QPixmap()
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

                    size_res = subprocess.run(
                        [self._ffprobe, '-v', 'error', '-select_streams', 'v:0',
                         '-show_entries', 'stream=width,height',
                         '-of', 'csv=p=0:s=x', self._video],
                        capture_output=True, text=True, timeout=10,
                    )
                    width, height = 1080, 1920
                    try:
                        parts = (size_res.stdout or "").strip().split('x')
                        width, height = int(parts[0]), int(parts[1])
                    except (ValueError, IndexError):
                        pass
                    self.frameReady.emit(pixmap, width, height)
                except Exception:
                    self.frameReady.emit(QPixmap(), 1080, 1920)

        ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        ffprobe = os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe'))
        self._av_sub_frame_thread = _FrameThread(first, ffmpeg, ffprobe)

        def _on_ready(pixmap, width, height):
            if hasattr(self, "av_sub_style_panel"):
                self.av_sub_style_panel.set_video_frame(pixmap)
                self.av_sub_style_panel.set_play_res(width, height)
            if hasattr(self, "av_sub_preview_large"):
                self.av_sub_preview_large.set_video_frame(pixmap)
                self.av_sub_preview_large.set_play_res(width, height)
            # 大预览同步面板当前样式（分辨率变了，比例跟随）
            if hasattr(self, "av_sub_style_panel") and hasattr(self, "av_sub_preview_large"):
                self.av_sub_preview_large.set_style(self.av_sub_style_panel.get_style())

        self._av_sub_frame_thread.frameReady.connect(_on_ready)
        self._av_sub_frame_thread.start()

    def start_av_add_subtitle(self):
        inputs = [p for p in self._split_input_paths(self.av_sub_video_line.text().strip()) if os.path.exists(p)]
        if not inputs:
            QMessageBox.warning(self, "缺少视频", "请先选择有效的视频文件。")
            return
        sub_style = (
            self.av_sub_style_panel.get_style()
            if hasattr(self, "av_sub_style_panel")
            else {}
        )
        self._start_av_task({
            'kind': 'add_subtitle',
            'input_text': self.av_sub_video_line.text().strip(),
            'export_only': self.av_sub_export_chk.isChecked(),
            'sub_style': sub_style,
            'suffix': '带字幕',
            'output_ext': 'mp4'
        })

    def build_av_add_cover_page(self):
        page = QFrame()
        page.setObjectName("card")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        title = QLabel("批量加封面")
        title.setObjectName("appTitle")
        desc = QLabel("给每个视频开头追加封面图前 2 帧，适合给外部成品视频统一加首帧封面。")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(desc)

        self.cover_video_line = QLineEdit()
        self.cover_video_line.setPlaceholderText("选择或拖入一个/多个视频 — 切换子功能时将自动匹配上游输出")
        self.av_input_lines['add_cover'] = self.cover_video_line
        QTimer.singleShot(100, lambda: self._try_av_chain_fill('add_cover'))
        video_drop = FileDropArea("拖拽视频文件到这里，或点击选择一个/多个视频")
        video_drop.filesDropped.connect(lambda files: self.cover_video_line.setText(";".join(files)))
        video_drop.clicked.connect(lambda: self.choose_av_inputs(self.cover_video_line, "Video Files (*.mp4 *.mov *.avi *.mkv *.webm)"))
        layout.addWidget(video_drop)
        video_row = QHBoxLayout()
        video_row.addWidget(self.cover_video_line, stretch=1)
        clear_video_btn = QPushButton("清空视频")
        clear_video_btn.clicked.connect(self.cover_video_line.clear)
        video_row.addWidget(clear_video_btn)
        layout.addLayout(video_row)

        self.cover_image_line = QLineEdit()
        self.cover_image_line.setPlaceholderText("选择或拖入封面图片；多张图片会按视频顺序循环使用")
        image_drop = FileDropArea("拖拽封面图片到这里，或点击选择一张/多张图片")
        image_drop.filesDropped.connect(lambda files: self.cover_image_line.setText(";".join(files)))
        image_drop.clicked.connect(lambda: self.choose_av_inputs(self.cover_image_line, "Image Files (*.jpg *.jpeg *.png *.webp *.bmp)"))
        layout.addWidget(image_drop)
        image_row = QHBoxLayout()
        image_row.addWidget(self.cover_image_line, stretch=1)
        clear_image_btn = QPushButton("清空封面")
        clear_image_btn.clicked.connect(self.cover_image_line.clear)
        image_row.addWidget(clear_image_btn)
        layout.addLayout(image_row)

        hint = QLabel("封面时长固定为前 2 帧；输出到音视频处理输出目录，不覆盖原视频。")
        hint.setObjectName("warningText")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addLayout(self.build_av_output_row())

        run_btn = QPushButton("开始批量加封面")
        run_btn.setObjectName("startButton")
        run_btn.clicked.connect(self.start_av_add_cover)
        layout.addWidget(run_btn)
        layout.addStretch()
        return page

    def start_av_add_cover(self):
        cover_paths = [p for p in self._split_input_paths(self.cover_image_line.text().strip()) if os.path.exists(p)]
        if not cover_paths:
            QMessageBox.warning(self, "缺少封面", "请先选择有效的封面图片。")
            return
        self._start_av_task({
            'kind': 'add_cover',
            'input_text': self.cover_video_line.text().strip(),
            'cover_paths': cover_paths,
            'cover_seconds': 2 / 30,
            'suffix': '加封面',
            'output_ext': 'mp4'
        })

    def probe_video_size_for_ui(self, input_path):
        ffprobe = os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe'))
        if not os.path.exists(ffprobe) or not os.path.exists(input_path):
            return 1080, 1920
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0:s=x', input_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self.get_si()
            )
            text = (res.stdout or "").strip().splitlines()[0]
            width, height = [int(x) for x in text.split('x')[:2]]
            return max(2, width), max(2, height)
        except Exception:
            return 1080, 1920

    def choose_av_watermark_videos(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择要加文字水印的视频", self._file_dialog_default_dir(), "Video Files (*.mp4 *.mov *.avi *.mkv *.webm)")
        if files:
            self._record_file_dialog_dirs(files)
            self.set_av_watermark_videos(files)

    def set_av_watermark_videos(self, files):
        paths = [p for p in files if p and os.path.exists(p)]
        if not paths:
            return
        self.av_wm_video_line.setText(";".join(paths))
        self.load_av_watermark_reference(paths[0])

    def load_av_watermark_reference(self, video_path):
        width, height = self.probe_video_size_for_ui(video_path)
        self.av_wm_design_w = width
        self.av_wm_design_h = height
        self.av_wm_x.setRange(0, width)
        self.av_wm_y.setRange(0, height)
        self.av_wm_x.setValue(width // 2)
        self.av_wm_y.setValue(height // 2)
        try:
            bg_path = os.path.join(os.path.abspath(self.cache_dir), "preview_bg_av_watermark.jpg")
            ffmpeg_exe = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
            extracted = extract_reference_frame(ffmpeg_exe, video_path, bg_path)
            self.av_wm_preview_bg = extracted if extracted and os.path.exists(extracted) else ""
        except Exception as exc:
            self.av_wm_preview_bg = ""
            self.update_log(f"[音视频处理] 水印参考帧提取失败: {exc}")
        self.draw_av_watermark_preview()

    def set_av_wm_position_from_preview(self, x, y):
        self.av_wm_x.blockSignals(True)
        self.av_wm_y.blockSignals(True)
        self.av_wm_x.setValue(x)
        self.av_wm_y.setValue(y)
        self.av_wm_x.blockSignals(False)
        self.av_wm_y.blockSignals(False)
        self.draw_av_watermark_preview()

    def current_av_watermark_data(self):
        font_name = self.av_wm_font.currentText()
        return {
            'text': self.av_wm_text.toPlainText(),
            'font_name': font_name,
            'font_path': self.sys_fonts.get(font_name, ""),
            'size': self._spinbox_live_value(self.av_wm_size),
            'color': self.av_wm_color.currentText(),
            'align': self.av_wm_align.currentText(),
            'line_spacing': self._spinbox_live_value(self.av_wm_line_spacing) if hasattr(self, 'av_wm_line_spacing') else 130,
            'letter_spacing': self._spinbox_live_value(self.av_wm_letter_spacing) if hasattr(self, 'av_wm_letter_spacing') else 0,
            'border_w': self._spinbox_live_value(self.av_wm_border_w),
            'border_color': self.av_wm_border_color.currentText(),
            'is_bold': self.av_wm_bold.isChecked(),
            'x': self._spinbox_live_value(self.av_wm_x),
            'y': self._spinbox_live_value(self.av_wm_y),
            'design_w': getattr(self, 'av_wm_design_w', 1080),
            'design_h': getattr(self, 'av_wm_design_h', 1920),
        }

    def set_av_watermark_mode(self, mode, row=-1):
        if not hasattr(self, 'av_wm_mode_label'):
            return
        if mode == "edit":
            self.av_wm_mode_label.setText(f'编辑模式：正在修改第 {row + 1} 条水印，点[清空面板/退出编辑]可回到新增')
            self.av_wm_mode_label.setStyleSheet("color:#FDBA74; background:#2A1F0B; border:1px solid #B45309; border-radius:8px; padding:7px; font-weight:bold;")
            self.av_wm_modify_btn.setEnabled(True)
            self.av_wm_add_btn.setText("另存为新水印")
        else:
            self.av_wm_mode_label.setText("新增模式：当前面板内容会作为一条新水印添加到列表")
            self.av_wm_mode_label.setStyleSheet("color:#BBF7D0; background:#052E16; border:1px solid #22C55E; border-radius:8px; padding:7px; font-weight:bold;")
            self.av_wm_modify_btn.setEnabled(False)
            self.av_wm_add_btn.setText("添加为新水印")

    def selected_av_watermark_row(self):
        if not hasattr(self, 'av_wm_table') or not self.av_wm_table.selectionModel():
            return -1
        selected = self.av_wm_table.selectionModel().selectedRows()
        if not selected:
            return -1
        row = selected[0].row()
        return row if 0 <= row < len(self.av_watermarks_data) else -1

    def reset_av_watermark_form(self):
        if hasattr(self, 'av_wm_table'):
            self.av_wm_table.blockSignals(True)
            self.av_wm_table.clearSelection()
            self.av_wm_table.blockSignals(False)
        self.av_editing_wm_index = -1
        self.set_av_watermark_mode("new")
        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_line_spacing, self.av_wm_letter_spacing, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            widget.blockSignals(True)
        self.av_wm_text.clear()
        self.av_wm_font.setCurrentText("🎲 随机系统字体")
        self.av_wm_size.setValue(60)
        self.av_wm_color.setCurrentText("white")
        self.av_wm_align.setCurrentText("居中对齐")
        self.av_wm_line_spacing.setValue(130)
        self.av_wm_letter_spacing.setValue(0)
        self.av_wm_bold.setChecked(False)
        self.av_wm_border_w.setValue(2)
        self.av_wm_border_color.setCurrentText("black")
        self.av_wm_x.setValue(int(getattr(self, 'av_wm_design_w', 1080)) // 2)
        self.av_wm_y.setValue(int(getattr(self, 'av_wm_design_h', 1920)) // 2)
        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_line_spacing, self.av_wm_letter_spacing, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            widget.blockSignals(False)
        self.draw_av_watermark_preview()

    def add_av_watermark_new(self):
        data = self.current_av_watermark_data()
        if not data['text'].strip():
            QMessageBox.warning(self, "缺少文字", "请先输入文字水印内容。")
            return
        self.av_watermarks_data.append(data)
        self.add_av_wm_to_table(data)
        self.reset_av_watermark_form()

    def modify_selected_av_watermark(self):
        row = self.selected_av_watermark_row()
        if row < 0:
            return
        data = self.current_av_watermark_data()
        if not data['text'].strip():
            QMessageBox.warning(self, "缺少文字", "请先输入文字水印内容。")
            return
        self.av_watermarks_data[row] = data
        self.update_av_wm_table_row(row, data)
        self.reset_av_watermark_form()

    def delete_selected_av_watermark(self):
        row = self.selected_av_watermark_row()
        if row < 0:
            return
        del self.av_watermarks_data[row]
        self.av_wm_table.removeRow(row)
        self.reset_av_watermark_form()

    def add_av_wm_to_table(self, data):
        row = self.av_wm_table.rowCount()
        self.av_wm_table.insertRow(row)
        self.update_av_wm_table_row(row, data)

    def update_av_wm_table_row(self, row, data):
        self.av_wm_table.setItem(row, 0, QTableWidgetItem(str(data.get('text', '')).replace('\n', '  ')[:80]))
        self.av_wm_table.setItem(row, 1, QTableWidgetItem(str(data.get('font_name', ''))))
        self.av_wm_table.setItem(row, 2, QTableWidgetItem(f"大小:{data.get('size', 60)} 行距:{data.get('line_spacing', 130)}% 字距:{data.get('letter_spacing', 0)}"))
        self.av_wm_table.setItem(row, 3, QTableWidgetItem(f"{data.get('x', 0)}, {data.get('y', 0)}"))

    def on_av_wm_table_selection_changed(self):
        row = self.selected_av_watermark_row()
        if row < 0:
            self.av_editing_wm_index = -1
            self.set_av_watermark_mode("new")
            self.draw_av_watermark_preview()
            return
        self.av_editing_wm_index = row
        self.set_av_watermark_mode("edit", row)
        wm = self.av_watermarks_data[row]
        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_line_spacing, self.av_wm_letter_spacing, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            widget.blockSignals(True)
        self.av_wm_text.setPlainText(wm.get('text', ''))
        self.av_wm_font.setCurrentText(wm.get('font_name', '🎲 随机系统字体'))
        self.av_wm_size.setValue(int(wm.get('size', 60)))
        self.av_wm_color.setCurrentText(wm.get('color', 'white'))
        self.av_wm_align.setCurrentText(wm.get('align', '居中对齐'))
        self.av_wm_line_spacing.setValue(int(wm.get('line_spacing', 130)))
        self.av_wm_letter_spacing.setValue(int(wm.get('letter_spacing', 0)))
        self.av_wm_bold.setChecked(bool(wm.get('is_bold', False)))
        self.av_wm_border_w.setValue(int(wm.get('border_w', 2)))
        self.av_wm_border_color.setCurrentText(wm.get('border_color', 'black'))
        self.av_wm_x.setValue(int(wm.get('x', 540)))
        self.av_wm_y.setValue(int(wm.get('y', 960)))
        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_line_spacing, self.av_wm_letter_spacing, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            widget.blockSignals(False)
        self.draw_av_watermark_preview()

    def load_mix_watermarks_to_av(self):
        if not self.watermarks_data:
            QMessageBox.information(self, "暂无水印", "批量混剪里的文字水印列表为空。")
            return
        self.av_watermarks_data = []
        self.av_wm_table.setRowCount(0)
        for wm in self.watermarks_data:
            data = dict(wm)
            data['design_w'] = getattr(self, 'av_wm_design_w', 1080)
            data['design_h'] = getattr(self, 'av_wm_design_h', 1920)
            self.av_watermarks_data.append(data)
            self.add_av_wm_to_table(data)
        self.reset_av_watermark_form()
        self.update_log("[音视频处理] 已复制批量混剪文字水印列表到当前页面。")

    def draw_av_text_lines(self, wm, scale_ratio):
        color = "#FF1493" if wm.get('color') == "🎲 随机颜色" else wm.get('color', 'white')
        border_color = "#00FFFF" if wm.get('border_color') == "🎲 随机颜色" else wm.get('border_color', 'black')
        lines = str(self.watermark_display_text(wm)).splitlines()
        font_size = max(8, int(float(wm.get('size', 60)) * scale_ratio * WATERMARK_PREVIEW_FONT_SCALE))
        border_w = max(0, int(float(wm.get('border_w', 0)) * scale_ratio))
        if wm.get('is_bold') and border_w <= 0:
            border_w = 1
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            temp_lbl = QLabel(line)
            font = temp_lbl.font()
            family = self.preview_font_family(wm.get('font_name', ''))
            if family:
                font.setFamily(family)
            font.setPixelSize(font_size)
            font.setBold(wm.get('is_bold', False))
            self.apply_preview_letter_spacing(font, wm, scale_ratio)
            temp_lbl.setFont(font)
            temp_lbl.adjustSize()
            base_x = float(wm.get('x', 0)) * scale_ratio
            line_y = (float(wm.get('y', 0)) + i * self.watermark_line_step(wm)) * scale_ratio
            align = wm.get('align', '居中对齐')
            if align == "居中对齐":
                final_x = base_x - temp_lbl.width() / 2
            elif align == "右对齐":
                final_x = base_x - temp_lbl.width()
            else:
                final_x = base_x
            temp_lbl.deleteLater()
            if border_w > 0:
                for dx, dy in [(-border_w, -border_w), (border_w, -border_w), (-border_w, border_w), (border_w, border_w), (0, -border_w), (0, border_w), (-border_w, 0), (border_w, 0)]:
                    border_lbl = QLabel(line, self.av_wm_preview_canvas)
                    border_lbl.setStyleSheet(f"color:{border_color}; background:transparent;")
                    border_lbl.setFont(font)
                    border_lbl.adjustSize()
                    border_lbl.move(int(final_x) + dx, int(line_y) + dy)
                    border_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
                    border_lbl.show()
            lbl = QLabel(line, self.av_wm_preview_canvas)
            lbl.setStyleSheet(f"color:{color}; background:transparent;")
            lbl.setFont(font)
            lbl.adjustSize()
            lbl.move(int(final_x), int(line_y))
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            lbl.show()

    def draw_av_watermark_preview(self):
        if not hasattr(self, 'av_wm_preview_canvas'):
            return
        for child in self.av_wm_preview_canvas.children():
            child.deleteLater()
        design_w = max(1, int(getattr(self, 'av_wm_design_w', 1080)))
        design_h = max(1, int(getattr(self, 'av_wm_design_h', 1920)))
        if design_h >= design_w:
            canvas_w = 360
            canvas_h = max(360, int(canvas_w * design_h / design_w))
            canvas_h = min(720, canvas_h)
        else:
            canvas_w = 640
            canvas_h = max(240, int(canvas_w * design_h / design_w))
            canvas_h = min(420, canvas_h)
        self.av_wm_preview_canvas.setFixedSize(canvas_w, canvas_h)
        self.av_wm_preview_canvas.set_design_size(design_w, design_h)
        if getattr(self, 'av_wm_preview_bg', '') and os.path.exists(self.av_wm_preview_bg):
            bg_lbl = QLabel(self.av_wm_preview_canvas)
            bg_lbl.setGeometry(0, 0, canvas_w, canvas_h)
            bg_lbl.setAlignment(Qt.AlignCenter)
            bg_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            bg_lbl.setPixmap(QPixmap(self.av_wm_preview_bg).scaled(canvas_w, canvas_h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            bg_lbl.show()
        scale_ratio = canvas_w / design_w
        for index, wm in enumerate(getattr(self, 'av_watermarks_data', [])):
            if index == getattr(self, 'av_editing_wm_index', -1):
                continue
            self.draw_av_text_lines(wm, scale_ratio)
        active = self.current_av_watermark_data()
        if active['text'].strip():
            self.draw_av_text_lines(active, scale_ratio)

    def build_av_text_watermark_page(self):
        page = QFrame()
        page.setObjectName("card")
        root = QHBoxLayout(page)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(14)

        left = QVBoxLayout()
        title = QLabel("视频加文字水印")
        title.setObjectName("appTitle")
        desc = QLabel("给外部做好的视频单独添加文字水印，支持批量视频、坐标预览、字体颜色和描边。")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        left.addWidget(title)
        left.addWidget(desc)
        self.av_watermarks_data = []
        self.av_editing_wm_index = -1

        self.av_wm_video_line = QLineEdit()
        self.av_wm_video_line.setPlaceholderText("选择或拖入一个/多个视频 — 切换子功能时将自动匹配上游输出")
        self.av_input_lines['text_watermark'] = self.av_wm_video_line
        QTimer.singleShot(100, lambda: self._try_av_chain_fill('text_watermark'))
        video_drop = FileDropArea("拖拽视频到这里，或点击选择一个/多个视频")
        video_drop.filesDropped.connect(self.set_av_watermark_videos)
        video_drop.clicked.connect(self.choose_av_watermark_videos)
        left.addWidget(video_drop)
        video_row = QHBoxLayout()
        video_row.addWidget(self.av_wm_video_line, stretch=1)
        clear_btn = QPushButton("清空视频")
        clear_btn.clicked.connect(self.av_wm_video_line.clear)
        video_row.addWidget(clear_btn)
        left.addLayout(video_row)

        form = QFormLayout()
        self.av_wm_text = PlainPasteTextEdit()
        self.av_wm_text.setPlaceholderText("输入要添加到视频里的文字水印，支持多行")
        self.av_wm_text.setMaximumHeight(88)
        self.av_wm_font = QComboBox()
        self.av_wm_font.addItem("🎲 随机系统字体")
        self.av_wm_font.addItems(list(self.sys_fonts.keys()))
        self.av_wm_size = QSpinBox()
        self.av_wm_size.setRange(20, 300)
        self.av_wm_size.setValue(60)
        self.av_wm_color = QComboBox()
        self.populate_color_combo(self.av_wm_color)
        self.av_wm_color.addItem("🎲 随机颜色")
        self.av_wm_color.setCurrentText("white")
        self.av_wm_align = QComboBox()
        self.av_wm_align.addItems(["左对齐", "居中对齐", "右对齐"])
        self.av_wm_align.setCurrentText("居中对齐")
        self.av_wm_line_spacing = QSpinBox()
        self.av_wm_line_spacing.setRange(60, 300)
        self.av_wm_line_spacing.setValue(130)
        self.av_wm_line_spacing.setSuffix("%")
        self.av_wm_letter_spacing = QSpinBox()
        self.av_wm_letter_spacing.setRange(0, 40)
        self.av_wm_letter_spacing.setValue(0)
        self.av_wm_letter_spacing.setSuffix("px")
        self.av_wm_bold = QCheckBox("强制加粗")
        self.av_wm_border_w = QSpinBox()
        self.av_wm_border_w.setRange(0, 10)
        self.av_wm_border_w.setValue(2)
        self.av_wm_border_color = QComboBox()
        self.populate_color_combo(self.av_wm_border_color)
        self.av_wm_border_color.addItem("🎲 随机颜色")
        self.av_wm_border_color.setCurrentText("black")
        self.av_wm_x = QSpinBox()
        self.av_wm_x.setRange(0, 3840)
        self.av_wm_x.setValue(540)
        self.av_wm_y = QSpinBox()
        self.av_wm_y.setRange(0, 3840)
        self.av_wm_y.setValue(960)

        self.av_wm_mode_label = QLabel("新增模式：当前面板内容会作为一条新水印添加到列表")
        self.av_wm_mode_label.setWordWrap(True)
        self.av_wm_mode_label.setStyleSheet("color:#BBF7D0; background:#052E16; border:1px solid #22C55E; border-radius:8px; padding:7px; font-weight:bold;")
        font_row = QHBoxLayout()
        font_row.addWidget(self.av_wm_font, 2)
        font_row.addWidget(self.av_wm_size, 1)
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("字体颜色:"))
        color_row.addWidget(self.av_wm_color)
        color_row.addWidget(QLabel("对齐:"))
        color_row.addWidget(self.av_wm_align)
        spacing_row = QHBoxLayout()
        spacing_row.addWidget(QLabel("换行间隔:"))
        spacing_row.addWidget(self.av_wm_line_spacing)
        spacing_row.addWidget(QLabel("字间距:"))
        spacing_row.addWidget(self.av_wm_letter_spacing)
        border_row = QHBoxLayout()
        border_row.addWidget(self.av_wm_bold)
        border_row.addWidget(QLabel("描边:"))
        border_row.addWidget(self.av_wm_border_w)
        border_row.addWidget(QLabel("描边颜色:"))
        border_row.addWidget(self.av_wm_border_color)
        coord_row = QHBoxLayout()
        coord_row.addWidget(QLabel("X:"))
        coord_row.addWidget(self.av_wm_x)
        coord_row.addWidget(QLabel("Y:"))
        coord_row.addWidget(self.av_wm_y)
        form.addRow(self.av_wm_mode_label)
        form.addRow("水印文字:", self.av_wm_text)
        form.addRow("字体与大小:", font_row)
        form.addRow("颜色与对齐:", color_row)
        form.addRow("间距设置:", spacing_row)
        form.addRow("格式强化:", border_row)
        form.addRow("坐标微调:", coord_row)
        left.addLayout(form)

        action_row = QHBoxLayout()
        self.av_wm_add_btn = QPushButton("添加为新水印")
        self.av_wm_add_btn.clicked.connect(self.add_av_watermark_new)
        self.av_wm_modify_btn = QPushButton("覆盖选中水印")
        self.av_wm_modify_btn.setEnabled(False)
        self.av_wm_modify_btn.clicked.connect(self.modify_selected_av_watermark)
        av_wm_delete_btn = QPushButton("删除")
        av_wm_delete_btn.clicked.connect(self.delete_selected_av_watermark)
        av_wm_reset_btn = QPushButton("清空面板/退出编辑")
        av_wm_reset_btn.clicked.connect(self.reset_av_watermark_form)
        action_row.addWidget(self.av_wm_add_btn)
        action_row.addWidget(self.av_wm_modify_btn)
        action_row.addWidget(av_wm_delete_btn)
        action_row.addWidget(av_wm_reset_btn)
        left.addLayout(action_row)

        self.av_wm_table = QTableWidget(0, 4)
        self.av_wm_table.setHorizontalHeaderLabels(["文本", "字体", "格式", "坐标"])
        self.av_wm_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.av_wm_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.av_wm_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.av_wm_table.verticalHeader().setVisible(False)
        self.av_wm_table.setMinimumHeight(145)
        self.av_wm_table.itemSelectionChanged.connect(self.on_av_wm_table_selection_changed)
        left.addWidget(self.av_wm_table)

        copy_mix_btn = QPushButton("复制批量混剪的文字水印列表")
        copy_mix_btn.clicked.connect(self.load_mix_watermarks_to_av)
        left.addWidget(copy_mix_btn)

        self.av_wm_size.setKeyboardTracking(True)
        self.av_wm_line_spacing.setKeyboardTracking(True)
        self.av_wm_letter_spacing.setKeyboardTracking(True)
        self.av_wm_border_w.setKeyboardTracking(True)
        self.av_wm_x.setKeyboardTracking(True)
        self.av_wm_y.setKeyboardTracking(True)
        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_line_spacing, self.av_wm_letter_spacing, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            signal = getattr(widget, 'textChanged', None) or getattr(widget, 'valueChanged', None) or getattr(widget, 'currentTextChanged', None) or getattr(widget, 'stateChanged', None)
            if signal:
                signal.connect(lambda *args: self.draw_av_watermark_preview())
        for spinbox in (self.av_wm_size, self.av_wm_line_spacing, self.av_wm_letter_spacing, self.av_wm_border_w, self.av_wm_x, self.av_wm_y):
            spinbox.lineEdit().textChanged.connect(lambda *args: self.draw_av_watermark_preview())

        left.addLayout(self.build_av_output_row())
        run_btn = QPushButton("开始给视频加文字水印")
        run_btn.setObjectName("startButton")
        run_btn.clicked.connect(self.start_av_text_watermark)
        left.addWidget(run_btn)
        left.addStretch()

        right = QVBoxLayout()
        preview_title = QLabel("参考预览：可直接点击/拖动定位水印")
        preview_title.setObjectName("appSubtitle")
        self.av_wm_preview_canvas = DragPreviewCanvas()
        self.av_wm_preview_canvas.setObjectName("referencePreview")
        self.av_wm_preview_canvas.setStyleSheet("background-color:#111; border:1px solid #334155; border-radius:6px;")
        self.av_wm_preview_canvas.setFixedSize(360, 640)
        self.av_wm_preview_canvas.pointChanged.connect(self.set_av_wm_position_from_preview)
        self.av_wm_design_w = 1080
        self.av_wm_design_h = 1920
        self.av_wm_preview_bg = ""
        right.addWidget(preview_title)
        right.addWidget(self.av_wm_preview_canvas, alignment=Qt.AlignCenter)
        right.addStretch()

        root.addLayout(left, 2)
        root.addLayout(right, 1)
        QTimer.singleShot(0, self.draw_av_watermark_preview)
        return page

    def start_av_text_watermark(self):
        watermarks = list(getattr(self, 'av_watermarks_data', []))
        active = self.current_av_watermark_data()
        if not watermarks and active['text'].strip():
            watermarks = [active]
        if not watermarks:
            QMessageBox.warning(self, "缺少水印", "请先添加至少一条文字水印。")
            return
        if watermarks and active['text'].strip() and getattr(self, 'av_editing_wm_index', -1) < 0 and active not in watermarks:
            self.update_log('[音视频处理] 当前面板有未添加文字，本次只处理列表里的水印；如需加入，请先点[添加为新水印]。')
        self._start_av_task({
            'kind': 'text_watermark',
            'input_text': self.av_wm_video_line.text().strip(),
            'watermarks': watermarks,
            'sys_fonts': self.sys_fonts,
            'suffix': '文字水印',
            'output_ext': 'mp4'
        })

    def build_av_transcribe_page(self):
        page = self.build_av_tool_page(
            "音视频转换成文本",
            "使用网盘资源包中的独立 AI 环境运行 SenseVoiceSmall；必须通过 GPU 检测，不再静默切换低效 CPU。",
            "Media Files (*.mp4 *.mov *.avi *.mkv *.webm *.mp3 *.wav *.m4a *.aac *.flac)",
            ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.aac', '.flac'],
            [],
            lambda line: self._start_av_task({
                'kind': 'transcribe',
                'input_text': line.text().strip(),
                'suffix': '转文本',
                'output_ext': 'txt'
            }),
            kind='transcribe'
        )
        result_group = QGroupBox("转写结果")
        result_layout = QVBoxLayout(result_group)
        copy_row = QHBoxLayout()
        copy_row.addStretch()
        preload_btn = QPushButton("检测转写环境")
        preload_btn.clicked.connect(self.preload_asr_model)
        copy_row.addWidget(preload_btn)
        copy_btn = QPushButton("一键复制")
        copy_btn.clicked.connect(self.copy_transcribe_result)
        copy_row.addWidget(copy_btn)
        self.transcribe_result_text = QTextEdit()
        self.transcribe_result_text.setPlaceholderText("单个音视频转写完成后，文本会显示在这里。")
        self.transcribe_result_text.setMinimumHeight(180)
        result_layout.addLayout(copy_row)
        result_layout.addWidget(self.transcribe_result_text)
        page.layout().addWidget(result_group)
        return page

    def preload_asr_model(self):
        if hasattr(self, 'asr_preload_worker') and self.asr_preload_worker.isRunning():
            return
        self.update_log("[音视频处理] 正在检测独立 AI 环境和 SenseVoiceSmall 转写模型...")
        self.asr_preload_worker = ASRPreloadWorker()
        self.asr_preload_worker.finished_signal.connect(self.on_asr_preload_finished)
        self.asr_preload_worker.start()

    def on_asr_preload_finished(self, ok, message):
        self.update_log(f"[音视频处理] {message}")
        if not ok:
            QMessageBox.warning(self, "环境检测失败", message)

    def build_av_trim_page(self):
        page = QFrame()
        page.setObjectName("card")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        title = QLabel("音频裁剪")
        title.setObjectName("appTitle")
        desc = QLabel("拖入或点击选择音频后，直接拖动左右选择框选取片段。播放按钮会循环试听当前选区。")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(desc)

        self.trim_input_line = QLineEdit()
        self.trim_input_line.setPlaceholderText("选择或拖入一个音频文件 — 切换子功能时将自动匹配上游输出")
        self.av_input_lines['trim_audio'] = self.trim_input_line
        QTimer.singleShot(100, lambda: self._try_av_chain_fill('trim_audio'))
        drop = FileDropArea("拖拽音频文件到这里，或点击选择文件")
        drop.filesDropped.connect(lambda files: self.set_trim_input(files[0] if files else ""))
        drop.clicked.connect(self.choose_trim_input)
        layout.addWidget(drop)
        layout.addWidget(self.trim_input_line)

        self.trim_range_selector = AudioRangeSelector()
        self.trim_range_selector.rangeChanged.connect(self.on_trim_range_changed)
        layout.addWidget(self.trim_range_selector)

        self.trim_range_label = QLabel("选区: 00:00.00 - 00:03.00")
        self.trim_range_label.setObjectName("appSubtitle")
        layout.addWidget(self.trim_range_label)
        layout.addLayout(self.build_av_output_row())

        control_row = QHBoxLayout()
        self.trim_play_btn = QPushButton()
        self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.trim_play_btn.setToolTip("播放/暂停选区")
        self.trim_play_btn.clicked.connect(self.toggle_trim_preview)
        stop_btn = QPushButton()
        stop_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        stop_btn.setToolTip("停止试听")
        stop_btn.clicked.connect(self.stop_trim_preview)
        fmt = QComboBox()
        fmt.addItems(["mp3", "wav"])
        control_row.addWidget(self.trim_play_btn)
        control_row.addWidget(stop_btn)
        control_row.addStretch()
        control_row.addWidget(QLabel("输出格式:"))
        control_row.addWidget(fmt)
        layout.addLayout(control_row)

        export_btn = QPushButton("导出选中片段")
        export_btn.setObjectName("startButton")
        export_btn.clicked.connect(lambda: self.export_trim_selection(fmt.currentText()))
        layout.addWidget(export_btn)
        layout.addStretch()
        return page

    def choose_trim_input(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "选择音频文件", self._file_dialog_default_dir(), "Audio Files (*.mp3 *.wav *.m4a *.aac *.flac)")
        if file_path:
            self._record_file_dialog_dir(file_path)
            self.set_trim_input(file_path)

    def set_trim_input(self, file_path):
        if not file_path:
            return
        self.trim_input_line.setText(file_path)
        duration = self.get_media_duration(file_path)
        if duration > 0:
            self.trim_range_selector.set_duration(duration)
            ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
            self.trim_range_selector.set_waveform(build_waveform_peaks(ffmpeg, file_path))
            self.update_trim_range_label(*self.trim_range_selector.selected_range())
            self.update_log(f"[音频裁剪] 已加载音频，时长 {duration:.2f} 秒。")
        else:
            self.update_log("[音频裁剪] 未能读取音频时长。")

    def update_trim_range_label(self, start, end):
        self.trim_range_label.setText(f"选区: {self.trim_range_selector._fmt(start)} - {self.trim_range_selector._fmt(end)}  共 {end - start:.2f} 秒")

    def on_trim_range_changed(self, start, end):
        self.update_trim_range_label(start, end)
        if getattr(self, 'trim_looping', False):
            self.stop_trim_preview()

    def _ensure_trim_player(self):
        if self.trim_player:
            return
        self.trim_player = QMediaPlayer(self)
        self.trim_player.positionChanged.connect(self.on_trim_position_changed)
        self.trim_player.mediaStatusChanged.connect(self.on_trim_media_status_changed)

    def play_trim_from_selection(self, path):
        start, end = self.trim_range_selector.selected_range()
        self.trim_looping = True
        self.trim_preview_start_ms = int(start * 1000)
        self.trim_preview_end_ms = int(end * 1000)
        self.trim_current_media_path = path
        self.trim_pending_seek = True

        current_media = self.trim_player.media()
        current_url = current_media.canonicalUrl().toLocalFile() if not current_media.isNull() else ""
        if os.path.abspath(current_url) != os.path.abspath(path):
            self.trim_player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
        else:
            self.trim_player.stop()
            self.trim_player.setPosition(self.trim_preview_start_ms)
            QTimer.singleShot(0, lambda: self.trim_player.setPosition(self.trim_preview_start_ms))
            QTimer.singleShot(30, self.trim_player.play)
            self.trim_pending_seek = False
        self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def on_trim_media_status_changed(self, status):
        if not getattr(self, 'trim_pending_seek', False):
            return
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self.trim_player.stop()
            self.trim_player.setPosition(self.trim_preview_start_ms)
            QTimer.singleShot(0, lambda: self.trim_player.setPosition(self.trim_preview_start_ms))
            QTimer.singleShot(30, self.trim_player.play)
            self.trim_pending_seek = False

    def toggle_trim_preview(self):
        path = self.trim_input_line.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "缺少音频", "请先选择音频文件。")
            return
        self._ensure_trim_player()
        if self.trim_looping and self.trim_player.state() == QMediaPlayer.PlayingState:
            self.trim_player.pause()
            self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            return
        if self.trim_looping and self.trim_player.state() == QMediaPlayer.PausedState:
            self.trim_player.play()
            self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
            return
        self.play_trim_from_selection(path)

    def on_trim_position_changed(self, pos):
        if self.trim_looping and hasattr(self, 'trim_preview_end_ms') and pos >= self.trim_preview_end_ms:
            self.trim_player.setPosition(self.trim_preview_start_ms)
            self.trim_player.play()

    def stop_trim_preview(self):
        self.trim_looping = False
        if self.trim_player:
            self.trim_player.stop()
        self.trim_pending_seek = False
        if hasattr(self, 'trim_play_btn'):
            self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def export_trim_selection(self, fmt):
        path = self.trim_input_line.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "缺少音频", "请先选择音频文件。")
            return
        start, end = self.trim_range_selector.selected_range()
        self._start_av_task({
            'kind': 'trim_audio',
            'input_text': path,
            'start': round(start, 3),
            'duration': round(end - start, 3),
            'format': fmt,
            'suffix': '裁剪',
            'output_ext': fmt
        })

    def build_av_vocal_page(self):
        fmt = QComboBox()
        fmt.addItems(["wav", "mp3"])
        return self.build_av_tool_page(
            "去除背景音乐保留人声",
            "基础版会做高低频过滤、降噪和人声响度增强。真正的人声/伴奏分离后续可接入 Demucs/UVR 本地模型。",
            "Media Files (*.mp4 *.mov *.avi *.mkv *.webm *.mp3 *.wav *.m4a *.aac *.flac)",
            ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.aac', '.flac'],
            [("输出格式:", fmt)],
            lambda line: self._start_av_task({
                'kind': 'vocal_enhance',
                'input_text': line.text().strip(),
                'format': fmt.currentText(),
                'suffix': '保留人声',
                'output_ext': fmt.currentText()
            }),
            kind='vocal_enhance'
        )

