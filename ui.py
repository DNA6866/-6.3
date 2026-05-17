import sys
import os
from paths import (
    APP_ROOT as PATH_APP_ROOT,
    core_path,
    ensure_core_path,
    logs_dir,
    models_dir,
    runtime_path,
    tools_dir,
    workspace_path,
)
ensure_core_path()
import shutil
import json 
import time
import platform
import traceback
import base64
import random
from PyQt5.QtWidgets import (QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, 
                             QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
                             QGroupBox, QFormLayout, QSpinBox, QDoubleSpinBox, 
                             QComboBox, QCheckBox, QPushButton, QLabel,
                             QFileDialog, QInputDialog, QMessageBox, QPlainTextEdit, 
                             QColorDialog, QLineEdit, QTextEdit, QApplication,
                             QStackedWidget, QGridLayout, QButtonGroup, QFrame,
                             QProgressBar, QScrollArea, QStyle, QSizePolicy,
                             QDialog, QDialogButtonBox)
from PyQt5.QtCore import Qt, pyqtSignal, QUrl, QEvent
from PyQt5.QtGui import QPixmap, QFont, QIcon, QColor, QPainter, QPen, QBrush
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
import subprocess 
from engine import SynthesisEngine
from services.media_service import build_waveform_peaks, extract_reference_frame, import_files_to_folder
from modules.voice_clone.voice_clone_widget import VoiceCloneWidget
from modules.image_generate.image_generate_widget import ImageGenerateWidget
from modules.live_downloader.live_downloader_widget import LiveDownloaderWidget
from modules.short_video.task_worker import ShortVideoDownloadWorker, ShortVideoParseWorker, ShortVideoTranscriptWorker
from modules.embedded_contact_assets import PAY_IMAGE_B64, CONTACT_IMAGE_B64
from utils import get_system_fonts, detect_performance_profile, ANIMATIONS, TRANS_MAP, EXTENDED_COLORS, path_to_ffmpeg
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, 
                             # ... 中间省略 ...
                             QStackedWidget, QGridLayout, QButtonGroup)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap, QFont, QIcon, QColor
# 👇 新增：光晕特效与动画引擎库
from PyQt5.QtWidgets import QGraphicsDropShadowEffect
from PyQt5.QtCore import QTimer, QThread, QPropertyAnimation, QEasingCurve

def app_root():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_root()

class DragPreviewCanvas(QWidget):
    pointChanged = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self.design_w = 1080
        self.design_h = 1920
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

    def set_design_size(self, width, height):
        self.design_w = max(1, int(width))
        self.design_h = max(1, int(height))

    def _emit_point(self, event):
        if self.width() <= 0 or self.height() <= 0:
            return
        x = round(event.x() / self.width() * self.design_w)
        y = round(event.y() / self.height() * self.design_h)
        x = max(0, min(self.design_w, x))
        y = max(0, min(self.design_h, y))
        self.pointChanged.emit(x, y)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._emit_point(event)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._emit_point(event)
        super().mouseMoveEvent(event)

class FileDropArea(QFrame):
    filesDropped = pyqtSignal(list)
    clicked = pyqtSignal()

    def __init__(self, title="拖拽文件到这里导入"):
        super().__init__()
        self.setObjectName("dropArea")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        label = QLabel(title)
        label.setAlignment(Qt.AlignCenter)
        label.setObjectName("dropAreaText")
        layout.addWidget(label)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        files = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                files.append(path)
        if files:
            self.filesDropped.emit(files)
        event.acceptProposedAction()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

