import hashlib
import json
import math
import os
import random
import re
import subprocess
import time
from datetime import datetime

from PyQt5.QtCore import QThread, pyqtSignal

from services.smart_sfx_service import (
    build_sfx_mix_filters,
    plan_smart_sfx,
    smart_sfx_strength_label,
)
from utils import TRANS_MAP

from .replacement_project import (
    ReplacementProjectStore,
    atomic_write_json,
    load_json,
    safe_filename,
)


LEGACY_TRANSITION_MODES = {
    "fade_black": "fadeblack",
    "fade_white": "fadewhite",
    "fade_random": "random",
}
XFADE_VALUES = frozenset(
    value for value in TRANS_MAP.values() if value not in {"none", "random"}
)


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _creation_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _parse_rate(value):
    try:
        numerator, denominator = str(value or "0/1").split("/", 1)
        return float(numerator) / max(1.0, float(denominator))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _path_signature(path):
    stat = os.stat(path)
    raw = f"{os.path.abspath(path)}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def portrait_output_size(source_info):
    """将输出固定为平台兼容的 9:16 竖屏标准档位。"""
    width = max(2, int((source_info or {}).get("width") or 0))
    height = max(2, int((source_info or {}).get("height") or 0))
    short_edge = min(width, height)
    if short_edge >= 2160:
        return 2160, 3840
    if short_edge >= 1080:
        return 1080, 1920
    return 720, 1280


def _parse_max_volume(output):
    matches = re.findall(
        r"max_volume:\s*(-?inf|[-+]?\d+(?:\.\d+)?)\s*dB",
        str(output or ""),
        flags=re.IGNORECASE,
    )
    if not matches:
        raise RuntimeError("ffmpeg 未返回可识别的音频峰值")
    value = matches[-1].lower()
    if value == "-inf":
        return -60.0
    return min(0.0, max(-60.0, float(value)))


def _detect_audio_peak(ffmpeg_path, path, timeout=180):
    null_output = "NUL" if os.name == "nt" else "/dev/null"
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostats",
        "-i",
        path,
        "-vn",
        "-af",
        "volumedetect",
        "-f",
        "null",
        null_output,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            startupinfo=hidden_startupinfo(),
            creationflags=_creation_flags(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"检测音频峰值超时：{os.path.basename(path)}") from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 ffmpeg 检测音量：{exc}") from exc
    if result.returncode != 0:
        detail = "\n".join((result.stdout or "").strip().splitlines()[-8:])
        raise RuntimeError(detail or f"音频峰值检测失败：{os.path.basename(path)}")
    return _parse_max_volume(result.stdout)


def _probe_media(ffprobe_path, path, timeout=30):
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        path,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            startupinfo=hidden_startupinfo(),
            creationflags=_creation_flags(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"读取视频信息超时：{os.path.basename(path)}") from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 ffprobe：{exc}") from exc
    if result.returncode != 0:
        raise RuntimeError((result.stdout or "ffprobe 读取失败").strip())
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe 返回内容无效：{os.path.basename(path)}") from exc

    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    if not video:
        raise RuntimeError(f"文件没有可读取的视频画面：{os.path.basename(path)}")
    format_info = payload.get("format") or {}
    duration = float(video.get("duration") or format_info.get("duration") or 0.0)
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotation = 0
    try:
        rotation = int(float((video.get("tags") or {}).get("rotate") or 0))
    except (TypeError, ValueError):
        rotation = 0
    for item in video.get("side_data_list") or []:
        if "rotation" not in item:
            continue
        try:
            rotation = int(float(item.get("rotation") or 0))
        except (TypeError, ValueError):
            pass
    display_width, display_height = width, height
    if abs(rotation) % 180 == 90:
        display_width, display_height = height, width
    return {
        "duration": max(0.0, duration),
        "width": max(2, display_width),
        "height": max(2, display_height),
        "fps": max(
            1.0,
            _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")) or 25.0,
        ),
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or ""),
        "audio_channels": int(audio.get("channels") or 0),
        "has_audio": bool(audio),
    }


def _probe_audio(ffprobe_path, path, timeout=30):
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        path,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            startupinfo=hidden_startupinfo(),
            creationflags=_creation_flags(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"读取音频信息超时：{os.path.basename(path)}") from exc
    except OSError as exc:
        raise RuntimeError(f"无法启动 ffprobe：{exc}") from exc
    if result.returncode != 0:
        raise RuntimeError((result.stdout or "ffprobe 读取失败").strip())
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe 返回内容无效：{os.path.basename(path)}") from exc

    streams = payload.get("streams") or []
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    if not audio:
        raise RuntimeError(f"文件没有可读取的音频：{os.path.basename(path)}")
    format_info = payload.get("format") or {}
    duration = float(audio.get("duration") or format_info.get("duration") or 0.0)
    if duration < 0.10:
        raise RuntimeError(f"音频时长过短：{os.path.basename(path)}")
    return {
        "duration": duration,
        "audio_codec": str(audio.get("codec_name") or ""),
        "audio_channels": int(audio.get("channels") or 0),
        "has_audio": True,
    }


