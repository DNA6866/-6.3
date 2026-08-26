import os
import subprocess
import random
import shutil
import time
import re  
import multiprocessing 
import threading
import json
import tempfile
import hashlib
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QGuiApplication, QRawFont
from services.asr_service import (
    SenseVoiceIsolatedSession,
    clean_asr_text,
    layout_subtitle_text,
    sensevoice_model_ready,
    transcribe_with_whisper_word_timestamps,
    write_srt_from_text,
)
from utils import (
    COVER_RANDOM_STATIC_STYLES,
    path_to_ffmpeg,
    detect_performance_profile,
    ANIMATIONS,
    SUBTITLE_ANIMATIONS,
    TRANS_MAP,
    _hidden_startupinfo,
)
from services.render_task import (
    AudioJobContext,
    EncoderProfile,
    RenderTaskData,
    parse_target_resolution,
)
from services.asset_usage_service import AssetUsageScheduler
from services.platform_service import is_remote_path
from services.cache_service import touch_cache_entry
from services.lut_service import resolve_lut_selection
from services.smart_sfx_service import (
    build_sfx_mix_filters,
    plan_smart_sfx,
    smart_sfx_strength_label,
)
from services.render_planner import (
    FastTransitionSelector,
    MediaSegmentPlanner,
    RenderFilterBuilder,
    RenderSampler,
    build_xfade_plan,
)
from services.render_checkpoint_service import (
    RenderCheckpointStore,
    ensure_resume_source_ids,
    resume_job_key,
)
from paths import APP_ROOT, config_dir, logs_dir, models_dir, resource_root_candidates, tools_dir, workspace_path

DRAWTEXT_FONT_PROBE_VERSION = 3


