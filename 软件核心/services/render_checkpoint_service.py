import hashlib
import json
import os
import re
import threading
from datetime import datetime

from paths import config_dir


CHECKPOINT_VERSION = 1


def checkpoint_path_for_task(task_id):
    safe_id = "".join(
        char for char in str(task_id or "") if char.isalnum() or char in "-_"
    )[:80]
    if not safe_id:
        return ""
    return os.path.join(
        config_dir(),
        "batch_queue_checkpoints",
        f"{safe_id}.json",
    )


def ensure_resume_source_ids(task_data):
    """给音频/第一轨道建立稳定编号，切换机器或暂存 NAS 后仍可识别。"""
    if not isinstance(task_data, dict):
        return task_data
    audios = task_data.get("audios")
    if not isinstance(audios, list):
        return task_data
    for index, item in enumerate(audios):
        if not isinstance(item, dict) or item.get("resume_source_id"):
            continue
        source = "|".join((
            str(index),
            os.path.normcase(os.path.abspath(str(item.get("path") or ""))),
            str(item.get("track_index", -1)),
        ))
        digest = hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest()
        item["resume_source_id"] = digest[:24]
    return task_data


def resume_job_key(audio_dict, audio_index, round_idx):
    item = audio_dict if isinstance(audio_dict, dict) else {}
    source_id = str(item.get("resume_source_id") or "").strip()
    if not source_id:
        source = "|".join((
            str(audio_index),
            os.path.normcase(os.path.abspath(str(item.get("path") or ""))),
            str(item.get("track_index", -1)),
        ))
        source_id = hashlib.sha256(
            source.encode("utf-8", errors="ignore")
        ).hexdigest()[:24]
    return f"{source_id}:round:{max(1, int(round_idx or 1))}"


def expected_resume_job_keys(task_data):
    data = ensure_resume_source_ids(task_data if isinstance(task_data, dict) else {})
    audios = data.get("audios") if isinstance(data.get("audios"), list) else []
    loop_count = max(1, int(data.get("loop_count") or 1))
    if data.get("preview_only"):
        audios = audios[:1]
        loop_count = 1
    return {
        resume_job_key(audio, index, round_idx)
        for round_idx in range(1, loop_count + 1)
        for index, audio in enumerate(audios)
        if isinstance(audio, dict)
    }


