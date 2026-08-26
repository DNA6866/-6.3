import json
import os
import threading
import time
from dataclasses import dataclass

from paths import config_dir, logs_dir, workspace_path


CACHE_RETENTION_DAYS = 5
RUNTIME_LOG_RETENTION_DAYS = 5
FEEDBACK_LOG_RETENTION_DAYS = 30
MAINTENANCE_INTERVAL_HOURS = 20
SHARED_CACHE_LIMIT_BYTES = 40 * 1024**3
NAS_CACHE_LIMIT_BYTES = 30 * 1024**3
ACTIVE_FILE_GRACE_SECONDS = 2 * 3600
CACHE_POLICY_VERSION = 1
_CACHE_ACCESS_LOCK = threading.RLock()


@dataclass
class CacheMaintenanceResult:
    ran: bool = False
    files_removed: int = 0
    directories_removed: int = 0
    bytes_reclaimed: int = 0
    primed_files: int = 0
    error: str = ""

    @property
    def reclaimed_gb(self):
        return self.bytes_reclaimed / 1024**3

    def message(self):
        if self.error:
            return f"[缓存维护] 清理未完全完成：{self.error}"
        if not self.ran:
            return ""
        message = (
            f"[缓存维护] 已按最近 {CACHE_RETENTION_DAYS} 天使用记录完成整理："
            f"删除 {self.files_removed} 个过期文件，释放 {self.reclaimed_gb:.2f}GB。"
        )
        if self.primed_files:
            message += (
                f" 首次启用已保护 {self.primed_files} 个现有提速缓存，"
                f"{CACHE_RETENTION_DAYS} 天内未再使用才会删除。"
            )
        return message


def touch_cache_entry(path):
    """缓存命中后续期，维护程序据此保留仍在使用的文件。"""
    with _CACHE_ACCESS_LOCK:
        try:
            if path and os.path.exists(path):
                os.utime(path, None)
                return True
        except OSError:
            pass
    return False


def _safe_workspace_root(path):
    try:
        root = os.path.abspath(workspace_path())
        target = os.path.abspath(path)
        return os.path.commonpath((root, target)) == root
    except (OSError, ValueError):
        return False


def _remove_file(path, result, dry_run, not_newer_than=None):
    with _CACHE_ACCESS_LOCK:
        try:
            stat = os.stat(path)
        except OSError:
            return False
        if not_newer_than is not None and stat.st_mtime >= not_newer_than:
            return False
        if not dry_run:
            try:
                os.remove(path)
            except OSError:
                return False
    result.files_removed += 1
    result.bytes_reclaimed += max(0, int(stat.st_size))
    return True


def _remove_empty_directories(root, result, dry_run):
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        if os.path.abspath(current) == os.path.abspath(root):
            continue
        if directories or files:
            try:
                if os.listdir(current):
                    continue
            except OSError:
                continue
        if os.path.islink(current):
            continue
        if not dry_run:
            try:
                os.rmdir(current)
            except OSError:
                continue
        result.directories_removed += 1


def _prune_cache_tree(root, result, retention_days, max_bytes, dry_run=False):
    if not os.path.isdir(root) or not _safe_workspace_root(root):
        return
    now = time.time()
    cutoff = now - max(1, int(retention_days)) * 86400
    stale_temp_cutoff = now - 86400
    active_cutoff = now - ACTIVE_FILE_GRACE_SECONDS
    survivors = []

    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories
            if not os.path.islink(os.path.join(current, name))
        ]
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            lowered = name.lower()
            is_temporary = (
                lowered.endswith((".part", ".tmp"))
                or ".tmp_" in lowered
                or lowered.startswith("temp_")
            )
            expired = stat.st_mtime < (stale_temp_cutoff if is_temporary else cutoff)
            expiration_cutoff = stale_temp_cutoff if is_temporary else cutoff
            if expired and _remove_file(
                path,
                result,
                dry_run,
                not_newer_than=expiration_cutoff,
            ):
                continue
            survivors.append((stat.st_mtime, stat.st_size, path))

    total_size = sum(size for _mtime, size, _path in survivors)
    if max_bytes and total_size > max_bytes:
        for mtime, size, path in sorted(survivors):
            if total_size <= max_bytes:
                break
            # 两小时内刚写入或刚命中的缓存视为活跃文件，不参与容量淘汰。
            if mtime >= active_cutoff:
                continue
            if _remove_file(
                path,
                result,
                dry_run,
                not_newer_than=active_cutoff,
            ):
                total_size -= size
    _remove_empty_directories(root, result, dry_run)


