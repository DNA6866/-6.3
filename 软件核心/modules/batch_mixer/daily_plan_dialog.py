import copy

from PyQt5.QtCore import Qt, QTime
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFileDialog, QFormLayout, QGridLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTimeEdit,
    QVBoxLayout, QWidget,
)

from services.daily_production_service import allocate_shop_targets


class DailyPlanDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.repo = owner.batch_project_repo
        self.plan = {}
        self.projects = self.repo.projects()
        self.setWindowTitle("每日自动任务")
        available = self.screen().availableGeometry()
        self.resize(min(920, available.width() - 40), min(680, available.height() - 60))
        self.setStyleSheet("""
            QDialog, QWidget { background-color: #101827; color: #e5e7eb; }
            QLineEdit, QSpinBox, QTimeEdit, QListWidget, QTableWidget {
                background-color: #060e1b; border: 1px solid #64748b; padding: 5px;
                selection-background-color: #2563eb; }
            QPushButton { background-color: #26364b; border: 1px solid #64748b;
                padding: 7px 12px; border-radius: 4px; }
            QHeaderView::section { background-color: #26364b; padding: 5px; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #94a3b8; }
            QCheckBox::indicator:checked { background-color: #2563eb; }
        """)
        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        self.plans = QListWidget()
        self.plans.setMinimumWidth(150)
        self.plans.currentItemChanged.connect(self._select_plan)
        saved_panel = QWidget()
        saved_layout = QVBoxLayout(saved_panel)
        saved_layout.setContentsMargins(0, 0, 0, 0)
        saved_layout.addWidget(QLabel("已保存计划"))
        saved_layout.addWidget(self.plans)
        splitter.addWidget(saved_panel)
        panel = QWidget()
        content = QVBoxLayout(panel)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("例如：锦裕禾每日生产")
        self.enabled = QCheckBox("每天自动执行（软件需保持运行）")
        self.total = QSpinBox()
        self.total.setRange(1, 1000000)
        self.total.setValue(800)
        self.total.setMaximumWidth(150)
        self.total.wheelEvent = lambda event: event.ignore()
        self.start_time = QTimeEdit(QTime(9, 0))
        self.start_time.setDisplayFormat("HH:mm")
        self.start_time.setMaximumWidth(150)
        self.start_time.wheelEvent = lambda event: event.ignore()
        form.addRow("计划名称（备注）", self.name)
        form.addRow("", self.enabled)
        form.addRow("每店每日目标（条）", self.total)
        form.addRow("每日开始时间", self.start_time)
        content.addLayout(form)
        self.table = QTableWidget(len(self.projects), 4)
        self.table.setHorizontalHeaderLabels(["参与分配的模板", "混剪模式", "钩子总目录（可留空）", "模板目标"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(160)
        self.table.setShowGrid(True)
        self.table.setStyleSheet("QTableWidget { gridline-color: #42536b; }")
        modes = {"traditional": "传统音频", "ordinary": "普通拼接", "first_track": "数字人",
                 "ai_intro_first_track": "AI+数字人", "ai_intro_audio": "AI+音频"}
        for row, project in enumerate(self.projects):
            item = QTableWidgetItem(f"{project.get('shop_name', '')} / {project.get('product_name', '')}")
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, project.get("id"))
            item.setToolTip(f"{item.text()}\n模板编号：{project.get('id', '')}")
            self.table.setItem(row, 0, item)
            mode = project.get("rules", {}).get("mixer_mode", "traditional")
            item = QTableWidgetItem(modes.get(mode, mode))
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 1, item)
            path_item = QTableWidgetItem("")
            path_item.setBackground(QColor("#182638"))
            self.table.setItem(row, 2, path_item)
            target_item = QTableWidgetItem("")
            target_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.table.setItem(row, 3, target_item)
        content.addWidget(self.table, 1)
        browse = QPushButton("选择当前行钩子总目录")
        browse.clicked.connect(self._browse)
        content.addWidget(browse)
        self.allocation = QLabel()
        self.allocation.setWordWrap(True)
        content.addWidget(self.allocation)
        hint = QLabel("按模板保存的店铺名称分组，每店目标由店内所选模板平分；实际产量按完整轮数取近似值。\n"
                      "钩子总目录留空时沿用模板；AI开场不使用钩子。当天已建任务不重复创建、不改变数量。")
        hint.setWordWrap(True)
        content.addWidget(hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([170, 720])
        layout.addWidget(splitter, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QGridLayout()
        self.save_button = QPushButton("创建计划")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button, 0, 0)
        for index, (label, slot) in enumerate((("另建计划", self._new), ("删除计划", self._delete),
                            ("立即执行今日计划", self._run),
                            ("暂停/恢复自动任务", self._toggle))):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button, (index + 1) // 3, (index + 1) % 3)
        layout.addLayout(buttons)
        self.total.valueChanged.connect(self._allocation)
        self.table.itemChanged.connect(self._allocation)
        self._reset_form()
        self._reload()
        if self.plans.count():
            self.plans.setCurrentRow(0)

    def _reload(self, selected=""):
        self.plans.blockSignals(True)
        self.plans.clear()
        state = self.repo.daily_state()
        for plan in state.get("plans", []):
            item = QListWidgetItem(str(plan.get("name") or "每日计划"))
            item.setData(Qt.UserRole, plan)
            self.plans.addItem(item)
            if plan.get("id") == selected:
                self.plans.setCurrentItem(item)
        self.plans.blockSignals(False)
        self.status.setText("自动任务已暂停" if state.get("suspended") else "自动任务已就绪；未启用的计划不执行")

    def _new(self):
        if not self._confirm_unsaved():
            return
        self._reset_form()
        self.status.setText("正在填写新计划；填写完成后点击“创建计划”")

    def _reset_form(self):
        self.plans.setCurrentRow(-1)
        self._load({"total": 800, "time": "09:00"})

    def _select_plan(self, item, previous=None):
        if item:
            # 保存时会重建列表，先复制目标，不能继续访问旧列表项。
            target = copy.deepcopy(item.data(Qt.UserRole))
            if not self._confirm_unsaved():
                self._select_saved_id(self.plan.get("id", ""))
                return
            self._select_saved_id(target.get("id", ""))
            self._load(target)

    def _select_saved_id(self, plan_id):
        self.plans.blockSignals(True)
        self.plans.setCurrentRow(-1)
        for row in range(self.plans.count()):
            item = self.plans.item(row)
            if item.data(Qt.UserRole).get("id") == plan_id:
                self.plans.setCurrentRow(row)
                self.plans.scrollToItem(item)
                break
        self.plans.blockSignals(False)

    def _confirm_unsaved(self):
        if self._values() == getattr(self, "_saved_values", self._values()):
            return True
        choice = QMessageBox.question(
            self, "内容尚未保存", "当前填写的内容尚未保存，是否先保存？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if choice == QMessageBox.Save:
            return self._save()
        return choice == QMessageBox.Discard

    def reject(self):
        if self._confirm_unsaved():
            super().reject()

    def _load(self, plan):
        self.plan = copy.deepcopy(plan)
        self.name.setText(plan.get("name", ""))
        self.enabled.setChecked(bool(plan.get("enabled")))
        self.total.setValue(int(plan.get("total", 800)))
        self.start_time.setTime(QTime.fromString(plan.get("time", "09:00"), "HH:mm"))
        self.table.blockSignals(True)
        for row, project in enumerate(self.projects):
            pid = project.get("id")
            self.table.item(row, 0).setCheckState(Qt.Checked if pid in plan.get("project_ids", []) else Qt.Unchecked)
            self.table.item(row, 2).setText(plan.get("hook_roots", {}).get(pid, ""))
        self.table.blockSignals(False)
        self._allocation()
        self._saved_values = copy.deepcopy(self._values())
        self.save_button.setText("保存修改" if self.plan.get("id") else "创建计划")

    def _values(self):
        ids, roots = [], {}
        for row, project in enumerate(self.projects):
            if self.table.item(row, 0).checkState() == Qt.Checked:
                pid = project.get("id")
                ids.append(pid)
                roots[pid] = self.table.item(row, 2).text().strip().strip('"')
        return dict(self.plan, name=self.name.text().strip(), enabled=self.enabled.isChecked(),
                    total=self.total.value(), time=self.start_time.time().toString("HH:mm"),
                    project_ids=ids, hook_roots=roots, quantity_scope="per_shop")

    def _allocation(self, *args):
        values = self._values()
        targets = {}
        try:
            groups = allocate_shop_targets(values.get("total"), values.get("project_ids"), self.projects)
            targets = {pid: target for group in groups for pid, target in group.get("targets", {}).items()}
            total = sum(group.get("target", 0) for group in groups)
            lines = [f"{len(groups)} 家店，每店 {values.get('total')} 条，合计目标 {total} 条"]
            lines.extend(f"{group.get('shop_name')}：{len(group.get('targets', {}))} 个模板，"
                         + " / ".join(str(n) for n in group.get("targets", {}).values()) + " 条"
                         for group in groups)
            self.allocation.setText("\n".join(lines))
        except ValueError as exc:
            self.allocation.setText(str(exc))
        blocked = self.table.blockSignals(True)
        for row, project in enumerate(self.projects):
            target = targets.get(project.get("id"))
            self.table.item(row, 3).setText(f"{target} 条" if target is not None else "未参与")
        self.table.blockSignals(blocked)

    def _browse(self):
        row = self.table.currentRow()
        if row < 0:
            return
        path = QFileDialog.getExistingDirectory(self, "选择包含各批次子文件夹的总目录")
        if path:
            self.table.item(row, 2).setText(path)

    def _save(self):
        values = self._values()
        try:
            if not values.get("name"):
                raise ValueError("请填写计划名称")
            allocate_shop_targets(values.get("total"), values.get("project_ids"), self.projects)
            self.plan = self.repo.save_daily_plan(values)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法保存计划", str(exc))
            return False
        self._reload(self.plan.get("id"))
        self._load(self.plan)
        self._select_saved_id(self.plan.get("id"))
        state = self.repo.daily_state()
        schedule = ("自动任务已暂停，请恢复后执行" if state.get("suspended") else
                    f"每天 {self.plan.get('time')} 自动执行" if self.plan.get("enabled") else
                    "未开启每天自动执行")
        self.status.setText(f"已保存到左侧：{self.plan.get('name')}；{schedule}")
        return True

    def _delete(self):
        if self.plan.get("id") and QMessageBox.question(self, "删除计划", "删除此计划？已生成的队列任务和成片仍然保留。") == QMessageBox.Yes:
            self.repo.delete_daily_plan(self.plan.get("id"))
            self._reload()
            self._reset_form()

    def _run(self):
        self.enabled.setChecked(True)
        if self._save():
            self.repo.suspend_daily(False)
            self.owner._daily_tick(force_plan_id=self.plan.get("id"))
            self.status.setText("已请求执行今日计划；任务进度及等待原因见主窗口日志")

    def _toggle(self):
        paused = not self.repo.daily_state().get("suspended", False)
        self.repo.suspend_daily(paused)
        if paused:
            self.owner.pause_batch_task_queue()
        self._reload(self.plan.get("id"))
