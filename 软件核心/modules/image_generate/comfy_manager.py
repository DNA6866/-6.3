import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    import psutil
except Exception:
    psutil = None


class ComfyUIError(Exception):
    pass


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _decode_log_bytes(raw):
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if text.count("\ufffd") > 5:
        try:
            return raw.decode("gbk", errors="replace")
        except Exception:
            pass
    return text


def read_log_tail(log_file, max_chars=5000):
    if not log_file or not os.path.exists(log_file):
        return ""
    try:
        size = os.path.getsize(log_file)
        with open(log_file, "rb") as handle:
            if size > max_chars * 4:
                handle.seek(max(0, size - max_chars * 4))
            raw = handle.read()
        text = _decode_log_bytes(raw).strip()
        return text[-max_chars:] if len(text) > max_chars else text
    except Exception as exc:
        return f"启动日志读取失败：{exc}"


def _returncode_u32(returncode):
    try:
        return int(returncode) & 0xFFFFFFFF
    except Exception:
        return 0


def _is_native_windows_crash(returncode):
    return _returncode_u32(returncode) in {
        0xC0000005,  # Access violation
        0xC0000017,  # Out of memory
        0xC0000409,  # Stack buffer overrun
    }


def _format_returncode(returncode):
    code = _returncode_u32(returncode)
    if os.name == "nt" and code >= 0x80000000:
        return f"{returncode} (0x{code:08X})"
    return str(returncode)


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


def _norm_path(path):
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return ""


def _cmdline_has_port(cmdline, port):
    port = str(int(port))
    parts = [str(item) for item in (cmdline or [])]
    for index, item in enumerate(parts):
        if item == "--port" and index + 1 < len(parts) and parts[index + 1] == port:
            return True
        if item.startswith("--port=") and item.split("=", 1)[1] == port:
            return True
    text = " ".join(parts)
    return f"--port {port}" in text or f"--port={port}" in text


def _cmdline_has_main_py(cmdline):
    for item in cmdline or []:
        name = os.path.basename(str(item)).lower()
        if name == "main.py":
            return True
    return " main.py " in f" {' '.join(str(x) for x in (cmdline or []))} "


def _process_matches_comfy(proc, comfyui_path, port):
    try:
        cmdline = proc.info.get("cmdline") if hasattr(proc, "info") else proc.cmdline()
        cwd = proc.info.get("cwd") if hasattr(proc, "info") else proc.cwd()
        exe = proc.info.get("exe") if hasattr(proc, "info") else proc.exe()
    except Exception:
        return False

    if not _cmdline_has_main_py(cmdline) or not _cmdline_has_port(cmdline, port):
        return False

    comfy_norm = _norm_path(comfyui_path)
    cwd_norm = _norm_path(cwd) if cwd else ""
    exe_norm = _norm_path(exe) if exe else ""
    if cwd_norm and comfy_norm and cwd_norm == comfy_norm:
        return True
    if exe_norm and comfy_norm:
        comfy_parent = _norm_path(os.path.dirname(comfy_norm))
        if exe_norm.startswith(comfy_norm + os.sep) or (comfy_parent and exe_norm.startswith(comfy_parent + os.sep)):
            return True
    for item in cmdline or []:
        item_norm = _norm_path(item)
        if item_norm and comfy_norm and item_norm.startswith(comfy_norm + os.sep):
            return True
    return False


def find_comfyui_pids(comfyui_path, port):
    """查找当前配置路径/端口对应的 ComfyUI 进程 PID。"""
    pids = []
    if psutil is not None:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd", "exe"]):
            try:
                if proc.pid == os.getpid():
                    continue
                if _process_matches_comfy(proc, comfyui_path, port):
                    pids.append(proc.pid)
            except Exception:
                continue
    else:
        pids.extend(_find_comfyui_pids_with_powershell(comfyui_path, port))
    return pids


def find_listening_pids(port):
    """查找监听指定端口的进程 PID。仅在 API 已确认是 ComfyUI 时作为兜底使用。"""
    pids = []
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if not conn.laddr or not conn.pid:
                    continue
                if int(conn.laddr.port) == int(port) and getattr(conn, "status", "") == psutil.CONN_LISTEN:
                    pids.append(conn.pid)
        except Exception:
            pass
    else:
        pids.extend(_find_listening_pids_with_powershell(port))
    return sorted(set(pids))


