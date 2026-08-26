import copy
from datetime import datetime
import json
import os
import re
import threading
import uuid

from paths import config_dir
from services.render_checkpoint_service import (
    checkpoint_path_for_task,
    checkpoint_progress,
    ensure_resume_source_ids,
    remove_checkpoint,
)


PROJECT_STATE_VERSION = 2
WAITING_STATUS = "waiting"
RUNNING_STATUS = "running"
COMPLETED_STATUS = "completed"
FAILED_STATUS = "failed"
STOPPED_STATUS = "stopped"
FINISHED_STATUSES = frozenset({COMPLETED_STATUS, FAILED_STATUS, STOPPED_STATUS})


def safe_output_component(value, fallback="未命名"):
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = text.rstrip(" .")
    return (text or fallback)[:60]


def project_display_name(project):
    if not isinstance(project, dict):
        return "未命名项目"
    shop = str(project.get("shop_name") or "未命名店铺").strip()
    product = str(project.get("product_name") or "未命名产品").strip()
    return f"{shop}｜{product}"


def queue_task_display_name(task):
    """返回队列任务名称，并兼容没有 task_name 的旧版本任务。"""
    if not isinstance(task, dict):
        return "未命名任务"
    return str(
        task.get("task_name")
        or task.get("mode_label")
        or project_display_name(task)
    ).strip()


