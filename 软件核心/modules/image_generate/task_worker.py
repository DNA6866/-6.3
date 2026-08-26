import random

from PyQt5.QtCore import QThread, pyqtSignal

from .comfy_api import ComfyUIApi
from .comfy_manager import ComfyUIManager
from .env_check import check_environment
from .workflow_manager import prepare_workflow


class EnvCheckWorker(QThread):
    finished = pyqtSignal(object, str, bool)
    failed = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config

    def run(self):
        try:
            self.finished.emit(*check_environment(self.config))
        except Exception as exc:
            self.failed.emit(f"环境检测失败：{exc}")


class ComfyStartWorker(QThread):
    message = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, config, manager_holder):
        super().__init__()
        self.config = config
        self.manager_holder = manager_holder

    def run(self):
        try:
            manager = ComfyUIManager(
                self.config.get("comfyui_path", ""),
                self.config.get("host", "127.0.0.1"),
                self.config.get("port", 8199),
            )
            self.message.emit("正在后台启动 ComfyUI...")
            _, msg = manager.start(
                timeout=max(90, min(300, int(self.config.get("startup_timeout", 180)))),
                extra_args=self.config.get("comfyui_args", []),
                log_file=self.config.get("startup_log_file"),
            )
            self.manager_holder["manager"] = manager
            self.finished.emit(msg)
        except Exception as exc:
            self.failed.emit(str(exc))


class ImageGenerateWorker(QThread):
    progress = pyqtSignal(int)
    message = pyqtSignal(str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal(object)

    def __init__(self, config, params):
        super().__init__()
        self.config = config
        self.params = params
        self._cancelled = False
        self._api = None
        self._files = []

    def cancel(self):
        """请求取消当前生成任务。"""
        self._cancelled = True
        if self._api:
            self._api.interrupt()
            self._api.clear_queue()

    def run(self):
        self._files = []
        try:
            self.progress.emit(10)
            self._api = ComfyUIApi(self.config["host"], self.config["port"])
            total = max(1, int(self.params.get("image_count", 1)))
            base_seed = int(self.params.get("seed", 0))
            self.message.emit(
                "正在准备 Z-Image 工作流..."
                if total == 1
                else f"正在准备 Z-Image 工作流，本次按单张循环生成 {total} 张，避免低显存显卡批量糊图。"
            )
            for index in range(total):
                if self._cancelled:
                    self.message.emit("用户取消，正在清理 ComfyUI 任务队列...")
                    self._api.interrupt()
                    self._api.clear_queue()
                    self.cancelled.emit(list(self._files))
                    return
                item_params = dict(self.params)
                item_params["image_count"] = 1
                if base_seed > 0:
                    item_params["seed"] = base_seed + index
                else:
                    item_params["seed"] = random.randint(1, 2147483647)
                start_progress = 10 + int(index / total * 85)
                self.progress.emit(start_progress)
                self.message.emit(f"正在提交第 {index + 1}/{total} 张图片生成任务，种子：{item_params['seed']}")
                workflow = prepare_workflow(
                    item_params["workflow_path"], item_params,
                    comfyui_path=self.config.get("comfyui_path")
                )
                prompt_id = self._api.queue_prompt(workflow)
                self.progress.emit(min(95, start_progress + 12))
                self.message.emit(f"ComfyUI 正在生成第 {index + 1}/{total} 张图片...")
                result = self._api.wait_for_images(prompt_id, self.config["output_dir"],
                                                     canceller=lambda: self._cancelled)
                if result is None and self._cancelled:
                    self._api.interrupt()
                    self._api.clear_queue()
                    self.cancelled.emit(list(self._files))
                    return
                self._files.extend(result or [])
                self.progress.emit(10 + int((index + 1) / total * 85))
            self.progress.emit(100)
            self.message.emit(f"图片生成完成，共 {len(self._files)} 张。")
            self.finished.emit(list(self._files))
        except Exception as exc:
            if self._cancelled:
                try:
                    self._api.interrupt()
                    self._api.clear_queue()
                except Exception:
                    pass
                self.cancelled.emit(list(self._files))
            else:
                self.failed.emit(f"图片生成失败，请先点击[检测环境]，确认通过后再重试。底层信息：{exc}")
