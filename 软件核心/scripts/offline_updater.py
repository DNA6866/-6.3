import argparse
import atexit
import csv
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path


APP_EXE = "牛爷爷电商视频工具箱.exe"
CORE_DIR = "软件核心依赖"
RESOURCE_DIR = "网盘资源包"
WORKSPACE_DIR = "用户工作区"
PAYLOAD_DIR = "更新文件"
PAYLOAD_ZIP = f"{PAYLOAD_DIR}.zip"


def updater_root():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def hide_console_window():
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    except Exception:
        pass


def enable_high_dpi():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def directory_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def iter_files(path):
    base = Path(path)
    if not base.exists():
        return
    for item in base.rglob("*"):
        if item.is_file():
            yield item


def safe_extract_zip(zip_path, extract_root):
    zip_path = Path(zip_path)
    extract_root = Path(extract_root).resolve()
    with zipfile.ZipFile(zip_path, "r") as archive:
        for info in archive.infolist():
            if not info.filename or info.filename.endswith("/"):
                continue
            target = (extract_root / info.filename).resolve()
            if target != extract_root and extract_root not in target.parents:
                raise RuntimeError(f"更新压缩包包含非法路径：{info.filename}")
        archive.extractall(extract_root)


def prepare_payload_root(package_root, requested_payload=None):
    if requested_payload:
        requested = Path(requested_payload).resolve()
        if requested.is_file() and requested.suffix.lower() == ".zip":
            zip_path = requested
        elif requested.is_dir():
            return requested
        else:
            raise RuntimeError(f"指定的更新载荷不存在：{requested}")
    else:
        zip_path = package_root / PAYLOAD_ZIP
        if not zip_path.is_file():
            folder = package_root / PAYLOAD_DIR
            if folder.is_dir():
                return folder
            raise RuntimeError(f"更新器旁边缺少“{PAYLOAD_ZIP}”或“{PAYLOAD_DIR}”文件夹。")

    temp_root = Path(tempfile.mkdtemp(prefix="nyy_update_payload_"))
    atexit.register(lambda: shutil.rmtree(temp_root, ignore_errors=True))
    safe_extract_zip(zip_path, temp_root)
    nested_payload = temp_root / PAYLOAD_DIR
    if (nested_payload / APP_EXE).is_file():
        return nested_payload
    if (temp_root / APP_EXE).is_file():
        return temp_root
    raise RuntimeError(f"更新压缩包格式错误：缺少 {APP_EXE}。")


class UpdateLogger:
    def __init__(self, target_dir=None):
        self.target_dir = Path(target_dir) if target_dir else None
        self.lines = []
        self.lock = threading.Lock()

    def set_target(self, target_dir):
        self.target_dir = Path(target_dir)

    def write(self, message):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
        with self.lock:
            self.lines.append(line)
        self.flush()

    def flush(self):
        if not self.target_dir:
            return
        log_dir = self.target_dir / WORKSPACE_DIR / "日志"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "offline_update_latest.log"
            log_path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        except Exception:
            pass


def load_package_version(payload_root):
    version_file = payload_root / CORE_DIR / "_package_version.json"
    try:
        data = json.loads(version_file.read_text(encoding="utf-8-sig"))
        return str(data.get("package_version") or "未知版本")
    except Exception:
        return "未知版本"


def load_hash_manifest(payload_root):
    manifest = payload_root / CORE_DIR / "_package_sha256.txt"
    if not manifest.is_file():
        raise RuntimeError("更新包缺少文件校验清单。")
    entries = []
    for raw_line in manifest.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise RuntimeError("更新包校验清单格式错误。")
        entries.append((parts[0].upper(), parts[1].strip()))
    if not entries:
        raise RuntimeError("更新包校验清单为空。")
    return entries


def validate_payload(payload_root, logger, progress=None):
    payload_root = Path(payload_root)
    if not (payload_root / APP_EXE).is_file():
        raise RuntimeError(f"更新文件中缺少 {APP_EXE}。")
    if not (payload_root / CORE_DIR).is_dir():
        raise RuntimeError(f"更新文件中缺少 {CORE_DIR}。")

    entries = load_hash_manifest(payload_root)
    logger.write(f"开始校验更新包，共 {len(entries)} 个关键文件。")
    for index, (expected, relative_path) in enumerate(entries, 1):
        path = payload_root / relative_path
        if not path.is_file():
            raise RuntimeError(f"更新包文件缺失：{relative_path}")
        if file_sha256(path) != expected:
            raise RuntimeError(f"更新包文件损坏：{relative_path}")
        if progress:
            progress(index, len(entries), f"正在校验：{relative_path}")
    logger.write("更新包完整性校验通过。")
    return entries


