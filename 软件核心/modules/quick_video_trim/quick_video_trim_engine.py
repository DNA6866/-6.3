import hashlib
import json
import os
import re
import subprocess
import time
from array import array

from PyQt5.QtCore import QThread, pyqtSignal

from paths import logs_dir
from services.job_resume_service import (
    JobResumeStore,
    source_identity,
    stable_job_key,
)
from services.media_validation_service import (
    atomic_media_path,
    hidden_creation_flags,
    validate_media_output,
)


VIDEO_EXTENSIONS = (
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".ts", ".mts", ".m2ts",
)
PREVIEW_CACHE_VERSION = 2


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def safe_filename(value):
    value = re.sub(r'[\\/:*?"<>|]+', "_", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value[:100] or "video"


def format_time(seconds, milliseconds=True):
    seconds = max(0.0, float(seconds or 0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if milliseconds:
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"
    return f"{hours:02d}:{minutes:02d}:{int(secs):02d}"


def _parse_rate(value):
    try:
        numerator, denominator = str(value or "0/1").split("/", 1)
        return float(numerator) / max(1.0, float(denominator))
    except Exception:
        return 0.0


def probe_video(ffprobe_path, source):
    command = [
        ffprobe_path,
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-of", "json",
        source,
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=hidden_startupinfo(),
        creationflags=hidden_creation_flags(),
        timeout=45,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout or "ffprobe 无法读取该视频").strip())
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    if not video:
        raise RuntimeError("文件中没有可读取的视频画面。")

    duration = float(
        video.get("duration")
        or (data.get("format") or {}).get("duration")
        or 0
    )
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotation = 0
    tags = video.get("tags") or {}
    try:
        rotation = int(float(tags.get("rotate") or 0))
    except Exception:
        rotation = 0
    for side_data in video.get("side_data_list") or []:
        if "rotation" in side_data:
            try:
                rotation = int(float(side_data.get("rotation") or 0))
            except Exception:
                pass
    display_width, display_height = width, height
    if abs(rotation) % 180 == 90:
        display_width, display_height = height, width

    return {
        "duration": max(0.0, duration),
        "width": width,
        "height": height,
        "display_width": display_width,
        "display_height": display_height,
        "rotation": rotation,
        "fps": _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "video_codec": video.get("codec_name") or "未知",
        "audio_codec": audio.get("codec_name") or "无音频",
        "size": int((data.get("format") or {}).get("size") or os.path.getsize(source)),
    }


def _cache_key(source, cache_identity=""):
    if cache_identity:
        text = f"{PREVIEW_CACHE_VERSION}|identity|{cache_identity}"
        return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:20]
    stat = os.stat(source)
    text = (
        f"{PREVIEW_CACHE_VERSION}|{os.path.abspath(source)}|"
        f"{stat.st_size}|{stat.st_mtime_ns}"
    )
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _waveform_peaks(ffmpeg_path, source, points=1200):
    command = [
        ffmpeg_path,
        "-hide_banner", "-loglevel", "error",
        "-i", source,
        "-vn", "-ac", "1", "-ar", "4000",
        "-f", "s16le", "pipe:1",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=hidden_startupinfo(),
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0 or not result.stdout:
        return []
    samples = array("h")
    samples.frombytes(result.stdout)
    if not samples:
        return []
    chunk = max(1, len(samples) // max(1, points))
    peaks = []
    maximum = 1
    for offset in range(0, len(samples), chunk):
        block = samples[offset:offset + chunk]
        peak = max((abs(value) for value in block), default=0)
        peaks.append(peak)
        maximum = max(maximum, peak)
    return [value / maximum for value in peaks[:points]]


class PreviewBuildWorker(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(str, dict)
    failed = pyqtSignal(str, str)

    def __init__(
        self,
        source,
        ffmpeg_path,
        ffprobe_path,
        cache_root,
        thumbnail_count=18,
        cache_identity="",
    ):
        super().__init__()
        self.source = os.path.abspath(source)
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.cache_root = cache_root
        self.thumbnail_count = max(8, int(thumbnail_count))
        self.cache_identity = str(cache_identity or "")
        self._process = None

    def cancel(self):
        self.requestInterruption()
        process = self._process
        if process and process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        startupinfo=hidden_startupinfo(),
                        timeout=10,
                        check=False,
                    )
                else:
                    process.terminate()
            except Exception:
                pass

    def run(self):
        try:
            self.progress.emit(5, "正在读取视频信息...")
            metadata = probe_video(self.ffprobe_path, self.source)
            if self.isInterruptionRequested():
                return

            cache_dir = os.path.join(
                self.cache_root,
                _cache_key(self.source, self.cache_identity),
            )
            os.makedirs(cache_dir, exist_ok=True)
            thumbnails = [
                os.path.join(cache_dir, f"thumb_{index:03d}.jpg")
                for index in range(1, self.thumbnail_count + 1)
            ]
            existing = [path for path in thumbnails if os.path.isfile(path)]
            if len(existing) < self.thumbnail_count:
                for path in existing:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                self.progress.emit(20, "正在生成完整时间轴缩略图...")
                duration = max(0.1, metadata.get("duration") or 0.1)
                fps = self.thumbnail_count / duration
                output_pattern = os.path.join(cache_dir, "thumb_%03d.jpg")
                video_filter = (
                    f"fps={fps:.8f},"
                    "scale=180:100:force_original_aspect_ratio=decrease"
                )
                command = [
                    self.ffmpeg_path,
                    "-y", "-hide_banner", "-loglevel", "error",
                    "-i", self.source,
                    "-vf", video_filter,
                    "-frames:v", str(self.thumbnail_count),
                    "-q:v", "4",
                    output_pattern,
                ]
                self._process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    startupinfo=hidden_startupinfo(),
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    _, stderr = self._process.communicate(
                        timeout=max(60, min(600, int(duration * 5)))
                    )
                except subprocess.TimeoutExpired as exc:
                    self._process.kill()
                    self._process.communicate(timeout=5)
                    raise RuntimeError("时间轴缩略图生成超时") from exc
                if self.isInterruptionRequested():
                    return
                if self._process.returncode != 0:
                    detail = (stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(detail or "时间轴缩略图生成失败")
            else:
                now = time.time()
                for path in existing:
                    try:
                        os.utime(path, (now, now))
                    except OSError:
                        pass

            self.progress.emit(70, "正在分析音频波形...")
            waveform_path = os.path.join(cache_dir, "waveform.json")
            peaks = []
            try:
                with open(waveform_path, "r", encoding="utf-8") as handle:
                    cached_peaks = json.load(handle)
                if isinstance(cached_peaks, list) and cached_peaks:
                    peaks = [float(value) for value in cached_peaks]
                    os.utime(waveform_path, None)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                peaks = []
            if not peaks:
                peaks = _waveform_peaks(self.ffmpeg_path, self.source)
                if peaks:
                    temp_waveform_path = waveform_path + ".tmp"
                    try:
                        with open(temp_waveform_path, "w", encoding="utf-8") as handle:
                            json.dump(peaks, handle, separators=(",", ":"))
                        os.replace(temp_waveform_path, waveform_path)
                    except OSError:
                        try:
                            os.remove(temp_waveform_path)
                        except OSError:
                            pass
            if self.isInterruptionRequested():
                return
            thumbnails = [path for path in thumbnails if os.path.isfile(path)]
            metadata["thumbnails"] = thumbnails
            metadata["waveform"] = peaks
            self.progress.emit(100, "预览时间轴已准备完成。")
            self.finished.emit(self.source, metadata)
        except Exception as exc:
            self.failed.emit(self.source, str(exc))
        finally:
            self._process = None


class ClipExportWorker(QThread):
    progress = pyqtSignal(int, int, int)
    message = pyqtSignal(str)
    file_finished = pyqtSignal(str)
    finished = pyqtSignal(int, int, str)
    failed = pyqtSignal(str)

    def __init__(self, tasks, ffmpeg_path, output_dir, mode, hardware_level="cpu"):
        super().__init__()
        self.tasks = list(tasks)
        self.ffmpeg_path = ffmpeg_path
        self.output_dir = output_dir
        self.mode = mode
        self.hardware_level = hardware_level
        self._stop_requested = False
        self._process = None
        self.log_path = os.path.join(logs_dir(), "quick_video_trim_latest.log")
        probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        self.ffprobe_path = os.path.join(os.path.dirname(ffmpeg_path), probe_name)

    def _task_key(self, task):
        return stable_job_key(
            [
                source_identity(str(task.get("source") or "")),
                round(float(task.get("start") or 0.0), 6),
                round(float(task.get("end") or 0.0), 6),
                int(task.get("number") or 0),
                self.mode,
            ]
        )

    def _resume_store(self):
        task_keys = [self._task_key(task) for task in self.tasks]
        fingerprint = stable_job_key(
            [
                os.path.normcase(os.path.abspath(self.output_dir)),
                self.mode,
                task_keys,
            ]
        )
        path = os.path.join(
            logs_dir(),
            f"quick_trim_resume_{fingerprint[:16]}.json",
        )
        return JobResumeStore(path, fingerprint)

    def cancel(self):
        self._stop_requested = True
        process = self._process
        if process and process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        startupinfo=hidden_startupinfo(),
                        timeout=10,
                        check=False,
                    )
                else:
                    process.terminate()
            except Exception:
                pass

    def _output_path(self, task):
        base = safe_filename(os.path.splitext(os.path.basename(task["source"]))[0])
        start_tag = format_time(task["start"], milliseconds=False).replace(":", "")
        end_tag = format_time(task["end"], milliseconds=False).replace(":", "")
        filename = f"{base}_高潮{task['number']:02d}_{start_tag}-{end_tag}.mp4"
        path = os.path.join(self.output_dir, filename)
        if not os.path.exists(path):
            return path
        stamp = time.strftime("%H%M%S")
        return os.path.join(
            self.output_dir,
            f"{os.path.splitext(filename)[0]}_{stamp}_{time.time_ns() % 10000:04d}.mp4",
        )

    def _precise_video_args(self, level):
        if level == "nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "17", "-b:v", "0"]
        if level == "amf":
            return [
                "-c:v", "h264_amf", "-quality", "quality",
                "-rc", "cqp", "-qp_i", "18", "-qp_p", "20",
            ]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]

    def _build_command(self, task, output_path, level=None):
        start = max(0.0, float(task["start"]))
        duration = max(0.05, float(task["end"]) - start)
        common = [
            self.ffmpeg_path,
            "-y", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats",
            "-ss", f"{start:.6f}",
            "-i", task["source"],
            "-t", f"{duration:.6f}",
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-map_metadata", "0",
        ]
        if self.mode == "fast":
            return common + [
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                output_path,
            ]
        return common + self._precise_video_args(level or self.hardware_level) + [
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]

    def _run_command(self, command, task_duration, log_handle, base_progress, total):
        log_handle.write("\n" + subprocess.list2cmdline(command) + "\n")
        log_handle.flush()
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            startupinfo=hidden_startupinfo(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        output_lines = []
        for raw_line in iter(self._process.stdout.readline, ""):
            if self._stop_requested:
                self.cancel()
                break
            line = raw_line.strip()
            if line:
                output_lines.append(line)
                log_handle.write(line + "\n")
            if line.startswith("out_time_ms="):
                try:
                    elapsed = int(line.split("=", 1)[1]) / 1_000_000
                    local = min(1.0, elapsed / max(0.05, task_duration))
                    overall = int((base_progress + local) / max(1, total) * 100)
                    self.progress.emit(overall, base_progress + 1, total)
                except Exception:
                    pass
        return_code = self._process.wait()
        self._process = None
        if self._stop_requested:
            raise InterruptedError("任务已停止")
        if return_code != 0:
            raise RuntimeError("\n".join(output_lines[-20:]) or f"ffmpeg 退出码 {return_code}")

    def run(self):
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        success = 0
        failed_count = 0
        total = len(self.tasks)
        resume_store = self._resume_store()
        try:
            with open(self.log_path, "w", encoding="utf-8", errors="replace") as log:
                log.write(f"视频快速剪辑导出日志 {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                for index, task in enumerate(self.tasks):
                    if self._stop_requested:
                        break
                    task_key = self._task_key(task)
                    resume_record = resume_store.job(task_key)
                    output_path = str(resume_record.get("output") or "")
                    if output_path:
                        valid, _detail = validate_media_output(
                            self.ffprobe_path,
                            output_path,
                            require_video=True,
                            minimum_bytes=1024,
                        )
                        if valid:
                            resume_store.update(
                                task_key,
                                "completed",
                                output_path,
                            )
                            success += 1
                            self.message.emit(
                                f"断点续传：跳过已完成片段 {index + 1}/{total}"
                            )
                            self.file_finished.emit(output_path)
                            self.progress.emit(
                                int((index + 1) / max(1, total) * 100),
                                index + 1,
                                total,
                            )
                            continue
                        resume_store.update(
                            task_key,
                            "pending",
                            output_path,
                            "原完成文件未通过校验，已重新导出",
                        )
                    if not output_path:
                        output_path = self._output_path(task)
                    resume_store.update(task_key, "running", output_path)
                    partial_path = atomic_media_path(output_path)
                    duration = float(task["end"]) - float(task["start"])
                    self.message.emit(
                        f"正在导出 {index + 1}/{total}："
                        f"{os.path.basename(task['source'])} 高潮{task['number']:02d}"
                    )
                    command = self._build_command(task, partial_path)
                    try:
                        self._run_command(command, duration, log, index, total)
                    except Exception as exc:
                        if self._stop_requested:
                            resume_store.update(
                                task_key,
                                "pending",
                                output_path,
                                "任务中断，等待下次继续",
                            )
                            try:
                                if os.path.isfile(partial_path):
                                    os.remove(partial_path)
                            except OSError:
                                pass
                            break
                        if self.mode == "precise" and self.hardware_level in {"nvenc", "amf"}:
                            self.message.emit("硬件精准编码失败，正在自动切换 CPU 精准编码...")
                            try:
                                if os.path.exists(partial_path):
                                    os.remove(partial_path)
                            except OSError:
                                pass
                            command = self._build_command(task, partial_path, level="cpu")
                            try:
                                self._run_command(command, duration, log, index, total)
                            except Exception as fallback_exc:
                                failed_count += 1
                                resume_store.update(
                                    task_key,
                                    "failed",
                                    output_path,
                                    str(fallback_exc),
                                )
                                self.message.emit(f"导出失败：{fallback_exc}")
                                try:
                                    if os.path.isfile(partial_path):
                                        os.remove(partial_path)
                                except OSError:
                                    pass
                                continue
                        else:
                            failed_count += 1
                            resume_store.update(
                                task_key,
                                "failed",
                                output_path,
                                str(exc),
                            )
                            self.message.emit(f"导出失败：{exc}")
                            try:
                                if os.path.isfile(partial_path):
                                    os.remove(partial_path)
                            except OSError:
                                pass
                            continue

                    valid, detail = validate_media_output(
                        self.ffprobe_path,
                        partial_path,
                        require_video=True,
                        minimum_bytes=1024,
                    )
                    if valid:
                        os.replace(partial_path, output_path)
                        success += 1
                        resume_store.update(task_key, "completed", output_path)
                        self.file_finished.emit(output_path)
                    else:
                        failed_count += 1
                        resume_store.update(
                            task_key,
                            "failed",
                            output_path,
                            detail,
                        )
                        self.message.emit(
                            f"导出结束但文件未通过完整性校验：{detail}"
                        )
                        try:
                            if os.path.isfile(partial_path):
                                os.remove(partial_path)
                        except OSError:
                            pass
                self.progress.emit(100 if not self._stop_requested else 0, total, total)
            resume_store.finish(
                not self._stop_requested
                and failed_count == 0
                and success == total
            )
            self.finished.emit(success, failed_count, self.output_dir)
        except Exception as exc:
            resume_store.finish(False)
            if self._stop_requested:
                self.finished.emit(success, failed_count, self.output_dir)
            else:
                self.failed.emit(f"{exc}\n完整日志：{self.log_path}")
        finally:
            self._process = None
            try:
                if "partial_path" in locals() and os.path.isfile(partial_path):
                    os.remove(partial_path)
            except OSError:
                pass
