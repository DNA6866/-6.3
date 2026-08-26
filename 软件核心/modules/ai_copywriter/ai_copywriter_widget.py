import json
import os

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from paths import workspace_path
from services.glm_service import (
    GLMClient,
    GLM_MODELS,
    GLMRequestConfig,
    MODE_LABELS,
    build_user_prompt,
)


CONFIG_PATH = workspace_path("配置", "glm_plugin.json")


class GLMWorker(QThread):
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, config, mode, prompt, parent=None):
        super().__init__(parent)
        self.config = config
        self.mode = mode
        self.prompt = prompt

    def run(self):
        try:
            result = GLMClient(self.config).generate(self.mode, self.prompt)
            if not self.isInterruptionRequested():
                self.completed.emit(result)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


class AICopywriterWidget(QWidget):
    log_signal = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = None
        self.config = self._load_config()
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        title = QLabel("AI文案助手")
        title.setObjectName("pageTitle")
        subtitle = QLabel("可选联网插件：口播文案、字幕润色、封面标题和随机卖点共用一个智谱 API Key。")
        subtitle.setObjectName("appSubtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        settings = QGroupBox("智谱 GLM 设置")
        settings_layout = QGridLayout(settings)
        self.api_key_edit = QLineEdit(self.config.get("api_key", ""))
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("在智谱开放平台创建并填写 API Key")
        self.show_key_chk = QCheckBox("显示")
        self.show_key_chk.toggled.connect(
            lambda checked: self.api_key_edit.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        self.model_combo = QComboBox()
        self.model_combo.addItems(GLM_MODELS)
        self.model_combo.setCurrentText(self.config.get("model", GLM_MODELS[0]))
        save_btn = QPushButton("保存设置")
        save_btn.clicked.connect(self._save_config)
        settings_layout.addWidget(QLabel("API Key"), 0, 0)
        settings_layout.addWidget(self.api_key_edit, 0, 1)
        settings_layout.addWidget(self.show_key_chk, 0, 2)
        settings_layout.addWidget(QLabel("模型"), 1, 0)
        settings_layout.addWidget(self.model_combo, 1, 1)
        settings_layout.addWidget(save_btn, 1, 2)
        settings_layout.setColumnStretch(1, 1)
        root.addWidget(settings)

        body = QFrame()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)

        input_group = QGroupBox("输入区")
        input_layout = QVBoxLayout(input_group)
        form = QFormLayout()
        self.mode_combo = QComboBox()
        for key, label in MODE_LABELS.items():
            self.mode_combo.addItem(label, key)
        self.product_edit = QLineEdit()
        self.product_edit.setPlaceholderText("例如：冰丝防晒袖套")
        self.audience_edit = QLineEdit()
        self.audience_edit.setPlaceholderText("例如：夏季通勤女性")
        self.style_combo = QComboBox()
        self.style_combo.addItems(["自然口语", "直接成交", "真实测评", "轻松种草", "专业讲解"])
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 30)
        self.count_spin.setValue(6)
        form.addRow("用途", self.mode_combo)
        form.addRow("产品名称", self.product_edit)
        form.addRow("目标人群", self.audience_edit)
        form.addRow("表达风格", self.style_combo)
        form.addRow("生成数量", self.count_spin)
        input_layout.addLayout(form)

        source_label = QLabel("产品资料 / 待处理文本（必填）")
        source_label.setStyleSheet("color:#F8FAFC;font-weight:800;")
        input_layout.addWidget(source_label)
        self.source_edit = QTextEdit()
        self.source_edit.setPlaceholderText(
            "【必填】填写真实产品资料、已有口播稿或待润色字幕。\n"
            "资料越具体，生成结果越可靠。此文本框为空时无法生成。"
        )
        input_layout.addWidget(self.source_edit, 1)
        action_row = QHBoxLayout()
        self.generate_btn = QPushButton("开始生成")
        self.generate_btn.clicked.connect(self.start_generate)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_generate)
        action_row.addWidget(self.generate_btn)
        action_row.addWidget(self.stop_btn)
        input_layout.addLayout(action_row)

        output_group = QGroupBox("结果区")
        output_layout = QVBoxLayout(output_group)
        self.output_edit = QTextEdit()
        self.output_edit.setPlaceholderText("生成结果会显示在这里")
        output_layout.addWidget(self.output_edit, 1)
        output_actions = QHBoxLayout()
        copy_btn = QPushButton("复制结果")
        copy_btn.clicked.connect(self.copy_result)
        apply_btn = QPushButton("应用到当前功能")
        apply_btn.clicked.connect(self.apply_result)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.output_edit.clear)
        output_actions.addWidget(copy_btn)
        output_actions.addWidget(apply_btn)
        output_actions.addWidget(clear_btn)
        output_layout.addLayout(output_actions)

        body_layout.addWidget(input_group, 1)
        body_layout.addWidget(output_group, 1)
        root.addWidget(body, 1)

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_config(self):
        data = {
            "api_key": self.api_key_edit.text().strip(),
            "model": self.model_combo.currentText(),
        }
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            self.config = data
            self.log_signal.emit("[AI文案助手] 智谱设置已保存到用户工作区。")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def start_generate(self):
        if self.worker and self.worker.isRunning():
            return
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            QMessageBox.information(self, "需要 API Key", "请先填写智谱 API Key。")
            return
        mode = self.mode_combo.currentData()
        # 前置校验：精确指出缺失字段
        source_text = self.source_edit.toPlainText().strip()
        missing = []
        if not source_text:
            missing.append("「产品资料/待处理文本」（输入区下方的大文本框）")
        if missing:
            QMessageBox.information(
                self,
                "输入不完整",
                "以下必填项为空，请补填后再生成：\n\n"
                + "\n".join(f"  • {item}" for item in missing)
                + "\n\n其余字段（产品名称、目标人群、风格等）均可选。",
            )
            self.source_edit.setFocus()
            return
        try:
            prompt = build_user_prompt(
                mode,
                source_text,
                self.product_edit.text(),
                self.audience_edit.text(),
                self.style_combo.currentText(),
                self.count_spin.value(),
            )
        except Exception as exc:
            QMessageBox.information(self, "输入不完整", str(exc))
            return
        self._save_config()
        config = GLMRequestConfig(
            api_key=api_key,
            model=self.model_combo.currentText(),
        )
        self.generate_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.output_edit.setPlainText("正在生成，请稍等...")
        self.worker = GLMWorker(config, mode, prompt, self)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()
        self.log_signal.emit(f"[AI文案助手] 开始{MODE_LABELS.get(mode, '文案生成')}。")

    def stop_generate(self):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.output_edit.setPlainText("已停止等待本次结果。")
        self._on_finished()

    def _on_completed(self, text):
        self.output_edit.setPlainText(text)
        self.log_signal.emit("[AI文案助手] 文案生成完成。")

    def _on_failed(self, message):
        self.output_edit.setPlainText("生成失败：" + message)
        self.log_signal.emit("[AI文案助手] 生成失败：" + message)

    def _on_finished(self):
        self.generate_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def copy_result(self):
        text = self.output_edit.toPlainText().strip()
        if text and not text.startswith("正在生成"):
            QApplication.clipboard().setText(text)
            self.log_signal.emit("[AI文案助手] 结果已复制。")

    def apply_result(self):
        text = self.output_edit.toPlainText().strip()
        if not text or text.startswith(("正在生成", "生成失败", "已停止")):
            return
        main_window = self.window()
        mode = self.mode_combo.currentData()
        if mode == "selling_points" and hasattr(main_window, "wm_text_pool"):
            main_window.wm_text_pool.setPlainText(text)
            main_window.wm_use_text_pool.setChecked(True)
            self.log_signal.emit("[AI文案助手] 已写入文字水印随机卖点池。")
            return
        if mode == "cover_title" and hasattr(main_window, "cover_text_input"):
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), text)
            main_window.cover_text_input.setText(first_line)
            self.log_signal.emit("[AI文案助手] 第一条标题已写入封面标题。")
            return
        QApplication.clipboard().setText(text)
        self.log_signal.emit("[AI文案助手] 当前结果已复制，可粘贴到目标输入框。")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            if not self.worker.wait(1500):
                self.worker.terminate()
                self.worker.wait(1000)
        super().closeEvent(event)
