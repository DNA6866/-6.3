from dataclasses import dataclass
import threading
import time
from typing import Callable, Dict, Iterable, Optional


@dataclass
class ManagedThread:
    worker: object
    label: str
    stop_callback: Optional[Callable[[], None]] = None


class ThreadLifecycleRegistry:
    """统一保存后台线程引用，并按“通知停止、等待、最终回收”顺序关闭。"""

    def __init__(self):
        self._items: Dict[int, ManagedThread] = {}
        self._lock = threading.RLock()

    def register(
        self,
        worker,
        label: str = "后台任务",
        stop_callback: Optional[Callable[[], None]] = None,
    ):
        if worker is None:
            return worker
        key = id(worker)
        with self._lock:
            self._items[key] = ManagedThread(worker, str(label), stop_callback)
        try:
            worker.finished.connect(lambda key=key: self.unregister(key))
        except (AttributeError, RuntimeError, TypeError):
            pass
        return worker

    def unregister(self, worker_or_key) -> None:
        key = worker_or_key if isinstance(worker_or_key, int) else id(worker_or_key)
        with self._lock:
            self._items.pop(key, None)

    def adopt_values(self, values: Iterable[object], prefix: str = "后台任务") -> None:
        seen = set()

        def visit(value, depth=0):
            if value is None or depth > 2 or id(value) in seen:
                return
            seen.add(id(value))
            if all(hasattr(value, name) for name in ("isRunning", "wait")):
                self.register(value, f"{prefix}:{value.__class__.__name__}")
                return
            if isinstance(value, dict):
                for item in value.values():
                    visit(item, depth + 1)
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    visit(item, depth + 1)

        for value in values:
            visit(value)

    @staticmethod
    def _is_running(worker) -> bool:
        try:
            return bool(worker.isRunning())
        except (AttributeError, RuntimeError):
            return False

    @staticmethod
    def _request_stop(item: ManagedThread) -> None:
        worker = item.worker
        try:
            if item.stop_callback:
                item.stop_callback()
            elif hasattr(worker, "stop"):
                worker.stop()
            elif hasattr(worker, "cancel"):
                worker.cancel()
        except Exception:
            pass
        try:
            if hasattr(worker, "requestInterruption"):
                worker.requestInterruption()
        except (AttributeError, RuntimeError):
            pass

    def shutdown(self, timeout_ms: int = 12000, force: bool = True):
        with self._lock:
            items = list(self._items.values())
        running = [item for item in items if self._is_running(item.worker)]
        for item in running:
            self._request_stop(item)

        deadline = time.monotonic() + max(0.0, float(timeout_ms) / 1000.0)
        while running and time.monotonic() < deadline:
            next_running = []
            for item in running:
                worker = item.worker
                if not self._is_running(worker):
                    continue
                try:
                    worker.wait(80)
                except (AttributeError, RuntimeError):
                    continue
                if self._is_running(worker):
                    next_running.append(item)
            running = next_running

        forced = []
        if running and force:
            for item in running:
                try:
                    item.worker.terminate()
                    item.worker.wait(1500)
                    forced.append(item.label)
                except (AttributeError, RuntimeError):
                    pass
        with self._lock:
            for item in items:
                if not self._is_running(item.worker):
                    self._items.pop(id(item.worker), None)
        remaining = [item.label for item in running if self._is_running(item.worker)]
        return {
            "requested": len(items),
            "forced": forced,
            "remaining": remaining,
        }
