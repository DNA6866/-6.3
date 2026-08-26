import os
import time
import uuid

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from paths import tools_dir
from services.platform_service import open_path, open_url
from ui_components import configure_spinbox_for_direct_input
from .config import OUTPUT_DIR, load_config, save_config
from .live_parser import select_stream_format
from .task_worker import LiveDownloadWorker, LiveParseWorker


QUALITY_OPTIONS = [
    ("蓝光/最高", "best"),
    ("超清", "high"),
    ("原画/中档", "medium"),
    ("高清", "low"),
    ("标清/最低", "lowest"),
]


class AddLiveRoomDialog(QDialog):
    def __init__(self, parent=None, room=None, default_quality="best"):
        super().__init__(parent)
        self.setWindowTitle("直播间配置")
        self.resize(560, 360)
        room = room or {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.url_edit = QTextEdit()
        self.url_edit.setPlaceholderText("粘贴抖音/快手手机分享出来的直播间文本或链接")
        self.url_edit.setMinimumHeight(120)
        self.url_edit.setPlainText(room.get("url", ""))

        self.note_edit = QTextEdit()
        self.note_edit.setPlaceholderText("例如：优品甄选、女装一号直播间")
        self.note_edit.setMaximumHeight(64)
        self.note_edit.setPlainText(room.get("note", ""))

        self.quality_combo = QComboBox()
        for label, value in QUALITY_OPTIONS:
            self.quality_combo.addItem(label, value)
        quality = room.get("quality") or default_quality
        index = self.quality_combo.findData(quality)
        if index >= 0:
            self.quality_combo.setCurrentIndex(index)

        self.monitor_check = QCheckBox("添加后开启监控（只看开播）")
        self.monitor_check.setToolTip("监控只检测直播间是否开播，不会自动开始录制。")
        self.monitor_check.setChecked(bool(room.get("monitor_enabled", False)))

        form.addRow("直播链接", self.url_edit)
        form.addRow("主播备注", self.note_edit)
        form.addRow("默认清晰度", self.quality_combo)
        form.addRow("", self.monitor_check)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        configure_spinbox_for_direct_input(self)

    def get_data(self):
        return {
            "url": self.url_edit.toPlainText().strip(),
            "note": self.note_edit.toPlainText().strip(),
            "quality": self.quality_combo.currentData(),
            "monitor_enabled": self.monitor_check.isChecked(),
        }


class LiveSettingsDialog(QDialog):
    def __init__(self, parent=None, interval=30, quality="best"):
        super().__init__(parent)
        self.setWindowTitle("全局设置")
        self.resize(360, 180)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(10, 600)
        self.interval_spin.setValue(int(interval or 30))
        self.interval_spin.setSuffix(" 秒")

        self.quality_combo = QComboBox()
        for label, value in QUALITY_OPTIONS:
            self.quality_combo.addItem(label, value)
        index = self.quality_combo.findData(quality or "best")
        if index >= 0:
            self.quality_combo.setCurrentIndex(index)

        form.addRow("监控间隔", self.interval_spin)
        form.addRow("默认清晰度", self.quality_combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        configure_spinbox_for_direct_input(self)

    def get_data(self):
        return {
            "monitor_interval": self.interval_spin.value(),
            "prefer_quality": self.quality_combo.currentData(),
        }


class LiveDownloaderWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = load_config()
        self.rooms = self.config.get("rooms") or []
        self.parse_workers = {}
        self.download_workers = {}
        self.record_after_parse = {}
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self.monitor_once)
        self._normalize_rooms()
        self.save_state()
        self._build_ui()
        configure_spinbox_for_direct_input(self)
        self.refresh_table()
        self.update_monitor_timer()

    def _normalize_rooms(self):
        if not self.rooms and self.config.get("last_url"):
            self.rooms.append({
                "id": uuid.uuid4().hex,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "url": self.config.get("last_url", ""),
                "note": "",
                "quality": self.config.get("prefer_quality", "best"),
                "status": "从旧配置导入，等待刷新",
                "monitor_enabled": False,
                "pinned": False,
                "formats": [],
            })
        for room in self.rooms:
            room.setdefault("id", uuid.uuid4().hex)
            room.setdefault("url", "")
            room.setdefault("note", "")
            room.setdefault("quality", self.config.get("prefer_quality", "best"))
            room.setdefault("platform", "")
            room.setdefault("title", "")
            room.setdefault("uploader", "")
            room.setdefault("status", "未开始监控/录制结束")
            room.setdefault("monitor_enabled", False)
            room.setdefault("pinned", False)
            room.setdefault("formats", [])
            # 监控和录制属于运行态，重启软件后不继承，避免打开软件就意外开录。
            if room.get("monitor_enabled") or room.get("status") in ("录制中", "正在停止录制", "监控中", "正在解析直播间"):
                room["monitor_enabled"] = False
                room["status"] = "未开始监控/录制结束"

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)

        toolbar = QHBoxLayout()
        title = QLabel("直播复盘录制工具")
        title.setObjectName("pageTitle")
        toolbar.addWidget(title, 1)

        self.add_btn = QPushButton("+ 添加监控")
        self.settings_btn = QPushButton("全局设置")
        self.open_output_btn = QPushButton("打开保存目录")
        self.add_btn.setObjectName("toolbarPrimaryButton")
        self.add_btn.setMinimumHeight(34)
        self.settings_btn.setMinimumHeight(34)
        self.open_output_btn.setMinimumHeight(34)
        self.add_btn.clicked.connect(self.add_room)
        self.settings_btn.clicked.connect(self.open_settings)
        self.open_output_btn.clicked.connect(self.open_output_dir)
        toolbar.addWidget(self.add_btn)
        toolbar.addWidget(self.settings_btn)
        toolbar.addWidget(self.open_output_btn)
        root.addLayout(toolbar)

        self.table = QTableWidget(0, 7)
        self.table.setObjectName("liveRoomTable")
        self.table.setHorizontalHeaderLabels([
            "序号",
            "平台",
            "主播名/备注（点击可在浏览器中打开直播间）",
            "当前状态",
            "监控",
            "录制",
            "编辑",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(True)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 56)
        self.table.setColumnWidth(1, 72)
        self.table.setColumnWidth(3, 190)
        self.table.setColumnWidth(4, 120)
        self.table.setColumnWidth(5, 180)
        self.table.setColumnWidth(6, 300)
        self.table.cellDoubleClicked.connect(self.open_room_from_cell)
        root.addWidget(self.table, 1)

        self.log_text = QTextEdit()
        self.log_text.setObjectName("logConsole")
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(130)
        root.addWidget(self.log_text)

        warning = QLabel("请确保你拥有直播内容的录制和使用权。仅支持公开且可访问的直播间，不提供绕过登录、付费、权限或平台风控的能力。")
        warning.setObjectName("warningText")
        warning.setWordWrap(True)
        root.addWidget(warning)

    def save_state(self):
        # 直播流地址通常带有效期 token，只保存直播间配置，不把临时播放地址写入配置文件。
        persisted_rooms = []
        for room in self.rooms:
            item = room.copy()
            item["formats"] = []
            persisted_rooms.append(item)
        self.config["rooms"] = persisted_rooms
        save_config(self.config)

    def log(self, text):
        self.log_text.append(f"{time.strftime('%H:%M:%S')}  {text}")

    def find_room(self, room_id):
        for index, room in enumerate(self.rooms):
            if room.get("id") == room_id:
                return index, room
        return -1, None

    def sorted_rooms(self):
        return sorted(self.rooms, key=lambda item: (not item.get("pinned", False), item.get("created_at", "")))

    def refresh_table(self):
        self.rooms = self.sorted_rooms()
        self.table.setRowCount(len(self.rooms))
        for row_index, room in enumerate(self.rooms):
            self.table.setRowHeight(row_index, 64)
            self.table.setItem(row_index, 0, self._center_item(str(row_index + 1)))
            self.table.setItem(row_index, 1, self._center_item(self.platform_badge(room)))
            self.table.setCellWidget(row_index, 2, self._build_name_widget(room))
            self.table.setCellWidget(row_index, 3, self._build_status_widget(room))
            self.table.setCellWidget(row_index, 4, self._build_monitor_widget(room))
            self.table.setCellWidget(row_index, 5, self._build_record_widget(room))
            self.table.setCellWidget(row_index, 6, self._build_edit_widget(room))

    def _center_item(self, text):
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        return item

    def platform_badge(self, room):
        platform = room.get("platform") or ""
        if "快手" in platform:
            return "快"
        if "抖音" in platform:
            return "抖"
        return "-"

    def _build_name_widget(self, room):
        box = QVBoxLayout()
        box.setContentsMargins(10, 4, 10, 4)
        box.setSpacing(2)
        widget = QWidget()
        widget.setLayout(box)

        name = room.get("note") or room.get("uploader") or room.get("title") or "未命名直播间"
        button = QPushButton(name)
        button.setObjectName("linkButton")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(lambda checked=False, rid=room["id"]: self.open_room(rid))
        detail = QLabel(room.get("expanded_url") or room.get("url") or "未配置链接")
        detail.setObjectName("mutedText")
        detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail.setWordWrap(False)
        box.addWidget(button)
        box.addWidget(detail)
        return widget

    def _build_status_widget(self, room):
        layout = QHBoxLayout()
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(8)
        widget = QWidget()
        widget.setLayout(layout)

        dot = QLabel("●")
        status = room.get("status") or "未开始监控/录制结束"
        if "录制中" in status:
            dot.setStyleSheet("color:#22C55E;")
        elif "失败" in status or "异常" in status:
            dot.setStyleSheet("color:#EF4444;")
        elif "监控" in status or "解析" in status:
            dot.setStyleSheet("color:#F59E0B;")
        else:
            dot.setStyleSheet("color:#94A3B8;")
        label = QLabel(status)
        label.setWordWrap(True)
        layout.addWidget(dot)
        layout.addWidget(label, 1)
        return widget

    def _build_monitor_widget(self, room):
        box = QHBoxLayout()
        box.setContentsMargins(8, 0, 8, 0)
        widget = QWidget()
        widget.setLayout(box)
        button = QPushButton("监控开" if room.get("monitor_enabled") else "监控关")
        button.setCheckable(True)
        button.setChecked(bool(room.get("monitor_enabled")))
        button.setProperty("monitorSwitch", "on" if room.get("monitor_enabled") else "off")
        button.clicked.connect(lambda checked=False, rid=room["id"]: self.toggle_room_monitor(rid))
        box.addWidget(button)
        return widget

    def _build_record_widget(self, room):
        box = QHBoxLayout()
        box.setContentsMargins(8, 0, 8, 0)
        box.setSpacing(6)
        widget = QWidget()
        widget.setLayout(box)
        is_recording = self.is_room_recording(room["id"])
        stopping = room.get("status") == "正在停止录制"

        start_btn = QPushButton("开始录制")
        stop_btn = QPushButton("正在停止..." if stopping else "停止录制")
        start_btn.setObjectName("linkButton")
        stop_btn.setObjectName("dangerLinkButton")
        start_btn.setEnabled(not is_recording and not stopping)
        stop_btn.setEnabled(is_recording and not stopping)
        start_btn.clicked.connect(lambda checked=False, rid=room["id"]: self.start_manual_record(rid))
        stop_btn.clicked.connect(lambda checked=False, rid=room["id"]: self.stop_record(rid))
        box.addWidget(start_btn)
        box.addWidget(stop_btn)
        return widget

    def _build_edit_widget(self, room):
        layout = QHBoxLayout()
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(6)
        widget = QWidget()
        widget.setLayout(layout)

        pin_btn = QPushButton("置顶" if not room.get("pinned") else "取消置顶")
        edit_btn = QPushButton("修改配置")
        delete_btn = QPushButton("删除")
        pin_btn.setObjectName("linkButton")
        edit_btn.setObjectName("linkButton")
        delete_btn.setObjectName("dangerLinkButton")
        pin_btn.clicked.connect(lambda checked=False, rid=room["id"]: self.toggle_pin(rid))
        edit_btn.clicked.connect(lambda checked=False, rid=room["id"]: self.edit_room(rid))
        delete_btn.clicked.connect(lambda checked=False, rid=room["id"]: self.delete_room(rid))
        layout.addWidget(pin_btn)
        layout.addWidget(edit_btn)
        layout.addWidget(delete_btn)
        return widget

    def add_room(self):
        dialog = AddLiveRoomDialog(self, default_quality=self.config.get("prefer_quality", "best"))
        if dialog.exec_() != QDialog.Accepted:
            return
        data = dialog.get_data()
        if not data["url"]:
            QMessageBox.warning(self, "缺少链接", "请先粘贴直播间分享链接。")
            return
        room = {
            "id": uuid.uuid4().hex,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "url": data["url"],
            "note": data["note"],
            "quality": data["quality"],
            "platform": "",
            "title": "",
            "uploader": "",
            "status": "未开始监控/录制结束",
            "monitor_enabled": data["monitor_enabled"],
            "pinned": False,
            "formats": [],
        }
        self.rooms.append(room)
        self.save_state()
        self.refresh_table()
        self.update_monitor_timer()
        self.log(f"已添加直播间：{room.get('note') or '未命名直播间'}")
        self.start_parse(room["id"], record_after=False)

    def edit_room(self, room_id):
        index, room = self.find_room(room_id)
        if not room:
            return
        dialog = AddLiveRoomDialog(self, room=room, default_quality=self.config.get("prefer_quality", "best"))
        if dialog.exec_() != QDialog.Accepted:
            return
        data = dialog.get_data()
        if not data["url"]:
            QMessageBox.warning(self, "缺少链接", "直播间链接不能为空。")
            return
        room.update(data)
        room["status"] = "配置已更新，等待刷新"
        room["formats"] = []
        self.rooms[index] = room
        self.save_state()
        self.refresh_table()
        self.update_monitor_timer()
        self.start_parse(room_id, record_after=False)

    def delete_room(self, room_id):
        index, room = self.find_room(room_id)
        if not room:
            return
        if self.is_room_recording(room_id):
            QMessageBox.warning(self, "正在录制", "请先停止录制，再删除这个直播间。")
            return
        if QMessageBox.question(self, "删除确认", "确定删除这个直播间配置吗？") != QMessageBox.Yes:
            return
        self.rooms.pop(index)
        self.save_state()
        self.refresh_table()
        self.update_monitor_timer()

    def toggle_pin(self, room_id):
        _, room = self.find_room(room_id)
        if not room:
            return
        room["pinned"] = not room.get("pinned", False)
        self.save_state()
        self.refresh_table()

    def toggle_room_monitor(self, room_id):
        _, room = self.find_room(room_id)
        if not room:
            return
        room["monitor_enabled"] = not room.get("monitor_enabled", False)
        room["status"] = "监控中" if room["monitor_enabled"] else "未开始监控/录制结束"
        if not room["monitor_enabled"]:
            self.record_after_parse.pop(room_id, None)
        self.save_state()
        self.refresh_table()
        self.update_monitor_timer()
        if room["monitor_enabled"]:
            self.start_parse(room_id, record_after=False, silent=True)

    def open_settings(self):
        dialog = LiveSettingsDialog(
            self,
            interval=self.config.get("monitor_interval", 30),
            quality=self.config.get("prefer_quality", "best"),
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        self.config.update(dialog.get_data())
        self.save_state()
        self.update_monitor_timer()
        self.log("全局设置已保存。")

    def update_monitor_timer(self):
        has_monitor = any(room.get("monitor_enabled") for room in self.rooms)
        if not has_monitor:
            self.monitor_timer.stop()
            return
        interval = int(self.config.get("monitor_interval", 30) or 30)
        self.monitor_timer.start(interval * 1000)

    def monitor_once(self):
        for room in list(self.rooms):
            if room.get("monitor_enabled") and not self.is_room_recording(room["id"]):
                self.start_parse(room["id"], record_after=False, silent=True)

    def start_manual_record(self, room_id):
        self.start_parse(room_id, record_after=True, record_reason="manual", silent=False)

    def start_parse(self, room_id, record_after=False, record_reason="", silent=False):
        index, room = self.find_room(room_id)
        if not room:
            return
        if room_id in self.parse_workers and self.parse_workers[room_id].isRunning():
            return
        if record_after:
            self.record_after_parse[room_id] = record_reason or "manual"
        room["status"] = "正在解析直播间"
        self.save_state()
        self.refresh_table()
        if not silent:
            self.log(f"正在解析：{room.get('note') or room.get('url')}")
        worker = LiveParseWorker(room.get("url", ""))
        worker.finished.connect(lambda info, rid=room_id: self.on_parse_finished(rid, info))
        worker.failed.connect(lambda message, rid=room_id: self.on_parse_failed(rid, message))
        self.parse_workers[room_id] = worker
        worker.start()

    def on_parse_finished(self, room_id, info):
        self.parse_workers.pop(room_id, None)
        index, room = self.find_room(room_id)
        if not room:
            return
        room.update({
            "platform": info.get("platform", ""),
            "title": info.get("title", ""),
            "uploader": info.get("uploader", ""),
            "expanded_url": info.get("expanded_url", ""),
            "is_live": info.get("is_live", False),
            "formats": info.get("formats", []),
            "last_parsed_at": info.get("parsed_at", ""),
            "status": "已开播，等待录制" if info.get("is_live") else "未开播",
        })
        self.rooms[index] = room
        self.save_state()
        self.refresh_table()
        self.log(f"解析成功：{room.get('note') or room.get('uploader') or room.get('title')}，找到 {len(room.get('formats', []))} 条直播流。")
        record_reason = self.record_after_parse.pop(room_id, "")
        should_record = bool(record_reason)
        if should_record and room.get("is_live"):
            if record_reason == "manual":
                self.start_record(room_id)
            else:
                self.log("已跳过自动录制：监控只检测开播状态，不自动录制。")

    def on_parse_failed(self, room_id, message):
        self.parse_workers.pop(room_id, None)
        self.record_after_parse.pop(room_id, None)
        index, room = self.find_room(room_id)
        if room:
            room["status"] = "解析失败"
            self.rooms[index] = room
            self.save_state()
            self.refresh_table()
        self.log(message)

    def start_record(self, room_id):
        index, room = self.find_room(room_id)
        if not room:
            return
        if self.is_room_recording(room_id):
            return
        formats = room.get("formats") or []
        if not formats:
            self.start_parse(room_id, record_after=True, record_reason="manual")
            return
        try:
            stream = select_stream_format(formats, room.get("quality") or self.config.get("prefer_quality", "best"))
        except Exception as exc:
            QMessageBox.warning(self, "缺少直播流", str(exc))
            return
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), "ffmpeg.exe"))
        title = room.get("note") or room.get("uploader") or room.get("title") or "直播间录制"
        worker = LiveDownloadWorker(
            ffmpeg,
            stream["url"],
            self.config.get("output_dir", OUTPUT_DIR),
            title,
            "mp4",
        )
        worker.message.connect(lambda text, rid=room_id: self.log(f"{title}：{text}"))
        worker.finished.connect(lambda output, rid=room_id: self.on_record_finished(rid, output))
        worker.failed.connect(lambda message, rid=room_id: self.on_record_failed(rid, message))
        self.download_workers[room_id] = worker
        room["status"] = "录制中"
        self.rooms[index] = room
        self.save_state()
        self.refresh_table()
        worker.start()

    def is_room_recording(self, room_id):
        worker = self.download_workers.get(room_id)
        return bool(worker and worker.isRunning())

    def stop_record(self, room_id):
        worker = self.download_workers.get(room_id)
        if worker and worker.isRunning():
            index, room = self.find_room(room_id)
            if room:
                room["status"] = "正在停止录制"
                self.rooms[index] = room
                self.save_state()
                self.refresh_table()
            self.log("正在停止录制，请等待 ffmpeg 写出完整文件...")
            worker.stop()

    def on_record_finished(self, room_id, output_path):
        self.download_workers.pop(room_id, None)
        index, room = self.find_room(room_id)
        if room:
            room["last_output"] = output_path
            room["status"] = "录制结束"
            self.rooms[index] = room
            self.save_state()
            self.refresh_table()
        self.log(f"录制结束：{output_path or '未生成文件'}")

    def on_record_failed(self, room_id, message):
        self.download_workers.pop(room_id, None)
        index, room = self.find_room(room_id)
        if room:
            room["status"] = "录制失败"
            self.rooms[index] = room
            self.save_state()
            self.refresh_table()
        self.log(message)
        QMessageBox.warning(self, "录制失败", message)

    def open_room_from_cell(self, row, column):
        if column != 2 or row < 0 or row >= len(self.rooms):
            return
        self.open_room(self.rooms[row]["id"])

    def open_room(self, room_id):
        _, room = self.find_room(room_id)
        if not room:
            return
        url = room.get("expanded_url") or room.get("url")
        if not url:
            return
        try:
            open_url(url)
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开直播间：{exc}")

    def open_output_dir(self):
        os.makedirs(self.config.get("output_dir", OUTPUT_DIR), exist_ok=True)
        try:
            open_path(self.config.get("output_dir", OUTPUT_DIR))
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开输出目录：{exc}")

    def closeEvent(self, event):
        self.monitor_timer.stop()
        for worker in list(self.download_workers.values()):
            if worker and worker.isRunning():
                worker.stop()
        super().closeEvent(event)
