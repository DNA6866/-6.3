import os
import subprocess
import threading
import glob

from PyQt5.QtCore import QThread, pyqtSignal

from .live_parser import build_segment_output_pattern, parse_live_url


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


class LiveParseWorker(QThread):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, text):
        super().__init__()
        self.text = text

    def run(self):
        try:
            self.finished.emit(parse_live_url(self.text))
        except Exception as exc:
            self.failed.emit(str(exc))


class LiveDownloadWorker(QThread):
    message = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, ffmpeg_path, stream_url, output_dir, title, ext="mp4"):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.stream_url = stream_url
        self.output_dir = output_dir
        self.title = title
        self.ext = ext if ext in ("mp4", "flv", "mkv", "ts") else "mp4"
        self.process = None
        self.output_path = ""
        self.output_prefix = ""
        self.segment_seconds = 600
        self._stopping = False
        self._force_timer = None

    def stop(self):
        self._stopping = True
        if self.process and self.process.poll() is None:
            self.message.emit("正在停止录制，等待 ffmpeg 正常写出文件索引...")
            try:
                if self.process.stdin:
                    # ffmpeg 收到 q 会正常收尾，比 terminate 更不容易损坏 mp4。
                    self.process.stdin.write("q\n")
                    self.process.stdin.flush()
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass
            self._force_timer = threading.Timer(15, self._force_stop_if_needed)
            self._force_timer.daemon = True
            self._force_timer.start()

    def _force_stop_if_needed(self):
        if not self.process or self.process.poll() is not None:
            return
        self.message.emit("正常停止超时，正在强制结束录制进程。当前文件可能不完整。")
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass

    def _segment_files(self):
        if not self.output_prefix:
            return []
        files = glob.glob(f"{self.output_prefix}_*.mp4")
        return sorted(path for path in files if os.path.exists(path) and os.path.getsize(path) > 1024)

    def run(self):
        try:
            if not os.path.exists(self.ffmpeg_path):
                raise RuntimeError("未找到 tools/ffmpeg.exe，无法下载直播流。")
            self.output_path, self.output_prefix = build_segment_output_pattern(self.output_dir, self.title, "mp4")
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-fflags",
                "+genpts",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-rw_timeout",
                "15000000",
                "-i",
                self.stream_url,
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "2",
                "-force_key_frames",
                f"expr:gte(t,n_forced*{self.segment_seconds})",
                "-f",
                "segment",
                "-segment_time",
                str(self.segment_seconds),
                "-reset_timestamps",
                "1",
                "-segment_format",
                "mp4",
                "-segment_format_options",
                "movflags=+faststart",
                self.output_path,
            ]
            self.message.emit(f"开始录制直播：每 10 分钟自动保存一个转码 MP4，文件前缀：{self.output_prefix}")
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                startupinfo=hidden_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            while True:
                line = self.process.stdout.readline() if self.process.stdout else ""
                if line:
                    text = line.strip()
                    if text:
                        self.message.emit(text)
                if self.process.poll() is not None:
                    break
            if self._force_timer:
                self._force_timer.cancel()
            code = self.process.returncode
            segment_files = self._segment_files()
            if self._stopping:
                if not segment_files:
                    raise RuntimeError("已停止录制，但文件未能正常写出。请重新开始录制并稍等几秒后再停止。")
                self.message.emit(f"录制已停止，已生成 {len(segment_files)} 个转码 MP4 分段。")
                self.finished.emit(os.path.dirname(segment_files[-1]))
                return
            if code != 0:
                raise RuntimeError("直播下载失败，请确认直播仍在进行，或尝试重新解析链接。")
            if not segment_files:
                raise RuntimeError("下载结束但未生成有效文件。")
            self.message.emit(f"录制结束，已生成 {len(segment_files)} 个转码 MP4 分段。")
            self.finished.emit(os.path.dirname(segment_files[-1]))
        except Exception as exc:
            self.failed.emit(str(exc))