def is_valid_target(path):
    target = Path(path)
    return target.is_dir() and (target / APP_EXE).is_file()


def limited_search(base, payload_root, max_depth=3):
    base = Path(base)
    payload_root = Path(payload_root).resolve()
    if not base.is_dir():
        return []
    ignored = {
        "$RECYCLE.BIN",
        "System Volume Information",
        WORKSPACE_DIR,
        RESOURCE_DIR,
        CORE_DIR,
        PAYLOAD_DIR,
        "__pycache__",
    }
    results = []

    def walk(current, depth):
        try:
            resolved = current.resolve()
        except OSError:
            return
        if resolved == payload_root or payload_root in resolved.parents:
            return
        if (current / APP_EXE).is_file():
            results.append(current)
            return
        if depth >= max_depth:
            return
        try:
            children = list(current.iterdir())
        except (OSError, PermissionError):
            return
        for child in children:
            if not child.is_dir() or child.name in ignored or child.name.startswith("."):
                continue
            walk(child, depth + 1)

    walk(base, 0)
    return results


def find_installations(package_root, payload_root):
    package_root = Path(package_root).resolve()
    search_roots = [package_root, package_root.parent]
    home = Path.home()
    search_roots.extend([home / "Desktop", home / "Downloads"])
    seen_roots = set()
    candidates = []
    seen_candidates = set()
    for root in search_roots:
        try:
            key = str(root.resolve()).lower()
        except OSError:
            continue
        if key in seen_roots:
            continue
        seen_roots.add(key)
        for candidate in limited_search(root, payload_root):
            candidate_key = str(candidate.resolve()).lower()
            if candidate_key not in seen_candidates:
                seen_candidates.add(candidate_key)
                candidates.append(candidate)
    return candidates


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def list_app_pids(target_root=None):
    if os.name != "nt":
        return []
    target_exe = ""
    if target_root:
        target_exe = str((Path(target_root).resolve() / APP_EXE)).replace("'", "''")
    app_name = APP_EXE.replace("'", "''")
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"$target='{target_exe}';"
        f"$items=Get-CimInstance Win32_Process | Where-Object {{ $_.Name -eq '{app_name}' -and "
        "([string]::IsNullOrWhiteSpace($target) -or [string]::IsNullOrWhiteSpace($_.ExecutablePath) -or "
        "[string]::Equals([IO.Path]::GetFullPath($_.ExecutablePath),$target,[StringComparison]::OrdinalIgnoreCase)) }} | "
        "Select-Object -ExpandProperty ProcessId;"
        "@($items) | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=_hidden_startupinfo(),
            timeout=10,
            check=False,
        )
        text = (result.stdout or "").strip().lstrip("\ufeff")
        data = json.loads(text) if text else []
        if not isinstance(data, list):
            data = [data]
        return sorted({int(pid) for pid in data if str(pid).isdigit()})
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {APP_EXE}", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="mbcs",
            errors="ignore",
            startupinfo=_hidden_startupinfo(),
            timeout=10,
            check=False,
        )
        pids = []
        for row in csv.reader((result.stdout or "").splitlines()):
            if len(row) < 2:
                continue
            try:
                pids.append(int(row[1]))
            except (TypeError, ValueError):
                continue
        return sorted(set(pids))
    except Exception:
        return []


def terminate_app(target_root, logger):
    if os.name != "nt":
        return
    pids = list_app_pids(target_root)
    if not pids:
        logger.write("未发现正在运行的旧版软件进程。")
        return

    logger.write(f"发现旧版软件进程 {', '.join(map(str, pids))}，正在关闭进程树。")
    for pid in pids:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="mbcs",
            errors="ignore",
            startupinfo=_hidden_startupinfo(),
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            logger.write(f"关闭进程 PID {pid} 返回码 {result.returncode}：{(result.stdout or '').strip()}")

    deadline = time.time() + 20
    remaining = list_app_pids(target_root)
    while remaining and time.time() < deadline:
        time.sleep(0.8)
        remaining = list_app_pids(target_root)
    if remaining:
        raise RuntimeError(
            "旧版软件进程仍未退出，无法替换正在使用的程序文件。"
            f"\n残留 PID：{', '.join(map(str, remaining))}"
            "\n请在任务管理器结束“牛爷爷电商视频工具箱.exe”，"
            "再右键选择“以管理员身份运行”一键更新。"
        )
    logger.write("旧版软件进程已全部退出。")