class AudioRangeSelector(QWidget):
    rangeChanged = pyqtSignal(float, float)

    def __init__(self):
        super().__init__()
        self.duration = 10.0
        self.start = 0.0
        self.end = 3.0
        self.active_handle = None
        self.peaks = []
        self.setMinimumHeight(150)
        self.setMouseTracking(True)

    def set_duration(self, duration):
        self.duration = max(0.1, float(duration))
        self.start = 0.0
        self.end = min(3.0, self.duration)
        self.update()
        self.rangeChanged.emit(self.start, self.end)

    def selected_range(self):
        return self.start, self.end

    def set_waveform(self, peaks):
        self.peaks = peaks or []
        self.update()

    def _track_rect(self):
        margin = 18
        top = 18
        height = max(60, self.height() - 48)
        return margin, top, max(1, self.width() - margin * 2), height

    def _x_from_time(self, value):
        x0, _, width, _ = self._track_rect()
        return x0 + int((value / self.duration) * width)

    def _time_from_x(self, x):
        x0, _, width, _ = self._track_rect()
        ratio = max(0.0, min(1.0, (x - x0) / width))
        return ratio * self.duration

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        x0, top, width, height = self._track_rect()
        mid = top + height // 2

        painter.setPen(QPen(QColor("#334155"), 1))
        painter.setBrush(QBrush(QColor("#020617")))
        painter.drawRoundedRect(x0, top, width, height, 8, 8)

        if self.peaks:
            bar_count = min(width, len(self.peaks))
            step = len(self.peaks) / max(1, bar_count)
            painter.setPen(QPen(QColor("#60A5FA"), 1))
            for i in range(bar_count):
                peak = self.peaks[int(i * step)]
                bar_h = max(2, int(peak * (height - 16)))
                x = x0 + i
                painter.drawLine(x, mid - bar_h // 2, x, mid + bar_h // 2)
        else:
            painter.setPen(QPen(QColor("#475569"), 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(x0 + 8, mid, x0 + width - 8, mid)

        sx = self._x_from_time(self.start)
        ex = self._x_from_time(self.end)
        painter.setPen(QPen(QColor("#F8FAFC"), 2))
        painter.setBrush(QBrush(QColor(37, 99, 235, 70)))
        painter.drawRect(sx, top + 4, max(4, ex - sx), height - 8)

        painter.setPen(QPen(QColor("#FFFFFF"), 2))
        painter.setBrush(QBrush(QColor("#2563EB")))
        painter.drawRoundedRect(sx - 5, top, 10, height, 4, 4)
        painter.drawRoundedRect(ex - 5, top, 10, height, 4, 4)

        painter.setPen(QColor("#E2E8F0"))
        painter.drawText(x0, top + height + 24, self._fmt(self.start))
        painter.drawText(ex - 44, top + height + 24, self._fmt(self.end))
        painter.drawText(x0 + width - 54, top - 4, self._fmt(self.duration))

    def mousePressEvent(self, event):
        sx = self._x_from_time(self.start)
        ex = self._x_from_time(self.end)
        self.active_handle = 'start' if abs(event.x() - sx) <= abs(event.x() - ex) else 'end'
        self._move_handle(event.x())

    def mouseMoveEvent(self, event):
        if self.active_handle:
            self._move_handle(event.x())

    def mouseReleaseEvent(self, event):
        self.active_handle = None

    def _move_handle(self, x):
        value = self._time_from_x(x)
        if self.active_handle == 'start':
            self.start = max(0.0, min(value, self.end - 0.1))
        elif self.active_handle == 'end':
            self.end = min(self.duration, max(value, self.start + 0.1))
        self.update()
        self.rangeChanged.emit(self.start, self.end)

    def _fmt(self, sec):
        m = int(sec // 60)
        s = sec - m * 60
        return f"{m:02d}:{s:05.2f}"

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
            watermarks = [wm for wm in watermarks if str(wm.get('text', '')).strip()]
            if not watermarks:
                raise RuntimeError("水印列表为空，请先添加至少一条文字水印。")
            ffprobe = job.get('ffprobe') or os.path.join(os.path.dirname(ffmpeg), 'ffprobe.exe')
            width, height = self._probe_video_size(ffprobe, input_path)
            temp_files = []
            filters = []
            sys_fonts = job.get('sys_fonts') or {}
            try:
                for wm_index, watermark in enumerate(watermarks):
                    text = str(watermark.get('text', '')).strip()
                    design_w = max(1, int(watermark.get('design_w') or width))
                    design_h = max(1, int(watermark.get('design_h') or height))
                    scale = width / design_w
                    font_size = max(8, int(float(watermark.get('size', 60)) * scale))
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
                            f.write(line)
                        temp_files.append(txt_path)
                        line_y = base_y + line_index * font_size * 1.3
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
            tools_dir = job['tools_dir']
            temp_wav = os.path.join(job['output_dir'], f"asr_{int(time.time())}_{os.getpid()}.wav")
            self._run_cmd([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', input_path, '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', temp_wav])
            try:
                from services.asr_service import sensevoice_model_ready, sensevoice_runtime_info, transcribe_to_txt_with_sensevoice
                sensevoice_dir = os.path.join(models_dir(), 'SenseVoiceSmall')
                if not sensevoice_model_ready(sensevoice_dir):
                    raise RuntimeError("SenseVoiceSmall 模型不可用")
                device, loaded = sensevoice_runtime_info()
                self.log_signal.emit(f"使用 SenseVoiceSmall 转文本，设备: {device}，模型状态: {'已加载' if loaded else '首次加载'}")
                transcribe_to_txt_with_sensevoice(temp_wav, sensevoice_dir, output_path)
            except Exception as sense_error:
                self.log_signal.emit(f"SenseVoice 转写失败，切换 Whisper: {sense_error}")
                whisper = self._whisper_exe()
                models = [os.path.join(tools_dir, f) for f in os.listdir(tools_dir) if f.endswith('.bin')]
                if not whisper or not models:
                    raise RuntimeError("未找到 SenseVoiceSmall，也未找到 whisper.exe 或 .bin 模型")
                self._run_cmd([whisper, '-m', models[0], '-f', temp_wav, '-otxt', '-l', 'zh', '--prompt', '简体中文'], cwd=job['output_dir'])
                txt_path = temp_wav + '.txt'
                if os.path.exists(txt_path):
                    shutil.move(txt_path, output_path)
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
        try:
            from services.asr_service import preload_sensevoice_model
            model_dir = os.path.join(models_dir(), 'SenseVoiceSmall')
            device, was_loaded = preload_sensevoice_model(model_dir)
            state = "已加载" if was_loaded else "加载完成"
            self.finished_signal.emit(True, f"SenseVoiceSmall {state}，当前设备：{device}")
        except Exception as e:
            self.finished_signal.emit(False, f"SenseVoiceSmall 预热失败：{e}")

class VideoMixerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        os.chdir(APP_DIR)
        self.tab_names = ['音频素材', '封面图片', '开头素材(段1)', '承接素材(段2)', '视频素材', '背景音乐', '智能字幕', '文字水印', '保存视频']
        self.tab_folders = {
            '音频素材': workspace_path('素材', '音频素材'),
            '封面图片': workspace_path('素材', '封面图片'),
            '开头素材(段1)': workspace_path('素材', '开头素材(段1)'),
            '承接素材(段2)': workspace_path('素材', '承接素材(段2)'),
            '视频素材': workspace_path('素材', '视频素材'),
            '背景音乐': workspace_path('素材', '背景音乐'),
            '保存视频': workspace_path('输出', '保存视频'),
        }
        self.cache_dir = workspace_path('缓存')
        self.av_output_dir = workspace_path('输出', '音视频处理输出')
        self.bin_dir = workspace_path('配置') 
        self.config_file = os.path.join(self.bin_dir, 'config.json')
        self.preset_file = os.path.join(self.bin_dir, 'wm_presets.json')
        
        self.tab_exts = {
            '音频素材': ['.mp3', '.wav', '.m4a'], '封面图片': ['.jpg', '.jpeg', '.png'], 
            '开头素材(段1)': ['.mp4', '.mov'], '承接素材(段2)': ['.mp4', '.mov'], 
            '视频素材': ['.mp4', '.mov', '.avi'], '背景音乐': ['.mp3', '.wav'], 
            '保存视频': ['.mp4']
        }
        self.select_all_cbx = {}
        self.duration_cache = {}
        self.preview_bg_path = None 
        self.preview_bg_auto_source = None
        self.preview_bg_manual = False
        self.sub_preview_bg_path = None
        self.sub_preview_bg_auto_source = None
        self.sub_preview_bg_manual = False
        
        raw_fonts = get_system_fonts()
        self.sys_fonts = self.filter_chinese_fonts(raw_fonts)
        
        self.watermarks_data = [] 
        self.editing_wm_index = -1 
        self.performance_profile = None
        self.trim_player = None
        self.trim_looping = False
        self.feedback_log_history = []
        self.last_feedback_report_path = ""
        self._original_excepthook = sys.excepthook
        sys.excepthook = self.handle_uncaught_exception

        self.initUI()
        self.init_directories()
        self.load_configuration()
        if self.auto_perf_chk.isChecked():
            self.apply_best_performance_profile(silent=True)
        self.load_preset_list()
        self.auto_refresh_all()
        self.auto_load_watermark_reference_frame(silent=True)
        self.auto_load_subtitle_reference_frame(silent=True)

    def filter_chinese_fonts(self, fonts_dict):
        keywords = ['雅黑', '黑', '宋', '楷', '仿', '隶', '圆', '华文', '等线', '方正', '站酷', '霞鹜', '思源', '特战',
                    'yahei', 'simhei', 'simsun', 'kaiti', 'fangsong', 'dengxian', 'pingfang', 'msjh']
        filtered = {}
        for name, path in fonts_dict.items():
            name_lower = name.lower()
            if "arial" in name_lower or "calibri" in name_lower or "segoe" in name_lower: continue
            if any(kw in name_lower for kw in keywords): filtered[name] = path
        return filtered if filtered else fonts_dict

    def init_directories(self):
        for folder in self.tab_folders.values(): os.makedirs(folder, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True); os.makedirs(self.av_output_dir, exist_ok=True); os.makedirs(self.bin_dir, exist_ok=True)
        self.clear_cache(silent=True)

    def choose_directory(self, tab_name):
        current_dir = self.tab_folders.get(tab_name, os.getcwd())
        dir_path = QFileDialog.getExistingDirectory(self, f"选择【{tab_name}】所在的文件夹", current_dir)
        if dir_path: self.tab_folders[tab_name] = dir_path; self.refresh_directory(tab_name)

    def open_directory_explorer(self, tab_name):
        try:
            folder_path = self.tab_folders.get(tab_name, "")
            if os.path.exists(folder_path): os.startfile(os.path.abspath(folder_path))
        except Exception: pass

    def handle_files_dropped(self, tab_name, files):
        folder_path = self.tab_folders.get(tab_name, "")
        if not folder_path:
            return
        os.makedirs(folder_path, exist_ok=True)
        allowed_exts = self.tab_exts.get(tab_name, [])
        imported = 0
        skipped = 0
        imported, skipped = import_files_to_folder(files, folder_path, allowed_exts)
        self.refresh_directory(tab_name, silent=True)
        if imported or skipped:
            self.update_log(f"[素材导入] {tab_name}: 导入 {imported} 个，跳过 {skipped} 个。")

    def _filter_from_exts(self, allowed_exts):
        if not allowed_exts:
            return "All Files (*.*)"
        patterns = " ".join([f"*{ext}" for ext in allowed_exts])
        return f"Supported Files ({patterns});;All Files (*.*)"

    def choose_import_files_to_tab(self, tab_name):
        allowed_exts = self.tab_exts.get(tab_name, [])
        files, _ = QFileDialog.getOpenFileNames(self, f"导入【{tab_name}】文件", os.getcwd(), self._filter_from_exts(allowed_exts))
        if files:
            self.handle_files_dropped(tab_name, files)

    def choose_import_folder_to_tab(self, tab_name):
        folder = QFileDialog.getExistingDirectory(self, f"导入【{tab_name}】文件夹", os.getcwd())
        if not folder:
            return
        allowed_exts = self.tab_exts.get(tab_name, [])
        files = []
        for root, _, names in os.walk(folder):
            for name in names:
                path = os.path.join(root, name)
                if not allowed_exts or any(path.lower().endswith(ext) for ext in allowed_exts):
                    files.append(path)
        self.handle_files_dropped(tab_name, files)

    def refresh_directory(self, tab_name, silent=False):
        folder_path = self.tab_folders.get(tab_name, "")
        if not os.path.exists(folder_path): os.makedirs(folder_path, exist_ok=True)
        table = self.tables[tab_name]
        table.setRowCount(0) 
        allowed_exts = self.tab_exts.get(tab_name, [])
        try:
            files = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f)) and any(f.lower().endswith(ext) for ext in allowed_exts)]
            current_select_all = self.select_all_cbx[tab_name].isChecked()
            for row, filename in enumerate(files):
                table.insertRow(row)
                chk_w = QWidget(); ch_layout = QHBoxLayout(); chk = QCheckBox()
                chk.setChecked(current_select_all); ch_layout.addWidget(chk); ch_layout.setAlignment(Qt.AlignCenter); ch_layout.setContentsMargins(0, 0, 0, 0); chk_w.setLayout(ch_layout)
                table.setCellWidget(row, 0, chk_w)
                table.setItem(row, 1, QTableWidgetItem(filename))
                table.setItem(row, 2, QTableWidgetItem(os.path.abspath(os.path.join(folder_path, filename))))
                if tab_name == '音频素材':
                    status_item = QTableWidgetItem("待处理"); status_item.setTextAlignment(Qt.AlignCenter)
                    table.setItem(row, 3, status_item)
            if tab_name == '视频素材' and hasattr(self, 'preview_canvas') and not self.preview_bg_manual:
                QTimer.singleShot(0, lambda: self.auto_load_watermark_reference_frame(silent=True))
            if tab_name == '视频素材' and hasattr(self, 'sub_preview_canvas') and not self.sub_preview_bg_manual:
                QTimer.singleShot(0, lambda: self.auto_load_subtitle_reference_frame(silent=True))
        except Exception: pass

    def update_table_status(self, row_index, text, color_name):
        table = self.tables.get('音频素材')
        if not table: return
        if row_index < table.rowCount():
            item = QTableWidgetItem(text)
            if color_name == "blue": item.setForeground(Qt.blue)
            elif color_name == "green": item.setForeground(Qt.darkGreen)
            elif color_name == "red": item.setForeground(Qt.red)
            elif color_name: item.setForeground(QColor(color_name))
            item.setTextAlignment(Qt.AlignCenter)
            font = item.font(); font.setBold(True); item.setFont(font)
            table.setItem(row_index, 3, item)

    def save_configuration(self, silent=False):
        config = {
            'watermarks': self.watermarks_data,
            'rules': {
                'min_clip': self.min_clip_spin.value(), 'max_clip': self.max_clip_spin.value(),
                'trans_type': self.trans_combo.currentText(), 'trans_dur': self.trans_duration.value(),
                'main_vol': self.main_vol_spin.value(), 'bgm_vol': self.bgm_vol_spin.value(),
                'resolution': self.res_combo.currentText(), 'gpu': self.gpu_combo.currentText(),
                'loop_count': self.loop_spin.value(), 'threads': self.thread_spin.value(),
                'anti_dedup': self.anti_dedup_chk.isChecked(),
                'auto_perf': self.auto_perf_chk.isChecked(),
                'output_date_folder': self.output_date_folder_chk.isChecked() if hasattr(self, 'output_date_folder_chk') else True,
                'mix_preset': self.mix_preset_combo.currentText() if hasattr(self, 'mix_preset_combo') else ''
            },
            'subtitle': {
                'enable': self.sub_enable_chk.isChecked(), 'font': self.sub_font.currentText(),
                'size': self.sub_size.value(), 'color': self.sub_color.currentText(),
                'border_w': self.sub_border_w.value(), 'border_color': self.sub_border_color.currentText(),
                'anim': self.sub_anim.currentText(), 'margin_v': self.sub_margin_v.value(),
                'max_len': self.sub_max_len.value()
            },
            'cover': {
                'text': self.cover_text_input.text(), 'font': self.cover_font.currentText(),
                'style': self.cover_style.currentText()
            }
        }
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4, ensure_ascii=False)
            if not silent: self.update_log("💾 [系统配置] 全局配置已自动保存。")
        except Exception: pass

    def load_configuration(self):
        if not os.path.exists(self.config_file): return
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f: config = json.load(f)
            rules = config.get('rules', {})
            if 'min_clip' in rules: self.min_clip_spin.setValue(rules['min_clip'])
            if 'max_clip' in rules: self.max_clip_spin.setValue(rules['max_clip'])
            if 'trans_type' in rules: self.trans_combo.setCurrentText(rules['trans_type'])
            if 'trans_dur' in rules: self.trans_duration.setValue(rules['trans_dur'])
            if 'main_vol' in rules: self.main_vol_spin.setValue(rules['main_vol'])
            if 'bgm_vol' in rules: self.bgm_vol_spin.setValue(rules['bgm_vol'])
            if 'resolution' in rules: self.res_combo.setCurrentText(rules['resolution'])
            if 'gpu' in rules: self.gpu_combo.setCurrentText(rules['gpu'])
            if 'loop_count' in rules: self.loop_spin.setValue(rules['loop_count'])
            if 'threads' in rules: self.thread_spin.setValue(rules['threads'])
            if 'anti_dedup' in rules: self.anti_dedup_chk.setChecked(rules['anti_dedup'])
            if 'auto_perf' in rules: self.auto_perf_chk.setChecked(rules['auto_perf'])
            if 'output_date_folder' in rules and hasattr(self, 'output_date_folder_chk'): self.output_date_folder_chk.setChecked(rules['output_date_folder'])
            if 'mix_preset' in rules and hasattr(self, 'mix_preset_combo'): self.mix_preset_combo.setCurrentText(rules['mix_preset'])

            sub = config.get('subtitle', {})
            if 'enable' in sub: self.sub_enable_chk.setChecked(sub['enable'])
            if 'font' in sub: self.sub_font.setCurrentText(sub['font'])
            if 'size' in sub: self.sub_size.setValue(sub['size'])
            if 'color' in sub: self.sub_color.setCurrentText(sub['color'])
            if 'border_w' in sub: self.sub_border_w.setValue(sub['border_w'])
            if 'border_color' in sub: self.sub_border_color.setCurrentText(sub['border_color'])
            if 'anim' in sub: self.sub_anim.setCurrentText(sub['anim'])
            if 'margin_v' in sub: self.sub_margin_v.setValue(sub['margin_v'])
            if 'max_len' in sub: self.sub_max_len.setValue(sub['max_len'])

            cov = config.get('cover', {})
            if 'text' in cov: self.cover_text_input.setText(cov['text'])
            if 'font' in cov: self.cover_font.setCurrentText(cov['font'])
            if 'style' in cov: self.cover_style.setCurrentText(cov['style'])

            self.watermarks_data = config.get('watermarks', [])
            for wm in self.watermarks_data:
                if wm['font_name'] in self.sys_fonts: wm['font_path'] = self.sys_fonts[wm['font_name']]
                self.add_wm_to_table(wm)
            self.draw_preview_canvas(); self.draw_subtitle_preview()
        except Exception: pass

    def load_preset_list(self):
        if os.path.exists(self.preset_file):
            try:
                with open(self.preset_file, 'r', encoding='utf-8') as f: self.wm_presets = json.load(f)
            except Exception: self.wm_presets = {}
        else: self.wm_presets = {}
        self.preset_combo.clear(); self.preset_combo.addItems(list(self.wm_presets.keys()))

    def save_current_as_preset(self):
        name = self.preset_name_input.text().strip()
        if not name: return
        self.wm_presets[name] = self.watermarks_data.copy()
        try:
            with open(self.preset_file, 'w', encoding='utf-8') as f: json.dump(self.wm_presets, f, indent=4, ensure_ascii=False)
            self.load_preset_list(); self.preset_combo.setCurrentText(name); QMessageBox.information(self, "成功", f"预设【{name}】已保存！")
        except Exception: pass

    def apply_selected_preset(self):
        name = self.preset_combo.currentText()
        if not name or name not in self.wm_presets: return
        self.watermarks_data = self.wm_presets[name].copy()
        self.wm_table.setRowCount(0)
        for wm in self.watermarks_data: self.add_wm_to_table(wm)
        self.reset_watermark_form(); self.save_configuration(silent=True)

    def delete_selected_preset(self):
        name = self.preset_combo.currentText()
        if not name or name not in self.wm_presets: return
        del self.wm_presets[name]
        try:
            with open(self.preset_file, 'w', encoding='utf-8') as f: json.dump(self.wm_presets, f, indent=4, ensure_ascii=False)
            self.load_preset_list()
        except Exception: pass

    def load_preview_bg(self, target='watermark'):
        try:
            file_path, _ = QFileDialog.getOpenFileName(self, "选择参考底图", "", "Media Files (*.mp4 *.mov *.jpg *.png)")
            if not file_path: return
            bg_path = os.path.join(os.path.abspath(self.cache_dir), f"preview_bg_{target}.jpg")
            if file_path.lower().endswith(('.mp4', '.mov', '.avi')):
                ffmpeg_exe = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
                subprocess.run([ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error', '-i', file_path, '-vframes', '1', '-q:v', '2', bg_path], startupinfo=self.get_si(), check=True)
            else: shutil.copy(file_path, bg_path)
            
            if bg_path and os.path.exists(bg_path):
                if target == 'watermark':
                    self.preview_bg_path = bg_path; self.preview_bg_auto_source = file_path; self.preview_bg_manual = True; self.draw_preview_canvas()
                else:
                    self.sub_preview_bg_path = bg_path; self.sub_preview_bg_auto_source = file_path; self.sub_preview_bg_manual = True; self.draw_subtitle_preview()
        except Exception: pass

    def _get_video_reference_source(self):
        table = self.tables.get('视频素材') if hasattr(self, 'tables') else None
        if not table:
            return None

        fallback = None
        for row in range(table.rowCount()):
            item = table.item(row, 2)
            if not item:
                continue
            path = item.text()
            if not fallback:
                fallback = path
            chk_w = table.cellWidget(row, 0)
            if chk_w and chk_w.layout().itemAt(0).widget().isChecked():
                return path
        return fallback

    def _extract_reference_frame(self, source, target):
        os.makedirs(self.cache_dir, exist_ok=True)
        bg_path = os.path.join(os.path.abspath(self.cache_dir), f"preview_bg_{target}.jpg")
        ffmpeg_exe = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        return extract_reference_frame(ffmpeg_exe, source, bg_path)

    def auto_load_watermark_reference_frame(self, silent=False, force=False):
        if self.preview_bg_manual and not force:
            return False
        source = self._get_video_reference_source()
        if not source or not os.path.exists(source):
            return False
        if not force and self.preview_bg_path and self.preview_bg_auto_source == source and os.path.exists(self.preview_bg_path):
            return True

        try:
            bg_path = self._extract_reference_frame(source, 'watermark')

            if bg_path and os.path.exists(bg_path):
                self.preview_bg_path = bg_path
                self.preview_bg_auto_source = source
                self.preview_bg_manual = False
                self.draw_preview_canvas()
                if not silent:
                    self.update_log(f"[水印预览] 已从视频素材取帧: {os.path.basename(source)}")
                return True
        except Exception as e:
            if not silent:
                self.update_log(f"[水印预览] 取帧失败: {e}")
        return False

    def auto_load_subtitle_reference_frame(self, silent=False, force=False):
        if self.sub_preview_bg_manual and not force:
            return False
        source = self._get_video_reference_source()
        if not source or not os.path.exists(source):
            return False
        if not force and self.sub_preview_bg_path and self.sub_preview_bg_auto_source == source and os.path.exists(self.sub_preview_bg_path):
            return True

        try:
            bg_path = self._extract_reference_frame(source, 'subtitle')
            if os.path.exists(bg_path):
                self.sub_preview_bg_path = bg_path
                self.sub_preview_bg_auto_source = source
                self.sub_preview_bg_manual = False
                self.draw_subtitle_preview()
                if not silent:
                    self.update_log(f"[字幕预览] 已从视频素材取帧: {os.path.basename(source)}")
                return True
        except Exception as e:
            if not silent:
                self.update_log(f"[字幕预览] 取帧失败: {e}")
        return False

    def set_watermark_position_from_preview(self, x, y):
        if not hasattr(self, 'wm_x') or not hasattr(self, 'wm_y'):
            return
        self.wm_x.blockSignals(True)
        self.wm_y.blockSignals(True)
        self.wm_x.setValue(x)
        self.wm_y.setValue(y)
        self.wm_x.blockSignals(False)
        self.wm_y.blockSignals(False)
        self.draw_preview_canvas()

    def switch_tab(self, index):
        self.stacked_widget.setCurrentIndex(index)
        name = self.tab_names[index] if 0 <= index < len(self.tab_names) else ""
        if name == '文字水印':
            self.auto_load_watermark_reference_frame(silent=True)
        if name == '智能字幕':
            self.auto_load_subtitle_reference_frame(silent=True)

    def switch_module(self, index):
        self.module_stack.setCurrentIndex(index)

    def get_si(self):
        si = subprocess.STARTUPINFO(); si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return si

    def apply_modern_theme(self):
        qss_path = core_path('styles', 'dark.qss')
        try:
            with open(qss_path, 'r', encoding='utf-8') as f:
                self.setStyleSheet(f.read())
        except Exception:
            self.setStyleSheet("")

    def _set_hardware_status(self, text, level='info'):
        if not hasattr(self, 'hardware_status_label'):
            return
        colors = {'nvenc': '#047857', 'amf': '#047857', 'cpu': '#B54708', 'info': '#667085'}
        self.hardware_status_label.setText(text)
        self.hardware_status_label.setStyleSheet(f"color: {colors.get(level, '#667085')}; font-weight: 700;")

    def apply_best_performance_profile(self, silent=False):
        profile = detect_performance_profile(tools_dir())
        self.performance_profile = profile
        self.gpu_combo.setCurrentText(profile['gpu_mode'])
        self.thread_spin.setValue(profile['threads'])
        self._set_hardware_status(f"{profile['gpu_mode']}\n{profile['summary']} | 推荐并发 {profile['threads']}", profile['level'])
        if not silent:
            detail = "；".join(profile.get('details', [])) or "已完成硬件与编码器检测"
            self.update_log(f"[性能检测] {profile['summary']}，已自动选择 {profile['gpu_mode']} / {profile['threads']} 线程。{detail}")

    def populate_color_combo(self, combo):
        combo.clear(); combo.addItem("🎨 自定义取色盘...")
        for c in EXTENDED_COLORS:
            pixmap = QPixmap(16, 16); pixmap.fill(QColor(c)); combo.addItem(QIcon(pixmap), c)

    def handle_color_selection(self, index, combo, update_func):
        text = combo.itemText(index)
        if text == "🎨 自定义取色盘...":
            color = QColorDialog.getColor()
            if color.isValid():
                hex_color = color.name().upper(); pixmap = QPixmap(16, 16); pixmap.fill(color)
                combo.insertItem(1, QIcon(pixmap), hex_color); combo.setCurrentIndex(1)
            else: combo.setCurrentIndex(2) 
        update_func()

    def get_hex_from_combo(self, combo):
        text = combo.currentText()
        if text == "🎲 随机颜色": return text
        if text == "🎨 自定义取色盘...": return "#FFFFFF"
        return QColor(text).name().upper()

    def build_subtitle_tab(self):
        tab = QWidget(); layout = QHBoxLayout(); left_v = QVBoxLayout(); form = QFormLayout()
        info_label = QLabel("🎤 本地智能大模型字幕 (100%纯正简体 & 双排防溢出)")
        info_label.setStyleSheet("color: #009688; font-weight: bold; font-size: 14px;")
        form.addRow(info_label)

        self.sub_enable_chk = QCheckBox("🔥 启用本地 AI 模型极速听写")
        self.sub_enable_chk.setStyleSheet("color: #FF5722; font-weight: bold; font-size: 15px;"); self.sub_enable_chk.setChecked(True)
        form.addRow(self.sub_enable_chk)

        self.sub_font = QComboBox(); self.sub_font.addItem("🎲 随机系统字体"); self.sub_font.addItems(list(self.sys_fonts.keys()))
        self.sub_size = QSpinBox(); self.sub_size.setRange(30, 200); self.sub_size.setValue(85)
        font_layout = QHBoxLayout(); font_layout.addWidget(self.sub_font, stretch=2); font_layout.addWidget(QLabel("字号:")); font_layout.addWidget(self.sub_size)
        form.addRow("字体设置:", font_layout)

        self.sub_color = QComboBox(); self.populate_color_combo(self.sub_color); self.sub_color.addItem("🎲 随机颜色"); self.sub_color.setCurrentText("🎲 随机颜色")
        self.sub_color.activated.connect(lambda idx: self.handle_color_selection(idx, self.sub_color, self.draw_subtitle_preview))
        self.sub_border_color = QComboBox(); self.populate_color_combo(self.sub_border_color); self.sub_border_color.addItem("🎲 随机颜色"); self.sub_border_color.setCurrentText("black")
        self.sub_border_color.activated.connect(lambda idx: self.handle_color_selection(idx, self.sub_border_color, self.draw_subtitle_preview))
        self.sub_border_w = QSpinBox(); self.sub_border_w.setRange(0, 15); self.sub_border_w.setValue(5)
        color_layout = QHBoxLayout(); color_layout.addWidget(QLabel("主色:")); color_layout.addWidget(self.sub_color)
        color_layout.addWidget(QLabel("描边:")); color_layout.addWidget(self.sub_border_color); color_layout.addWidget(QLabel("粗细:")); color_layout.addWidget(self.sub_border_w)
        form.addRow("色彩调色:", color_layout)

        self.sub_margin_v = QSpinBox(); self.sub_margin_v.setRange(10, 1500); self.sub_margin_v.setValue(180) 
        self.sub_max_len = QSpinBox(); self.sub_max_len.setRange(5, 30); self.sub_max_len.setValue(14) 
        pos_layout = QHBoxLayout(); pos_layout.addWidget(QLabel("底边距:")); pos_layout.addWidget(self.sub_margin_v)
        pos_layout.addWidget(QLabel("字数折行:")); pos_layout.addWidget(self.sub_max_len)
        form.addRow("排版控制:", pos_layout)

        self.sub_anim = QComboBox()
        # 🚀 扩充：加入大量高级影视级动效
        self.sub_anim.addItems([
            "无动效", "柔和淡入淡出", "字号弹性弹出", "持续缓慢放大", "字间距呼吸展宽", 
            "Y轴3D翻转入场", "X轴3D翻转入场", "微倾斜摇摆", "高斯模糊聚焦入场", 
            "颜色渐变(白转青)", "描边粗细脉冲", "字符压扁复原", "幽灵漂浮显影", "暴击震动", "🎲 随机动效"
        ])
        self.sub_anim.setCurrentText("🎲 随机动效")
        form.addRow("影视动效:", self.sub_anim)
        
        self.sub_test_input = QLineEdit("老板你好，这是一条用来测试智能双排防溢出功能的测试字幕哦！")
        form.addRow("沙盘测试文本:", self.sub_test_input)

        left_v.addLayout(form); left_v.addStretch(); layout.addLayout(left_v, stretch=2)

        right_v = QVBoxLayout(); bg_btn_layout = QHBoxLayout(); bg_btn_layout.addWidget(QLabel("📺 字幕沙盘:")); load_bg_btn = QPushButton("🖼️ 加载参考底图")
        load_bg_btn.clicked.connect(lambda: self.load_preview_bg('subtitle'))
        video_frame_btn = QPushButton("🎞️ 视频素材取帧"); video_frame_btn.clicked.connect(lambda: self.auto_load_subtitle_reference_frame(silent=False, force=True))
        bg_btn_layout.addWidget(load_bg_btn); bg_btn_layout.addWidget(video_frame_btn); right_v.addLayout(bg_btn_layout)
        self.sub_preview_canvas = QWidget(); self.sub_preview_canvas.setObjectName("referencePreview"); self.sub_preview_canvas.setStyleSheet("background-color: #111; border: 2px solid #38BDF8; border-radius: 8px;"); self.sub_preview_canvas.setFixedSize(360, 640); right_v.addWidget(self.sub_preview_canvas, alignment=Qt.AlignCenter)
        layout.addLayout(right_v, stretch=1); tab.setLayout(layout)
        
        self.sub_size.valueChanged.connect(self.draw_subtitle_preview); self.sub_margin_v.valueChanged.connect(self.draw_subtitle_preview)
        self.sub_max_len.valueChanged.connect(self.draw_subtitle_preview); self.sub_test_input.textChanged.connect(self.draw_subtitle_preview)
        self.sub_border_w.valueChanged.connect(self.draw_subtitle_preview)
        return tab

    def draw_subtitle_preview(self):
        for child in self.sub_preview_canvas.children(): child.deleteLater()
        if "竖屏" in self.res_combo.currentText():
            self.sub_preview_canvas.setFixedSize(360, 640)
            design_w, design_h = 1080, 1920
        else:
            self.sub_preview_canvas.setFixedSize(640, 360)
            design_w, design_h = 1920, 1080
        scale_ratio = self.sub_preview_canvas.width() / design_w
        if self.sub_preview_bg_path and os.path.exists(self.sub_preview_bg_path):
            bg_lbl = QLabel(self.sub_preview_canvas); bg_lbl.setGeometry(0, 0, self.sub_preview_canvas.width(), self.sub_preview_canvas.height())
            bg_lbl.setAlignment(Qt.AlignCenter)
            bg_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            bg_lbl.setPixmap(QPixmap(self.sub_preview_bg_path).scaled(self.sub_preview_canvas.width(), self.sub_preview_canvas.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation)); bg_lbl.show()
            
        text = self.sub_test_input.text().strip()
        if not text: return
        
        max_l = self.sub_max_len.value()
        if len(text) > max_l:
            half = (len(text) + 1) // 2
            lines = [text[:half], text[half:]]
        else: lines = [text]

        color = "#00FFFF" if self.sub_color.currentText() == "🎲 随机颜色" else self.sub_color.currentText()
        border_color = "black" if self.sub_border_color.currentText() == "🎲 随机颜色" else self.sub_border_color.currentText()
        bw = max(1, int(self.sub_border_w.value() * scale_ratio))
        font_size = int(self.sub_size.value() * scale_ratio)
        canvas_h = self.sub_preview_canvas.height(); canvas_w = self.sub_preview_canvas.width()
        margin_v_scaled = int(self.sub_margin_v.value() * scale_ratio)
        
        for i, line in enumerate(lines):
            temp_lbl = QLabel(line); font = temp_lbl.font(); font.setPointSize(font_size); font.setBold(True); temp_lbl.setFont(font); temp_lbl.adjustSize()
            line_y = canvas_h - margin_v_scaled - (len(lines) - i) * font_size * 1.3
            final_x = (canvas_w - temp_lbl.width()) / 2
            temp_lbl.deleteLater()
            if self.sub_border_w.value() > 0:
                for dx, dy in [(-bw,-bw), (bw,-bw), (-bw,bw), (bw,bw), (0,-bw), (0,bw), (-bw,0), (bw,0)]:
                    bg_lbl = QLabel(line, self.sub_preview_canvas); bg_lbl.setStyleSheet(f"color: {border_color}; background: transparent;"); bg_lbl.setFont(font); bg_lbl.adjustSize(); bg_lbl.move(int(final_x) + dx, int(line_y) + dy); bg_lbl.show()
                    bg_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            lbl = QLabel(line, self.sub_preview_canvas)
            lbl.setStyleSheet(f"color: {color}; background: transparent;"); lbl.setFont(font); lbl.adjustSize(); lbl.move(int(final_x), int(line_y)); lbl.show()
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

    def open_text_popup(self):
        text, ok = QInputDialog.getMultiLineText(self, "多行水印输入框", "请输入水印内容：", self.wm_text.toPlainText())
        if ok: self.wm_text.setPlainText(text); self.draw_preview_canvas()

    def set_watermark_mode(self, mode, row=-1):
        if mode == "edit":
            self.mode_label.setText(f"🟠 编辑模式：正在修改第 {row + 1} 条水印，点表格空白或清空面板可回到新增")
            self.mode_label.setStyleSheet("color: #FDBA74; background:#2A1F0B; border:1px solid #B45309; border-radius:8px; font-weight:bold; font-size:14px; padding:7px;")
            if hasattr(self, 'btn_modify_sel'):
                self.btn_modify_sel.setEnabled(True)
            if hasattr(self, 'btn_add_new'):
                self.btn_add_new.setText("➕ 另存为新水印")
            return
        self.mode_label.setText("🟢 新增模式：当前内容会作为一条新水印添加")
        self.mode_label.setStyleSheet("color: #BBF7D0; background:#052E16; border:1px solid #22C55E; border-radius:8px; font-weight:bold; font-size:14px; padding:7px;")
        if hasattr(self, 'btn_modify_sel'):
            self.btn_modify_sel.setEnabled(False)
        if hasattr(self, 'btn_add_new'):
            self.btn_add_new.setText("➕ 添加为新水印")

    def selected_watermark_row(self):
        if not hasattr(self, 'wm_table') or not self.wm_table.selectionModel():
            return -1
        selected = self.wm_table.selectionModel().selectedRows()
        if not selected:
            return -1
        row = selected[0].row()
        return row if 0 <= row < len(self.watermarks_data) else -1

    def reset_watermark_form(self):
        if hasattr(self, 'wm_table'):
            self.wm_table.blockSignals(True)
            self.wm_table.clearSelection()
            self.wm_table.blockSignals(False)
        self.editing_wm_index = -1
        self.set_watermark_mode("new")
        self.wm_text.blockSignals(True); self.wm_text.setPlainText(""); self.wm_text.blockSignals(False)
        self.draw_preview_canvas()

    def _get_active_text(self): return self.wm_text.toPlainText()
    def _get_current_wm_data(self):
        return {
            'text': self._get_active_text(), 'font_name': self.wm_font.currentText(), 'font_path': self.sys_fonts.get(self.wm_font.currentText(), ""), 
            'size': self.wm_size.value(), 'color': self.wm_color.currentText(), 'align': self.wm_align.currentText(),
            'border_w': self.wm_border_w.value(), 'border_color': self.wm_border_color.currentText(), 'is_bold': self.wm_is_bold.isChecked(),
            'x': self.wm_x.value(), 'y': self.wm_y.value(), 'anim': self.wm_anim.currentText()
        }
        
    def add_watermark_new(self):
        if not self._get_active_text().strip(): return
        data = self._get_current_wm_data(); self.watermarks_data.append(data); self.add_wm_to_table(data)
        self.reset_watermark_form(); self.save_configuration(silent=True)
        
    def modify_selected_watermark(self):
        curr_row = self.selected_watermark_row()
        if curr_row < 0: return
        data = self._get_current_wm_data(); self.watermarks_data[curr_row] = data; self.update_wm_table_row(curr_row, data)
        self.reset_watermark_form(); self.save_configuration(silent=True)

    def on_wm_table_selection_changed(self):
        curr_row = self.selected_watermark_row()
        if curr_row < 0:
            self.editing_wm_index = -1
            self.set_watermark_mode("new")
            self.draw_preview_canvas()
            return
        self.editing_wm_index = curr_row
        self.set_watermark_mode("edit", curr_row)
        wm = self.watermarks_data[curr_row]
        
        self.wm_text.blockSignals(True); self.wm_font.blockSignals(True); self.wm_size.blockSignals(True)
        self.wm_color.blockSignals(True); self.wm_align.blockSignals(True); self.wm_x.blockSignals(True); self.wm_y.blockSignals(True)
        
        self.wm_text.setPlainText(wm.get('text', ''))
        self.wm_font.setCurrentText(wm.get('font_name', '🎲 随机系统字体'))
        self.wm_size.setValue(wm.get('size', 60))
        self.wm_color.setCurrentText(wm.get('color', 'white'))
        self.wm_align.setCurrentText(wm.get('align', '居中对齐'))
        self.wm_is_bold.setChecked(wm.get('is_bold', False))
        self.wm_border_w.setValue(wm.get('border_w', 2))
        self.wm_border_color.setCurrentText(wm.get('border_color', 'black'))
        self.wm_x.setValue(wm.get('x', 540))
        self.wm_y.setValue(wm.get('y', 960))
        self.wm_anim.setCurrentText(wm.get('anim', '静态展示'))
        
        self.wm_text.blockSignals(False); self.wm_font.blockSignals(False); self.wm_size.blockSignals(False)
        self.wm_color.blockSignals(False); self.wm_align.blockSignals(False); self.wm_x.blockSignals(False); self.wm_y.blockSignals(False)
        self.draw_preview_canvas()

    def add_wm_to_table(self, data):
        row = self.wm_table.rowCount(); self.wm_table.insertRow(row); self.update_wm_table_row(row, data)
        
    def update_wm_table_row(self, row, data):
        self.wm_table.setItem(row, 0, QTableWidgetItem(data['text'].replace('\n', '  '))); self.wm_table.setItem(row, 1, QTableWidgetItem(data['font_name']))
        self.wm_table.setItem(row, 2, QTableWidgetItem(f"大小:{data['size']} 颜色:{data['color']}")); self.wm_table.setItem(row, 3, QTableWidgetItem(f"{data['x']}, {data['y']}"))
        self.wm_table.setItem(row, 4, QTableWidgetItem(data['anim'].split(' ')[0]))
        
    def delete_selected_watermark(self):
        curr_row = self.selected_watermark_row()
        if curr_row < 0: return
        del self.watermarks_data[curr_row]; self.wm_table.removeRow(curr_row); self.reset_watermark_form(); self.save_configuration(silent=True)

    def draw_preview_canvas(self):
        for child in self.preview_canvas.children(): child.deleteLater()
        if "竖屏" in self.res_combo.currentText():
            self.preview_canvas.setFixedSize(360, 640)
            self.preview_canvas.set_design_size(1080, 1920)
            design_w = 1080
        else:
            self.preview_canvas.setFixedSize(640, 360)
            self.preview_canvas.set_design_size(1920, 1080)
            design_w = 1920
        scale_ratio = self.preview_canvas.width() / design_w
        if self.preview_bg_path and os.path.exists(self.preview_bg_path):
            bg_lbl = QLabel(self.preview_canvas); bg_lbl.setGeometry(0, 0, self.preview_canvas.width(), self.preview_canvas.height())
            bg_lbl.setAlignment(Qt.AlignCenter)
            bg_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            bg_lbl.setPixmap(QPixmap(self.preview_bg_path).scaled(self.preview_canvas.width(), self.preview_canvas.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation)); bg_lbl.show()
        for i, wm in enumerate(self.watermarks_data):
            if i == self.editing_wm_index: continue 
            self.draw_text_lines(wm, scale_ratio, is_cursor=False)
        if self._get_active_text():
            self.draw_text_lines(self._get_current_wm_data(), scale_ratio, is_cursor=True)

    def draw_text_lines(self, wm_data, scale_ratio, is_cursor):
        lines = str(wm_data['text']).split('\n')
        for i, line in enumerate(lines):
            if not line.strip(): continue
            color = "#FF1493" if wm_data['color'] == "🎲 随机颜色" else wm_data['color']
            border_w = wm_data.get('border_w', 0); border_color = "#00FFFF" if wm_data.get('border_color', 'black') == "🎲 随机颜色" else wm_data.get('border_color', 'black')
            temp_lbl = QLabel(line); font = temp_lbl.font(); font.setPointSize(int(wm_data['size'] * scale_ratio)); font.setBold(wm_data.get('is_bold', False)); temp_lbl.setFont(font); temp_lbl.adjustSize()
            base_x = wm_data['x'] * scale_ratio; line_y = (wm_data['y'] + i * wm_data['size'] * 1.3) * scale_ratio
            if wm_data['align'] == "居中对齐": final_x = base_x - temp_lbl.width() / 2
            elif wm_data['align'] == "右对齐": final_x = base_x - temp_lbl.width()
            else: final_x = base_x
            temp_lbl.deleteLater()
            if border_w > 0:
                bw = max(1, int(border_w * scale_ratio))
                for dx, dy in [(-bw,-bw), (bw,-bw), (-bw,bw), (bw,bw), (0,-bw), (0,bw), (-bw,0), (bw,0)]:
                    bg_lbl = QLabel(line, self.preview_canvas); bg_lbl.setStyleSheet(f"color: {border_color}; background: transparent;"); bg_lbl.setFont(font); bg_lbl.adjustSize(); bg_lbl.move(int(final_x) + dx, int(line_y) + dy); bg_lbl.show()
                    bg_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            lbl = QLabel(line, self.preview_canvas)
            lbl.setStyleSheet(f"color: {color}; background: transparent;"); lbl.setFont(font); lbl.adjustSize(); lbl.move(int(final_x), int(line_y)); lbl.show()
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents)

    def eventFilter(self, obj, event):
        if hasattr(self, 'wm_table') and obj == self.wm_table.viewport() and event.type() == QEvent.MouseButtonPress:
            if self.wm_table.itemAt(event.pos()) is None:
                self.reset_watermark_form()
        return super().eventFilter(obj, event)

    def build_watermark_tab(self):
        tab = QWidget(); layout = QHBoxLayout(); left_v = QVBoxLayout(); form = QFormLayout()
        self.mode_label = QLabel("🟢 新增模式：当前内容会作为一条新水印添加")
        self.mode_label.setWordWrap(True)
        self.mode_label.setStyleSheet("color: #BBF7D0; background:#052E16; border:1px solid #22C55E; border-radius:8px; font-weight:bold; font-size:14px; padding:7px;")
        input_layout = QVBoxLayout(); self.wm_text = QPlainTextEdit(""); self.wm_text.setMaximumHeight(70)
        text_btn_layout = QHBoxLayout(); btn_popup = QPushButton("🪟 弹窗输入"); btn_popup.clicked.connect(self.open_text_popup)
        btn_reset = QPushButton("🔄 清空面板/退出编辑"); btn_reset.clicked.connect(self.reset_watermark_form)
        text_btn_layout.addWidget(btn_popup); text_btn_layout.addWidget(btn_reset)
        input_layout.addWidget(self.wm_text); input_layout.addLayout(text_btn_layout)
        self.wm_font = QComboBox(); self.wm_font.addItem("🎲 随机系统字体"); self.wm_font.addItems(list(self.sys_fonts.keys()))
        self.wm_size = QSpinBox(); self.wm_size.setRange(20, 300); self.wm_size.setValue(60)
        self.wm_color = QComboBox(); self.populate_color_combo(self.wm_color); self.wm_color.addItem("🎲 随机颜色"); self.wm_color.setCurrentText("white")
        self.wm_color.activated.connect(lambda idx: self.handle_color_selection(idx, self.wm_color, self.draw_preview_canvas))
        self.wm_align = QComboBox(); self.wm_align.addItems(["左对齐", "居中对齐", "右对齐"]); self.wm_align.setCurrentText("居中对齐")
        format_layout = QHBoxLayout(); self.wm_is_bold = QCheckBox("强制加粗")
        self.wm_border_w = QSpinBox(); self.wm_border_w.setRange(0, 10); self.wm_border_w.setValue(2)
        self.wm_border_color = QComboBox(); self.populate_color_combo(self.wm_border_color); self.wm_border_color.addItem("🎲 随机颜色"); self.wm_border_color.setCurrentText("black")
        self.wm_border_color.activated.connect(lambda idx: self.handle_color_selection(idx, self.wm_border_color, self.draw_preview_canvas))
        format_layout.addWidget(self.wm_is_bold); format_layout.addWidget(QLabel("描边粗细:")); format_layout.addWidget(self.wm_border_w)
        format_layout.addWidget(QLabel("描边颜色:")); format_layout.addWidget(self.wm_border_color)
        self.wm_x = QSpinBox(); self.wm_x.setRange(0, 3840); self.wm_x.setValue(540); self.wm_y = QSpinBox(); self.wm_y.setRange(0, 3840); self.wm_y.setValue(960)
        self.wm_anim = QComboBox(); self.wm_anim.addItems(list(ANIMATIONS.keys()))
        
        form.addRow(self.mode_label); form.addRow("水印文字:", input_layout)
        font_layout = QHBoxLayout(); font_layout.addWidget(self.wm_font, stretch=2); font_layout.addWidget(self.wm_size, stretch=1)
        form.addRow("字体与大小:", font_layout); form.addRow("格式强化:", format_layout)
        color_layout = QHBoxLayout(); color_layout.addWidget(QLabel("字体颜色:")); color_layout.addWidget(self.wm_color); color_layout.addWidget(QLabel("对齐方式:")); color_layout.addWidget(self.wm_align)
        form.addRow("颜色与对齐:", color_layout)
        coord_layout = QHBoxLayout(); coord_layout.addWidget(QLabel("X 坐标:")); coord_layout.addWidget(self.wm_x); coord_layout.addWidget(QLabel("Y 坐标:")); coord_layout.addWidget(self.wm_y)
        form.addRow("位置坐标:", coord_layout); form.addRow("百变动画:", self.wm_anim); left_v.addLayout(form)
        
        btn_layout = QHBoxLayout()
        self.btn_add_new = QPushButton("➕ 添加为新水印"); self.btn_add_new.clicked.connect(self.add_watermark_new)
        self.btn_modify_sel = QPushButton("📝 覆盖选中水印"); self.btn_modify_sel.clicked.connect(self.modify_selected_watermark); self.btn_modify_sel.setEnabled(False) 
        del_one_btn = QPushButton("🗑️ 删除"); del_one_btn.clicked.connect(self.delete_selected_watermark)
        btn_layout.addWidget(self.btn_add_new); btn_layout.addWidget(self.btn_modify_sel); btn_layout.addWidget(del_one_btn); left_v.addLayout(btn_layout)
        
        self.wm_table = QTableWidget(0, 5); self.wm_table.setHorizontalHeaderLabels(['文本', '字体', '格式', '坐标', '特效'])
        self.wm_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.wm_table.setSelectionBehavior(QTableWidget.SelectRows) 
        self.wm_table.itemSelectionChanged.connect(self.on_wm_table_selection_changed)
        self.wm_table.viewport().installEventFilter(self)
        left_v.addWidget(self.wm_table)
        
        preset_group = QGroupBox("商业矩阵水印预设库")
        p_layout = QVBoxLayout(); r1 = QHBoxLayout(); self.preset_name_input = QLineEdit("我的矩阵预设1"); btn_save_p = QPushButton("💾 保存当前列表为新预设"); btn_save_p.clicked.connect(self.save_current_as_preset)
        r1.addWidget(QLabel("保存名称:")); r1.addWidget(self.preset_name_input); r1.addWidget(btn_save_p)
        r2 = QHBoxLayout(); self.preset_combo = QComboBox(); btn_load_p = QPushButton("📂 加载预设"); btn_load_p.clicked.connect(self.apply_selected_preset); btn_del_p = QPushButton("🗑️ 删除预设"); btn_del_p.clicked.connect(self.delete_selected_preset)
        r2.addWidget(QLabel("选择预设:")); r2.addWidget(self.preset_combo, stretch=1); r2.addWidget(btn_load_p); r2.addWidget(btn_del_p)
        p_layout.addLayout(r1); p_layout.addLayout(r2); preset_group.setLayout(p_layout); left_v.addWidget(preset_group)
        layout.addLayout(left_v, stretch=2)
        
        right_v = QVBoxLayout(); bg_btn_layout = QHBoxLayout(); bg_btn_layout.addWidget(QLabel("📺 实时沙盘:")); load_bg_btn = QPushButton("🖼️ 加载参考底图"); load_bg_btn.clicked.connect(lambda: self.load_preview_bg('watermark'))
        video_frame_btn = QPushButton("🎞️ 视频素材取帧"); video_frame_btn.clicked.connect(lambda: self.auto_load_watermark_reference_frame(silent=False, force=True))
        bg_btn_layout.addWidget(load_bg_btn); bg_btn_layout.addWidget(video_frame_btn); right_v.addLayout(bg_btn_layout)
        self.preview_canvas = DragPreviewCanvas(); self.preview_canvas.setObjectName("referencePreview"); self.preview_canvas.setStyleSheet("background-color: #111; border: 2px solid #22C55E; border-radius: 8px;"); self.preview_canvas.setFixedSize(360, 640); self.preview_canvas.pointChanged.connect(self.set_watermark_position_from_preview); right_v.addWidget(self.preview_canvas, alignment=Qt.AlignCenter)
        layout.addLayout(right_v, stretch=1); tab.setLayout(layout)
        
        self.wm_x.valueChanged.connect(self.draw_preview_canvas); self.wm_y.valueChanged.connect(self.draw_preview_canvas)
        self.wm_size.valueChanged.connect(self.draw_preview_canvas); self.wm_text.textChanged.connect(self.draw_preview_canvas)
        return tab

    def build_card_page(self, title, content_widget):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("appTitle")
        outer.addWidget(heading)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 16)
        card_layout.addWidget(content_widget)
        outer.addWidget(card, stretch=1)
        return page

    def build_file_tab(self, name):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(12)

        if name != '保存视频':
            drop_area = FileDropArea(f"拖拽文件到这里，或点击选择【{name}】文件")
            drop_area.filesDropped.connect(lambda files, t=name: self.handle_files_dropped(t, files))
            drop_area.clicked.connect(lambda t=name: self.choose_import_files_to_tab(t))
            tab_layout.addWidget(drop_area)

        btn_layout = QHBoxLayout()
        select_all_chk = QCheckBox("全选")
        select_all_chk.stateChanged.connect(lambda state, t=name: self.toggle_select_all(t, state))
        self.select_all_cbx[name] = select_all_chk

        open_dir_explorer_btn = QPushButton("打开所在目录")
        open_dir_explorer_btn.clicked.connect(lambda checked, t=name: self.open_directory_explorer(t))
        refresh_btn = QPushButton("刷新列表")
        refresh_btn.clicked.connect(lambda checked, t=name: self.refresh_directory(t))

        btn_layout.addWidget(select_all_chk)
        btn_layout.addWidget(open_dir_explorer_btn)
        btn_layout.addWidget(refresh_btn)

        if name == '保存视频':
            save_btn = QPushButton("保存全局配置")
            save_btn.clicked.connect(lambda: self.save_configuration(False))
            open_explorer_btn = QPushButton("打开输出文件夹")
            open_explorer_btn.clicked.connect(self.open_output_folder)
            btn_layout.addWidget(save_btn)
            btn_layout.addWidget(open_explorer_btn)

        btn_layout.addStretch()
        tab_layout.addLayout(btn_layout)

        if name in ['音频素材']:
            table = QTableWidget(0, 4)
            table.setHorizontalHeaderLabels(['选择', '素材名称', '文件路径', '当前状态'])
        else:
            table = QTableWidget(0, 3)
            table.setHorizontalHeaderLabels(['选择', '素材名称', '文件路径'])

        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tables[name] = table
        tab_layout.addWidget(table)
        return tab

    def build_file_page(self, name):
        tab = self.build_file_tab(name)
        return self.build_card_page(name, tab)

    def build_task_settings_widget(self):
        container = QWidget()
        form_layout = QFormLayout(container)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(12)
        form_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        workflow_group = QGroupBox("0. 工作流预设与开工检查")
        workflow_layout = QFormLayout()
        preset_layout = QHBoxLayout()
        self.mix_preset_combo = QComboBox()
        self.mix_preset_combo.addItems([
            "抖音竖屏带字幕",
            "快手轻量差异化",
            "低配电脑稳定",
            "高性能批量生产",
            "直播切片样片",
        ])
        preset_btn = QPushButton("应用预设")
        preset_btn.clicked.connect(self.apply_mix_workflow_preset)
        preset_layout.addWidget(self.mix_preset_combo, stretch=1)
        preset_layout.addWidget(preset_btn)
        workflow_layout.addRow("生产模板:", preset_layout)

        check_layout = QHBoxLayout()
        self.preflight_btn = QPushButton("开始前检查")
        self.preflight_btn.clicked.connect(lambda: self.run_mixer_preflight(show_dialog=True))
        self.preview_sample_btn = QPushButton("先生成一条预览样片")
        self.preview_sample_btn.clicked.connect(self.start_preview_synthesis)
        check_layout.addWidget(self.preflight_btn)
        check_layout.addWidget(self.preview_sample_btn)
        workflow_layout.addRow("生产确认:", check_layout)

        self.output_date_folder_chk = QCheckBox("按日期自动归档输出")
        self.output_date_folder_chk.setChecked(True)
        self.mixer_summary_label = QLabel("任务概览：等待选择素材")
        self.mixer_summary_label.setObjectName("appSubtitle")
        self.mixer_summary_label.setWordWrap(True)
        workflow_layout.addRow("输出管理:", self.output_date_folder_chk)
        workflow_layout.addRow(self.mixer_summary_label)
        workflow_group.setLayout(workflow_layout)
        form_layout.addRow(workflow_group)

        rule_group = QGroupBox("1. 裁剪混合"); r_layout = QFormLayout()
        clip_layout = QHBoxLayout(); self.min_clip_spin = QDoubleSpinBox(); self.min_clip_spin.setRange(0.5, 10.0); self.min_clip_spin.setValue(2.0); self.max_clip_spin = QDoubleSpinBox(); self.max_clip_spin.setRange(0.5, 20.0); self.max_clip_spin.setValue(3.0)
        clip_layout.addWidget(QLabel("从")); clip_layout.addWidget(self.min_clip_spin); clip_layout.addWidget(QLabel("到")); clip_layout.addWidget(self.max_clip_spin); clip_layout.addWidget(QLabel("秒"))
        r_layout.addRow("随机裁剪:", clip_layout)
        audio_layout = QHBoxLayout(); self.main_vol_spin = QDoubleSpinBox(); self.main_vol_spin.setValue(2.0); self.bgm_vol_spin = QDoubleSpinBox(); self.bgm_vol_spin.setValue(0.2)
        audio_layout.addWidget(QLabel("主音:")); audio_layout.addWidget(self.main_vol_spin); audio_layout.addWidget(QLabel("BGM:")); audio_layout.addWidget(self.bgm_vol_spin)
        r_layout.addRow("混音倍率:", audio_layout); rule_group.setLayout(r_layout); form_layout.addRow(rule_group)

        vis_group = QGroupBox("2. 视觉特效防重"); v_layout = QFormLayout()
        trans_layout = QHBoxLayout(); self.trans_combo = QComboBox(); self.trans_combo.addItems(list(TRANS_MAP.keys())); self.trans_combo.setCurrentText("🎲 随机特效 (防限流神器)")
        self.trans_duration = QDoubleSpinBox(); self.trans_duration.setRange(0.1, 2.0); self.trans_duration.setValue(0.5)
        trans_layout.addWidget(self.trans_combo); trans_layout.addWidget(QLabel("时长:")); trans_layout.addWidget(self.trans_duration); v_layout.addRow("转场库:", trans_layout)
        
        self.anti_dedup_chk = QCheckBox("启用【微变速/调色】防重"); self.anti_dedup_chk.setChecked(True); v_layout.addRow("平台过审:", self.anti_dedup_chk)
        output_layout = QHBoxLayout(); self.res_combo = QComboBox(); self.res_combo.addItems(["1080x1920 (竖屏)", "1920x1080 (横屏)"]); self.res_combo.currentTextChanged.connect(self.draw_preview_canvas); self.res_combo.currentTextChanged.connect(self.draw_subtitle_preview)
        output_layout.addWidget(self.res_combo); v_layout.addRow("画面比例:", output_layout); vis_group.setLayout(v_layout); form_layout.addRow(vis_group)
        
        perf_group = QGroupBox("3. 渲染与硬件自适应"); p_layout = QFormLayout()
        loop_layout = QHBoxLayout(); self.loop_spin = QSpinBox(); self.loop_spin.setRange(1, 999); self.loop_spin.setValue(1); self.thread_spin = QSpinBox(); self.thread_spin.setRange(1, 16); self.thread_spin.setValue(2) 
        loop_layout.addWidget(QLabel("执行")); loop_layout.addWidget(self.loop_spin); loop_layout.addWidget(QLabel("轮,")); loop_layout.addWidget(self.thread_spin); loop_layout.addWidget(QLabel("线程"))
        p_layout.addRow("任务列队:", loop_layout)
        self.auto_perf_chk = QCheckBox("自动选择最快档位"); self.auto_perf_chk.setChecked(True)
        p_layout.addRow("自动适配:", self.auto_perf_chk)
        gpu_layout = QHBoxLayout(); self.gpu_combo = QComboBox(); self.gpu_combo.addItems(["Nvidia NVENC 加速", "AMD AMF 加速", "CPU 软解"]); self.detect_gpu_btn = QPushButton("重新检测"); self.detect_gpu_btn.clicked.connect(self.detect_system_gpu) 
        gpu_layout.addWidget(self.gpu_combo); gpu_layout.addWidget(self.detect_gpu_btn); p_layout.addRow("引擎:", gpu_layout)
        status_layout = QVBoxLayout()
        status_layout.setSpacing(4)
        status_title = QLabel("当前档位")
        status_title.setObjectName("hardwareStatusTitle")
        self.hardware_status_label = QLabel("等待硬件检测")
        self.hardware_status_label.setObjectName("hardwareStatus")
        self.hardware_status_label.setWordWrap(True)
        self.hardware_status_label.setMinimumHeight(44)
        status_layout.addWidget(status_title)
        status_layout.addWidget(self.hardware_status_label)
        p_layout.addRow(status_layout)
        perf_group.setLayout(p_layout); form_layout.addRow(perf_group)

        # 🚀 核心新增：专属封面包装区
        cover_group = QGroupBox("4. 封面静态大字包装 (避开字幕)")
        c_layout = QFormLayout()
        self.cover_text_input = QLineEdit()
        self.cover_text_input.setPlaceholderText("在此输入封面主标题 (留空则不加文字)")
        self.cover_font = QComboBox()
        self.cover_font.addItem("🎲 随机系统字体")
        self.cover_font.addItems(list(self.sys_fonts.keys()))
        self.cover_style = QComboBox()
        self.cover_style.addItems(["无特效 (仅原图)", "经典白字黑边", "高亮霓虹发光", "综艺黑字黄底", "复古牛皮纸色", "🎲 随机静态风格"])
        self.cover_style.setCurrentText("经典白字黑边")
        
        c_layout.addRow("封面大标题:", self.cover_text_input)
        font_style_layout = QHBoxLayout()
        font_style_layout.addWidget(self.cover_font, stretch=1)
        font_style_layout.addWidget(self.cover_style, stretch=1)
        c_layout.addRow("字体与特效:", font_style_layout)
        cover_group.setLayout(c_layout)
        form_layout.addRow(cover_group)

        action_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始执行任务"); self.start_btn.setObjectName("startButton"); self.start_btn.setMinimumHeight(52); self.start_btn.clicked.connect(self.start_synthesis)
        self.stop_btn = QPushButton("终止任务"); self.stop_btn.setObjectName("stopButton"); self.stop_btn.setMinimumHeight(52); self.stop_btn.clicked.connect(self.stop_and_clean)
        action_layout.addWidget(self.start_btn, stretch=2); action_layout.addWidget(self.stop_btn, stretch=1); form_layout.addRow(action_layout)

        self.task_progress = QProgressBar()
        self.task_progress.setRange(0, 100)
        self.task_progress.setValue(0)
        form_layout.addRow("任务进度:", self.task_progress)
        return container

    def build_task_settings_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.build_card_page("任务控制", self.build_task_settings_widget()))
        return scroll

    def build_batch_mixer_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        workspace = QFrame()
        workspace.setObjectName("card")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(16, 14, 16, 16)
        workspace_layout.setSpacing(12)

        title = QLabel("批量混剪工作台")
        title.setObjectName("appTitle")
        workspace_layout.addWidget(title)

        sub_nav = QGridLayout()
        sub_nav.setSpacing(8)
        self.stacked_widget = QStackedWidget()
        self.tab_btn_group = QButtonGroup()
        self.tables = {}

        row, col = 0, 0
        for i, name in enumerate(self.tab_names):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setProperty("navButton", True)
            btn.setMinimumHeight(38)
            if i == 0:
                btn.setChecked(True)
            self.tab_btn_group.addButton(btn, i)
            sub_nav.addWidget(btn, row, col)

            if name == '文字水印':
                tab = self.build_watermark_tab()
            elif name == '智能字幕':
                tab = self.build_subtitle_tab()
            else:
                tab = self.build_file_tab(name)
            self.stacked_widget.addWidget(tab)

            col += 1
            if col >= 5:
                col = 0
                row += 1

        self.tab_btn_group.idClicked.connect(self.switch_tab)
        workspace_layout.addLayout(sub_nav)
        workspace_layout.addWidget(self.stacked_widget, stretch=1)

        settings_card = QFrame()
        settings_card.setObjectName("card")
        settings_card.setMinimumWidth(430)
        settings_layout = QVBoxLayout(settings_card)
        settings_layout.setContentsMargins(16, 14, 16, 16)
        settings_layout.setSpacing(12)
        settings_title = QLabel("核心参数")
        settings_title.setObjectName("appTitle")
        settings_layout.addWidget(settings_title)
        settings_layout.addWidget(self.build_task_settings_widget())

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        settings_scroll.setMinimumWidth(420)
        settings_scroll.setMaximumWidth(640)
        settings_scroll.setWidget(settings_card)

        layout.addWidget(workspace, stretch=1)
        layout.addWidget(settings_scroll, stretch=0)
        return page

    def build_av_processing_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        nav_card = QFrame()
        nav_card.setObjectName("card")
        nav_card.setFixedWidth(230)
        nav_layout = QVBoxLayout(nav_card)
        nav_layout.setContentsMargins(14, 14, 14, 14)
        nav_title = QLabel("音视频处理")
        nav_title.setObjectName("appTitle")
        nav_layout.addWidget(nav_title)

        self.av_stack = QStackedWidget()
        self.av_btn_group = QButtonGroup()
        av_tools = [
            ('抖快视频解析', self.build_av_short_video_page),
            ('视频分离音频', self.build_av_extract_audio_page),
            ('音视频转换格式', self.build_av_convert_page),
            ('批量加封面', self.build_av_add_cover_page),
            ('视频加文字水印', self.build_av_text_watermark_page),
            ('音视频转文本', self.build_av_transcribe_page),
            ('音频裁剪', self.build_av_trim_page),
            ('保留人声', self.build_av_vocal_page),
        ]

        for i, (name, builder) in enumerate(av_tools):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setProperty("navButton", True)
            if i == 0:
                btn.setChecked(True)
            self.av_btn_group.addButton(btn, i)
            nav_layout.addWidget(btn)
            self.av_stack.addWidget(builder())

        nav_layout.addStretch()
        self.av_btn_group.idClicked.connect(self.av_stack.setCurrentIndex)
        layout.addWidget(nav_card)
        layout.addWidget(self.av_stack, stretch=1)
        return page

    def build_av_tool_page(self, title, desc, input_filter, allowed_exts, controls, run_callback):
        page = QFrame()
        page.setObjectName("card")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("appTitle")
        desc_lbl = QLabel(desc)
        desc_lbl.setObjectName("appSubtitle")
        desc_lbl.setWordWrap(True)
        layout.addWidget(title_lbl)
        layout.addWidget(desc_lbl)

        input_line = QLineEdit()
        input_line.setPlaceholderText("选择或拖入文件")
        drop = FileDropArea("拖拽音视频文件到这里，或点击选择一个/多个文件")
        drop.filesDropped.connect(lambda files, line=input_line: line.setText(";".join(files)))
        drop.clicked.connect(lambda line=input_line, filt=input_filter: self.choose_av_inputs(line, filt))
        layout.addWidget(drop)

        row = QHBoxLayout()
        row.addWidget(input_line, stretch=1)
        clear_btn = QPushButton("清空选择")
        clear_btn.clicked.connect(input_line.clear)
        row.addWidget(clear_btn)
        layout.addLayout(row)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        for label, widget in controls:
            form.addRow(label, widget)
        layout.addLayout(form)

        layout.addLayout(self.build_av_output_row())

        run_btn = QPushButton("开始处理")
        run_btn.setObjectName("startButton")
        run_btn.clicked.connect(lambda: run_callback(input_line))
        layout.addWidget(run_btn)
        layout.addStretch()
        return page

    def build_av_output_row(self):
        row = QHBoxLayout()
        output_hint = QLabel(f"输出目录: {os.path.abspath(self.av_output_dir)}")
        output_hint.setObjectName("appSubtitle")
        output_hint.setWordWrap(True)
        open_btn = QPushButton("打开输出目录")
        open_btn.clicked.connect(self.open_av_output_dir)
        row.addWidget(output_hint, stretch=1)
        row.addWidget(open_btn)
        return row

    def open_av_output_dir(self):
        os.makedirs(self.av_output_dir, exist_ok=True)
        try:
            os.startfile(os.path.abspath(self.av_output_dir))
        except Exception as exc:
            QMessageBox.warning(self, "打开失败", f"无法打开输出目录：{exc}")

    def choose_av_inputs(self, line_edit, file_filter):
        files, _ = QFileDialog.getOpenFileNames(self, "选择音视频文件", os.getcwd(), file_filter)
        if files:
            line_edit.setText(";".join(files))

    def _split_input_paths(self, text):
        return [p.strip().strip('"') for p in text.split(';') if p.strip()]

    def _av_output_path(self, input_path, suffix, ext):
        os.makedirs(self.av_output_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(input_path))[0]
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return os.path.abspath(os.path.join(self.av_output_dir, f"{base}_{suffix}_{stamp}.{ext}"))

    def get_media_duration(self, input_path):
        ffprobe = os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe'))
        if not os.path.exists(ffprobe) or not os.path.exists(input_path):
            return 0.0
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self.get_si()
            )
            return max(0.0, float(res.stdout.strip()))
        except Exception:
            return 0.0

    def _start_av_task(self, task):
        if hasattr(self, 'av_worker') and self.av_worker.isRunning():
            QMessageBox.warning(self, "任务进行中", "当前已有音视频处理任务在运行。")
            return
        inputs = self._split_input_paths(task.pop('input_text', task.get('input', '')))
        if not inputs:
            inputs = [task.get('input', '')]
        inputs = [p for p in inputs if p and os.path.exists(p)]
        if not inputs:
            QMessageBox.warning(self, "缺少文件", "请先选择有效的音视频文件。")
            return
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        if not os.path.exists(ffmpeg):
            QMessageBox.warning(self, "缺少组件", "未找到 tools/ffmpeg.exe。")
            return

        jobs = []
        for input_path in inputs:
            job = task.copy()
            covers = job.pop('cover_paths', [])
            if covers:
                job['cover_path'] = covers[(len(jobs)) % len(covers)]
            job.update({
                'input': input_path,
                'output': self._av_output_path(input_path, job.get('suffix', '输出'), job.get('output_ext', job.get('format', 'mp4'))),
                'ffmpeg': ffmpeg,
                'ffprobe': os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe')),
                'tools_dir': os.path.abspath(tools_dir()),
                'output_dir': os.path.abspath(self.av_output_dir),
                'startupinfo': self.get_si(),
            })
            jobs.append(job)
        self.task_progress.setValue(0)
        self.av_worker = AVProcessWorker({'jobs': jobs, 'startupinfo': self.get_si()})
        self.current_av_kind = task.get('kind', '')
        self.av_worker.log_signal.connect(lambda text: self.update_log(f"[音视频处理] {text}"))
        self.av_worker.progress_signal.connect(self.task_progress.setValue)
        self.av_worker.finished_signal.connect(self.on_av_task_finished)
        self.av_worker.start()
        self.update_log("[音视频处理] 任务已进入后台处理。")

    def on_av_task_finished(self, ok, message):
        if ok:
            self.task_progress.setValue(100)
            self.update_log(f"[音视频处理] 完成: {message}")
            if getattr(self, 'current_av_kind', '') == 'transcribe':
                self.load_transcribe_result(message)
        else:
            self.update_log(f"[音视频处理] 失败: {message}")
            QMessageBox.warning(self, "处理失败", message)

    def load_transcribe_result(self, message):
        if not hasattr(self, 'transcribe_result_text'):
            return
        paths = [p for p in str(message).split('；') if p.strip()]
        if len(paths) != 1:
            self.transcribe_result_text.setPlainText(f"已完成 {len(paths)} 个文件转写，结果已保存到输出目录。")
            return
        path = paths[0]
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                self.transcribe_result_text.setPlainText(f.read().strip())
        except Exception as e:
            self.transcribe_result_text.setPlainText(f"读取转写结果失败: {e}")

    def copy_transcribe_result(self):
        if not hasattr(self, 'transcribe_result_text'):
            return
        text = self.transcribe_result_text.toPlainText().strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.update_log("[音视频处理] 转写文本已复制到剪贴板。")

    def build_av_short_video_page(self):
        page = QFrame()
        page.setObjectName("card")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        title = QLabel("抖音快手视频解析")
        title.setObjectName("appTitle")
        desc = QLabel("粘贴抖音或快手分享链接，先解析视频流，再自动识别视频里的口播文案；也可一键下载音频或未加平台水印的原视频。")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(desc)

        self.short_video_input = QPlainTextEdit()
        self.short_video_input.setPlaceholderText("粘贴手机分享出来的完整文本，例如：复制下方链接，打开抖音/快手观看...")
        self.short_video_input.setMaximumHeight(92)
        layout.addWidget(self.short_video_input)

        cookie_group = QGroupBox("Cookie 设置")
        cookie_layout = QGridLayout(cookie_group)
        self.short_video_cookie_mode = QComboBox()
        self.short_video_cookie_mode.addItem("不使用 Cookie", "none")
        self.short_video_cookie_mode.addItem("读取 Chrome Cookie", "chrome")
        self.short_video_cookie_mode.addItem("读取 Edge Cookie", "edge")
        self.short_video_cookie_mode.addItem("读取 Firefox Cookie", "firefox")
        self.short_video_cookie_mode.addItem("选择 cookies.txt", "file")
        self.short_video_cookie_mode.setCurrentIndex(1)
        self.short_video_cookie_file = QLineEdit()
        self.short_video_cookie_file.setPlaceholderText("选择 cookies.txt 时填写；浏览器 Cookie 模式可留空")
        cookie_file_btn = QPushButton("选择")
        cookie_file_btn.clicked.connect(self.choose_short_video_cookie_file)
        cookie_layout.addWidget(QLabel("Cookie 来源:"), 0, 0)
        cookie_layout.addWidget(self.short_video_cookie_mode, 0, 1)
        cookie_layout.addWidget(self.short_video_cookie_file, 1, 0, 1, 2)
        cookie_layout.addWidget(cookie_file_btn, 1, 2)
        cookie_tip = QLabel("抖音提示 Fresh cookies 时，先用对应浏览器打开一次抖音/快手再解析；如果提示 Cookie 数据库被占用，请完全关闭对应浏览器后重试，或改用 cookies.txt。Cookie 只在本机读取，不上传。")
        cookie_tip.setObjectName("appSubtitle")
        cookie_tip.setWordWrap(True)
        cookie_layout.addWidget(cookie_tip, 2, 0, 1, 3)
        layout.addWidget(cookie_group)

        action_row = QHBoxLayout()
        parse_btn = QPushButton("解析链接")
        parse_btn.setObjectName("startButton")
        parse_btn.clicked.connect(self.start_short_video_parse)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.clear_short_video_page)
        action_row.addWidget(parse_btn)
        action_row.addWidget(clear_btn)
        action_row.addStretch()
        layout.addLayout(action_row)

        info_row = QHBoxLayout()
        self.short_video_status = QLabel("状态：等待解析")
        self.short_video_status.setObjectName("appSubtitle")
        self.short_video_meta = QLabel("")
        self.short_video_meta.setObjectName("appSubtitle")
        self.short_video_meta.setWordWrap(True)
        info_row.addWidget(self.short_video_status, 1)
        info_row.addWidget(self.short_video_meta, 3)
        layout.addLayout(info_row)

        result_group = QGroupBox("口播文案")
        result_layout = QVBoxLayout(result_group)
        copy_row = QHBoxLayout()
        copy_hint = QLabel("这里显示的是视频声音识别出来的文案，不是平台标题。")
        copy_hint.setObjectName("appSubtitle")
        copy_row.addWidget(copy_hint)
        copy_row.addStretch()
        copy_btn = QPushButton("一键复制文案")
        copy_btn.clicked.connect(self.copy_short_video_caption)
        copy_row.addWidget(copy_btn)
        self.short_video_caption = QTextEdit()
        self.short_video_caption.setPlaceholderText("解析成功后会自动识别视频口播内容，并显示在这里。")
        self.short_video_caption.setMinimumHeight(170)
        result_layout.addLayout(copy_row)
        result_layout.addWidget(self.short_video_caption)
        layout.addWidget(result_group)

        download_row = QHBoxLayout()
        self.short_video_audio_btn = QPushButton("一键下载音频")
        self.short_video_audio_btn.setEnabled(False)
        self.short_video_audio_btn.clicked.connect(lambda: self.start_short_video_download("audio"))
        self.short_video_video_btn = QPushButton("一键下载无水印原视频")
        self.short_video_video_btn.setEnabled(False)
        self.short_video_video_btn.clicked.connect(lambda: self.start_short_video_download("video"))
        open_btn = QPushButton("打开输出目录")
        open_btn.clicked.connect(self.open_av_output_dir)
        download_row.addWidget(self.short_video_audio_btn)
        download_row.addWidget(self.short_video_video_btn)
        download_row.addStretch()
        download_row.addWidget(open_btn)
        layout.addLayout(download_row)

        tip = QLabel("提示：请确认你拥有视频内容的合法使用权，并遵守平台规则；下载功能会优先使用平台返回的原始视频流。")
        tip.setObjectName("appSubtitle")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        layout.addStretch()

        self.short_video_info = None
        self.short_video_transcript_path = ""
        return page

    def choose_short_video_cookie_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", os.getcwd(), "Cookie Files (*.txt);;All Files (*.*)")
        if path:
            self.short_video_cookie_file.setText(path)
            if hasattr(self, 'short_video_cookie_mode'):
                index = self.short_video_cookie_mode.findData("file")
                if index >= 0:
                    self.short_video_cookie_mode.setCurrentIndex(index)

    def current_short_video_cookie_options(self):
        mode = "none"
        if hasattr(self, 'short_video_cookie_mode'):
            mode = self.short_video_cookie_mode.currentData() or "none"
        cookie_file = self.short_video_cookie_file.text().strip() if hasattr(self, 'short_video_cookie_file') else ""
        return {"mode": mode, "cookie_file": cookie_file}

    def clear_short_video_page(self):
        if hasattr(self, 'short_video_input'):
            self.short_video_input.clear()
        if hasattr(self, 'short_video_caption'):
            self.short_video_caption.clear()
        if hasattr(self, 'short_video_status'):
            self.short_video_status.setText("状态：等待解析")
        if hasattr(self, 'short_video_meta'):
            self.short_video_meta.clear()
        self.short_video_info = None
        self.short_video_transcript_path = ""
        for btn_name in ('short_video_audio_btn', 'short_video_video_btn'):
            if hasattr(self, btn_name):
                getattr(self, btn_name).setEnabled(False)

    def start_short_video_parse(self):
        raw_text = self.short_video_input.toPlainText().strip() if hasattr(self, 'short_video_input') else ""
        if not raw_text:
            QMessageBox.warning(self, "缺少链接", "请先粘贴抖音或快手分享链接。")
            return
        if hasattr(self, 'short_video_parse_worker') and self.short_video_parse_worker.isRunning():
            QMessageBox.information(self, "解析中", "当前链接正在解析，请稍等。")
            return
        self.short_video_status.setText("状态：正在解析")
        self.short_video_caption.setPlainText("")
        self.short_video_audio_btn.setEnabled(False)
        self.short_video_video_btn.setEnabled(False)
        cookie_options = self.current_short_video_cookie_options()
        mode_text = self.short_video_cookie_mode.currentText() if hasattr(self, 'short_video_cookie_mode') else "不使用 Cookie"
        self.update_log(f"[短视频解析] Cookie 来源：{mode_text}")
        self.short_video_parse_worker = ShortVideoParseWorker(raw_text, cookie_options)
        self.short_video_parse_worker.message.connect(lambda text: self.update_log(f"[短视频解析] {text}"))
        self.short_video_parse_worker.finished.connect(self.on_short_video_parsed)
        self.short_video_parse_worker.failed.connect(self.on_short_video_parse_failed)
        self.short_video_parse_worker.start()
        self.update_log("[短视频解析] 任务已进入后台处理。")

    def on_short_video_parsed(self, info):
        self.short_video_info = info
        self.short_video_transcript_path = ""
        title = info.get("title") or info.get("description") or ""
        share_caption = info.get("share_caption") or info.get("caption") or ""
        meta = " | ".join([item for item in [
            info.get("platform", ""),
            info.get("uploader", ""),
            f"作品标题/描述：{title}" if title else "",
            info.get("format_summary", ""),
            info.get("parsed_at", ""),
        ] if item])
        self.short_video_status.setText("状态：解析成功")
        self.short_video_meta.setText(meta)
        partial = bool(info.get("partial"))
        self.short_video_audio_btn.setEnabled(not partial)
        self.short_video_video_btn.setEnabled(not partial)
        if partial:
            self.short_video_status.setText("状态：仅提取到分享文本")
            self.short_video_caption.setPlainText(share_caption)
            self.update_log(f"[短视频解析] 仅提取到分享文本，无法识别口播文案：{info.get('parse_error', '')}")
        else:
            self.update_log(f"[短视频解析] 解析成功：{info.get('title', '')}")
            self.start_short_video_transcript()

    def start_short_video_transcript(self):
        if not self.short_video_info or self.short_video_info.get("partial"):
            return
        if hasattr(self, 'short_video_transcript_worker') and self.short_video_transcript_worker.isRunning():
            self.update_log("[短视频解析] 口播文案识别正在进行中。")
            return
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        sensevoice_dir = os.path.abspath(os.path.join(models_dir(), 'SenseVoiceSmall'))
        if not os.path.exists(ffmpeg):
            self.short_video_status.setText("状态：等待识别组件")
            self.short_video_caption.setPlainText("未找到 ffmpeg.exe，无法从视频里提取音频识别口播文案。")
            self.update_log("[短视频解析] 未找到 ffmpeg.exe，跳过口播文案识别。")
            return
        self.short_video_status.setText("状态：正在识别口播文案")
        self.short_video_caption.setPlainText("正在识别视频里的口播文案，请稍等...")
        self.task_progress.setValue(5)
        temp_dir = os.path.abspath(os.path.join(self.cache_dir, '短视频口播识别'))
        self.short_video_transcript_worker = ShortVideoTranscriptWorker(
            self.short_video_info,
            temp_dir,
            os.path.abspath(self.av_output_dir),
            ffmpeg,
            sensevoice_dir,
            self.current_short_video_cookie_options(),
        )
        self.short_video_transcript_worker.message.connect(lambda text: self.update_log(f"[短视频解析] {text}"))
        self.short_video_transcript_worker.progress.connect(self.task_progress.setValue)
        self.short_video_transcript_worker.finished.connect(self.on_short_video_transcribed)
        self.short_video_transcript_worker.failed.connect(self.on_short_video_transcribe_failed)
        self.short_video_transcript_worker.start()

    def on_short_video_transcribed(self, result):
        text = result.get("text", "")
        self.short_video_transcript_path = result.get("txt_path", "")
        self.short_video_caption.setPlainText(text)
        self.short_video_status.setText("状态：口播文案识别完成")
        self.task_progress.setValue(100)
        self.update_log(f"[短视频解析] 口播文案识别完成：{self.short_video_transcript_path}")

    def on_short_video_transcribe_failed(self, message):
        self.task_progress.setValue(0)
        fallback = ""
        if self.short_video_info:
            fallback = self.short_video_info.get("share_caption") or ""
        if fallback:
            self.short_video_caption.setPlainText(fallback)
        else:
            self.short_video_caption.setPlainText("口播文案识别失败。请确认 SenseVoiceSmall 模型已放入网盘资源包/models/SenseVoiceSmall，并确认该视频有清晰人声。")
        self.short_video_status.setText("状态：口播文案识别失败")
        self.update_log(f"[短视频解析] 口播文案识别失败：{message}")

    def on_short_video_parse_failed(self, message):
        self.short_video_info = None
        self.short_video_status.setText("状态：解析失败")
        self.short_video_audio_btn.setEnabled(False)
        self.short_video_video_btn.setEnabled(False)
        self.update_log(f"[短视频解析] 解析失败：{message}")
        QMessageBox.warning(self, "解析失败", message)

    def copy_short_video_caption(self):
        text = self.short_video_caption.toPlainText().strip() if hasattr(self, 'short_video_caption') else ""
        if not text:
            QMessageBox.information(self, "暂无文案", "当前没有可复制的视频文案。")
            return
        QApplication.clipboard().setText(text)
        self.update_log("[短视频解析] 口播文案已复制到剪贴板。")

    def start_short_video_download(self, kind):
        if not self.short_video_info:
            QMessageBox.warning(self, "未解析", "请先解析视频链接。")
            return
        if hasattr(self, 'short_video_download_worker') and self.short_video_download_worker.isRunning():
            QMessageBox.information(self, "下载中", "当前下载任务正在进行，请稍等。")
            return
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        if not os.path.exists(ffmpeg):
            QMessageBox.warning(self, "缺少组件", "未找到 网盘资源包/tools/ffmpeg.exe，无法合并视频或提取音频。")
            return
        self.task_progress.setValue(10)
        self.short_video_status.setText("状态：正在下载音频" if kind == "audio" else "状态：正在下载原视频")
        self.short_video_download_worker = ShortVideoDownloadWorker(
            self.short_video_info,
            os.path.abspath(self.av_output_dir),
            ffmpeg,
            kind,
            self.current_short_video_cookie_options(),
        )
        self.short_video_download_worker.message.connect(lambda text: self.update_log(f"[短视频解析] {text}"))
        self.short_video_download_worker.finished.connect(lambda path, k=kind: self.on_short_video_downloaded(k, path))
        self.short_video_download_worker.failed.connect(self.on_short_video_download_failed)
        self.short_video_download_worker.start()

    def on_short_video_downloaded(self, kind, path):
        self.task_progress.setValue(100)
        self.short_video_status.setText("状态：下载完成")
        label = "音频" if kind == "audio" else "原视频"
        self.update_log(f"[短视频解析] {label}下载完成：{path}")
        QMessageBox.information(self, "下载完成", f"{label}已保存：\n{path}")

    def on_short_video_download_failed(self, message):
        self.task_progress.setValue(0)
        self.short_video_status.setText("状态：下载失败")
        self.update_log(f"[短视频解析] 下载失败：{message}")
        QMessageBox.warning(self, "下载失败", message)

    def build_av_extract_audio_page(self):
        fmt = QComboBox(); fmt.addItems(["mp3", "wav"])
        return self.build_av_tool_page(
            "视频分离音频",
            "从视频文件中提取音频，输出到固定目录。",
            "Video Files (*.mp4 *.mov *.avi *.mkv *.webm)",
            ['.mp4', '.mov', '.avi', '.mkv', '.webm'],
            [("输出格式:", fmt)],
            lambda line: self._start_av_task({
                'kind': 'extract_audio',
                'input_text': line.text().strip(),
                'format': fmt.currentText(),
                'suffix': '提取音频',
                'output_ext': fmt.currentText()
            })
        )

    def build_av_convert_page(self):
        fmt = QComboBox(); fmt.addItems(["mp4", "mov", "avi", "mp3", "wav", "m4a"])
        return self.build_av_tool_page(
            "音视频转换格式",
            "转换常见音视频格式。视频格式会重新编码，音频格式会自动只保留音频流。",
            "Media Files (*.mp4 *.mov *.avi *.mkv *.webm *.mp3 *.wav *.m4a *.aac *.flac)",
            ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.aac', '.flac'],
            [("目标格式:", fmt)],
            lambda line: self._start_av_task({
                'kind': 'convert',
                'input_text': line.text().strip(),
                'format': fmt.currentText(),
                'suffix': '格式转换',
                'output_ext': fmt.currentText()
            })
        )

    def build_av_add_cover_page(self):
        page = QFrame()
        page.setObjectName("card")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        title = QLabel("批量加封面")
        title.setObjectName("appTitle")
        desc = QLabel("给每个视频开头追加封面图前 2 帧，适合给外部成品视频统一加首帧封面。")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(desc)

        self.cover_video_line = QLineEdit()
        self.cover_video_line.setPlaceholderText("选择或拖入一个/多个视频")
        video_drop = FileDropArea("拖拽视频文件到这里，或点击选择一个/多个视频")
        video_drop.filesDropped.connect(lambda files: self.cover_video_line.setText(";".join(files)))
        video_drop.clicked.connect(lambda: self.choose_av_inputs(self.cover_video_line, "Video Files (*.mp4 *.mov *.avi *.mkv *.webm)"))
        layout.addWidget(video_drop)
        video_row = QHBoxLayout()
        video_row.addWidget(self.cover_video_line, stretch=1)
        clear_video_btn = QPushButton("清空视频")
        clear_video_btn.clicked.connect(self.cover_video_line.clear)
        video_row.addWidget(clear_video_btn)
        layout.addLayout(video_row)

        self.cover_image_line = QLineEdit()
        self.cover_image_line.setPlaceholderText("选择或拖入封面图片；多张图片会按视频顺序循环使用")
        image_drop = FileDropArea("拖拽封面图片到这里，或点击选择一张/多张图片")
        image_drop.filesDropped.connect(lambda files: self.cover_image_line.setText(";".join(files)))
        image_drop.clicked.connect(lambda: self.choose_av_inputs(self.cover_image_line, "Image Files (*.jpg *.jpeg *.png *.webp *.bmp)"))
        layout.addWidget(image_drop)
        image_row = QHBoxLayout()
        image_row.addWidget(self.cover_image_line, stretch=1)
        clear_image_btn = QPushButton("清空封面")
        clear_image_btn.clicked.connect(self.cover_image_line.clear)
        image_row.addWidget(clear_image_btn)
        layout.addLayout(image_row)

        hint = QLabel("封面时长固定为前 2 帧；输出到音视频处理输出目录，不覆盖原视频。")
        hint.setObjectName("warningText")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addLayout(self.build_av_output_row())

        run_btn = QPushButton("开始批量加封面")
        run_btn.setObjectName("startButton")
        run_btn.clicked.connect(self.start_av_add_cover)
        layout.addWidget(run_btn)
        layout.addStretch()
        return page

    def start_av_add_cover(self):
        cover_paths = [p for p in self._split_input_paths(self.cover_image_line.text().strip()) if os.path.exists(p)]
        if not cover_paths:
            QMessageBox.warning(self, "缺少封面", "请先选择有效的封面图片。")
            return
        self._start_av_task({
            'kind': 'add_cover',
            'input_text': self.cover_video_line.text().strip(),
            'cover_paths': cover_paths,
            'cover_seconds': 2 / 30,
            'suffix': '加封面',
            'output_ext': 'mp4'
        })

    def probe_video_size_for_ui(self, input_path):
        ffprobe = os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe'))
        if not os.path.exists(ffprobe) or not os.path.exists(input_path):
            return 1080, 1920
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0:s=x', input_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self.get_si()
            )
            text = (res.stdout or "").strip().splitlines()[0]
            width, height = [int(x) for x in text.split('x')[:2]]
            return max(2, width), max(2, height)
        except Exception:
            return 1080, 1920

    def choose_av_watermark_videos(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择要加文字水印的视频", os.getcwd(), "Video Files (*.mp4 *.mov *.avi *.mkv *.webm)")
        if files:
            self.set_av_watermark_videos(files)

    def set_av_watermark_videos(self, files):
        paths = [p for p in files if p and os.path.exists(p)]
        if not paths:
            return
        self.av_wm_video_line.setText(";".join(paths))
        self.load_av_watermark_reference(paths[0])

    def load_av_watermark_reference(self, video_path):
        width, height = self.probe_video_size_for_ui(video_path)
        self.av_wm_design_w = width
        self.av_wm_design_h = height
        self.av_wm_x.setRange(0, width)
        self.av_wm_y.setRange(0, height)
        self.av_wm_x.setValue(width // 2)
        self.av_wm_y.setValue(height // 2)
        try:
            bg_path = os.path.join(os.path.abspath(self.cache_dir), "preview_bg_av_watermark.jpg")
            ffmpeg_exe = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
            extracted = extract_reference_frame(ffmpeg_exe, video_path, bg_path)
            self.av_wm_preview_bg = extracted if extracted and os.path.exists(extracted) else ""
        except Exception as exc:
            self.av_wm_preview_bg = ""
            self.update_log(f"[音视频处理] 水印参考帧提取失败: {exc}")
        self.draw_av_watermark_preview()

    def set_av_wm_position_from_preview(self, x, y):
        self.av_wm_x.blockSignals(True)
        self.av_wm_y.blockSignals(True)
        self.av_wm_x.setValue(x)
        self.av_wm_y.setValue(y)
        self.av_wm_x.blockSignals(False)
        self.av_wm_y.blockSignals(False)
        self.draw_av_watermark_preview()

    def current_av_watermark_data(self):
        font_name = self.av_wm_font.currentText()
        return {
            'text': self.av_wm_text.toPlainText(),
            'font_name': font_name,
            'font_path': self.sys_fonts.get(font_name, ""),
            'size': self.av_wm_size.value(),
            'color': self.av_wm_color.currentText(),
            'align': self.av_wm_align.currentText(),
            'border_w': self.av_wm_border_w.value(),
            'border_color': self.av_wm_border_color.currentText(),
            'is_bold': self.av_wm_bold.isChecked(),
            'x': self.av_wm_x.value(),
            'y': self.av_wm_y.value(),
            'design_w': getattr(self, 'av_wm_design_w', 1080),
            'design_h': getattr(self, 'av_wm_design_h', 1920),
        }

    def set_av_watermark_mode(self, mode, row=-1):
        if not hasattr(self, 'av_wm_mode_label'):
            return
        if mode == "edit":
            self.av_wm_mode_label.setText(f"编辑模式：正在修改第 {row + 1} 条水印，点“清空面板/退出编辑”可回到新增")
            self.av_wm_mode_label.setStyleSheet("color:#FDBA74; background:#2A1F0B; border:1px solid #B45309; border-radius:8px; padding:7px; font-weight:bold;")
            self.av_wm_modify_btn.setEnabled(True)
            self.av_wm_add_btn.setText("另存为新水印")
        else:
            self.av_wm_mode_label.setText("新增模式：当前面板内容会作为一条新水印添加到列表")
            self.av_wm_mode_label.setStyleSheet("color:#BBF7D0; background:#052E16; border:1px solid #22C55E; border-radius:8px; padding:7px; font-weight:bold;")
            self.av_wm_modify_btn.setEnabled(False)
            self.av_wm_add_btn.setText("添加为新水印")

    def selected_av_watermark_row(self):
        if not hasattr(self, 'av_wm_table') or not self.av_wm_table.selectionModel():
            return -1
        selected = self.av_wm_table.selectionModel().selectedRows()
        if not selected:
            return -1
        row = selected[0].row()
        return row if 0 <= row < len(self.av_watermarks_data) else -1

    def reset_av_watermark_form(self):
        if hasattr(self, 'av_wm_table'):
            self.av_wm_table.blockSignals(True)
            self.av_wm_table.clearSelection()
            self.av_wm_table.blockSignals(False)
        self.av_editing_wm_index = -1
        self.set_av_watermark_mode("new")
        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            widget.blockSignals(True)
        self.av_wm_text.clear()
        self.av_wm_font.setCurrentText("🎲 随机系统字体")
        self.av_wm_size.setValue(60)
        self.av_wm_color.setCurrentText("white")
        self.av_wm_align.setCurrentText("居中对齐")
        self.av_wm_bold.setChecked(False)
        self.av_wm_border_w.setValue(2)
        self.av_wm_border_color.setCurrentText("black")
        self.av_wm_x.setValue(int(getattr(self, 'av_wm_design_w', 1080)) // 2)
        self.av_wm_y.setValue(int(getattr(self, 'av_wm_design_h', 1920)) // 2)
        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            widget.blockSignals(False)
        self.draw_av_watermark_preview()

    def add_av_watermark_new(self):
        data = self.current_av_watermark_data()
        if not data['text'].strip():
            QMessageBox.warning(self, "缺少文字", "请先输入文字水印内容。")
            return
        self.av_watermarks_data.append(data)
        self.add_av_wm_to_table(data)
        self.reset_av_watermark_form()

    def modify_selected_av_watermark(self):
        row = self.selected_av_watermark_row()
        if row < 0:
            return
        data = self.current_av_watermark_data()
        if not data['text'].strip():
            QMessageBox.warning(self, "缺少文字", "请先输入文字水印内容。")
            return
        self.av_watermarks_data[row] = data
        self.update_av_wm_table_row(row, data)
        self.reset_av_watermark_form()

    def delete_selected_av_watermark(self):
        row = self.selected_av_watermark_row()
        if row < 0:
            return
        del self.av_watermarks_data[row]
        self.av_wm_table.removeRow(row)
        self.reset_av_watermark_form()

    def add_av_wm_to_table(self, data):
        row = self.av_wm_table.rowCount()
        self.av_wm_table.insertRow(row)
        self.update_av_wm_table_row(row, data)

    def update_av_wm_table_row(self, row, data):
        self.av_wm_table.setItem(row, 0, QTableWidgetItem(str(data.get('text', '')).replace('\n', '  ')[:80]))
        self.av_wm_table.setItem(row, 1, QTableWidgetItem(str(data.get('font_name', ''))))
        self.av_wm_table.setItem(row, 2, QTableWidgetItem(f"大小:{data.get('size', 60)} 颜色:{data.get('color', 'white')}"))
        self.av_wm_table.setItem(row, 3, QTableWidgetItem(f"{data.get('x', 0)}, {data.get('y', 0)}"))

    def on_av_wm_table_selection_changed(self):
        row = self.selected_av_watermark_row()
        if row < 0:
            self.av_editing_wm_index = -1
            self.set_av_watermark_mode("new")
            self.draw_av_watermark_preview()
            return
        self.av_editing_wm_index = row
        self.set_av_watermark_mode("edit", row)
        wm = self.av_watermarks_data[row]
        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            widget.blockSignals(True)
        self.av_wm_text.setPlainText(wm.get('text', ''))
        self.av_wm_font.setCurrentText(wm.get('font_name', '🎲 随机系统字体'))
        self.av_wm_size.setValue(int(wm.get('size', 60)))
        self.av_wm_color.setCurrentText(wm.get('color', 'white'))
        self.av_wm_align.setCurrentText(wm.get('align', '居中对齐'))
        self.av_wm_bold.setChecked(bool(wm.get('is_bold', False)))
        self.av_wm_border_w.setValue(int(wm.get('border_w', 2)))
        self.av_wm_border_color.setCurrentText(wm.get('border_color', 'black'))
        self.av_wm_x.setValue(int(wm.get('x', 540)))
        self.av_wm_y.setValue(int(wm.get('y', 960)))
        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            widget.blockSignals(False)
        self.draw_av_watermark_preview()

    def load_mix_watermarks_to_av(self):
        if not self.watermarks_data:
            QMessageBox.information(self, "暂无水印", "批量混剪里的文字水印列表为空。")
            return
        self.av_watermarks_data = []
        self.av_wm_table.setRowCount(0)
        for wm in self.watermarks_data:
            data = dict(wm)
            data['design_w'] = getattr(self, 'av_wm_design_w', 1080)
            data['design_h'] = getattr(self, 'av_wm_design_h', 1920)
            self.av_watermarks_data.append(data)
            self.add_av_wm_to_table(data)
        self.reset_av_watermark_form()
        self.update_log("[音视频处理] 已复制批量混剪文字水印列表到当前页面。")

    def draw_av_text_lines(self, wm, scale_ratio):
        color = "#FF1493" if wm.get('color') == "🎲 随机颜色" else wm.get('color', 'white')
        border_color = "#00FFFF" if wm.get('border_color') == "🎲 随机颜色" else wm.get('border_color', 'black')
        lines = str(wm.get('text', '')).splitlines()
        font_size = max(8, int(float(wm.get('size', 60)) * scale_ratio))
        border_w = max(0, int(float(wm.get('border_w', 0)) * scale_ratio))
        if wm.get('is_bold') and border_w <= 0:
            border_w = 1
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            temp_lbl = QLabel(line)
            font = temp_lbl.font()
            font.setPointSize(font_size)
            font.setBold(wm.get('is_bold', False))
            temp_lbl.setFont(font)
            temp_lbl.adjustSize()
            base_x = float(wm.get('x', 0)) * scale_ratio
            line_y = (float(wm.get('y', 0)) + i * float(wm.get('size', 60)) * 1.3) * scale_ratio
            align = wm.get('align', '居中对齐')
            if align == "居中对齐":
                final_x = base_x - temp_lbl.width() / 2
            elif align == "右对齐":
                final_x = base_x - temp_lbl.width()
            else:
                final_x = base_x
            temp_lbl.deleteLater()
            if border_w > 0:
                for dx, dy in [(-border_w, -border_w), (border_w, -border_w), (-border_w, border_w), (border_w, border_w), (0, -border_w), (0, border_w), (-border_w, 0), (border_w, 0)]:
                    border_lbl = QLabel(line, self.av_wm_preview_canvas)
                    border_lbl.setStyleSheet(f"color:{border_color}; background:transparent;")
                    border_lbl.setFont(font)
                    border_lbl.adjustSize()
                    border_lbl.move(int(final_x) + dx, int(line_y) + dy)
                    border_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
                    border_lbl.show()
            lbl = QLabel(line, self.av_wm_preview_canvas)
            lbl.setStyleSheet(f"color:{color}; background:transparent;")
            lbl.setFont(font)
            lbl.adjustSize()
            lbl.move(int(final_x), int(line_y))
            lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            lbl.show()

    def draw_av_watermark_preview(self):
        if not hasattr(self, 'av_wm_preview_canvas'):
            return
        for child in self.av_wm_preview_canvas.children():
            child.deleteLater()
        design_w = max(1, int(getattr(self, 'av_wm_design_w', 1080)))
        design_h = max(1, int(getattr(self, 'av_wm_design_h', 1920)))
        if design_h >= design_w:
            canvas_w = 360
            canvas_h = max(360, int(canvas_w * design_h / design_w))
            canvas_h = min(720, canvas_h)
        else:
            canvas_w = 640
            canvas_h = max(240, int(canvas_w * design_h / design_w))
            canvas_h = min(420, canvas_h)
        self.av_wm_preview_canvas.setFixedSize(canvas_w, canvas_h)
        self.av_wm_preview_canvas.set_design_size(design_w, design_h)
        if getattr(self, 'av_wm_preview_bg', '') and os.path.exists(self.av_wm_preview_bg):
            bg_lbl = QLabel(self.av_wm_preview_canvas)
            bg_lbl.setGeometry(0, 0, canvas_w, canvas_h)
            bg_lbl.setAlignment(Qt.AlignCenter)
            bg_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
            bg_lbl.setPixmap(QPixmap(self.av_wm_preview_bg).scaled(canvas_w, canvas_h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            bg_lbl.show()
        scale_ratio = canvas_w / design_w
        for index, wm in enumerate(getattr(self, 'av_watermarks_data', [])):
            if index == getattr(self, 'av_editing_wm_index', -1):
                continue
            self.draw_av_text_lines(wm, scale_ratio)
        active = self.current_av_watermark_data()
        if active['text'].strip():
            self.draw_av_text_lines(active, scale_ratio)

    def build_av_text_watermark_page(self):
        page = QFrame()
        page.setObjectName("card")
        root = QHBoxLayout(page)
        root.setContentsMargins(18, 16, 18, 18)
        root.setSpacing(14)

        left = QVBoxLayout()
        title = QLabel("视频加文字水印")
        title.setObjectName("appTitle")
        desc = QLabel("给外部做好的视频单独添加文字水印，支持批量视频、坐标预览、字体颜色和描边。")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        left.addWidget(title)
        left.addWidget(desc)
        self.av_watermarks_data = []
        self.av_editing_wm_index = -1

        self.av_wm_video_line = QLineEdit()
        self.av_wm_video_line.setPlaceholderText("选择或拖入一个/多个视频")
        video_drop = FileDropArea("拖拽视频到这里，或点击选择一个/多个视频")
        video_drop.filesDropped.connect(self.set_av_watermark_videos)
        video_drop.clicked.connect(self.choose_av_watermark_videos)
        left.addWidget(video_drop)
        video_row = QHBoxLayout()
        video_row.addWidget(self.av_wm_video_line, stretch=1)
        clear_btn = QPushButton("清空视频")
        clear_btn.clicked.connect(self.av_wm_video_line.clear)
        video_row.addWidget(clear_btn)
        left.addLayout(video_row)

        form = QFormLayout()
        self.av_wm_text = QPlainTextEdit()
        self.av_wm_text.setPlaceholderText("输入要添加到视频里的文字水印，支持多行")
        self.av_wm_text.setMaximumHeight(88)
        self.av_wm_font = QComboBox()
        self.av_wm_font.addItem("🎲 随机系统字体")
        self.av_wm_font.addItems(list(self.sys_fonts.keys()))
        self.av_wm_size = QSpinBox(); self.av_wm_size.setRange(20, 300); self.av_wm_size.setValue(60)
        self.av_wm_color = QComboBox(); self.populate_color_combo(self.av_wm_color); self.av_wm_color.addItem("🎲 随机颜色"); self.av_wm_color.setCurrentText("white")
        self.av_wm_align = QComboBox(); self.av_wm_align.addItems(["左对齐", "居中对齐", "右对齐"]); self.av_wm_align.setCurrentText("居中对齐")
        self.av_wm_bold = QCheckBox("强制加粗")
        self.av_wm_border_w = QSpinBox(); self.av_wm_border_w.setRange(0, 10); self.av_wm_border_w.setValue(2)
        self.av_wm_border_color = QComboBox(); self.populate_color_combo(self.av_wm_border_color); self.av_wm_border_color.addItem("🎲 随机颜色"); self.av_wm_border_color.setCurrentText("black")
        self.av_wm_x = QSpinBox(); self.av_wm_x.setRange(0, 3840); self.av_wm_x.setValue(540)
        self.av_wm_y = QSpinBox(); self.av_wm_y.setRange(0, 3840); self.av_wm_y.setValue(960)

        self.av_wm_mode_label = QLabel("新增模式：当前面板内容会作为一条新水印添加到列表")
        self.av_wm_mode_label.setWordWrap(True)
        self.av_wm_mode_label.setStyleSheet("color:#BBF7D0; background:#052E16; border:1px solid #22C55E; border-radius:8px; padding:7px; font-weight:bold;")
        font_row = QHBoxLayout(); font_row.addWidget(self.av_wm_font, 2); font_row.addWidget(self.av_wm_size, 1)
        color_row = QHBoxLayout(); color_row.addWidget(QLabel("字体颜色:")); color_row.addWidget(self.av_wm_color); color_row.addWidget(QLabel("对齐:")); color_row.addWidget(self.av_wm_align)
        border_row = QHBoxLayout(); border_row.addWidget(self.av_wm_bold); border_row.addWidget(QLabel("描边:")); border_row.addWidget(self.av_wm_border_w); border_row.addWidget(QLabel("描边颜色:")); border_row.addWidget(self.av_wm_border_color)
        coord_row = QHBoxLayout(); coord_row.addWidget(QLabel("X:")); coord_row.addWidget(self.av_wm_x); coord_row.addWidget(QLabel("Y:")); coord_row.addWidget(self.av_wm_y)
        form.addRow(self.av_wm_mode_label)
        form.addRow("水印文字:", self.av_wm_text)
        form.addRow("字体与大小:", font_row)
        form.addRow("颜色与对齐:", color_row)
        form.addRow("格式强化:", border_row)
        form.addRow("坐标微调:", coord_row)
        left.addLayout(form)

        action_row = QHBoxLayout()
        self.av_wm_add_btn = QPushButton("添加为新水印")
        self.av_wm_add_btn.clicked.connect(self.add_av_watermark_new)
        self.av_wm_modify_btn = QPushButton("覆盖选中水印")
        self.av_wm_modify_btn.setEnabled(False)
        self.av_wm_modify_btn.clicked.connect(self.modify_selected_av_watermark)
        av_wm_delete_btn = QPushButton("删除")
        av_wm_delete_btn.clicked.connect(self.delete_selected_av_watermark)
        av_wm_reset_btn = QPushButton("清空面板/退出编辑")
        av_wm_reset_btn.clicked.connect(self.reset_av_watermark_form)
        action_row.addWidget(self.av_wm_add_btn)
        action_row.addWidget(self.av_wm_modify_btn)
        action_row.addWidget(av_wm_delete_btn)
        action_row.addWidget(av_wm_reset_btn)
        left.addLayout(action_row)

        self.av_wm_table = QTableWidget(0, 4)
        self.av_wm_table.setHorizontalHeaderLabels(["文本", "字体", "格式", "坐标"])
        self.av_wm_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.av_wm_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.av_wm_table.verticalHeader().setVisible(False)
        self.av_wm_table.setMinimumHeight(145)
        self.av_wm_table.itemSelectionChanged.connect(self.on_av_wm_table_selection_changed)
        left.addWidget(self.av_wm_table)

        copy_mix_btn = QPushButton("复制批量混剪的文字水印列表")
        copy_mix_btn.clicked.connect(self.load_mix_watermarks_to_av)
        left.addWidget(copy_mix_btn)

        for widget in (self.av_wm_text, self.av_wm_font, self.av_wm_size, self.av_wm_color, self.av_wm_align, self.av_wm_bold, self.av_wm_border_w, self.av_wm_border_color, self.av_wm_x, self.av_wm_y):
            signal = getattr(widget, 'textChanged', None) or getattr(widget, 'valueChanged', None) or getattr(widget, 'currentTextChanged', None) or getattr(widget, 'stateChanged', None)
            if signal:
                signal.connect(lambda *args: self.draw_av_watermark_preview())

        left.addLayout(self.build_av_output_row())
        run_btn = QPushButton("开始给视频加文字水印")
        run_btn.setObjectName("startButton")
        run_btn.clicked.connect(self.start_av_text_watermark)
        left.addWidget(run_btn)
        left.addStretch()

        right = QVBoxLayout()
        preview_title = QLabel("参考预览：可直接点击/拖动定位水印")
        preview_title.setObjectName("appSubtitle")
        self.av_wm_preview_canvas = DragPreviewCanvas()
        self.av_wm_preview_canvas.setObjectName("referencePreview")
        self.av_wm_preview_canvas.setStyleSheet("background-color:#111; border:2px solid #22C55E; border-radius:8px;")
        self.av_wm_preview_canvas.setFixedSize(360, 640)
        self.av_wm_preview_canvas.pointChanged.connect(self.set_av_wm_position_from_preview)
        self.av_wm_design_w = 1080
        self.av_wm_design_h = 1920
        self.av_wm_preview_bg = ""
        right.addWidget(preview_title)
        right.addWidget(self.av_wm_preview_canvas, alignment=Qt.AlignCenter)
        right.addStretch()

        root.addLayout(left, 2)
        root.addLayout(right, 1)
        QTimer.singleShot(0, self.draw_av_watermark_preview)
        return page

    def start_av_text_watermark(self):
        watermarks = list(getattr(self, 'av_watermarks_data', []))
        active = self.current_av_watermark_data()
        if not watermarks and active['text'].strip():
            watermarks = [active]
        if not watermarks:
            QMessageBox.warning(self, "缺少水印", "请先添加至少一条文字水印。")
            return
        if watermarks and active['text'].strip() and getattr(self, 'av_editing_wm_index', -1) < 0 and active not in watermarks:
            self.update_log("[音视频处理] 当前面板有未添加文字，本次只处理列表里的水印；如需加入，请先点“添加为新水印”。")
        self._start_av_task({
            'kind': 'text_watermark',
            'input_text': self.av_wm_video_line.text().strip(),
            'watermarks': watermarks,
            'sys_fonts': self.sys_fonts,
            'suffix': '文字水印',
            'output_ext': 'mp4'
        })

    def build_av_transcribe_page(self):
        page = self.build_av_tool_page(
            "音视频转换成文本",
            "优先使用本地 SenseVoiceSmall 转成 txt 文本；模型不可用时才回退 Whisper。",
            "Media Files (*.mp4 *.mov *.avi *.mkv *.webm *.mp3 *.wav *.m4a *.aac *.flac)",
            ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.aac', '.flac'],
            [],
            lambda line: self._start_av_task({
                'kind': 'transcribe',
                'input_text': line.text().strip(),
                'suffix': '转文本',
                'output_ext': 'txt'
            })
        )
        result_group = QGroupBox("转写结果")
        result_layout = QVBoxLayout(result_group)
        copy_row = QHBoxLayout()
        copy_row.addStretch()
        preload_btn = QPushButton("预热转写模型")
        preload_btn.clicked.connect(self.preload_asr_model)
        copy_row.addWidget(preload_btn)
        copy_btn = QPushButton("一键复制")
        copy_btn.clicked.connect(self.copy_transcribe_result)
        copy_row.addWidget(copy_btn)
        self.transcribe_result_text = QTextEdit()
        self.transcribe_result_text.setPlaceholderText("单个音视频转写完成后，文本会显示在这里。")
        self.transcribe_result_text.setMinimumHeight(180)
        result_layout.addLayout(copy_row)
        result_layout.addWidget(self.transcribe_result_text)
        page.layout().addWidget(result_group)
        return page

    def preload_asr_model(self):
        if hasattr(self, 'asr_preload_worker') and self.asr_preload_worker.isRunning():
            return
        self.update_log("[音视频处理] 正在预热 SenseVoiceSmall 转写模型...")
        self.asr_preload_worker = ASRPreloadWorker()
        self.asr_preload_worker.finished_signal.connect(self.on_asr_preload_finished)
        self.asr_preload_worker.start()

    def on_asr_preload_finished(self, ok, message):
        self.update_log(f"[音视频处理] {message}")
        if not ok:
            QMessageBox.warning(self, "预热失败", message)

    def build_av_trim_page(self):
        page = QFrame()
        page.setObjectName("card")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        title = QLabel("音频裁剪")
        title.setObjectName("appTitle")
        desc = QLabel("拖入或点击选择音频后，直接拖动左右选择框选取片段。播放按钮会循环试听当前选区。")
        desc.setObjectName("appSubtitle")
        desc.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(desc)

        self.trim_input_line = QLineEdit()
        self.trim_input_line.setPlaceholderText("选择或拖入一个音频文件")
        drop = FileDropArea("拖拽音频文件到这里，或点击选择文件")
        drop.filesDropped.connect(lambda files: self.set_trim_input(files[0] if files else ""))
        drop.clicked.connect(self.choose_trim_input)
        layout.addWidget(drop)
        layout.addWidget(self.trim_input_line)

        self.trim_range_selector = AudioRangeSelector()
        self.trim_range_selector.rangeChanged.connect(self.on_trim_range_changed)
        layout.addWidget(self.trim_range_selector)

        self.trim_range_label = QLabel("选区: 00:00.00 - 00:03.00")
        self.trim_range_label.setObjectName("appSubtitle")
        layout.addWidget(self.trim_range_label)
        layout.addLayout(self.build_av_output_row())

        control_row = QHBoxLayout()
        self.trim_play_btn = QPushButton()
        self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.trim_play_btn.setToolTip("播放/暂停选区")
        self.trim_play_btn.clicked.connect(self.toggle_trim_preview)
        stop_btn = QPushButton()
        stop_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        stop_btn.setToolTip("停止试听")
        stop_btn.clicked.connect(self.stop_trim_preview)
        fmt = QComboBox(); fmt.addItems(["mp3", "wav"])
        control_row.addWidget(self.trim_play_btn)
        control_row.addWidget(stop_btn)
        control_row.addStretch()
        control_row.addWidget(QLabel("输出格式:"))
        control_row.addWidget(fmt)
        layout.addLayout(control_row)

        export_btn = QPushButton("导出选中片段")
        export_btn.setObjectName("startButton")
        export_btn.clicked.connect(lambda: self.export_trim_selection(fmt.currentText()))
        layout.addWidget(export_btn)
        layout.addStretch()
        return page

    def choose_trim_input(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "选择音频文件", os.getcwd(), "Audio Files (*.mp3 *.wav *.m4a *.aac *.flac)")
        if file_path:
            self.set_trim_input(file_path)

    def set_trim_input(self, file_path):
        if not file_path:
            return
        self.trim_input_line.setText(file_path)
        duration = self.get_media_duration(file_path)
        if duration > 0:
            self.trim_range_selector.set_duration(duration)
            ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
            self.trim_range_selector.set_waveform(build_waveform_peaks(ffmpeg, file_path))
            self.update_trim_range_label(*self.trim_range_selector.selected_range())
            self.update_log(f"[音频裁剪] 已加载音频，时长 {duration:.2f} 秒。")
        else:
            self.update_log("[音频裁剪] 未能读取音频时长。")

    def update_trim_range_label(self, start, end):
        self.trim_range_label.setText(f"选区: {self.trim_range_selector._fmt(start)} - {self.trim_range_selector._fmt(end)}  共 {end - start:.2f} 秒")

    def on_trim_range_changed(self, start, end):
        self.update_trim_range_label(start, end)
        if getattr(self, 'trim_looping', False):
            self.stop_trim_preview()

    def _ensure_trim_player(self):
        if self.trim_player:
            return
        self.trim_player = QMediaPlayer(self)
        self.trim_player.positionChanged.connect(self.on_trim_position_changed)
        self.trim_player.mediaStatusChanged.connect(self.on_trim_media_status_changed)

    def play_trim_from_selection(self, path):
        start, end = self.trim_range_selector.selected_range()
        self.trim_looping = True
        self.trim_preview_start_ms = int(start * 1000)
        self.trim_preview_end_ms = int(end * 1000)
        self.trim_current_media_path = path
        self.trim_pending_seek = True

        current_media = self.trim_player.media()
        current_url = current_media.canonicalUrl().toLocalFile() if not current_media.isNull() else ""
        if os.path.abspath(current_url) != os.path.abspath(path):
            self.trim_player.setMedia(QMediaContent(QUrl.fromLocalFile(path)))
        else:
            self.trim_player.stop()
            self.trim_player.setPosition(self.trim_preview_start_ms)
            QTimer.singleShot(0, lambda: self.trim_player.setPosition(self.trim_preview_start_ms))
            QTimer.singleShot(30, self.trim_player.play)
            self.trim_pending_seek = False
        self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def on_trim_media_status_changed(self, status):
        if not getattr(self, 'trim_pending_seek', False):
            return
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self.trim_player.stop()
            self.trim_player.setPosition(self.trim_preview_start_ms)
            QTimer.singleShot(0, lambda: self.trim_player.setPosition(self.trim_preview_start_ms))
            QTimer.singleShot(30, self.trim_player.play)
            self.trim_pending_seek = False

    def toggle_trim_preview(self):
        path = self.trim_input_line.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "缺少音频", "请先选择音频文件。")
            return
        self._ensure_trim_player()
        if self.trim_looping and self.trim_player.state() == QMediaPlayer.PlayingState:
            self.trim_player.pause()
            self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            return
        if self.trim_looping and self.trim_player.state() == QMediaPlayer.PausedState:
            self.trim_player.play()
            self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
            return
        self.play_trim_from_selection(path)

    def on_trim_position_changed(self, pos):
        if self.trim_looping and hasattr(self, 'trim_preview_end_ms') and pos >= self.trim_preview_end_ms:
            self.trim_player.setPosition(self.trim_preview_start_ms)
            self.trim_player.play()

    def stop_trim_preview(self):
        self.trim_looping = False
        if self.trim_player:
            self.trim_player.stop()
        self.trim_pending_seek = False
        if hasattr(self, 'trim_play_btn'):
            self.trim_play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def export_trim_selection(self, fmt):
        path = self.trim_input_line.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "缺少音频", "请先选择音频文件。")
            return
        start, end = self.trim_range_selector.selected_range()
        self._start_av_task({
            'kind': 'trim_audio',
            'input_text': path,
            'start': round(start, 3),
            'duration': round(end - start, 3),
            'format': fmt,
            'suffix': '裁剪',
            'output_ext': fmt
        })

    def build_av_vocal_page(self):
        fmt = QComboBox(); fmt.addItems(["wav", "mp3"])
        return self.build_av_tool_page(
            "去除背景音乐保留人声",
            "基础版会做高低频过滤、降噪和人声响度增强。真正的人声/伴奏分离后续可接入 Demucs/UVR 本地模型。",
            "Media Files (*.mp4 *.mov *.avi *.mkv *.webm *.mp3 *.wav *.m4a *.aac *.flac)",
            ['.mp4', '.mov', '.avi', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.aac', '.flac'],
            [("输出格式:", fmt)],
            lambda line: self._start_av_task({
                'kind': 'vocal_enhance',
                'input_text': line.text().strip(),
                'format': fmt.currentText(),
                'suffix': '保留人声',
                'output_ext': fmt.currentText()
            })
        )

    def initUI(self):
        self.setObjectName("root")
        self.apply_modern_theme()
        self.setWindowTitle('牛爷爷电商视频工具箱 V6.3')
        self.resize(1890, 1185)
        self.setMinimumSize(1360, 820)

        central = QWidget()
        central.setObjectName("root")
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(14, 18, 14, 18)
        side_layout.setSpacing(8)

        brand_title = QLabel("电商视频工具箱")
        brand_title.setObjectName("brandTitle")
        brand_subtitle = QLabel("内容生产工作台")
        brand_subtitle.setObjectName("brandSubTitle")
        side_layout.addWidget(brand_title)
        side_layout.addWidget(brand_subtitle)
        side_layout.addSpacing(14)

        self.module_stack = QStackedWidget()
        self.module_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.module_btn_group = QButtonGroup()
        self.nav_names = ['批量混剪', '音视频处理', 'AI音频克隆', 'AI商品图生成', '直播间解析下载']

        for i, name in enumerate(self.nav_names):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setProperty("navButton", True)
            btn.setMinimumHeight(42)
            if i == 0:
                btn.setChecked(True)
            self.module_btn_group.addButton(btn, i)
            side_layout.addWidget(btn)

            if name == '批量混剪':
                page = self.build_batch_mixer_page()
            elif name == '音视频处理':
                page = self.build_av_processing_page()
            elif name == 'AI音频克隆':
                page = VoiceCloneWidget()
            elif name == 'AI商品图生成':
                page = ImageGenerateWidget()
            elif name == '直播间解析下载':
                page = LiveDownloaderWidget()
            else:
                page = self.build_card_page(name, QWidget())
            self.module_stack.addWidget(page)

        side_layout.addStretch()
        self.module_btn_group.idClicked.connect(self.switch_module)

        content = QFrame()
        content.setObjectName("contentPanel")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 14, 18, 14)
        content_layout.setSpacing(10)

        header_layout = QHBoxLayout()
        app_title = QLabel("牛爷爷电商视频工具箱 ｜ 商业混剪 / 音视频处理 / AI音频克隆 / AI商品图生成 / 直播录制")
        app_title.setObjectName("appTitle")
        app_title.setWordWrap(False)
        header_layout.addWidget(app_title)
        header_layout.addStretch()
        feedback_btn = QPushButton("复制反馈日志")
        feedback_btn.setToolTip("复制系统反馈信息，方便用户发给开发者排查问题")
        feedback_btn.clicked.connect(self.copy_feedback_report)
        contact_btn = QPushButton("打赏与联系作者")
        contact_btn.clicked.connect(self.show_contact_author_dialog)
        header_layout.addWidget(feedback_btn)
        header_layout.addWidget(contact_btn)
        content_layout.addLayout(header_layout)

        self.module_stack.setMinimumHeight(0)
        content_layout.addWidget(self.module_stack, stretch=1)
        self.log_console = QTextEdit(); self.log_console.setObjectName("logConsole"); self.log_console.setReadOnly(True)
        self.log_console.setMinimumHeight(68)
        self.log_console.setMaximumHeight(110)
        content_layout.addWidget(QLabel("执行日志:"))
        content_layout.addWidget(self.log_console, stretch=0)

        root_layout.addWidget(sidebar)
        root_layout.addWidget(content, stretch=1)
        self.setCentralWidget(central)
        self.apply_modern_theme()

    def detect_system_gpu(self):
        self.apply_best_performance_profile(silent=False)
        
    def open_output_folder(self):
        try: os.startfile(os.path.abspath(self.tab_folders['保存视频']))
        except Exception: pass
        
    def stop_and_clean(self):
        if hasattr(self, 'engine_thread') and self.engine_thread.isRunning(): 
            self.engine_thread.stop() 
            os.system("taskkill /F /IM ffmpeg.exe /T >nul 2>&1")
            os.system("taskkill /F /IM whisper.exe /T >nul 2>&1")
            self.engine_thread.terminate() 
            self.log_console.append("\n[任务控制] ❌ 已强制终止任务并清除了底层残留进程！\n")
            self.start_btn.setEnabled(True); self.start_btn.setText("开始执行任务")
        self.clear_cache()
        
    def clear_cache(self, silent=False):
        try:
            p = os.path.abspath(self.cache_dir)
            if os.path.exists(p):
                for f in os.listdir(p):
                    if f.startswith("preview_bg_"): continue
                    fp = os.path.join(p, f)
                    if os.path.isfile(fp): os.remove(fp)
            if not silent: self.log_console.append(f"[系统缓存] ♻️ 已手动释放。")
        except Exception: pass
        
    def auto_refresh_all(self):
        for t in self.tab_folders.keys(): self.refresh_directory(t, silent=True)
        
    def toggle_select_all(self, tab_name, state):
        table = self.tables[tab_name]; is_checked = (state == Qt.Checked)
        for row in range(table.rowCount()):
            chk_widget = table.cellWidget(row, 0)
            if chk_widget: chk_widget.layout().itemAt(0).widget().setChecked(is_checked)
            
    def update_log(self, text):
        line = str(text)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.feedback_log_history.append(f"{stamp}  {line}")
        self.feedback_log_history = self.feedback_log_history[-500:]
        self.log_console.append(line)

    def handle_uncaught_exception(self, exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        self.feedback_log_history.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [未捕获异常]\n{text}")
        try:
            self.write_feedback_report(extra_error=text)
        except Exception:
            pass
        if self._original_excepthook:
            self._original_excepthook(exc_type, exc_value, exc_tb)

    def current_feedback_context(self):
        module = ""
        sub = ""
        try:
            module_index = self.module_stack.currentIndex()
            module = self.nav_names[module_index] if 0 <= module_index < len(self.nav_names) else ""
            if module == "批量混剪" and hasattr(self, "stacked_widget"):
                sub_index = self.stacked_widget.currentIndex()
                sub = self.tab_names[sub_index] if 0 <= sub_index < len(self.tab_names) else ""
            elif module == "音视频处理" and hasattr(self, "av_stack"):
                sub_index = self.av_stack.currentIndex()
                btn = self.av_btn_group.button(sub_index)
                sub = btn.text() if btn else ""
        except Exception:
            pass
        return module, sub

    def widget_log_snapshot(self):
        parts = []
        try:
            current = self.module_stack.currentWidget()
            if current:
                for edit in current.findChildren(QTextEdit):
                    text = edit.toPlainText().strip()
                    if text:
                        name = edit.objectName() or edit.__class__.__name__
                        parts.append(f"[{name}]\n{text[-4000:]}")
        except Exception:
            pass
        return "\n\n".join(parts)

    def write_feedback_report(self, extra_error=""):
        os.makedirs(logs_dir(), exist_ok=True)
        module, sub = self.current_feedback_context()
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), "ffmpeg.exe"))
        ffprobe = os.path.abspath(os.path.join(tools_dir(), "ffprobe.exe"))
        sensevoice = os.path.abspath(os.path.join(models_dir(), "SenseVoiceSmall"))
        voxcpm = os.path.abspath(os.path.join(models_dir(), "VoxCPM2"))
        comfy = os.path.abspath(runtime_path("engines", "ComfyUI", "ComfyUI"))
        lines = [
            "【牛爷爷电商视频工具箱 - 开发者反馈日志】",
            f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"软件版本：牛爷爷电商视频工具箱 V6.3",
            f"当前功能区：{module or '未知'}",
            f"当前子功能：{sub or '无'}",
            "",
            "【系统环境】",
            f"系统：{platform.platform()}",
            f"Python：{sys.version.replace(os.linesep, ' ')}",
            f"工作目录：{os.getcwd()}",
            f"ffmpeg：{'存在' if os.path.exists(ffmpeg) else '缺失'} - {ffmpeg}",
            f"ffprobe：{'存在' if os.path.exists(ffprobe) else '缺失'} - {ffprobe}",
            f"SenseVoiceSmall：{'存在' if os.path.exists(sensevoice) else '缺失'} - {sensevoice}",
            f"VoxCPM2：{'存在' if os.path.exists(voxcpm) else '缺失'} - {voxcpm}",
            f"ComfyUI 内置引擎：{'存在' if os.path.exists(comfy) else '缺失'} - {comfy}",
            "",
            "【最近全局运行日志】",
            "\n".join(self.feedback_log_history[-220:]) or "暂无全局日志",
        ]
        widget_logs = self.widget_log_snapshot()
        if widget_logs:
            lines.extend(["", "【当前页面日志】", widget_logs])
        if extra_error:
            lines.extend(["", "【异常堆栈】", extra_error])
        report = "\n".join(lines)
        path = os.path.abspath(os.path.join(logs_dir(), f"developer_feedback_{time.strftime('%Y%m%d_%H%M%S')}.txt"))
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        self.last_feedback_report_path = path
        return report, path

    def copy_feedback_report(self):
        try:
            report, path = self.write_feedback_report()
            QApplication.clipboard().setText(report)
            self.update_log(f"[开发者反馈] 已复制反馈日志，并保存到: {path}")
            QMessageBox.information(self, "反馈日志已复制", f"系统反馈已复制到剪贴板。\n日志文件：{path}\n\n用户直接发给开发者即可。")
        except Exception as exc:
            QMessageBox.warning(self, "反馈日志生成失败", str(exc))

    def pixmap_from_embedded_b64(self, data):
        pix = QPixmap()
        pix.loadFromData(base64.b64decode(data))
        return pix

    def show_contact_author_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("打赏与联系作者")
        dialog.resize(920, 680)
        layout = QVBoxLayout(dialog)
        tip = QLabel("感谢支持牛爷爷电商视频工具箱。左侧为打赏码，右侧为作者微信。")
        tip.setObjectName("appSubtitle")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        image_row = QHBoxLayout()
        for title, data in (("推荐使用微信支付", PAY_IMAGE_B64), ("联系作者微信", CONTACT_IMAGE_B64)):
            card = QFrame()
            card.setObjectName("card")
            card_layout = QVBoxLayout(card)
            label = QLabel(title)
            label.setObjectName("appTitle")
            label.setAlignment(Qt.AlignCenter)
            img_label = QLabel()
            img_label.setAlignment(Qt.AlignCenter)
            pix = self.pixmap_from_embedded_b64(data)
            img_label.setPixmap(pix.scaled(390, 520, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            card_layout.addWidget(label)
            card_layout.addWidget(img_label, 1)
            image_row.addWidget(card)
        layout.addLayout(image_row)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec_()
        
    def on_task_done(self): self.refresh_directory('保存视频', silent=True)
    
    def on_synthesis_finished(self):
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始执行任务")
        if hasattr(self, 'preview_sample_btn'):
            self.preview_sample_btn.setEnabled(True)
        self.refresh_directory('保存视频', silent=True)

    def get_selected_mixer_assets(self, tab):
        res = []
        tbl = self.tables.get(tab)
        if not tbl:
            return res
        for r in range(tbl.rowCount()):
            chk_w = tbl.cellWidget(r, 0)
            if chk_w and chk_w.layout().itemAt(0).widget().isChecked():
                item = tbl.item(r, 2)
                if not item:
                    continue
                if tab in ['音频素材']:
                    res.append({'path': item.text(), 'row': r})
                else:
                    res.append({'path': item.text()})
        return res

    def _mixer_output_dir(self):
        base_dir = os.path.abspath(self.tab_folders['保存视频'])
        if hasattr(self, 'output_date_folder_chk') and self.output_date_folder_chk.isChecked():
            base_dir = os.path.join(base_dir, time.strftime("%Y-%m-%d"))
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    def apply_mix_workflow_preset(self):
        name = self.mix_preset_combo.currentText() if hasattr(self, 'mix_preset_combo') else ""
        presets = {
            "抖音竖屏带字幕": {
                "min_clip": 2.0, "max_clip": 3.5, "main_vol": 2.0, "bgm_vol": 0.18,
                "trans": "🎲 随机特效 (防限流神器)", "trans_dur": 0.45,
                "res": "1080x1920 (竖屏)", "anti": True, "subtitle": True, "loops": 1,
            },
            "快手轻量差异化": {
                "min_clip": 1.8, "max_clip": 3.0, "main_vol": 2.0, "bgm_vol": 0.12,
                "trans": "叠化溶解", "trans_dur": 0.35,
                "res": "1080x1920 (竖屏)", "anti": True, "subtitle": False, "loops": 1,
            },
            "低配电脑稳定": {
                "min_clip": 2.5, "max_clip": 4.0, "main_vol": 1.8, "bgm_vol": 0.15,
                "trans": "不使用转场", "trans_dur": 0.3,
                "res": "1080x1920 (竖屏)", "anti": False, "subtitle": False, "loops": 1,
                "gpu": "CPU 软解", "threads": 1,
            },
            "高性能批量生产": {
                "min_clip": 1.5, "max_clip": 3.0, "main_vol": 2.0, "bgm_vol": 0.18,
                "trans": "🎲 随机特效 (防限流神器)", "trans_dur": 0.45,
                "res": "1080x1920 (竖屏)", "anti": True, "subtitle": True, "loops": 3,
                "threads": 3,
            },
            "直播切片样片": {
                "min_clip": 4.0, "max_clip": 7.0, "main_vol": 2.0, "bgm_vol": 0.08,
                "trans": "不使用转场", "trans_dur": 0.3,
                "res": "1080x1920 (竖屏)", "anti": True, "subtitle": True, "loops": 1,
            },
        }
        preset = presets.get(name)
        if not preset:
            return
        self.min_clip_spin.setValue(preset["min_clip"])
        self.max_clip_spin.setValue(preset["max_clip"])
        self.main_vol_spin.setValue(preset["main_vol"])
        self.bgm_vol_spin.setValue(preset["bgm_vol"])
        self.trans_combo.setCurrentText(preset["trans"])
        self.trans_duration.setValue(preset["trans_dur"])
        self.res_combo.setCurrentText(preset["res"])
        self.anti_dedup_chk.setChecked(preset["anti"])
        self.sub_enable_chk.setChecked(preset["subtitle"])
        self.loop_spin.setValue(preset["loops"])
        if "gpu" in preset:
            self.gpu_combo.setCurrentText(preset["gpu"])
        if "threads" in preset:
            self.thread_spin.setValue(preset["threads"])
        self.update_log(f"[批量混剪] 已应用生产预设：{name}")
        self.run_mixer_preflight(show_dialog=False)

    def _probe_media_duration_for_check(self, path):
        ffprobe = os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe'))
        if not os.path.exists(ffprobe) or not os.path.exists(path):
            return 0.0
        try:
            res = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self.get_si(),
                timeout=8,
            )
            return max(0.0, float((res.stdout or "0").strip()))
        except Exception:
            return 0.0

    def run_mixer_preflight(self, show_dialog=True):
        selected_audios = self.get_selected_mixer_assets('音频素材')
        selected_vids = self.get_selected_mixer_assets('视频素材')
        selected_covers = self.get_selected_mixer_assets('封面图片')
        selected_bgms = self.get_selected_mixer_assets('背景音乐')
        selected_seq1 = self.get_selected_mixer_assets('开头素材(段1)')
        selected_seq2 = self.get_selected_mixer_assets('承接素材(段2)')

        errors = []
        warnings = []
        ok = []
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        ffprobe = os.path.abspath(os.path.join(tools_dir(), 'ffprobe.exe'))
        if os.path.exists(ffmpeg) and os.path.exists(ffprobe):
            ok.append("ffmpeg / ffprobe 已就绪")
        else:
            errors.append("缺少 ffmpeg.exe 或 ffprobe.exe，请检查网盘资源包/tools")

        if not selected_audios:
            errors.append("未勾选音频素材")
        else:
            ok.append(f"已选择音频 {len(selected_audios)} 条")
        if not selected_vids:
            errors.append("未勾选视频素材")
        else:
            ok.append(f"已选择视频素材 {len(selected_vids)} 条")
        if selected_covers:
            ok.append(f"封面图片 {len(selected_covers)} 张")
        if selected_bgms:
            ok.append(f"背景音乐 {len(selected_bgms)} 条")
        if selected_seq1 or selected_seq2:
            ok.append(f"固定开头/承接素材：{len(selected_seq1)} / {len(selected_seq2)}")

        if self.min_clip_spin.value() > self.max_clip_spin.value():
            errors.append("最小裁剪秒数不能大于最大裁剪秒数")
        if self.trans_combo.currentText() != "不使用转场" and self.trans_duration.value() >= self.min_clip_spin.value():
            warnings.append("转场时长接近或超过最短片段，可能导致转场失败，建议降低转场时长")

        bad_files = []
        short_videos = []
        for item in selected_vids[:12]:
            path = item.get('path', '')
            if not os.path.exists(path):
                bad_files.append(os.path.basename(path))
                continue
            duration = self._probe_media_duration_for_check(path)
            if duration <= 0:
                bad_files.append(os.path.basename(path))
            elif duration < max(1.2, self.min_clip_spin.value() + 0.8):
                short_videos.append(f"{os.path.basename(path)}({duration:.1f}s)")
        if bad_files:
            errors.append("部分视频无法读取：" + "、".join(bad_files[:5]))
        if short_videos:
            warnings.append("部分视频过短，可能被跳过：" + "、".join(short_videos[:5]))

        bad_audios = []
        total_audio_seconds = 0.0
        for item in selected_audios[:20]:
            path = item.get('path', '')
            duration = self._probe_media_duration_for_check(path)
            if duration <= 0:
                bad_audios.append(os.path.basename(path))
            total_audio_seconds += duration
        if bad_audios:
            errors.append("部分音频无法读取：" + "、".join(bad_audios[:5]))

        if self.sub_enable_chk.isChecked():
            sensevoice_dir = os.path.abspath(os.path.join(models_dir(), 'SenseVoiceSmall'))
            whisper_exists = any(name.endswith('.bin') for name in os.listdir(tools_dir())) if os.path.isdir(tools_dir()) else False
            if os.path.exists(os.path.join(sensevoice_dir, 'model.pt')):
                ok.append("智能字幕：SenseVoiceSmall 可用")
            elif whisper_exists:
                warnings.append("智能字幕将回退 Whisper，速度可能较慢")
            else:
                errors.append("已开启智能字幕，但未找到 SenseVoiceSmall 或 Whisper 模型")

        try:
            out_dir = self._mixer_output_dir()
            test_path = os.path.join(out_dir, ".write_test")
            with open(test_path, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test_path)
            ok.append(f"输出目录可写：{out_dir}")
        except Exception as exc:
            errors.append(f"输出目录不可写：{exc}")

        task_count = len(selected_audios) * max(1, self.loop_spin.value())
        estimate = ""
        if total_audio_seconds > 0 and task_count > 0:
            estimate = f"预计生成 {task_count} 条，音频总时长约 {total_audio_seconds * max(1, self.loop_spin.value()) / 60:.1f} 分钟"
        else:
            estimate = f"预计生成 {task_count} 条"
        summary = f"任务概览：{estimate}；视频 {len(selected_vids)}，封面 {len(selected_covers)}，BGM {len(selected_bgms)}；输出 {self._mixer_output_dir()}"
        if hasattr(self, 'mixer_summary_label'):
            self.mixer_summary_label.setText(summary)

        lines = ["【开始前检查】", summary, ""]
        if ok:
            lines.extend(["通过："] + [f"  √ {item}" for item in ok])
        if warnings:
            lines.extend(["", "提醒："] + [f"  ! {item}" for item in warnings])
        if errors:
            lines.extend(["", "需要处理："] + [f"  × {item}" for item in errors])
        report = "\n".join(lines)
        self.update_log(report.replace("\n", " | "))
        if show_dialog:
            if errors:
                QMessageBox.warning(self, "开始前检查未通过", report)
            elif warnings:
                QMessageBox.information(self, "开始前检查有提醒", report)
            else:
                QMessageBox.information(self, "开始前检查通过", report)
        return not errors, report

    def start_preview_synthesis(self):
        self.start_synthesis(preview_only=True)

    def start_synthesis(self, preview_only=False):
        selected_audios = self.get_selected_mixer_assets('音频素材')
        selected_vids = self.get_selected_mixer_assets('视频素材')
        if not selected_audios or not selected_vids: QMessageBox.warning(self, "拦截", "❌ 音频素材 或 视频素材 未勾选！"); return
        ok, report = self.run_mixer_preflight(show_dialog=False)
        if not ok:
            QMessageBox.warning(self, "开始前检查未通过", report)
            return
        if self.auto_perf_chk.isChecked():
            self.apply_best_performance_profile(silent=True)
        if preview_only:
            selected_audios = selected_audios[:1]
        for a in selected_audios: self.tables['音频素材'].setItem(a['row'], 3, QTableWidgetItem("排队中..."))
        self.save_configuration(silent=True)

        task_data = {
            'audios': selected_audios, 
            'videos': selected_vids, 'covers': self.get_selected_mixer_assets('封面图片'), 'bgms': self.get_selected_mixer_assets('背景音乐'),
            'seq1_videos': self.get_selected_mixer_assets('开头素材(段1)'), 'seq2_videos': self.get_selected_mixer_assets('承接素材(段2)'), 
            'watermarks': self.watermarks_data, 'duration_cache': self.duration_cache,
            'min_clip': self.min_clip_spin.value(), 'max_clip': self.max_clip_spin.value(),
            'trans_type': self.trans_combo.currentText(), 'trans_dur': self.trans_duration.value(),
            'main_vol': self.main_vol_spin.value(), 'bgm_vol': self.bgm_vol_spin.value(),
            'resolution': self.res_combo.currentText(), 'gpu_mode': self.gpu_combo.currentText(), 
            'threads': 1 if preview_only else self.thread_spin.value(), 'loop_count': 1 if preview_only else self.loop_spin.value(),
            'cache_dir': os.path.abspath(self.cache_dir), 'output_dir': self._mixer_output_dir(),
            'anti_dedup': self.anti_dedup_chk.isChecked(), 
            'preview_only': preview_only,
            'sys_fonts': self.sys_fonts,
            'sub_config': {
                'enable': self.sub_enable_chk.isChecked(),
                'font': self.sub_font.currentText(),
                'size': self.sub_size.value(),
                'color': self.get_hex_from_combo(self.sub_color),
                'border_w': self.sub_border_w.value(),
                'border_color': self.get_hex_from_combo(self.sub_border_color),
                'anim': self.sub_anim.currentText(),
                'margin_v': self.sub_margin_v.value(),
                'max_len': self.sub_max_len.value()
            },
            'cover_config': {
                'text': self.cover_text_input.text().strip(),
                'font': self.cover_font.currentText(),
                'style': self.cover_style.currentText()
            }
        }

        self.start_btn.setEnabled(False)
        self.start_btn.setText("正在生成预览样片..." if preview_only else "合成阵列执行中...")
        if hasattr(self, 'preview_sample_btn'):
            self.preview_sample_btn.setEnabled(False)
        self.engine_thread = SynthesisEngine(task_data)
        self.engine_thread.log_signal.connect(self.update_log)
        self.engine_thread.task_done_signal.connect(self.on_task_done) 
        self.engine_thread.status_update_signal.connect(self.update_table_status) 
        self.engine_thread.finished_signal.connect(self.on_synthesis_finished)
        self.engine_thread.start()

# =========================================================================
# 🌟 专属定制：红色光晕渐显启动屏 (Splash Screen)
# =========================================================================
class SplashScreen(QWidget):
    def __init__(self, image_path):
        super().__init__()
        # 1. 强行扒掉 Windows 默认边框，并把背景变透明
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QVBoxLayout()
        self.setLayout(layout)

        # 2. 加载你的专属图片
        self.image_label = QLabel()
        pixmap = QPixmap(image_path)
        # 如果图太大，按比例限制在 600 像素内，保持平滑
        self.image_label.setPixmap(pixmap.scaled(600, 600, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(self.image_label)

        # 3. 🔥 核心特效：极其炫酷的红色半透明光晕
        glow_effect = QGraphicsDropShadowEffect(self)
        glow_effect.setOffset(0, 0) # 居中发光
        glow_effect.setBlurRadius(60) # 光晕扩散范围（越大越朦胧）
        glow_effect.setColor(QColor(255, 0, 0, 180)) # RGB纯红，透明度180
        self.image_label.setGraphicsEffect(glow_effect)

        # 4. 🎬 影视级渐显入场动画 (Fade-in)
        self.setWindowOpacity(0.0) # 初始透明
        self.animation = QPropertyAnimation(self, b"windowOpacity")
        self.animation.setDuration(1500) # 渐显持续 1.5 秒
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.InOutQuad)
        self.animation.start()

    def close_splash(self):
        # 🎬 影视级渐隐退场动画 (Fade-out)
        self.fade_out = QPropertyAnimation(self, b"windowOpacity")
        self.fade_out.setDuration(800) # 0.8 秒平滑消失
        self.fade_out.setStartValue(1.0)
        self.fade_out.setEndValue(0.0)
        self.fade_out.finished.connect(self.close) # 动画播完自动销毁
        self.fade_out.start()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    # 🚀 新增：智能寻路机制，专治 PyInstaller 封装
    def resource_path(relative_path):
        """ 获取资源的绝对路径，兼容开发环境与 PyInstaller 打包环境 """
        try:
            # PyInstaller 打包后，会将资源解压到一个名为 _MEIPASS 的临时目录
            base_path = sys._MEIPASS
        except Exception:
            # 如果没有 _MEIPASS，说明是在普通 Python 开发环境下运行
            base_path = os.path.abspath(".")
        return os.path.join(base_path, relative_path)

    # 使用智能寻路获取封印后的图片路径
    splash_image = resource_path(os.path.join("软件核心", "assets", "splash.png"))
    if not os.path.exists(splash_image):
        splash_image = resource_path("splash.png")
    if os.path.exists(splash_image):
        # 1. 优先展示光晕启动屏
        splash = SplashScreen(splash_image)
        splash.show()
        
        # 2. 在后台静默加载沉重的牛爷爷混剪主界面
        ex = VideoMixerApp()
        
        # 3. 设定时间轴：3.5秒后启动屏开始渐隐，4秒后主界面华丽登场！
        QTimer.singleShot(3500, splash.close_splash)
        QTimer.singleShot(4000, ex.show)
    else:
        # 兜底机制：万一没放图，直接秒开主界面，绝不卡死
        ex = VideoMixerApp()
        ex.show()

    sys.exit(app.exec_())
