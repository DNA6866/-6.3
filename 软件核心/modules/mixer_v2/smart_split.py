import os
import re
import subprocess
import time

from PyQt5.QtCore import QThread, pyqtSignal


_PTS_TIME_RE = re.compile(r"\bpts_time:([0-9]+(?:\.[0-9]+)?)")


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _creation_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def consolidate_cut_points(
    raw_points,
    duration,
    minimum_segment=1.0,
    transition_window=0.45,
):
    """合并转场期间的密集切点，并确保首尾及相邻片段不短于设定值。"""
    duration = max(0.0, float(duration or 0.0))
    minimum_segment = max(0.10, float(minimum_segment or 1.0))
    transition_window = max(0.05, float(transition_window or 0.45))
    points = sorted(
        {
            round(float(value), 6)
            for value in raw_points or []
            if 0.001 < float(value) < duration - 0.001
        }
    )
    if not points:
        return []

    clusters = []
    for point in points:
        if not clusters or point - clusters[-1][-1] > transition_window:
            clusters.append([point])
        else:
            clusters[-1].append(point)

    candidates = [
        cluster[len(cluster) // 2]
        for cluster in clusters
        if cluster
    ]
    result = []
    previous = 0.0
    for point in candidates:
        if point - previous < minimum_segment:
            continue
        result.append(point)
        previous = point
    while result and duration - result[-1] < minimum_segment:
        result.pop()
    return [round(point, 6) for point in result]


class SceneDetectWorker(QThread):
    progress = pyqtSignal(int, str)
    completed = pyqtSignal(list, int)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(
        self,
        ffmpeg_path,
        source_path,
        duration,
        threshold=0.32,
        minimum_segment=1.0,
        parent=None,
    ):
        super().__init__(parent)
        self.ffmpeg_path = os.path.abspath(str(ffmpeg_path or ""))
        self.source_path = os.path.abspath(str(source_path or ""))
        self.duration = max(0.0, float(duration or 0.0))
        self.threshold = min(0.90, max(0.05, float(threshold or 0.32)))
        self.minimum_segment = max(0.10, float(minimum_segment or 1.0))
        self._stop_requested = False
        self._process = None

    def stop(self):
        self._stop_requested = True
        self.requestInterruption()
        self._terminate_process()

    def _is_stopping(self):
        return self._stop_requested or self.isInterruptionRequested()

    def _terminate_process(self):
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
                    startupinfo=_hidden_startupinfo(),
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

    def run(self):
        try:
            if not os.path.isfile(self.ffmpeg_path):
                raise FileNotFoundError("未找到 ffmpeg.exe")
            if not os.path.isfile(self.source_path):
                raise FileNotFoundError("竞品原片不存在")

            scene_filter = (
                "scale=320:-2:flags=fast_bilinear,"
                f"select=gt(scene\\,{self.threshold:.3f}),showinfo"
            )
            command = [
                self.ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "info",
                "-i",
                self.source_path,
                "-an",
                "-sn",
                "-dn",
                "-vf",
                scene_filter,
                "-fps_mode",
                "vfr",
                "-progress",
                "pipe:1",
                "-nostats",
                "-f",
                "null",
                os.devnull,
            ]
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=_hidden_startupinfo(),
                creationflags=_creation_flags(),
            )
            started = time.monotonic()
            timeout = max(180.0, self.duration * 12.0)
            raw_points = []
            output_tail = []
            while True:
                if self._is_stopping():
                    self._terminate_process()
                    self.cancelled.emit()
                    return
                if time.monotonic() - started > timeout:
                    self._terminate_process()
                    raise RuntimeError("智能分割分析超时，已安全停止")

                line = self._process.stdout.readline() if self._process.stdout else ""
                if line:
                    output_tail.append(line.strip())
                    output_tail = output_tail[-20:]
                    match = _PTS_TIME_RE.search(line)
                    if match:
                        raw_points.append(float(match.group(1)))
                    if line.startswith(("out_time_us=", "out_time_ms=")):
                        try:
                            seconds = float(line.split("=", 1)[1]) / 1_000_000.0
                        except (TypeError, ValueError):
                            seconds = 0.0
                        percent = (
                            min(99, max(0, int(seconds / self.duration * 100)))
                            if self.duration > 0
                            else 0
                        )
                        self.progress.emit(percent, f"正在分析画面变化：{percent}%")
                elif self._process.poll() is not None:
                    break
                else:
                    self.msleep(25)

            if self._process.returncode != 0:
                detail = "\n".join(item for item in output_tail[-8:] if item)
                raise RuntimeError(detail or "ffmpeg 场景分析失败")
            cut_points = consolidate_cut_points(
                raw_points,
                self.duration,
                self.minimum_segment,
            )
            self.completed.emit(cut_points, len(raw_points))
        except Exception as exc:
            if self._is_stopping():
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
        finally:
            self._process = None