def retry_file_operation(action, operation, attempts=12, delay=1.0):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except OSError as exc:
            last_error = exc
            retryable = getattr(exc, "winerror", None) in (5, 32, 33, 145) or isinstance(exc, PermissionError)
            if not retryable or attempt >= attempts:
                break
            time.sleep(delay)
    hint = (
        "\n请确认软件已完全退出；如果仍失败，请右键“一键更新.exe”选择“以管理员身份运行”，"
        "并暂时允许安全软件访问该目录。"
    )
    raise RuntimeError(f"{action}失败：{last_error}{hint}") from last_error


def move_path(source, target, action):
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    retry_file_operation(action, lambda: os.replace(str(source), str(target)))


def remove_path(path):
    path = Path(path)
    if not path.exists():
        return
    def remove():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    retry_file_operation(f"删除 {path}", remove)


def copy_file(source, target):
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    retry_file_operation(
        f"复制 {source.name}",
        lambda: shutil.copy2(source, target),
    )


def copy_tree(source, target, on_file=None):
    source = Path(source)
    target = Path(target)
    files = list(iter_files(source))
    for index, source_file in enumerate(files, 1):
        relative = source_file.relative_to(source)
        copy_file(source_file, target / relative)
        if on_file:
            on_file(index, len(files), str(relative))


def backup_resource_patch(payload_root, target_root, backup_root, state, logger):
    patch_root = payload_root / RESOURCE_DIR
    if not patch_root.is_dir():
        return
    original_root = backup_root / "resource_originals"
    for source_file in iter_files(patch_root):
        relative = source_file.relative_to(patch_root)
        target_file = target_root / RESOURCE_DIR / relative
        if target_file.exists():
            copy_file(target_file, original_root / relative)
            state["resource_existing"].append(str(relative))
        else:
            state["resource_new"].append(str(relative))
    logger.write(
        f"资源补丁备份完成：原有 {len(state['resource_existing'])} 个，新增 {len(state['resource_new'])} 个。"
    )


def restore_resource_patch(target_root, backup_root, state, logger):
    original_root = backup_root / "resource_originals"
    for relative in state.get("resource_new", []):
        remove_path(target_root / RESOURCE_DIR / relative)
    for relative in state.get("resource_existing", []):
        source = original_root / relative
        if source.is_file():
            copy_file(source, target_root / RESOURCE_DIR / relative)
    logger.write("网盘资源补丁已回滚。")