class SynthesisEngine(QThread):
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal()
    task_done_signal = pyqtSignal() 
    status_update_signal = pyqtSignal(int, str, str) 

    def __init__(self, task_data):
        super().__init__()
        raw_task_data = task_data if isinstance(task_data, dict) else {}
        ensure_resume_source_ids(raw_task_data)
        self.task = RenderTaskData.from_dict(task_data)
        self.task_config = self.task.config
        self.tools_dir = tools_dir()
        self.ffmpeg_exe = os.path.join(self.tools_dir, 'ffmpeg.exe')
        self.ffprobe_exe = os.path.join(self.tools_dir, 'ffprobe.exe')
        self.sensevoice_model_dir = os.path.join(models_dir(), 'SenseVoiceSmall')
        self.is_running = True
        self._sensevoice_session = None
        self._sensevoice_session_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._failure_log_lock = threading.Lock()
        self._active_processes = {}
        self._silence_points_cache = {}
        self._video_color_cache = {}
        self._video_shape_cache = {}
        self._hdr_sources_logged = set()
        self._hdr_cache_map = {}
        self._hdr_cache_hits_logged = set()
        self._hdr_cache_locks = {}
        self._hdr_cache_locks_guard = threading.Lock()
        self._standard_cache_map = {}
        self._standard_cache_hits_logged = set()
        self._standard_cache_paths = set()
        self._standard_cache_locks = {}
        self._standard_cache_locks_guard = threading.Lock()
        self._standard_cache_requests = {}
        self._asset_usage_scheduler = AssetUsageScheduler()
        self._resume_checkpoint_path = str(
            raw_task_data.get("resume_checkpoint_path") or ""
        )
        self._resume_task_id = str(raw_task_data.get("resume_task_id") or "")
        self._resume_store = (
            RenderCheckpointStore(
                self._resume_checkpoint_path,
                task_id=self._resume_task_id,
            )
            if self._resume_checkpoint_path else None
        )
        
        total_cores = multiprocessing.cpu_count()
        self.optimal_threads = str(max(4, total_cores - 2)) 

    def _refresh_task_config(self):
        self.task_config = self.task.config
        return self.task_config

    def stop(self):
        self.is_running = False
        self._terminate_active_processes()

    def _get_sensevoice_session(self):
        with self._sensevoice_session_lock:
            if self._sensevoice_session and self._sensevoice_session.is_alive():
                return self._sensevoice_session
            if self._sensevoice_session:
                self._sensevoice_session.close(force=True)
            self._sensevoice_session = SenseVoiceIsolatedSession(self.sensevoice_model_dir, timeout_sec=300)
            self.log_signal.emit(f"✅ SenseVoice 隔离识别服务已启动，设备：{self._sensevoice_session.device}")
            return self._sensevoice_session

    def _close_sensevoice_session(self):
        with self._sensevoice_session_lock:
            if self._sensevoice_session:
                self._sensevoice_session.close()
                self._sensevoice_session = None

    def _get_whisper_exe(self, gpu_mode):
        if "NVENC" in gpu_mode:
            target = os.path.join(self.tools_dir, "nv", "whisper.exe")
        elif "AMF" in gpu_mode:
            target = os.path.join(self.tools_dir, "amd", "whisper.exe")
        else:
            target = os.path.join(self.tools_dir, "cpu", "whisper.exe")
            
        if os.path.exists(target):
            return target
        
        for fallback in ["whisper_cpu.exe", "whisper_nv.exe", "whisper_amd.exe", "whisper.exe"]:
            fallback_path = os.path.join(self.tools_dir, fallback)
            if os.path.exists(fallback_path):
                return fallback_path
            
        return None

    def check_env(self):
        if not os.path.exists(self.ffmpeg_exe) or not os.path.exists(self.ffprobe_exe):
            ffmpeg_state = "存在" if os.path.isfile(self.ffmpeg_exe) else "缺失"
            ffprobe_state = "存在" if os.path.isfile(self.ffprobe_exe) else "缺失"
            searched = "；".join(resource_root_candidates())
            self.log_signal.emit(f"[环境路径] 软件目录：{APP_ROOT}")
            self.log_signal.emit(f"[环境路径] tools 解析结果：{self.tools_dir}")
            self.log_signal.emit(
                f"[致命错误] FFmpeg 工具不完整：ffmpeg.exe={ffmpeg_state}，ffprobe.exe={ffprobe_state}。"
            )
            self.log_signal.emit(f"[环境路径] 已检查资源包位置：{searched}")
            self.log_signal.emit(
                "[处理建议] 请确认主程序 EXE 与“网盘资源包”处于同一层，且网盘资源包\\tools 中保留两个文件；"
                "不要直接运行从“更新文件.zip”里解压出的主程序。"
            )
            return False

        profile = detect_performance_profile(self.tools_dir)
        selected_gpu = self.task_config.gpu_mode
        if "NVENC" in selected_gpu and profile.get('level') != 'nvenc':
            self.log_signal.emit(f"[性能兜底] 当前环境无法稳定启用 NVENC，已切换为 {profile['gpu_mode']}。")
            self.task.with_runtime_profile(profile['gpu_mode'], profile['threads'])
            self.task_config = self.task.config
        elif "AMF" in selected_gpu and profile.get('level') != 'amf':
            self.log_signal.emit(f"[性能兜底] 当前环境无法稳定启用 AMF，已切换为 {profile['gpu_mode']}。")
            self.task.with_runtime_profile(profile['gpu_mode'], profile['threads'])
            self.task_config = self.task.config
            
        if self.task_config.sub_config.get('enable'):
            if sensevoice_model_ready(self.sensevoice_model_dir):
                return True
            models = [f for f in os.listdir(self.tools_dir) if f.endswith('.bin')]
            whisper_exe = self._get_whisper_exe(self.task_config.gpu_mode)
            if not whisper_exe:
                self.log_signal.emit("[AI 警告] ⚠️ 未找到 SenseVoiceSmall 或 whisper.exe，无法生成智能字幕。")
                return False
            if not models:
                self.log_signal.emit("[AI 警告] ⚠️ 未找到 SenseVoiceSmall 或 Whisper .bin 模型，无法生成智能字幕。")
                return False
                
        self._refresh_task_config()
        return True

    @staticmethod
    def _returncode_u32(returncode):
        try:
            return int(returncode) & 0xFFFFFFFF
        except Exception:
            return 0

    def _is_native_ffmpeg_crash(self, returncode):
        return self._returncode_u32(returncode) in {
            0xC0000005,  # Access violation
            0xC0000017,  # Out of memory
            0xC0000409,  # Stack buffer overrun
        }

    @staticmethod
    def _is_encoder_unavailable(returncode, out_text):
        """检测硬件编码器（NVENC/AMF）是否因环境不可用而失败。
        常见：远程桌面/无独显/驱动过旧 → 'Error while opening encoder'、
        'Could not open encoder'、'No NVENC capable devices' 等。"""
        lowered = (out_text or "").lower()
        return any(
            marker in lowered
            for marker in (
                "error while opening encoder",
                "could not open encoder",
                "no nvenc capable devices",
                "nvenc",
                "cannot load nvcuda",
                "amf_createencoder",
            )
        )

    @staticmethod
    def _cpu_fallback_command(cmd):
        fallback = list(cmd)
        changed = False
        for index, part in enumerate(fallback):
            value = str(part)
            if value in ("h264_nvenc", "h264_amf"):
                fallback[index] = "libx264"
                changed = True
            elif value == "nv12":
                fallback[index] = "yuv420p"
        return fallback if changed else None

    @staticmethod
    def _ffmpeg_duration_hint(cmd):
        durations = []
        for index, part in enumerate(cmd[:-1]):
            if str(part) != "-t":
                continue
            try:
                durations.append(float(cmd[index + 1]))
            except Exception:
                continue
        return max(durations) if durations else 0.0

    @staticmethod
    def _ffmpeg_output_path(cmd, cwd=None):
        if not cmd:
            return ""
        candidate = str(cmd[-1])
        if candidate in ("-", "NUL", "nul") or candidate.startswith("pipe:"):
            return ""
        if not os.path.isabs(candidate) and cwd:
            candidate = os.path.join(cwd, candidate)
        extension = os.path.splitext(candidate)[1].lower()
        return os.path.abspath(candidate) if extension else ""

    def _terminate_process_tree(self, process):
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=self._get_si(),
                    timeout=10,
                    check=False,
                )
            else:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _terminate_active_processes(self):
        with self._process_lock:
            processes = list(self._active_processes.values())
        for process in processes:
            self._terminate_process_tree(process)

    def _run_ffmpeg_once(self, cmd, cwd=None):
        process = None
        duration_hint = self._ffmpeg_duration_hint(cmd)
        output_path = self._ffmpeg_output_path(cmd, cwd)
        max_runtime = max(600.0, min(3600.0, 300.0 + duration_hint * 30.0))
        started_at = time.monotonic()
        last_progress_at = started_at
        last_heartbeat_at = started_at
        last_output_size = -1
        try:
            with tempfile.TemporaryFile() as output_file:
                process = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdout=output_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    startupinfo=self._get_si(),
                )
                with self._process_lock:
                    self._active_processes[process.pid] = process

                while process.poll() is None:
                    if not self.is_running:
                        self._terminate_process_tree(process)
                        return -1, "任务已停止"

                    now = time.monotonic()
                    if output_path and now - started_at >= 30.0:
                        try:
                            current_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
                        except Exception:
                            current_size = 0
                        if current_size != last_output_size:
                            last_output_size = current_size
                            last_progress_at = now
                        elif now - last_progress_at >= 180.0:
                            self._terminate_process_tree(process)
                            return -2, "ffmpeg 输出连续 180 秒无进展，已回收卡死进程"

                    if now - started_at >= 25.0 and now - last_heartbeat_at >= 45.0:
                        size_text = ""
                        if output_path:
                            try:
                                if os.path.exists(output_path):
                                    size_text = f"，当前输出 {os.path.getsize(output_path) / 1024 / 1024:.1f}MB"
                            except Exception:
                                size_text = ""
                        self.log_signal.emit(f"⏳ ffmpeg 长节点仍在运行 {int(now - started_at)} 秒{size_text}，请稍等。")
                        last_heartbeat_at = now

                    if now - started_at >= max_runtime:
                        self._terminate_process_tree(process)
                        return -3, f"ffmpeg 超过稳定运行时限 {int(max_runtime)} 秒，已回收进程"
                    time.sleep(0.25)

                returncode = process.returncode
                output_file.seek(0)
                output = output_file.read().decode("utf-8", errors="ignore")
                return returncode, output
        finally:
            if process is not None:
                with self._process_lock:
                    self._active_processes.pop(process.pid, None)

    def run_ffmpeg(self, cmd, cwd=None):
        try:
            returncode, out_text = self._run_ffmpeg_once(cmd, cwd)
            if returncode != 0:
                if not self.is_running:
                    return False

                fallback_cmd = self._cpu_fallback_command(cmd)
                uses_xfade = any("xfade=" in str(part) for part in cmd)
                no_detail_output = not (out_text or "").strip()
                encoder_unavailable = self._is_encoder_unavailable(returncode, out_text)
                # 硬件编码器打开失败（NVENC/AMF 环境不可用）时，即使不是原生崩溃也强制 CPU 兜底，
                # 避免“任务看似成功、输出目录却为空”。
                if fallback_cmd and (
                    self._is_native_ffmpeg_crash(returncode)
                    or no_detail_output
                    or encoder_unavailable
                ) and not uses_xfade:
                    self.log_signal.emit(
                        "[稳定兜底] ffmpeg 硬件编码节点异常，当前节点自动切换 CPU 重试。"
                    )
                    fallback_code, fallback_output = self._run_ffmpeg_once(fallback_cmd, cwd)
                    if fallback_code == 0:
                        return True
                    returncode = fallback_code
                    out_text = fallback_output

                out_text = out_text or ""
                err_lines = [line.strip() for line in out_text.split('\n') if line.strip() and "Fontconfig" not in line]
                err_msg = " | ".join(err_lines[-8:]) if err_lines else "ffmpeg 未返回详细错误"
                log_path = self._write_ffmpeg_failure_log(cmd, out_text, returncode, cwd)
                self.log_signal.emit(f"⚠️ 节点重试: ffmpeg 返回码 {returncode}；{err_msg}；详情: {log_path}")
                return False
            return True
        except Exception as e:
            self.log_signal.emit(f"❌ 进程异常: {e}")
            return False

    def get_duration(self, file_path):
        cache = self.task.duration_cache
        if file_path in cache:
            return cache[file_path]
        try:
            cmd = [self.ffprobe_exe, '-v', 'error', '-show_entries', 
                   'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self._get_si(),
                timeout=15,
            )
            dur = float(result.stdout.strip())
            cache[file_path] = dur 
            return dur
        except Exception:
            return 0.0

    @staticmethod
    def _parse_media_ratio(value):
        try:
            numerator, denominator = str(value or "").split(":", 1)
            return float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0

    def _validate_platform_output(self, file_path, target_w, target_h):
        command = [
            self.ffprobe_exe,
            '-v', 'error',
            '-show_streams',
            '-show_format',
            '-of', 'json',
            file_path,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                startupinfo=self._get_si(),
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return ["ffprobe 平台合规检查超时"], {}
        except OSError as exc:
            return [f"ffprobe 平台合规检查无法启动：{exc}"], {}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return [f"ffprobe 无法读取成品：{detail or result.returncode}"], {}
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return ["ffprobe 返回了无效 JSON"], {}

        streams = payload.get('streams') or []
        video = next(
            (item for item in streams if item.get('codec_type') == 'video'),
            {},
        )
        audio = next(
            (item for item in streams if item.get('codec_type') == 'audio'),
            {},
        )
        format_info = payload.get('format') or {}
        issues = []
        width = int(video.get('width') or 0)
        height = int(video.get('height') or 0)
        sar_text = str(video.get('sample_aspect_ratio') or "")
        sar_value = self._parse_media_ratio(sar_text)
        expected_ratio = float(target_w) / max(1.0, float(target_h))
        display_ratio = width * sar_value / height if height and sar_value else 0.0

        if width != int(target_w) or height != int(target_h):
            issues.append(
                f"编码尺寸 {width}x{height}，目标应为 {int(target_w)}x{int(target_h)}"
            )
        if abs(sar_value - 1.0) > 0.000001:
            issues.append(f"像素宽高比 SAR={sar_text or 'N/A'}，目标应为 1:1")
        if not display_ratio or abs(display_ratio - expected_ratio) > 0.000001:
            issues.append(
                f"显示宽高比异常，实际约 {display_ratio:.8f}，"
                f"目标为 {expected_ratio:.8f}"
            )
        if str(video.get('codec_name') or "").lower() != 'h264':
            issues.append(f"视频编码为 {video.get('codec_name') or '未知'}，目标应为 H.264")
        if str(video.get('pix_fmt') or "").lower() not in {'yuv420p', 'nv12'}:
            issues.append(f"像素格式为 {video.get('pix_fmt') or '未知'}，平台兼容性不足")
        if not audio:
            issues.append("成品缺少音频流")
        elif str(audio.get('codec_name') or "").lower() != 'aac':
            issues.append(f"音频编码为 {audio.get('codec_name') or '未知'}，目标应为 AAC")

        rotation = 0
        try:
            rotation = int(float((video.get('tags') or {}).get('rotate') or 0))
        except (TypeError, ValueError):
            rotation = 0
        for side_data in video.get('side_data_list') or []:
            if 'rotation' not in side_data:
                continue
            try:
                rotation = int(float(side_data.get('rotation') or 0))
            except (TypeError, ValueError):
                pass
        if rotation % 360:
            issues.append(f"成品仍带有 {rotation}° 旋转元数据")

        duration = float(format_info.get('duration') or 0.0)
        if duration <= 0.2:
            issues.append(f"成品时长异常：{duration:.3f} 秒")
        summary = {
            'width': width,
            'height': height,
            'sar': sar_text,
            'display_ratio': display_ratio,
            'duration': duration,
            'video_codec': str(video.get('codec_name') or ""),
            'audio_codec': str(audio.get('codec_name') or ""),
            'pix_fmt': str(video.get('pix_fmt') or ""),
            'rotation': rotation,
        }
        return issues, summary

    @staticmethod
    def _sdr_color_filter():
        return (
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        )

    @staticmethod
    def _hdr_to_sdr_filter():
        return (
            "zscale=t=linear:npl=100,format=gbrpf32le,"
            "tonemap=tonemap=hable:desat=0:peak=1000,"
            "zscale=p=bt709:t=bt709:m=bt709:r=tv,"
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        )

    def _video_color_info(self, file_path):
        key = os.path.abspath(str(file_path or ""))
        if not key:
            return {}
        if key in self._video_color_cache:
            return self._video_color_cache[key]
        info = {}
        try:
            cmd = [
                self.ffprobe_exe, '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=pix_fmt,color_space,color_transfer,color_primaries,color_range',
                '-of', 'default=noprint_wrappers=1',
                key,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self._get_si(),
                timeout=8,
            )
            for line in (result.stdout or "").splitlines():
                if "=" not in line:
                    continue
                name, value = line.split("=", 1)
                info[name.strip()] = value.strip().lower()
        except Exception:
            info = {}
        self._video_color_cache[key] = info
        return info

    def _is_hdr_video(self, file_path):
        info = self._video_color_info(file_path)
        transfer = info.get('color_transfer', '')
        primaries = info.get('color_primaries', '')
        colorspace = info.get('color_space', '')
        return (
            transfer in {'smpte2084', 'arib-std-b67'}
            or primaries == 'bt2020'
            or colorspace in {'bt2020nc', 'bt2020c'}
        )

    def _source_color_filter(self, source_path, hdr_tonemap_filter, color_normalize_filter):
        if source_path and self._is_hdr_video(source_path):
            key = os.path.abspath(str(source_path))
            if key not in self._hdr_sources_logged:
                self._hdr_sources_logged.add(key)
                info = self._video_color_info(source_path)
                detail = ", ".join(
                    f"{name}={value}"
                    for name, value in info.items()
                    if value and value != "unknown"
                ) or "metadata unknown"
                self.log_signal.emit(
                    f"[色彩保护] 检测到 HDR/BT.2020 素材，已自动转 SDR："
                    f"{os.path.basename(source_path)} ({detail})"
                )
            return hdr_tonemap_filter
        return color_normalize_filter

    def _hdr_cache_lock(self, cache_key):
        with self._hdr_cache_locks_guard:
            lock = self._hdr_cache_locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                self._hdr_cache_locks[cache_key] = lock
            return lock

    def _hdr_cache_key(self, source_path):
        abs_path = os.path.abspath(str(source_path or ""))
        try:
            stat = os.stat(abs_path)
            stamp = f"{stat.st_size}:{getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1000000000))}"
        except OSError:
            stamp = "missing"
        raw = f"{abs_path.lower()}|{stamp}"
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]

    def _hdr_cache_path(self, source_path, cache_key):
        cache_root = workspace_path("缓存", "HDR标准化素材")
        base = os.path.splitext(os.path.basename(str(source_path or "video")))[0]
        safe_base = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5._-]+", "_", base).strip("._-") or "video"
        if len(safe_base) > 48:
            safe_base = safe_base[:48]
        return os.path.join(cache_root, f"{safe_base}_{cache_key}_SDR.mp4")

    def _valid_hdr_cache(self, source_path, cache_path):
        try:
            if not cache_path or not os.path.exists(cache_path) or os.path.getsize(cache_path) <= 1024:
                return False
            if self._is_hdr_video(cache_path):
                return False
            source_dur = self.get_duration(source_path)
            cache_dur = self.get_duration(cache_path)
            if source_dur > 0.5 and cache_dur > 0:
                return abs(source_dur - cache_dur) <= max(0.7, source_dur * 0.03)
            return True
        except Exception:
            return False

    def _cached_sdr_video(self, source_path):
        source_path = os.path.abspath(str(source_path or ""))
        if not source_path or not os.path.exists(source_path):
            return source_path
        if not self._is_hdr_video(source_path):
            return source_path
        cache_key = self._hdr_cache_key(source_path)
        cached = self._hdr_cache_map.get(cache_key)
        if cached and self._valid_hdr_cache(source_path, cached):
            touch_cache_entry(cached)
            return cached
        cache_path = self._hdr_cache_path(source_path, cache_key)
        lock = self._hdr_cache_lock(cache_key)
        with lock:
            cached = self._hdr_cache_map.get(cache_key)
            if cached and self._valid_hdr_cache(source_path, cached):
                touch_cache_entry(cached)
                return cached
            if self._valid_hdr_cache(source_path, cache_path):
                self._hdr_cache_map[cache_key] = cache_path
                touch_cache_entry(cache_path)
                if cache_key not in self._hdr_cache_hits_logged:
                    self._hdr_cache_hits_logged.add(cache_key)
                    self.log_signal.emit(
                        f"[色彩缓存] 复用 HDR→SDR 缓存：{os.path.basename(source_path)}"
                    )
                return cache_path
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            tmp_path = f"{cache_path}.tmp_{os.getpid()}_{threading.get_ident()}.mp4"
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            info = self._video_color_info(source_path)
            detail = ", ".join(
                f"{name}={value}"
                for name, value in info.items()
                if value and value != "unknown"
            ) or "metadata unknown"
            self.log_signal.emit(
                f"[色彩缓存] 首次标准化 HDR/BT.2020 素材，后续将直接复用："
                f"{os.path.basename(source_path)} ({detail})"
            )
            cmd = [
                self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                '-i', source_path,
                '-map', '0:v:0',
                '-vf', f"{self._hdr_to_sdr_filter()},format=yuv420p",
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
                '-pix_fmt', 'yuv420p',
                '-an',
                '-color_range', 'tv',
                '-color_primaries', 'bt709',
                '-color_trc', 'bt709',
                '-colorspace', 'bt709',
                '-bsf:v', 'h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1',
                '-movflags', '+faststart',
                tmp_path,
            ]
            if not self.run_ffmpeg(cmd, cwd=os.path.dirname(cache_path)):
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                self.log_signal.emit(
                    f"[色彩缓存] ⚠️ HDR 缓存生成失败，当前素材本次仍走实时色彩转换：{os.path.basename(source_path)}"
                )
                return source_path
            try:
                if not self._valid_hdr_cache(source_path, tmp_path):
                    raise RuntimeError("缓存文件校验未通过")
                os.replace(tmp_path, cache_path)
                self._video_color_cache.pop(cache_path, None)
                self.task.duration_cache.pop(cache_path, None)
                self._hdr_cache_map[cache_key] = cache_path
                self.log_signal.emit(
                    f"[色彩缓存] ✅ HDR→SDR 缓存已生成：{os.path.basename(cache_path)}"
                )
                return cache_path
            except Exception as exc:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                self.log_signal.emit(
                    f"[色彩缓存] ⚠️ HDR 缓存校验失败，当前素材本次仍走实时色彩转换："
                    f"{os.path.basename(source_path)}；{exc}"
                )
                return source_path

    def _standard_cache_lock(self, cache_key):
        with self._standard_cache_locks_guard:
            lock = self._standard_cache_locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                self._standard_cache_locks[cache_key] = lock
            return lock

    def _standard_cache_key(self, source_path, target_w, target_h):
        abs_path = os.path.abspath(str(source_path or ""))
        try:
            stat = os.stat(abs_path)
            stamp = f"{stat.st_size}:{getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1000000000))}"
        except OSError:
            stamp = "missing"
        raw = f"std-v2|{abs_path.lower()}|{stamp}|{int(target_w)}x{int(target_h)}"
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]

    def _standard_cache_path(self, source_path, target_w, target_h, cache_key):
        cache_root = workspace_path("缓存", "标准化视频素材", f"{int(target_w)}x{int(target_h)}")
        base = os.path.splitext(os.path.basename(str(source_path or "video")))[0]
        safe_base = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5._-]+", "_", base).strip("._-") or "video"
        if len(safe_base) > 48:
            safe_base = safe_base[:48]
        return os.path.join(cache_root, f"{safe_base}_{cache_key}_STD.mp4")

    def _cache_video_shape(self, file_path):
        key = os.path.abspath(str(file_path or ""))
        try:
            stat = os.stat(key)
            stamp = (
                int(stat.st_size),
                getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1000000000)),
            )
        except OSError:
            stamp = (0, 0)
        cached = self._video_shape_cache.get(key)
        if cached and cached[0] == stamp:
            return dict(cached[1])
        try:
            cmd = [
                self.ffprobe_exe, '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name,width,height,pix_fmt',
                '-of', 'default=noprint_wrappers=1',
                file_path,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self._get_si(),
                timeout=8,
            )
            info = {}
            for line in (result.stdout or "").splitlines():
                if "=" not in line:
                    continue
                name, value = line.split("=", 1)
                info[name.strip()] = value.strip().lower()
            self._video_shape_cache[key] = (stamp, dict(info))
            return info
        except Exception:
            return {}

    def _valid_standard_cache(self, source_path, cache_path, target_w, target_h):
        try:
            if not cache_path or not os.path.exists(cache_path) or os.path.getsize(cache_path) <= 1024:
                return False
            if self._is_hdr_video(cache_path):
                return False
            shape = self._cache_video_shape(cache_path)
            if int(shape.get('width') or 0) != int(target_w):
                return False
            if int(shape.get('height') or 0) != int(target_h):
                return False
            if shape.get('codec_name') != 'h264':
                return False
            if shape.get('pix_fmt') not in {'yuv420p', 'nv12'}:
                return False
            source_dur = self.get_duration(source_path)
            cache_dur = self.get_duration(cache_path)
            if source_dur > 0.5 and cache_dur > 0:
                return abs(source_dur - cache_dur) <= max(0.7, source_dur * 0.03)
            return True
        except Exception:
            return False

    def _is_standard_cache_source(self, source_path):
        try:
            abs_path = os.path.abspath(str(source_path or ""))
        except Exception:
            return False
        return abs_path in self._standard_cache_paths

    def _cached_standard_video(self, source_path, target_w, target_h):
        source_path = os.path.abspath(str(source_path or ""))
        if not source_path or not os.path.exists(source_path):
            return source_path
        if self._is_standard_cache_source(source_path):
            touch_cache_entry(source_path)
            return source_path
        if self._is_hdr_video(source_path):
            return source_path

        source_dur = self.get_duration(source_path)
        if source_dur <= 0.3:
            return source_path
        # Very long material is usually a talking-head/base-track source. Caching it fully can be slower
        # than rendering the short segment that is actually used.
        if source_dur > 45.0:
            return source_path

        cache_key = self._standard_cache_key(source_path, target_w, target_h)
        cached = self._standard_cache_map.get(cache_key)
        if cached and os.path.exists(cached) and os.path.getsize(cached) > 1024:
            self._standard_cache_paths.add(os.path.abspath(cached))
            touch_cache_entry(cached)
            return cached

        cache_path = self._standard_cache_path(source_path, target_w, target_h, cache_key)
        lock = self._standard_cache_lock(cache_key)
        with lock:
            cached = self._standard_cache_map.get(cache_key)
            if cached and os.path.exists(cached) and os.path.getsize(cached) > 1024:
                self._standard_cache_paths.add(os.path.abspath(cached))
                touch_cache_entry(cached)
                return cached
            if self._valid_standard_cache(source_path, cache_path, target_w, target_h):
                self._standard_cache_map[cache_key] = cache_path
                self._standard_cache_paths.add(os.path.abspath(cache_path))
                touch_cache_entry(cache_path)
                if cache_key not in self._standard_cache_hits_logged:
                    self._standard_cache_hits_logged.add(cache_key)
                    self.log_signal.emit(
                        f"[素材缓存] 复用标准化素材：{os.path.basename(source_path)}"
                    )
                return cache_path

            source_shape = self._cache_video_shape(source_path)
            if (
                int(source_shape.get('width') or 0) == int(target_w)
                and int(source_shape.get('height') or 0) == int(target_h)
                and source_shape.get('codec_name') == 'h264'
                and source_shape.get('pix_fmt') in {'yuv420p', 'nv12'}
            ):
                # 已经是目标规格的 H.264 素材直接使用，避免制造无意义的重复缓存。
                return source_path

            request_count = self._standard_cache_requests.get(cache_key, 0) + 1
            self._standard_cache_requests[cache_key] = request_count
            if request_count < 2:
                # 只出现一次的素材直接参与剪辑；同批任务再次使用时才建立长期缓存。
                return source_path

            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            tmp_path = f"{cache_path}.tmp_{os.getpid()}_{threading.get_ident()}.mp4"
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

            self.log_signal.emit(
                f"[素材缓存] 首次标准化素材，后续混剪将复用：{os.path.basename(source_path)}"
            )
            scale_quality = "flags=bicubic"
            vf = (
                f"{self._sdr_color_filter()},"
                f"scale={int(target_w)}:{int(target_h)}:force_original_aspect_ratio=increase:{scale_quality},"
                f"crop={int(target_w)}:{int(target_h)}:(iw-ow)/2:(ih-oh)/2,"
                "setsar=1,format=yuv420p,fps=30,settb=1/90000"
            )
            cmd = [
                self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                '-i', source_path,
                '-map', '0:v:0',
                '-vf', vf,
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
                '-pix_fmt', 'yuv420p',
                '-an',
                '-color_range', 'tv',
                '-color_primaries', 'bt709',
                '-color_trc', 'bt709',
                '-colorspace', 'bt709',
                '-bsf:v', 'h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1',
                '-movflags', '+faststart',
                tmp_path,
            ]
            if not self.run_ffmpeg(cmd, cwd=os.path.dirname(cache_path)):
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                self.log_signal.emit(
                    f"[素材缓存] ⚠️ 标准化失败，当前素材本次仍直接参与混剪：{os.path.basename(source_path)}"
                )
                return source_path
            try:
                if not self._valid_standard_cache(source_path, tmp_path, target_w, target_h):
                    raise RuntimeError("缓存文件校验未通过")
                os.replace(tmp_path, cache_path)
                self._video_color_cache.pop(cache_path, None)
                self._video_shape_cache.pop(cache_path, None)
                self.task.duration_cache.pop(cache_path, None)
                self._standard_cache_map[cache_key] = cache_path
                self._standard_cache_paths.add(os.path.abspath(cache_path))
                self.log_signal.emit(
                    f"[素材缓存] ✅ 标准化素材缓存已生成：{os.path.basename(cache_path)}"
                )
                return cache_path
            except Exception as exc:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
                self.log_signal.emit(
                    f"[素材缓存] ⚠️ 标准化缓存校验失败，当前素材本次仍直接参与混剪："
                    f"{os.path.basename(source_path)}；{exc}"
                )
                return source_path

    def _cached_edit_source_video(self, source_path, target_w, target_h, normalize=True):
        source_path = os.path.abspath(str(source_path or ""))
        if not source_path or not os.path.exists(source_path):
            return source_path
        source_is_hdr = self._is_hdr_video(source_path)
        sdr_source = self._cached_sdr_video(source_path)
        if not normalize:
            return sdr_source
        if source_is_hdr or self._is_hdr_video(sdr_source):
            return sdr_source
        if not self.task.enable_standard_video_cache:
            return sdr_source
        return self._cached_standard_video(sdr_source, target_w, target_h)

    def _audio_silence_points(self, media_path):
        """Return silence start points for phrase-friendly strong-structure cuts."""
        key = os.path.abspath(str(media_path or ""))
        if not key:
            return []
        if key in self._silence_points_cache:
            return self._silence_points_cache[key]
        points = []
        try:
            duration = max(0.0, float(self.get_duration(key) or 0.0))
            timeout = max(8, min(45, int(duration / 12) + 8))
            cmd = [
                self.ffmpeg_exe,
                "-hide_banner",
                "-nostdin",
                "-loglevel", "info",
                "-i", key,
                "-vn",
                "-af", "silencedetect=noise=-32dB:d=0.16",
                "-f", "null",
                "-",
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                startupinfo=_hidden_startupinfo(),
                timeout=timeout,
            )
            text = (res.stderr or "") + "\n" + (res.stdout or "")
            points = [
                max(0.0, float(match.group(1)))
                for match in re.finditer(r"silence_start:\s*([0-9.]+)", text)
            ]
        except Exception:
            points = []
        self._silence_points_cache[key] = points
        return points

    @staticmethod
    def _is_remote_source(file_path):
        return is_remote_path(file_path)

    def _stage_remote_audio_sources(self, audios, cache_dir):
        remote_paths = []
        for item in audios:
            path = os.path.abspath(str(item.get("path", "")))
            if path and self._is_remote_source(path) and path not in remote_paths:
                remote_paths.append(path)
        if not remote_paths:
            return audios, ""

        try:
            for name in os.listdir(cache_dir):
                if not name.startswith("first_track_stage_"):
                    continue
                stale_path = os.path.join(cache_dir, name)
                if os.path.isdir(stale_path) and time.time() - os.path.getmtime(stale_path) > 86400:
                    shutil.rmtree(stale_path, ignore_errors=True)
        except Exception:
            pass

        self.log_signal.emit(
            f"[稳定加速] 检测到 {len(remote_paths)} 个网络第一轨道，正在一次性暂存到本机；后续各轮不再重复读取网盘。"
        )
        missing = list(remote_paths)
        for attempt in range(1, 7):
            missing = [path for path in missing if not os.path.isfile(path)]
            if not missing or not self.is_running:
                break
            if attempt == 1 or attempt == 6:
                self.log_signal.emit(
                    f"[网络素材] 仍有 {len(missing)} 个文件暂时不可访问，第 {attempt}/6 次等待映射盘恢复..."
                )
            time.sleep(5)
        if missing:
            self.log_signal.emit(
                "[致命错误] 网络第一轨道仍不可访问，任务已安全停止，未继续生成缺素材视频："
                + "、".join(os.path.basename(path) for path in missing[:6])
            )
            return None, ""

        required_bytes = sum(os.path.getsize(path) for path in remote_paths)
        free_bytes = shutil.disk_usage(cache_dir).free
        if free_bytes < required_bytes + 1024 * 1024 * 1024:
            self.log_signal.emit(
                f"[致命错误] 本机缓存空间不足：网络素材约 {required_bytes / 1024**3:.2f}GB，"
                f"当前仅剩 {free_bytes / 1024**3:.2f}GB。"
            )
            return None, ""

        stage_dir = os.path.join(
            cache_dir,
            f"first_track_stage_{os.getpid()}_{int(time.time())}",
        )
        os.makedirs(stage_dir, exist_ok=True)

        def copy_one(index_and_path):
            index, source = index_and_path
            source_size = os.path.getsize(source)
            digest = hashlib.sha1(source.lower().encode("utf-8", errors="ignore")).hexdigest()[:10]
            extension = os.path.splitext(source)[1] or ".mp4"
            target = os.path.join(stage_dir, f"{index:03d}_{digest}{extension}")
            temp_target = target + ".part"
            for attempt in range(1, 4):
                if not self.is_running:
                    return source, ""
                try:
                    if os.path.exists(temp_target):
                        os.remove(temp_target)
                    shutil.copy2(source, temp_target)
                    if os.path.getsize(temp_target) != source_size:
                        raise OSError("复制后文件大小不一致")
                    os.replace(temp_target, target)
                    return source, target
                except Exception:
                    try:
                        if os.path.exists(temp_target):
                            os.remove(temp_target)
                    except Exception:
                        pass
                    if attempt < 3:
                        time.sleep(3)
            return source, ""

        staged_map = {}
        with ThreadPoolExecutor(max_workers=min(2, len(remote_paths))) as executor:
            futures = [
                executor.submit(copy_one, pair)
                for pair in enumerate(remote_paths)
            ]
            completed = 0
            for future in as_completed(futures):
                source, staged = future.result()
                completed += 1
                if staged:
                    staged_map[source] = staged
                if completed == len(remote_paths) or completed % 5 == 0:
                    self.log_signal.emit(
                        f"[稳定加速] 网络第一轨道本地化进度：{completed}/{len(remote_paths)}"
                    )

        failed = [path for path in remote_paths if path not in staged_map]
        if failed:
            shutil.rmtree(stage_dir, ignore_errors=True)
            self.log_signal.emit(
                "[致命错误] 网络素材复制失败，任务已安全停止："
                + "、".join(os.path.basename(path) for path in failed[:6])
            )
            return None, ""

        staged_audios = []
        for item in audios:
            staged_item = dict(item)
            original_path = os.path.abspath(str(item.get("path", "")))
            staged_item["source_basename"] = os.path.basename(original_path).split(".")[0]
            staged_item["path"] = staged_map.get(original_path, original_path)
            staged_audios.append(staged_item)
        self.log_signal.emit(
            f"[稳定加速] 网络第一轨道已全部转为本机读取，共 {required_bytes / 1024**3:.2f}GB。"
        )
        return staged_audios, stage_dir

    def _write_ffmpeg_failure_log(self, cmd, output, returncode, cwd=None):
        try:
            os.makedirs(logs_dir(), exist_ok=True)
            path = os.path.join(logs_dir(), f"ffmpeg_fail_{time.strftime('%Y%m%d')}.log")
            with self._failure_log_lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"\n{'=' * 72}\n")
                    f.write(f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"returncode: {returncode}\n")
                    f.write(f"cwd: {cwd or ''}\n\n")
                    f.write("command:\n")
                    f.write(" ".join(str(part) for part in cmd))
                    f.write("\n\noutput:\n")
                    f.write(output or "")
                    f.write("\n")
            return path
        except Exception:
            return "日志写入失败"

    def _safe_font_path(self, font_path="", required_text=""):
        font_path = self._normalize_font_path(font_path)
        if (
            font_path
            and self._is_drawtext_font_safe(font_path)
            and self._font_supports_text(font_path, required_text)
        ):
            return font_path
        for candidate in self._fallback_font_candidates():
            if (
                self._is_drawtext_font_safe(candidate)
                and self._font_supports_text(candidate, required_text)
            ):
                return candidate
        return ""

    def _normalize_font_path(self, font_path=""):
        if not font_path:
            return ""
        candidate = os.path.abspath(str(font_path).replace("\\", os.sep))
        if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
            return candidate
        return ""

    def _fallback_font_candidates(self):
        preferred = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
        ]
        candidates = []
        seen = set()
        for path in preferred:
            normalized = self._normalize_font_path(path)
            if normalized and normalized not in seen:
                candidates.append(normalized)
                seen.add(normalized)
        for path in self.task_config.sys_fonts.values():
            normalized = self._normalize_font_path(path)
            if normalized and normalized not in seen:
                candidates.append(normalized)
                seen.add(normalized)
        return candidates

    def _drawtext_font_cache_file(self):
        return os.path.join(config_dir(), ".ffmpeg_drawtext_font_cache.json")

    def _load_drawtext_font_cache(self):
        if hasattr(self, "_drawtext_font_cache"):
            return self._drawtext_font_cache
        path = self._drawtext_font_cache_file()
        try:
            with open(path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}
        self._drawtext_font_cache = cache
        return cache

    def _save_drawtext_font_cache(self):
        try:
            os.makedirs(config_dir(), exist_ok=True)
            with open(self._drawtext_font_cache_file(), "w", encoding="utf-8") as f:
                json.dump(self._drawtext_font_cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _font_cache_key(self, font_path):
        try:
            stat = os.stat(font_path)
            return f"{os.path.abspath(font_path)}|{int(stat.st_mtime)}|{stat.st_size}"
        except Exception:
            return os.path.abspath(str(font_path))

    @staticmethod
    def _font_probe_text():
        return "字体测试商品视频水印医疗器械中文"

    def _font_supports_text(self, font_path, text=""):
        """验证字体包含待烧录中文字形，避免扩展字库渲染成方框。"""
        font_path = self._normalize_font_path(font_path)
        if not font_path:
            return False
        required_chars = "".join(
            dict.fromkeys(
                char
                for char in str(text or self._font_probe_text())
                if (
                    0x3400 <= ord(char) <= 0x9FFF
                    or 0xF900 <= ord(char) <= 0xFAFF
                )
            )
        )
        if not required_chars:
            return True

        file_name = os.path.basename(font_path).lower()
        if file_name == "simsunb.ttf" or (
            file_name.startswith("simsun") and "ext" in file_name
        ):
            return False

        cache = getattr(self, "_font_glyph_support_cache", None)
        if cache is None:
            cache = {}
            self._font_glyph_support_cache = cache
        cache_key = (self._font_cache_key(font_path), required_chars)
        if cache_key in cache:
            return cache[cache_key]

        # 正常桌面运行时已有 QApplication；无图形实例的纯测试环境使用文件名兜底。
        if QGuiApplication.instance() is None:
            cache[cache_key] = True
            return True
        try:
            raw_font = QRawFont(font_path, 32)
            supported = raw_font.isValid() and all(
                raw_font.supportsCharacter(ord(char)) for char in required_chars
            )
        except Exception:
            supported = False
        cache[cache_key] = supported
        return supported

    def _is_drawtext_font_safe(self, font_path):
        font_path = self._normalize_font_path(font_path)
        if not font_path:
            return False
        cache = self._load_drawtext_font_cache()
        key = self._font_cache_key(font_path)
        cached = cache.get(key, {})
        if cached.get("probe_version") == DRAWTEXT_FONT_PROBE_VERSION:
            return bool(cached.get("ok"))

        if not self._font_supports_text(font_path, self._font_probe_text()):
            cache[key] = {
                "ok": False,
                "probe_version": DRAWTEXT_FONT_PROBE_VERSION,
                "path": font_path,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "returncode": "missing_glyphs",
                "message": "字体不包含常用中文字形",
            }
            self._save_drawtext_font_cache()
            return False

        temp_dir = self.task_config.cache_dir or logs_dir()
        os.makedirs(temp_dir, exist_ok=True)
        text_path = os.path.join(temp_dir, f"font_probe_{random.randint(10000, 99999)}.txt")
        try:
            with open(text_path, "w", encoding="utf-8") as f:
                f.write("字体测试")
            vf = (
                f"drawtext=fontfile='{path_to_ffmpeg(font_path)}':"
                f"textfile='{path_to_ffmpeg(text_path)}':fontsize=18:fontcolor=white:x=2:y=2"
            )
            cmd = [
                self.ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=black:s=80x80:d=0.1",
                "-vf", vf, "-frames:v", "1", "-f", "null", "-"
            ]
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore",
                startupinfo=self._get_si(), timeout=8
            )
            probe_output = (res.stdout or "").strip()
            incompatible_output = bool(re.search(
                r"fontconfig\s+error|cannot\s+load\s+default\s+config|"
                r"could\s+not\s+load\s+font|error\s+loading\s+font",
                probe_output,
                re.IGNORECASE,
            ))
            ok = res.returncode == 0 and not incompatible_output
            cache[key] = {
                "ok": ok,
                "probe_version": DRAWTEXT_FONT_PROBE_VERSION,
                "path": font_path,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "returncode": res.returncode,
                "message": probe_output[-500:],
            }
            self._save_drawtext_font_cache()
            return ok
        except Exception as exc:
            cache[key] = {
                "ok": False,
                "probe_version": DRAWTEXT_FONT_PROBE_VERSION,
                "path": font_path,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "returncode": "exception",
                "message": str(exc),
            }
            self._save_drawtext_font_cache()
            return False
        finally:
            try:
                if os.path.exists(text_path):
                    os.remove(text_path)
            except Exception:
                pass

    def _drawtext_safe_font_pool(self):
        if hasattr(self, "_drawtext_safe_fonts"):
            return self._drawtext_safe_fonts
        candidates = self._fallback_font_candidates()
        safe = []
        for font_path in candidates:
            if self._is_drawtext_font_safe(font_path):
                safe.append(font_path)
        self._drawtext_safe_fonts = safe
        total = len(candidates)
        skipped = max(0, total - len(safe))
        self.log_signal.emit(f"[字体] ffmpeg 安全随机字体池：可用 {len(safe)} 个，已剔除 {skipped} 个不稳定字体。")
        return safe

    def _random_drawtext_font(self, required_text=""):
        pool = self._drawtext_safe_font_pool()
        supported = [
            font_path
            for font_path in pool
            if self._font_supports_text(font_path, required_text)
        ]
        if supported:
            return random.choice(supported)
        return self._safe_font_path("", required_text)

    def _log_watermark_font_choice(self, font_path, requested_name=""):
        logged = getattr(self, "_watermark_font_choices_logged", None)
        if logged is None:
            logged = set()
            self._watermark_font_choices_logged = logged
        normalized = self._normalize_font_path(font_path)
        key = (str(requested_name or ""), normalized)
        if key in logged:
            return
        logged.add(key)
        if normalized:
            self.log_signal.emit(
                f"[字体][文字水印] {requested_name or '自动字体'} → {normalized}"
            )
        else:
            self.log_signal.emit(
                f"[字体][文字水印] ⚠️ {requested_name or '自动字体'} 没有找到兼容当前中文的字体，"
                "本条水印已跳过，避免输出方框。"
            )

    @staticmethod
    def _ffmpeg_color(color, fallback="white"):
        color = str(color or "").strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            return "0x" + color[1:]
        return color or fallback

    def _has_audio_stream(self, file_path):
        """检测媒体文件是否包含音频流"""
        try:
            cmd = [self.ffprobe_exe, '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', file_path]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=self._get_si(),
                timeout=15,
            )
            return 'audio' in (result.stdout.strip().lower() or '')
        except Exception:
            return False

    def _generate_silent_audio(self, output_path, duration_sec):
        """生成指定时长的静音音频文件"""
        cmd = [
            self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
            '-t', f'{duration_sec:.3f}',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            output_path
        ]
        self.run_ffmpeg(cmd, cwd=os.path.dirname(os.path.abspath(output_path)))

    @staticmethod
    def split_watermark_text_pool(text):
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        return [block.strip() for block in re.split(r"\n\s*\n+", text) if block.strip()]

    @staticmethod
    def apply_watermark_letter_spacing(text, spacing):
        spacing = max(0, int(float(spacing or 0)))
        if spacing <= 0:
            return text
        # 部分中文字体缺少 U+2006，会被 FFmpeg 渲染成方框；普通空格兼容性更稳。
        spacer = " " * max(1, min(10, round(spacing / 4)))
        chars = list(str(text))
        if len(chars) <= 1:
            return text
        result = []
        for idx, char in enumerate(chars):
            result.append(char)
            if idx < len(chars) - 1 and char.strip() and chars[idx + 1].strip():
                result.append(spacer)
        return "".join(result)

    def generate_watermark_filter(
        self,
        resolved_watermarks,
        cache_dir,
        unique_id,
        time_offset=0.0,
        is_first_clip=False,
        target_w=1080,
        target_h=1920,
    ):
        if not resolved_watermarks:
            return ""
        wm_str = ""
        t_expr = f"(t+{time_offset:.3f})"
        delay_filter = "enable='gt(t\\, 0.2)':" if is_first_clip else ""
        scale_x = max(0.1, float(target_w or 1080) / 1080.0)
        scale_y = max(0.1, float(target_h or 1920) / 1920.0)
        font_scale = min(scale_x, scale_y)
        
        for idx, r_wm in enumerate(resolved_watermarks):
            wm = r_wm['original']
            actual_text = str(r_wm.get('actual_text', wm.get('text', '')))
            lines = actual_text.split('\n')
            safe_font_path = path_to_ffmpeg(
                self._safe_font_path(r_wm.get('actual_font_path', ''), actual_text)
            )
            size = max(8, int(round(float(wm.get('size', 60) or 60) * font_scale)))
            align = wm.get('align', '居中对齐')
            color = self._ffmpeg_color(r_wm['actual_color'])
            border_w = max(0, int(round(float(wm.get('border_w', 0) or 0) * font_scale)))
            border_color = self._ffmpeg_color(r_wm['actual_border_color'], "black")
            is_bold = wm.get('is_bold', False)
            line_step = size * max(40.0, float(wm.get('line_spacing', 130) or 130)) / 100.0
            letter_spacing = float(wm.get('letter_spacing', 0) or 0) * font_scale
            
            offset_x = float(r_wm.get('offset_x', 0.0) or 0.0) * scale_x
            offset_y = float(r_wm.get('offset_y', 0.0) or 0.0) * scale_y
            anim_key = r_wm['anim_key']
            base_x = float(wm.get('x', 540) or 540) * scale_x + offset_x
            base_y = float(wm.get('y', 960) or 960) * scale_y + offset_y
            x_tpl, y_tpl, alpha_tpl = ANIMATIONS.get(anim_key, ANIMATIONS["静态展示"])
            
            for i, line in enumerate(lines):
                if not line.strip():
                    continue
                txt_file = os.path.join(cache_dir, f"wm_{unique_id}_{idx}_l{i}.txt")
                with open(txt_file, 'w', encoding='utf-8') as f:
                    f.write(self.apply_watermark_letter_spacing(line, letter_spacing))
                safe_txt_file = path_to_ffmpeg(txt_file)
                
                line_spacing = i * line_step
                line_y_str = f"({base_y}) + {line_spacing}"
                if align == "居中对齐":
                    x_base_str = f"({base_x}) - tw/2"
                elif align == "右对齐":
                    x_base_str = f"({base_x}) - tw"
                else:
                    x_base_str = f"({base_x})"
                    
                x_expr = x_tpl.replace("(base_x)", x_base_str).replace("(offset_x)", str(offset_x)).replace("TIME_VAR", t_expr)
                y_expr = y_tpl.replace("(line_y)", line_y_str).replace("(offset_y)", f"({offset_y} + {line_spacing})").replace("TIME_VAR", t_expr)
                alpha = alpha_tpl.replace("TIME_VAR", t_expr)
                
                font_cmd = f"fontfile='{safe_font_path}':" if safe_font_path else ""
                actual_bw = border_w
                actual_bc = border_color
                if is_bold and border_w == 0:
                    actual_bw = 2
                    actual_bc = color
                elif is_bold and border_w > 0:
                    actual_bw = border_w + 1
                border_cmd = f"borderw={actual_bw}:bordercolor={actual_bc}:" if actual_bw > 0 else ""
                wm_str += f",drawtext={delay_filter}{font_cmd}textfile='{safe_txt_file}':fontsize={size}:fontcolor={color}:{border_cmd}x={x_expr}:y={y_expr}:alpha='{alpha}'"
        return wm_str

    def process_asr_and_color(self, audio_path, cache_dir, audio_name, sub_config, sys_fonts, gpu_mode, timeline_offset=0.0):
        safe_audio_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', audio_name)[:60] or "audio"
        ass_out_path = os.path.join(cache_dir, f"subtitle_{safe_audio_name}_{random.randint(10000, 99999)}.ass")
        if os.path.exists(ass_out_path):
            return ass_out_path
        
        engine_type = "N卡加速" if "NVENC" in gpu_mode else ("A卡加速" if "AMF" in gpu_mode else "CPU满载")
        self.log_signal.emit("🎧 [AI 引擎] 正在开启 SenseVoice 本地识别生成字幕...")
        
        temp_wav_path = ""
        temp_srt_path = os.path.join(cache_dir, f"sensevoice_{random.randint(10000, 99999)}.srt")
        actual_srt = None

        if sub_config.get('word_timestamps'):
            whisper_exe = self._get_whisper_exe(gpu_mode)
            models = [
                os.path.join(self.tools_dir, name)
                for name in os.listdir(self.tools_dir)
                if name.endswith('.bin')
            ]
            if whisper_exe and models:
                try:
                    self.log_signal.emit("🎯 正在生成精准词级时间轴，这一步会比普通听写稍慢...")
                    word_srt_path = os.path.join(
                        cache_dir,
                        f"word_timeline_{random.randint(10000, 99999)}.srt",
                    )
                    actual_srt, word_segments = transcribe_with_whisper_word_timestamps(
                        whisper_exe,
                        models[0],
                        self.ffmpeg_exe,
                        audio_path,
                        word_srt_path,
                        timeout_sec=900,
                    )
                    temp_srt_path = actual_srt
                    self.log_signal.emit(
                        f"✅ 精准词级时间轴完成，共 {len(word_segments)} 句，字幕将按真实停顿对齐。"
                    )
                except Exception as exc:
                    self.log_signal.emit(
                        f"⚠️ 精准词级时间轴失败，自动回退普通字幕识别：{exc}"
                    )
            else:
                self.log_signal.emit("⚠️ 未找到可用 Whisper 程序或模型，精准时间轴已回退普通字幕。")

        if not actual_srt and sensevoice_model_ready(self.sensevoice_model_dir):
            try:
                session = self._get_sensevoice_session()
                text, device = session.transcribe(audio_path, timeout_sec=300)
                audio_dur = self.get_duration(audio_path)
                write_srt_from_text(text, audio_dur, temp_srt_path)
                actual_srt = temp_srt_path
                self.log_signal.emit(f"✅ SenseVoice 字幕识别完成（隔离进程，设备：{device}）。")
            except Exception as e:
                self.log_signal.emit(f"⚠️ SenseVoice 识别失败，尝试 Whisper 回退: {e}")

        if not actual_srt:
            whisper_exe = self._get_whisper_exe(gpu_mode)
            if not whisper_exe:
                return None
            models = [f for f in os.listdir(self.tools_dir) if f.endswith('.bin')]
            if not models:
                return None
            model_filename = models[0]

            self.log_signal.emit(f"🎧 [AI 引擎] 正在回退 Whisper {engine_type} 听写...")
            temp_wav_name = f"temp_whisper_{random.randint(10000, 99999)}.wav"
            temp_wav_path = os.path.join(self.tools_dir, temp_wav_name)
            temp_srt_path = temp_wav_path + ".srt"

            cmd_wav = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error', '-i', audio_path, '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', temp_wav_path]
            if not self.run_ffmpeg(cmd_wav, cwd=self.tools_dir):
                self.log_signal.emit("❌ 提取音频给 AI 失败！")
                return None

            if not os.path.exists(temp_wav_path):
                self.log_signal.emit("❌ 提取音频给 AI 失败！")
                return None

            cmd_whisper = [
                whisper_exe, '-m', model_filename, '-f', temp_wav_name,
                '-osrt', '-l', 'zh', '--prompt', '简体中文',
                '-t', self.optimal_threads
            ]
            try:
                res = subprocess.run(
                    cmd_whisper,
                    cwd=self.tools_dir,
                    startupinfo=self._get_si(),
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='ignore',
                    timeout=900,
                )
            except subprocess.TimeoutExpired:
                self.log_signal.emit("❌ Whisper 听写超过 900 秒，已终止并跳过本条字幕。")
                return None
            except Exception as exc:
                self.log_signal.emit(f"❌ Whisper 启动失败：{exc}")
                return None

            if res.returncode != 0:
                err_text = res.stderr.strip() if res.stderr else res.stdout.strip()
                err_line = " | ".join(err_text.split('\n')[-3:]) if err_text else "程序闪退(请检查是否放置了对应的硬件版 whisper 及其 dll)"
                self.log_signal.emit(f"❌ Whisper 崩溃: {err_line}")

            actual_srt = temp_srt_path if os.path.exists(temp_srt_path) else None
        
        if not actual_srt or os.path.getsize(actual_srt) == 0: 
            self.log_signal.emit("⚠️ 未能生成有效字幕，跳过字幕压制。")
            if os.path.exists(temp_wav_path):
                os.remove(temp_wav_path)
            return None 

        def hex_to_ass(h):
            h = h.strip('#')
            if len(h) != 6:
                return "&H00FFFFFF&"
            return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}&"

        vibrant_colors = [
            "#00FFFF", "#FFFF00", "#00FF00", "#FF00FF",
            "#00A5FF", "#FFFFFF", "#FF5722", "#E91E63",
        ]
        border_colors = ["#000000", "#FFFFFF", "#FF0000", "#003366"]
        random_color = sub_config.get('color') == "🎲 随机颜色"
        random_border_color = sub_config.get('border_color') == "🎲 随机颜色"
        base_color = "#FFFFFF" if random_color else sub_config.get('color', '#FFFFFF')
        base_border_color = "#000000" if random_border_color else sub_config.get('border_color', '#000000')

        ass_c = hex_to_ass(base_color)
        ass_bc = hex_to_ass(base_border_color)
        font_name = sub_config.get('font', 'Arial')
        if font_name == "🎲 随机系统字体":
            font_name = random.choice(list(sys_fonts.keys())) if sys_fonts else "Arial"

        selected_anim = sub_config.get('anim', '无动效')
        random_animation = selected_anim == "🎲 随机动效"
        random_animation_choices = [
            (key, value)
            for key, value in SUBTITLE_ANIMATIONS.items()
            if key not in ("无动效", "🎲 随机动效")
            and value not in ("", "random")
            # 随机变色动效会覆盖用户选择的主色/描边色，因此不放入随机池。
            and not re.search(r"\\(?:[134]?c)(?:&|\\)", value)
        ]
        previous_color = ""
        previous_border_color = ""
        previous_animation = ""

        def choose_different(items, previous):
            choices = [item for item in items if item != previous]
            return random.choice(choices or items)

        def event_override(event_font_size=None):
            nonlocal previous_color, previous_border_color, previous_animation
            actual_color = (
                choose_different(vibrant_colors, previous_color)
                if random_color else base_color
            )
            actual_border = (
                choose_different(
                    [color for color in border_colors if color != actual_color],
                    previous_border_color,
                )
                if random_border_color else base_border_color
            )
            previous_color = actual_color
            previous_border_color = actual_border

            anim_value = SUBTITLE_ANIMATIONS.get(selected_anim, "")
            if random_animation and random_animation_choices:
                available = [
                    item for item in random_animation_choices
                    if item[0] != previous_animation
                ] or random_animation_choices
                anim_key, anim_value = random.choice(available)
                previous_animation = anim_key
            if anim_value == "random":
                anim_value = ""
            anim_body = anim_value[1:-1] if anim_value.startswith("{") and anim_value.endswith("}") else anim_value
            font_body = f"\\fs{int(event_font_size)}" if event_font_size else ""
            # 所有 ASS 标签保持在同一对大括号中，避免颜色代码被烧成可见文字。
            return (
                "{"
                f"\\c{hex_to_ass(actual_color)}"
                f"\\3c{hex_to_ass(actual_border)}"
                f"{font_body}"
                f"{anim_body}"
                "}"
            )
        
        margin_v = sub_config.get('margin_v', 180)
        margin_side = 80

        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{sub_config.get('size', 85)},{ass_c},&H000000FF,{ass_bc},&H00000000,{sub_config.get('bold', -1)},0,0,0,100,100,0,0,1,{sub_config.get('border_w', 5)},{sub_config.get('shadow', 0)},2,{margin_side},{margin_side},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        try:
            with open(actual_srt, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            events = []
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if '-->' in line:
                    times = line.split('-->')
                    def fix_t(t):
                        h, m, s = t.strip().replace(',', '.').split(':')
                        total_sec = int(h) * 3600 + int(m) * 60 + float(s)
                        # AI开场和封面都位于数字人口播之前，字幕统一后移到主内容时间轴。
                        total_sec += max(0.0, float(timeline_offset or 0.0))
                        new_h = int(total_sec // 3600)
                        new_m = int((total_sec % 3600) // 60)
                        new_s = total_sec % 60
                        return f"{new_h:01d}:{new_m:02d}:{new_s:05.2f}"
                        
                    start = fix_t(times[0])
                    end = fix_t(times[1])
                    i += 1
                    text_lines = []
                    while i < len(lines) and lines[i].strip() != "":
                        clean_text = lines[i].strip()
                        if not (clean_text.startswith('[') and clean_text.endswith(']')):
                            clean_text = clean_asr_text(clean_text)
                            clean_text = clean_text.strip("。，、？！. ,?!\n\t")
                            if len(clean_text) == 0 or len(clean_text) > 35:
                                i += 1
                                continue
                            if "字幕" in clean_text or "翻译" in clean_text or "未经允许" in clean_text:
                                i += 1
                                continue
                            
                            text_lines.append(clean_text)
                        i += 1
                    text = " ".join(text_lines)
                    if text:
                        text, event_font_size = layout_subtitle_text(
                            text,
                            font_size=sub_config.get('size', 85),
                            play_res_x=1080,
                            margin_side=margin_side,
                        )
                        events.append(
                            f"Dialogue: 0,{start},{end},Default,,0,0,0,,"
                            f"{event_override(event_font_size)}{text}"
                        )
                i += 1
            with open(ass_out_path, 'w', encoding='utf-8') as f:
                f.write(header + "\n".join(events) + "\n")
            if not events:
                self.log_signal.emit("⚠️ 智能字幕识别到了文本，但过滤后没有可烧录的字幕行。请调大“每行字数”或更换更清晰的音频。")
                return None
            
            if os.path.exists(temp_wav_path):
                os.remove(temp_wav_path)
            if os.path.exists(temp_srt_path):
                os.remove(temp_srt_path)
            
            return ass_out_path
        except Exception as e:
            self.log_signal.emit(f"⚠️ 字幕处理失败: {str(e)}")
            return None

    def build_subtitle_filter(self, ass_path):
        safe_ass = path_to_ffmpeg(os.path.abspath(ass_path))
        fonts_dir = path_to_ffmpeg("C:/Windows/Fonts") if os.name == "nt" else ""
        if fonts_dir:
            return f"subtitles=filename='{safe_ass}':fontsdir='{fonts_dir}'"
        return f"subtitles=filename='{safe_ass}'"

    def _build_encoder_profile(self, gpu_mode, target_w=1080, target_h=1920):
        color_tag_args = [
            '-color_range', 'tv',
            '-color_primaries', 'bt709',
            '-color_trc', 'bt709',
            '-colorspace', 'bt709',
            '-bsf:v', 'h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1',
        ]
        is_720_portrait = int(target_w or 0) * int(target_h or 0) <= 720 * 1280
        stage_rate = '4M' if is_720_portrait else '8M'
        stage_maxrate = '6M' if is_720_portrait else '12M'
        stage_bufsize = '8M' if is_720_portrait else '16M'
        final_rate = '5M' if is_720_portrait else '12M'
        final_maxrate = '8M' if is_720_portrait else '18M'
        final_bufsize = '10M' if is_720_portrait else '24M'
        if "NVENC" in gpu_mode:
            return EncoderProfile(
                pix_fmt="yuv420p",
                enc_args=[
                    '-c:v', 'h264_nvenc', '-preset', 'fast',
                    '-b:v', stage_rate, '-maxrate', stage_maxrate, '-bufsize', stage_bufsize,
                    *color_tag_args,
                ],
                final_enc_args=[
                    '-c:v', 'h264_nvenc', '-preset', 'fast',
                    '-b:v', final_rate, '-maxrate', final_maxrate, '-bufsize', final_bufsize,
                    '-profile:v', 'high', '-level', '4.2',
                    *color_tag_args,
                ],
            )
        if "AMF" in gpu_mode:
            return EncoderProfile(
                pix_fmt="nv12",
                enc_args=[
                    '-c:v', 'h264_amf',
                    '-b:v', stage_rate, '-maxrate', stage_maxrate, '-bufsize', stage_bufsize,
                    *color_tag_args,
                ],
                final_enc_args=[
                    '-c:v', 'h264_amf',
                    '-b:v', final_rate, '-maxrate', final_maxrate, '-bufsize', final_bufsize,
                    *color_tag_args,
                ],
            )
        return EncoderProfile(
            pix_fmt="yuv420p",
            enc_args=[
                # veryfast 比 fast 快约 40-60%，画质损失可接受；无独显/远程桌面机器收益明显
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                '-maxrate', stage_maxrate, '-bufsize', stage_bufsize,
                *color_tag_args,
            ],
            final_enc_args=[
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
                '-profile:v', 'high', '-level', '4.2',
                '-maxrate', final_maxrate, '-bufsize', final_bufsize,
                *color_tag_args,
            ],
        )

    def _build_audio_job_context(self, audio_dict, round_idx):
        return AudioJobContext.from_audio_dict(audio_dict, round_idx)

    def _cleanup_render_temp_files(
        self,
        cache_dir,
        unique_id,
        audio_name,
        concat_list,
        transition_temp_files,
        watermarks,
        extra_files=None,
    ):
        safe_audio_name = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5]', '_', audio_name)
        trash_files = list(concat_list or []) + list(transition_temp_files or []) + [
            os.path.join(cache_dir, f"trans_{unique_id}.ts"),
            os.path.join(cache_dir, f"main_v_{unique_id}.ts"),
            os.path.join(cache_dir, f"concat_{unique_id}.txt"),
            os.path.join(cache_dir, f"cover_v_{unique_id}.ts"),
            os.path.join(cache_dir, f"c_concat_{unique_id}.txt"),
            os.path.join(cache_dir, f"cover_final_{unique_id}.ts"),
            os.path.join(cache_dir, f"asr_{safe_audio_name}_16k.wav"),
            os.path.join(cache_dir, f"asr_{safe_audio_name}_16k.wav.srt"),
            os.path.join(cache_dir, f"asr_{safe_audio_name}_16k.srt"),
            os.path.join(cache_dir, f"silent_{unique_id}.aac"),
        ]
        trash_files.extend(extra_files or [])
        for idx in range(len(watermarks or [])):
            for i in range(80):
                tf = os.path.join(cache_dir, f"wm_{unique_id}_{idx}_l{i}.txt")
                if os.path.exists(tf):
                    trash_files.append(tf)
        try:
            for name in os.listdir(cache_dir):
                if (
                    name.startswith(f"strong_fill_{unique_id}_")
                    or name.startswith(f"strong_fill_mix_{unique_id}")
                    or name.startswith(f"strong_fill_concat_{unique_id}")
                ):
                    trash_files.append(os.path.join(cache_dir, name))
        except Exception:
            pass
        for temp_file in trash_files:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

    def _mix_xfade_group(
        self,
        clips,
        output_path,
        transition_duration,
        transition_selector,
        enc_args,
        pix_fmt,
        cache_dir,
    ):
        if len(clips) <= 1 or not self.is_running:
            return False
        inputs = []
        clip_durations = []
        for clip in clips:
            inputs.extend(['-i', clip])
            clip_durations.append(max(0.05, self.get_duration(clip)))
        plan = build_xfade_plan(
            clip_durations,
            transition_duration,
            transition_selector,
        )
        if plan is None or not self.is_running:
            return False
        command = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error'] + inputs + [
            '-filter_complex', plan.filter_complex,
            '-map', f"[{plan.output_label}]",
            '-t', f"{plan.duration:.3f}",
            *enc_args, '-an', '-r', '30', '-pix_fmt', pix_fmt, output_path,
        ]
        return (
            self.run_ffmpeg(command, cwd=cache_dir)
            and os.path.exists(output_path)
            and os.path.getsize(output_path) > 1024
        )

    def _merge_xfade_hierarchy(
        self,
        clips,
        final_group_output,
        transition_duration,
        transition_selector,
        enc_args,
        pix_fmt,
        cache_dir,
        intermediate_name_pattern,
        transition_temp_files=None,
    ):
        stage_clips = list(clips or [])
        if len(stage_clips) <= 1:
            return stage_clips[0] if stage_clips else ""
        level = 0
        while len(stage_clips) > 1:
            if not self.is_running:
                return ""
            level += 1
            next_stage = []
            group_count = (len(stage_clips) + 4) // 5
            for group_index in range(group_count):
                group = stage_clips[group_index * 5:(group_index + 1) * 5]
                if len(group) == 1:
                    next_stage.append(group[0])
                    continue
                is_final_group = group_count == 1
                group_output = final_group_output if is_final_group else os.path.join(
                    cache_dir,
                    intermediate_name_pattern.format(level=level, group_index=group_index),
                )
                if transition_temp_files is not None and group_output not in transition_temp_files:
                    transition_temp_files.append(group_output)
                if not self._mix_xfade_group(
                    group,
                    group_output,
                    transition_duration,
                    transition_selector,
                    enc_args,
                    pix_fmt,
                    cache_dir,
                ):
                    return ""
                next_stage.append(group_output)
            stage_clips = next_stage
        result = stage_clips[0] if stage_clips else ""
        if result and os.path.exists(result) and os.path.getsize(result) > 1024:
            return result
        return ""

    def _concat_video_clips(
        self,
        clips,
        concat_list_path,
        output_path,
        cache_dir,
        duration=None,
    ):
        if not clips or not self.is_running:
            return False
        try:
            with open(concat_list_path, 'w', encoding='utf-8') as handle:
                for clip in clips:
                    safe_clip_path = str(clip).replace('\\', '/')
                    handle.write(f"file '{safe_clip_path}'\n")
        except Exception as exc:
            self.log_signal.emit(f"[拼接失败] 无法写入临时列表：{exc}")
            return False

        command = [
            self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'concat', '-safe', '0', '-i', concat_list_path,
        ]
        if duration is not None:
            command.extend(['-t', f"{float(duration):.3f}"])
        command.extend(['-c:v', 'copy', output_path])
        return self.run_ffmpeg(command, cwd=cache_dir)

    def _build_dissolve_fill(
        self,
        clips,
        output_path,
        duration,
        transition_duration,
        target_w,
        target_h,
        pix_fmt,
        enc_args,
        cache_dir,
        transition_enabled,
    ):
        if not transition_enabled or transition_duration <= 0 or len(clips) <= 1:
            return False
        inputs = [
            '-f', 'lavfi', '-i',
            f"color=c=black:s={target_w}x{target_h}:r=30:d={duration:.3f}",
        ]
        for clip in clips:
            inputs.extend(['-i', clip])
        filter_parts = ["[0:v]format=yuv420p,fps=30,settb=1/90000[fillbase0]"]
        last_label = "fillbase0"
        offset = 0.0
        for clip_index, clip in enumerate(clips, 1):
            clip_duration = max(0.05, self.get_duration(clip))
            fade_duration = min(
                transition_duration,
                max(0.05, clip_duration / 2.0 - 0.03),
            )
            fade_filter = (
                f",fade=t=in:st=0:d={fade_duration:.3f}:alpha=1"
                if clip_index > 1 and fade_duration > 0.05
                else ""
            )
            overlay_label = f"fillov{clip_index}"
            output_label = f"fillbase{clip_index}"
            filter_parts.append(
                f"[{clip_index}:v]setpts=PTS-STARTPTS,format=rgba{fade_filter},"
                f"setpts=PTS+{offset:.3f}/TB[{overlay_label}]"
            )
            filter_parts.append(
                f"[{last_label}][{overlay_label}]overlay=0:0:format=auto:eof_action=pass:"
                f"enable='between(t\\,{offset:.3f}\\,{min(duration, offset + clip_duration):.3f})'"
                f"[{output_label}]"
            )
            last_label = output_label
            offset += max(0.05, clip_duration - transition_duration)
        filter_parts.append(f"[{last_label}]format={pix_fmt},fps=30,settb=1/90000[fillout]")
        command = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error'] + inputs + [
            '-filter_complex', ';'.join(filter_parts),
            '-map', '[fillout]',
            '-t', f"{duration:.3f}",
            *enc_args, '-an', '-r', '30', '-pix_fmt', pix_fmt, output_path,
        ]
        return (
            self.run_ffmpeg(command, cwd=cache_dir)
            and os.path.exists(output_path)
            and os.path.getsize(output_path) > 1024
        )

    def _resolve_watermarks(self, watermarks):
        resolved = []
        for watermark in watermarks or []:
            if not isinstance(watermark, dict):
                continue
            anim_key = watermark.get('anim', '静态展示')
            if anim_key == "从右向左滚动":
                anim_key = "➡️ 向左匀速滚动 (速100)"
            elif anim_key == "上下浮动呼吸":
                anim_key = "🌊 上下平滑呼吸 (幅30_频3)"
            elif anim_key == "透明度闪烁":
                anim_key = "💡 平滑呼吸闪烁 (频4)"
            if anim_key == "🎲 随机动态特效":
                choices = [
                    key for key in ANIMATIONS
                    if key not in ["静态展示", "🎲 随机动态特效"]
                ]
                anim_key = random.choice(choices) if choices else "静态展示"

            color = (
                f"#{random.randint(0, 0xFFFFFF):06x}"
                if watermark.get('color') == "🎲 随机颜色"
                else watermark.get('color', 'white')
            )
            border_color = (
                f"#{random.randint(0, 0xFFFFFF):06x}"
                if watermark.get('border_color', 'black') == "🎲 随机颜色"
                else watermark.get('border_color', 'black')
            )
            text = watermark.get('text', '')
            if watermark.get('use_text_pool'):
                text_pool = self.split_watermark_text_pool(watermark.get('text_pool', ''))
                if not text_pool:
                    continue
                text = random.choice(text_pool)
            if not str(text).strip():
                continue
            font_name = watermark.get('font_name', '')
            font_path = watermark.get('font_path', '')
            if font_name == "🎲 随机系统字体":
                font_path = self._random_drawtext_font(text)
            else:
                font_path = self._safe_font_path(font_path, text)
            self._log_watermark_font_choice(font_path, font_name)
            if not font_path:
                continue
            resolved.append({
                'original': watermark,
                'anim_key': anim_key,
                'actual_font_path': font_path,
                'actual_color': color,
                'actual_border_color': border_color,
                'actual_text': text,
                'offset_x': random.randint(-15, 15),
                'offset_y': random.randint(-15, 15),
            })
        return resolved

    def _plan_distributed_avatar_windows(
        self,
        timeline_start,
        timeline_end,
        requested_count,
        planned_avatar_duration,
        min_product_gap,
        duration_resolver,
        force_first_avatar=False,
    ):
        """把数字人露出均匀打散到主时间轴，窗口外仍由产品层覆盖底轨。"""
        start = max(0.0, float(timeline_start or 0.0))
        end = max(start, float(timeline_end or 0.0))
        available = end - start
        if available <= 0.2:
            return []

        count = max(1, int(requested_count or 1))
        avatar_duration = max(0.2, float(planned_avatar_duration or 0.2))
        product_gap = max(0.2, float(min_product_gap or 0.2))
        while count > 1 and (
            count * avatar_duration + (count - 1) * product_gap > available
        ):
            count -= 1
        avatar_duration = min(
            avatar_duration,
            max(0.2, (available - (count - 1) * product_gap) / count),
        )
        required = count * avatar_duration + (count - 1) * product_gap
        free_time = max(0.0, available - required)

        gaps = [0.0] + [product_gap] * max(0, count - 1) + [0.0]
        starts_with_avatar = force_first_avatar or random.choice((True, False))
        if not starts_with_avatar and free_time >= 0.4:
            opening_gap = min(
                free_time,
                max(0.6, min(product_gap, free_time * 0.45)),
            )
            gaps[0] += opening_gap
            free_time -= opening_gap
        else:
            starts_with_avatar = True

        eligible_gaps = list(range(1 if starts_with_avatar else 0, count + 1))
        if free_time > 0.0 and eligible_gaps:
            weights = [random.uniform(0.7, 1.3) for _ in eligible_gaps]
            weight_total = sum(weights) or 1.0
            for gap_index, weight in zip(eligible_gaps, weights):
                gaps[gap_index] += free_time * weight / weight_total

        windows = []
        cursor = start + gaps[0]
        for index in range(count):
            if cursor >= end - 0.1:
                break
            resolved_duration = max(0.2, float(duration_resolver(cursor) or 0.2))
            duration = min(avatar_duration, resolved_duration, end - cursor)
            if duration <= 0.1:
                break
            avatar_end = cursor + duration
            windows.append((cursor, avatar_end))
            cursor = avatar_end
            if index + 1 < count:
                cursor += gaps[index + 1]
        return windows

    def _build_traditional_clip_plan(
        self,
        video_pool,
        hook_videos,
        second_segment_videos,
        audio_dur,
        hook_duration,
        segment_planner,
        render_sampler,
        trans_dur,
        use_trans,
        unique_id,
    ):
        current_time = 0.0
        clip_plan = []
        if hook_duration > 0 and hook_videos:
            hook_plan = segment_planner.choose_media_segment(
                hook_videos,
                min(hook_duration, max(0.0, audio_dur)),
                "hook",
                allow_loop=True,
            )
            if hook_plan:
                hook_plan['global_start'] = 0.0
                hook_plan['fade_out'] = False
                clip_plan.append(hook_plan)
                current_time = hook_plan['duration']
                self.log_signal.emit(
                    f"[{unique_id}] 第一段钩子：从素材 {hook_plan.get('start', 0.0):.1f} 秒处"
                    f"随机截取 {hook_plan['duration']:.1f} 秒。"
                )

        # 指定第二段但没有可用钩子时，先用普通视频补齐第一段，保证位置语义不变。
        if not clip_plan and second_segment_videos and current_time < audio_dur:
            first_plan = segment_planner.choose_random_product_segment(
                video_pool,
                0.0,
                audio_dur,
                "product",
            )
            if first_plan:
                first_plan['fade_out'] = False
                clip_plan.append(first_plan)
                current_time = float(first_plan.get('duration', 0.0) or 0.0)
                self.log_signal.emit(
                    f"[{unique_id}] 未选择可用钩子，第一段已由普通视频素材补位。"
                )

        if second_segment_videos and current_time < audio_dur:
            overlap = trans_dur if (use_trans and clip_plan) else 0.0
            second_plan = segment_planner.choose_random_product_segment(
                second_segment_videos,
                current_time,
                audio_dur - current_time + overlap,
                "second_segment",
            )
            if second_plan:
                progress = max(0.05, float(second_plan['duration']) - overlap)
                second_plan['fade_out'] = progress >= audio_dur - current_time
                clip_plan.append(second_plan)
                current_time += progress
                self.log_signal.emit(
                    f"[{unique_id}] 第二段指定素材：{os.path.basename(second_plan.get('source_video', ''))}，"
                    f"从 {second_plan.get('start', 0.0):.1f} 秒处随机截取。"
                )
            else:
                self.log_signal.emit(
                    f"[{unique_id}] ⚠️ 第二段指定目录没有可用素材，已自动回退普通视频素材。"
                )

        while current_time < audio_dur and self.is_running:
            overlap = trans_dur if (use_trans and clip_plan) else 0.0
            gap = audio_dur - current_time
            requested_duration = gap + overlap
            if requested_duration < 0.5:
                # 转场会留下不足半秒的尾差，直接并入上一段，避免为不可渲染尾差反复遍历素材池。
                if clip_plan and gap > 0.001:
                    clip_plan[-1]['duration'] = (
                        float(clip_plan[-1].get('duration', 0.0) or 0.0) + gap
                    )
                    clip_plan[-1]['loop'] = True
                    clip_plan[-1]['fade_out'] = True
                    current_time = audio_dur
                break
            plan = segment_planner.choose_random_product_segment(
                video_pool,
                current_time,
                requested_duration,
                "product",
            )
            if not plan:
                self.log_signal.emit(
                    f"[{unique_id}] ⚠️ 剩余 {gap:.2f} 秒未找到可用素材，"
                    "已停止继续挑选，避免任务空转。"
                )
                break
            clip_duration = float(plan.get('duration', 0.0) or 0.0)
            progress = max(0.05, clip_duration - overlap)
            if progress >= gap:
                plan['duration'] = gap + overlap
                progress = gap
                plan['fade_out'] = True
            else:
                plan['fade_out'] = False
            clip_plan.append(plan)
            current_time += progress
        return clip_plan

    def _render_traditional_clips(
        self,
        clip_plan,
        cache_dir,
        unique_id,
        render_sampler,
        filter_builder,
        anti_dedup,
        use_trans,
        enc_args,
        pix_fmt,
    ):
        rendered = []
        for index, plan in enumerate(clip_plan):
            if not self.is_running:
                return None
            output_path = os.path.join(cache_dir, f"temp_{unique_id}_clip_{index}.ts")
            zoom_plan = render_sampler.sample_zoom_plan()
            offset_x, offset_y = render_sampler.sample_material_offset(
                zoom_plan.get('zoom_start', zoom_plan['zoom'])
            )
            video_filter = filter_builder.fit(
                zoom_plan['zoom'],
                offset_x,
                offset_y,
                duration=plan['duration'],
                zoom_start=zoom_plan.get('zoom_start'),
                zoom_end=zoom_plan.get('zoom_end'),
                zoom_mode=zoom_plan.get('zoom_mode'),
                source_path=plan.get('video', ''),
            )
            speed = 1.0
            if anti_dedup:
                speed = round(random.uniform(0.95, 1.05), 3)
                video_filter += f",setpts={1.0 / speed}*PTS"
                brightness = round(random.uniform(-0.012, 0.012), 3)
                contrast = round(random.uniform(0.99, 1.02), 3)
                saturation = round(random.uniform(1.00, 1.04), 3)
                video_filter += (
                    f",eq=brightness={brightness}:contrast={contrast}:saturation={saturation}"
                )
            if plan.get('fade_out') and not use_trans and plan['duration'] > 1.5:
                video_filter += f",fade=t=out:st={plan['duration'] - 0.5:.3f}:d=0.5"
            video_filter += f",format={pix_fmt},fps=30,settb=1/90000"

            input_args = []
            if plan.get('loop'):
                input_args.extend(['-stream_loop', '-1'])
            input_args.extend(['-ss', f"{plan['start']:.3f}", '-i', plan['video']])
            command = [
                self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                *input_args, '-t', f"{plan['duration'] * speed:.3f}",
                '-vf', video_filter, *enc_args, '-an', '-r', '30',
                '-pix_fmt', pix_fmt, output_path,
            ]
            if not self.run_ffmpeg(command, cwd=cache_dir):
                return None
            rendered.append(output_path)
        return rendered

    def _assemble_traditional_video(
        self,
        concat_list,
        main_video,
        cache_dir,
        unique_id,
        use_trans,
        trans_dur,
        transition_selector,
        enc_args,
        pix_fmt,
        transition_temp_files,
    ):
        if use_trans and len(concat_list) > 1 and float(trans_dur or 0.0) > 0:
            transition_output = os.path.join(cache_dir, f"trans_{unique_id}.ts")
            transition_temp_files.append(transition_output)
            stage_output = self._merge_xfade_hierarchy(
                concat_list,
                transition_output,
                trans_dur,
                transition_selector,
                enc_args,
                pix_fmt,
                cache_dir,
                f"trans_{unique_id}_l{{level}}_g{{group_index}}.ts",
                transition_temp_files,
            )
            if stage_output:
                copy_command = [
                    self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                    '-i', stage_output, '-c:v', 'copy', main_video,
                ]
                if self.run_ffmpeg(copy_command, cwd=cache_dir):
                    return True

        concat_path = os.path.join(cache_dir, f"concat_{unique_id}.txt")
        return self._concat_video_clips(
            concat_list,
            concat_path,
            main_video,
            cache_dir,
        )

    def _build_strong_product_fill_video(
        self,
        start_time,
        duration,
        video_pool,
        trans_dur,
        use_trans,
        segment_planner,
        render_sampler,
        filter_builder,
        anti_dedup,
        pix_fmt,
        enc_args,
        cache_dir,
        unique_id,
        target_w,
        target_h,
        transition_selector,
        transition_temp_files,
    ):
        duration = max(0.0, float(duration or 0.0))
        if duration <= 0.2:
            return None
        self.log_signal.emit(
            f"[{unique_id}] 正在生成强结构后半段产品接管层（约 {duration:.1f} 秒）..."
        )
        fill_clips = []
        fill_cursor = 0.0
        clip_index = 0
        while self.is_running and fill_cursor < duration - 0.05:
            remaining = duration - fill_cursor
            overlap = trans_dur if (use_trans and fill_clips) else 0.0
            plan = segment_planner.choose_random_product_segment(
                video_pool,
                start_time + fill_cursor,
                remaining + overlap,
                "product_fill_build",
            )
            if not plan:
                fallback_duration = min(
                    remaining + overlap,
                    random.uniform(2.0, 4.0),
                )
                plan = segment_planner.choose_media_segment(
                    video_pool,
                    fallback_duration,
                    "product_fill_loop",
                    allow_loop=True,
                )
            if not plan:
                break
            clip_duration = min(
                float(plan.get('duration', 0.0) or 0.0),
                remaining + overlap,
            )
            if clip_duration <= 0.2:
                break
            temp_clip = os.path.join(
                cache_dir,
                f"strong_fill_{unique_id}_{clip_index}.ts",
            )
            zoom_plan = render_sampler.sample_zoom_plan()
            offset_x, offset_y = render_sampler.sample_material_offset(
                zoom_plan.get('zoom_start', zoom_plan['zoom'])
            )
            video_filter = filter_builder.fit(
                zoom_plan['zoom'],
                offset_x,
                offset_y,
                duration=clip_duration,
                zoom_start=zoom_plan.get('zoom_start'),
                zoom_end=zoom_plan.get('zoom_end'),
                zoom_mode=zoom_plan.get('zoom_mode'),
                source_path=plan.get('video', ''),
            )
            speed = 1.0
            if anti_dedup:
                speed = round(random.uniform(0.96, 1.04), 3)
                video_filter += f",setpts={1.0 / speed}*PTS"
                brightness = round(random.uniform(-0.012, 0.012), 3)
                contrast = round(random.uniform(0.99, 1.02), 3)
                saturation = round(random.uniform(1.00, 1.04), 3)
                video_filter += (
                    f",eq=brightness={brightness}:contrast={contrast}:saturation={saturation}"
                )
            video_filter += f",format={pix_fmt},fps=30,settb=1/90000"
            loop_args = ['-stream_loop', '-1'] if plan.get('loop') else []
            command = [
                self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                *loop_args, '-ss', f"{plan['start']:.3f}", '-i', plan['video'],
                '-t', f"{clip_duration * speed:.3f}",
                '-vf', video_filter, *enc_args, '-an', '-r', '30',
                '-pix_fmt', pix_fmt, temp_clip,
            ]
            if not self.run_ffmpeg(command, cwd=cache_dir):
                break
            fill_clips.append(temp_clip)
            fill_cursor += max(0.05, clip_duration - overlap)
            clip_index += 1

        if not fill_clips:
            return None
        if len(fill_clips) == 1:
            return fill_clips[0]

        fill_output = os.path.join(cache_dir, f"strong_fill_mix_{unique_id}.ts")
        fallback_concat = False
        fill_trans_dur = (
            min(0.8, max(0.45, float(trans_dur or 0.0)))
            if use_trans else 0.0
        )
        if use_trans:
            min_fill_duration = min(
                max(0.05, self.get_duration(clip)) for clip in fill_clips
            )
            fill_trans_dur = min(
                fill_trans_dur,
                max(0.08, min_fill_duration / 2.0 - 0.03),
            )
        if use_trans and trans_dur > 0:
            stage_output = self._merge_xfade_hierarchy(
                fill_clips,
                fill_output,
                fill_trans_dur,
                transition_selector,
                enc_args,
                pix_fmt,
                cache_dir,
                f"strong_fill_{unique_id}_mix_l{{level}}_g{{group_index}}.ts",
                transition_temp_files,
            )
            fallback_concat = not bool(stage_output)
            if stage_output:
                if stage_output != fill_output:
                    try:
                        shutil.copyfile(stage_output, fill_output)
                    except Exception:
                        fallback_concat = True
                fallback_concat = fallback_concat or not (
                    os.path.exists(fill_output) and os.path.getsize(fill_output) > 1024
                )
        else:
            fallback_concat = True

        if fallback_concat:
            if len(fill_clips) <= 6 and self._build_dissolve_fill(
                fill_clips,
                fill_output,
                duration,
                fill_trans_dur,
                target_w,
                target_h,
                pix_fmt,
                enc_args,
                cache_dir,
                use_trans,
            ):
                self.log_signal.emit(f"[{unique_id}] 后半段产品层已启用稳定叠化转场。")
                return fill_output
            if len(fill_clips) > 6:
                self.log_signal.emit(
                    f"[{unique_id}] ⚠️ 大批量转场节点已降级为稳定直拼，避免高内存滤镜拖垮任务。"
                )
            concat_path = os.path.join(
                cache_dir,
                f"strong_fill_concat_{unique_id}.txt",
            )
            if not self._concat_video_clips(
                fill_clips,
                concat_path,
                fill_output,
                cache_dir,
                duration=duration,
            ):
                return fill_clips[0]
        return fill_output if os.path.exists(fill_output) else fill_clips[0]

    def _prepend_ai_intro_video(
        self,
        main_video,
        ai_intro_path,
        ai_intro_duration,
        cached_source_getter,
        filter_builder,
        enc_args,
        pix_fmt,
        cache_dir,
        unique_id,
        audio_row,
    ):
        intro_source = cached_source_getter(ai_intro_path)
        intro_video = os.path.join(cache_dir, f"ai_intro_{unique_id}.ts")
        intro_filter = (
            f"{filter_builder.fit(source_path=intro_source)},setpts=PTS-STARTPTS,"
            f"format={pix_fmt},fps=30,settb=1/90000"
        )
        intro_command = [
            self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
            '-i', intro_source, '-t', f"{ai_intro_duration:.3f}",
            '-vf', intro_filter, *enc_args, '-an', '-r', '30',
            '-pix_fmt', pix_fmt, intro_video,
        ]
        if not self.run_ffmpeg(intro_command, cwd=cache_dir):
            self.status_update_signal.emit(audio_row, "❌ AI开场处理失败", "red")
            self.log_signal.emit(
                f"[{unique_id}] ❌ AI开场标准化失败，未输出缺少开场的降级视频。"
            )
            return None, intro_video, "", ""

        concat_path = os.path.join(cache_dir, f"ai_intro_concat_{unique_id}.txt")
        try:
            with open(concat_path, 'w', encoding='utf-8') as handle:
                safe_intro_path = str(intro_video).replace('\\', '/')
                safe_main_path = str(main_video).replace('\\', '/')
                handle.write(f"file '{safe_intro_path}'\n")
                handle.write(f"file '{safe_main_path}'\n")
        except Exception as exc:
            self.status_update_signal.emit(audio_row, "❌ AI开场衔接失败", "red")
            self.log_signal.emit(f"[{unique_id}] ❌ AI开场临时列表写入失败：{exc}")
            return None, intro_video, concat_path, ""

        prefixed_video = os.path.join(cache_dir, f"ai_intro_final_{unique_id}.ts")
        prepend_command = [
            self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'concat', '-safe', '0', '-i', concat_path,
            '-c:v', 'copy', prefixed_video,
        ]
        if not self.run_ffmpeg(prepend_command, cwd=cache_dir):
            self.log_signal.emit(
                f"[{unique_id}] AI开场直拼失败，正在使用稳定重编码方式重新衔接。"
            )
            fallback_command = [
                self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                '-i', intro_video, '-i', main_video,
                '-filter_complex',
                (
                    "[0:v]setpts=PTS-STARTPTS,fps=30,settb=1/90000[v0];"
                    "[1:v]setpts=PTS-STARTPTS,fps=30,settb=1/90000[v1];"
                    "[v0][v1]concat=n=2:v=1:a=0[vout]"
                ),
                '-map', '[vout]', *enc_args, '-an', '-r', '30',
                '-pix_fmt', pix_fmt, prefixed_video,
            ]
            if not self.run_ffmpeg(fallback_command, cwd=cache_dir):
                self.status_update_signal.emit(audio_row, "❌ AI开场衔接失败", "red")
                self.log_signal.emit(
                    f"[{unique_id}] ❌ AI开场无法与原混剪画面衔接，当前任务停止。"
                )
                return None, intro_video, concat_path, prefixed_video
        self.log_signal.emit(
            f"[{unique_id}] ✅ AI开场已完整接入主内容之前。"
        )
        return prefixed_video, intro_video, concat_path, prefixed_video

    def _mux_final_output(
        self,
        final_video,
        final_output,
        temp_output,
        audio_path,
        audio_duration,
        bgm_pool,
        main_volume,
        bgm_volume,
        ai_intro_mode,
        ai_intro_path,
        ai_intro_duration,
        final_ass_path,
        sub_config,
        color_normalize_filter,
        final_enc_args,
        pix_fmt,
        cache_dir,
        unique_id,
        audio_row,
        round_idx,
        total_rounds,
        target_w,
        target_h,
        smart_sfx_events,
        resume_job_key_value="",
    ):
        mix_inputs = ['-i', final_video]
        current_input_count = 1
        ai_audio_input_idx = -1
        ai_intro_silent_audio = ""
        if ai_intro_mode:
            if self._has_audio_stream(ai_intro_path):
                mix_inputs.extend(['-i', ai_intro_path])
            else:
                self.log_signal.emit(
                    f"[{unique_id}] ⚠️ AI开场视频无音频流，自动补充等长静音，画面仍完整播放。"
                )
                ai_intro_silent_audio = os.path.join(
                    cache_dir,
                    f"ai_intro_silent_{unique_id}.aac",
                )
                self._generate_silent_audio(ai_intro_silent_audio, ai_intro_duration)
                mix_inputs.extend(['-i', ai_intro_silent_audio])
            ai_audio_input_idx = current_input_count
            current_input_count += 1

        if self._has_audio_stream(audio_path):
            mix_inputs.extend(['-i', audio_path])
            audio_input_idx = current_input_count
            current_input_count += 1
        else:
            self.log_signal.emit(
                f"[{unique_id}] ⚠️ 第一轨道视频无音频流，自动生成静音轨({audio_duration:.1f}s)"
            )
            silent_audio = os.path.join(cache_dir, f"silent_{unique_id}.aac")
            self._generate_silent_audio(silent_audio, audio_duration)
            mix_inputs.extend(['-i', silent_audio])
            audio_input_idx = current_input_count
            current_input_count += 1

        bgm_input_idx = -1
        if bgm_pool:
            mix_inputs.extend(['-stream_loop', '-1', '-i', random.choice(bgm_pool)])
            bgm_input_idx = current_input_count
            current_input_count += 1

        indexed_sfx_events = []
        for event in smart_sfx_events or []:
            if not os.path.isfile(str(event.path or "")):
                continue
            mix_inputs.extend(['-i', event.path])
            indexed_sfx_events.append((current_input_count, event))
            current_input_count += 1

        mix_command = [
            self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
            *mix_inputs,
        ]
        base_audio_label = "base_audio" if indexed_sfx_events else "aout"
        if ai_intro_mode:
            audio_filters = [
                (
                    f"[{ai_audio_input_idx}:a]atrim=duration={ai_intro_duration:.3f},"
                    "asetpts=PTS-STARTPTS,aresample=44100,"
                    "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[ai_opening]"
                ),
                (
                    f"[{audio_input_idx}:a]volume={main_volume},"
                    f"atrim=duration={audio_duration:.3f},"
                    "asetpts=PTS-STARTPTS,aresample=44100,"
                    "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[main_voice]"
                ),
            ]
            main_audio_label = "main_voice"
            if bgm_input_idx != -1:
                audio_filters.extend([
                    (
                        f"[{bgm_input_idx}:a]volume={bgm_volume},"
                        f"atrim=duration={audio_duration:.3f},"
                        "asetpts=PTS-STARTPTS,aresample=44100,"
                        "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[main_bgm]"
                    ),
                    (
                        "[main_voice][main_bgm]amix=inputs=2:duration=first:"
                        "dropout_transition=3[main_mix]"
                    ),
                ])
                main_audio_label = "main_mix"
            audio_filters.append(
                f"[ai_opening][{main_audio_label}]concat=n=2:v=0:a=1"
                f"[{base_audio_label}]"
            )
            if indexed_sfx_events:
                audio_filters.extend(
                    build_sfx_mix_filters(
                        base_audio_label,
                        indexed_sfx_events,
                        44100,
                        "aout",
                    )
                )
            mix_command.extend([
                '-filter_complex', ';'.join(audio_filters),
                '-map', '0:v:0', '-map', '[aout]',
            ])
        elif bgm_input_idx != -1:
            audio_filter = (
                f"[{audio_input_idx}:a]volume={main_volume}[a1];"
                f"[{bgm_input_idx}:a]volume={bgm_volume}[a2];"
                "[a1][a2]amix=inputs=2:duration=first:dropout_transition=3"
                f"[{base_audio_label}]"
            )
            audio_filters = [audio_filter]
            if indexed_sfx_events:
                audio_filters.extend(
                    build_sfx_mix_filters(
                        base_audio_label,
                        indexed_sfx_events,
                        44100,
                        "aout",
                    )
                )
            mix_command.extend([
                '-filter_complex', ';'.join(audio_filters),
                '-map', '0:v:0', '-map', '[aout]',
            ])
        elif indexed_sfx_events:
            audio_filters = [
                (
                    f"[{audio_input_idx}:a]volume={main_volume},"
                    f"atrim=duration={audio_duration:.3f},"
                    "asetpts=PTS-STARTPTS,aresample=44100,"
                    "aformat=sample_fmts=fltp:sample_rates=44100:"
                    "channel_layouts=stereo[base_audio]"
                )
            ]
            audio_filters.extend(
                build_sfx_mix_filters(
                    "base_audio",
                    indexed_sfx_events,
                    44100,
                    "aout",
                )
            )
            mix_command.extend([
                '-filter_complex', ';'.join(audio_filters),
                '-map', '0:v:0', '-map', '[aout]',
            ])
        else:
            mix_command.extend([
                '-map', '0:v:0', '-map', f'{audio_input_idx}:a:0',
                '-af', f"volume={main_volume}",
            ])

        platform_video_filter = (
            f"setsar=1,setdar=dar={int(target_w)}/{int(target_h)}"
        )
        mix_command_base = list(mix_command) + ['-vf', platform_video_filter]
        mix_command = list(mix_command_base)
        subtitle_added = False
        if final_ass_path and os.path.exists(final_ass_path):
            subtitle_filter = self.build_subtitle_filter(final_ass_path)
            self.log_signal.emit(f"✅ 智能字幕文件已生成，准备烧录：{final_ass_path}")
            mix_command = list(mix_command[:-2])
            mix_command.extend([
                '-vf',
                f"{color_normalize_filter},{platform_video_filter},{subtitle_filter}",
            ])
            subtitle_added = True
        elif sub_config.get('enable'):
            self.log_signal.emit(
                "⚠️ 已开启智能字幕，但没有生成可用字幕文件，本条视频将不烧录字幕。"
            )

        suffix = [
            *final_enc_args, '-pix_fmt', pix_fmt,
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            '-r', '30',
            '-aspect', f"{int(target_w)}:{int(target_h)}",
            '-metadata:s:v:0', 'rotate=0',
            '-video_track_timescale', '30000',
            '-shortest', '-avoid_negative_ts', 'make_zero',
            '-movflags', '+faststart', temp_output,
        ]
        mix_command.extend(suffix)
        action = "烧录字幕并封装最终视频" if subtitle_added else "封装音频到最终视频"
        self.log_signal.emit(f"[{unique_id}] 正在{action}...")
        mix_ok = self.run_ffmpeg(mix_command, cwd=cache_dir)
        if not mix_ok and subtitle_added:
            self.log_signal.emit(
                f"[{unique_id}] ⚠️ 字幕烧录节点失败，自动重试无字幕封装，保证先输出成片。"
            )
            try:
                if os.path.exists(temp_output):
                    os.remove(temp_output)
            except Exception:
                pass
            mix_ok = self.run_ffmpeg(mix_command_base + suffix, cwd=cache_dir)

        if mix_ok and os.path.exists(temp_output) and os.path.getsize(temp_output) > 1024:
            compliance_issues, compliance = self._validate_platform_output(
                temp_output,
                target_w,
                target_h,
            )
            if compliance_issues:
                mix_ok = False
                self.status_update_signal.emit(audio_row, "❌ 平台规格异常", "red")
                self.log_signal.emit(
                    f"[{unique_id}] ❌ 成品平台合规检查未通过："
                    + "；".join(compliance_issues)
                )
                try:
                    os.remove(temp_output)
                except OSError:
                    pass
            else:
                shutil.move(temp_output, final_output)
                self.log_signal.emit(
                    f"[{unique_id}] ✅ 平台规格检查通过："
                    f"{compliance.get('width')}x{compliance.get('height')}，"
                    f"SAR {compliance.get('sar')}，H.264/AAC，"
                    f"{compliance.get('duration', 0.0):.2f} 秒。"
                )
                if self._resume_store and resume_job_key_value:
                    try:
                        self._resume_store.mark_completed(
                            resume_job_key_value,
                            final_output,
                            source_path=audio_path,
                            round_idx=round_idx,
                        )
                    except OSError as exc:
                        self.log_signal.emit(
                            f"[{unique_id}] ⚠️ 成片已完成，但断点记录写入失败：{exc}"
                        )
                self.log_signal.emit(f"[{unique_id}] ✅ 已成功封装输出：{final_output}\n")
                self._asset_usage_scheduler.finish_job(unique_id, successful=True)
                self.task_done_signal.emit()
                if round_idx == total_rounds:
                    self.status_update_signal.emit(audio_row, "✅ 全部完成", "green")
        elif mix_ok:
            self.status_update_signal.emit(audio_row, "❌ 生成异常", "red")
            self.log_signal.emit(f"[{unique_id}] ❌ 封装失败：文件生成异常！")
        else:
            self.status_update_signal.emit(audio_row, "❌ 封装失败", "red")
            self.log_signal.emit(f"[{unique_id}] ❌ 混合封装过程发生崩溃！")
        return ai_intro_silent_audio

    def process_single_audio(self, audio_dict, video_pool, bgm_pool, min_clip, max_clip, gpu_mode, 
                             target_res, trans_type, trans_dur, cache_dir, output_dir, covers, watermarks, 
                             round_idx, total_rounds, main_vol, bgm_vol):
        if not self.is_running:
            return

        task_config = self.task_config
        job_ctx = self._build_audio_job_context(audio_dict, round_idx)
        audio_path = job_ctx.audio_path
        audio_row = job_ctx.audio_row
        audio_name = job_ctx.audio_name
        output_stamp = job_ctx.output_stamp
        salt = job_ctx.salt
        unique_id = job_ctx.unique_id
        if not audio_path:
            self.status_update_signal.emit(audio_row, "❌ 音频缺失", "red")
            self.log_signal.emit(f"[{unique_id}] ❌ 当前任务缺少音频路径，已跳过。")
            return
        
        self.log_signal.emit(f"[{unique_id}] ▶ 第 {round_idx} 轮混剪开始...")
        self.status_update_signal.emit(audio_row, "处理中...", "#60A5FA")
        
        final_ass_path = ""
        sub_config = task_config.sub_config
        ai_intro_mode = task_config.ai_intro_mode
        ai_intro_path = str(audio_dict.get('ai_intro_path', '') or '')
        ai_intro_duration = 0.0
        if ai_intro_mode:
            if not ai_intro_path or not os.path.exists(ai_intro_path):
                self.status_update_signal.emit(audio_row, "❌ AI开场缺失", "red")
                self.log_signal.emit(f"[{unique_id}] ❌ 未分配到可用的AI开场视频，当前任务停止。")
                return
            ai_intro_duration = self.get_duration(ai_intro_path)
            if ai_intro_duration <= 0.2:
                self.status_update_signal.emit(audio_row, "❌ AI开场异常", "red")
                self.log_signal.emit(f"[{unique_id}] ❌ AI开场视频时长读取失败：{ai_intro_path}")
                return
            self.log_signal.emit(
                f"[{unique_id}] AI完整开场：{os.path.basename(ai_intro_path)}，"
                f"完整播放 {ai_intro_duration:.2f} 秒；钩子素材已忽略。"
            )
        has_cover = bool(covers)
        # 封面帧时长（2帧 @30fps ≈ 0.067s），用于字幕时间偏移，确保封面文字与视频字幕严格隔离
        cover_duration = 0.067 if has_cover else 0.0
        if sub_config.get('enable'):
            sys_fonts = task_config.sys_fonts
            timeline_offset = ai_intro_duration + cover_duration
            final_ass_path = self.process_asr_and_color(
                audio_path,
                cache_dir,
                audio_name,
                sub_config,
                sys_fonts,
                gpu_mode,
                timeline_offset,
            )
            
        audio_dur = self.get_duration(audio_path)
        smart_sfx_candidates = []
        if task_config.smart_sfx_enabled and ai_intro_mode:
            smart_sfx_candidates.append(
                {"time": 0.0, "kind": "intro", "priority": 3}
            )
        use_trans = trans_type != "不使用转场"
        target_w_int, target_h_int = parse_target_resolution(
            target_res,
            (task_config.target_w, task_config.target_h),
        )
        encoder_profile = self._build_encoder_profile(
            gpu_mode,
            target_w_int,
            target_h_int,
        )
        pix_fmt = encoder_profile.pix_fmt
        enc_args = encoder_profile.enc_args
        final_enc_args = encoder_profile.final_enc_args
            
        anti_dedup = task_config.anti_dedup
        first_track_videos = self.task.first_track_videos
        second_segment_videos = [
            item.get('path')
            for item in self.task.second_segment_videos
            if item.get('path') and os.path.exists(item.get('path'))
        ]
        use_first_track_audio = task_config.use_first_track_audio
        # AI开场模式也保留钩子素材：AI视频播完后，钩子会落在主内容时间轴开头。
        hook_videos = list(self.task.hook_videos or [])
        strong_structure = task_config.strong_structure
        hook_duration = task_config.hook_duration
        strong_avatar_duration = task_config.strong_avatar_duration
        strong_avatar_smart_tail = task_config.strong_avatar_smart_tail
        strong_product_duration = task_config.strong_product_duration
        strong_avatar_count = task_config.strong_avatar_count
        clip_rhythm = task_config.clip_rhythm
        clip_jitter = task_config.clip_jitter
        overlay_gap_min = task_config.overlay_gap_min
        overlay_gap_max = task_config.overlay_gap_max
        overlay_zoom_min = task_config.overlay_zoom_min
        overlay_zoom_max = task_config.overlay_zoom_max
        overlay_zoom_mode = task_config.overlay_zoom_mode
        overlay_offset_strength = task_config.overlay_offset_strength
        legacy_overlay_offset = task_config.legacy_overlay_offset
        if overlay_offset_strength is None:
            overlay_offset_strength = 0.0
        overlay_offset_strength = max(0.0, min(1.0, float(overlay_offset_strength or 0.0) / 100.0))
        target_w, target_h = str(target_w_int), str(target_h_int)
        color_normalize_filter = self._sdr_color_filter()
        hdr_tonemap_filter = self._hdr_to_sdr_filter()
        scale_quality = "flags=lanczos+accurate_rnd+full_chroma_int"
        try:
            lut_selection = resolve_lut_selection(task_config.lut_style)
        except Exception as exc:
            lut_selection = resolve_lut_selection("none")
            self.log_signal.emit(
                f"[{unique_id}] [LUT] 载入失败，已安全回退为保持原色：{exc}"
            )
        if lut_selection.enabled:
            self.log_signal.emit(
                f"[{unique_id}] [LUT] 本条成片使用：{lut_selection.label}。"
            )
        transition_selector = FastTransitionSelector.from_map(trans_type, TRANS_MAP)
        render_sampler = RenderSampler(
            target_w=target_w_int,
            target_h=target_h_int,
            zoom_min=overlay_zoom_min,
            zoom_max=overlay_zoom_max,
            zoom_mode=overlay_zoom_mode,
            offset_strength=overlay_offset_strength,
            legacy_offset=legacy_overlay_offset,
            clip_rhythm=clip_rhythm,
            min_clip=min_clip,
            max_clip=max_clip,
            clip_jitter=clip_jitter,
            overlay_gap_min=overlay_gap_min,
            overlay_gap_max=overlay_gap_max,
        )
        filter_builder = RenderFilterBuilder(
            target_w=target_w_int,
            target_h=target_h_int,
            color_normalize_filter=color_normalize_filter,
            scale_quality=scale_quality,
            is_standard_source=self._is_standard_cache_source,
            color_filter_for_source=lambda source_path: self._source_color_filter(
                source_path,
                hdr_tonemap_filter,
                color_normalize_filter,
            ),
            transition_enabled=use_trans,
            transition_duration=trans_dur,
            lut_filter=lut_selection.ffmpeg_filter,
        )

        def cached_video_source(source_path, normalize=True):
            return (
                self._cached_edit_source_video(source_path, target_w_int, target_h_int, normalize=normalize)
                if source_path else source_path
            )
        usage_candidates = list(video_pool)
        usage_candidates.extend(
            item.get('path')
            for item in hook_videos
            if isinstance(item, dict) and item.get('path')
        )
        usage_candidates.extend(second_segment_videos)
        self._asset_usage_scheduler.begin_job(
            unique_id,
            self.task.asset_scope,
            usage_candidates,
        )
        segment_planner = MediaSegmentPlanner(
            duration_getter=self.get_duration,
            cached_source_getter=cached_video_source,
            render_sampler=render_sampler,
            asset_selector=lambda candidates, source_type: self._asset_usage_scheduler.choose(
                candidates,
                source_type,
                unique_id,
            ),
            plan_recorder=lambda plan: self._asset_usage_scheduler.record_segment(
                unique_id,
                plan,
            ),
        )

        resolved_watermarks = self._resolve_watermarks(watermarks)
        
        main_v_no_audio = os.path.join(cache_dir, f"main_v_{unique_id}.ts")
        watermarked_main_v = ""
        concat_list = []
        transition_temp_files = []
        if first_track_videos:
            base_track = audio_path if use_first_track_audio else random.choice(first_track_videos)['path']
            base_track_video = cached_video_source(base_track, normalize=False)
            overlay_plan = []
            avatar_windows = []

            def avatar_exposure_duration(start_time):
                base = min(strong_avatar_duration, max(0.0, audio_dur - start_time))
                if not strong_avatar_smart_tail or base <= 0.05:
                    return base
                max_extra = min(0.85, max(0.0, audio_dur - start_time - base))
                if max_extra <= 0.05:
                    return base
                target_cut = start_time + base
                for pause_start in self._audio_silence_points(base_track):
                    if target_cut - 0.12 <= pause_start <= target_cut + max_extra:
                        return min(audio_dur - start_time, max(base, pause_start - start_time + 0.06))
                return base + min(0.45, max_extra)

            def append_overlay(plan, start_time):
                if not plan or start_time >= audio_dur:
                    return 0.0
                plan['global_start'] = max(0.0, start_time)
                plan['duration'] = min(float(plan.get('duration', 0.0) or 0.0), max(0.0, audio_dur - plan['global_start']))
                if plan['duration'] <= 0.05:
                    return 0.0
                if 'zoom' not in plan:
                    plan.update(render_sampler.sample_zoom_plan())
                if plan.get('prebuilt'):
                    plan['offset_x'], plan['offset_y'] = 0.0, 0.0
                    plan['eq_filter'] = ""
                else:
                    plan['offset_x'], plan['offset_y'] = render_sampler.sample_material_offset(plan.get('zoom_start', plan['zoom']))
                if anti_dedup and not plan.get('prebuilt'):
                    eq_b = round(random.uniform(-0.012, 0.012), 3)
                    eq_c = round(random.uniform(0.99, 1.02), 3)
                    eq_s = round(random.uniform(1.00, 1.04), 3)
                    plan['eq_filter'] = f",eq=brightness={eq_b}:contrast={eq_c}:saturation={eq_s}"
                elif not plan.get('prebuilt'):
                    plan['eq_filter'] = ""
                overlay_plan.append(plan)
                return plan['duration']

            hook_end = 0.0
            if hook_duration > 0 and (hook_videos or strong_structure):
                # AI开场模式下，钩子同样叠加在主内容时间轴开头（即AI视频播放完之后的第一个镜头）
                hook_plan = segment_planner.choose_media_segment(
                    hook_videos or video_pool,
                    min(hook_duration, audio_dur),
                    "hook",
                    allow_loop=True,
                )
                used = append_overlay(hook_plan, 0.0)
                hook_end = used
                if used <= 0:
                    self.log_signal.emit(f"[{unique_id}] ⚠️ 未找到可用钩子素材，开头钩子覆盖已跳过。")

            if strong_structure:
                planned_avatar_duration = strong_avatar_duration + (
                    0.45 if strong_avatar_smart_tail else 0.0
                )
                avatar_windows.extend(self._plan_distributed_avatar_windows(
                    hook_end,
                    audio_dur,
                    strong_avatar_count,
                    planned_avatar_duration,
                    strong_product_duration,
                    avatar_exposure_duration,
                    force_first_avatar=ai_intro_mode,
                ))
                product_windows = []
                product_cursor = hook_end
                for avatar_start, avatar_end in avatar_windows:
                    if avatar_start - product_cursor > 0.1:
                        product_windows.append((product_cursor, avatar_start))
                    product_cursor = max(product_cursor, avatar_end)
                if audio_dur - product_cursor > 0.1:
                    product_windows.append((product_cursor, audio_dur))

                for gap_index, (gap_start, gap_end) in enumerate(product_windows):
                    if not self.is_running:
                        return
                    gap_duration = max(0.0, gap_end - gap_start)
                    fill_video = (
                        self._build_strong_product_fill_video(
                            gap_start,
                            gap_duration,
                            video_pool,
                            trans_dur,
                            use_trans,
                            segment_planner,
                            render_sampler,
                            filter_builder,
                            anti_dedup,
                            pix_fmt,
                            enc_args,
                            cache_dir,
                            f"{unique_id}_gap{gap_index}",
                            target_w,
                            target_h,
                            transition_selector,
                            transition_temp_files,
                        )
                        if gap_duration > 0.2
                        else None
                    )
                    if fill_video:
                        append_overlay({
                            'video': fill_video,
                            'start': 0.0,
                            'duration': gap_duration,
                            'source_type': 'product_fill',
                            'loop': False,
                            'prebuilt': True,
                            'zoom': 1.0,
                            'eq_filter': '',
                        }, gap_start)
                    else:
                        self.log_signal.emit(
                            f"[{unique_id}] ⚠️ 第 {gap_index + 1} 个产品区间生成失败，"
                            "该区间将保留对应时间的数字人底轨。"
                        )
            else:
                if hook_end > 0.0:
                    # 有钩子时，产品覆盖从钩子结束后开始（AI开场模式下同样适用）
                    cursor = hook_end + render_sampler.sample_overlay_gap()
                elif ai_intro_mode:
                    cursor = render_sampler.sample_overlay_gap()
                else:
                    cursor = (
                        render_sampler.sample_overlay_gap()
                        if random.choice((True, False)) else 0.0
                    )
                while cursor < audio_dur:
                    if not self.is_running:
                        return
                    product_plan = segment_planner.choose_random_product_segment(
                        video_pool,
                        cursor,
                        audio_dur - cursor,
                        "product",
                    )
                    used = append_overlay(product_plan, cursor)
                    if used <= 0:
                        break
                    cursor += used + render_sampler.sample_overlay_gap()

            if task_config.smart_sfx_enabled:
                main_offset = ai_intro_duration
                for plan in overlay_plan:
                    source_type = str(plan.get('source_type') or 'product')
                    smart_sfx_candidates.append({
                        "time": main_offset + float(plan.get('global_start') or 0.0),
                        "kind": "hook" if source_type == "hook" else "product",
                        "priority": 3 if source_type == "hook" else 1,
                    })
                for avatar_start, _avatar_end in avatar_windows:
                    smart_sfx_candidates.append({
                        "time": main_offset + float(avatar_start),
                        "kind": "avatar",
                        "priority": 1,
                    })

            hook_count = len([p for p in overlay_plan if p.get('source_type') == 'hook'])
            product_count = len(overlay_plan) - hook_count
            if strong_structure:
                windows_text = "、".join(
                    f"{start:.1f}-{end:.1f}s" for start, end in avatar_windows
                ) or "无"
                self.log_signal.emit(
                    f"[{unique_id}] 第一轨道强结构模式：底轨 {os.path.basename(base_track)}，"
                    f"钩子 {hook_count} 段，产品覆盖 {product_count} 个区间，"
                    f"数字人按原音频时间随机露出 {len(avatar_windows)} 次（{windows_text}）。"
                )
            elif use_trans and overlay_plan:
                self.log_signal.emit(f"[{unique_id}] 第一轨道覆盖模式：底轨 {os.path.basename(base_track)}，钩子 {hook_count} 段，覆盖片段 {product_count} 段，已启用覆盖转场淡入淡出({trans_dur:.2f}s)。")
            else:
                self.log_signal.emit(f"[{unique_id}] 第一轨道覆盖模式：底轨 {os.path.basename(base_track)}，钩子 {hook_count} 段，覆盖片段 {product_count} 段。")
            overlay_cmd_inputs = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error', '-stream_loop', '-1', '-i', base_track_video]
            base_vf = filter_builder.avatar_base_fit(
                avatar_windows,
                strong_structure,
                source_path=base_track_video,
            )
            filter_parts = [f"[0:v]{base_vf},trim=duration={audio_dur:.3f},setpts=PTS-STARTPTS[base0]"]
            last_label = "base0"
            for idx, plan in enumerate(overlay_plan, 1):
                if plan.get('loop'):
                    overlay_cmd_inputs.extend(['-stream_loop', '-1'])
                overlay_cmd_inputs.extend(['-ss', f"{plan['start']:.3f}", '-t', f"{plan['duration']:.3f}", '-i', plan['video']])
                start_t = plan['global_start']
                end_t = start_t + plan['duration']
                ov_label = f"ov{idx}"
                out_label = f"base{idx}"
                fade_out = plan.get('fade_out_to_avatar', True)
                trans_filter = (
                    ""
                    if plan.get('prebuilt')
                    else filter_builder.overlay_transition(
                        plan['duration'],
                        fade_in=start_t > 0.05,
                        fade_out=fade_out,
                    )
                )
                if plan.get('prebuilt'):
                    ov_filter = f"{color_normalize_filter},setsar=1,format={pix_fmt},fps=30,settb=1/90000"
                else:
                    ov_filter = filter_builder.fit(
                        plan['zoom'],
                        plan.get('offset_x', 0.0),
                        plan.get('offset_y', 0.0),
                        duration=plan['duration'],
                        zoom_start=plan.get('zoom_start'),
                        zoom_end=plan.get('zoom_end'),
                        zoom_mode=plan.get('zoom_mode'),
                        source_path=plan.get('video', ''),
                    ) + plan.get('eq_filter', '')
                if trans_filter:
                    filter_parts.append(f"[{idx}:v]{ov_filter},trim=duration={plan['duration']:.3f},setpts=PTS-STARTPTS{trans_filter},setpts=PTS+{start_t:.3f}/TB[{ov_label}]")
                else:
                    filter_parts.append(f"[{idx}:v]{ov_filter},trim=duration={plan['duration']:.3f},setpts=PTS-STARTPTS+{start_t:.3f}/TB[{ov_label}]")
                filter_parts.append(
                    f"[{last_label}][{ov_label}]overlay=0:0:format=auto:eof_action=pass:"
                    f"enable='between(t\\,{start_t:.3f}\\,{end_t:.3f})'[{out_label}]"
                )
                last_label = out_label

            final_vf = f"[{last_label}]format={pix_fmt},fps=30,settb=1/90000"
            final_vf += self.generate_watermark_filter(
                resolved_watermarks,
                cache_dir,
                unique_id,
                time_offset=0.0,
                is_first_clip=True,
                target_w=target_w_int,
                target_h=target_h_int,
            )
            filter_parts.append(final_vf + "[vout]")
            overlay_filter_complex = ';'.join(filter_parts)
            overlay_cmd = overlay_cmd_inputs + [
                '-filter_complex', overlay_filter_complex,
                '-map', '[vout]', *enc_args, '-an', '-r', '30', '-pix_fmt', pix_fmt, main_v_no_audio
            ]
            if strong_structure:
                self.log_signal.emit(
                    f"[{unique_id}] 正在合成第一轨道覆盖层；这一步较长，进度条会保持在当前已完成条数。"
                )
            if not self.run_ffmpeg(overlay_cmd, cwd=cache_dir):
                stable_overlay_filter = re.sub(r",format=(?:yuva420p|rgba)", "", overlay_filter_complex)
                stable_overlay_filter = re.sub(
                    r",fade=t=(?:in|out):st=[0-9.]+:d=[0-9.]+:alpha=1",
                    "",
                    stable_overlay_filter,
                )
                stable_overlay_cmd = overlay_cmd_inputs + [
                    '-filter_complex', stable_overlay_filter,
                    '-map', '[vout]', *enc_args, '-an', '-r', '30', '-pix_fmt', pix_fmt, main_v_no_audio
                ]
                if self.run_ffmpeg(stable_overlay_cmd, cwd=cache_dir):
                    self.log_signal.emit(
                        f"[{unique_id}] 覆盖层颜色格式不兼容，已保留完整结构并改用稳定硬切转场。"
                    )
                else:
                    self.log_signal.emit(f"[{unique_id}] ⚠️ 覆盖混剪节点失败，启用第一轨道保底渲染，避免整条任务无输出。")
                    base_filter = f"{base_vf},trim=duration={audio_dur:.3f},setpts=PTS-STARTPTS,format={pix_fmt},fps=30,settb=1/90000"
                    base_filter += self.generate_watermark_filter(
                        resolved_watermarks,
                        cache_dir,
                        unique_id,
                        time_offset=0.0,
                        is_first_clip=True,
                        target_w=target_w_int,
                        target_h=target_h_int,
                    )
                    fallback_cmd = [
                        self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                        '-stream_loop', '-1', '-i', base_track_video,
                        '-vf', base_filter,
                        '-t', f"{audio_dur:.3f}", *enc_args, '-an', '-r', '30', '-pix_fmt', pix_fmt, main_v_no_audio
                    ]
                    if not self.run_ffmpeg(fallback_cmd, cwd=cache_dir):
                        return
        else:
            clip_plan = self._build_traditional_clip_plan(
                video_pool,
                hook_videos,
                second_segment_videos,
                audio_dur,
                hook_duration,
                segment_planner,
                render_sampler,
                trans_dur,
                use_trans,
                unique_id,
            )
            if task_config.smart_sfx_enabled:
                main_offset = ai_intro_duration
                for plan in clip_plan:
                    source_type = str(plan.get('source_type') or 'product')
                    smart_sfx_candidates.append({
                        "time": main_offset + float(plan.get('global_start') or 0.0),
                        "kind": "hook" if source_type == "hook" else "transition",
                        "priority": 3 if source_type == "hook" else 1,
                    })
            concat_list = self._render_traditional_clips(
                clip_plan,
                cache_dir,
                unique_id,
                render_sampler,
                filter_builder,
                anti_dedup,
                use_trans,
                enc_args,
                pix_fmt,
            )
            if not concat_list:
                return

            if not self._assemble_traditional_video(
                concat_list,
                main_v_no_audio,
                cache_dir,
                unique_id,
                use_trans,
                trans_dur,
                transition_selector,
                enc_args,
                pix_fmt,
                transition_temp_files,
            ):
                return

        if not self.is_running:
            return

        if not first_track_videos and resolved_watermarks:
            watermarked_main_v = os.path.join(cache_dir, f"wm_main_{unique_id}.ts")
            watermark_vf = (
                f"{color_normalize_filter},format={pix_fmt},fps=30,settb=1/90000"
                + self.generate_watermark_filter(
                    resolved_watermarks,
                    cache_dir,
                    unique_id,
                    time_offset=0.0,
                    is_first_clip=True,
                    target_w=target_w_int,
                    target_h=target_h_int,
                )
            )
            watermark_cmd = [
                self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                '-i', main_v_no_audio,
                '-vf', watermark_vf,
                *enc_args, '-an', '-r', '30', '-pix_fmt', pix_fmt,
                watermarked_main_v,
            ]
            if self.run_ffmpeg(watermark_cmd, cwd=cache_dir) and os.path.exists(watermarked_main_v) and os.path.getsize(watermarked_main_v) > 1024:
                main_v_no_audio = watermarked_main_v
            else:
                self.log_signal.emit(f"[{unique_id}] ⚠️ 整条水印烧录失败，已保留无水印画面继续输出，避免整条任务跳过。")

        if not self.is_running:
            return

        ai_intro_video = ""
        ai_intro_concat_txt = ""
        ai_intro_prefixed_video = ""
        video_before_cover = main_v_no_audio
        if ai_intro_mode:
            (
                video_before_cover,
                ai_intro_video,
                ai_intro_concat_txt,
                ai_intro_prefixed_video,
            ) = self._prepend_ai_intro_video(
                main_v_no_audio,
                ai_intro_path,
                ai_intro_duration,
                cached_video_source,
                filter_builder,
                enc_args,
                pix_fmt,
                cache_dir,
                unique_id,
                audio_row,
            )
            if not video_before_cover:
                return

        # 封面属于整条成片，必须固定在 AI 开场和主内容之前的最前两帧。
        final_v_with_cover = video_before_cover
        if covers:
            chosen_cover_path = random.choice(covers)
            cover_v = os.path.join(cache_dir, f"cover_v_{unique_id}.ts")
            
            cover_cfg = task_config.cover_config
            c_text = cover_cfg.get('text', '').strip()
            c_style = cover_cfg.get('style', '无特效 (仅原图)')
            
            vf_cover_clean = (
                f"{color_normalize_filter},"
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase:{scale_quality},"
                f"crop={target_w}:{target_h}:(iw-ow)/2:(ih-oh)/2,setsar=1,"
                f"format={pix_fmt},fps=30,settb=1/90000"
            )
            
            if c_text and c_style != "无特效 (仅原图)":
                c_font_name = cover_cfg.get('font', '🎲 随机系统字体')
                if c_font_name == "🎲 随机系统字体":
                    c_font_path = self._random_drawtext_font(c_text)
                else:
                    # 通过显示名称查找对应的字体文件路径
                    sys_fonts_map = task_config.sys_fonts
                    c_font_path = self._safe_font_path(
                        sys_fonts_map.get(c_font_name, ''),
                        c_text,
                    )
                    if not c_font_path:
                        c_font_path = self._random_drawtext_font(c_text)
                safe_f = path_to_ffmpeg(c_font_path)
                if c_style == "🎲 随机静态风格":
                    c_style = random.choice(COVER_RANDOM_STATIC_STYLES)
                    
                # 对 cover 文本做转义处理，避免单引号等特殊字符破坏 ffmpeg 命令
                safe_c_text = c_text.replace("'", "'\\\\''").replace(":", "\\\\:").replace("%", "\\\\%")

                def cover_draw(fontsize, fontcolor, y="(h-th)/2", borderw=0, bordercolor="black",
                               boxcolor=None, boxborderw=0, shadow=None):
                    parts = [
                        f"drawtext=fontfile='{safe_f}'",
                        f"text='{safe_c_text}'",
                        f"fontsize={fontsize}",
                        f"fontcolor={fontcolor}",
                        "x=(w-tw)/2",
                        f"y={y}",
                    ]
                    if borderw > 0:
                        parts.extend([f"borderw={borderw}", f"bordercolor={bordercolor}"])
                    if boxcolor:
                        parts.extend(["box=1", f"boxcolor={boxcolor}", f"boxborderw={boxborderw}"])
                    if shadow:
                        sx, sy, sc = shadow
                        parts.extend([f"shadowx={sx}", f"shadowy={sy}", f"shadowcolor={sc}"])
                    return ":".join(parts)

                cover_style_specs = {
                    "经典白字黑边": dict(fontsize=120, fontcolor="white", borderw=4, bordercolor="black"),
                    "高亮霓虹发光": dict(fontsize=130, fontcolor="0x00FFFF", borderw=8, bordercolor="0xFF00FF"),
                    "综艺黑字黄底": dict(fontsize=100, fontcolor="black", y="(h-th)/2+200", boxcolor="yellow@0.9", boxborderw=15, shadow=(6, 6, "red")),
                    "复古牛皮纸色": dict(fontsize=140, fontcolor="0xD2B48C", borderw=5, bordercolor="0x5D4037"),
                    "冲击红字白边": dict(fontsize=132, fontcolor="0xFF1744", borderw=7, bordercolor="white", shadow=(5, 5, "black@0.65")),
                    "蓝黄电商条幅": dict(fontsize=104, fontcolor="0x0F172A", y="(h-th)/2+160", boxcolor="0xFACC15@0.95", boxborderw=18, borderw=2, bordercolor="0x2563EB"),
                    "黑金质感": dict(fontsize=132, fontcolor="0xFFD700", borderw=5, bordercolor="black", shadow=(5, 5, "0x5D4037@0.8")),
                    "清新绿白底": dict(fontsize=108, fontcolor="0x16A34A", y="(h-th)/2+180", boxcolor="white@0.9", boxborderw=16, borderw=2, bordercolor="0x14532D"),
                    "紫粉发光": dict(fontsize=130, fontcolor="0xF0ABFC", borderw=8, bordercolor="0x7E22CE", shadow=(4, 4, "0xDB2777@0.85")),
                    "橙色爆款标签": dict(fontsize=112, fontcolor="white", y="(h-th)/2+210", boxcolor="0xF97316@0.95", boxborderw=18, borderw=3, bordercolor="0x7C2D12"),
                    "白字红底横幅": dict(fontsize=116, fontcolor="white", y="(h-th)/2+180", boxcolor="0xDC2626@0.92", boxborderw=18, borderw=2, bordercolor="white"),
                    "深蓝科技风": dict(fontsize=124, fontcolor="0x93C5FD", borderw=5, bordercolor="0x0F172A", shadow=(6, 6, "0x38BDF8@0.7")),
                    "粉色少女风": dict(fontsize=118, fontcolor="0xBE185D", y="(h-th)/2+170", boxcolor="0xFCE7F3@0.9", boxborderw=16, borderw=2, bordercolor="white"),
                    "金色描边大字": dict(fontsize=142, fontcolor="white", borderw=7, bordercolor="0xF59E0B", shadow=(5, 5, "black@0.6")),
                    "黑字白底清晰款": dict(fontsize=110, fontcolor="black", y="(h-th)/2+190", boxcolor="white@0.92", boxborderw=16, borderw=2, bordercolor="0x111827"),
                    "青色霓虹描边": dict(fontsize=128, fontcolor="white", borderw=8, bordercolor="0x06B6D4", shadow=(4, 4, "0x0E7490@0.85")),
                    "红黄直播间款": dict(fontsize=122, fontcolor="0xFFFF00", borderw=7, bordercolor="0xB91C1C", shadow=(6, 6, "black@0.75")),
                    "白字阴影款": dict(fontsize=126, fontcolor="white", borderw=2, bordercolor="black", shadow=(8, 8, "black@0.75")),
                    "绿色价格牌": dict(fontsize=108, fontcolor="white", y="(h-th)/2+200", boxcolor="0x16A34A@0.94", boxborderw=18, borderw=2, bordercolor="0x052E16"),
                    "银灰高级感": dict(fontsize=128, fontcolor="0xE5E7EB", borderw=5, bordercolor="0x374151", shadow=(5, 5, "black@0.6")),
                }
                spec = cover_style_specs.get(c_style)
                draw_cmd = cover_draw(**spec) if spec else ""
                if draw_cmd:
                    vf_cover_clean += f",{draw_cmd}"

            cover_cmd = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                         '-loop', '1', '-framerate', '30', '-i', chosen_cover_path,
                         '-vf', vf_cover_clean,
                         # 两帧短片使用低延迟编码，避免编码器帧缓存导致空 TS 或多出一帧。
                         '-frames:v', '2', '-c:v', 'libx264', '-preset', 'ultrafast',
                         '-tune', 'zerolatency', '-crf', '18',
                         '-an', '-r', '30', '-pix_fmt', 'yuv420p', cover_v]
            
            if self.run_ffmpeg(cover_cmd, cwd=cache_dir) and os.path.exists(cover_v) and os.path.getsize(cover_v) > 256:
                self.log_signal.emit(f"[{unique_id}] ✅ 封面已生成，并固定在整条成片最前两帧。")
                cover_concat_txt = os.path.join(cache_dir, f"c_concat_{unique_id}.txt")
                with open(cover_concat_txt, 'w', encoding='utf-8') as f:
                    safe_cover_path = str(cover_v).replace('\\', '/')
                    # 封面必须接在完整时间轴前，不能丢掉已经拼好的 AI 开场。
                    safe_main_path = str(video_before_cover).replace('\\', '/')
                    f.write(f"file '{safe_cover_path}'\n")
                    f.write(f"file '{safe_main_path}'\n")
                temp_final = os.path.join(cache_dir, f"cover_final_{unique_id}.ts")
                merge_cmd = [self.ffmpeg_exe, '-y', '-hide_banner', '-loglevel', 'error',
                             '-f', 'concat', '-safe', '0', '-i', cover_concat_txt,
                             '-c:v', 'copy', temp_final]
                if self.run_ffmpeg(merge_cmd, cwd=cache_dir) and os.path.exists(temp_final) and os.path.getsize(temp_final) > 256:
                    final_v_with_cover = temp_final
            else:
                self.log_signal.emit(f"[{unique_id}] ⚠️ 封面生成失败，跳过封面，直接使用视频画面。")

        if not self.is_running:
            return

        if task_config.preview_only:
            final_output_name = f"预览样片_{output_stamp}_{audio_name}_{salt}.mp4"
        else:
            final_output_name = (
                f"{output_stamp}_{audio_name}_回{round_idx}_{salt}.mp4"
                if total_rounds > 1
                else f"{output_stamp}_{audio_name}_{salt}.mp4"
            )
        final_output = os.path.join(output_dir, final_output_name)
        temp_out_mp4 = os.path.join(cache_dir, f"temp_out_{unique_id}.mp4")
        smart_sfx_events = []
        if task_config.smart_sfx_enabled:
            total_audio_duration = audio_dur + ai_intro_duration
            smart_sfx_candidates.append({
                "time": max(0.0, total_audio_duration - 0.45),
                "kind": "ending",
                "priority": 2,
            })
            try:
                smart_sfx_events = plan_smart_sfx(
                    self.ffmpeg_exe,
                    total_audio_duration,
                    smart_sfx_candidates,
                    task_config.smart_sfx_strength,
                    unique_id,
                )
                self.log_signal.emit(
                    f"[{unique_id}] 智能音效："
                    f"{smart_sfx_strength_label(task_config.smart_sfx_strength)}，"
                    f"已规划 {len(smart_sfx_events)} 个结构音效。"
                )
            except Exception as exc:
                self.log_signal.emit(
                    f"[{unique_id}] ⚠️ 智能音效准备失败，已自动关闭本条音效：{exc}"
                )
                smart_sfx_events = []
        ai_intro_silent_audio = self._mux_final_output(
            final_v_with_cover,
            final_output,
            temp_out_mp4,
            audio_path,
            audio_dur,
            bgm_pool,
            main_vol,
            bgm_vol,
            ai_intro_mode,
            ai_intro_path,
            ai_intro_duration,
            final_ass_path,
            sub_config,
            color_normalize_filter,
            final_enc_args,
            pix_fmt,
            cache_dir,
            unique_id,
            audio_row,
            round_idx,
            total_rounds,
            target_w_int,
            target_h_int,
            smart_sfx_events,
            str(audio_dict.get("_resume_job_key") or ""),
        )

        self._cleanup_render_temp_files(
            cache_dir,
            unique_id,
            audio_name,
            concat_list,
            transition_temp_files,
            watermarks,
            extra_files=[
                watermarked_main_v,
                ai_intro_video,
                ai_intro_concat_txt,
                ai_intro_prefixed_video,
                ai_intro_silent_audio,
            ],
        )

    def run(self):
        self.log_signal.emit("[引擎管线] 后台线程已接管任务，正在验证 FFmpeg 与硬件编码环境。")
        try:
            self._run_pipeline()
        except Exception as exc:
            detail = traceback.format_exc()
            self.log_signal.emit(f"[引擎错误] 后台线程启动或调度失败：{exc}\n{detail}")
        finally:
            self._close_sensevoice_session()
            self._terminate_active_processes()
            self._asset_usage_scheduler.discard_pending()
            self._asset_usage_scheduler.flush()
            self.finished_signal.emit()

    def _run_pipeline(self):
        if not self.check_env():
            self.log_signal.emit("[引擎管线] 环境验证未通过，任务未启动。")
            return
        task_config = self._refresh_task_config()
        videos = [v.get('path') for v in self.task.videos if v.get('path')]
        covers = [c.get('path') for c in self.task.covers if c.get('path')]
        bgms = [b.get('path') for b in self.task.bgms if b.get('path')]
        audios = list(self.task.audios)
        ai_intro_mode = task_config.ai_intro_mode
        ai_intro_videos = [
            item.get('path')
            for item in self.task.ai_intro_videos
            if item.get('path') and os.path.exists(item.get('path'))
        ]
        watermarks = self.task.watermarks
        min_clip = task_config.min_clip
        max_clip = task_config.max_clip
        gpu_mode = task_config.gpu_mode
        target_res = task_config.target_res
        cache_dir = task_config.cache_dir
        output_dir = task_config.output_dir
        trans_type = task_config.trans_type
        trans_dur = task_config.trans_dur
        loop_count = task_config.loop_count
        main_vol = task_config.main_vol
        bgm_vol = task_config.bgm_vol
        staged_source_dir = ""
        if videos:
            self.log_signal.emit(
                "[素材调度] 已自动启用新素材优先、使用冷却和近期组合防重复；"
                "记录按当前视频素材目录隔离，无需额外设置。"
            )

        if task_config.use_first_track_audio:
            audios, staged_source_dir = self._stage_remote_audio_sources(audios, cache_dir)
            if audios is None or not self.is_running:
                if staged_source_dir:
                    shutil.rmtree(staged_source_dir, ignore_errors=True)
                return

        if task_config.preview_only:
            audios = audios[:1]
            loop_count = 1
            self.log_signal.emit("[引擎管线] 预览样片模式：只生成第一条音频的一条样片。\n")
        if ai_intro_mode and not ai_intro_videos:
            self.log_signal.emit("[引擎管线] ❌ AI完整开场模式没有可用AI视频，任务已停止。")
            return

        jobs = []
        ai_cycle = []

        def next_ai_intro():
            nonlocal ai_cycle
            if not ai_cycle:
                ai_cycle = list(ai_intro_videos)
                random.shuffle(ai_cycle)
            return ai_cycle.pop()

        ensure_resume_source_ids({"audios": audios})
        for round_idx in range(1, loop_count + 1):
            if not self.is_running:
                break
            for audio_index, audio_dict in enumerate(audios):
                job_audio = dict(audio_dict)
                job_audio["_resume_job_key"] = resume_job_key(
                    job_audio,
                    audio_index,
                    round_idx,
                )
                if ai_intro_mode:
                    job_audio['ai_intro_path'] = next_ai_intro()
                jobs.append((job_audio, round_idx))

        if self._resume_store:
            expected_keys = {
                str(job_audio.get("_resume_job_key") or "")
                for job_audio, _ in jobs
                if job_audio.get("_resume_job_key")
            }
            completed_keys = self._resume_store.completed_keys(expected_keys)
            if completed_keys:
                jobs = [
                    (job_audio, round_idx)
                    for job_audio, round_idx in jobs
                    if job_audio.get("_resume_job_key") not in completed_keys
                ]
                self.log_signal.emit(
                    f"[断点续传] 已核验并跳过 {len(completed_keys)} 条成功成片，"
                    f"剩余 {len(jobs)} 条继续执行。"
                )

        if ai_intro_mode:
            usage = {}
            for job_audio, _ in jobs:
                name = os.path.basename(job_audio.get('ai_intro_path', ''))
                usage[name] = usage.get(name, 0) + 1
            usage_text = "、".join(f"{name}×{count}" for name, count in sorted(usage.items()))
            self.log_signal.emit(
                f"[AI开场调度] 输出数量由第一轨道和轮次决定；"
                f"{len(ai_intro_videos)} 条AI视频按均衡随机循环分配：{usage_text}"
            )

        max_workers = max(1, min(int(task_config.threads or 1), len(jobs) or 1))
        if task_config.strong_structure and max_workers > 2:
            max_workers = 2
            self.log_signal.emit(
                "[稳定模式] 强结构混剪已限制为 2 路并发，在速度和长任务内存占用之间保持平衡。"
            )
        if max_workers == 1:
            self.log_signal.emit(f"[引擎管线] 共需执行 {len(jobs)} 条任务，使用稳定单线程队列...\n")
            for audio_dict, round_idx in jobs:
                if not self.is_running:
                    break
                try:
                    self.process_single_audio(audio_dict, videos, bgms, min_clip, max_clip, gpu_mode, target_res, trans_type, trans_dur, cache_dir, output_dir, covers, watermarks, round_idx, loop_count, main_vol, bgm_vol)
                except Exception as e:
                    self.log_signal.emit(f"[引擎错误] 任务管线发生异常: {str(e)}")
        else:
            self.log_signal.emit(f"[引擎管线] 共需执行 {len(jobs)} 条任务，启动 {max_workers} 路并发队列...\n")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = []
                for audio_dict, round_idx in jobs:
                    if not self.is_running:
                        break
                    future = executor.submit(self.process_single_audio, audio_dict, videos, bgms, min_clip, max_clip, gpu_mode, target_res, trans_type, trans_dur, cache_dir, output_dir, covers, watermarks, round_idx, loop_count, main_vol, bgm_vol)
                    futures.append(future)
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        self.log_signal.emit(f"[引擎错误] 并发管线发生异常: {str(e)}")
        if staged_source_dir:
            shutil.rmtree(staged_source_dir, ignore_errors=True)
        if self.is_running:        
            self.log_signal.emit("🎉 全部混剪任务完毕！硬件引擎进入休眠。")

    def _get_si(self):
        return _hidden_startupinfo()
