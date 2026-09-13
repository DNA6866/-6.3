# ruff: noqa: E402
import sys
import os
from paths import (
    core_path,
    ensure_core_path,
    logs_dir,
    models_dir,
    runtime_path,
    tools_dir,
    workspace_path,
)
ensure_core_path()
import time
import platform
import traceback
import base64
from PyQt5.QtWidgets import (QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QMessageBox, QPlainTextEdit,
                             QTextEdit, QApplication, QStackedWidget,
                             QButtonGroup, QFrame, QSizePolicy,
                             QDialog, QDialogButtonBox)
from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QPixmap
from modules.embedded_contact_assets import PAY_IMAGE_B64, CONTACT_IMAGE_B64
from utils import (
    detect_performance_profile,
    _hidden_startupinfo,
)
from services.platform_service import open_path
from services.cache_service import run_cache_maintenance
from services.thread_lifecycle_service import ThreadLifecycleRegistry
from services.ui_layout_service import choose_window_layout
from ui_components import (
    SplashScreen,
    configure_spinbox_for_direct_input,
)

# ───── 重型模块延迟导入（避免启动阻塞）─────
# VoiceCloneWidget / ImageGenerateWidget / LiveDownloaderWidget 不再顶级导入，
# 改为在 initUI 内按需懒加载。下方保留占位变量供类型提示使用。
_VoiceCloneWidget = None
_ImageGenerateWidget = None
_LiveDownloaderWidget = None
_ROICalculatorWidget = None
_MixerV2Widget = None
_QuickVideoTrimWidget = None
_AICopywriterWidget = None

def _lazy_import_voice_clone():
    global _VoiceCloneWidget
    if _VoiceCloneWidget is None:
        from modules.voice_clone.voice_clone_widget import VoiceCloneWidget as VCW
        _VoiceCloneWidget = VCW
    return _VoiceCloneWidget


def _lazy_import_image_generate():
    global _ImageGenerateWidget
    if _ImageGenerateWidget is None:
        from modules.image_generate.image_generate_widget import ImageGenerateWidget as IGW
        _ImageGenerateWidget = IGW
    return _ImageGenerateWidget


def _lazy_import_live_downloader():
    global _LiveDownloaderWidget
    if _LiveDownloaderWidget is None:
        from modules.live_downloader.live_downloader_widget import LiveDownloaderWidget as LDW
        _LiveDownloaderWidget = LDW
    return _LiveDownloaderWidget


def _lazy_import_roi_calculator():
    global _ROICalculatorWidget
    if _ROICalculatorWidget is None:
        from modules.roi_calculator.roi_calculator_widget import ROICalculatorWidget as ROIW
        _ROICalculatorWidget = ROIW
    return _ROICalculatorWidget


def _lazy_import_mixer_v2():
    global _MixerV2Widget
    if _MixerV2Widget is None:
        from modules.mixer_v2.mixer_v2_widget import MixerV2Widget as MV2W
        _MixerV2Widget = MV2W
    return _MixerV2Widget


def _lazy_import_quick_video_trim():
    global _QuickVideoTrimWidget
    if _QuickVideoTrimWidget is None:
        from modules.quick_video_trim.quick_video_trim_widget import QuickVideoTrimWidget as QVTW
        _QuickVideoTrimWidget = QVTW
    return _QuickVideoTrimWidget


def _lazy_import_ai_copywriter():
    global _AICopywriterWidget
    if _AICopywriterWidget is None:
        from modules.ai_copywriter.ai_copywriter_widget import AICopywriterWidget as AICW
        _AICopywriterWidget = AICW
    return _AICopywriterWidget


def app_root():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_root()


class CacheMaintenanceWorker(QThread):
    completed = pyqtSignal(object)

    def run(self):
        self.completed.emit(run_cache_maintenance())


from modules.av_processing.page_mixin import AVProcessingMixin
from modules.batch_mixer.subtitle_mixin import SubtitleMixin
from modules.batch_mixer.watermark_mixin import WatermarkMixin
from modules.batch_mixer.file_tabs_mixin import FileTabsMixin
from modules.batch_mixer.workbench_mixin import WorkbenchMixin
from modules.batch_mixer.asset_mixin import AssetMixin
from modules.batch_mixer.font_mixin import FontMixin
from modules.batch_mixer.preview_mixin import PreviewMixin
from modules.batch_mixer.config_mixin import ConfigMixin
from modules.batch_mixer.mode_mixin import ModeMixin
from modules.batch_mixer.ui_helpers_mixin import UIHelpersMixin
from modules.batch_mixer.task_mixin import TaskMixin
from modules.batch_mixer.project_queue_mixin import ProjectQueueMixin