class BatchProjectRepository:
    """持久化店铺产品配置和混剪等待队列。"""

    def __init__(self, state_path=""):
        self.state_path = state_path or os.path.join(
            config_dir(),
            "batch_project_tasks.json",
        )
        self._lock = threading.RLock()
        self._state = self._load()

    @staticmethod
    def _empty_state():
        return {
            "version": PROJECT_STATE_VERSION,
            "current_project_id": "",
            "queue_paused": True,
            "projects": [],
            "tasks": [],
        }

    def _load(self):
        state = self._empty_state()
        # 主文件损坏时自动回退 .bak 备份（防止断电写入中断导致队列丢失）
        for path in (self.state_path, self.state_path + ".bak"):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    state.update(loaded)
                    break
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        state["projects"] = [
            item for item in state.get("projects", []) if isinstance(item, dict)
        ]
        state["tasks"] = [
            item for item in state.get("tasks", []) if isinstance(item, dict)
        ]
        state["version"] = PROJECT_STATE_VERSION
        state["queue_paused"] = True
        state_changed = False
        used_task_names = set()
        for task in state["tasks"]:
            base_name = queue_task_display_name(task)
            task_name = base_name
            suffix = 2
            while task_name.casefold() in used_task_names:
                task_name = f"{base_name} {suffix:02d}"
                suffix += 1
            used_task_names.add(task_name.casefold())
            if task.get("task_name") != task_name:
                task["task_name"] = task_name
                state_changed = True
            task_id = str(task.get("id") or "")
            task_data = task.get("task_data")
            if isinstance(task_data, dict) and task_id:
                ensure_resume_source_ids(task_data)
                checkpoint_path = str(
                    task.get("checkpoint_path")
                    or task_data.get("resume_checkpoint_path")
                    or checkpoint_path_for_task(task_id)
                )
                if (
                    task.get("checkpoint_path") != checkpoint_path
                    or task_data.get("resume_checkpoint_path") != checkpoint_path
                    or task_data.get("resume_task_id") != task_id
                ):
                    state_changed = True
                task["checkpoint_path"] = checkpoint_path
                task_data["resume_checkpoint_path"] = checkpoint_path
                task_data["resume_task_id"] = task_id
                if task.get("status") != COMPLETED_STATUS:
                    completed, _ = checkpoint_progress(
                        task_data,
                        checkpoint_path,
                        task_id=task_id,
                    )
                    task["completed"] = completed
            if task.get("status") == RUNNING_STATUS:
                task["status"] = WAITING_STATUS
                completed = int(task.get("completed") or 0)
                task["message"] = (
                    f"上次运行被中断，已恢复断点 {completed} 条，等待继续"
                )
                state_changed = True
        if state_changed:
            self._save_state(state)
        return state

    def _save_state(self, state):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        # 先写备份（独立临时文件+替换），再写主文件；
        # 主文件写入异常时 .bak 仍是上次完整状态，加载时自动回退。
        backup_path = self.state_path + ".bak"
        temp_backup = f"{backup_path}.{os.getpid()}.tmp"
        try:
            with open(temp_backup, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_backup, backup_path)
        except Exception:
            try:
                if os.path.exists(temp_backup):
                    os.remove(temp_backup)
            except OSError:
                pass
        temp_path = f"{self.state_path}.{os.getpid()}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_path, self.state_path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def save(self):
        with self._lock:
            self._save_state(self._state)

    def projects(self):
        with self._lock:
            return copy.deepcopy(self._state.get("projects", []))

    def tasks(self, include_payload=True):
        with self._lock:
            source_tasks = self._state.get("tasks", [])
            if include_payload:
                return copy.deepcopy(source_tasks)
            # 任务中心只需要摘要字段，不能每 0.8 秒深拷贝数千条素材路径。
            return [
                {
                    key: copy.deepcopy(value)
                    for key, value in task.items()
                    if key not in {"task_data", "directories", "project_snapshot"}
                }
                for task in source_tasks
            ]

    def project(self, project_id):
        with self._lock:
            for project in self._state.get("projects", []):
                if project.get("id") == project_id:
                    return copy.deepcopy(project)
        return None

    def current_project_id(self):
        with self._lock:
            return str(self._state.get("current_project_id") or "")

    def set_current_project(self, project_id):
        with self._lock:
            self._state["current_project_id"] = str(project_id or "")
            self._save_state(self._state)

    def upsert_project(self, project):
        data = copy.deepcopy(project if isinstance(project, dict) else {})
        project_id = str(data.get("id") or uuid.uuid4().hex)
        data["id"] = project_id
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            projects = self._state.setdefault("projects", [])
            for index, existing in enumerate(projects):
                if existing.get("id") == project_id:
                    projects[index] = data
                    break
            else:
                data.setdefault("created_at", data["updated_at"])
                projects.append(data)
            self._state["current_project_id"] = project_id
            self._save_state(self._state)
        return copy.deepcopy(data)

    def delete_project(self, project_id):
        with self._lock:
            before = len(self._state.get("projects", []))
            self._state["projects"] = [
                item
                for item in self._state.get("projects", [])
                if item.get("id") != project_id
            ]
            if self._state.get("current_project_id") == project_id:
                self._state["current_project_id"] = ""
            changed = len(self._state["projects"]) != before
            if changed:
                self._save_state(self._state)
            return changed

    def add_task(self, task):
        data = copy.deepcopy(task if isinstance(task, dict) else {})
        data["id"] = str(data.get("id") or uuid.uuid4().hex)
        data["task_name"] = queue_task_display_name(data)
        task_data = data.get("task_data")
        if isinstance(task_data, dict):
            ensure_resume_source_ids(task_data)
            checkpoint_path = str(
                data.get("checkpoint_path")
                or checkpoint_path_for_task(data["id"])
            )
            data["checkpoint_path"] = checkpoint_path
            task_data["resume_checkpoint_path"] = checkpoint_path
            task_data["resume_task_id"] = data["id"]
        data["status"] = WAITING_STATUS
        data["created_at"] = datetime.now().isoformat(timespec="seconds")
        data["started_at"] = ""
        data["finished_at"] = ""
        data["completed"] = 0
        data.setdefault("expected", 0)
        data.setdefault("message", "等待执行")
        with self._lock:
            tasks = self._state.setdefault("tasks", [])
            tasks.append(data)
            removed_tasks = tasks[:-200]
            self._state["tasks"] = tasks[-200:]
            self._save_state(self._state)
            for removed_task in removed_tasks:
                remove_checkpoint(
                    str(removed_task.get("checkpoint_path") or "")
                )
        return copy.deepcopy(data)

    def replace_task(self, task_id, replacement):
        """原位替换可编辑任务，并清空与旧参数不兼容的断点。"""
        task_id = str(task_id or "")
        data = copy.deepcopy(replacement if isinstance(replacement, dict) else {})
        with self._lock:
            tasks = self._state.get("tasks", [])
            index = next(
                (i for i, task in enumerate(tasks) if task.get("id") == task_id),
                -1,
            )
            if index < 0 or tasks[index].get("status") == RUNNING_STATUS:
                return None
            old_task = tasks[index]
            checkpoint_path = str(
                old_task.get("checkpoint_path") or checkpoint_path_for_task(task_id)
            )
            data["id"] = task_id
            data["task_name"] = queue_task_display_name(data)
            data["checkpoint_path"] = checkpoint_path
            data["created_at"] = str(
                old_task.get("created_at")
                or datetime.now().isoformat(timespec="seconds")
            )
            data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            data["status"] = WAITING_STATUS
            data["started_at"] = ""
            data["finished_at"] = ""
            data["completed"] = 0
            data.setdefault("expected", 0)
            data["message"] = "参数已修改，等待执行"
            task_data = data.get("task_data")
            if isinstance(task_data, dict):
                ensure_resume_source_ids(task_data)
                task_data["resume_checkpoint_path"] = checkpoint_path
                task_data["resume_task_id"] = task_id
            tasks[index] = data
            self._save_state(self._state)
        remove_checkpoint(checkpoint_path)
        return copy.deepcopy(data)

    def task(self, task_id):
        with self._lock:
            for task in self._state.get("tasks", []):
                if task.get("id") == task_id:
                    return copy.deepcopy(task)
        return None

    def update_task(self, task_id, persist=True, **changes):
        with self._lock:
            for task in self._state.get("tasks", []):
                if task.get("id") != task_id:
                    continue
                task.update(copy.deepcopy(changes))
                if persist:
                    self._save_state(self._state)
                return copy.deepcopy(task)
        return None

    def next_waiting_task(self):
        with self._lock:
            for task in self._state.get("tasks", []):
                if task.get("status") == WAITING_STATUS:
                    return copy.deepcopy(task)
        return None

    def set_queue_paused(self, paused):
        with self._lock:
            self._state["queue_paused"] = bool(paused)
            self._save_state(self._state)

    def waiting_count(self):
        with self._lock:
            return sum(
                1
                for task in self._state.get("tasks", [])
                if task.get("status") == WAITING_STATUS
            )

    def move_task(self, task_id, offset):
        with self._lock:
            tasks = self._state.get("tasks", [])
            index = next(
                (i for i, task in enumerate(tasks) if task.get("id") == task_id),
                -1,
            )
            target = index + int(offset)
            if index < 0 or target < 0 or target >= len(tasks):
                return False
            if tasks[index].get("status") == RUNNING_STATUS:
                return False
            tasks[index], tasks[target] = tasks[target], tasks[index]
            self._save_state(self._state)
            return True

    def remove_task(self, task_id):
        with self._lock:
            tasks = self._state.get("tasks", [])
            target = next(
                (task for task in tasks if task.get("id") == task_id),
                None,
            )
            if not target or target.get("status") == RUNNING_STATUS:
                return False
            self._state["tasks"] = [
                task for task in tasks if task.get("id") != task_id
            ]
            self._save_state(self._state)
            remove_checkpoint(str(target.get("checkpoint_path") or ""))
            return True

    def retry_task(self, task_id):
        task = self.task(task_id)
        if not task or task.get("status") not in FINISHED_STATUSES:
            return False
        checkpoint_path = str(task.get("checkpoint_path") or "")
        if task.get("status") == COMPLETED_STATUS:
            remove_checkpoint(checkpoint_path)
            completed = 0
        else:
            completed, _ = checkpoint_progress(
                task.get("task_data", {}),
                checkpoint_path,
                task_id=task_id,
            )
        return bool(self.update_task(
            task_id,
            status=WAITING_STATUS,
            completed=completed,
            message=(
                f"已保留断点 {completed} 条，等待继续执行"
                if completed else "等待重新执行"
            ),
            started_at="",
            finished_at="",
        ))

    def clear_completed(self):
        with self._lock:
            removed_tasks = [
                task
                for task in self._state.get("tasks", [])
                if task.get("status") == COMPLETED_STATUS
            ]
            before = len(self._state.get("tasks", []))
            self._state["tasks"] = [
                task
                for task in self._state.get("tasks", [])
                if task.get("status") != COMPLETED_STATUS
            ]
            removed = before - len(self._state["tasks"])
            if removed:
                self._save_state(self._state)
                for task in removed_tasks:
                    remove_checkpoint(str(task.get("checkpoint_path") or ""))
            return removed