def write_state(state_path, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def rollback_update(target_root, backup_root, temp_core, temp_exe, state, logger):
    logger.write("更新失败，正在自动恢复旧版本。")
    try:
        remove_path(temp_core)
        remove_path(temp_exe)
        old_core = backup_root / CORE_DIR
        old_exe = backup_root / APP_EXE
        if state.get("new_core_installed") or state.get("old_core_backed_up"):
            remove_path(target_root / CORE_DIR)
        if state.get("new_exe_installed") or state.get("old_exe_backed_up"):
            remove_path(target_root / APP_EXE)
        if state.get("old_core_backed_up") and old_core.exists():
            move_path(old_core, target_root / CORE_DIR, "恢复旧版软件核心")
        if state.get("old_exe_backed_up") and old_exe.exists():
            move_path(old_exe, target_root / APP_EXE, "恢复旧版主程序")
        restore_resource_patch(target_root, backup_root, state, logger)
        logger.write("旧版本恢复完成。")
    except Exception as rollback_error:
        logger.write(f"自动回滚未完全完成：{rollback_error}")


def _installed_file_issues(target_root, entries):
    issues = []
    for expected, relative_path in entries:
        target_file = target_root / relative_path
        if not target_file.is_file():
            issues.append((expected, relative_path, "缺失"))
            continue
        try:
            actual = file_sha256(target_file)
        except OSError as exc:
            issues.append((expected, relative_path, f"无法读取：{exc}"))
            continue
        if actual != expected:
            issues.append((expected, relative_path, "哈希不一致"))
    return issues


def _repair_installed_file(payload_root, target_root, expected, relative_path, logger):
    source_file = payload_root / relative_path
    target_file = target_root / relative_path
    if not source_file.is_file():
        raise RuntimeError(f"已校验的更新载荷中也缺少：{relative_path}")
    if file_sha256(source_file) != expected:
        raise RuntimeError(f"更新载荷在安装过程中被改动：{relative_path}")

    target_file.parent.mkdir(parents=True, exist_ok=True)
    staging_file = target_file.with_name(
        f"{target_file.name}.__repair_{os.getpid()}_{time.time_ns()}"
    )
    last_error = None
    for attempt in range(1, 4):
        try:
            copy_file(source_file, staging_file)
            if file_sha256(staging_file) != expected:
                raise RuntimeError("补齐临时文件哈希不一致")
            retry_file_operation(
                f"补齐 {relative_path}",
                lambda: os.replace(str(staging_file), str(target_file)),
                attempts=6,
                delay=0.8,
            )
            if target_file.is_file() and file_sha256(target_file) == expected:
                logger.write(f"安装校验自动补齐成功：{relative_path}")
                return
            raise RuntimeError("补齐后文件再次缺失或哈希异常")
        except (OSError, RuntimeError) as exc:
            last_error = exc
            try:
                staging_file.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt < 3:
                time.sleep(1.2)
    raise RuntimeError(f"自动补齐失败：{relative_path}；{last_error}") from last_error


def verify_installed_files(target_root, entries, logger, payload_root=None):
    issues = _installed_file_issues(target_root, entries)
    if issues and payload_root is not None:
        logger.write(
            f"安装校验发现 {len(issues)} 个文件异常，正在从已校验载荷自动补齐。"
        )
        repair_errors = []
        for expected, relative_path, reason in issues:
            logger.write(f"准备补齐：{relative_path}（{reason}）")
            try:
                _repair_installed_file(
                    Path(payload_root),
                    Path(target_root),
                    expected,
                    relative_path,
                    logger,
                )
            except Exception as exc:
                repair_errors.append(f"{relative_path}：{exc}")
        issues = _installed_file_issues(target_root, entries)
        if issues:
            details = repair_errors or [
                f"{relative_path}（{reason}）"
                for _, relative_path, reason in issues
            ]
            raise RuntimeError(
                "安装后文件被拦截或无法落盘："
                + "；".join(details[:6])
                + "\n请打开 Windows 安全中心或第三方安全软件的隔离记录，"
                "允许牛爷爷电商视频工具箱目录后重新更新。"
            )
    elif issues:
        _, relative_path, reason = issues[0]
        raise RuntimeError(f"安装后文件{reason}：{relative_path}")
    logger.write("安装后的关键文件校验通过。")


def launch_and_check(target_root, logger, wait_seconds=8):
    executable = target_root / APP_EXE
    logger.write("正在启动最新版软件进行检查。")
    process = subprocess.Popen([str(executable)], cwd=str(target_root))
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"最新版软件启动后提前退出，退出码：{code}")
        time.sleep(0.5)
    logger.write("最新版软件启动检查通过。")
    return process


