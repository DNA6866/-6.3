import os
import subprocess
import time

from PyQt5.QtCore import QThread, pyqtSignal

from .parser import download_short_video, parse_short_video, safe_filename


class ShortVideoParseWorker(QThread):
    message = pyqtSignal(str)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, raw_text, cookie_options=None):
        super().__init__()
        self.raw_text = raw_text
        self.cookie_options = cookie_options or {}

    def run(self):
        try:
            self.message.emit("正在解析短视频链接...")
            info = parse_short_video(self.raw_text, self.cookie_options)
            self.finished.emit(info)
        except Exception as exc:
            self.failed.emit(str(exc))


class ShortVideoDownloadWorker(QThread):
    message = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, info, output_dir, ffmpeg_path="", kind="video", cookie_options=None):
        super().__init__()
        self.info = info
        self.output_dir = output_dir
        self.ffmpeg_path = ffmpeg_path
        self.kind = kind
        self.cookie_options = cookie_options or {}

    def run(self):
        try:
            action = "音频" if self.kind == "audio" else "无水印原视频"
            self.message.emit(f"正在下载{action}...")
            output = download_short_video(self.info, self.output_dir, self.ffmpeg_path, self.kind, self.cookie_options)
            self.finished.emit(output)
        except Exception as exc:
            self.failed.emit(str(exc))


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _run_ffmpeg(cmd):
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        startupinfo=_hidden_startupinfo(),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout or "ffmpeg 音频处理失败").strip())


class ShortVideoTranscriptWorker(QThread):
    message = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, info, temp_dir, output_dir, ffmpeg_path, sensevoice_model_dir, cookie_options=None):
        super().__init__()
        self.info = info
        self.temp_dir = temp_dir
        self.output_dir = output_dir
        self.ffmpeg_path = ffmpeg_path
        self.sensevoice_model_dir = sensevoice_model_dir
        self.cookie_options = cookie_options or {}

    def run(self):
        audio_path = ""
        wav_path = ""
        try:
            if not self.info or self.info.get("partial"):
                raise RuntimeError("未解析到可识别的视频流，暂时只能显示分享文本。")
            if not self.ffmpeg_path or not os.path.exists(self.ffmpeg_path):
                raise RuntimeError("未找到 ffmpeg.exe，无法提取视频音频。")

            from services.asr_service import (
                sensevoice_model_ready,
                sensevoice_runtime_info,
                transcribe_to_txt_with_sensevoice,
            )

            if not sensevoice_model_ready(self.sensevoice_model_dir):
                raise RuntimeError("SenseVoiceSmall 模型目录不完整，无法自动识别口播文案。")

            os.makedirs(self.temp_dir, exist_ok=True)
            os.makedirs(self.output_dir, exist_ok=True)
            title = safe_filename(self.info.get("title") or self.info.get("caption") or "短视频")
            stamp = time.strftime("%Y%m%d_%H%M%S")

            self.progress.emit(15)
            self.message.emit("正在提取视频音频，用于识别口播文案...")
            audio_path = download_short_video(
                self.info,
                self.temp_dir,
                self.ffmpeg_path,
                "audio",
                self.cookie_options,
            )

            self.progress.emit(40)
            self.message.emit("正在转换为识别专用音频格式...")
            wav_path = os.path.join(self.temp_dir, f"{title}_{stamp}_asr.wav")
            _run_ffmpeg([
                self.ffmpeg_path,
                "-y", "-hide_banner", "-loglevel", "error",
                "-i", audio_path,
                "-ar", "16000",
                "-ac", "1",
                "-c:a", "pcm_s16le",
                wav_path,
            ])

            device, loaded = sensevoice_runtime_info()
            self.progress.emit(60)
            self.message.emit(f"正在用 SenseVoice 识别口播文案，设备：{device}，模型状态：{'已加载' if loaded else '首次加载'}...")
            txt_path = os.path.join(self.output_dir, f"{title}_口播文案_{stamp}.txt")
            transcribe_to_txt_with_sensevoice(
                wav_path,
                self.sensevoice_model_dir,
                txt_path,
                event_callback=lambda event: self.message.emit(
                    str(event.get("message") or "")
                )
                if event.get("message")
                else None,
                context="短视频口播",
            )
            with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read().strip()
            if not text:
                raise RuntimeError("没有识别到有效口播文案。")

            self.progress.emit(100)
            self.finished.emit({
                "text": text,
                "txt_path": txt_path,
                "audio_cache": audio_path,
            })
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            for path in (audio_path, wav_path):
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
