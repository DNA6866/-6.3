import os
import platform
import random
import re
import shutil
import subprocess
import sys
import time

from PyQt5.QtCore import QEvent, Qt, QTimer
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .comfy_api import ComfyUIApi, ComfyAPIError
from .comfy_manager import (
    ComfyUIManager,
    find_comfyui_pids,
    find_listening_pids,
    find_python_executable,
    hidden_startupinfo,
    is_api_available,
    is_port_open,
)
from .config import (
    APP_ROOT,
    DIAGNOSTIC_FILE,
    ENV_REPORT_FILE,
    LOG_DIR,
    OUTPUT_DIR,
    STARTUP_LOG_FILE,
    WORKFLOW_DIR,
    load_config,
    save_config,
)
from paths import workspace_path
from services.platform_service import open_path
from .hardware_profile import detect_hardware_profile, profile_summary
from .image_tools import open_output_dir, reveal_file
from .task_worker import ComfyStartWorker, EnvCheckWorker, ImageGenerateWorker
from .workflow_manager import copy_workflow_to_local, scan_zimage_workflows
from ui_components import configure_spinbox_for_direct_input


COMFY_KEEPALIVE_SECONDS = 30 * 60


class AspectPreviewLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.original_pixmap = QPixmap()
        self.setAlignment(Qt.AlignCenter)

    def set_image(self, path):
        pix = QPixmap(path)
        if pix.isNull():
            self.original_pixmap = QPixmap()
            self.setText("图片预览失败")
            self.update()
            return False
        self.original_pixmap = pix
        self.setText("")
        self.update()
        return True

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.original_pixmap.isNull():
            return
        target = self.rect().adjusted(14, 14, -14, -14)
        scaled = self.original_pixmap.scaled(target.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = target.x() + (target.width() - scaled.width()) // 2
        y = target.y() + (target.height() - scaled.height()) // 2
        painter = QPainter(self)
        painter.drawPixmap(x, y, scaled)


class ImageGenerateWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("imageGeneratePage")
        self.config = load_config()
        self.manager_holder = {}
        self.env_worker = None
        self.start_worker = None
        self.generate_worker = None
        self.generated_files = []
        # 批量提示词状态（断点记忆）
        self._batch_prompts = []           # 解析后的提示词列表
        self._batch_index = 0              # 当前生成到的索引
        self._batch_txt_path = ""          # 上一次导入的 TXT 路径
        self._batch_total = 0              # 总提示词数
        self._app_close_prepared = False
        self._build_ui()
        configure_spinbox_for_direct_input(self)
        self.load_to_ui()
        QTimer.singleShot(0, self._apply_responsive_layout)
        # 耗时操作下放到后台，避免阻塞主线程
        QTimer.singleShot(0, lambda: self.refresh_workflows(copy_first=True))
        QTimer.singleShot(100, self._async_refresh_engine_state)
        # 启动时清理上次未正常停止的 ComfyUI（延迟执行）
        QTimer.singleShot(3000, self._check_shutdown_cleanup)

    def _async_refresh_engine_state(self):
        """后台检测 ComfyUI 状态，为 UI 提供即时占位提示。"""
        try:
            self.refresh_engine_state()
        except Exception:
            self.set_engine_state("stopped")

    def _apply_card_style(self, group):
        group.setStyleSheet("""
            QGroupBox {
                background: #111827;
                border: 1px solid #64748B;
                border-radius: 10px;
                margin-top: 18px;
                padding: 14px 12px 12px 12px;
                color: #F8FAFC;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 8px;
                color: #F8FAFC;
                background: #111827;
            }
            QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #020617;
                border: 1px solid #64748B;
                border-radius: 8px;
                color: #F8FAFC;
                padding: 7px;
                selection-background-color: #2563EB;
            }
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #60A5FA;
            }
            QLabel {
                background: transparent;
                color: #F8FAFC;
            }
            QLabel#mutedText {
                color: #CBD5E1;
            }
            QLabel#warningText {
                background: #2A1F0B;
                border: 1px solid #B45309;
                border-radius: 8px;
                color: #FDE68A;
                padding: 8px 10px;
            }
            QTableWidget, QListWidget {
                background: #020617;
                alternate-background-color: #0F172A;
                border: 1px solid #64748B;
                border-radius: 8px;
                color: #F8FAFC;
                gridline-color: #334155;
            }
            QHeaderView::section {
                background: #1E293B;
                color: #F8FAFC;
                border: none;
                border-right: 1px solid #334155;
                padding: 8px;
                font-weight: 700;
            }
        """)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("AI封面生成")
        title.setObjectName("pageTitle")
        sub = QLabel("复用本机 ComfyUI + Z-Image 工作流，软件内完成封面图生成。")
        sub.setObjectName("mutedText")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        header.addLayout(title_box, 1)
        self.status_label = QLabel("当前状态：未检测")
        self.status_label.setObjectName("statusPill")
        header.addWidget(self.status_label)
        root.addLayout(header)

        left_container = QWidget()
        left_panel = QVBoxLayout(left_container)
        left_panel.setContentsMargins(0, 0, 4, 0)
        left_panel.setSpacing(8)
        left_panel.addWidget(self._build_param_group())
        left_panel.addWidget(self._build_top_group())
        left_panel.addWidget(self._build_env_table())
        left_panel.addStretch()

        left_scroll = QScrollArea()
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        left_scroll.setStyleSheet("QScrollArea{background:#0B1120;border:none;}")
        left_scroll.viewport().setStyleSheet("background:#0B1120;border:none;")
        left_scroll.setWidget(left_container)
        left_scroll.setMinimumWidth(300)
        self.left_scroll = left_scroll
        self.preview_group = self._build_preview_group()

        self.body_splitter = QSplitter(Qt.Horizontal)
        self.body_splitter.setObjectName("imageGenerateSplitter")
        self.body_splitter.setChildrenCollapsible(False)
        self.body_splitter.setHandleWidth(8)
        self.body_splitter.addWidget(left_scroll)
        self.body_splitter.addWidget(self.preview_group)
        self.body_splitter.setStretchFactor(0, 2)
        self.body_splitter.setStretchFactor(1, 3)
        self.body_splitter.setSizes([430, 650])
        root.addWidget(self.body_splitter, 1)

        log_group = QGroupBox("运行日志")
        log_group.setObjectName("imageCard")
        self._apply_card_style(log_group)
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setObjectName("logConsole")
        self.log_text.setMinimumHeight(44)
        self.log_text.setMaximumHeight(64)
        log_layout.addWidget(self.log_text)
        root.addWidget(log_group)

    def _build_top_group(self):
        group = QGroupBox("ComfyUI 后台引擎")
        group.setObjectName("imageCard")
        self._apply_card_style(group)
        layout = QVBoxLayout(group)
        layout.setSpacing(8)
        self.comfy_path_edit = QLineEdit()
        self.comfy_path_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.workflow_combo = QComboBox()
        self.workflow_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.auto_start_chk = QCheckBox("生成前自动启动 ComfyUI")
        self.detect_btn = QPushButton("检测环境")
        self.choose_btn = QPushButton("选择路径")
        self.start_btn = QPushButton("启动")
        self.stop_btn = QPushButton("停止")
        self.open_output_btn = QPushButton("输出目录")
        self.report_btn = QPushButton("检测报告")
        self.copy_diag_btn = QPushButton("复制诊断")
        for btn in (self.detect_btn, self.choose_btn, self.open_output_btn, self.report_btn, self.copy_diag_btn):
            btn.setObjectName("imageActionButton")
            btn.setMinimumHeight(30)
        self.start_btn.setObjectName("imagePrimaryButton")
        self.stop_btn.setObjectName("imageDangerButton")
        self.start_btn.setMinimumHeight(30)
        self.stop_btn.setMinimumHeight(30)
        self.engine_progress = QProgressBar()
        self.engine_progress.setVisible(False)
        self.engine_hint = QLabel("ComfyUI 未启动")
        self.engine_hint.setObjectName("mutedText")
        self.engine_hint.setWordWrap(True)
        self.engine_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.profile_label = QLabel("当前适配档位：未检测")
        self.profile_label.setObjectName("warningText")
        self.profile_label.setWordWrap(True)
        self.profile_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.profile_label.setMaximumHeight(56)

        self.detect_btn.clicked.connect(self.run_env_check)
        self.choose_btn.clicked.connect(self.choose_comfy_path)
        self.start_btn.clicked.connect(self.start_comfyui)
        self.stop_btn.clicked.connect(self.stop_comfyui)
        self.open_output_btn.clicked.connect(lambda: open_output_dir(self.config.get("output_dir", OUTPUT_DIR)))
        self.report_btn.clicked.connect(self.open_report)
        self.copy_diag_btn.clicked.connect(self.copy_diagnostic_info)
        self.workflow_combo.currentIndexChanged.connect(self.on_workflow_changed)
        self.auto_start_chk.stateChanged.connect(self.save_from_ui)

        path_row = QGridLayout()
        path_row.setHorizontalSpacing(8)
        path_row.setColumnStretch(1, 1)
        path_row.addWidget(QLabel("路径"), 0, 0)
        path_row.addWidget(self.comfy_path_edit, 0, 1)
        path_row.addWidget(self.choose_btn, 0, 2)
        layout.addLayout(path_row)

        workflow_row = QGridLayout()
        workflow_row.setHorizontalSpacing(8)
        workflow_row.setColumnStretch(1, 1)
        workflow_row.addWidget(QLabel("工作流"), 0, 0)
        workflow_row.addWidget(self.workflow_combo, 0, 1)
        layout.addLayout(workflow_row)
        layout.addWidget(self.auto_start_chk)

        button_grid = QGridLayout()
        button_grid.setHorizontalSpacing(8)
        button_grid.setVerticalSpacing(8)
        button_grid.addWidget(self.detect_btn, 0, 0)
        button_grid.addWidget(self.start_btn, 0, 1)
        button_grid.addWidget(self.stop_btn, 0, 2)
        button_grid.addWidget(self.open_output_btn, 1, 0)
        button_grid.addWidget(self.report_btn, 1, 1)
        button_grid.addWidget(self.copy_diag_btn, 1, 2)
        button_grid.setColumnStretch(0, 1)
        button_grid.setColumnStretch(1, 1)
        button_grid.setColumnStretch(2, 1)
        layout.addLayout(button_grid)
        layout.addWidget(self.engine_hint)
        layout.addWidget(self.engine_progress)
        layout.addWidget(self.profile_label)
        return group

    def _build_param_group(self):
        params = QGroupBox("生成参数")
        params.setObjectName("imageCard")
        self._apply_card_style(params)
        form = QVBoxLayout(params)
        form.setSpacing(8)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText("例如：电商商品主图，白底棚拍，高级质感，柔和布光，产品清晰，适合直播间")
        self.prompt_edit.setMinimumHeight(78)
        self.prompt_edit.setMaximumHeight(112)
        # 批量提示词：导入 TXT + 进度标签
        self.import_txt_btn = QPushButton("导入提示词TXT")
        self.import_txt_btn.setObjectName("imageActionButton")
        self.import_txt_btn.setMinimumHeight(34)
        self.import_txt_btn.clicked.connect(self.import_prompt_txt)
        self.batch_progress_label = QLabel("")
        self.batch_progress_label.setObjectName("mutedText")
        self.batch_progress_label.setWordWrap(True)
        self.batch_progress_label.setVisible(False)
        self.width_spin = QSpinBox(); self.width_spin.setRange(512, 2048); self.width_spin.setSingleStep(20); self.width_spin.setValue(900)
        self.height_spin = QSpinBox(); self.height_spin.setRange(512, 2048); self.height_spin.setSingleStep(20); self.height_spin.setValue(1600)
        self.steps_spin = QSpinBox(); self.steps_spin.setRange(1, 80); self.steps_spin.setValue(4)
        self.cfg_spin = QDoubleSpinBox(); self.cfg_spin.setRange(0.1, 20.0); self.cfg_spin.setSingleStep(0.1); self.cfg_spin.setValue(1.0)
        self.seed_spin = QSpinBox(); self.seed_spin.setRange(0, 2147483647); self.seed_spin.setValue(0)
        self.count_spin = QSpinBox(); self.count_spin.setRange(1, 999); self.count_spin.setValue(1)
        self.count_spin.setToolTip("可直接输入张数；多张会按单张循环提交，不使用 ComfyUI 批量 batch，避免 20 系显卡生成糊图。")
        random_seed_btn = QPushButton("随机种子")
        random_seed_btn.setObjectName("imageActionButton")
        random_seed_btn.setMinimumHeight(34)
        random_seed_btn.clicked.connect(lambda: self.seed_spin.setValue(random.randint(1, 2147483647)))
        self.generate_btn = QPushButton("生成封面图")
        self.generate_btn.setObjectName("imageGenerateButton")
        self.generate_btn.setMinimumHeight(38)
        self.generate_btn.clicked.connect(self.generate_images)
        self.stop_generate_btn = QPushButton("停止生成")
        self.stop_generate_btn.setObjectName("imageDangerButton")
        self.stop_generate_btn.setMinimumHeight(38)
        self.stop_generate_btn.clicked.connect(self.stop_generation)
        self.stop_generate_btn.setVisible(False)
        self.progress = QProgressBar()

        form.addWidget(QLabel("正向提示词"))
        form.addWidget(self.prompt_edit)

        import_row = QHBoxLayout()
        import_row.addWidget(self.import_txt_btn)
        import_row.addWidget(self.batch_progress_label, 1)
        form.addLayout(import_row)

        value_grid = QGridLayout()
        value_grid.setHorizontalSpacing(8)
        value_grid.setVerticalSpacing(8)
        value_grid.setColumnStretch(1, 1)
        value_grid.setColumnStretch(3, 1)
        value_grid.addWidget(QLabel("宽度"), 0, 0); value_grid.addWidget(self.width_spin, 0, 1)
        value_grid.addWidget(QLabel("高度"), 0, 2); value_grid.addWidget(self.height_spin, 0, 3)
        value_grid.addWidget(QLabel("步数"), 1, 0); value_grid.addWidget(self.steps_spin, 1, 1)
        value_grid.addWidget(QLabel("CFG"), 1, 2); value_grid.addWidget(self.cfg_spin, 1, 3)
        value_grid.addWidget(QLabel("种子"), 2, 0); value_grid.addWidget(self.seed_spin, 2, 1)
        value_grid.addWidget(random_seed_btn, 2, 2, 1, 2)
        value_grid.addWidget(QLabel("张数"), 3, 0); value_grid.addWidget(self.count_spin, 3, 1)
        count_hint = QLabel("多张会逐张生成，更稳但耗时会按张数增加")
        count_hint.setObjectName("appSubtitle")
        count_hint.setWordWrap(True)
        value_grid.addWidget(count_hint, 3, 2, 1, 2)
        form.addLayout(value_grid)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        action_row.addWidget(self.generate_btn, 1)
        action_row.addWidget(self.stop_generate_btn, 1)
        form.addLayout(action_row)
        form.addWidget(self.progress)
        return params

    def _build_preview_group(self):
        preview = QGroupBox("生成预览")
        preview.setObjectName("imagePreviewGroup")
        preview.setMinimumWidth(300)
        self._apply_card_style(preview)
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        preview_layout.setSpacing(6)

        self.preview_container = QWidget()
        self.preview_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_container.setMinimumHeight(220)
        self.preview_container.setStyleSheet("background:#070B12; border-radius:8px;")
        self.big_preview = AspectPreviewLabel("生成完成后在这里预览")
        self.big_preview.setObjectName("imageBigPreview")
        self.big_preview.setStyleSheet("background:transparent; border:none;")
        self.big_preview.setMinimumSize(0, 0)
        self.big_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        container_layout = QVBoxLayout(self.preview_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addWidget(self.big_preview)

        self.thumb_list = QListWidget()
        self.thumb_list.setMaximumHeight(90)
        self.thumb_list.itemClicked.connect(self.on_thumb_clicked)
        open_file_btn = QPushButton("打开当前图片目录")
        open_file_btn.setObjectName("imageActionButton")
        open_file_btn.setMinimumHeight(32)
        open_file_btn.clicked.connect(self.open_selected_image_dir)
        self.move_to_cover_btn = QPushButton("移至封面素材库")
        self.move_to_cover_btn.setObjectName("imageActionButton")
        self.move_to_cover_btn.setMinimumHeight(32)
        self.move_to_cover_btn.clicked.connect(self.move_to_cover_library)
        self.move_to_cover_btn.setEnabled(False)
        btn_row = QHBoxLayout()
        btn_row.addWidget(open_file_btn, 1)
        btn_row.addWidget(self.move_to_cover_btn, 1)

        preview_layout.addWidget(self.preview_container, 1)
        preview_layout.addWidget(self.thumb_list)
        preview_layout.addLayout(btn_row)
        return preview

    def _apply_responsive_layout(self):
        if not hasattr(self, "body_splitter"):
            return
        compact = self.width() <= 1050
        target_orientation = Qt.Vertical if compact else Qt.Horizontal
        if self.body_splitter.orientation() != target_orientation:
            self.body_splitter.setOrientation(target_orientation)
            if compact:
                self.body_splitter.setSizes([260, 240])
            else:
                self.body_splitter.setSizes([430, 650])
        if hasattr(self, "preview_container"):
            self.preview_container.setMinimumHeight(140 if compact else 220)
        if hasattr(self, "preview_group"):
            self.preview_group.setMinimumHeight(240 if compact else 0)
        if hasattr(self, "thumb_list"):
            self.thumb_list.setMaximumHeight(56 if compact else 90)
        if hasattr(self, "log_text"):
            self.log_text.setMaximumHeight(50 if compact else 64)

    def _build_env_table(self):
        group = QGroupBox("环境检测结果")
        group.setObjectName("imageCard")
        self._apply_card_style(group)
        layout = QVBoxLayout(group)
        self.env_table = QTableWidget(0, 4)
        self.env_table.setHorizontalHeaderLabels(["项目", "状态", "检测值", "客户提示"])
        self.env_table.horizontalHeader().setStretchLastSection(True)
        self.env_table.verticalHeader().setVisible(False)
        self.env_table.setMinimumHeight(82)
        self.env_table.setMaximumHeight(112)
        layout.addWidget(self.env_table)
        return group

    def log(self, text):
        self.log_text.append(text)

    def load_to_ui(self):
        self.comfy_path_edit.setText(self.config.get("comfyui_path", "D:/ComfyUI/ComfyUI"))
        self.auto_start_chk.setChecked(bool(self.config.get("auto_start_comfyui", True)))
        if self.config.get("profile_summary"):
            self.profile_label.setText(f"当前适配档位：{self.config.get('profile_summary')}")

    def save_from_ui(self):
        self.config["comfyui_path"] = self.comfy_path_edit.text().strip()
        self.config["auto_start_comfyui"] = self.auto_start_chk.isChecked()
        self.config["workflow_path"] = self.workflow_combo.currentData() or self.config.get("workflow_path", "")
        save_config(self.config)

    def refresh_workflows(self, copy_first=False):
        workflows = scan_zimage_workflows(self.config.get("comfyui_path", ""))
        if copy_first and workflows:
            try:
                first_path = os.path.abspath(workflows[0])
                local_dir = os.path.abspath(WORKFLOW_DIR)
                if os.path.normcase(os.path.dirname(first_path)) != os.path.normcase(local_dir):
                    local = copy_workflow_to_local(workflows[0])
                    if local not in workflows:
                        workflows.insert(0, local)
            except Exception as exc:
                self.log(f"工作流复制失败：{exc}")
        self.workflow_combo.blockSignals(True)
        self.workflow_combo.clear()
        for path in workflows:
            self.workflow_combo.addItem(os.path.basename(path), path)
        selected = self.config.get("workflow_path", "")
        if selected:
            idx = self.workflow_combo.findData(selected)
            if idx >= 0:
                self.workflow_combo.setCurrentIndex(idx)
        self.workflow_combo.blockSignals(False)
        if self.workflow_combo.count() > 0:
            self.config["workflow_path"] = self.workflow_combo.currentData()
            save_config(self.config)

    def on_workflow_changed(self):
        self.save_from_ui()

    def choose_comfy_path(self):
        path = QFileDialog.getExistingDirectory(self, "选择 ComfyUI 主目录", self.comfy_path_edit.text() or "D:/ComfyUI/ComfyUI")
        if path:
            self.comfy_path_edit.setText(path)
            self.save_from_ui()
            self.refresh_workflows(copy_first=True)

    def run_env_check(self):
        self.save_from_ui()
        if self.env_worker and self.env_worker.isRunning():
            return
        self.status_label.setText("当前状态：检测中")
        self.env_worker = EnvCheckWorker(self.config.copy())
        self.env_worker.finished.connect(self.on_env_finished)
        self.env_worker.failed.connect(lambda msg: QMessageBox.warning(self, "检测失败", msg))
        self.env_worker.start()

    def on_env_finished(self, results, report_path, passed):
        self.fill_env_table(results)
        self.apply_hardware_profile()
        if passed:
            self.refresh_engine_state()
        else:
            self.set_engine_state("error", "环境检测未通过，请根据表格里的红色项目处理。")
        self.log(f"环境检测完成：{report_path}")

    def fill_env_table(self, results):
        self.env_table.setRowCount(len(results))
        colors = {"pass": QColor("#22C55E"), "warn": QColor("#F59E0B"), "fail": QColor("#EF4444")}
        labels = {"pass": "通过", "warn": "警告", "fail": "失败"}
        for row, item in enumerate(results):
            values = [item["name"], labels.get(item["status"], item["status"]), item["value"], item.get("suggestion", "")]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if col == 1:
                    cell.setForeground(colors.get(item["status"], QColor("#E5E7EB")))
                self.env_table.setItem(row, col, cell)
        self.env_table.resizeColumnsToContents()

    def set_engine_state(self, state, hint=""):
        styles = {
            "stopped": {
                "status": "当前状态：ComfyUI 未启动",
                "status_style": "background:#020617;border:1px solid #475569;border-radius:8px;color:#CBD5E1;padding:8px 10px;font-weight:700;",
                "start_style": "background:#16A34A;color:#FFFFFF;border:1px solid #22C55E;border-radius:8px;padding:8px 14px;font-weight:700;min-height:36px;",
                "stop_style": "background:#151A23;color:#667085;border:1px solid #242C3A;border-radius:8px;padding:8px 14px;font-weight:700;min-height:36px;",
                "hint": "ComfyUI 未启动，点击[启动 ComfyUI]后等待后台引擎就绪。",
            },
            "starting": {
                "status": "当前状态：ComfyUI 启动中",
                "status_style": "background:#2A1F0B;border:1px solid #B45309;border-radius:8px;color:#FDE68A;padding:8px 10px;font-weight:700;",
                "start_style": "background:#78350F;color:#FDE68A;border:1px solid #F59E0B;border-radius:8px;padding:8px 14px;font-weight:700;min-height:36px;",
                "stop_style": "background:#151A23;color:#667085;border:1px solid #242C3A;border-radius:8px;padding:8px 14px;font-weight:700;min-height:36px;",
                "hint": "正在启动 ComfyUI，首次启动可能需要几十秒，请稍等。",
            },
            "running": {
                "status": "当前状态：ComfyUI 已启动",
                "status_style": "background:#052E16;border:1px solid #22C55E;border-radius:8px;color:#BBF7D0;padding:8px 10px;font-weight:700;",
                "start_style": "background:#151A23;color:#667085;border:1px solid #242C3A;border-radius:8px;padding:8px 14px;font-weight:700;min-height:36px;",
                "stop_style": "background:#7F1D1D;color:#FFFFFF;border:1px solid #EF4444;border-radius:8px;padding:8px 14px;font-weight:700;min-height:36px;",
                "hint": "ComfyUI 后台引擎已就绪，可以生成商品图。",
            },
            "error": {
                "status": "当前状态：ComfyUI 异常",
                "status_style": "background:#3A1012;border:1px solid #EF4444;border-radius:8px;color:#FCA5A5;padding:8px 10px;font-weight:700;",
                "start_style": "background:#16A34A;color:#FFFFFF;border:1px solid #22C55E;border-radius:8px;padding:8px 14px;font-weight:700;min-height:36px;",
                "stop_style": "background:#151A23;color:#667085;border:1px solid #242C3A;border-radius:8px;padding:8px 14px;font-weight:700;min-height:36px;",
                "hint": "ComfyUI 启动失败，请先检测环境或查看运行日志。",
            },
        }
        current = styles.get(state, styles["stopped"])
        self.status_label.setText(current["status"])
        self.status_label.setStyleSheet(current["status_style"])
        self.start_btn.setStyleSheet(current["start_style"])
        self.stop_btn.setStyleSheet(current["stop_style"])
        self.engine_hint.setText(hint or current["hint"])
        self.engine_progress.setVisible(state == "starting")
        if state == "starting":
            self.engine_progress.setRange(0, 0)
            self.start_btn.setText("启动中...")
        else:
            self.engine_progress.setRange(0, 100)
            self.engine_progress.setValue(0)
            self.start_btn.setText("启动")
        self.start_btn.setEnabled(state in ("stopped", "error"))
        self.stop_btn.setEnabled(state == "running")
        if state == "starting":
            self.generate_btn.setEnabled(False)
        elif not (self.generate_worker and self.generate_worker.isRunning()):
            self.generate_btn.setEnabled(True)

    def refresh_engine_state(self):
        manager = ComfyUIManager(self.config["comfyui_path"], self.config["host"], self.config["port"])
        self.set_engine_state("running" if manager.is_running() else "stopped")

    def apply_hardware_profile(self):
        profile = detect_hardware_profile(self.config["comfyui_path"])
        summary = profile_summary(profile)
        self.profile_label.setText(f"当前适配档位：{summary}；推荐 {profile['width']}x{profile['height']}")
        self.config["profile_summary"] = summary
        self.config["comfyui_args"] = profile.get("comfyui_args", [])
        if self.config.get("auto_hardware_profile", True):
            self.width_spin.setValue(profile["width"])
            self.height_spin.setValue(profile["height"])
            self.steps_spin.setValue(profile["steps"])
        save_config(self.config)
        return profile

    def start_comfyui(self, then_generate=False):
        self.save_from_ui()
        if self.start_worker and self.start_worker.isRunning():
            return
        profile = self.apply_hardware_profile()
        if profile.get("status") == "fail":
            QMessageBox.warning(self, "硬件环境不满足", "当前电脑暂不满足 AI 商品图生成环境：\n" + "\n".join(profile.get("failures", [])))
            self.set_engine_state("error")
            return
        self.set_engine_state("starting")
        self.config["startup_log_file"] = STARTUP_LOG_FILE
        self.start_worker = ComfyStartWorker(self.config.copy(), self.manager_holder)
        self.start_worker.message.connect(self.log)
        self.start_worker.finished.connect(lambda msg: self.on_start_finished(msg, then_generate))
        self.start_worker.failed.connect(self.on_start_failed)
        self.start_worker.start()

    def on_start_finished(self, msg, then_generate):
        self.set_engine_state("running")
        self.log(msg)
        if then_generate:
            self.generate_images(skip_start=True)

    def on_start_failed(self, msg):
        self.set_engine_state("error", "ComfyUI 启动失败。请点击[复制诊断]，把复制内容发给技术排查。")
        QMessageBox.warning(self, "启动失败", f"{msg}\n\n请点击[复制诊断]，把复制内容发给技术排查。")
        self.log(msg)

    def stop_comfyui(self):
        """手动停止 ComfyUI：清空任务队列，并真正结束后台服务。"""
        self.save_from_ui()
        host = self.config.get("host", "127.0.0.1")
        port = self.config.get("port", 8188)
        messages = []

        self._remove_shutdown_timestamp()

        try:
            api = ComfyUIApi(host, port)
            api.interrupt()
            api.clear_queue()
            messages.append("已通过 API 中断当前任务并清空队列。")
        except Exception as exc:
            messages.append(f"API 调用失败：{exc}")

        manager = self.manager_holder.get("manager") or ComfyUIManager(
            self.config.get("comfyui_path", ""),
            host,
            port,
        )
        ok, stop_msg = manager.stop()
        messages.append(stop_msg)
        if ok:
            self.manager_holder.pop("manager", None)

        msg = "；".join(messages)
        self.log(msg)

        still_running = ComfyUIManager(self.config.get("comfyui_path", ""), host, port).is_running()
        if still_running:
            self.set_engine_state("running")
            QMessageBox.warning(self, "ComfyUI 仍在运行", msg)
        else:
            self.set_engine_state("stopped")
            QMessageBox.information(self, "ComfyUI 已停止", msg)

    def generate_images(self, skip_start=False):
        self.save_from_ui()
        workflow_path = self.workflow_combo.currentData()
        if not workflow_path or not os.path.exists(workflow_path):
            QMessageBox.warning(self, "缺少工作流", "未找到 Z-Image 工作流文件，请确认工作流已正确放置。")
            return
        if not self.prompt_edit.toPlainText().strip():
            QMessageBox.warning(self, "缺少提示词", "请先输入正向提示词。")
            return
        manager = ComfyUIManager(self.config["comfyui_path"], self.config["host"], self.config["port"])
        if not skip_start and self.config.get("auto_start_comfyui", True) and not manager.is_running():
            self.start_comfyui(then_generate=True)
            return
        if not manager.is_running():
            QMessageBox.warning(self, "ComfyUI 未启动", "请先点击[启动 ComfyUI]，或确认后台 API 可访问。")
            return
        params = {
            "workflow_path": workflow_path,
            "prompt": self.prompt_edit.toPlainText().strip(),
            "width": self.width_spin.value(),
            "height": self.height_spin.value(),
            "steps": self.steps_spin.value(),
            "cfg": self.cfg_spin.value(),
            "seed": self.seed_spin.value(),
            "image_count": self.count_spin.value(),
        }
        self.generate_btn.setEnabled(False)
        self.stop_generate_btn.setText("停止生成")
        self.stop_generate_btn.setEnabled(True)
        self.stop_generate_btn.setVisible(True)
        self.progress.setValue(0)
        self.generate_worker = ImageGenerateWorker(self.config.copy(), params)
        self.generate_worker.progress.connect(self.progress.setValue)
        self.generate_worker.message.connect(self.log)
        self.generate_worker.finished.connect(self.on_generate_finished)
        self.generate_worker.failed.connect(self.on_generate_failed)
        self.generate_worker.cancelled.connect(self.on_generate_cancelled)
        self.generate_worker.start()

    def _set_generation_idle(self):
        self.generate_btn.setEnabled(True)
        self.stop_generate_btn.setVisible(False)
        self.stop_generate_btn.setEnabled(True)
        self.stop_generate_btn.setText("停止生成")

    def _show_generated_files(self, files):
        self.generated_files = list(files or [])
        self.thumb_list.clear()
        for path in self.generated_files:
            pix = QPixmap(path)
            icon = QIcon(pix.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            item = QListWidgetItem(icon, os.path.basename(path))
            item.setData(Qt.UserRole, path)
            self.thumb_list.addItem(item)
        if self.generated_files:
            self.show_image(self.generated_files[0])

    def on_generate_finished(self, files):
        self._set_generation_idle()
        self._show_generated_files(files)
        self.log(f"生成完成：{len(self.generated_files)} 张图片")
        # 每次生成完成后自动刷新随机种子，提升连续生成操作体验
        self.seed_spin.setValue(random.randint(1, 2147483647))
        self._update_move_btn_state()
        # 批量提示词模式：继续下一条
        if self._batch_prompts and self._batch_index < self._batch_total:
            QTimer.singleShot(500, self._batch_generate_next)
        elif self._batch_prompts:
            self.log(f"✅ 全部 {self._batch_total} 条提示词生成完毕。")
            self.batch_progress_label.setText(f"已完成 {self._batch_total}/{self._batch_total} 条")
            self._batch_prompts = []
            self._batch_index = 0
            self._batch_total = 0
            self._batch_txt_path = ""
            self.save_from_ui()

    def on_generate_cancelled(self, files):
        self._set_generation_idle()
        self.progress.setValue(0)
        self._show_generated_files(files)
        generated_count = len(self.generated_files)
        if self._batch_prompts:
            self.batch_progress_label.setText(f"已停止，保留已生成 {generated_count} 张")
        self._clear_batch_state_after_stop()
        self._update_move_btn_state()
        self.log(f"已停止生成任务，保留已完成图片 {generated_count} 张。")

    # ───── 批量提示词 TXT 导入与断点续传 ─────
    def import_prompt_txt(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入提示词 TXT", workspace_path(),
                                               "文本文件 (*.txt);;所有文件 (*.*)")
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read()
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", f"无法读取 TXT 文件：{exc}")
            return
        prompts = self._parse_prompts_from_txt(raw)
        if not prompts:
            QMessageBox.warning(self, "无有效内容", "TXT 文件中未找到有效的提示词（空行分隔）。")
            return
        # 断点记忆：同一 TXT 文件 → 从上次中断处继续
        saved_path = self.config.get("batch_txt_path", "")
        saved_index = self.config.get("batch_txt_index", 0)
        if os.path.abspath(path) == os.path.abspath(saved_path) and saved_index > 0:
            resume = saved_index if saved_index < len(prompts) else 0
            reply = QMessageBox.question(self, "断点续传",
                f"检测到上次未完成的批量任务。\n"
                f"已完成 {resume}/{len(prompts)} 条提示词。\n"
                f"是否从第 {resume + 1} 条继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply == QMessageBox.Yes:
                self._batch_index = resume
            else:
                self._batch_index = 0
        else:
            self._batch_index = 0
        self._batch_prompts = prompts
        self._batch_total = len(prompts)
        self._batch_txt_path = os.path.abspath(path)
        self.config["batch_txt_path"] = self._batch_txt_path
        self.config["batch_txt_index"] = self._batch_index
        self.save_from_ui()
        # 显示批量状态
        self.batch_progress_label.setVisible(True)
        self.batch_progress_label.setText(
            f"已导入 {len(prompts)} 条提示词，当前进度：{self._batch_index}/{len(prompts)}"
        )
        self.prompt_edit.setPlaceholderText(
            f"批量模式：{len(prompts)} 条提示词（空行分隔），将依次生成"
        )
        self.log(f"导入批量提示词：{len(prompts)} 条，起始索引 {self._batch_index + 1}")
        # 首次导入：将第一条提示词填入选区
        if self._batch_index < self._batch_total:
            self.prompt_edit.setPlainText(self._batch_prompts[self._batch_index])

    def _parse_prompts_from_txt(self, text):
        """按空行分隔解析提示词，过滤空白行。"""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n\s*\n", text)
        prompts = [b.strip() for b in blocks if b.strip()]
        return prompts

    def _batch_generate_next(self):
        """批量模式：填入下一条提示词并开始生成。"""
        if not self._batch_prompts or self._batch_index >= self._batch_total:
            self.batch_progress_label.setText(f"已完成 {self._batch_total}/{self._batch_total} 条")
            self.log(f"✅ 全部 {self._batch_total} 条提示词生成完毕。")
            self._batch_prompts = []
            self._batch_index = 0
            self._batch_total = 0
            self._batch_txt_path = ""
            self.config.pop("batch_txt_path", None)
            self.config.pop("batch_txt_index", None)
            self.save_from_ui()
            return
        self.prompt_edit.setPlainText(self._batch_prompts[self._batch_index])
        self.batch_progress_label.setText(
            f"正在生成第 {self._batch_index + 1}/{self._batch_total} 条提示词"
        )
        self.log(f"批量生成：第 {self._batch_index + 1}/{self._batch_total} 条 → {self._batch_prompts[self._batch_index][:60]}...")
        self._batch_index += 1
        self.config["batch_txt_index"] = self._batch_index
        self.save_from_ui()
        self.generate_images(skip_start=True)

    def stop_generation(self):
        """终止当前生成任务：取消 worker + 中断 ComfyUI + 清空队列。"""
        worker = self.generate_worker
        if not (worker and worker.isRunning()):
            return
        self.stop_generate_btn.setEnabled(False)
        self.stop_generate_btn.setText("停止中...")
        worker.cancel()
        worker.wait(5000)
        if worker.isRunning():
            worker.terminate()
            worker.wait(2000)
            self.on_generate_cancelled(getattr(worker, "_files", []))

    def stop_batch_generation(self):
        self.stop_generation()

    def _clear_batch_state_after_stop(self):
        self._batch_prompts = []
        self._batch_index = 0
        self._batch_total = 0
        self._batch_txt_path = ""
        self.config.pop("batch_txt_path", None)
        self.config.pop("batch_txt_index", None)
        self.save_from_ui()

    def on_generate_failed(self, msg):
        self._set_generation_idle()
        QMessageBox.warning(self, "生成失败", msg)
        self.log(msg)

    def on_thumb_clicked(self, item):
        self.show_image(item.data(Qt.UserRole))

    def show_image(self, path):
        self.big_preview.set_image(path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_responsive_layout()
        current = self.thumb_list.currentItem()
        if current:
            self.show_image(current.data(Qt.UserRole))

    def open_selected_image_dir(self):
        item = self.thumb_list.currentItem()
        if item:
            reveal_file(item.data(Qt.UserRole))
        else:
            open_output_dir(self.config.get("output_dir", OUTPUT_DIR))

    def _update_move_btn_state(self):
        """更新'移至封面素材库'按钮状态：有生成图片时可用，否则禁用"""
        existing = False
        src_dir = self.config.get("output_dir", OUTPUT_DIR)
        if os.path.isdir(src_dir):
            existing = any(
                f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp'))
                for f in os.listdir(src_dir)
            )
        self.move_to_cover_btn.setEnabled(existing)

    def move_to_cover_library(self):
        """一键将生成目录中的所有图片移动至批量混剪的封面素材目录"""
        src_dir = self.config.get("output_dir", OUTPUT_DIR)
        if not os.path.isdir(src_dir):
            QMessageBox.warning(self, "目录不存在", "生成图片目录不存在，请先生成图片。")
            return
        # 扫描所有图片文件
        images = sorted([
            f for f in os.listdir(src_dir)
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp'))
        ])
        if not images:
            QMessageBox.information(self, "无图片", "生成目录中没有图片文件，请先生成封面图。")
            self.move_to_cover_btn.setEnabled(False)
            return
        # 目标目录：批量混剪的封面图片素材目录
        dst_dir = workspace_path('素材', '封面图片')
        os.makedirs(dst_dir, exist_ok=True)
        # 移动前暂停按钮，显示进度
        self.move_to_cover_btn.setEnabled(False)
        self.move_to_cover_btn.setText("移动中...")
        QTimer.singleShot(50, lambda: self._do_move_images(src_dir, dst_dir, images))

    def _do_move_images(self, src_dir, dst_dir, images):
        """执行实际文件移动，处理重名冲突"""
        moved = 0
        skipped = 0
        renamed = 0
        total = len(images)
        for idx, filename in enumerate(images):
            src = os.path.join(src_dir, filename)
            dst = os.path.join(dst_dir, filename)
            # 重名处理：存在同名文件时自动加序号
            if os.path.exists(dst):
                base, ext = os.path.splitext(filename)
                counter = 1
                while True:
                    new_name = f"{base}_{counter}{ext}"
                    new_dst = os.path.join(dst_dir, new_name)
                    if not os.path.exists(new_dst):
                        dst = new_dst
                        break
                    counter += 1
                    if counter > 999:
                        self.log(f"跳过：{filename}（重名过多）")
                        skipped += 1
                        break
                else:
                    continue
                renamed += 1
            try:
                shutil.move(src, dst)
                moved += 1
            except Exception as exc:
                self.log(f"移动失败：{filename} → {exc}")
                skipped += 1
            # 进度提示
            if (idx + 1) % max(1, total // 5) == 0 or idx == total - 1:
                self.log(f"移动进度：{idx + 1}/{total}")
        # 完成反馈
        parts = [f"成功移动 {moved} 张"]
        if renamed:
            parts.append(f"{renamed} 张因重名自动重命名")
        if skipped:
            parts.append(f"跳过 {skipped} 张")
        self.log(f"✅ 移至封面素材库完成：{'，'.join(parts)} → {dst_dir}")
        QMessageBox.information(self, "移动完成",
            f"已将所有封面图移至批量混剪封面素材库。\n\n{'，'.join(parts)}\n目标目录：{dst_dir}")
        # 恢复按钮状态
        self.move_to_cover_btn.setText("移至封面素材库")
        self._update_move_btn_state()

    def _decode_log_bytes(self, raw):
        if not raw:
            return ""
        text = raw.decode("utf-8", errors="replace")
        if text.count("\ufffd") > 5:
            try:
                return raw.decode("gbk", errors="replace")
            except Exception:
                return text
        return text

    def _read_file_tail(self, path, max_chars=16000):
        if not path or not os.path.exists(path):
            return f"未找到：{path}"
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > max_chars * 4:
                    f.seek(max(0, size - max_chars * 4))
                raw = f.read()
            text = self._decode_log_bytes(raw).strip()
            if len(text) > max_chars:
                text = text[-max_chars:]
            return text or "文件存在，但内容为空。"
        except Exception as exc:
            return f"读取失败：{exc}"

    def _run_diagnostic_cmd(self, cmd, timeout=8):
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                startupinfo=hidden_startupinfo(),
            )
            output = (res.stdout or "").strip()
            return f"退出码：{res.returncode}\n{output or '无输出'}"
        except Exception as exc:
            return f"执行失败：{exc}"

    def _yes_no(self, value):
        return "是" if value else "否"

    def _diagnostic_path_checks(self, comfyui_path, python_exe, workflow_path, output_dir):
        checks = [
            ("ComfyUI 目录", comfyui_path),
            ("ComfyUI main.py", os.path.join(comfyui_path, "main.py")),
            ("ComfyUI Python", python_exe),
            ("工作流文件", workflow_path),
            ("输出目录", output_dir),
            ("日志目录", LOG_DIR),
            ("环境检测报告", ENV_REPORT_FILE),
            ("启动日志", STARTUP_LOG_FILE),
        ]
        return [f"- {name}：{self._yes_no(os.path.exists(path))}；{path}" for name, path in checks]

    def build_diagnostic_text(self):
        self.save_from_ui()
        comfyui_path = os.path.abspath(self.config.get("comfyui_path", ""))
        host = self.config.get("host", "127.0.0.1")
        port = int(self.config.get("port", 8188))
        workflow_path = self.workflow_combo.currentData() or self.config.get("workflow_path", "")
        output_dir = self.config.get("output_dir", OUTPUT_DIR)
        python_exe = find_python_executable(comfyui_path)
        extra_args = list(self.config.get("comfyui_args", []) or [])
        cmd = [
            python_exe,
            "main.py",
            "--listen",
            host,
            "--port",
            str(port),
            "--disable-all-custom-nodes",
        ] + [str(arg) for arg in extra_args]

        try:
            api_ok = is_api_available(host, port, timeout=2)
        except Exception:
            api_ok = False
        try:
            port_open = is_port_open(host, port, timeout=1)
        except Exception:
            port_open = False
        try:
            comfy_pids = find_comfyui_pids(comfyui_path, port)
        except Exception as exc:
            comfy_pids = [f"查询失败：{exc}"]
        try:
            listening_pids = find_listening_pids(port)
        except Exception as exc:
            listening_pids = [f"查询失败：{exc}"]

        ui_log = self.log_text.toPlainText().strip()
        ui_log_tail = "\n".join(ui_log.splitlines()[-60:]) if ui_log else "无"
        nvidia_info = self._run_diagnostic_cmd([
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ])
        try:
            hardware_profile = detect_hardware_profile(comfyui_path)
            hardware_lines = [
                profile_summary(hardware_profile),
                "显卡来源：" + str(hardware_profile.get("gpu", {}).get("source") or "未知"),
                "失败项：" + ("；".join(hardware_profile.get("failures", [])) or "无"),
                "提醒项：" + ("；".join(hardware_profile.get("warnings", [])) or "无"),
            ]
        except Exception as exc:
            hardware_lines = [f"硬件适配检测失败：{exc}"]

        lines = [
            "【牛爷爷 AI封面生成诊断信息】",
            f"复制时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"软件目录：{APP_ROOT}",
            f"当前 EXE/Python：{sys.executable}",
            f"系统信息：{platform.platform()}",
            "",
            "【ComfyUI 配置】",
            f"ComfyUI 路径：{comfyui_path}",
            f"ComfyUI Python：{python_exe}",
            f"工作流路径：{workflow_path}",
            f"输出目录：{output_dir}",
            f"监听地址：http://{host}:{port}",
            f"推荐启动参数：{' '.join(extra_args) or '默认'}",
            f"实际启动命令：{' '.join(cmd)}",
            "",
            "【实时状态】",
            f"API 是否可访问：{self._yes_no(api_ok)}",
            f"端口是否打开：{self._yes_no(port_open)}",
            f"匹配 ComfyUI PID：{', '.join(map(str, comfy_pids)) if comfy_pids else '无'}",
            f"监听端口 PID：{', '.join(map(str, listening_pids)) if listening_pids else '无'}",
            "",
            "【关键路径检查】",
            *self._diagnostic_path_checks(comfyui_path, python_exe, workflow_path, output_dir),
            "",
            "【显卡快速检测 nvidia-smi】",
            nvidia_info,
            "",
            "【AI 硬件适配检测】",
            *hardware_lines,
            "",
            "【界面运行日志尾部】",
            ui_log_tail,
            "",
            "【环境检测报告尾部】",
            self._read_file_tail(ENV_REPORT_FILE),
            "",
            "【ComfyUI 启动日志尾部】",
            self._read_file_tail(STARTUP_LOG_FILE),
            "",
            "【备注】",
            "如果环境检测通过但启动失败，优先看上面的 ComfyUI 启动日志尾部，通常会显示真实原因。",
        ]
        return "\n".join(lines)

    def copy_diagnostic_info(self):
        try:
            text = self.build_diagnostic_text()
            os.makedirs(os.path.dirname(DIAGNOSTIC_FILE), exist_ok=True)
            with open(DIAGNOSTIC_FILE, "w", encoding="utf-8") as f:
                f.write(text)
            QApplication.clipboard().setText(text)
            self.log(f"诊断信息已复制，并保存到：{DIAGNOSTIC_FILE}")
            QMessageBox.information(
                self,
                "诊断信息已复制",
                "已复制 AI封面生成诊断信息。\n\n请让客户直接粘贴发送给你，或发送日志目录里的 image_generate_diagnostic.txt。",
            )
        except Exception as exc:
            QMessageBox.warning(self, "复制失败", f"诊断信息生成失败：{exc}")

    def open_report(self):
        if not os.path.exists(ENV_REPORT_FILE):
            QMessageBox.information(self, "暂无报告", "还没有检测报告，请先点击[检测环境]。")
            return
        open_path(ENV_REPORT_FILE)

    def prepare_app_close(self):
        if self._app_close_prepared:
            return
        self._app_close_prepared = True
        manager = self.manager_holder.get("manager") or ComfyUIManager(
            self.config.get("comfyui_path", ""),
            self.config.get("host", "127.0.0.1"),
            self.config.get("port", 8188),
        )
        comfy_running = manager.is_running()

        # 如果正在生成，先取消任务和队列，但保留 ComfyUI 服务供短时间内复用。
        if self.generate_worker and self.generate_worker.isRunning():
            self._batch_prompts = []
            self.generate_worker.cancel()
            self.generate_worker.wait(2000)

        if comfy_running:
            self._write_shutdown_timestamp()
            self._schedule_delayed_comfyui_shutdown()
        else:
            self._remove_shutdown_timestamp()

    def closeEvent(self, event):
        self.prepare_app_close()
        super().closeEvent(event)

    def _write_shutdown_timestamp(self):
        """写入关闭时间戳，供下一次启动检查是否需要在超时后清理 ComfyUI。"""
        stamp_path = self._shutdown_stamp_path()
        try:
            os.makedirs(os.path.dirname(stamp_path), exist_ok=True)
            with open(stamp_path, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        return stamp_path

    def _shutdown_stamp_path(self):
        return os.path.join(WORKFLOW_DIR, ".comfyui_shutdown")

    def _remove_shutdown_timestamp(self):
        try:
            stamp_path = self._shutdown_stamp_path()
            if os.path.exists(stamp_path):
                os.remove(stamp_path)
        except Exception:
            pass

    def _schedule_delayed_comfyui_shutdown(self):
        """关闭软件后保活 30 分钟；若期间未重新打开软件，则后台清理 ComfyUI。"""
        try:
            helper = os.path.join(os.path.dirname(__file__), "comfy_shutdown_helper.py")
            if not os.path.exists(helper):
                return
            python_exe = find_python_executable(self.config.get("comfyui_path", ""))
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen(
                [
                    python_exe,
                    helper,
                    self._shutdown_stamp_path(),
                    self.config.get("comfyui_path", ""),
                    self.config.get("host", "127.0.0.1"),
                    str(self.config.get("port", 8188)),
                    str(COMFY_KEEPALIVE_SECONDS),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(),
                creationflags=flags,
            )
        except Exception as exc:
            self.log(f"ComfyUI 延迟清理任务启动失败：{exc}")

    def _check_shutdown_cleanup(self):
        """启动时检查：如果上次关闭超过 30 分钟，清理残留的 ComfyUI 进程。"""
        try:
            stamp_path = self._shutdown_stamp_path()
            if not os.path.exists(stamp_path):
                return
            with open(stamp_path, "r", encoding="utf-8") as f:
                last_shutdown = float(f.read().strip())
            elapsed = time.time() - last_shutdown
            if elapsed > COMFY_KEEPALIVE_SECONDS:
                manager = ComfyUIManager(
                    self.config.get("comfyui_path", ""),
                    self.config.get("host", "127.0.0.1"),
                    self.config.get("port", 8188)
                )
                if manager.is_running():
                    self.log("检测到 ComfyUI 已在后台保活超过 30 分钟，自动停止。")
                    manager.stop()
            else:
                minutes = max(0, int((COMFY_KEEPALIVE_SECONDS - elapsed) // 60))
                self.log(f"检测到上次关闭后仍在 30 分钟保活期内，继续复用 ComfyUI（剩余约 {minutes} 分钟）。")
            self._remove_shutdown_timestamp()
        except Exception:
            pass