def _powershell_json(script):
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=hidden_startupinfo(),
            timeout=10,
        )
        text = (result.stdout or "").strip()
        if not text:
            return []
        data = json.loads(text)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
        return [data]
    except Exception:
        return []
    return []


def _find_comfyui_pids_with_powershell(comfyui_path, port):
    port = int(port)
    script = (
        "$items = Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.CommandLine -and $_.CommandLine -like '*main.py*' -and $_.CommandLine -like '*--port*' -and $_.CommandLine -like '*{port}*' }} | "
        "Select-Object ProcessId,CommandLine,ExecutablePath; "
        "$items | ConvertTo-Json -Compress"
    )
    comfy_norm = _norm_path(comfyui_path)
    comfy_parent = _norm_path(os.path.dirname(comfy_norm))
    pids = []
    for item in _powershell_json(script):
        try:
            pid = int(item.get("ProcessId"))
            if pid == os.getpid():
                continue
            command = str(item.get("CommandLine") or "")
            exe_norm = _norm_path(item.get("ExecutablePath") or "")
            command_lower = command.lower()
            if not _cmdline_has_port([command], port) or "main.py" not in command_lower:
                continue
            if (exe_norm and comfy_parent and exe_norm.startswith(comfy_parent + os.sep)) or (
                comfy_norm and comfy_norm.lower() in command_lower
            ):
                pids.append(pid)
        except Exception:
            continue
    return sorted(set(pids))


def _find_listening_pids_with_powershell(port):
    port = int(port)
    script = (
        f"$items = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty OwningProcess -Unique; "
        "$items | ConvertTo-Json -Compress"
    )
    data = _powershell_json(script)
    pids = []
    for item in data:
        try:
            if isinstance(item, dict):
                continue
            pid = int(item)
            if pid != os.getpid():
                pids.append(pid)
        except Exception:
            continue
    if pids:
        return sorted(set(pids))

    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=hidden_startupinfo(),
            timeout=10,
        )
        needle = f":{port}"
        for line in (result.stdout or "").splitlines():
            if needle not in line or "LISTENING" not in line.upper():
                continue
            parts = line.split()
            if parts:
                pid = int(parts[-1])
                if pid != os.getpid():
                    pids.append(pid)
    except Exception:
        pass
    return sorted(set(pids))


def terminate_pid_tree(pid, timeout=8):
    if psutil is not None:
        try:
            proc = psutil.Process(int(pid))
            children = proc.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except Exception:
                    pass
            try:
                proc.terminate()
            except Exception:
                pass
            gone, alive = psutil.wait_procs(children + [proc], timeout=timeout)
            for item in alive:
                try:
                    item.kill()
                except Exception:
                    pass
            return True
        except psutil.NoSuchProcess:
            return True
        except Exception:
            pass

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(),
                check=False,
            )
            return True
        except Exception:
            return False
    try:
        os.kill(int(pid), 15)
        return True
    except Exception:
        return False