def perform_update(target_dir, payload_dir, launch_after=True, callback=None):
    target_root = Path(target_dir).resolve()
    payload_root = Path(payload_dir).resolve()
    logger = UpdateLogger(target_root)
    version = load_package_version(payload_root)

    def report(percent, message):
        logger.write(message)
        if callback:
            callback(percent, message)

    if not is_valid_target(target_root):
        raise RuntimeError(f"选择的目录中没有 {APP_EXE}。")
    if target_root == payload_root or payload_root in target_root.parents:
        raise RuntimeError("不能把更新文件目录本身作为软件安装目录。")

    logger.write(f"准备更新到版本 {version}。")
    entries = validate_payload(
        payload_root,
        logger,
        lambda current, total, text: callback(
            int(current / max(total, 1) * 10), text
        )
        if callback
        else None,
    )

    required_bytes = directory_size(payload_root)
    free_bytes = shutil.disk_usage(target_root).free
    if free_bytes < required_bytes + 256 * 1024 * 1024:
        raise RuntimeError("安装盘剩余空间不足，请至少预留更新包大小再加 256MB。")

    workspace = target_root / WORKSPACE_DIR
    backup_parent = workspace / "更新备份"
    backup_root = backup_parent / f"{datetime.now():%Y%m%d_%H%M%S}_{version}"
    temp_core = target_root / f"{CORE_DIR}.__updating__"
    temp_exe = target_root / f"{APP_EXE}.__updating__"
    state_path = backup_root / "update_state.json"
    state = {
        "version": version,
        "target": str(target_root),
        "resource_existing": [],
        "resource_new": [],
        "old_exe_backed_up": False,
        "old_core_backed_up": False,
        "new_exe_installed": False,
        "new_core_installed": False,
    }

    backup_root.mkdir(parents=True, exist_ok=False)
    write_state(state_path, state)
    terminate_app(target_root, logger)

    try:
        report(12, "正在备份旧版主程序和软件核心。")
        old_exe = target_root / APP_EXE
        old_core = target_root / CORE_DIR
        if old_exe.exists():
            move_path(old_exe, backup_root / APP_EXE, "备份旧版主程序")
            state["old_exe_backed_up"] = True
            write_state(state_path, state)
        if old_core.exists():
            move_path(old_core, backup_root / CORE_DIR, "备份旧版软件核心")
            state["old_core_backed_up"] = True
            write_state(state_path, state)

        backup_resource_patch(payload_root, target_root, backup_root, state, logger)
        write_state(state_path, state)

        remove_path(temp_core)
        remove_path(temp_exe)
        report(20, "正在复制最新版主程序。")
        copy_file(payload_root / APP_EXE, temp_exe)

        def core_progress(current, total, relative):
            percent = 20 + int(current / max(total, 1) * 65)
            if callback:
                callback(percent, f"正在更新软件核心：{relative}")

        copy_tree(payload_root / CORE_DIR, temp_core, core_progress)
        move_path(temp_exe, target_root / APP_EXE, "安装最新版主程序")
        state["new_exe_installed"] = True
        write_state(state_path, state)
        move_path(temp_core, target_root / CORE_DIR, "安装最新版软件核心")
        state["new_core_installed"] = True
        write_state(state_path, state)

        patch_root = payload_root / RESOURCE_DIR
        if patch_root.is_dir():
            report(87, "正在合并网盘资源补丁。")
            copy_tree(patch_root, target_root / RESOURCE_DIR)

        report(92, "正在校验安装结果。")
        verify_installed_files(target_root, entries, logger, payload_root)

        if launch_after:
            report(96, "正在启动最新版软件。")
            launch_and_check(target_root, logger)

        report(100, f"更新完成，当前版本：{version}")
        try:
            shutil.rmtree(backup_root)
            if backup_parent.is_dir() and not any(backup_parent.iterdir()):
                backup_parent.rmdir()
        except Exception as cleanup_error:
            logger.write(f"旧版临时备份未能清理，可稍后手动删除：{cleanup_error}")
        return version
    except Exception:
        rollback_update(target_root, backup_root, temp_core, temp_exe, state, logger)
        raise


