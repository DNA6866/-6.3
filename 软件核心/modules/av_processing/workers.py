import os
import random
import re
import subprocess
import time

from PyQt5.QtCore import QThread, pyqtSignal

from paths import models_dir
from utils import path_to_ffmpeg

class AVProcessWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, task):
        super().__init__()
        self.task = task

    def _run_cmd(self, cmd, cwd=None):
        self.log_signal.emit(" ".join([str(x) for x in cmd]))
        res = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='ignore',
            startupinfo=self.task.get('startupinfo')
        )
        if res.returncode != 0:
            lines = [line for line in (res.stdout or "").splitlines() if line.strip()]
            raise RuntimeError(" | ".join(lines[-6:]) if lines else "处理失败")

    def _whisper_exe(self):
        tools_dir = self.current_task['tools_dir']
        for rel in ['nv/whisper.exe', 'amd/whisper.exe', 'cpu/whisper.exe', 'whisper_cpu.exe', 'whisper.exe']:
            path = os.path.join(tools_dir, rel)
            if os.path.exists(path):
                return path
        return None

    def _probe_video_size(self, ffprobe, input_path):
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0:s=x', input_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self.task.get('startupinfo')
            )
            text = (res.stdout or "").strip().splitlines()[0]
            w, h = [int(x) for x in text.split('x')[:2]]
            return max(2, w), max(2, h)
        except Exception:
            return 1080, 1920

    def _has_audio_stream(self, ffprobe, input_path):
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=index', '-of', 'csv=p=0', input_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self.task.get('startupinfo')
            )
            return bool((res.stdout or "").strip())
        except Exception:
            return False

    def _probe_video_color_info(self, ffprobe, input_path):
        try:
            res = subprocess.run(
                [
                    ffprobe, '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'stream=pix_fmt,color_space,color_transfer,color_primaries,color_range',
                    '-of', 'default=noprint_wrappers=1',
                    input_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self.task.get('startupinfo'),
                timeout=8,
            )
            info = {}
            for line in (res.stdout or "").splitlines():
                if "=" not in line:
                    continue
                name, value = line.split("=", 1)
                info[name.strip()] = value.strip().lower()
            return info
        except Exception:
            return {}

    @staticmethod
    def _is_hdr_video(color_info):
        transfer = color_info.get('color_transfer', '')
        primaries = color_info.get('color_primaries', '')
        colorspace = color_info.get('color_space', '')
        return (
            transfer in {'smpte2084', 'arib-std-b67'}
            or primaries == 'bt2020'
            or colorspace in {'bt2020nc', 'bt2020c'}
        )

    def _standard_mp4_cmd(self, ffmpeg, ffprobe, input_path, output_path):
        color_info = self._probe_video_color_info(ffprobe, input_path)
        is_hdr = self._is_hdr_video(color_info)
        if is_hdr:
            detail = ", ".join(
                f"{name}={value}"
                for name, value in color_info.items()
                if value and value != "unknown"
            ) or "metadata unknown"
            self.log_signal.emit(f"检测到 HDR/BT.2020 素材，正在转换为标准 SDR MP4：{os.path.basename(input_path)} ({detail})")
            vf = (
                "zscale=t=linear:npl=100,format=gbrpf32le,"
                "tonemap=tonemap=hable:desat=0:peak=1000,"
                "zscale=p=bt709:t=bt709:m=bt709:r=tv,"
                "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
                "format=yuv420p"
            )
        else:
            self.log_signal.emit(f"未检测到 HDR，正在输出标准 SDR MP4：{os.path.basename(input_path)}")
            vf = (
                "scale=w=iw:h=ih:in_range=auto:out_range=tv:"
                "in_color_matrix=auto:out_color_matrix=bt709,"
                "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
                "format=yuv420p"
            )
        return [
            ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
            '-i', input_path,
            '-map', '0:v:0', '-map', '0:a?',
            '-vf', vf,
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '160k',
            '-color_range', 'tv',
            '-color_primaries', 'bt709',
            '-color_trc', 'bt709',
            '-colorspace', 'bt709',
            '-bsf:v', 'h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1',
            '-movflags', '+faststart',
            output_path,
        ]

    def _process_one(self, job):
        self.current_task = job
        kind = job['kind']
        ffmpeg = job['ffmpeg']
        input_path = job['input']
        output_path = job['output']

        if kind == 'extract_audio':
            fmt = job.get('format', 'mp3')
            codec = ['-c:a', 'pcm_s16le'] if fmt == 'wav' else ['-c:a', 'libmp3lame', '-b:a', '192k']
            self._run_cmd([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', input_path, '-vn', *codec, output_path])

        elif kind == 'convert':
            fmt = job.get('format', 'mp4')
            if fmt in ['mp3', 'wav', 'm4a']:
                codec = {
                    'mp3': ['-vn', '-c:a', 'libmp3lame', '-b:a', '192k'],
                    'wav': ['-vn', '-c:a', 'pcm_s16le'],
                    'm4a': ['-vn', '-c:a', 'aac', '-b:a', '192k']
                }[fmt]
            elif fmt == 'avi':
                codec = ['-c:v', 'mpeg4', '-q:v', '4', '-c:a', 'mp3']
            elif job.get('standard_sdr_mp4'):
                ffprobe = job.get('ffprobe') or os.path.join(os.path.dirname(ffmpeg), 'ffprobe.exe')
                self._run_cmd(self._standard_mp4_cmd(ffmpeg, ffprobe, input_path, output_path))
                return
            else:
                codec = ['-c:v', 'libx264', '-preset', 'fast', '-c:a', 'aac', '-b:a', '160k']
            self._run_cmd([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', input_path, *codec, output_path])

        elif kind == 'trim_audio':
            start = str(job.get('start', 0))
            duration = str(job.get('duration', 10))
            fmt = job.get('format', 'mp3')
            codec = ['-c:a', 'pcm_s16le'] if fmt == 'wav' else ['-c:a', 'libmp3lame', '-b:a', '192k']
            self._run_cmd([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-ss', start, '-i', input_path, '-t', duration, '-vn', *codec, output_path])

        elif kind == 'vocal_enhance':
            fmt = job.get('format', 'wav')
            codec = ['-c:a', 'pcm_s16le'] if fmt == 'wav' else ['-c:a', 'libmp3lame', '-b:a', '192k']
            af = 'highpass=f=120,lowpass=f=8000,afftdn=nf=-25,dynaudnorm=f=150:g=15'
            self._run_cmd([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', input_path, '-vn', '-af', af, *codec, output_path])

        elif kind == 'add_cover':
            cover_path = job.get('cover_path', '')
            if not cover_path or not os.path.exists(cover_path):
                raise RuntimeError("未找到封面图片，请先选择有效的封面图片。")
            ffprobe = job.get('ffprobe') or os.path.join(os.path.dirname(ffmpeg), 'ffprobe.exe')
            width, height = self._probe_video_size(ffprobe, input_path)
            cover_seconds = float(job.get('cover_seconds', 2 / 30))
            cover_seconds = max(0.04, min(1.0, cover_seconds))
            has_audio = self._has_audio_stream(ffprobe, input_path)
            token = f"{int(time.time() * 1000)}_{os.getpid()}"
            temp_dir = job.get('output_dir') or os.path.dirname(output_path)
            cover_seg = os.path.join(temp_dir, f"cover_seg_{token}.mp4")
            main_seg = os.path.join(temp_dir, f"main_seg_{token}.mp4")
            concat_txt = os.path.join(temp_dir, f"cover_concat_{token}.txt")
            vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p,fps=30"
            try:
                if has_audio:
                    self._run_cmd([
                        ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-loop', '1', '-framerate', '30', '-t', str(cover_seconds), '-i', cover_path,
                        '-f', 'lavfi', '-t', str(cover_seconds), '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100',
                        '-frames:v', '2', '-vf', vf,
                        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                        '-c:a', 'aac', '-b:a', '160k', '-shortest', cover_seg
                    ])
                    self._run_cmd([
                        ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-i', input_path, '-vf', vf,
                        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                        '-c:a', 'aac', '-b:a', '160k', main_seg
                    ])
                else:
                    self._run_cmd([
                        ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-loop', '1', '-framerate', '30', '-t', str(cover_seconds), '-i', cover_path,
                        '-frames:v', '2', '-vf', vf,
                        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                        '-an', cover_seg
                    ])
                    self._run_cmd([
                        ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-i', input_path, '-vf', vf,
                        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                        '-an', main_seg
                    ])
                safe_cover_seg = cover_seg.replace('\\', '/')
                safe_main_seg = main_seg.replace('\\', '/')
                with open(concat_txt, 'w', encoding='utf-8') as f:
                    f.write(f"file '{safe_cover_seg}'\n")
                    f.write(f"file '{safe_main_seg}'\n")
                self._run_cmd([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', concat_txt, '-c', 'copy', '-movflags', '+faststart', output_path])
            finally:
                for temp_path in (cover_seg, main_seg, concat_txt):
                    try:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                    except Exception:
                        pass

        elif kind == 'text_watermark':
            watermarks = job.get('watermarks')
            if not watermarks:
                watermark = job.get('watermark') or {}
                watermarks = [watermark] if watermark else []
            def split_text_pool(text):
                text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
                return [block.strip() for block in re.split(r"\n\s*\n+", text) if block.strip()]
            def resolve_text(wm):
                if wm.get('use_text_pool'):
                    pool = split_text_pool(wm.get('text_pool', ''))
                    if pool:
                        return random.choice(pool)
                    return ''
                return str(wm.get('text', '')).strip()
            def apply_letter_spacing(text, spacing):
                spacing = max(0, int(float(spacing or 0)))
                if spacing <= 0:
                    return text
                spacer = "\u2006" * max(1, min(10, round(spacing / 4)))
                chars = list(str(text))
                result = []
                for char_index, char in enumerate(chars):
                    result.append(char)
                    if char_index < len(chars) - 1 and char.strip() and chars[char_index + 1].strip():
                        result.append(spacer)
                return "".join(result)
            resolved_watermarks = []
            for wm in watermarks:
                actual_text = resolve_text(wm)
                if actual_text.strip():
                    item = dict(wm)
                    item['actual_text'] = actual_text
                    resolved_watermarks.append(item)
            watermarks = resolved_watermarks
            if not watermarks:
                raise RuntimeError("水印列表为空，请先添加至少一条文字水印。")
            ffprobe = job.get('ffprobe') or os.path.join(os.path.dirname(ffmpeg), 'ffprobe.exe')
            width, height = self._probe_video_size(ffprobe, input_path)
            temp_files = []
            filters = []
            sys_fonts = job.get('sys_fonts') or {}
            try:
                for wm_index, watermark in enumerate(watermarks):
                    text = str(watermark.get('actual_text', watermark.get('text', ''))).strip()
                    design_w = max(1, int(watermark.get('design_w') or width))
                    design_h = max(1, int(watermark.get('design_h') or height))
                    scale = width / design_w
                    font_size = max(8, int(float(watermark.get('size', 60)) * scale))
                    line_step = float(watermark.get('size', 60)) * max(40.0, float(watermark.get('line_spacing', 130) or 130)) / 100.0 * scale
                    letter_spacing = watermark.get('letter_spacing', 0)
                    border_w = max(0, int(float(watermark.get('border_w', 0)) * scale))
                    if watermark.get('is_bold') and border_w <= 0:
                        border_w = 2
                    font_path = watermark.get('font_path') or ''
                    if watermark.get('font_name') == "🎲 随机系统字体" and sys_fonts:
                        font_path = random.choice(list(sys_fonts.values()))
                    font_cmd = f"fontfile='{path_to_ffmpeg(font_path)}':" if font_path and os.path.exists(font_path) else ""
                    color = watermark.get('color') or 'white'
                    if color == "🎲 随机颜色":
                        color = f"#{random.randint(0, 0xFFFFFF):06x}"
                    border_color = watermark.get('border_color') or 'black'
                    if border_color == "🎲 随机颜色":
                        border_color = f"#{random.randint(0, 0xFFFFFF):06x}"
                    align = watermark.get('align') or '居中对齐'
                    base_x = float(watermark.get('x', design_w / 2)) / design_w * width
                    base_y = float(watermark.get('y', design_h / 2)) / design_h * height
                    for line_index, line in enumerate(text.splitlines()):
                        if not line.strip():
                            continue
                        txt_path = os.path.join(job.get('output_dir') or os.path.dirname(output_path), f"av_wm_{int(time.time() * 1000)}_{os.getpid()}_{wm_index}_{line_index}.txt")
                        with open(txt_path, 'w', encoding='utf-8') as f:
                            f.write(apply_letter_spacing(line, letter_spacing))
                        temp_files.append(txt_path)
                        line_y = base_y + line_index * line_step
                        if align == "居中对齐":
                            x_expr = f"{base_x:.2f}-tw/2"
                        elif align == "右对齐":
                            x_expr = f"{base_x:.2f}-tw"
                        else:
                            x_expr = f"{base_x:.2f}"
                        border_cmd = f"borderw={border_w}:bordercolor={border_color}:" if border_w > 0 else ""
                        filters.append(
                            f"drawtext={font_cmd}textfile='{path_to_ffmpeg(txt_path)}':"
                            f"fontsize={font_size}:fontcolor={color}:{border_cmd}"
                            f"x={x_expr}:y={line_y:.2f}"
                        )
                if not filters:
                    raise RuntimeError("水印列表为空，请先添加至少一条文字水印。")
                vf = ",".join(filters + ["format=yuv420p"])
                self._run_cmd([
                    ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                    '-i', input_path,
                    '-map', '0:v:0', '-map', '0:a?',
                    '-vf', vf,
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                    '-c:a', 'aac', '-b:a', '160k',
                    '-movflags', '+faststart',
                    output_path
                ])
            finally:
                for temp_path in temp_files:
                    try:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)
                    except Exception:
                        pass

        elif kind == 'transcribe':
            temp_wav = os.path.join(job['output_dir'], f"asr_{int(time.time())}_{os.getpid()}.wav")
            self._run_cmd([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', input_path, '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', temp_wav])
            try:
                from services.asr_service import sensevoice_model_ready, transcribe_to_txt_with_sensevoice
                sensevoice_dir = os.path.join(models_dir(), 'SenseVoiceSmall')
                if not sensevoice_model_ready(sensevoice_dir):
                    raise RuntimeError("SenseVoiceSmall 模型不可用")
                self.log_signal.emit("正在调用独立 AI 环境进行 SenseVoiceSmall 转写...")
                transcribe_to_txt_with_sensevoice(
                    temp_wav,
                    sensevoice_dir,
                    output_path,
                    event_callback=lambda event: self.log_signal.emit(
                        str(event.get("message") or "")
                    )
                    if event.get("message")
                    else None,
                    context="音视频",
                )
            finally:
                if os.path.exists(temp_wav):
                    os.remove(temp_wav)
            if not os.path.exists(output_path):
                raise RuntimeError("转文本完成但未找到输出文本")

    def run(self):
        try:
            jobs = self.task.get('jobs', [self.task])
            outputs = []
            total = max(1, len(jobs))
            self.progress_signal.emit(2)
            for idx, job in enumerate(jobs, 1):
                self.log_signal.emit(f"开始处理 {idx}/{total}: {os.path.basename(job['input'])}")
                self._process_one(job)
                outputs.append(job['output'])
                self.progress_signal.emit(int(idx / total * 100))
            self.progress_signal.emit(100)
            self.finished_signal.emit(True, "；".join(outputs))
        except Exception as e:
            self.finished_signal.emit(False, str(e))

class ASRPreloadWorker(QThread):
    finished_signal = pyqtSignal(bool, str)

    def run(self):
        session = None
        try:
            from modules.voice_clone.runtime_support import probe_asr_runtime
            from services.asr_service import SenseVoiceIsolatedSession
            runtime_ok, runtime_info = probe_asr_runtime(timeout=120)
            if not runtime_ok:
                raise RuntimeError(
                    runtime_info.get("error")
                    or "独立 AI 环境的 CUDA 张量探针未通过。"
                )
            model_dir = os.path.join(models_dir(), 'SenseVoiceSmall')
            session = SenseVoiceIsolatedSession(model_dir, timeout_sec=180)
            self.finished_signal.emit(
                True,
                "SenseVoiceSmall GPU 环境检测完成："
                f"{runtime_info.get('gpu_name', session.device)}，"
                f"显存 {runtime_info.get('vram_mb', '未知')} MB，"
                f"CUDA {runtime_info.get('cuda_runtime', '未知')}，"
                f"算力 {runtime_info.get('compute_capability', '未知')}。"
            )
        except Exception as e:
            self.finished_signal.emit(False, f"SenseVoiceSmall 独立环境检测失败：{e}")
        finally:
            if session:
                session.close()


class AddSubtitleWorker(QThread):
    """给完整视频添加识别字幕（独立烧录，不改原视频）。"""

    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, task):
        super().__init__()
        self.task = task
        self._sensevoice_session = None

    def _log(self, msg):
        self.log_signal.emit(str(msg))

    def _run_cmd(self, cmd, cwd=None, timeout=3600):
        self._log(" ".join([str(x) for x in cmd]))
        res = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='ignore',
            startupinfo=self.task.get('startupinfo'),
            timeout=timeout,
        )
        if res.returncode != 0:
            lines = [line for line in (res.stdout or "").splitlines() if line.strip()]
            raise RuntimeError(" | ".join(lines[-6:]) if lines else "处理失败")
        return res.stdout or ""

    def _whisper_exe(self):
        tools_dir = self.task.get('tools_dir', '')
        for rel in ['nv/whisper.exe', 'amd/whisper.exe', 'cpu/whisper.exe', 'whisper_cpu.exe', 'whisper.exe']:
            path = os.path.join(tools_dir, rel)
            if os.path.exists(path):
                return path
        return None

    def _probe_video_size(self, ffprobe, input_path):
        """探测视频分辨率，默认竖屏 1080x1920。"""
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=width,height', '-of', 'csv=p=0:s=x', input_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8', errors='ignore',
                startupinfo=self.task.get('startupinfo'), timeout=8,
            )
            text = (res.stdout or "").strip().splitlines()[0]
            w, h = [int(x) for x in text.split('x')[:2]]
            return max(2, w), max(2, h)
        except Exception:
            return 1080, 1920

    def _has_audio_stream(self, ffprobe, input_path):
        """检测视频是否包含音轨。"""
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-select_streams', 'a:0',
                 '-show_entries', 'stream=index', '-of', 'csv=p=0', input_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8', errors='ignore',
                startupinfo=self.task.get('startupinfo'), timeout=8,
            )
            return bool((res.stdout or "").strip())
        except Exception:
            return False

    def _media_duration(self, ffprobe, path):
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', path],
                capture_output=True, text=True, encoding='utf-8', errors='ignore',
                startupinfo=self.task.get('startupinfo'), timeout=20,
            )
            return max(0.0, float((res.stdout or "").strip()))
        except Exception:
            return 0.0

    def _extract_audio(self, ffmpeg, input_path, wav_path):
        self._run_cmd([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                       '-i', input_path, '-vn', '-ar', '16000', '-ac', '1',
                       '-c:a', 'pcm_s16le', wav_path])

    def _transcribe(self, ffmpeg, ffprobe, tools_dir, models_dir, audio_path, cache_dir):
        """ASR：优先 SenseVoice 隔离识别，失败回退 Whisper 词级时间轴。返回 (srt_path, 引擎名)。"""
        # 1. SenseVoice
        sensevoice_dir = os.path.join(models_dir, 'SenseVoiceSmall')
        if os.path.isdir(sensevoice_dir) and os.path.exists(os.path.join(sensevoice_dir, 'model.pt')):
            try:
                self._log("正在使用 SenseVoice 识别语音...")
                from services.asr_service import SenseVoiceIsolatedSession
                self._sensevoice_session = SenseVoiceIsolatedSession(sensevoice_dir, timeout_sec=300)
                text, _device = self._sensevoice_session.transcribe(audio_path, timeout_sec=300)
                text = (text or "").strip()
                if text:
                    duration = self._media_duration(ffprobe, audio_path)
                    srt_path = os.path.join(cache_dir, "subtitle_sensevoice.srt")
                    # 用静音检测做分句对齐，避免整段文本按总时长均分导致字幕漂移
                    self._log("正在根据语音停顿对齐字幕时间轴...")
                    silence_points = self._silence_points(ffmpeg, audio_path)
                    aligned = self._write_srt_aligned(text, duration, silence_points, srt_path)
                    if aligned and os.path.exists(srt_path) and os.path.getsize(srt_path) > 0:
                        return srt_path, "SenseVoice"
                self._log("SenseVoice 未识别到文字，尝试 Whisper 回退。")
            except Exception as exc:
                self._log(f"SenseVoice 识别失败，尝试 Whisper 回退: {exc}")

        # 2. Whisper 词级时间轴
        whisper_exe = self._whisper_exe()
        models = [f for f in os.listdir(tools_dir) if f.endswith('.bin')]
        if whisper_exe and models:
            try:
                self._log("正在使用 Whisper 生成词级时间轴（稍慢）...")
                from services.asr_service import transcribe_with_whisper_word_timestamps
                word_srt = os.path.join(cache_dir, "subtitle_whisper_word.srt")
                actual_srt, _segments = transcribe_with_whisper_word_timestamps(
                    whisper_exe, models[0], ffmpeg, audio_path, word_srt, timeout_sec=900,
                )
                if actual_srt and os.path.exists(actual_srt) and os.path.getsize(actual_srt) > 0:
                    return actual_srt, "Whisper"
            except Exception as exc:
                self._log(f"Whisper 词级时间轴失败: {exc}")
        elif not whisper_exe:
            self._log("未找到 whisper.exe，仅使用 SenseVoice。")

        return None, ""

    def _srt_to_ass(self, srt_path, sub_style):
        """将 SRT 转成带样式的 ASS，返回 ass_path。"""
        import re as _re
        # 视频实际分辨率（横/竖屏自适应），默认竖屏基准
        play_w = int(sub_style.get('play_res_w', 1080) or 1080)
        play_h = int(sub_style.get('play_res_h', 1920) or 1920)
        base_w, base_h = 1080, 1920
        # 底部边距/字号按实际分辨率等比缩放（横屏时避免偏离画面中心）
        ratio = (play_h / base_h) if play_h else 1.0
        if play_w < play_h:
            ratio = (play_w / base_w) if play_w else 1.0
        try:
            with open(srt_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        except Exception:
            return None

        def parse_time(t):
            t = t.strip().replace(',', '.')
            parts = t.split(':')
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + float(s)
            if len(parts) == 2:
                m, s = parts
                return int(m) * 60 + float(s)
            return 0.0

        def fmt_ts(sec):
            h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
            return f"{h:01d}:{m:02d}:{s:05.2f}"

        font_size = int(sub_style.get('size', 72))
        color = sub_style.get('color', '#FFFFFF')
        border_w = int(sub_style.get('border_w', 4))
        border_color = sub_style.get('border_color', '#000000')
        margin_v = int(sub_style.get('margin_v', 120))
        font_name = sub_style.get('font_name', '微软雅黑')
        shadow = int(sub_style.get('shadow', 0) or 0)
        bold = int(sub_style.get('bold', 1))

        def hex_to_ass(h):
            h = str(h).strip('#')
            if len(h) != 6:
                return "&H00FFFFFF&"
            return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}&"

        ass_c = hex_to_ass(color)
        ass_bc = hex_to_ass(border_color)
        # 按实际分辨率换算字号/边距（保持视觉比例一致）
        scaled_font = max(10, int(font_size * ratio))
        scaled_margin = max(10, int(margin_v * ratio))
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{scaled_font},{ass_c},&H000000FF,{ass_bc},&H00000000,{bold},0,0,0,100,100,0,0,1,{border_w},{shadow},2,60,60,{scaled_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if '-->' in line:
                try:
                    start_s, end_s = line.split('-->')
                    start_t = parse_time(start_s)
                    end_t = parse_time(end_s)
                except Exception:
                    i += 1
                    continue
                i += 1
                texts = []
                while i < len(lines) and lines[i].strip():
                    texts.append(lines[i].strip())
                    i += 1
                text = "\\N".join(texts)
                if text:
                    events.append(
                        f"Dialogue: 0,{fmt_ts(start_t)},{fmt_ts(end_t)},Default,,0,0,0,,{text}"
                    )
            i += 1
        if not events:
            return None
        ass_path = os.path.join(os.path.dirname(srt_path), "subtitle_burn.ass")
        with open(ass_path, 'w', encoding='utf-8') as f:
            f.write(header + "\n".join(events) + "\n")
        return ass_path

    def run(self):
        try:
            job = self.task
            ffmpeg = job['ffmpeg']
            ffprobe = job['ffprobe']
            tools_dir = job.get('tools_dir', '')
            models_dir = job.get('models_dir', '')
            cache_dir = job.get('cache_dir', '')
            input_path = job['input']
            output_path = job['output']
            sub_style = job.get('sub_style', {}) or {}
            export_only = bool(job.get('export_only', False))
            os.makedirs(cache_dir, exist_ok=True)

            self.progress_signal.emit(5)
            self._log(f"开始给视频添加字幕: {os.path.basename(input_path)}")

            # 0. 无音轨检测（兼容性）
            if not self._has_audio_stream(ffprobe, input_path):
                raise RuntimeError("该视频没有音轨，无法识别语音生成字幕。")

            # 分辨率探测（横/竖屏自适应）
            play_w, play_h = self._probe_video_size(ffprobe, input_path)
            sub_style = dict(sub_style)
            sub_style['play_res_w'] = play_w
            sub_style['play_res_h'] = play_h
            self._log(f"视频分辨率 {play_w}x{play_h}，字幕按此比例适配。")

            # 1. 识别缓存：按输入文件特征缓存 SRT，避免重复识别
            cache_key = self._build_cache_key(input_path)
            cached_srt = self._load_cached_srt(cache_dir, cache_key)
            wav_path = ""
            if cached_srt:
                srt_path = cached_srt
                engine_name = "缓存"
                self._log("命中字幕识别缓存，跳过语音识别。")
            else:
                wav_path = os.path.join(cache_dir, f"subtitle_{cache_key}.wav")
                self._extract_audio(ffmpeg, input_path, wav_path)
                self.progress_signal.emit(25)
                self._log("音频提取完成，开始语音识别...")
                # 2. ASR
                srt_path, engine_name = self._transcribe(
                    ffmpeg, ffprobe, tools_dir, models_dir, wav_path, cache_dir,
                )
                if not srt_path:
                    raise RuntimeError("语音识别未生成有效字幕（SenseVoice/Whisper 均不可用）")
                self._save_cached_srt(cache_dir, cache_key, srt_path)
            self.progress_signal.emit(60)
            self._log(f"语音识别完成（{engine_name}），正在生成字幕样式...")

            # 3. SRT → ASS
            ass_path = self._srt_to_ass(srt_path, sub_style)
            if not ass_path:
                raise RuntimeError("字幕解析失败，请检查音频是否有人声")
            self.progress_signal.emit(75)

            # 仅导出字幕文件模式（不烧录）
            if export_only:
                export_srt = os.path.join(cache_dir, f"{os.path.splitext(os.path.basename(input_path))[0]}_字幕.srt")
                import shutil
                shutil.copy2(srt_path, export_srt)
                export_ass = os.path.join(cache_dir, f"{os.path.splitext(os.path.basename(input_path))[0]}_字幕.ass")
                shutil.copy2(ass_path, export_ass)
                self.progress_signal.emit(100)
                self._log(f"字幕文件已导出:\n  {export_srt}\n  {export_ass}")
                self.finished_signal.emit(True, export_srt)
                return

            # 4. 烧录（保留原视频画面与音轨）
            self._log("字幕样式已生成，正在烧录到视频...")
            from utils import path_to_ffmpeg
            safe_ass = path_to_ffmpeg(os.path.abspath(ass_path))
            fonts_dir = path_to_ffmpeg("C:/Windows/Fonts") if os.name == "nt" else ""
            vf = f"subtitles=filename='{safe_ass}'"
            if fonts_dir:
                vf += f":fontsdir='{fonts_dir}'"
            burn_cmd = [
                ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                '-i', input_path,
                '-vf', vf,
                '-map', '0:v:0', '-map', '0:a?',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
                '-c:a', 'copy',
                '-movflags', '+faststart',
                output_path,
            ]
            self._run_cmd(burn_cmd)
            self.progress_signal.emit(100)
            self._log(f"字幕烧录完成: {os.path.basename(output_path)}")
            self.finished_signal.emit(True, output_path)
        except Exception as exc:
            self.finished_signal.emit(False, str(exc))
        finally:
            try:
                if self._sensevoice_session:
                    self._sensevoice_session.close()
            except Exception:
                pass

    def _build_cache_key(self, input_path):
        """基于文件大小+修改时间生成缓存标识。"""
        try:
            st = os.stat(input_path)
            return f"{st.st_size}_{int(st.st_mtime)}"
        except Exception:
            return str(abs(hash(os.path.abspath(input_path))) % 1000000)

    def _load_cached_srt(self, cache_dir, cache_key):
        p = os.path.join(cache_dir, f"subtitle_{cache_key}.srt")
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
        return None

    def _save_cached_srt(self, cache_dir, cache_key, srt_path):
        try:
            dst = os.path.join(cache_dir, f"subtitle_{cache_key}.srt")
            import shutil
            shutil.copy2(srt_path, dst)
        except Exception:
            pass

    def _silence_points(self, ffmpeg, audio_path):
        """用 ffmpeg silencedetect 检测停顿点，用于字幕分句对齐。"""
        try:
            duration = self._media_duration(self.task.get('ffprobe', ''), audio_path)
            timeout = max(8, min(45, int(duration / 12) + 8))
            res = subprocess.run(
                [ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'info',
                 '-i', audio_path, '-vn', '-af', 'silencedetect=noise=-32dB:d=0.18',
                 '-f', 'null', '-'],
                capture_output=True, text=True, encoding='utf-8', errors='ignore',
                startupinfo=self.task.get('startupinfo'), timeout=timeout,
            )
            text = (res.stderr or "") + "\n" + (res.stdout or "")
            import re as _re
            return [max(0.0, float(m.group(1)))
                    for m in _re.finditer(r"silence_start:\s*([0-9.]+)", text)]
        except Exception:
            return []

    def _write_srt_aligned(self, text, duration, silence_points, srt_path):
        """将整段文本按静音点分配时间轴，近似对齐语音节奏。

        策略：
          - 文本按语句切分成 N 段；
          - 将 N 段映射到时长轴上，段边界尽量贴近最近静音点；
          - 生成 SRT，时间点与停顿对齐，避免整段均分漂移。
        """
        import re as _re
        from services.asr_service import split_text_for_subtitle, clean_asr_text

        text = clean_asr_text(text)
        parts = split_text_for_subtitle(text)
        if not parts:
            return False
        if duration <= 0:
            duration = 10.0

        # 候选切分点：0 和 duration 之间均匀插入 N-1 个理想切点，吸附到最近静音点
        cuts = []
        if len(parts) > 1:
            for i in range(1, len(parts)):
                ideal = duration * i / len(parts)
                if silence_points:
                    nearest = min(silence_points, key=lambda p: abs(p - ideal))
                    # 只允许吸附在理想点前后 15% 范围内，避免过度偏移
                    if abs(nearest - ideal) <= duration * 0.15:
                        cuts.append(nearest)
                    else:
                        cuts.append(ideal)
                else:
                    cuts.append(ideal)
            cuts = sorted(set(round(c, 2) for c in cuts))
            # 去重过近的切点
            filtered = []
            for c in cuts:
                if filtered and c - filtered[-1] < 0.25:
                    continue
                filtered.append(c)
            cuts = filtered

        # 组装事件：start/end
        events = []
        prev = 0.0
        for idx, part in enumerate(parts):
            end = cuts[idx] if idx < len(cuts) else duration
            end = max(prev + 0.2, min(duration, end))
            if end - prev < 0.2:
                end = min(duration, prev + 0.2)
            events.append((prev, end, part))
            prev = end

        def fmt_ts(sec):
            h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
            return f"{h:02d}:{m:02d}:{s:06.3f}".replace('.', ',')

        with open(srt_path, 'w', encoding='utf-8') as f:
            for idx, (start, end, content) in enumerate(events, 1):
                f.write(f"{idx}\n{fmt_ts(start)} --> {fmt_ts(end)}\n{content}\n\n")
        return True

