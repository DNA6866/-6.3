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
            manager = ComfyUIManager(self.config["comfyui_path"], self.config["host"], self.config["port"])
            self.message.emit("正在后台启动 ComfyUI...")
            _, msg = manager.start(extra_args=self.config.get("comfyui_args", []))
            self.manager_holder["manager"] = manager
            self.finished.emit(msg)
        except Exception as exc:
            self.failed.emit(str(exc))


class ImageGenerateWorker(QThread):
    progress = pyqtSignal(int)
    message = pyqtSignal(str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, config, params):
        super().__init__()
        self.config = config
        self.params = params

    def run(self):
        try:
            self.progress.emit(10)
            self.message.emit("正在准备 Z-Image 工作流...")
            workflow = prepare_workflow(self.params["workflow_path"], self.params)
            api = ComfyUIApi(self.config["host"], self.config["port"])
            self.progress.emit(25)
            self.message.emit("正在提交图片生成任务...")
            prompt_id = api.queue_prompt(workflow)
            self.progress.emit(45)
            self.message.emit("ComfyUI 正在生成图片...")
            files = api.wait_for_images(prompt_id, self.config["output_dir"])
            self.progress.emit(100)
            self.message.emit("图片生成完成。")
            self.finished.emit(files)
        except Exception as exc:
            self.failed.emit(f"图片生成失败，请先点击“检测环境”，确认通过后再重试。底层信息：{exc}")