class UpdaterWindow:
    def __init__(self, package_root, payload_root):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk = tk
        self.filedialog = filedialog
        self.messagebox = messagebox
        self.ttk = ttk
        self.package_root = Path(package_root)
        self.payload_root = Path(payload_root)
        self.version = load_package_version(self.payload_root)
        self.busy = False

        self.root = tk.Tk()
        self.root.title("牛爷爷工具箱离线更新器")
        self.root.geometry("680x360")
        self.root.minsize(620, 330)
        self.root.configure(bg="#F4F6F8")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"), background="#F4F6F8")
        style.configure("Body.TLabel", font=("Microsoft YaHei UI", 10), background="#F4F6F8")
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 10), background="#FFFFFF")
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(16, 8))

        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="离线一键更新", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text=f"目标版本：{self.version}    更新过程中会保留用户工作区，并自动回滚失败更新。",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(4, 18))

        path_frame = ttk.LabelFrame(outer, text="软件安装目录", padding=12)
        path_frame.pack(fill="x")
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(path_frame, textvariable=self.path_var, font=("Microsoft YaHei UI", 10))
        self.path_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.browse_button = ttk.Button(path_frame, text="选择目录", command=self.browse)
        self.browse_button.pack(side="right")

        status_frame = ttk.Frame(outer, padding=(12, 12))
        status_frame.pack(fill="both", expand=True, pady=(14, 12))
        self.status_var = tk.StringVar(value="正在查找软件安装目录...")
        ttk.Label(
            status_frame,
            textvariable=self.status_var,
            style="Status.TLabel",
            wraplength=590,
        ).pack(anchor="w")
        self.progress = ttk.Progressbar(status_frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(14, 0))

        self.start_button = ttk.Button(
            outer,
            text="开始更新",
            style="Accent.TButton",
            command=self.start_update,
        )
        self.start_button.pack(anchor="e")
        self.root.after(200, self.auto_detect)

    def auto_detect(self):
        candidates = find_installations(self.package_root, self.payload_root)
        if len(candidates) == 1:
            self.path_var.set(str(candidates[0]))
            self.status_var.set("已自动找到软件目录，请点击“开始更新”。")
        elif len(candidates) > 1:
            self.path_var.set(str(candidates[0]))
            self.status_var.set(f"找到 {len(candidates)} 个软件目录，请确认路径后开始更新。")
        else:
            self.status_var.set("未自动找到软件，请点击“选择目录”指定软件所在文件夹。")

    def browse(self):
        selected = self.filedialog.askdirectory(title="选择牛爷爷工具箱所在目录")
        if selected:
            self.path_var.set(selected)
            self.status_var.set("已选择软件目录，可以开始更新。")

    def set_progress(self, percent, message):
        self.root.after(0, lambda: self._apply_progress(percent, message))

    def _apply_progress(self, percent, message):
        self.progress["value"] = percent
        self.status_var.set(message)

    def start_update(self):
        target = self.path_var.get().strip()
        if not is_valid_target(target):
            self.messagebox.showerror("目录不正确", f"所选目录中没有 {APP_EXE}。")
            return
        if not self.messagebox.askyesno(
            "确认更新",
            f"即将更新：\n{target}\n\n"
            f"目标版本：{self.version}\n"
            "软件会被自动关闭，用户素材、配置和输出不会删除。",
        ):
            return
        self.busy = True
        self.start_button.configure(state="disabled")
        self.browse_button.configure(state="disabled")
        self.path_entry.configure(state="disabled")
        worker = threading.Thread(target=self.run_update, args=(target,), daemon=True)
        worker.start()

    def run_update(self, target):
        try:
            version = perform_update(target, self.payload_root, launch_after=True, callback=self.set_progress)
            self.root.after(0, lambda: self.finish_success(version))
        except Exception as exc:
            detail = f"{exc}\n\n详细日志：{Path(target) / WORKSPACE_DIR / '日志' / 'offline_update_latest.log'}"
            self.root.after(0, lambda: self.finish_error(detail))

    def finish_success(self, version):
        self.busy = False
        self.progress["value"] = 100
        self.status_var.set(f"更新成功，当前版本：{version}")
        self.messagebox.showinfo("更新完成", "更新成功，最新版软件已经启动。\n\n现在可以关闭更新器。")
        self.start_button.configure(text="关闭", state="normal", command=self.root.destroy)

    def finish_error(self, detail):
        self.busy = False
        self.status_var.set("更新失败，旧版本已自动恢复。")
        self.messagebox.showerror("更新失败", detail)
        self.start_button.configure(state="normal")
        self.browse_button.configure(state="normal")
        self.path_entry.configure(state="normal")

    def on_close(self):
        if self.busy:
            self.messagebox.showwarning("正在更新", "更新尚未完成，请不要关闭更新器。")
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target")
    parser.add_argument("--payload")
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--no-launch", action="store_true")
    args = parser.parse_args()

    package_root = updater_root()
    try:
        payload_root = prepare_payload_root(package_root, args.payload)
    except Exception as exc:
        message = str(exc)
        if args.silent:
            raise RuntimeError(message)
        from tkinter import messagebox

        messagebox.showerror("更新包不完整", message)
        return 2

    if args.target:
        perform_update(
            args.target,
            payload_root,
            launch_after=not args.no_launch,
            callback=None,
        )
        return 0

    window = UpdaterWindow(package_root, payload_root)
    window.run()
    return 0


if __name__ == "__main__":
    enable_high_dpi()
    if getattr(sys, "frozen", False):
        hide_console_window()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        error_path = updater_root() / "更新器错误日志.txt"
        try:
            error_path.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        if "--silent" not in sys.argv:
            try:
                from tkinter import messagebox

                messagebox.showerror("更新器错误", f"更新器运行失败。\n\n日志：{error_path}")
            except Exception:
                pass
        raise
