import os
import re
import shutil
import subprocess
import time

from PyQt5.QtCore import QThread, pyqtSignal

from paths import models_dir, tools_dir
from .config import APP_ROOT, OUTPUT_DIR
from .env_check import check_environment
from .voice_engine import VoxCPM2Engine, VoiceCloneError, humanize_error
from .audio_tools import extract_audio_from_media, normalize_audio_loudness, make_output_path


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
    return shutil.which("ffmpeg") or ""


def _clean_asr_text(text):
    text = re.sub(r"<\|[^>]+?\|>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


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
        self.engine = None

    def run(self):
        try:
            self.progress.emit(5)
            self.message.emit("正在加载 VoxCPM2 模型，首次加载可能较慢...")
            self.engine = VoxCPM2Engine(
                self.task["model_path"],
                zipenhancer_model_path=self.task.get("zipenhancer_model_path", ""),
                enable_denoiser=bool(self.task.get("enable_denoiser", False)),
                optimize=bool(self.task.get("optimize", True)),
            )
            self.engine.load_model()
            self.progress.emit(35)

            kind = self.task.get("kind")
            self.message.emit("正在生成音频，请勿重复点击...")
            if kind == "tts":
                output = self.engine.generate_tts(
                    text=self.task["text"],
                    voice_description=self.task.get("voice_description", ""),
                    output_path=self.task["output_path"],
                    cfg_value=self.task.get("cfg_value", 2.0),
                    inference_timesteps=self.task.get("inference_timesteps", 10),
                    normalize=self.task.get("normalize", False),
                )
            elif kind == "clone":
                output = self.engine.generate_clone(
                    text=self.task["text"],
                    reference_audio=self.task["reference_audio"],
                    control_prompt=self.task.get("control_prompt", ""),
                    output_path=self.task["output_path"],
                    cfg_value=self.task.get("cfg_value", 2.0),
                    inference_timesteps=self.task.get("inference_timesteps", 10),
                    normalize=self.task.get("normalize", False),
                    denoise=self.task.get("denoise", False),
                )
            elif kind == "ultimate":
                output = self.engine.generate_ultimate_clone(
                    text=self.task["text"],
                    reference_audio=self.task["reference_audio"],
                    prompt_text=self.task.get("prompt_text", ""),
                    output_path=self.task["output_path"],
                    cfg_value=self.task.get("cfg_value", 2.0),
                    inference_timesteps=self.task.get("inference_timesteps", 10),
                    normalize=self.task.get("normalize", False),
                    denoise=self.task.get("denoise", False),
                )
            else:
                raise VoiceCloneError("未知的音频生成任务。")

            self.progress.emit(100)
            self.message.emit("音频生成完成。")
            self.finished.emit(output)
        except VoiceCloneError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(humanize_error(exc))
        finally:
            if self.engine:
                self.engine.unload_model()


class BatchGenerateWorker(QThread):
    progress = pyqtSignal(int)
    message = pyqtSignal(str)
    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, task):
        super().__init__()
        self.task = task

    def run(self):
        engine = None
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
            self.message.emit(f"正在加载 VoxCPM2 模型，准备批量生成 {len(texts)} 条...")
            self.progress.emit(5)
            engine = VoxCPM2Engine(
                self.task["model_path"],
                zipenhancer_model_path=self.task.get("zipenhancer_model_path", ""),
                enable_denoiser=bool(self.task.get("enable_denoiser", False)),
                optimize=bool(self.task.get("optimize", True)),
            )
            engine.load_model()
            success = 0
            failed = 0
            reference_audio = self.task.get("reference_audio", "")
            prompt_text = self.task.get("prompt_text", "")
            control_prompt = self.task.get("control_prompt", "")
            for index, text in enumerate(texts, 1):
                try:
                    output_path = make_output_path(output_dir, f"batch_{index:03d}", "wav")
                    self.message.emit(f"正在生成 {index}/{len(texts)}...")
                    if reference_audio and prompt_text:
                        engine.generate_ultimate_clone(
                            text=text,
                            reference_audio=reference_audio,
                            prompt_text=prompt_text,
                            output_path=output_path,
                            cfg_value=self.task.get("cfg_value", 2.0),
                            inference_timesteps=self.task.get("inference_timesteps", 10),
                            normalize=self.task.get("normalize", False),
                            denoise=self.task.get("denoise", False),
                        )
                    elif reference_audio:
                        engine.generate_clone(
                            text=text,
                            reference_audio=reference_audio,
                            control_prompt=control_prompt,
                            output_path=output_path,
                            cfg_value=self.task.get("cfg_value", 2.0),
                            inference_timesteps=self.task.get("inference_timesteps", 10),
                            normalize=self.task.get("normalize", False),
                            denoise=self.task.get("denoise", False),
                        )
                    else:
                        engine.generate_tts(
                            text=text,
                            voice_description=control_prompt,
                            output_path=output_path,
                            cfg_value=self.task.get("cfg_value", 2.0),
                            inference_timesteps=self.task.get("inference_timesteps", 10),
                            normalize=self.task.get("normalize", False),
                        )
                    success += 1
                except Exception as item_error:
                    failed += 1
                    self.message.emit(f"第 {index} 条生成失败：{item_error}")
                self.progress.emit(int(index / len(texts) * 100))
            self.finished.emit(f"批量生成完成：成功 {success} 条，失败 {failed} 条，输出目录：{output_dir}")
        except Exception as exc:
            self.failed.emit(f"批量生成失败：{exc}")
        finally:
            if engine:
                engine.unload_model()


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

    def _transcribe_with_sensevoice(self):
        if not self.asr_model_path or not os.path.isdir(self.asr_model_path):
            raise RuntimeError("未找到 SenseVoiceSmall 模型目录")
        model_file = os.path.join(self.asr_model_path, "model.pt")
        if not os.path.exists(model_file):
            raise RuntimeError("SenseVoiceSmall 模型文件不完整")
        from funasr import AutoModel

        self.message.emit("正在使用 SenseVoice 识别参考音频文字稿...")
        self.progress.emit(35)
        model = AutoModel(model=self.asr_model_path, trust_remote_code=False, disable_update=True)
        result = model.generate(input=self.audio_path, language="zh", use_itn=True, batch_size_s=60)
        if isinstance(result, list) and result:
            text = result[0].get("text", "")
        elif isinstance(result, dict):
            text = result.get("text", "")
        else:
            text = str(result)
        text = _clean_asr_text(text)
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
                text = _clean_asr_text(f.read())
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
            self.failed.emit(f"参考音频文字稿自动识别失败：{exc}")