class RenderCheckpointStore:
    """线程安全地记录已经成功落盘的混剪子任务。"""

    def __init__(self, path, task_id=""):
        self.path = os.path.abspath(str(path or "")) if path else ""
        self.task_id = str(task_id or "")
        self._lock = threading.RLock()
        self._state = self._load()

    def _empty_state(self):
        return {
            "version": CHECKPOINT_VERSION,
            "task_id": self.task_id,
            "updated_at": "",
            "completed_jobs": {},
        }

    def _load(self):
        state = self._empty_state()
        if not self.path:
            return state
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                state.update(loaded)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        jobs = state.get("completed_jobs")
        state["completed_jobs"] = jobs if isinstance(jobs, dict) else {}
        state["version"] = CHECKPOINT_VERSION
        state["task_id"] = self.task_id or str(state.get("task_id") or "")
        return state

    @staticmethod
    def _output_valid(output_path):
        try:
            return os.path.isfile(output_path) and os.path.getsize(output_path) > 1024
        except OSError:
            return False

    def _save_locked(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temp_path = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        self._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def completed_keys(self, expected_keys=None):
        with self._lock:
            jobs = self._state.get("completed_jobs", {})
            expected = set(expected_keys) if expected_keys is not None else None
            valid = set()
            stale = []
            for key, record in jobs.items():
                if expected is not None and key not in expected:
                    continue
                output_path = str(
                    record.get("output_path") if isinstance(record, dict) else ""
                )
                if self._output_valid(output_path):
                    valid.add(key)
                else:
                    stale.append(key)
            if stale:
                for key in stale:
                    jobs.pop(key, None)
                try:
                    self._save_locked()
                except OSError:
                    pass
            return valid

    def mark_completed(self, job_key, output_path, source_path="", round_idx=1):
        if not self.path or not job_key or not self._output_valid(output_path):
            return False
        with self._lock:
            self._state.setdefault("completed_jobs", {})[str(job_key)] = {
                "output_path": os.path.abspath(str(output_path)),
                "source_path": str(source_path or ""),
                "round_idx": max(1, int(round_idx or 1)),
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._save_locked()
        return True

    def completed_output_paths(self):
        with self._lock:
            jobs = self._state.get("completed_jobs", {})
            return {
                os.path.normcase(os.path.abspath(str(record.get("output_path") or "")))
                for record in jobs.values()
                if isinstance(record, dict)
                and self._output_valid(str(record.get("output_path") or ""))
            }


def checkpoint_progress(task_data, checkpoint_path, task_id=""):
    expected_keys = expected_resume_job_keys(task_data)
    if not checkpoint_path:
        return 0, len(expected_keys)
    completed = RenderCheckpointStore(
        checkpoint_path,
        task_id=task_id,
    ).completed_keys(expected_keys)
    return len(completed), len(expected_keys)


def adopt_existing_outputs(task_data, output_dir, checkpoint_path, task_id=""):
    """将旧版已经成功落盘、但未写断点的成片补登记到续传文件。"""
    data = ensure_resume_source_ids(task_data if isinstance(task_data, dict) else {})
    if not checkpoint_path or not os.path.isdir(output_dir):
        return 0
    audios = data.get("audios") if isinstance(data.get("audios"), list) else []
    if not audios:
        return 0
    loop_count = max(1, int(data.get("loop_count") or 1))
    try:
        output_files = []
        for filename in os.listdir(output_dir):
            if not filename.lower().endswith(".mp4") or filename.startswith("预览样片_"):
                continue
            stem = os.path.splitext(filename)[0]
            match = re.match(
                r"^(?:\d{8}_\d{6}_)?(?P<audio>.+?)(?:_回(?P<round>\d+))?_(?P<salt>\d{5})$",
                stem,
            )
            if not match:
                continue
            output_files.append({
                "path": os.path.join(output_dir, filename),
                "audio_name": match.group("audio"),
                "round_idx": int(match.group("round") or 1),
            })
    except OSError:
        return 0

    store = RenderCheckpointStore(checkpoint_path, task_id=task_id)
    claimed_outputs = store.completed_output_paths()
    adopted = 0
    for audio_index, audio in enumerate(audios):
        if not isinstance(audio, dict):
            continue
        audio_path = str(audio.get("path") or "")
        audio_name = str(
            audio.get("source_basename")
            or os.path.splitext(os.path.basename(audio_path))[0]
        )
        track_index = int(audio.get("track_index", -1) or -1)
        if track_index >= 0:
            audio_name = f"{audio_name}_Track{track_index}"
        matches = [
            item
            for item in output_files
            if item.get("audio_name") == audio_name
            and os.path.normcase(os.path.abspath(item.get("path", "")))
            not in claimed_outputs
        ]
        if not matches:
            continue
        for round_idx in range(1, loop_count + 1):
            round_matches = [
                item
                for item in matches
                if int(item.get("round_idx") or 1) == round_idx
            ]
            if not round_matches:
                continue
            output_item = max(
                round_matches,
                key=lambda item: os.path.getmtime(item.get("path", "")),
            )
            output_path = output_item.get("path", "")
            job_key = resume_job_key(audio, audio_index, round_idx)
            if job_key in store.completed_keys({job_key}):
                continue
            if store.mark_completed(
                job_key,
                output_path,
                source_path=audio_path,
                round_idx=round_idx,
            ):
                adopted += 1
                claimed_outputs.add(
                    os.path.normcase(os.path.abspath(output_path))
                )
                matches.remove(output_item)
    return adopted


def remove_checkpoint(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False
