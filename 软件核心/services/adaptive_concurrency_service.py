from dataclasses import dataclass
import os
import tempfile
import threading
import time
from typing import Iterable

from services.platform_service import is_remote_path


_BENCHMARK_LOCK = threading.RLock()
_BENCHMARK_CACHE = {}


@dataclass(frozen=True)
class ConcurrencyDecision:
    workers: int
    storage_mbps: float
    remote_sources: int
    reason: str


def _storage_write_mbps(directory: str) -> float:
    """用小文件顺序写入估算临时盘能力；结果缓存，避免每个任务重复测速。"""
    root = os.path.abspath(str(directory or ""))
    now = time.monotonic()
    with _BENCHMARK_LOCK:
        cached = _BENCHMARK_CACHE.get(root)
        if cached and now - cached[0] < 3600:
            return float(cached[1])
        try:
            os.makedirs(root, exist_ok=True)
            payload = b"\0" * (1024 * 1024)
            started = time.monotonic()
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix="render_disk_probe_",
                suffix=".tmp",
                dir=root,
                delete=False,
            ) as handle:
                path = handle.name
                for _ in range(8):
                    handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            elapsed = max(0.001, time.monotonic() - started)
            mbps = 8.0 / elapsed
            os.remove(path)
        except OSError:
            mbps = 0.0
            try:
                if "path" in locals() and os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        _BENCHMARK_CACHE[root] = (now, mbps)
        return mbps


def choose_render_concurrency(
    requested_workers: int,
    gpu_mode: str,
    cache_dir: str,
    source_paths: Iterable[str],
    auto_enabled: bool = True,
    fast_render_enabled: bool = True,
) -> ConcurrencyDecision:
    requested = max(1, min(16, int(requested_workers or 1)))
    sources = [str(path or "") for path in source_paths if path]
    remote_count = sum(1 for path in sources if is_remote_path(path))
    storage_mbps = _storage_write_mbps(cache_dir)
    if not auto_enabled:
        return ConcurrencyDecision(
            requested,
            storage_mbps,
            remote_count,
            "使用手动并发设置",
        )

    cpu_count = max(1, int(os.cpu_count() or 1))
    mode = str(gpu_mode or "")
    if "NVENC" in mode:
        workers = min(requested, 4 if cpu_count >= 16 else 3)
    elif "AMF" in mode:
        workers = min(requested, 3)
    else:
        workers = min(requested, max(1, cpu_count // 4), 4)

    reasons = []
    if storage_mbps and storage_mbps < 35:
        workers = min(workers, 1)
        reasons.append(f"临时盘写入仅 {storage_mbps:.0f}MB/s")
    elif storage_mbps and storage_mbps < 90:
        workers = min(workers, 2)
        reasons.append(f"临时盘写入约 {storage_mbps:.0f}MB/s")
    else:
        reasons.append(f"临时盘写入约 {storage_mbps:.0f}MB/s" if storage_mbps else "临时盘测速不可用")

    if remote_count:
        remote_ratio = remote_count / max(1, len(sources))
        if remote_ratio >= 0.5:
            workers = min(workers, 2)
        reasons.append(f"检测到 {remote_count} 个网络素材")
    if fast_render_enabled:
        reasons.append("已启用单进程快速管线")
    return ConcurrencyDecision(
        workers=max(1, workers),
        storage_mbps=storage_mbps,
        remote_sources=remote_count,
        reason="；".join(reasons),
    )
