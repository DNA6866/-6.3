import os

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from services.batch_project_service import (
    project_display_name,
    queue_task_display_name,
)


STATUS_LABELS = {
    "waiting": "等待",
    "running": "运行中",
    "completed": "已完成",
    "failed": "部分失败",
    "stopped": "已停止",
}


class NewBatchProjectDialog(QDialog):
    def __init__(self, parent=None, project=None):
        super().__init__(parent)
        project = project if isinstance(project, dict) else {}
        self.setWindowTitle("店铺产品项目")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.shop_input = QLineEdit(str(project.get("shop_name") or ""))
        self.shop_input.setPlaceholderText("例如：抖音一店")
        self.product_input = QLineEdit(str(project.get("product_name") or ""))
        self.product_input.setPlaceholderText("例如：黄金面膜")
        self.root_input = QLineEdit()
        self.root_input.setPlaceholderText("可选：选择后自动识别常用素材子目录")
        root_layout = QHBoxLayout()
        root_layout.addWidget(self.root_input, 1)
        browse_btn = QPushButton("选择根目录")
        browse_btn.clicked.connect(self._choose_root)
        root_layout.addWidget(browse_btn)
        form.addRow("店铺名称:", self.shop_input)
        form.addRow("产品名称:", self.product_input)
        form.addRow("素材根目录:", root_layout)
        layout.addLayout(form)
        hint = QLabel(
            "根目录可以留空，软件会直接保存当前页面已经配置好的全部目录和参数。"
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("保存项目")
        save_btn.setObjectName("startButton")
        save_btn.clicked.connect(self._accept_if_valid)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)
        layout.addLayout(buttons)

    def _choose_root(self):
        current = self.root_input.text().strip()
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择产品素材根目录",
            current if os.path.isdir(current) else "",
        )
        if folder:
            self.root_input.setText(os.path.abspath(folder))

    def _accept_if_valid(self):
        if not self.shop_input.text().strip() or not self.product_input.text().strip():
            QMessageBox.warning(self, "信息不完整", "请填写店铺名称和产品名称。")
            return
        self.accept()

    def values(self):
        return {
            "shop_name": self.shop_input.text().strip(),
            "product_name": self.product_input.text().strip(),
            "root_dir": self.root_input.text().strip(),
        }


class BatchTaskCenterDialog(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.setWindowTitle("混剪任务队列")
        self.resize(1280, 660)
        layout = QVBoxLayout(self)
        title = QLabel("混剪任务队列")
        title.setObjectName("appTitle")
        layout.addWidget(title)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("mutedText")
        layout.addWidget(self.summary_label)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "顺序",
            "任务名称",
            "公共素材配置",
            "模式",
            "素材概览",
            "计划产量",
            "状态",
            "进度",
            "输出目录 / 提示",
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.doubleClicked.connect(self._edit)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        header.setSectionResizeMode(8, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        controls = QHBoxLayout()
        start_btn = QPushButton("开始全部等待任务")
        start_btn.setObjectName("startButton")
        start_btn.clicked.connect(self.host.start_batch_task_queue)
        pause_btn = QPushButton("当前任务完成后暂停")
        pause_btn.clicked.connect(self.host.pause_batch_task_queue)
        stop_btn = QPushButton("停止当前任务")
        stop_btn.setObjectName("stopButton")
        stop_btn.clicked.connect(self.host.stop_current_queue_task)
        controls.addWidget(start_btn)
        controls.addWidget(pause_btn)
        controls.addWidget(stop_btn)
        controls.addStretch(1)

        for text, callback in (
            ("编辑任务", self._edit),
            ("上移", lambda: self._move(-1)),
            ("下移", lambda: self._move(1)),
            ("重试", self._retry),
            ("打开输出", self._open_output),
            ("移除", self._remove),
            ("清理已完成", self._clear_completed),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            controls.addWidget(button)
        layout.addLayout(controls)

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def selected_task_id(self):
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.UserRole) or "") if item else ""

    def refresh(self):
        selected_id = self.selected_task_id()
        tasks = self.host.batch_queue_tasks()
        waiting = sum(task.get("status") == "waiting" for task in tasks)
        running = sum(task.get("status") == "running" for task in tasks)
        completed = sum(task.get("status") == "completed" for task in tasks)
        self.summary_label.setText(
            f"等待 {waiting} 个，运行 {running} 个，已完成 {completed} 个。"
            "任务按顺序执行；双击等待任务也可以载回工作台修改。"
        )
        self.table.setRowCount(len(tasks))
        selected_row = -1
        for row, task in enumerate(tasks):
            order_item = QTableWidgetItem(f"{row + 1:03d}")
            order_item.setData(Qt.UserRole, task.get("id", ""))
            output_dir = str(task.get("output_dir") or "")
            message = str(task.get("message") or "")
            output_message = output_dir
            if message:
                output_message = f"{output_dir} | {message}" if output_dir else message
            values = [
                order_item,
                QTableWidgetItem(queue_task_display_name(task)),
                QTableWidgetItem(project_display_name(task)),
                QTableWidgetItem(str(task.get("mode_label") or "")),
                QTableWidgetItem(str(task.get("asset_summary") or "")),
                QTableWidgetItem(str(task.get("expected") or 0)),
                QTableWidgetItem(STATUS_LABELS.get(task.get("status"), "未知")),
                QTableWidgetItem(
                    f"{int(task.get('completed') or 0)}/{int(task.get('expected') or 0)}"
                ),
                QTableWidgetItem(output_message),
            ]
            for column, item in enumerate(values):
                self.table.setItem(row, column, item)
            if task.get("id") == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.table.selectRow(selected_row)

    def _move(self, offset):
        task_id = self.selected_task_id()
        if task_id:
            self.host.move_batch_queue_task(task_id, offset)
            self.refresh()

    def _edit(self, *_args):
        task_id = self.selected_task_id()
        if task_id and self.host.edit_batch_queue_task(task_id):
            self.hide()

    def _retry(self):
        task_id = self.selected_task_id()
        if task_id:
            self.host.retry_batch_queue_task(task_id)
            self.refresh()

    def _remove(self):
        task_id = self.selected_task_id()
        if task_id:
            self.host.remove_batch_queue_task(task_id)
            self.refresh()

    def _clear_completed(self):
        self.host.clear_completed_batch_queue_tasks()
        self.refresh()

    def _open_output(self):
        task_id = self.selected_task_id()
        if task_id:
            self.host.open_batch_queue_output(task_id)
