import hashlib
import json
import os
import threading
import time
from typing import Dict, Iterable


_WRITE_LOCK = threading.RLock()


def stable_job_key(parts: Iterable[object]) -> str:
    """把任务关键参数转换成稳定键，供中断恢复时精确匹配。"""
    payload = json.dumps(
        list(parts),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_identity(path: str) -> Dict[str, object]:
    """记录源文件身份；文件变化后不会误用旧任务进度。"""
    absolute = os.path.abspath(str(path or ""))
    try:
        stat = os.stat(absolute)
        mtime_ns = int(
            getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
        )
        return {
            "path": os.path.normcase(absolute),
            "size": int(stat.st_size),
            "mtime_ns": mtime_ns,
        }
    except OSError:
        return {"path": os.path.normcase(absolute), "size": 0, "mtime_ns": 0}


class JobResumeStore:
    """小型原子任务清单，仅恢复同一批且尚未完成的任务。"""

    def __init__(self, path: str, fingerprint: str):
        self.path = os.path.abspath(path)
        self.fingerprint = str(fingerprint or "")
        self.resumed = False
        self.data = {
            "version": 1,
            "fingerprint": self.fingerprint,
            "finished": False,
            "updated_at": time.time(),
            "jobs": {},
        }
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return
            if str(payload.get("fingerprint") or "") != self.fingerprint:
                return
            if bool(payload.get("finished", False)):
                return
            jobs = payload.get("jobs")
            if not isinstance(jobs, dict):
                return
            self.data = payload
            self.resumed = True
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return

    def job(self, key: str) -> Dict[str, object]:
        jobs = self.data.get("jobs")
        if not isinstance(jobs, dict):
            return {}
        value = jobs.get(str(key))
        return dict(value) if isinstance(value, dict) else {}

    def update(
        self,
        key: str,
        status: str,
        output_path: str = "",
        error: str = "",
    ) -> None:
        jobs = self.data.setdefault("jobs", {})
        current = jobs.get(str(key))
        record = dict(current) if isinstance(current, dict) else {}
        record.update(
            {
                "status": str(status or "pending"),
                "output": os.path.abspath(output_path) if output_path else "",
                "error": str(error or "")[-1000:],
                "updated_at": time.time(),
            }
        )
        jobs[str(key)] = record
        self.data["updated_at"] = time.time()
        self.data["finished"] = False
        self.save()

    def finish(self, completed: bool) -> None:
        self.data["finished"] = bool(completed)
        self.data["updated_at"] = time.time()
        self.save()

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        temporary = self.path + f".{os.getpid()}.tmp"
        try:
            os.makedirs(directory, exist_ok=True)
            with _WRITE_LOCK:
                with open(temporary, "w", encoding="utf-8") as handle:
                    json.dump(
                        self.data,
                        handle,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                os.replace(temporary, self.path)
        except (OSError, TypeError, ValueError):
            try:
                if os.path.isfile(temporary):
                    os.remove(temporary)
            except OSError:
                pass
