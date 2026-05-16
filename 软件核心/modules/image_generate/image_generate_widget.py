import os
import random

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (
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
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .comfy_manager import ComfyUIManager
from .config import ENV_REPORT_FILE, OUTPUT_DIR, WORKFLOW_DIR, load_config, save_config
from .hardware_profile import detect_hardware_profile, profile_summary
from .image_tools import open_output_dir, reveal_file
from .task_worker import ComfyStartWorker, EnvCheckWorker, ImageGenerateWorker
from .workflow_manager import copy_workflow_to_local, scan_zimage_workflows


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
        self._build_ui()
        self.load_to_ui()
        self.refresh_workflows(copy_first=True)
        self.refresh_engine_state()

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
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("AI商品图生成")
        title.setObjectName("pageTitle")
        sub = QLabel("复用本机 ComfyUI + Z-Image 工作流，软件内完成商品图生成。")
        sub.setObjectName("mutedText")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        header.addLayout(title_box, 1)
        self.status_label = QLabel("当前状态：未检测")
        self.status_label.setObjectName("statusPill")
        header.addWidget(self.status_label)
        root.addLayout(header)

        body_widget = QWidget()
        body_widget.setObjectName("imageGenerateBody")
        body_widget.setStyleSheet("QWidget#imageGenerateBody{background:#0B1120;border:none;}")
        body = QHBoxLayout(body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(14)
        left_panel = QVBoxLayout()
        left_panel.setSpacing(12)
        left_panel.addWidget(self._build_top_group())
        left_panel.addWidget(self._build_param_group())
        left_panel.addWidget(self._build_env_table(), 1)
        body.addLayout(left_panel, 2)
        body.addWidget(self._build_preview_group(), 3)

        scroll = QScrollArea()
        scroll.setObjectName("imageGenerateScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet("QScrollArea#imageGenerateScroll{background:#0B1120;border:none;}")
        scroll.viewport().setStyleSheet("background:#0B1120;border:none;")
        scroll.setWidget(body_widget)
        root.addWidget(scroll, 1)

        log_group = QGroupBox("运行日志")
        log_group.setObjectName("imageCard")
        self._apply_card_style(log_group)
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setObjectName("logConsole")
        self.log_text.setMinimumHeight(86)
        self.log_text.setMaximumHeight(130)
        log_layout.addWidget(self.log_text)
        root.addWidget(log_group)

    def _build_top_group(self):
        group = QGroupBox("ComfyUI 后台引擎")
        group.setObjectName("imageCard")
        self._apply_card_style(group)
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        layout.setColumnStretch(1, 1)
        self.comfy_path_edit = QLineEdit()
        self.comfy_path_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.workflow_combo = QComboBox()
        self.workflow_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.auto_start_chk = QCheckBox("生成前自动启动 ComfyUI")
        self.detect_btn = QPushButton("检测环境")
        self.choose_btn = QPushButton("选择 ComfyUI 路径")
        self.start_btn = QPushButton("启动 ComfyUI")
        self.stop_btn = QPushButton("停止 ComfyUI")
        self.open_output_btn = QPushButton("打开输出目录")
        self.report_btn = QPushButton("打开检测报告")
        for btn in (self.detect_btn, self.choose_btn, self.open_output_btn, self.report_btn):
            btn.setObjectName("imageActionButton")
            btn.setMinimumHeight(36)
        self.start_btn.setObjectName("imagePrimaryButton")
        self.stop_btn.setObjectName("imageDangerButton")
        self.start_btn.setMinimumHeight(36)
        self.stop_btn.setMinimumHeight(36)
        self.engine_progress = QProgressBar()
        self.engine_progress.setVisible(False)
        self.engine_hint = QLabel("ComfyUI 未启动")
        self.engine_hint.setObjectName("mutedText")
        self.engine_hint.setWordWrap(True)
        self.profile_label = QLabel("当前适配档位：未检测")
        self.profile_label.setObjectName("warningText")
        self.profile_label.setWordWrap(True)

        self.detect_btn.clicked.connect(self.run_env_check)
        self.choose_btn.clicked.connect(self.choose_comfy_path)
        self.start_btn.clicked.connect(self.start_comfyui)
        self.stop_btn.clicked.connect(self.stop_comfyui)
        self.open_output_btn.clicked.connect(lambda: open_output_dir(self.config.get("output_dir", OUTPUT_DIR)))
        self.report_btn.clicked.connect(self.open_report)
        self.workflow_combo.currentIndexChanged.connect(self.on_workflow_changed)
        self.auto_start_chk.stateChanged.connect(self.save_from_ui)

        layout.addWidget(QLabel("ComfyUI 路径"), 0, 0)
        layout.addWidget(self.comfy_path_edit, 0, 1, 1, 3)
        layout.addWidget(self.choose_btn, 0, 4)
        layout.addWidget(QLabel("Z-Image 工作流"), 1, 0)
        layout.addWidget(self.workflow_combo, 1, 1, 1, 3)
        layout.addWidget(self.auto_start_chk, 1, 4)
        layout.addWidget(self.detect_btn, 2, 0)
        layout.addWidget(self.start_btn, 2, 1)
        layout.addWidget(self.stop_btn, 2, 2)
        layout.addWidget(self.open_output_btn, 2, 3)
        layout.addWidget(self.report_btn, 2, 4)
        layout.addWidget(self.engine_hint, 3, 0, 1, 2)
        layout.addWidget(self.engine_progress, 3, 2, 1, 3)
        layout.addWidget(self.profile_label, 4, 0, 1, 5)
        return group

    def _build_param_group(self):
        params = QGroupBox("生成参数")
        params.setObjectName("imageCard")
        self._apply_card_style(params)
        form = QGridLayout(params)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(9)
        form.setColumnStretch(1, 1)
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText("例如：电商商品主图，白底棚拍，高级质感，柔和布光，产品清晰，适合直播间")
        self.prompt_edit.setMinimumHeight(150)
        self.prompt_edit.setMaximumHeight(190)
        self.width_spin = QSpinBox(); self.width_spin.setRange(512, 2048); self.width_spin.setSingleStep(20); self.width_spin.setValue(900)
        self.height_spin = QSpinBox(); self.height_spin.setRange(512, 2048); self.height_spin.setSingleStep(20); self.height_spin.setValue(1600)
        self.steps_spin = QSpinBox(); self.steps_spin.setRange(1, 80); self.steps_spin.setValue(4)
        self.cfg_spin = QDoubleSpinBox(); self.cfg_spin.setRange(0.1, 20.0); self.cfg_spin.setSingleStep(0.1); self.cfg_spin.setValue(1.0)
        self.seed_spin = QSpinBox(); self.seed_spin.setRange(0, 2147483647); self.seed_spin.setValue(0)
        self.count_spin = QSpinBox(); self.count_spin.setRange(1, 8); self.count_spin.setValue(1)
        self.count_spin.setToolTip("多张会按单张循环提交，不使用 ComfyUI 批量 batch，避免 20 系显卡生成糊图。")
        random_seed_btn = QPushButton("随机种子")
        random_seed_btn.setObjectName("imageActionButton")
        random_seed_btn.setMinimumHeight(34)
        random_seed_btn.clicked.connect(lambda: self.seed_spin.setValue(random.randint(1, 2147483647)))
        self.generate_btn = QPushButton("生成商品图")
        self.generate_btn.setObjectName("imageGenerateButton")
        self.generate_btn.setMinimumHeight(46)
        self.generate_btn.clicked.connect(self.generate_images)
        self.progress = QProgressBar()

        form.addWidget(QLabel("正向提示词"), 0, 0)
        form.addWidget(self.prompt_edit, 0, 1, 2, 3)
        form.addWidget(QLabel("宽度"), 2, 0); form.addWidget(self.width_spin, 2, 1)
        form.addWidget(QLabel("高度"), 2, 2); form.addWidget(self.height_spin, 2, 3)
        form.addWidget(QLabel("步数"), 3, 0); form.addWidget(self.steps_spin, 3, 1)
        form.addWidget(QLabel("CFG"), 3, 2); form.addWidget(self.cfg_spin, 3, 3)
        form.addWidget(QLabel("种子"), 4, 0); form.addWidget(self.seed_spin, 4, 1)
        form.addWidget(random_seed_btn, 4, 2)
        form.addWidget(QLabel("张数"), 5, 0); form.addWidget(self.count_spin, 5, 1)
        count_hint = QLabel("多张会逐张生成，更稳但耗时会按张数增加")
        count_hint.setObjectName("appSubtitle")
        form.addWidget(count_hint, 5, 2, 1, 2)
        form.addWidget(self.generate_btn, 6, 1)
        form.addWidget(self.progress, 6, 2, 1, 2)
        return params

    def _build_preview_group(self):
        preview = QGroupBox("生成预览")
        preview.setObjectName("imagePreviewGroup")
        self._apply_card_style(preview)
        preview_layout = QVBoxLayout(preview)
        preview_layout.setSpacing(10)
        self.big_preview = AspectPreviewLabel("生成完成后在这里预览")
        self.big_preview.setObjectName("imageBigPreview")
        self.big_preview.setMinimumSize(420, 620)
        self.big_preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.big_preview.setStyleSheet("")
        self.thumb_list = QListWidget()
        self.thumb_list.setMaximumHeight(118)
        self.thumb_list.itemClicked.connect(self.on_thumb_clicked)
        open_file_btn = QPushButton("打开当前图片目录")
        open_file_btn.setObjectName("imageActionButton")
        open_file_btn.setMinimumHeight(38)
        open_file_btn.clicked.connect(self.open_selected_image_dir)
        preview_layout.addWidget(self.big_preview, 1)
        preview_layout.addWidget(self.thumb_list)
        preview_layout.addWidget(open_file_btn)
        return preview

    def _build_env_table(self):
        group = QGroupBox("环境检测结果")
        group.setObjectName("imageCard")
        self._apply_card_style(group)
        layout = QVBoxLayout(group)
        self.env_table = QTableWidget(0, 4)
        self.env_table.setHorizontalHeaderLabels(["项目", "状态", "检测值", "客户提示"])
        self.env_table.horizontalHeader().setStretchLastSection(True)
        self.env_table.verticalHeader().setVisible(False)
        self.env_table.setMinimumHeight(110)
        self.env_table.setMaximumHeight(180)
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
                "hint": "ComfyUI 未启动，点击“启动 ComfyUI”后等待后台引擎就绪。",
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
            self.start_btn.setText("启动 ComfyUI")
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
            self.count_spin.setValue(profile["image_count"])
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
        self.set_engine_state("error")
        QMessageBox.warning(self, "启动失败", msg)
        self.log(msg)

    def stop_comfyui(self):
        manager = self.manager_holder.get("manager")
        if not manager:
            manager = ComfyUIManager(self.config["comfyui_path"], self.config["host"], self.config["port"])
        ok, msg = manager.stop()
        self.log(msg)
        if ok:
            self.manager_holder.pop("manager", None)
            self.set_engine_state("stopped")
        else:
            self.refresh_engine_state()
        if not ok:
            QMessageBox.information(self, "停止 ComfyUI", msg)

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
            QMessageBox.warning(self, "ComfyUI 未启动", "请先点击“启动 ComfyUI”，或确认后台 API 可访问。")
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
        self.progress.setValue(0)
        self.generate_worker = ImageGenerateWorker(self.config.copy(), params)
        self.generate_worker.progress.connect(self.progress.setValue)
        self.generate_worker.message.connect(self.log)
        self.generate_worker.finished.connect(self.on_generate_finished)
        self.generate_worker.failed.connect(self.on_generate_failed)
        self.generate_worker.start()

    def on_generate_finished(self, files):
        self.generate_btn.setEnabled(True)
        self.generated_files = list(files)
        self.thumb_list.clear()
        for path in self.generated_files:
            pix = QPixmap(path)
            icon = QIcon(pix.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            item = QListWidgetItem(icon, os.path.basename(path))
            item.setData(Qt.UserRole, path)
            self.thumb_list.addItem(item)
        if self.generated_files:
            self.show_image(self.generated_files[0])
        self.log(f"生成完成：{len(self.generated_files)} 张图片")

    def on_generate_failed(self, msg):
        self.generate_btn.setEnabled(True)
        QMessageBox.warning(self, "生成失败", msg)
        self.log(msg)

    def on_thumb_clicked(self, item):
        self.show_image(item.data(Qt.UserRole))

    def show_image(self, path):
        self.big_preview.set_image(path)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        current = self.thumb_list.currentItem()
        if current:
            self.show_image(current.data(Qt.UserRole))

    def open_selected_image_dir(self):
        item = self.thumb_list.currentItem()
        if item:
            reveal_file(item.data(Qt.UserRole))
        else:
            open_output_dir(self.config.get("output_dir", OUTPUT_DIR))

    def open_report(self):
        if not os.path.exists(ENV_REPORT_FILE):
            QMessageBox.information(self, "暂无报告", "还没有检测报告，请先点击“检测环境”。")
            return
        os.startfile(os.path.abspath(ENV_REPORT_FILE))

    def closeEvent(self, event):
        manager = self.manager_holder.get("manager")
        if manager:
            manager.stop()
        super().closeEvent(event)
