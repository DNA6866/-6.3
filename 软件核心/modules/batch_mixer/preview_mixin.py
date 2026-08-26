import os
import shutil
import subprocess

from PyQt5.QtWidgets import QFileDialog

from paths import tools_dir
from services.media_service import extract_reference_frame
from ui_components import ReferenceFrameWorker


class PreviewMixin:
    def _start_reference_frame_extraction(self):
        """后台线程提取视频预览参考帧，不阻塞 UI。"""
        if getattr(self, '_ref_frame_worker_running', False):
            return
        if self.preview_bg_manual and self.sub_preview_bg_manual:
            return  # 两个都是手动模式，无需自动取帧
        source = self._get_video_reference_source()
        if not source or not os.path.exists(source):
            return

        # 缓存 key：基于源文件路径和时间戳，避免重复提取
        source_key = f"{os.path.abspath(source)}_{int(os.path.getmtime(source))}"
        cached_key = getattr(self, '_ref_frame_cache_key', '')
        if source_key == cached_key:
            return  # 同一帧已提取完成

        ffmpeg_exe = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        wm_out = os.path.join(os.path.abspath(self.cache_dir), "preview_bg_watermark.jpg")
        sub_out = os.path.join(os.path.abspath(self.cache_dir), "preview_bg_subtitle.jpg")

        self._ref_frame_worker_running = True
        self._ref_frame_worker = ReferenceFrameWorker(ffmpeg_exe, source, wm_out, sub_out)
        self._ref_frame_worker.finished.connect(self._on_ref_frame_ready)
        self._ref_frame_worker.error.connect(self._on_ref_frame_error)
        self._ref_frame_worker.start()

    def _on_ref_frame_ready(self, wm_path, sub_path):
        self._ref_frame_worker_running = False
        if not self.preview_bg_manual:
            if wm_path and os.path.exists(wm_path):
                self.preview_bg_path = wm_path
                self.preview_bg_auto_source = self._get_video_reference_source()
                self.preview_bg_manual = False
                self.draw_preview_canvas()
        if not self.sub_preview_bg_manual:
            if sub_path and os.path.exists(sub_path):
                self.sub_preview_bg_path = sub_path
                self.sub_preview_bg_auto_source = self._get_video_reference_source()
                self.sub_preview_bg_manual = False
                self.draw_subtitle_preview()
        # 标记缓存完成，避免重复提取
        source = self._get_video_reference_source()
        if source and os.path.exists(source):
            self._ref_frame_cache_key = f"{os.path.abspath(source)}_{int(os.path.getmtime(source))}"

    def _on_ref_frame_error(self, msg):
        self._ref_frame_worker_running = False
        print(f"[预览] 参考帧提取失败：{msg}", flush=True)

    def load_preview_bg(self, target='watermark'):
        try:
            file_path, _ = QFileDialog.getOpenFileName(self, "选择参考底图", self._file_dialog_default_dir(), "Media Files (*.mp4 *.mov *.jpg *.png)")
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
        except Exception as exc:
            print(f"[预览] 加载参考底图失败：{exc}", flush=True)
            self.update_log(f"[预览] 加载参考底图失败：{exc}")

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
            if self._asset_row_checked(table, row):
                return path
        return fallback

    def _extract_reference_frame(self, source, target):
        os.makedirs(self.cache_dir, exist_ok=True)
        bg_path = os.path.join(os.path.abspath(self.cache_dir), f"preview_bg_{target}.jpg")
        ffmpeg_exe = os.path.abspath(os.path.join(tools_dir(), 'ffmpeg.exe'))
        return extract_reference_frame(ffmpeg_exe, source, bg_path)

    def auto_load_watermark_reference_frame(self, silent=False, force=False):
        """触发器：不再同步阻塞，直接调度后台线程提取参考帧。"""
        if self.preview_bg_manual and not force:
            return False
        source = self._get_video_reference_source()
        if not source or not os.path.exists(source):
            return False
        if not force and self.preview_bg_path and self.preview_bg_auto_source == source and os.path.exists(self.preview_bg_path):
            return True
        # 委派给后台线程（非阻塞）
        self._start_reference_frame_extraction()
        return True

    def auto_load_subtitle_reference_frame(self, silent=False, force=False):
        """触发器：与 watermark 共用同一个后台线程提取参考帧。"""
        if self.sub_preview_bg_manual and not force:
            return False
        source = self._get_video_reference_source()
        if not source or not os.path.exists(source):
            return False
        if not force and self.sub_preview_bg_path and self.sub_preview_bg_auto_source == source and os.path.exists(self.sub_preview_bg_path):
            return True
        self._start_reference_frame_extraction()
        return True

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

