import os
import json
import time

from PyQt5.QtGui import QFontDatabase

from ui_components import FontDownloadWorker, FontScannerWorker, RECOMMENDED_CHINESE_FONTS
from utils import get_system_fonts


class FontMixin:
    def _ensure_fonts_loaded(self, block=False):
        """懒加载字体。UI 路径不阻塞，合成路径 block=True 确保字体可用。
        完成后自动刷新所有字体下拉框。"""
        if self._fonts_loaded:
            if not self.sys_fonts and block:
                # 后台扫描尚未完成 → 等它或者同步扫描
                self._ensure_fonts_sync()
            return
        self._fonts_loaded = True

        # 1. 尝试从磁盘缓存读取（快速路径）
        cache_file = os.path.join(self.bin_dir, '.fonts_cache.json')
        try:
            if os.path.exists(cache_file):
                mtime = os.path.getmtime(cache_file)
                if time.time() - mtime < 86400 * 3:  # 3 天内有效
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        cached = json.load(f)
                    if cached:
                        self.sys_fonts = self.filter_chinese_fonts(cached)
                        self.load_local_chinese_fonts()
                        return
        except Exception:
            pass

        # 2. 无缓存
        if block:
            # 合成路径 → 同步扫描（1-3s，可以接受）
            self._ensure_fonts_sync()
        else:
            # UI 路径 → 后台扫描
            self.sys_fonts = {}
            try:
                self._font_scanner = FontScannerWorker(self.font_dir)
                self._font_scanner.finished.connect(self._on_font_scan_done)
                self._font_scanner.start()
            except Exception:
                pass

    def _ensure_fonts_sync(self):
        """同步扫描（合成前兜底），带完整异常保护。"""
        try:
            # 等待后台扫描线程（如果正在运行）
            scanner = getattr(self, '_font_scanner', None)
            if scanner and scanner.isRunning():
                scanner.wait(5000)  # 最多等 5 秒
                if scanner.isRunning():
                    scanner.terminate()
                    scanner.wait(2000)
        except Exception:
            pass

        try:
            raw_fonts = get_system_fonts()
            self.sys_fonts = self.filter_chinese_fonts(raw_fonts)
            self.load_local_chinese_fonts()
            cache_file = os.path.join(self.bin_dir, '.fonts_cache.json')
            try:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(self.sys_fonts, f, ensure_ascii=False)
            except Exception:
                pass
        except Exception:
            # 字体扫描失败不阻塞合成，用空字典兜底
            if not self.sys_fonts:
                self.sys_fonts = {}

    def _on_font_scan_done(self, raw_fonts):
        """后台字体扫描完成后，过滤、缓存并刷新所有组合框。"""
        try:
            self.sys_fonts = self.filter_chinese_fonts(raw_fonts)
        except Exception:
            self.sys_fonts = raw_fonts or {}
        # 加载本地字体
        self.load_local_chinese_fonts()
        # 写入缓存
        cache_file = os.path.join(self.bin_dir, '.fonts_cache.json')
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.sys_fonts, f, ensure_ascii=False)
        except Exception:
            pass
        # 刷新所有字体下拉框
        self.refresh_font_combos()

    def filter_chinese_fonts(self, fonts_dict):
        keywords = ['雅黑', '黑', '宋', '楷', '仿', '隶', '圆', '华文', '等线', '方正', '站酷', '霞鹜', '思源', '特战',
                    'yahei', 'simhei', 'simsun', 'kaiti', 'fangsong', 'dengxian', 'pingfang', 'msjh']
        filtered = {}
        for name, path in fonts_dict.items():
            file_name = os.path.basename(str(path or '')).lower()
            if file_name == 'simsunb.ttf' or (
                file_name.startswith('simsun') and 'ext' in file_name
            ):
                continue
            name_lower = name.lower()
            if "arial" in name_lower or "calibri" in name_lower or "segoe" in name_lower: continue
            if any(kw in name_lower for kw in keywords): filtered[name] = path
        return filtered if filtered else fonts_dict

    def load_local_chinese_fonts(self):
        """加载用户工作区/字体里的中文字体，供预览和 ffmpeg 共同使用。"""
        os.makedirs(self.font_dir, exist_ok=True)
        loaded = {}
        known_names = {item["file"]: item["name"] for item in RECOMMENDED_CHINESE_FONTS}
        for file_name in os.listdir(self.font_dir):
            if not file_name.lower().endswith((".ttf", ".ttc", ".otf")):
                continue
            path = os.path.join(self.font_dir, file_name)
            try:
                font_id = QFontDatabase.addApplicationFont(path)
                families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
                family = families[0] if families else os.path.splitext(file_name)[0]
                display_name = known_names.get(file_name, f"本地字体: {family}")
                loaded[display_name] = path
                self.font_display_families[display_name] = family
            except Exception:
                continue
        if loaded:
            self.sys_fonts.update(loaded)
        return loaded

    def refresh_font_combos(self):
        for combo_name in ("wm_font", "sub_font", "cover_font", "av_wm_font"):
            combo = getattr(self, combo_name, None)
            if not combo:
                continue
            current = combo.currentText()
            existing = {combo.itemText(i) for i in range(combo.count())}
            for name in self.sys_fonts.keys():
                if name not in existing:
                    combo.addItem(name)
            if current:
                combo.setCurrentText(current)

    def ensure_recommended_chinese_fonts(self):
        if getattr(self, "font_download_worker", None) and self.font_download_worker.isRunning():
            return
        missing = []
        for font_def in RECOMMENDED_CHINESE_FONTS:
            path = os.path.join(self.font_dir, font_def["file"])
            if not os.path.exists(path) or os.path.getsize(path) < int(font_def.get("min_bytes", 1) or 1):
                missing.append(font_def)
        if not missing:
            loaded = self.load_local_chinese_fonts()
            if loaded:
                self.refresh_font_combos()
            return
        self.font_download_worker = FontDownloadWorker(self.font_dir, missing)
        self.font_download_worker.message_signal.connect(self.update_log)
        self.font_download_worker.finished_signal.connect(self.on_recommended_fonts_ready)
        self.font_download_worker.start()

    def on_recommended_fonts_ready(self, ready_paths, failed):
        before = set(self.sys_fonts.keys())
        loaded = self.load_local_chinese_fonts()
        self.refresh_font_combos()
        for wm in self.watermarks_data:
            font_name = wm.get('font_name', '')
            if font_name in self.sys_fonts:
                wm['font_path'] = self.sys_fonts[font_name]
        added = [name for name in loaded.keys() if name not in before]
        if added:
            self.update_log(f"[字体] 已补齐并加载：{'、'.join(added)}")
        elif ready_paths:
            self.update_log("[字体] 推荐中文字体已就绪。")


