import os
import subprocess
import time

from PyQt5.QtCore import QThread, pyqtSignal

from paths import models_dir, tools_dir
from .config import APP_ROOT, OUTPUT_DIR
from .env_check import check_environment
from .audio_tools import extract_audio_from_media, normalize_audio_loudness, make_output_path
from .runtime_support import RUNTIME_LOG_FILE, cancel_voice_task, run_voice_task


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _find_whisper_exe():
    local_tools_dir = tools_dir()
    for rel in ["nv/whisper.exe", "amd/whisper.exe", "cpu/whisper.exe", "whisper_cpu.exe", "whisper.exe"]:
        path = os.path.join(local_tools_dir, rel)
        if os.path.exists(path):
            return path
    return ""


def _find_whisper_model():
    local_tools_dir = tools_dir()
    if not os.path.isdir(local_tools_dir):
        return ""
    for root, _, files in os.walk(local_tools_dir):
        for name in files:
            if name.lower().endswith(".bin"):
                return os.path.join(root, name)
    return ""


def _find_ffmpeg():
    bundled = os.path.join(tools_dir(), "ffmpeg.exe")
    if os.path.exists(bundled):
        return bundled
    return ""


def _run_cmd(cmd, cwd=None):
    res = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        startupinfo=_hidden_startupinfo(),
    )
    if res.returncode != 0:
        lines = [line for line in (res.stdout or "").splitlines() if line.strip()]
        raise RuntimeError(" | ".join(lines[-8:]) if lines else "命令执行失败")


class EnvCheckWorker(QThread):
    progress = pyqtSignal(int)
    message = pyqtSignal(str)
    finished = pyqtSignal(object, str, bool)
    failed = pyqtSignal(str)

    def __init__(self, model_path, output_dir):
        super().__init__()
        self.model_path = model_path
        self.output_dir = output_dir

    def run(self):
        try:
            self.message.emit("正在检测运行环境...")
            self.progress.emit(10)
            results, report_path, passed = check_environment(self.model_path, self.output_dir)
            self.progress.emit(100)
            self.message.emit("环境检测完成。")
            self.finished.emit(results, report_path, passed)
        except Exception as exc:
            self.failed.emit(f"环境检测失败：{exc}")


class VoiceGenerateWorker(QThread):
    progress = pyqtSignal(int)
    message = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, task):
        super().__init__()
        self.task = task
        self._process_holder = {}

    def cancel(self):
        cancel_voice_task(self._process_holder)

    def _on_event(self, event):
        if "progress" in event:
            self.progress.emit(int(event["progress"]))
        if event.get("message"):
            self.message.emit(str(event["message"]))

    def run(self):
        try:
            self.progress.emit(5)
            result = run_voice_task(
                self.task,
                event_callback=self._on_event,
                process_holder=self._process_holder,
            )
            self.progress.emit(100)
            self.finished.emit(result["output"])
        except Exception as exc:
            self.failed.emit(str(exc))


class BatchGenerateWorker(QThread):
    progress = pyqtSignal(int)
    message = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, task):
        super().__init__()
        self.task = task
        self._process_holder = {}

    def cancel(self):
        cancel_voice_task(self._process_holder)

    def _on_event(self, event):
        if "progress" in event:
            self.progress.emit(int(event["progress"]))
        if event.get("message"):
            self.message.emit(str(event["message"]))

    def run(self):
        try:
            input_file = self.task.get("input_file", "")
            output_dir = self.task.get("output_dir") or OUTPUT_DIR
            if not input_file or not os.path.exists(input_file):
                raise RuntimeError("批量 txt 文件不存在。")
            with open(input_file, "r", encoding="utf-8", errors="ignore") as f:
                texts = [line.strip() for line in f if line.strip()]
            if not texts:
                raise RuntimeError("批量 txt 文件为空。")
            os.makedirs(output_dir, exist_ok=True)
            self.progress.emit(5)
            task = dict(self.task)
            task.update({
                "operation": "batch",
                "texts": texts,
                "output_dir": output_dir,
                "output_paths": [
                    make_output_path(output_dir, f"batch_{index:03d}", "wav")
                    for index in range(1, len(texts) + 1)
                ],
            })
            result = run_voice_task(
                task,
                event_callback=self._on_event,
                process_holder=self._process_holder,
            )
            self.finished.emit(
                f"批量生成完成：成功 {result.get('success', 0)} 条，"
                f"失败 {result.get('failed', 0)} 条，输出目录：{output_dir}"
            )
        except Exception as exc:
            self.failed.emit(f"批量生成失败：{exc}")