class ComfyUIManager:
    def __init__(self, comfyui_path, host="127.0.0.1", port=8188):
        self.comfyui_path = os.path.abspath(comfyui_path)
        self.host = host
        self.port = int(port)
        self.process = None
        self._log_handle = None

    def validate(self):
        ok, msg = check_comfyui_path(self.comfyui_path)
        if not ok:
            raise ComfyUIError(msg)
        return True

    def is_running(self):
        return is_api_available(self.host, self.port)

    def _open_startup_log(self, log_file, cmd, append=False):
        if not log_file:
            return None
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            with open(log_file, "a" if append else "w", encoding="utf-8", errors="replace") as f:
                if append:
                    f.write("\n\n【兼容模式自动重试】\n")
                else:
                    f.write("【ComfyUI 启动日志】\n")
                f.write(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"工作目录：{self.comfyui_path}\n")
                f.write(f"监听地址：http://{self.host}:{self.port}\n")
                f.write(f"启动命令：{' '.join(cmd)}\n")
                f.write("\n【底层输出】\n")
            return open(log_file, "ab", buffering=0)
        except Exception:
            return None

    def _close_startup_log(self):
        if self._log_handle:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def _launch(self, cmd, log_file, append=False):
        self._close_startup_log()
        self._log_handle = self._open_startup_log(log_file, cmd, append=append)
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=self.comfyui_path,
                stdout=self._log_handle or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if self._log_handle else subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            self._close_startup_log()
            raise ComfyUIError(f"ComfyUI 启动失败，请检查运行环境。底层信息：{exc}")

    def _wait_for_start(self, timeout):
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.is_running():
                return True, None
            if self.process and self.process.poll() is not None:
                return False, self.process.returncode
            time.sleep(1)
        return False, None

    @staticmethod
    def _compatibility_command(cmd):
        result = []
        remove_flags = {
            "--cuda-malloc",
            "--enable-dynamic-vram",
            "--force-non-blocking",
        }
        index = 0
        while index < len(cmd):
            item = cmd[index]
            value = str(item)
            if value == "--async-offload":
                index += 1
                if index < len(cmd) and not str(cmd[index]).startswith("--"):
                    index += 1
                continue
            if value in remove_flags:
                index += 1
                continue
            result.append(value)
            index += 1
        for flag in (
            "--disable-cuda-malloc",
            "--disable-async-offload",
            "--disable-dynamic-vram",
            "--disable-pinned-memory",
        ):
            if flag not in result:
                result.append(flag)
        return result

    def _failure_message(self, returncode, log_file, prefix="ComfyUI 启动后立即退出"):
        detail = read_log_tail(log_file, max_chars=3500)
        message = f"{prefix}，退出码：{_format_returncode(returncode)}。"
        if detail:
            message += f"\n\n【ComfyUI 启动日志末尾】\n{detail}"
        else:
            message += "\n启动日志为空，请检查内置 Python、系统运行库或安全软件拦截。"
        return message

    def start(self, timeout=90, extra_args=None, log_file=None):
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
        self._launch(cmd, log_file)
        started, returncode = self._wait_for_start(timeout)
        if started:
            return True, "ComfyUI 后台服务启动成功。"

        if returncode is not None:
            self._close_startup_log()
            if _is_native_windows_crash(returncode):
                compat_cmd = self._compatibility_command(cmd)
                self._launch(compat_cmd, log_file, append=True)
                compat_started, compat_returncode = self._wait_for_start(min(timeout, 90))
                if compat_started:
                    return True, "ComfyUI 兼容模式启动成功，已关闭不稳定的异步显存优化。"
                if compat_returncode is not None:
                    self._close_startup_log()
                    raise ComfyUIError(
                        self._failure_message(
                            compat_returncode,
                            log_file,
                            prefix="ComfyUI 普通模式和兼容模式均立即退出",
                        )
                    )
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                self._close_startup_log()
                raise ComfyUIError(
                    "ComfyUI 兼容模式启动超时。"
                    f"\n\n【ComfyUI 启动日志末尾】\n{read_log_tail(log_file, max_chars=3500)}"
                )
            raise ComfyUIError(self._failure_message(returncode, log_file))

        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self._close_startup_log()
        raise ComfyUIError("ComfyUI 启动超时，请检查端口是否被占用或模型加载是否卡住。")

    def stop(self):
        stopped_pids = []
        if self.process and self.process.poll() is not None:
            self._close_startup_log()
        if self.process and self.process.poll() is None:
            pid = self.process.pid
            if terminate_pid_tree(pid):
                stopped_pids.append(pid)
            self.process = None
            self._close_startup_log()

        candidate_pids = find_comfyui_pids(self.comfyui_path, self.port)
        if self.is_running():
            candidate_pids.extend(find_listening_pids(self.port))

        for pid in sorted(set(candidate_pids)):
            if pid in stopped_pids or pid == os.getpid():
                continue
            if terminate_pid_tree(pid):
                stopped_pids.append(pid)

        if stopped_pids:
            deadline = time.time() + 10
            while time.time() < deadline:
                if not self.is_running():
                    break
                time.sleep(0.5)
            if self.is_running():
                return False, f"已尝试停止 ComfyUI 进程（PID: {', '.join(map(str, stopped_pids))}），但 API 仍可访问。"
            return True, f"已停止 ComfyUI 后台进程（PID: {', '.join(map(str, stopped_pids))}）。"

        if not self.is_running():
            return False, "ComfyUI 当前未运行。"
        return False, "ComfyUI API 仍可访问，但未找到匹配的后台进程；可能是外部程序或权限限制。"