class MediaProbeCache:
    def __init__(self, cache_path, ffprobe_path):
        self.cache_path = cache_path
        self.ffprobe_path = ffprobe_path
        payload = load_json(cache_path, {})
        self.items = dict((payload or {}).get("items") or {})
        self.dirty = False

    def get(self, path):
        path = os.path.abspath(path)
        signature = _path_signature(path)
        key = os.path.normcase(path)
        cached = self.items.get(key) or {}
        if (
            cached.get("signature") == signature
            and isinstance(cached.get("info"), dict)
            and "audio_channels" in cached["info"]
        ):
            return dict(cached["info"])
        info = _probe_media(self.ffprobe_path, path)
        self.items[key] = {
            "path": path,
            "signature": signature,
            "info": info,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.dirty = True
        return dict(info)

    def save(self):
        if not self.dirty:
            return
        atomic_write_json(
            self.cache_path,
            {
                "version": 1,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "items": self.items,
            },
        )
        self.dirty = False


class AudioProbeCache(MediaProbeCache):
    def get(self, path):
        path = os.path.abspath(path)
        signature = _path_signature(path)
        key = os.path.normcase(path)
        cached = self.items.get(key) or {}
        if (
            cached.get("signature") == signature
            and isinstance(cached.get("info"), dict)
            and "audio_channels" in cached["info"]
        ):
            return dict(cached["info"])
        info = _probe_audio(self.ffprobe_path, path)
        self.items[key] = {
            "path": path,
            "signature": signature,
            "info": info,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.dirty = True
        return dict(info)


class AudioPeakCache:
    def __init__(self, cache_path, ffmpeg_path):
        self.cache_path = cache_path
        self.ffmpeg_path = ffmpeg_path
        payload = load_json(cache_path, {})
        self.items = dict((payload or {}).get("items") or {})
        self.dirty = False

    def get(self, path):
        path = os.path.abspath(path)
        signature = _path_signature(path)
        key = os.path.normcase(path)
        cached = self.items.get(key) or {}
        if cached.get("signature") == signature:
            return float(cached.get("peak_db", 0.0))
        peak_db = _detect_audio_peak(self.ffmpeg_path, path)
        self.items[key] = {
            "path": path,
            "signature": signature,
            "peak_db": peak_db,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.dirty = True
        return peak_db

    def save(self):
        if not self.dirty:
            return
        atomic_write_json(
            self.cache_path,
            {
                "version": 1,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "items": self.items,
            },
        )
        self.dirty = False


class MaterialUsageLedger:
    def __init__(self, path):
        self.path = path
        payload = load_json(path, {})
        self.counts = {
            str(key): int(value or 0)
            for key, value in dict((payload or {}).get("counts") or {}).items()
        }
        self.last_used = {
            str(key): str(value or "")
            for key, value in dict((payload or {}).get("last_used") or {}).items()
        }

    def count(self, path):
        return int(self.counts.get(os.path.normcase(os.path.abspath(path)), 0))

    def add(self, paths):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for path in set(paths or []):
            key = os.path.normcase(os.path.abspath(path))
            self.counts[key] = int(self.counts.get(key, 0)) + 1
            self.last_used[key] = stamp

    def save(self):
        atomic_write_json(
            self.path,
            {
                "version": 1,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "counts": self.counts,
                "last_used": self.last_used,
            },
        )


def _choose_path(candidates, counts, previous_path, rng):
    usable = [os.path.abspath(path) for path in candidates if os.path.isfile(path)]
    if not usable:
        raise RuntimeError("替换槽位没有可用素材")
    alternatives = [
        path
        for path in usable
        if os.path.normcase(path) != os.path.normcase(previous_path or "")
    ]
    pool = alternatives or usable
    minimum = min(int(counts.get(os.path.normcase(path), 0)) for path in pool)
    low_use = [
        path
        for path in pool
        if int(counts.get(os.path.normcase(path), 0)) <= minimum + 1
    ]
    return rng.choice(low_use)


def build_slot_plan(candidates, target_duration, probe_cache, counts, rng):
    remaining = max(0.04, float(target_duration))
    plan = []
    previous_path = ""
    attempts = 0
    while remaining > 0.015:
        attempts += 1
        if attempts > 100:
            raise RuntimeError("替换素材过短，无法在合理片段数内填满区间")
        path = _choose_path(candidates, counts, previous_path, rng)
        info = probe_cache.get(path)
        duration = float(info.get("duration") or 0.0)
        if duration < 0.04:
            candidates = [
                candidate
                for candidate in candidates
                if os.path.normcase(candidate) != os.path.normcase(path)
            ]
            if not candidates:
                raise RuntimeError("替换槽位中的视频均无法读取或时长过短")
            continue
        take = min(remaining, duration)
        max_start = max(0.0, duration - take)
        start = rng.uniform(0.0, max_start) if max_start > 0.05 else 0.0
        plan.append(
            {
                "path": path,
                "start": round(start, 6),
                "duration": round(take, 6),
            }
        )
        key = os.path.normcase(path)
        counts[key] = int(counts.get(key, 0)) + 1
        previous_path = path
        remaining -= take
    return plan


def _project_fingerprint(
    project,
    loops,
    audio_candidates=None,
    intro_candidates=None,
):
    segments = [
        {
            "id": int(segment.get("id") or 0),
            "start": round(float(segment.get("start") or 0.0), 6),
            "end": round(float(segment.get("end") or 0.0), 6),
            "folder": str(segment.get("folder") or ""),
        }
        for segment in project.get("segment_mappings", [])
        if isinstance(segment, dict)
    ]
    payload = {
        "render_pipeline_version": 3,
        "replacement_motion_version": 1,
        "source_signature": project.get("source_signature") or {},
        "segments": segments,
        "loops": int(loops),
        "transition": {
            "mode": str(
                (project.get("settings") or {}).get("transition_mode") or "none"
            ),
            "duration": round(
                float(
                    (project.get("settings") or {}).get("transition_duration")
                    or 0.25
                ),
                3,
            ),
        },
        "smart_sfx": {
            "enabled": bool(
                (project.get("settings") or {}).get(
                    "smart_sfx_enabled",
                    False,
                )
            ),
            "strength": str(
                (project.get("settings") or {}).get("smart_sfx_strength")
                or "standard"
            ),
        },
        "intro": {
            "enabled": bool(
                (project.get("settings") or {}).get("intro_enabled", False)
            ),
            "folder": str(
                (project.get("settings") or {}).get("intro_external_dir") or ""
            ),
            "full_play": bool(
                (project.get("settings") or {}).get("intro_full_play", True)
            ),
            "duration": round(
                float(
                    (project.get("settings") or {}).get("intro_duration") or 3.0
                ),
                3,
            ),
            "files": [
                {
                    "path": os.path.normcase(os.path.abspath(path)),
                    "signature": _path_signature(path),
                }
                for path in intro_candidates or []
                if os.path.isfile(path)
            ],
        },
        "audio": {
            "mode": str((project.get("settings") or {}).get("audio_mode") or "source"),
            "folder": str(
                (project.get("settings") or {}).get("audio_external_dir") or ""
            ),
            "files": [
                {
                    "path": os.path.normcase(os.path.abspath(path)),
                    "signature": _path_signature(path),
                }
                for path in audio_candidates or []
                if os.path.isfile(path)
            ],
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class ReplacementRenderWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)
    jobs_prepared = pyqtSignal(object)
    job_status = pyqtSignal(int, str, str)
    completed = pyqtSignal(bool, str)

    def __init__(
        self,
        project_path,
        loops,
        ffmpeg_path,
        ffprobe_path,
        encoder="auto",
        resume=True,
        parent=None,
    ):
        super().__init__(parent)
        self.project_path = os.path.abspath(project_path)
        self.loops = max(1, int(loops or 1))
        self.ffmpeg_path = os.path.abspath(ffmpeg_path)
        self.ffprobe_path = os.path.abspath(ffprobe_path)
        self.encoder = str(encoder or "auto")
        self.resume = bool(resume)
        self._stop_requested = False
        self._process = None

    def stop(self):
        self._stop_requested = True
        self.requestInterruption()
        self._terminate_current_process()

    def _is_stopping(self):
        return self._stop_requested or self.isInterruptionRequested()

    def _terminate_current_process(self):
        process = self._process
        if not process or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    startupinfo=hidden_startupinfo(),
                    creationflags=_creation_flags(),
                    check=False,
                )
            else:
                process.terminate()
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def _run_process(self, command, timeout):
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(),
                creationflags=_creation_flags(),
            )
            started = time.monotonic()
            while self._process.poll() is None:
                if self._is_stopping():
                    self._terminate_current_process()
                    raise InterruptedError("任务已暂停")
                if time.monotonic() - started > timeout:
                    self._terminate_current_process()
                    raise RuntimeError("ffmpeg 处理超时，已终止当前任务")
                self.msleep(100)
            stderr = (self._process.stderr.read() or b"").decode(
                "utf-8", errors="replace"
            )
            return_code = self._process.returncode
            if return_code != 0:
                detail = "\n".join(stderr.strip().splitlines()[-12:])
                raise RuntimeError(detail or f"ffmpeg 返回错误码 {return_code}")
        except OSError as exc:
            raise RuntimeError(f"无法启动 ffmpeg：{exc}") from exc
        finally:
            self._process = None

    def _safe_audio_peak(self, cache, path, fallback):
        try:
            return float(cache.get(path))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.log.emit(
                f"音量检测失败，使用安全基准：{os.path.basename(path)}；{exc}"
            )
            return float(fallback)

    def _prepare_audio_levels(self, project, source_info, manifest, peak_cache):
        source_peak_db = -1.0
        source_path = str(project.get("source_path") or "")
        if source_info.get("has_audio") and source_path:
            source_peak_db = self._safe_audio_peak(
                peak_cache,
                source_path,
                -1.0,
            )
        if source_peak_db <= -55.0:
            source_peak_db = -1.0
        target_peak_db = min(-0.5, max(-30.0, source_peak_db))
        for job in (manifest or {}).get("jobs") or []:
            job["audio_target_peak_db"] = round(target_peak_db, 3)
            intro_plan = dict(job.get("intro_plan") or {})
            if intro_plan.get("path") and intro_plan.get("has_audio"):
                intro_plan["peak_db"] = round(
                    self._safe_audio_peak(
                        peak_cache,
                        str(intro_plan["path"]),
                        target_peak_db,
                    ),
                    3,
                )
                job["intro_plan"] = intro_plan
            audio_plan = dict(job.get("audio_plan") or {})
            if audio_plan.get("path"):
                audio_plan["peak_db"] = round(
                    self._safe_audio_peak(
                        peak_cache,
                        str(audio_plan["path"]),
                        target_peak_db,
                    ),
                    3,
                )
                job["audio_plan"] = audio_plan
        self.log.emit(
            f"音量统一基准：取原片最高峰值 {target_peak_db:.1f} dB，"
            "片头和替换音频将自动对齐。"
        )
        return target_peak_db

    @staticmethod
    def _audio_peak_filter(input_peak_db, target_peak_db):
        input_peak_db = min(0.0, max(-60.0, float(input_peak_db)))
        target_peak_db = min(-0.5, max(-30.0, float(target_peak_db)))
        gain_db = min(24.0, max(-24.0, target_peak_db - input_peak_db))
        return f"volume={gain_db:.3f}dB"

    @staticmethod
    def _audio_format_filter(channels):
        if int(channels or 0) == 1:
            return "aresample=48000,pan=stereo|c0=c0|c1=c0"
        return (
            "aresample=48000,"
            "aformat=sample_fmts=fltp:sample_rates=48000:"
            "channel_layouts=stereo"
        )

    def _quantized_slots(self, project, source_info):
        fps = max(1.0, float(source_info.get("fps") or 25.0))
        source_duration = float(source_info.get("duration") or 0.0)
        result = []
        previous_end = 0.0
        for slot in sorted(
            [
                dict(item)
                for item in project.get("segment_mappings", [])
                if isinstance(item, dict) and str(item.get("folder") or "").strip()
            ],
            key=lambda item: float(item.get("start") or 0.0),
        ):
            start = max(previous_end, math.floor(float(slot.get("start") or 0.0) * fps) / fps)
            end = math.ceil(float(slot.get("end") or 0.0) * fps) / fps
            end = min(source_duration, max(start + 1.0 / fps, end))
            slot["_render_start"] = round(start, 6)
            slot["_render_end"] = round(end, 6)
            slot["_render_duration"] = round(end - start, 6)
            result.append(slot)
            previous_end = end
        return result

    @staticmethod
    def _sample_replacement_motion(rng):
        """为每个替换素材生成可复现的缓慢拉近与安全偏移参数。"""
        zoom_start = rng.uniform(1.01, 1.035)
        zoom_end = rng.uniform(min(1.10, zoom_start + 0.01), 1.10)

        def signed_pan():
            magnitude = rng.uniform(0.18, 0.78)
            return magnitude * rng.choice((-1.0, 1.0))

        return {
            "zoom_start": round(zoom_start, 5),
            "zoom_end": round(zoom_end, 5),
            "pan_x": round(signed_pan(), 5),
            "pan_y": round(signed_pan(), 5),
        }

    @staticmethod
    def _expanded_slots(slots, source_duration, target_duration):
        source_duration = max(0.10, float(source_duration or 0.0))
        target_duration = max(0.10, float(target_duration or 0.0))
        result = []
        cycle_count = max(1, int(math.ceil(target_duration / source_duration)))
        for cycle_index in range(cycle_count):
            offset = cycle_index * source_duration
            for slot in slots:
                start = offset + float(slot.get("_render_start") or 0.0)
                if start >= target_duration - 0.001:
                    continue
                end = min(
                    target_duration,
                    offset + float(slot.get("_render_end") or 0.0),
                )
                if end - start < 0.015:
                    continue
                occurrence = dict(slot)
                occurrence["_cycle_index"] = cycle_index
                occurrence["_render_start"] = round(start, 6)
                occurrence["_render_end"] = round(end, 6)
                occurrence["_render_duration"] = round(end - start, 6)
                result.append(occurrence)
        return result

    def _build_jobs(
        self,
        store,
        project,
        slots,
        source_info,
        probe_cache,
        audio_probe_cache,
        ledger,
    ):
        project_dir = store.project_dir(project)
        manifest_path = os.path.join(project_dir, "render_manifest.json")
        settings = dict(project.get("settings") or {})
        intro_enabled = bool(settings.get("intro_enabled", False))
        intro_candidates = store.intro_materials(project) if intro_enabled else []
        audio_mode = str(settings.get("audio_mode") or "source")
        audio_candidates = store.audio_materials(project) if audio_mode == "folder" else []
        fingerprint = _project_fingerprint(
            project,
            self.loops,
            audio_candidates,
            intro_candidates,
        )
        previous = load_json(manifest_path, {}) if self.resume else {}
        if (previous or {}).get("fingerprint") == fingerprint:
            jobs = list(previous.get("jobs") or [])
            unfinished = any(
                item.get("status") != "completed"
                or not os.path.isfile(str(item.get("output") or ""))
                for item in jobs
            )
            if len(jobs) == self.loops and unfinished:
                return manifest_path, previous

        intro_full_play = bool(settings.get("intro_full_play", True))
        requested_intro_duration = max(
            0.20,
            float(settings.get("intro_duration") or 3.0),
        )
        intro_infos = {}
        if intro_enabled:
            for path in intro_candidates:
                try:
                    intro_infos[path] = probe_cache.get(path)
                except RuntimeError as exc:
                    self.log.emit(
                        f"跳过无法读取的片头素材：{os.path.basename(path)}；{exc}"
                    )
            if not intro_infos:
                raise RuntimeError("片头素材文件夹中没有可正常读取的视频")
            if not intro_full_play:
                intro_infos = {
                    path: info
                    for path, info in intro_infos.items()
                    if float(info.get("duration") or 0.0)
                    >= requested_intro_duration - 0.02
                }
                if not intro_infos:
                    raise RuntimeError(
                        f"没有时长达到 {requested_intro_duration:.2f} 秒的片头视频，"
                        "请缩短截取时长或添加更长素材"
                    )

        date_dir = os.path.join(store.output_dir(project), datetime.now().strftime("%Y-%m-%d"))
        os.makedirs(date_dir, exist_ok=True)
        batch_id = (
            datetime.now().strftime("%Y%m%d_%H%M%S")
            + f"_{time.time_ns() % 10000:04d}"
        )
        counts = dict(ledger.counts)
        jobs = []
        previous_intro_path = ""
        previous_audio_path = ""
        for round_index in range(1, self.loops + 1):
            seed_text = f"{fingerprint}|{round_index}|{time.time_ns()}"
            seed = int(hashlib.sha1(seed_text.encode("utf-8")).hexdigest()[:16], 16)
            rng = random.Random(seed)
            intro_plan = None
            if intro_enabled:
                intro_path = _choose_path(
                    list(intro_infos),
                    counts,
                    previous_intro_path,
                    rng,
                )
                intro_info = intro_infos[intro_path]
                source_intro_duration = float(intro_info.get("duration") or 0.0)
                intro_duration = (
                    source_intro_duration
                    if intro_full_play
                    else requested_intro_duration
                )
                intro_plan = {
                    "path": intro_path,
                    "duration": round(intro_duration, 6),
                    "source_duration": round(source_intro_duration, 6),
                    "full_play": intro_full_play,
                    "has_audio": bool(intro_info.get("has_audio")),
                    "audio_codec": str(intro_info.get("audio_codec") or ""),
                    "audio_channels": int(
                        intro_info.get("audio_channels") or 0
                    ),
                }
                intro_key = os.path.normcase(intro_path)
                counts[intro_key] = int(counts.get(intro_key, 0)) + 1
                previous_intro_path = intro_path
            audio_plan = None
            target_duration = float(source_info.get("duration") or 0.0)
            if audio_mode == "folder":
                audio_path = _choose_path(
                    audio_candidates,
                    counts,
                    previous_audio_path,
                    rng,
                )
                audio_info = audio_probe_cache.get(audio_path)
                target_duration = float(audio_info.get("duration") or 0.0)
                audio_plan = {
                    "path": audio_path,
                    "duration": round(target_duration, 6),
                    "codec": str(audio_info.get("audio_codec") or ""),
                    "audio_channels": int(
                        audio_info.get("audio_channels") or 0
                    ),
                }
                audio_key = os.path.normcase(audio_path)
                counts[audio_key] = int(counts.get(audio_key, 0)) + 1
                previous_audio_path = audio_path
            slot_plans = []
            expanded_slots = self._expanded_slots(
                slots,
                float(source_info.get("duration") or 0.0),
                target_duration,
            )
            for occurrence_index, slot in enumerate(expanded_slots, 1):
                candidates = store.segment_materials(slot)
                plan = build_slot_plan(
                    candidates,
                    float(slot.get("_render_duration") or 0.0),
                    probe_cache,
                    counts,
                    rng,
                )
                for clip in plan:
                    clip.update(self._sample_replacement_motion(rng))
                slot_plans.append(
                    {
                        "slot_id": int(slot.get("id") or 0),
                        "occurrence": occurrence_index,
                        "cycle_index": int(slot.get("_cycle_index") or 0),
                        "start": float(slot.get("_render_start") or 0.0),
                        "end": float(slot.get("_render_end") or 0.0),
                        "duration": float(slot.get("_render_duration") or 0.0),
                        "clips": plan,
                    }
                )
            stem = safe_filename(project.get("name") or "局部替换")
            filename = (
                f"{batch_id}_{stem}_"
                f"局部替换_{round_index:03d}.mp4"
            )
            jobs.append(
                {
                    "index": round_index,
                    "status": "pending",
                    "output": os.path.join(date_dir, filename),
                    "seed": seed,
                    "target_duration": round(target_duration, 6),
                    "total_duration": round(
                        target_duration
                        + float((intro_plan or {}).get("duration") or 0.0),
                        6,
                    ),
                    "intro_plan": intro_plan,
                    "audio_plan": audio_plan,
                    "slot_plans": slot_plans,
                    "error": "",
                }
            )
        manifest = {
            "version": 1,
            "fingerprint": fingerprint,
            "project_path": self.project_path,
            "batch_id": batch_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "jobs": jobs,
        }
        atomic_write_json(manifest_path, manifest)
        return manifest_path, manifest

    @staticmethod
    def _save_manifest(path, manifest):
        manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        atomic_write_json(path, manifest)

    def _encoder_args(self, encoder):
        if encoder == "nvenc":
            return [
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p4",
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                "20",
                "-b:v",
                "0",
                "-pix_fmt",
                "yuv420p",
            ]
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
        ]

    def _preferred_encoder(self):
        if self.encoder in {"cpu", "libx264"}:
            return "cpu"
        if self.encoder in {"nvenc", "auto"}:
            return "nvenc"
        return "cpu"

    @staticmethod
    def _expanded_timeline_segments(project, source_duration, target_duration):
        source_duration = max(0.10, float(source_duration or 0.0))
        target_duration = max(0.10, float(target_duration or 0.0))
        base_segments = sorted(
            [
                {
                    "start": max(0.0, float(item.get("start") or 0.0)),
                    "end": min(source_duration, float(item.get("end") or 0.0)),
                }
                for item in project.get("segment_mappings", [])
                if isinstance(item, dict)
                and float(item.get("end") or 0.0) - float(item.get("start") or 0.0)
                > 0.001
            ],
            key=lambda item: item["start"],
        )
        if not base_segments:
            base_segments = [{"start": 0.0, "end": source_duration}]

        result = []
        cycle_count = max(1, int(math.ceil(target_duration / source_duration)))
        for cycle_index in range(cycle_count):
            offset = cycle_index * source_duration
            for segment in base_segments:
                start = offset + segment["start"]
                if start >= target_duration - 0.001:
                    continue
                end = min(target_duration, offset + segment["end"])
                if end - start >= 0.015:
                    result.append({"start": start, "end": end})
        return result

    @staticmethod
    def _transition_value(mode, rng):
        normalized = LEGACY_TRANSITION_MODES.get(str(mode or "none"), str(mode or "none"))
        if normalized == "random":
            return rng.choice(tuple(sorted(XFADE_VALUES)))
        return normalized if normalized in XFADE_VALUES else "fade"

    @staticmethod
    def _replacement_motion_filter(clip, duration, target_w, target_h):
        """构造始终位于安全裁剪范围内的动态缩放与偏移滤镜。"""
        zoom_start = min(
            1.10,
            max(1.01, float(clip.get("zoom_start") or 1.01)),
        )
        zoom_end = min(
            1.10,
            max(zoom_start, float(clip.get("zoom_end") or 1.06)),
        )
        pan_x = min(0.85, max(-0.85, float(clip.get("pan_x") or 0.0)))
        pan_y = min(0.85, max(-0.85, float(clip.get("pan_y") or 0.0)))
        safe_duration = max(0.04, float(duration or 0.04))
        zoom_expression = (
            f"({zoom_start:.5f}+({zoom_end:.5f}-{zoom_start:.5f})*"
            f"min(t/{safe_duration:.5f}\\,1))"
        )
        return (
            f"scale=w='trunc(iw*{zoom_expression}/2)*2':"
            f"h='trunc(ih*{zoom_expression}/2)*2':"
            "flags=lanczos:eval=frame,"
            f"crop={int(target_w)}:{int(target_h)}:"
            f"(iw-ow)/2+((iw-ow)/2*{pan_x:.5f}):"
            f"(ih-oh)/2+((ih-oh)/2*{pan_y:.5f}),setsar=1"
        )

    def _filter_graph(self, project, job, source_info):
        width, height = portrait_output_size(source_info)
        fps = max(1.0, min(120.0, float(source_info.get("fps") or 25.0)))
        target_duration = max(
            0.10,
            float(job.get("target_duration") or source_info.get("duration") or 0.0),
        )
        intro_plan = dict(job.get("intro_plan") or {})
        intro_duration = max(
            0.0,
            float(intro_plan.get("duration") or 0.0),
        )
        filters = [
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps:.6f},format=yuv420p,"
            f"settb=AVTB,trim=duration={target_duration:.6f},"
            f"setpts=PTS-STARTPTS[base0]"
        ]
        if intro_duration > 0.0:
            filters.append(
                f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps={fps:.6f},format=yuv420p,"
                f"settb=AVTB,trim=duration={intro_duration:.6f},"
                f"setpts=PTS-STARTPTS[intro_video]"
            )
        input_specs = []
        input_index = 2 if intro_duration > 0.0 else 1
        previous_base = "base0"
        for slot_index, slot_plan in enumerate(job.get("slot_plans") or []):
            clip_labels = []
            for clip_index, clip in enumerate(slot_plan.get("clips") or []):
                take = max(0.04, float(clip.get("duration") or 0.0))
                input_specs.append(
                    {
                        "path": str(clip.get("path") or ""),
                        "start": max(0.0, float(clip.get("start") or 0.0)),
                        "duration": take,
                    }
                )
                label = f"s{slot_index}c{clip_index}"
                motion_filter = self._replacement_motion_filter(
                    clip,
                    take,
                    width,
                    height,
                )
                filters.append(
                    f"[{input_index}:v]"
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},setsar=1,fps={fps:.6f},format=yuv420p,"
                    f"settb=AVTB,trim=duration={take:.6f},"
                    f"setpts=PTS-STARTPTS,{motion_filter}[{label}]"
                )
                clip_labels.append(f"[{label}]")
                input_index += 1
            if not clip_labels:
                raise RuntimeError(
                    f"片段{int(slot_plan.get('slot_id') or 0):03d}没有素材计划"
                )
            replacement_label = f"replacement{slot_index}"
            slot_duration = max(0.04, float(slot_plan.get("duration") or 0.0))
            if len(clip_labels) == 1:
                filters.append(
                    f"{clip_labels[0]}tpad=stop_mode=clone:stop_duration=0.2,"
                    f"trim=duration={slot_duration:.6f},"
                    f"setpts=PTS-STARTPTS+{float(slot_plan.get('start') or 0.0):.6f}/TB"
                    f"[{replacement_label}]"
                )
            else:
                concat_label = f"concat{slot_index}"
                filters.append(
                    f"{''.join(clip_labels)}concat=n={len(clip_labels)}:v=1:a=0"
                    f"[{concat_label}]"
                )
                filters.append(
                    f"[{concat_label}]tpad=stop_mode=clone:stop_duration=0.2,"
                    f"trim=duration={slot_duration:.6f},"
                    f"setpts=PTS-STARTPTS+{float(slot_plan.get('start') or 0.0):.6f}/TB"
                    f"[{replacement_label}]"
                )
            output_base = f"base{slot_index + 1}"
            start = float(slot_plan.get("start") or 0.0)
            end = float(slot_plan.get("end") or 0.0)
            filters.append(
                f"[{previous_base}][{replacement_label}]"
                f"overlay=0:0:eof_action=pass:shortest=0:"
                f"enable='between(t,{start:.6f},{end:.6f})'[{output_base}]"
            )
            previous_base = output_base

        settings = dict(project.get("settings") or {})
        transition_mode = LEGACY_TRANSITION_MODES.get(
            str(settings.get("transition_mode") or "none"),
            str(settings.get("transition_mode") or "none"),
        )
        transition_duration = min(
            0.80,
            max(0.10, float(settings.get("transition_duration") or 0.25)),
        )
        if transition_mode != "none":
            segments = self._expanded_timeline_segments(
                project,
                float(source_info.get("duration") or 0.0),
                target_duration,
            )
            if len(segments) > 1:
                shortest_segment = min(
                    float(item["end"]) - float(item["start"]) for item in segments
                )
                effective_duration = min(
                    transition_duration,
                    shortest_segment * 0.45,
                )
                if effective_duration >= 0.04:
                    half_duration = effective_duration / 2.0
                    split_labels = [
                        f"transition_source{index}" for index in range(len(segments))
                    ]
                    filters.append(
                        f"[{previous_base}]split={len(split_labels)}"
                        + "".join(f"[{label}]" for label in split_labels)
                    )
                    clip_labels = []
                    clip_durations = []
                    for index, segment in enumerate(segments):
                        start = max(
                            0.0,
                            float(segment["start"])
                            - (half_duration if index > 0 else 0.0),
                        )
                        end = min(
                            target_duration,
                            float(segment["end"])
                            + (half_duration if index < len(segments) - 1 else 0.0),
                        )
                        label = f"transition_clip{index}"
                        filters.append(
                            f"[{split_labels[index]}]trim=start={start:.6f}:"
                            f"end={end:.6f},setpts=PTS-STARTPTS[{label}]"
                        )
                        clip_labels.append(label)
                        clip_durations.append(end - start)

                    transition_rng = random.Random(
                        int(job.get("seed") or 0) ^ 0x5A17
                    )
                    current_label = clip_labels[0]
                    current_duration = clip_durations[0]
                    for index in range(1, len(clip_labels)):
                        output_label = f"transition{index}"
                        transition_value = self._transition_value(
                            transition_mode,
                            transition_rng,
                        )
                        offset = max(0.01, current_duration - effective_duration)
                        filters.append(
                            f"[{current_label}][{clip_labels[index]}]"
                            f"xfade=transition={transition_value}:"
                            f"duration={effective_duration:.6f}:"
                            f"offset={offset:.6f}[{output_label}]"
                        )
                        current_duration += (
                            clip_durations[index] - effective_duration
                        )
                        current_label = output_label
                    previous_base = current_label
        if intro_duration > 0.0:
            filters.append(
                f"[intro_video][{previous_base}]concat=n=2:v=1:a=0[video_with_intro]"
            )
            previous_base = "video_with_intro"
        return input_specs, ";".join(filters), previous_base

    def _render_job(self, project, job, source_info, encoder):
        output_path = os.path.abspath(job["output"])
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        partial_path = output_path + ".partial.mp4"
        try:
            os.remove(partial_path)
        except FileNotFoundError:
            pass

        input_specs, filter_graph, output_label = self._filter_graph(
            project,
            job,
            source_info,
        )
        target_duration = max(
            0.10,
            float(job.get("target_duration") or source_info.get("duration") or 0.0),
        )
        source_duration = max(0.10, float(source_info.get("duration") or 0.0))
        intro_plan = dict(job.get("intro_plan") or {})
        intro_duration = max(0.0, float(intro_plan.get("duration") or 0.0))
        has_intro = bool(intro_plan.get("path") and intro_duration > 0.0)
        total_duration = max(
            0.10,
            float(job.get("total_duration") or target_duration + intro_duration),
        )
        audio_plan = dict(job.get("audio_plan") or {})
        settings = dict(project.get("settings") or {})
        smart_sfx_events = []
        if bool(settings.get("smart_sfx_enabled", False)):
            main_offset = intro_duration
            smart_sfx_candidates = []
            if has_intro:
                smart_sfx_candidates.append(
                    {"time": 0.0, "kind": "intro", "priority": 3}
                )
            for slot_plan in job.get("slot_plans") or []:
                smart_sfx_candidates.append({
                    "time": main_offset
                    + float(slot_plan.get("start") or 0.0),
                    "kind": "product",
                    "priority": 2,
                })
            transition_mode = LEGACY_TRANSITION_MODES.get(
                str(settings.get("transition_mode") or "none"),
                str(settings.get("transition_mode") or "none"),
            )
            if transition_mode != "none":
                timeline_segments = self._expanded_timeline_segments(
                    project,
                    source_duration,
                    target_duration,
                )
                for segment in timeline_segments[1:]:
                    smart_sfx_candidates.append({
                        "time": main_offset
                        + float(segment.get("start") or 0.0),
                        "kind": "transition",
                        "priority": 1,
                    })
            smart_sfx_candidates.append({
                "time": max(0.0, total_duration - 0.45),
                "kind": "ending",
                "priority": 2,
            })
            try:
                smart_sfx_events = plan_smart_sfx(
                    self.ffmpeg_path,
                    total_duration,
                    smart_sfx_candidates,
                    str(settings.get("smart_sfx_strength") or "standard"),
                    f"{job.get('seed')}|{job.get('index')}",
                )
                self.log.emit(
                    f"智能音效已规划 {len(smart_sfx_events)} 个结构点，"
                    f"档位：{smart_sfx_strength_label(settings.get('smart_sfx_strength'))}。"
                )
            except Exception as exc:
                self.log.emit(f"智能音效准备失败，本条自动按无音效继续：{exc}")
                smart_sfx_events = []
        command = [
            self.ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
        ]
        if target_duration > source_duration + 0.02:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", str(project.get("source_path") or "")])
        if has_intro:
            command.extend(
                [
                    "-t",
                    f"{intro_duration + 0.12:.6f}",
                    "-i",
                    str(intro_plan["path"]),
                ]
            )
        for item in input_specs:
            command.extend(
                [
                    "-ss",
                    f"{item['start']:.6f}",
                    "-t",
                    f"{item['duration'] + 0.12:.6f}",
                    "-i",
                    item["path"],
                ]
            )
        audio_input_index = -1
        if audio_plan.get("path"):
            audio_input_index = 1 + int(has_intro) + len(input_specs)
            command.extend(["-i", str(audio_plan["path"])])
        next_input_index = (
            1
            + int(has_intro)
            + len(input_specs)
            + int(audio_input_index >= 0)
        )
        indexed_sfx_events = []
        for event in smart_sfx_events:
            if not os.path.isfile(str(event.path or "")):
                continue
            command.extend(["-i", event.path])
            indexed_sfx_events.append((next_input_index, event))
            next_input_index += 1
        audio_output_label = ""
        target_peak_db = float(job.get("audio_target_peak_db") or -1.0)
        if has_intro:
            audio_filters = []
            if intro_plan.get("has_audio"):
                intro_audio_format = self._audio_format_filter(
                    intro_plan.get("audio_channels")
                )
                intro_peak_filter = self._audio_peak_filter(
                    float(intro_plan.get("peak_db", target_peak_db)),
                    target_peak_db,
                )
                audio_filters.append(
                    f"[1:a:0]{intro_audio_format},"
                    f"{intro_peak_filter},"
                    f"apad=pad_dur={intro_duration:.6f},"
                    f"atrim=duration={intro_duration:.6f},"
                    f"asetpts=PTS-STARTPTS[intro_audio]"
                )
            else:
                audio_filters.append(
                    f"anullsrc=r=48000:cl=stereo,"
                    f"atrim=duration={intro_duration:.6f},"
                    f"asetpts=PTS-STARTPTS[intro_audio]"
                )
            if audio_input_index >= 0:
                main_audio_format = self._audio_format_filter(
                    audio_plan.get("audio_channels")
                )
                main_peak_filter = self._audio_peak_filter(
                    float(audio_plan.get("peak_db", target_peak_db)),
                    target_peak_db,
                )
                audio_filters.append(
                    f"[{audio_input_index}:a:0]{main_audio_format},"
                    f"{main_peak_filter},"
                    f"apad=pad_dur={target_duration:.6f},"
                    f"atrim=duration={target_duration:.6f},"
                    f"asetpts=PTS-STARTPTS[main_audio]"
                )
            elif source_info.get("has_audio"):
                source_audio_format = self._audio_format_filter(
                    source_info.get("audio_channels")
                )
                source_peak_filter = self._audio_peak_filter(
                    target_peak_db,
                    target_peak_db,
                )
                audio_filters.append(
                    f"[0:a:0]{source_audio_format},"
                    f"{source_peak_filter},"
                    f"apad=pad_dur={target_duration:.6f},"
                    f"atrim=duration={target_duration:.6f},"
                    f"asetpts=PTS-STARTPTS[main_audio]"
                )
            else:
                audio_filters.append(
                    f"anullsrc=r=48000:cl=stereo,"
                    f"atrim=duration={target_duration:.6f},"
                    f"asetpts=PTS-STARTPTS[main_audio]"
                )
            audio_filters.append(
                "[intro_audio][main_audio]concat=n=2:v=0:a=1[combined_audio]"
            )
            filter_graph = filter_graph + ";" + ";".join(audio_filters)
            audio_output_label = "combined_audio"
        elif audio_input_index >= 0:
            main_audio_format = self._audio_format_filter(
                audio_plan.get("audio_channels")
            )
            main_peak_filter = self._audio_peak_filter(
                float(audio_plan.get("peak_db", target_peak_db)),
                target_peak_db,
            )
            audio_filter = (
                f"[{audio_input_index}:a:0]{main_audio_format},"
                f"{main_peak_filter},"
                f"apad=pad_dur={target_duration:.6f},"
                f"atrim=duration={target_duration:.6f},"
                "asetpts=PTS-STARTPTS[normalized_audio]"
            )
            filter_graph = filter_graph + ";" + audio_filter
            audio_output_label = "normalized_audio"
        if indexed_sfx_events:
            if audio_output_label:
                base_audio_label = audio_output_label
            elif source_info.get("has_audio"):
                source_audio_format = self._audio_format_filter(
                    source_info.get("audio_channels")
                )
                base_audio_filter = (
                    f"[0:a:0]{source_audio_format},"
                    f"apad=pad_dur={total_duration:.6f},"
                    f"atrim=duration={total_duration:.6f},"
                    "asetpts=PTS-STARTPTS[smart_sfx_base]"
                )
                filter_graph = filter_graph + ";" + base_audio_filter
                base_audio_label = "smart_sfx_base"
            else:
                base_audio_filter = (
                    "anullsrc=r=48000:cl=stereo,"
                    f"atrim=duration={total_duration:.6f},"
                    "asetpts=PTS-STARTPTS[smart_sfx_base]"
                )
                filter_graph = filter_graph + ";" + base_audio_filter
                base_audio_label = "smart_sfx_base"
            sfx_filters = build_sfx_mix_filters(
                base_audio_label,
                indexed_sfx_events,
                48000,
                "audio_with_sfx",
            )
            filter_graph = filter_graph + ";" + ";".join(sfx_filters)
            audio_output_label = "audio_with_sfx"
        command.extend(["-filter_complex", filter_graph, "-map", f"[{output_label}]"])
        if audio_output_label:
            command.extend(["-map", f"[{audio_output_label}]"])
        elif audio_input_index >= 0:
            command.extend(["-map", f"{audio_input_index}:a:0"])
        elif source_info.get("has_audio"):
            command.extend(["-map", "0:a:0?"])
        command.extend(self._encoder_args(encoder))
        if audio_output_label or audio_input_index >= 0:
            command.extend(["-c:a", "aac", "-b:a", "192k"])
        elif source_info.get("has_audio"):
            if str(source_info.get("audio_codec") or "").lower() in {
                "aac",
                "mp3",
                "ac3",
                "eac3",
                "alac",
            }:
                command.extend(["-c:a", "copy"])
            else:
                command.extend(["-c:a", "aac", "-b:a", "192k"])
        command.extend(
            [
                "-t",
                f"{total_duration:.6f}",
                "-movflags",
                "+faststart",
                partial_path,
            ]
        )
        timeout = max(300, int(total_duration * 20))
        self._run_process(command, timeout)
        if not os.path.isfile(partial_path) or os.path.getsize(partial_path) < 1024:
            raise RuntimeError("ffmpeg 未生成有效视频")
        os.replace(partial_path, output_path)
        return output_path

    def _verify_output(self, output_path, source_info, expected_duration, requires_audio):
        info = _probe_media(self.ffprobe_path, output_path, timeout=45)
        if abs(float(info.get("duration") or 0.0) - float(expected_duration)) > 0.35:
            raise RuntimeError("输出时长与片头及正文计划不一致")
        expected_width, expected_height = portrait_output_size(source_info)
        if (
            int(info.get("width") or 0) != expected_width
            or int(info.get("height") or 0) != expected_height
        ):
            raise RuntimeError(
                f"输出画面不是标准竖屏尺寸 "
                f"{expected_width}×{expected_height}"
            )
        if requires_audio and not info.get("has_audio"):
            raise RuntimeError("输出视频缺少音频")

    def run(self):
        try:
            if not os.path.isfile(self.ffmpeg_path) or not os.path.isfile(self.ffprobe_path):
                raise RuntimeError("缺少 ffmpeg 或 ffprobe，请检查网盘资源包")
            project_dir = os.path.dirname(self.project_path)
            store = ReplacementProjectStore(os.path.dirname(os.path.dirname(project_dir)))
            project = store.load(self.project_path)
            project = store.sync_segment_mappings(project, save=True)
            issues, status = store.validate(project)
            if issues:
                raise RuntimeError("；".join(issues))
            replacement_status = [
                item for item in status if str(item.get("folder") or "").strip()
            ]
            if replacement_status:
                self.log.emit(
                    "替换路径校验通过："
                    + "、".join(
                        f"片段{int(item.get('segment_id') or 0):03d}"
                        f"（{int(item.get('material_count') or 0)}个素材）"
                        for item in replacement_status
                    )
                )
                self.log.emit(
                    "替换素材已启用内置防重：每段自动缓慢放大1%-10%，"
                    "并在无黑边范围内随机偏移。"
                )

            self.log.emit("正在读取竞品原片与替换素材信息...")
            source_info = _probe_media(
                self.ffprobe_path, str(project.get("source_path") or ""), timeout=45
            )
            output_width, output_height = portrait_output_size(source_info)
            self.log.emit(
                f"平台竖屏保护：输出统一为 "
                f"{output_width}×{output_height}（最低 720×1280）。"
            )
            project["source_metadata"] = {
                key: source_info.get(key)
                for key in (
                    "duration",
                    "width",
                    "height",
                    "fps",
                    "audio_codec",
                    "audio_channels",
                    "video_codec",
                )
            }
            project = store.sync_segment_mappings(project, save=True)
            slots = self._quantized_slots(project, source_info)
            cache = MediaProbeCache(
                os.path.join(project_dir, ".v2_media_probe_cache.json"),
                self.ffprobe_path,
            )
            audio_cache = AudioProbeCache(
                os.path.join(project_dir, ".v2_audio_probe_cache.json"),
                self.ffprobe_path,
            )
            peak_cache = AudioPeakCache(
                os.path.join(project_dir, ".v2_audio_peak_cache.json"),
                self.ffmpeg_path,
            )
            ledger = MaterialUsageLedger(
                os.path.join(project_dir, ".v2_material_usage.json")
            )
            manifest_path, manifest = self._build_jobs(
                store,
                project,
                slots,
                source_info,
                cache,
                audio_cache,
                ledger,
            )
            self._prepare_audio_levels(
                project,
                source_info,
                manifest,
                peak_cache,
            )
            self._save_manifest(manifest_path, manifest)
            cache.save()
            audio_cache.save()
            peak_cache.save()

            jobs = manifest.get("jobs") or []
            self.jobs_prepared.emit(
                [
                    {
                        "index": int(job.get("index") or 0),
                        "status": str(job.get("status") or "pending"),
                        "output": str(job.get("output") or ""),
                    }
                    for job in jobs
                ]
            )
            total = len(jobs)
            completed_count = 0
            for job in jobs:
                output_path = str(job.get("output") or "")
                if job.get("status") == "completed" and os.path.isfile(output_path):
                    completed_count += 1
                    self.job_status.emit(
                        int(job.get("index") or 0), "已完成（跳过）", "#22C55E"
                    )
            self.progress.emit(completed_count, total, "已恢复上次任务进度")

            encoder = self._preferred_encoder()
            for job in jobs:
                if self._is_stopping():
                    raise InterruptedError("任务已暂停，可点击继续生成")
                output_path = str(job.get("output") or "")
                if job.get("status") == "completed" and os.path.isfile(output_path):
                    continue
                index = int(job.get("index") or 0)
                job["status"] = "running"
                job["error"] = ""
                self._save_manifest(manifest_path, manifest)
                self.job_status.emit(index, "正在生成", "#38BDF8")
                intro_plan = dict(job.get("intro_plan") or {})
                audio_plan = dict(job.get("audio_plan") or {})
                plan_notes = []
                if intro_plan.get("path"):
                    plan_notes.append(
                        f"片头：{os.path.basename(str(intro_plan['path']))}"
                        f"（{float(intro_plan.get('duration') or 0.0):.2f} 秒）"
                    )
                if audio_plan.get("path"):
                    plan_notes.append(
                        f"正文音频：{os.path.basename(str(audio_plan['path']))}"
                        f"（{float(job.get('target_duration') or 0.0):.2f} 秒）"
                    )
                if plan_notes:
                    self.log.emit(
                        f"开始生成第 {index}/{total} 个局部替换视频；"
                        + "；".join(plan_notes)
                    )
                else:
                    self.log.emit(f"开始生成第 {index}/{total} 个局部替换视频")
                try:
                    rendered = self._render_job(project, job, source_info, encoder)
                except RuntimeError as exc:
                    if encoder != "nvenc" or self._is_stopping():
                        raise
                    self.log.emit(f"NVENC 失败，当前视频自动切换 CPU：{exc}")
                    encoder = "cpu"
                    rendered = self._render_job(project, job, source_info, encoder)
                self._verify_output(
                    rendered,
                    source_info,
                    float(
                        job.get("total_duration")
                        or job.get("target_duration")
                        or source_info.get("duration")
                        or 0.0
                    ),
                    bool(
                        intro_plan.get("path")
                        or audio_plan.get("path")
                        or source_info.get("has_audio")
                        or (project.get("settings") or {}).get(
                            "smart_sfx_enabled",
                            False,
                        )
                    ),
                )
                used_paths = [
                    clip.get("path")
                    for plan in job.get("slot_plans") or []
                    for clip in plan.get("clips") or []
                    if clip.get("path")
                ]
                if audio_plan.get("path"):
                    used_paths.append(audio_plan["path"])
                if intro_plan.get("path"):
                    used_paths.append(intro_plan["path"])
                ledger.add(used_paths)
                ledger.save()
                job["status"] = "completed"
                job["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                job["encoder"] = encoder
                self._save_manifest(manifest_path, manifest)
                completed_count += 1
                self.job_status.emit(index, "已完成", "#22C55E")
                self.progress.emit(completed_count, total, f"已完成 {completed_count}/{total}")

            self.completed.emit(True, f"全部完成，共生成 {completed_count} 个视频")
        except InterruptedError as exc:
            self.completed.emit(False, str(exc))
        except Exception as exc:
            for job in (locals().get("manifest") or {}).get("jobs") or []:
                if job.get("status") == "running":
                    job["status"] = "failed"
                    job["error"] = str(exc)
                    self.job_status.emit(
                        int(job.get("index") or 0), "失败", "#EF4444"
                    )
            manifest_path = locals().get("manifest_path")
            manifest = locals().get("manifest")
            if manifest_path and manifest:
                self._save_manifest(manifest_path, manifest)
            self.log.emit(f"生成失败：{exc}")
            self.completed.emit(False, f"生成失败：{exc}")
        finally:
            self._terminate_current_process()
