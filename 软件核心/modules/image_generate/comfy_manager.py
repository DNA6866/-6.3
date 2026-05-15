import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


class ComfyUIError(Exception):
    pass


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def find_python_executable(comfyui_path):
    path = Path(comfyui_path)
    candidates = [
        path.parent / "python" / "python.exe",
        path / "python" / "python.exe",
        path.parent / "python_embeded" / "python.exe",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def check_comfyui_path(comfyui_path):
    path = Path(comfyui_path)
    if not path.exists():
        return False, "未找到 ComfyUI，请检查路径是否正确。"
    if not (path / "main.py").exists():
        return False, "ComfyUI 路径下未找到 main.py，请确认选择的是 ComfyUI 主目录。"
    return True, "ComfyUI 路径有效。"


def is_port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def is_api_available(host, port, timeout=2):
    try:
        with urllib.request.urlopen(f"http://{host}:{int(port)}/system_stats", timeout=timeout) as res:
            return res.status == 200
    except Exception:
        return False


class ComfyUIManager:
    def __init__(self, comfyui_path, host="127.0.0.1", port=8188):
        self.comfyui_path = os.path.abspath(comfyui_path)
        self.host = host
        self.port = int(port)
        self.process = None

    def validate(self):
        ok, msg = check_comfyui_path(self.comfyui_path)
        if not ok:
            raise ComfyUIError(msg)
        return True

    def is_running(self):
        return is_api_available(self.host, self.port)

    def start(self, timeout=90, extra_args=None):
        self.validate()
        if self.is_running():
            return True, "ComfyUI 已在后台运行。"

        if is_port_open(self.host, self.port):
            raise ComfyUIError("ComfyUI 启动失败：端口已被占用，但 API 不可访问。")

        python_exe = find_python_executable(self.comfyui_path)
        cmd = [
            python_exe,
            "main.py",
            "--listen",
            self.host,
            "--port",
            str(self.port),
            "--disable-all-custom-nodes",
        ]
        for arg in extra_args or []:
            if arg and arg not in cmd:
                cmd.append(str(arg))
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=self.comfyui_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            raise ComfyUIError(f"ComfyUI 启动失败，请检查运行环境。底层信息：{exc}")

        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.is_running():
                return True, "ComfyUI 后台服务启动成功。"
            if self.process and self.process.poll() is not None:
                raise ComfyUIError("ComfyUI 启动后立即退出，请检查 ComfyUI 依赖或模型环境。")
            time.sleep(1)
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
        raise ComfyUIError("ComfyUI 启动超时，请检查端口是否被占用或模型加载是否卡住。")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
            return True, "已停止本软件启动的 ComfyUI 后台进程。"
        return False, "当前没有由本软件启动的 ComfyUI 进程；如果是外部运行的 ComfyUI，请在对应程序里关闭。"