class AudioPrepareWorker(QThread):
    progress = pyqtSignal(int)
    message = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, task):
        super().__init__()
        self.task = task

    def run(self):
        try:
            ffmpeg = _find_ffmpeg()
            if not ffmpeg:
                raise RuntimeError("未找到 ffmpeg，无法提取或裁剪音频。")
            input_path = self.task.get("input_path", "")
            if not input_path or not os.path.exists(input_path):
                raise RuntimeError("输入文件不存在。")
            output_dir = self.task.get("output_dir") or OUTPUT_DIR
            os.makedirs(output_dir, exist_ok=True)
            prefix = self.task.get("prefix", "reference_audio")
            output_path = make_output_path(output_dir, prefix, "wav")
            start = float(self.task.get("start", 0) or 0)
            duration = float(self.task.get("duration", 0) or 0)
            self.progress.emit(20)
            self.message.emit("正在提取/裁剪参考音频...")
            extract_audio_from_media(ffmpeg, input_path, output_path, start=start, duration=duration, sample_rate=16000)
            if self.task.get("normalize_audio"):
                normalized = make_output_path(output_dir, f"{prefix}_norm", "wav")
                self.message.emit("正在统一音量...")
                normalize_audio_loudness(ffmpeg, output_path, normalized, sample_rate=16000)
                try:
                    os.remove(output_path)
                except Exception:
                    pass
                output_path = normalized
            self.progress.emit(100)
            self.finished.emit(output_path)
        except Exception as exc:
            self.failed.emit(f"参考音频处理失败：{exc}")


class ReferenceTranscribeWorker(QThread):
    progress = pyqtSignal(int)
    message = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, audio_path, output_dir=None, asr_model_path=None):
        super().__init__()
        self.audio_path = audio_path
        self.output_dir = output_dir or OUTPUT_DIR
        self.asr_model_path = asr_model_path or os.path.join(models_dir(), "SenseVoiceSmall")
        self._process_holder = {}

    def cancel(self):
        cancel_voice_task(self._process_holder)

    def _on_event(self, event):
        if "progress" in event:
            self.progress.emit(int(event["progress"]))
        if event.get("message"):
            self.message.emit(str(event["message"]))

    def _transcribe_with_sensevoice(self):
        if not self.asr_model_path or not os.path.isdir(self.asr_model_path):
            raise RuntimeError("未找到 SenseVoiceSmall 模型目录")
        model_file = os.path.join(self.asr_model_path, "model.pt")
        if not os.path.exists(model_file):
            raise RuntimeError("SenseVoiceSmall 模型文件不完整")
        result = run_voice_task(
            {
                "operation": "transcribe",
                "kind": "transcribe",
                "audio_path": self.audio_path,
                "asr_model_path": self.asr_model_path,
            },
            event_callback=self._on_event,
            process_holder=self._process_holder,
        )
        text = str(result.get("text") or "").strip()
        if not text:
            raise RuntimeError("SenseVoice 没有识别到有效文字")
        return text

    def _transcribe_with_whisper(self):
        temp_wav = ""
        ffmpeg = _find_ffmpeg()
        whisper = _find_whisper_exe()
        model = _find_whisper_model()
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg，无法预处理参考音频。")
        if not whisper or not model:
            raise RuntimeError("未找到本地 Whisper 程序或识别模型，无法自动识别参考音频文字稿。")

        os.makedirs(self.output_dir, exist_ok=True)
        self.message.emit("正在整理参考音频...")
        self.progress.emit(15)
        temp_wav = os.path.join(self.output_dir, f"ultimate_ref_{int(time.time())}_{os.getpid()}.wav")
        try:
            _run_cmd([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", self.audio_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", temp_wav])
            self.message.emit("正在使用 Whisper 识别参考音频文字稿...")
            self.progress.emit(45)
            _run_cmd([whisper, "-m", model, "-f", temp_wav, "-otxt", "-l", "zh", "--prompt", "简体中文"], cwd=self.output_dir)
            txt_path = temp_wav + ".txt"
            if not os.path.exists(txt_path):
                raise RuntimeError("识别已结束，但没有生成文字稿文件。")
            with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
                text = " ".join(f.read().split())
            if not text:
                raise RuntimeError("没有识别到有效文字，请换一段人声更清晰的参考音频。")
            return text
        finally:
            for path in [temp_wav, temp_wav + ".txt" if temp_wav else ""]:
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass

    def run(self):
        try:
            if not self.audio_path or not os.path.exists(self.audio_path):
                raise RuntimeError("参考音频不存在，请重新选择音频文件。")
            try:
                text = self._transcribe_with_sensevoice()
            except Exception as sense_error:
                self.message.emit(f"SenseVoice 不可用，切换 Whisper 识别：{sense_error}")
                text = self._transcribe_with_whisper()
            self.progress.emit(100)
            self.message.emit("参考音频文字稿识别完成。")
            self.finished.emit(text)
        except Exception as exc:
            self.failed.emit(
                "参考音频文字稿自动识别失败。请确认 SenseVoiceSmall 模型存在、参考音频有人声，"
                f"并尽量使用 wav/mp3 格式。底层信息：{exc}。完整日志：{RUNTIME_LOG_FILE}"
            )
