import os
import subprocess
import sys
import webbrowser

from paths import APP_ROOT


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _absolute_path(path):
    raw_path = os.fspath(path) if path is not None else ""
    if not raw_path:
        return ""
    if os.path.isabs(raw_path):
        return os.path.abspath(raw_path)
    # 兼容旧配置中的相对路径，但不再依赖进程当前工作目录。
    return os.path.abspath(os.path.join(APP_ROOT, raw_path))


def open_path(path):
    """使用当前系统的默认程序打开文件或目录。"""
    target = _absolute_path(path)
    if not target:
        raise ValueError("打开路径不能为空")
    if not os.path.exists(target):
        raise FileNotFoundError(target)
    if os.name == "nt":
        os.startfile(target)
        return

    command = ["open", target] if sys.platform == "darwin" else ["xdg-open", target]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            startupinfo=_hidden_startupinfo(),
            timeout=10,
            check=False,
        )
    except Exception as exc:
        raise OSError(f"无法打开路径：{target}") from exc
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="ignore").strip()
        raise OSError(detail or f"无法打开路径：{target}")


def reveal_file(path):
    """在文件管理器中打开文件所在目录。"""
    target = _absolute_path(path)
    if not target:
        raise ValueError("文件路径不能为空")
    open_path(target if os.path.isdir(target) else os.path.dirname(target))


def open_url(url):
    """使用默认浏览器打开网址。"""
    target = str(url or "").strip()
    if not target or not webbrowser.open(target, new=2):
        raise OSError(f"无法打开网址：{target}")


def is_remote_path(file_path):
    """判断路径是否位于 UNC 或 Windows 网络映射盘。"""
    path = _absolute_path(str(file_path or ""))
    if not path:
        return False
    if path.startswith("\\\\"):
        return True
    if os.name != "nt":
        return False
    drive, _ = os.path.splitdrive(path)
    if not drive:
        return False
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")) == 4
    except Exception:
        return False


def resolve_network_path(path):
    """把 Windows 映射盘路径转换为更稳定的 UNC 路径。"""
    original = _absolute_path(str(path or ""))
    if not original:
        return ""
    if os.name != "nt" or original.startswith("\\\\"):
        return original
    drive, tail = os.path.splitdrive(original)
    if not drive:
        return original

    remote = ""
    try:
        import ctypes

        buffer_size = ctypes.c_ulong(2048)
        buffer = ctypes.create_unicode_buffer(buffer_size.value)
        result = ctypes.windll.mpr.WNetGetConnectionW(
            drive,
            buffer,
            ctypes.byref(buffer_size),
        )
        if result == 0:
            remote = buffer.value
    except Exception:
        remote = ""

    if not remote:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Network\{drive[0]}") as key:
                remote = str(winreg.QueryValueEx(key, "RemotePath")[0] or "")
        except Exception:
            remote = ""

    if not remote:
        return original
    return os.path.normpath(remote.rstrip("\\/") + tail)


def get_primary_screen_metrics():
    """返回 Windows 主屏幕宽高和系统 DPI，其他平台返回空值。"""
    if os.name != "nt":
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        try:
            dpi = int(user32.GetDpiForSystem())
        except Exception:
            dpi = 96
        return width, height, dpi
    except Exception:
        return None