class VideoMixerApp(ProjectQueueMixin, TaskMixin, UIHelpersMixin, ModeMixin, ConfigMixin, PreviewMixin, FontMixin, AssetMixin, WorkbenchMixin, FileTabsMixin, WatermarkMixin, SubtitleMixin, AVProcessingMixin, QMainWindow):
    # ───── 类常量 ─────
    RANDOM_FONT = "🎲 随机系统字体"
    RANDOM_COLOR = "🎲 随机颜色"
    RANDOM_ANIM = "🎲 随机动态特效"
    RANDOM_STYLE = "🎲 随机静态风格"
    RANDOM_SUB_ANIM = "🎲 随机动效"
    NO_EFFECT = "无特效 (仅原图)"
    NO_TRANSITION = "不使用转场"
    RES_PORTRAIT = "720x1280 (竖屏)"
    RES_LANDSCAPE = "1280x720 (横屏)"
    # ───── 音视频子功能上下游串联映射 ─────
    AV_CHAIN_MAP = {
        'vocal_enhance': 'extract_audio',   # 保留人声 ← 视频分离音频
        'trim_audio':    'extract_audio',   # 音频裁剪 ← 视频分离音频
        'transcribe':    'vocal_enhance',   # 转文本 ← 保留人声
        'add_cover':     'convert',         # 加封面 ← 格式转换
        'text_watermark':'convert',         # 加水印 ← 格式转换
    }
    AV_CHAIN_FALLBACK = {
        'transcribe':    'extract_audio',   # 转文本备用: ← 视频分离音频
    }

    def __init__(self):
        super().__init__()
        second_segment_folder = workspace_path('素材', '第二段指定素材')
        legacy_second_segment_folder = workspace_path('素材', '首段优选素材')
        try:
            legacy_has_files = (
                os.path.isdir(legacy_second_segment_folder)
                and any(entry.is_file() for entry in os.scandir(legacy_second_segment_folder))
            )
            new_has_files = (
                os.path.isdir(second_segment_folder)
                and any(entry.is_file() for entry in os.scandir(second_segment_folder))
            )
            if legacy_has_files and not new_has_files:
                second_segment_folder = legacy_second_segment_folder
        except OSError:
            pass
        self.tab_names = [
            '音频素材', '封面图片', 'AI开场视频', '钩子素材', '第一轨道',
            '第二段指定素材', '视频素材', '背景音乐', '智能字幕', '文字水印', '保存视频'
        ]
        self.tab_folders = {
            '音频素材': workspace_path('素材', '音频素材'),
            '封面图片': workspace_path('素材', '封面图片'),
            'AI开场视频': workspace_path('素材', 'AI开场视频'),
            '钩子素材': workspace_path('素材', '钩子素材'),
            '第一轨道': workspace_path('素材', '第一轨道'),
            '第二段指定素材': second_segment_folder,
            '视频素材': workspace_path('素材', '视频素材'),
            '背景音乐': workspace_path('素材', '背景音乐'),
            '保存视频': workspace_path('输出', '保存视频'),
        }
        self.cache_dir = workspace_path('缓存')
        self.av_output_dir = workspace_path('输出', '音视频处理输出')
        self.av_last_outputs = {}       # kind → 输出文件路径列表（用于上下游串联）
        self.av_page_kinds = {}         # page_index → kind 映射
        self.av_input_lines = {}        # kind → QLineEdit 引用（用于自动填入）
        self._last_file_dialog_dir = None  # 记录上次文件对话框使用的目录
        self.bin_dir = workspace_path('配置') 
        self.config_file = os.path.join(self.bin_dir, 'config.json')
        self.preset_file = os.path.join(self.bin_dir, 'wm_presets.json')
        
        self.tab_exts = {
            '音频素材': ['.mp3', '.wav', '.m4a'], '封面图片': ['.jpg', '.jpeg', '.png'], 
            'AI开场视频': ['.mp4', '.mov', '.avi', '.mkv'],
            '钩子素材': ['.mp4', '.mov', '.avi', '.mkv'],
            '第一轨道': ['.mp4', '.mov', '.avi', '.mkv'],
            '第二段指定素材': ['.mp4', '.mov', '.avi', '.mkv'],
            '视频素材': ['.mp4', '.mov', '.avi'], '背景音乐': ['.mp3', '.wav'], 
            '保存视频': ['.mp4']
        }
        self.select_all_cbx = {}
        self.asset_count_labels = {}
        self._asset_count_update_blocked = set()
        self._directory_scan_workers = {}
        self._directory_scan_latest = {}
        self._directory_scan_seq = 0
        self._completed_mix_tasks = 0
        self._expected_mix_tasks = 0
        self.duration_cache = {}
        self.preview_bg_path = None 
        self.preview_bg_auto_source = None
        self.preview_bg_manual = False
        self.sub_preview_bg_path = None
        self.sub_preview_bg_auto_source = None
        self.sub_preview_bg_manual = False
        
        self.font_dir = workspace_path('字体')
        self.font_display_families = {}
        self.sys_fonts = {}           # 延迟加载，首次使用时通过 _ensure_fonts_loaded 自动填充
        self._fonts_loaded = False
        
        self.watermarks_data = [] 
        self.editing_wm_index = -1 
        self._wm_new_draft = {}
        self.performance_profile = None
        self.trim_player = None
        self.trim_looping = False
        self.feedback_log_history = []
        self.last_feedback_report_path = ""
        self._original_excepthook = sys.excepthook
        sys.excepthook = self.handle_uncaught_exception
        self._post_show_queued = False
        self._cache_maintenance_worker = None
        self._thread_registry = ThreadLifecycleRegistry()
        self.init_batch_project_state()

        self.initUI()
        self.init_directories()
        self.load_configuration()
        self.load_batch_project_state()
        self.apply_mixer_mode_ui()
        if self.auto_perf_chk.isChecked():
            self.apply_best_performance_profile(silent=True)
        self.load_preset_list()
        # 启动后延迟初始化：字体扫描/大量 I/O 放到窗口显示后再执行
        QTimer.singleShot(50, self._deferred_startup_tasks)
        QTimer.singleShot(800, self.ensure_recommended_chinese_fonts)

    # ───── 启动性能优化 ─────
    def showEvent(self, event):
        """窗口首次显示后，触发延迟初始化任务。"""
        super().showEvent(event)
        if not self._post_show_queued:
            self._post_show_queued = True
            QTimer.singleShot(100, self._post_show_init)

    def _post_show_init(self):
        """窗口可见后执行的初始化：分阶段执行，避免阻塞主线程。"""
        # 阶段1：素材列表逐标签页增量刷新（每个标签页之间让出事件循环）
        self._incremental_auto_refresh()
        # 阶段2：参考帧提取在后台线程执行（避免 ffmpeg 阻塞 UI）
        QTimer.singleShot(500, self._start_reference_frame_extraction)
        # 阶段3：每天最多一次后台整理，不占用启动和目录读取的主线程。
        QTimer.singleShot(8000, self._start_cache_maintenance)

    def _start_cache_maintenance(self):
        if QApplication.platformName().lower() == "offscreen":
            return
        worker = getattr(self, "_cache_maintenance_worker", None)
        if worker is not None and worker.isRunning():
            return
        engine = getattr(self, "engine_thread", None)
        if engine is not None and engine.isRunning():
            QTimer.singleShot(60000, self._start_cache_maintenance)
            return
        worker = CacheMaintenanceWorker(self)
        worker.completed.connect(self._on_cache_maintenance_completed, Qt.QueuedConnection)
        worker.finished.connect(self._on_cache_maintenance_worker_finished)
        self._cache_maintenance_worker = worker
        self._thread_registry.register(worker, "缓存维护")
        worker.start()

    def _on_cache_maintenance_completed(self, result):
        message = result.message()
        if message:
            self.update_log(message)

    def _on_cache_maintenance_worker_finished(self):
        worker = getattr(self, "_cache_maintenance_worker", None)
        self._cache_maintenance_worker = None
        if worker is not None:
            worker.deleteLater()

    def _incremental_auto_refresh(self):
        """逐个标签页刷新素材列表，每刷新一个标签页让出事件循环避免 UI 冻结。"""
        tabs = list(self.tab_folders.keys())
        if not tabs:
            return

        def _refresh_next(index=0):
            if index >= len(tabs):
                return
            tab = tabs[index]
            try:
                self.refresh_directory_async(tab, silent=True)
            except Exception:
                pass
            if index + 1 < len(tabs):
                QTimer.singleShot(20, lambda i=index+1: _refresh_next(i))

        QTimer.singleShot(200, lambda: _refresh_next(0))

    def _deferred_startup_tasks(self):
        """在 Qt 事件循环开始后（50ms 延迟）执行的初始化任务。"""
        pass  # 预留扩展点

    def switch_module(self, index):
        # 首次导航 → 按需构建页面
        if index not in self._module_built and index in self._module_builders:
            try:
                real_page = self._module_builders[index]()
                page_log_signal = getattr(real_page, "log_signal", None)
                if page_log_signal is not None:
                    try:
                        page_log_signal.connect(self.update_log)
                    except Exception:
                        pass
                placeholder = self.module_stack.widget(index)
                real_page._thread_registry = self._thread_registry
                configure_spinbox_for_direct_input(real_page)
                self.module_stack.insertWidget(index, real_page)
                self.module_stack.removeWidget(placeholder)
                placeholder.deleteLater()
                self._module_built.add(index)
            except Exception as exc:
                print(f"[启动] 模块 {self.nav_names[index]} 加载失败：{exc}", flush=True)
        self.module_stack.setCurrentIndex(index)

    def get_si(self):
        return _hidden_startupinfo()

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
        try:
            profile = detect_performance_profile(tools_dir())
        except Exception as exc:
            profile = {
                'gpu_mode': 'CPU 软解',
                'threads': 1,
                'summary': '硬件检测异常，已临时使用 CPU 渲染模式',
                'details': [f'检测异常: {exc}'],
                'level': 'cpu',
            }
        self.performance_profile = profile
        self.gpu_combo.setCurrentText(profile['gpu_mode'])
        self.thread_spin.setValue(profile['threads'])
        self._set_hardware_status(f"{profile['gpu_mode']}\n{profile['summary']} | 推荐并发 {profile['threads']}", profile['level'])
        if not silent:
            detail = "；".join(profile.get('details', [])) or "已完成硬件与编码器检测"
            self.update_log(f"[性能检测] {profile['summary']}，已自动选择 {profile['gpu_mode']} / {profile['threads']} 线程。{detail}")

    def initUI(self):
        self.setObjectName("root")
        self.apply_modern_theme()
        self.setWindowTitle('牛爷爷电商视频工具箱 V6.3')
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        layout_profile = choose_window_layout(
            available.width() if available is not None else 1920,
            available.height() if available is not None else 1080,
        )
        self._window_layout_profile = layout_profile
        self.resize(layout_profile.window_width, layout_profile.window_height)
        self.setMinimumSize(
            layout_profile.minimum_width,
            layout_profile.minimum_height,
        )

        central = QWidget()
        central.setObjectName("root")
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(layout_profile.sidebar_width)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(10, 16, 10, 16)
        side_layout.setSpacing(7)

        brand_title = QLabel("电商视频工具箱")
        brand_title.setObjectName("brandTitle")
        brand_title.setWordWrap(True)
        brand_subtitle = QLabel("内容生产工作台")
        brand_subtitle.setObjectName("brandSubTitle")
        side_layout.addWidget(brand_title)
        side_layout.addWidget(brand_subtitle)
        side_layout.addSpacing(14)

        self.module_stack = QStackedWidget()
        self.module_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.module_btn_group = QButtonGroup()
        self.nav_names = [
            '批量混剪',
            '视频快速剪辑',
            '音视频处理',
            'AI音频克隆',
            'AI封面生成',
            'ROI计算器',
            '直播间解析下载',
            '混剪实验室 V2',
            'AI文案助手',
        ]

        # 延迟构建：仅索引 0 页面在 initUI 中立即构建，其余页面在首次点击时按需构建
        self._module_builders = {
            0: lambda: self.build_batch_mixer_page(),
            1: lambda: _lazy_import_quick_video_trim()(),
            2: lambda: self.build_av_processing_page(),
            3: lambda: _lazy_import_voice_clone()(),
            4: lambda: _lazy_import_image_generate()(),
            5: lambda: _lazy_import_roi_calculator()(),
            6: lambda: _lazy_import_live_downloader()(),
            7: lambda: _lazy_import_mixer_v2()(),
            8: lambda: _lazy_import_ai_copywriter()(),
        }
        self._module_built = set()

        for i, name in enumerate(self.nav_names):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setProperty("navButton", True)
            btn.setMinimumHeight(38)
            if i == 0:
                btn.setChecked(True)
            self.module_btn_group.addButton(btn, i)
            side_layout.addWidget(btn)

            if i == 0:
                # 首页立即构建
                page = self._module_builders[0]()
                self._module_built.add(0)
            else:
                # 其余页用占位符，首次导航时按需构建
                page = QWidget()
            self.module_stack.addWidget(page)

        side_layout.addStretch()
        self.module_btn_group.idClicked.connect(self.switch_module)

        content = QFrame()
        content.setObjectName("contentPanel")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(
            layout_profile.content_margin,
            10 if layout_profile.compact_header else 14,
            layout_profile.content_margin,
            10 if layout_profile.compact_header else 14,
        )
        content_layout.setSpacing(10)

        header_layout = QHBoxLayout()
        title_text = (
            "牛爷爷电商视频工具箱 ｜ 混剪 / 快剪 / V2 / 音视频 / AI"
            if layout_profile.compact_header
            else "牛爷爷电商视频工具箱 ｜ 商业混剪 / 快速剪辑 / 混剪实验室 V2 / 音视频处理 / AI工具"
        )
        app_title = QLabel(title_text)
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
        self.log_console = QTextEdit()
        self.log_console.setObjectName("logConsole")
        self.log_console.setReadOnly(True)
        self.log_console.setMinimumHeight(layout_profile.log_minimum_height)
        self.log_console.setMaximumHeight(layout_profile.log_maximum_height)
        content_layout.addWidget(QLabel("执行日志:"))
        content_layout.addWidget(self.log_console, stretch=0)

        root_layout.addWidget(sidebar)
        root_layout.addWidget(content, stretch=1)
        self.setCentralWidget(central)
        self.apply_modern_theme()
        configure_spinbox_for_direct_input(self)

    def detect_system_gpu(self):
        self.apply_best_performance_profile(silent=False)
        
    def open_output_folder(self):
        try:
            open_path(
                self.tab_folders.get(
                    '保存视频',
                    workspace_path('输出', '保存视频'),
                )
            )
        except Exception as exc:
            print(f"[打开目录] 打开输出文件夹失败：{exc}", flush=True)
            self.update_log(f"[打开目录] 打开输出文件夹失败：{exc}")
        
    def stop_and_clean(self):
        self.cancel_pending_asset_refresh()
        if getattr(self, '_daily_worker', None) is not None:
            self.batch_project_repo.suspend_daily(True)
            self._daily_worker.requestInterruption()
        engine = getattr(self, 'engine_thread', None)
        if engine is not None and engine.isRunning():
            if getattr(self, '_active_queue_task_id', ''):
                self.stop_current_queue_task()
            else:
                engine.stop()
            if not engine.wait(10000):
                engine.terminate()
                engine.wait(3000)
            self.log_console.append("\n[任务控制] 已终止当前混剪任务并回收本任务的底层进程。\n")
            self.start_btn.setEnabled(True)
            self.start_btn.setText("开始执行任务")
        self.clear_cache()
        
    def clear_cache(self, silent=False):
        try:
            p = os.path.abspath(self.cache_dir)
            if os.path.exists(p):
                for f in os.listdir(p):
                    if f.startswith("preview_bg_"):
                        continue
                    fp = os.path.join(p, f)
                    if os.path.isfile(fp):
                        os.remove(fp)
            if not silent:
                self.log_console.append("[系统缓存] ♻️ 已手动释放。")
        except Exception:
            print("[警告] 缓存清理失败", flush=True)
        

    def update_log(self, text):
        line = str(text)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.feedback_log_history.append(f"{stamp}  {line}")
        self.feedback_log_history = self.feedback_log_history[-500:]
        try:
            os.makedirs(logs_dir(), exist_ok=True)
            live_log = os.path.join(logs_dir(), f"app_live_{time.strftime('%Y%m%d')}.txt")
            with open(live_log, "a", encoding="utf-8") as f:
                f.write(f"{stamp}  {line}\n")
        except Exception:
            pass
        self.log_console.append(line)

    def _stop_qthread_worker(self, worker, wait_ms=3000, terminate_ms=1500):
        if worker is None:
            return
        self._thread_registry.register(worker, worker.__class__.__name__)
        self._thread_registry.shutdown(
            timeout_ms=max(wait_ms, terminate_ms),
            force=True,
        )

    def _shutdown_ui_background_workers(self):
        try:
            workers = list(getattr(self, "_directory_scan_workers", {}).values())
            for worker in workers:
                self._stop_qthread_worker(worker, wait_ms=3000, terminate_ms=1000)
        except Exception:
            pass
        for attr, wait_ms in (
            ("_font_scanner", 5000),
            ("font_download_worker", 1500),
            ("_ref_frame_worker", 1500),
            ("_cache_maintenance_worker", 10000),
        ):
            try:
                self._stop_qthread_worker(getattr(self, attr, None), wait_ms=wait_ms)
            except Exception:
                pass

    def closeEvent(self, event):
        try:
            source = "用户/系统窗口事件" if event.spontaneous() else "代码触发"
            self.update_log(f"[窗口] 收到关闭事件，来源：{source}，正在保存配置。")
            try:
                os.makedirs(logs_dir(), exist_ok=True)
                path = os.path.join(logs_dir(), f"close_event_{time.strftime('%Y%m%d_%H%M%S')}.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"source: {source}\n")
                    f.write("stack:\n")
                    f.write("".join(traceback.format_stack(limit=12)))
            except Exception:
                pass
        except Exception:
            pass
        try:
            self.prepare_batch_queue_close()
            engine = getattr(self, "engine_thread", None)
            if engine is not None and engine.isRunning():
                engine.stop()
                engine.wait(8000)
        except Exception:
            pass
        self._shutdown_ui_background_workers()
        try:
            self.save_configuration(silent=True)
        except Exception:
            pass
        try:
            if hasattr(self, "module_stack"):
                for index in range(self.module_stack.count()):
                    page = self.module_stack.widget(index)
                    if hasattr(page, "prepare_app_close"):
                        page.prepare_app_close()
        except Exception as exc:
            try:
                self.update_log(f"[窗口] 子模块关闭前清理失败：{exc}")
            except Exception:
                pass
        try:
            values = list(vars(self).values())
            if hasattr(self, "module_stack"):
                for index in range(self.module_stack.count()):
                    page = self.module_stack.widget(index)
                    values.extend(vars(page).values())
            self._thread_registry.adopt_values(values, "窗口关闭")
            shutdown_result = self._thread_registry.shutdown(
                timeout_ms=12000,
                force=True,
            )
            if shutdown_result.get("forced"):
                self.update_log(
                    "[线程回收] 以下任务超时后已最终回收："
                    + "、".join(shutdown_result["forced"])
                )
        except Exception:
            pass
        super().closeEvent(event)
        try:
            self.deleteLater()
        except Exception:
            pass

    def handle_uncaught_exception(self, exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        self.feedback_log_history.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  [未捕获异常]\n{text}")
        try:
            self.write_feedback_report(extra_error=text)
        except Exception:
            pass
        # 同时写入崩溃日志文件
        try:
            crash_log = os.path.join(self.bin_dir, "crash_log.txt")
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write(f"\n{'─'*60}\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(text)
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
            elif module == "混剪实验室 V2":
                page = self.module_stack.currentWidget()
                if hasattr(page, "tabs"):
                    sub = page.tabs.tabText(page.tabs.currentIndex())
        except Exception:
            pass
        return module, sub

    def widget_log_snapshot(self):
        parts = []
        try:
            current = self.module_stack.currentWidget()
            if current:
                edits = list(current.findChildren(QTextEdit)) + list(current.findChildren(QPlainTextEdit))
                seen = set()
                for edit in edits:
                    if id(edit) in seen:
                        continue
                    seen.add(id(edit))
                    text = edit.toPlainText().strip()
                    if text:
                        name = edit.objectName() or edit.__class__.__name__
                        parts.append(f"[{name}]\n{text[-4000:]}")
        except Exception:
            pass
        return "\n\n".join(parts)

    def feedback_file_tail(self, path, max_chars=14000):
        if not path or not os.path.exists(path):
            return f"未找到：{path}"
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > max_chars * 4:
                    f.seek(max(0, size - max_chars * 4))
                raw = f.read()
            text = raw.decode("utf-8", errors="replace")
            if text.count("\ufffd") > 5:
                text = raw.decode("gbk", errors="replace")
            text = text.strip()
            return text[-max_chars:] if len(text) > max_chars else (text or "文件存在，但内容为空。")
        except Exception as exc:
            return f"读取失败：{exc}"

    def write_feedback_report(self, extra_error=""):
        os.makedirs(logs_dir(), exist_ok=True)
        module, sub = self.current_feedback_context()
        ffmpeg = os.path.abspath(os.path.join(tools_dir(), "ffmpeg.exe"))
        ffprobe = os.path.abspath(os.path.join(tools_dir(), "ffprobe.exe"))
        sensevoice = os.path.abspath(os.path.join(models_dir(), "SenseVoiceSmall"))
        voxcpm = os.path.abspath(os.path.join(models_dir(), "VoxCPM2"))
        comfy = os.path.abspath(runtime_path("engines", "ComfyUI", "ComfyUI"))
        try:
            perf = detect_performance_profile(tools_dir())
            perf_lines = [
                f"混剪渲染引擎：{perf.get('gpu_mode', '未知')}",
                f"检测摘要：{perf.get('summary', '未知')}",
                f"推荐并发：{perf.get('threads', '未知')}",
                "检测详情：" + ("；".join(perf.get("details", [])) or "无"),
            ]
        except Exception as exc:
            perf_lines = [f"混剪硬件检测失败：{exc}"]
        lines = [
            "【牛爷爷电商视频工具箱 - 开发者反馈日志】",
            f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "软件版本：牛爷爷电商视频工具箱 V6.3",
            f"当前功能区：{module or '未知'}",
            f"当前子功能：{sub or '无'}",
            "",
            "【系统环境】",
            f"系统：{platform.platform()}",
            f"Python：{sys.version.replace(os.linesep, ' ')}",
            f"软件目录：{APP_DIR}",
            f"ffmpeg：{'存在' if os.path.exists(ffmpeg) else '缺失'} - {ffmpeg}",
            f"ffprobe：{'存在' if os.path.exists(ffprobe) else '缺失'} - {ffprobe}",
            f"SenseVoiceSmall：{'存在' if os.path.exists(sensevoice) else '缺失'} - {sensevoice}",
            f"VoxCPM2：{'存在' if os.path.exists(voxcpm) else '缺失'} - {voxcpm}",
            f"ComfyUI 内置引擎：{'存在' if os.path.exists(comfy) else '缺失'} - {comfy}",
            "",
            "【混剪硬件检测】",
            *perf_lines,
            "",
            "【最近全局运行日志】",
            "\n".join(self.feedback_log_history[-220:]) or "暂无全局日志",
        ]
        widget_logs = self.widget_log_snapshot()
        if widget_logs:
            lines.extend(["", "【当前页面日志】", widget_logs])
        if module == "AI封面生成":
            image_logs = (
                ("AI 环境检测报告", os.path.join(logs_dir(), "image_generate_env_report.txt")),
                ("ComfyUI 启动日志", os.path.join(logs_dir(), "comfyui_startup.log")),
                ("AI 专项诊断", os.path.join(logs_dir(), "image_generate_diagnostic.txt")),
            )
            for title, log_path in image_logs:
                lines.extend([
                    "",
                    f"【{title}】",
                    f"文件：{log_path}",
                    self.feedback_file_tail(log_path),
                ])
        if module == "AI音频克隆":
            voice_logs = (
                ("音频克隆环境检测报告", os.path.join(logs_dir(), "voice_clone_env_report.txt")),
                ("音频克隆独立运行日志", os.path.join(logs_dir(), "voice_clone_runtime_latest.log")),
            )
            for title, log_path in voice_logs:
                lines.extend([
                    "",
                    f"【{title}】",
                    f"文件：{log_path}",
                    self.feedback_file_tail(log_path),
                ])
        if module == "批量混剪":
            mixer_start_log = os.path.join(logs_dir(), "mixer_start_latest.log")
            lines.extend([
                "",
                "【批量混剪启动日志】",
                f"文件：{mixer_start_log}",
                self.feedback_file_tail(mixer_start_log),
            ])
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