def _log_retention_days(filename):
    lowered = filename.lower()
    if lowered == ".gitkeep":
        return 0
    if lowered.startswith("developer_feedback_"):
        return FEEDBACK_LOG_RETENTION_DAYS
    if lowered.startswith(("ffmpeg_fail_", "close_event_", "app_live_")):
        return RUNTIME_LOG_RETENTION_DAYS
    # 固定名称的 latest / 环境报告会被覆盖写入，不按历史文件删除。
    if "latest" in lowered or lowered.endswith("_env_report.txt"):
        return 0
    return FEEDBACK_LOG_RETENTION_DAYS


def _prune_logs(result, dry_run=False):
    root = logs_dir()
    if not os.path.isdir(root) or not _safe_workspace_root(root):
        return
    now = time.time()
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [
            name for name in directories
            if not os.path.islink(os.path.join(current, name))
        ]
        for name in files:
            retention_days = _log_retention_days(name)
            if retention_days <= 0:
                continue
            path = os.path.join(current, name)
            if os.path.islink(path):
                continue
            try:
                expired = os.path.getmtime(path) < now - retention_days * 86400
            except OSError:
                continue
            if expired:
                _remove_file(
                    path,
                    result,
                    dry_run,
                    not_newer_than=now - retention_days * 86400,
                )
    _remove_empty_directories(root, result, dry_run)


def _state_path():
    return os.path.join(config_dir(), "cache_maintenance.json")


def _lock_path():
    return os.path.join(config_dir(), ".cache_maintenance.lock")


def _load_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _maintenance_due():
    state = _load_state()
    try:
        last_run = float(state.get("last_run") or 0)
    except (TypeError, ValueError):
        last_run = 0
    return time.time() - last_run >= MAINTENANCE_INTERVAL_HOURS * 3600


def _prime_existing_video_caches(result):
    roots = (
        workspace_path("缓存", "HDR标准化素材"),
        workspace_path("缓存", "标准化视频素材"),
        workspace_path("缓存", "视频快速剪辑"),
        workspace_path("混剪实验室V2", "缓存", "NAS素材缓存"),
    )
    now = time.time()
    for root in roots:
        if not os.path.isdir(root) or not _safe_workspace_root(root):
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [
                name for name in directories
                if not os.path.islink(os.path.join(current, name))
            ]
            for name in files:
                lowered = name.lower()
                if lowered.endswith((".part", ".tmp")) or ".tmp_" in lowered:
                    continue
                path = os.path.join(current, name)
                if os.path.islink(path):
                    continue
                try:
                    os.utime(path, (now, now))
                    result.primed_files += 1
                except OSError:
                    continue


def _write_state(result):
    os.makedirs(config_dir(), exist_ok=True)
    previous = _load_state()
    payload = {
        "policy_version": CACHE_POLICY_VERSION,
        "policy_started_at": previous.get("policy_started_at") or time.time(),
        "last_run": time.time(),
        "cache_retention_days": CACHE_RETENTION_DAYS,
        "runtime_log_retention_days": RUNTIME_LOG_RETENTION_DAYS,
        "files_removed": result.files_removed,
        "bytes_reclaimed": result.bytes_reclaimed,
    }
    temp_path = _state_path() + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, _state_path())


def _acquire_lock():
    os.makedirs(config_dir(), exist_ok=True)
    path = _lock_path()
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) > 2 * 3600:
            os.remove(path)
    except OSError:
        pass
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, str(os.getpid()).encode("ascii", errors="ignore"))
        os.close(descriptor)
        return path
    except OSError:
        return ""


def run_cache_maintenance(force=False, dry_run=False):
    result = CacheMaintenanceResult()
    if not force and not _maintenance_due():
        return result
    lock_path = _acquire_lock()
    if not lock_path:
        return result
    try:
        result.ran = True
        state = _load_state()
        try:
            policy_version = int(state.get("policy_version", 0) or 0)
        except (TypeError, ValueError):
            policy_version = 0
        if not dry_run and policy_version < CACHE_POLICY_VERSION:
            _prime_existing_video_caches(result)
        _prune_cache_tree(
            workspace_path("缓存"),
            result,
            retention_days=CACHE_RETENTION_DAYS,
            max_bytes=SHARED_CACHE_LIMIT_BYTES,
            dry_run=dry_run,
        )
        _prune_cache_tree(
            workspace_path("混剪实验室V2", "缓存", "NAS素材缓存"),
            result,
            retention_days=CACHE_RETENTION_DAYS,
            max_bytes=NAS_CACHE_LIMIT_BYTES,
            dry_run=dry_run,
        )
        _prune_cache_tree(
            workspace_path("混剪实验室V2", "缓存", "render_temp"),
            result,
            retention_days=1,
            max_bytes=5 * 1024**3,
            dry_run=dry_run,
        )
        _prune_logs(result, dry_run=dry_run)
        if not dry_run:
            _write_state(result)
    except Exception as exc:
        result.error = str(exc)
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass
    return result
