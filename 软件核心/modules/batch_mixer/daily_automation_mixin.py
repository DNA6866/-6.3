from datetime import datetime
import os
import time

from PyQt5.QtCore import QThread, QTimer, Qt, pyqtSignal

from modules.batch_mixer.daily_plan_dialog import DailyPlanDialog
from services.daily_production_service import (
    ASSET_FIELDS, build_daily_payloads, plan_due, plans_in_rotation, scan_materials,
)


class DailyPrepareWorker(QThread):
    prepared = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, plan, projects, runs, day, extensions, cache_dir):
        super().__init__()
        self.plan = plan
        self.args = (plan, projects, runs, day, extensions, cache_dir)
        self.day = day

    def run(self):
        try:
            payloads, hooks = build_daily_payloads(*self.args, cancelled=self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.prepared.emit((self.plan, self.day, payloads, hooks))
        except InterruptedError:
            pass
        except Exception as exc:
            self.failed.emit(str(exc))


class MaterialPollWorker(QThread):
    scanned = pyqtSignal(str, str, object)

    def __init__(self, roots, extensions):
        super().__init__()
        self.roots = roots
        self.extensions = extensions

    def run(self):
        for tab, root in self.roots.items():
            if self.isInterruptionRequested():
                return
            try:
                files = scan_materials(root, self.extensions.get(tab, []), self.isInterruptionRequested)
                self.scanned.emit(tab, root, files)
            except InterruptedError:
                return
            except OSError:
                # 网络瞬断保留上次列表，任务准备阶段会明确报告不可访问目录。
                continue


class DailyAutomationMixin:
    def _init_daily_automation(self):
        self._daily_closing = False
        self._daily_worker = None
        self._material_poll_worker = None
        self._daily_retry_after = {}
        self._daily_notice = ""
        self._daily_forced_plan_ids = set()
        self._daily_timer = QTimer(self)
        self._daily_timer.setInterval(30000)
        self._daily_timer.timeout.connect(self._daily_tick)
        self._material_poll_timer = QTimer(self)
        self._material_poll_timer.setInterval(120000)
        self._material_poll_timer.timeout.connect(self._poll_current_materials)

    def _start_daily_automation(self):
        self._daily_timer.start()
        self._material_poll_timer.start()

    def show_daily_plans(self):
        # 显式保存当前模板，其他模板直接使用各自已保存的配置。
        if not self._project_pending_scans and not self._editing_queue_task_id:
            self.save_current_batch_project(silent=True)
        dialog = DailyPlanDialog(self)
        dialog.exec_()

    def _daily_log_once(self, message):
        if message != self._daily_notice:
            self._daily_notice = message
            self.update_log(f"[每日自动任务] {message}")

    def _daily_tick(self, force_plan_id=""):
        if force_plan_id:
            self._daily_forced_plan_ids.add(force_plan_id)
        if self._daily_closing or self._daily_worker is not None:
            return
        state = self.batch_project_repo.daily_state()
        if state.get("suspended"):
            return
        if self._editing_queue_task_id or getattr(self, "_fresh_asset_request", None):
            return
        engine = getattr(self, "engine_thread", None)
        if self._active_queue_task_id or (engine is not None and engine.isRunning()):
            if force_plan_id:
                self._daily_log_once("当前任务运行中，将在空闲后按计划执行；不会修改正在运行的素材清单。")
            return
        waiting = [t for t in self.batch_project_repo.tasks(False) if t.get("status") == "waiting"]
        if waiting:
            if not all(t.get("daily_plan_id") for t in waiting):
                self._daily_log_once("队列中有手动等待任务，请先处理或启动等待队列。")
                return
            enabled_ids = {p.get("id") for p in state.get("plans", []) if p.get("enabled")}
            if waiting[0].get("daily_plan_id") not in enabled_ids:
                self._daily_log_once("队首每日计划已停用，请在任务队列中处理遗留任务。")
                return
            self._daily_log_once("继续执行未完成的每日任务。")
            self.start_batch_task_queue()
            return
        now = datetime.now()
        runs = state.get("runs", {})
        for plan in plans_in_rotation(state.get("plans", []), runs):
            forced = plan.get("id") in self._daily_forced_plan_ids
            if force_plan_id and not forced:
                continue
            key = f"{plan.get('id')}:{now.date().isoformat()}"
            if key in runs:
                self._daily_forced_plan_ids.discard(plan.get("id"))
                if forced:
                    self._daily_log_once("该计划今天已建立任务，不会重复生成；失败任务请在队列中重试。")
                continue
            if not plan.get("enabled") or (not forced and not plan_due(plan, runs, now)):
                continue
            if not forced and time.monotonic() < self._daily_retry_after.get(plan.get("id"), 0):
                continue
            self._daily_log_once(f"正在读取“{plan.get('name')}”的最新素材，并计算轮数。")
            worker = DailyPrepareWorker(
                plan, self.batch_project_repo.projects(), runs, now.date().isoformat(),
                dict(self.tab_exts), os.path.abspath(self.cache_dir),
            )
            self._daily_worker = worker
            self._thread_registry.register(worker, "每日任务准备")
            worker.prepared.connect(self._daily_prepared, Qt.QueuedConnection)
            worker.failed.connect(self._daily_prepare_failed, Qt.QueuedConnection)
            worker.finished.connect(self._daily_worker_finished, Qt.QueuedConnection)
            worker.start()
            return

    def _daily_prepared(self, result):
        if self._daily_closing:
            return
        plan, day, payloads, hooks = result
        self._daily_forced_plan_ids.discard(plan.get("id"))
        try:
            tasks = self.batch_project_repo.add_daily_batch(plan, day, payloads, hooks)
        except Exception as exc:
            self._daily_prepare_failed(f"队列保存失败：{exc}")
            return
        if not tasks:
            self._daily_log_once("计划已变更、暂停或今日任务已存在，本次准备结果未加入队列。")
            return
        for task in tasks:
            self.update_log(
                f"[每日自动任务] {task.get('task_name')}：{task.get('asset_summary')}，"
                f"预计 {task.get('expected')} 条；输出：{task.get('output_dir')}"
            )
            for message in task.get("skipped_drivers", []):
                self.update_log(f"[每日自动任务] 已跳过无效驱动素材：{message}")
        for pid, path in hooks.items():
            self.update_log(f"[每日自动任务] 模板 {pid[:6]} 今日钩子：{path}")
        self._refresh_task_center()
        # 准备期间用户可能开始了手动任务，此时只入队，不抢占运行任务。
        engine = getattr(self, "engine_thread", None)
        waiting = [t for t in self.batch_project_repo.tasks(False) if t.get("status") == "waiting"]
        if (not self._editing_queue_task_id and not getattr(self, "_fresh_asset_request", None)
                and all(t.get("daily_plan_id") for t in waiting)
                and not (engine is not None and engine.isRunning())):
            self.start_batch_task_queue()

    def _daily_prepare_failed(self, message):
        worker = self._daily_worker
        if worker:
            self._daily_retry_after[worker.plan.get("id")] = time.monotonic() + 300
            self._daily_forced_plan_ids.discard(worker.plan.get("id"))
        self._daily_log_once(f"准备未完成，5分钟后重试：{message}")

    def _daily_worker_finished(self):
        worker = self._daily_worker
        self._daily_worker = None
        if worker:
            worker.deleteLater()

    def _poll_current_materials(self):
        if (self._daily_closing or self._material_poll_worker is not None
                or self._daily_worker is not None or self._project_pending_scans
                or self._directory_scan_workers):
            return
        # 渲染中不改素材表行号，避免音频进度写到另一行；每日任务仍使用冻结快照。
        engine = getattr(self, "engine_thread", None)
        if engine is not None and engine.isRunning():
            return
        roots = {tab: root for tab, root in self.tab_folders.items() if tab in ASSET_FIELDS and root}
        worker = MaterialPollWorker(roots, dict(self.tab_exts))
        self._material_poll_worker = worker
        self._thread_registry.register(worker, "素材自动刷新")
        worker.scanned.connect(self._on_material_poll, Qt.QueuedConnection)
        worker.finished.connect(self._material_poll_finished, Qt.QueuedConnection)
        worker.start()

    def _on_material_poll(self, tab, root, files):
        engine = getattr(self, "engine_thread", None)
        if (self._daily_closing or self._project_pending_scans or self._directory_scan_workers
                or getattr(self, "_fresh_asset_request", None)
                or (engine is not None and engine.isRunning())
                or os.path.normcase(root) != os.path.normcase(self.tab_folders.get(tab, ""))):
            return
        table = self.tables.get(tab)
        if table is None:
            return
        current = [(table.item(row, 1).text(), table.item(row, 2).text())
                   for row in range(table.rowCount()) if table.item(row, 1) and table.item(row, 2)]
        if current == files:
            return
        selected = {table.item(row, 2).text() for row in range(table.rowCount())
                    if table.item(row, 2) and self._asset_row_checked(table, row)}
        select_all = self.select_all_cbx.get(tab).isChecked()
        self._populate_directory_table(tab, files)
        if not select_all:
            for row, (_, path) in enumerate(files):
                self._set_asset_check_item_visual(table.item(row, 0), path in selected)
            self.update_asset_count_label(tab)
        self.sync_template_asset_selection(tab)
        self.update_log(f"[素材自动刷新] {tab}：{len(current)} → {len(files)} 条；运行任务不受影响。")

    def _material_poll_finished(self):
        worker = self._material_poll_worker
        self._material_poll_worker = None
        if worker:
            worker.deleteLater()

    def _close_daily_automation(self):
        self._daily_closing = True
        self._daily_timer.stop()
        self._material_poll_timer.stop()
        for worker in (self._daily_worker, self._material_poll_worker):
            if worker:
                worker.requestInterruption()
