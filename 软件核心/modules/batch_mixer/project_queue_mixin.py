import copy
from datetime import datetime
import os
import time

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
)

from modules.batch_mixer.task_center_dialog import (
    BatchTaskCenterDialog,
    NewBatchProjectDialog,
)
from services.batch_project_service import (
    BatchProjectRepository,
    COMPLETED_STATUS,
    FAILED_STATUS,
    RUNNING_STATUS,
    STOPPED_STATUS,
    project_display_name,
    queue_task_display_name,
    safe_output_component,
)
from services.render_checkpoint_service import (
    adopt_existing_outputs,
    checkpoint_progress,
)
from services.platform_service import open_path, resolve_network_path


PROJECT_MEDIA_TABS = (
    "音频素材",
    "封面图片",
    "AI开场视频",
    "钩子素材",
    "第一轨道",
    "第二段指定素材",
    "视频素材",
    "背景音乐",
)

MODE_LABELS = {
    "traditional": "传统音频混剪",
    "ordinary": "普通视频拼接",
    "first_track": "数字人口播第一轨道",
    "ai_intro_first_track": "AI完整开场",
    "ai_intro_audio": "AI开场+音频拼接",
}


class ProjectQueueMixin:
    def init_batch_project_state(self):
        self.batch_project_repo = BatchProjectRepository()
        self._project_switch_guard = False
        self._applied_batch_project_id = ""
        self._project_pending_scans = set()
        self._pending_project_asset_selections = {}
        self._active_queue_task_id = ""
        self._editing_queue_task_id = ""
        self._queue_auto_run = False
        self._queue_stop_requested = False
        self._last_queue_persist_at = 0.0
        self._task_center_dialog = None

    def build_batch_project_bar(self):
        frame = QFrame()
        frame.setObjectName("tabSectionFrame")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(7)
        selector_layout = QHBoxLayout()
        selector_layout.setSpacing(8)
        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)
        label = QLabel("公共素材配置:")
        self.batch_project_combo = QComboBox()
        self.batch_project_combo.setMinimumWidth(250)
        self.batch_project_combo.currentIndexChanged.connect(
            self._on_batch_project_changed
        )
        new_btn = QPushButton("新建项目")
        new_btn.clicked.connect(self.create_batch_project)
        self.save_batch_project_btn = QPushButton("保存当前")
        self.save_batch_project_btn.clicked.connect(
            lambda: self.save_current_batch_project(silent=False)
        )
        self.add_batch_queue_btn = QPushButton("加入队列")
        self.add_batch_queue_btn.setObjectName("startButton")
        self.add_batch_queue_btn.clicked.connect(self.add_current_project_to_queue)
        self.batch_task_name_input = QLineEdit()
        self.batch_task_name_input.setMinimumWidth(210)
        self.batch_task_name_input.setMaximumWidth(340)
        self.batch_task_name_input.setPlaceholderText("任务名称，如：数字人第1组")
        self.cancel_batch_queue_edit_btn = QPushButton("取消修改")
        self.cancel_batch_queue_edit_btn.clicked.connect(
            self.cancel_batch_queue_task_edit
        )
        self.cancel_batch_queue_edit_btn.hide()
        self.batch_queue_btn = QPushButton("任务队列")
        self.batch_queue_btn.clicked.connect(self.show_batch_task_center)

        action_btn = QToolButton()
        action_btn.setText("项目操作")
        action_btn.setPopupMode(QToolButton.InstantPopup)
        action_menu = QMenu(action_btn)
        rename_action = action_menu.addAction("重命名当前项目")
        rename_action.triggered.connect(self.rename_current_batch_project)
        delete_action = action_menu.addAction("删除当前项目")
        delete_action.triggered.connect(self.delete_current_batch_project)
        action_btn.setMenu(action_menu)

        self.batch_project_status_label = QLabel("当前为临时配置")
        self.batch_project_status_label.setObjectName("mutedText")
        selector_layout.addWidget(label)
        selector_layout.addWidget(self.batch_project_combo, 1)
        selector_layout.addWidget(self.batch_project_status_label)
        action_layout.addWidget(new_btn)
        action_layout.addWidget(self.save_batch_project_btn)
        action_layout.addWidget(action_btn)
        action_layout.addWidget(QLabel("队列任务名:"))
        action_layout.addWidget(self.batch_task_name_input)
        action_layout.addStretch(1)
        action_layout.addWidget(self.cancel_batch_queue_edit_btn)
        action_layout.addWidget(self.add_batch_queue_btn)
        action_layout.addWidget(self.batch_queue_btn)
        layout.addLayout(selector_layout)
        layout.addLayout(action_layout)
        return frame

    def load_batch_project_state(self):
        self._reload_batch_project_combo()
        project_id = self.batch_project_repo.current_project_id()
        project = self.batch_project_repo.project(project_id)
        if project:
            self._set_batch_project_combo(project_id)
            self._apply_batch_project(project, refresh=False)
        self._update_batch_project_bar()

    def _reload_batch_project_combo(self):
        if not hasattr(self, "batch_project_combo"):
            return
        current_id = self.batch_project_repo.current_project_id()
        self._project_switch_guard = True
        try:
            self.batch_project_combo.clear()
            self.batch_project_combo.addItem("未选择项目（当前临时配置）", "")
            for project in self.batch_project_repo.projects():
                self.batch_project_combo.addItem(
                    project_display_name(project),
                    project.get("id", ""),
                )
            index = self.batch_project_combo.findData(current_id)
            self.batch_project_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._project_switch_guard = False

    def _set_batch_project_combo(self, project_id):
        if not hasattr(self, "batch_project_combo"):
            return
        index = self.batch_project_combo.findData(project_id)
        if index < 0:
            return
        self._project_switch_guard = True
        try:
            self.batch_project_combo.setCurrentIndex(index)
        finally:
            self._project_switch_guard = False

    def current_batch_project(self):
        return self.batch_project_repo.project(
            self.batch_project_repo.current_project_id()
        )

    def current_batch_project_context(self):
        project = self.current_batch_project()
        if not project:
            return {}
        return {
            "id": project.get("id", ""),
            "shop_name": project.get("shop_name", ""),
            "product_name": project.get("product_name", ""),
            "display_name": project_display_name(project),
        }

    def _capture_asset_selections(self):
        selections = {}
        for tab in PROJECT_MEDIA_TABS:
            table = self.tables.get(tab)
            checkbox = self.select_all_cbx.get(tab)
            paths = []
            if table is not None:
                for row in range(table.rowCount()):
                    if not self._asset_row_checked(table, row):
                        continue
                    item = table.item(row, 2)
                    if item and item.text():
                        paths.append(item.text())
            selections[tab] = {
                "select_all": bool(checkbox and checkbox.isChecked()),
                "selected_paths": paths,
            }
        return selections

    def _capture_batch_project(self, base_project=None):
        project = copy.deepcopy(base_project if isinstance(base_project, dict) else {})
        existing_selections = project.get("asset_selections", {})
        captured_selections = self._capture_asset_selections()
        for tab in self._project_pending_scans:
            if tab in existing_selections:
                captured_selections[tab] = copy.deepcopy(existing_selections[tab])
        project["directories"] = {
            tab: str(self.tab_folders.get(tab) or "")
            for tab in tuple(PROJECT_MEDIA_TABS) + ("保存视频",)
        }
        project["asset_selections"] = captured_selections
        project["rules"] = {
            "mixer_mode": self.mixer_mode_combo.currentData(),
            "strong_structure": self.strong_structure_chk.isChecked(),
            "hook_duration": self.hook_duration_spin.value(),
            "strong_avatar_duration": self.strong_avatar_duration_spin.value(),
            "strong_avatar_smart_tail": self.strong_avatar_smart_tail_chk.isChecked(),
            "strong_product_duration": self.strong_product_duration_spin.value(),
            "strong_avatar_count": self.strong_avatar_count_spin.value(),
            "clip_rhythm": self.clip_rhythm_combo.currentData(),
            "min_clip": self.min_clip_spin.value(),
            "max_clip": self.max_clip_spin.value(),
            "clip_jitter": self.clip_jitter_spin.value(),
            "overlay_gap_min": self.overlay_gap_min_spin.value(),
            "overlay_gap_max": self.overlay_gap_max_spin.value(),
            "overlay_offset_strength": self.overlay_offset_spin.value(),
            "overlay_zoom_mode": self.overlay_zoom_mode_combo.currentData(),
            "overlay_zoom_min": self.overlay_zoom_min_spin.value(),
            "overlay_zoom_max": self.overlay_zoom_max_spin.value(),
            "trans_type": self.trans_combo.currentText(),
            "trans_dur": self.trans_duration.value(),
            "lut_style": self.lut_combo.currentData(),
            "main_vol": self.main_vol_spin.value(),
            "bgm_vol": self.bgm_vol_spin.value(),
            "smart_sfx_enabled": self.smart_sfx_chk.isChecked(),
            "smart_sfx_strength": self.smart_sfx_strength_combo.currentData(),
            "resolution": self.res_combo.currentText(),
            "gpu": self.gpu_combo.currentText(),
            "loop_count": self.loop_spin.value(),
            "threads": self.thread_spin.value(),
            "anti_dedup": self.anti_dedup_chk.isChecked(),
            "auto_perf": self.auto_perf_chk.isChecked(),
        }
        project["subtitle"] = {
            "enable": self.sub_enable_chk.isChecked(),
            "word_timestamps": self.sub_word_timestamps_chk.isChecked(),
            "font": self.sub_font.currentText(),
            "size": self.sub_size.value(),
            "color": self.sub_color.currentText(),
            "border_w": self.sub_border_w.value(),
            "border_color": self.sub_border_color.currentText(),
            "anim": self.sub_anim.currentText(),
            "margin_v": self.sub_margin_v.value(),
        }
        project["cover"] = {
            "text": self.cover_text_input.text(),
            "font": self.cover_font.currentText(),
            "style": self.cover_style.currentText(),
        }
        project["watermarks"] = copy.deepcopy(self.watermarks_data)
        project["watermark_draft"] = copy.deepcopy(
            self.watermark_draft_for_config()
        )
        return project

    @staticmethod
    def _root_directory_mapping(root_dir, current_dirs):
        result = dict(current_dirs)
        if not root_dir or not os.path.isdir(root_dir):
            return result
        aliases = {
            "音频素材": ("音频素材", "音频", "audio"),
            "封面图片": ("封面图片", "封面", "cover"),
            "AI开场视频": ("AI开场视频", "AI开场", "ai_intro"),
            "钩子素材": ("钩子素材", "钩子", "hook"),
            "第一轨道": ("第一轨道", "数字人", "数字人素材"),
            "第二段指定素材": ("第二段指定素材", "第二段素材"),
            "视频素材": ("视频素材", "产品素材", "素材", "video"),
            "背景音乐": ("背景音乐", "BGM", "bgm"),
        }
        try:
            children = {
                entry.name.lower(): entry.path
                for entry in os.scandir(root_dir)
                if entry.is_dir(follow_symlinks=False)
            }
        except OSError:
            children = {}
        for tab, names in aliases.items():
            for name in names:
                path = children.get(name.lower())
                if path:
                    result[tab] = os.path.abspath(path)
                    break
        output_path = next(
            (
                children[name.lower()]
                for name in ("保存视频", "输出", "output")
                if name.lower() in children
            ),
            os.path.join(root_dir, "输出"),
        )
        result["保存视频"] = os.path.abspath(output_path)
        return result

    def create_batch_project(self):
        dialog = NewBatchProjectDialog(self)
        if dialog.exec_() != QDialog.Accepted:
            return
        values = dialog.values()
        project = self._capture_batch_project()
        project["shop_name"] = values["shop_name"]
        project["product_name"] = values["product_name"]
        if values.get("root_dir"):
            project["asset_selections"] = {
                tab: {"select_all": True, "selected_paths": []}
                for tab in PROJECT_MEDIA_TABS
            }
        project["directories"] = self._root_directory_mapping(
            values.get("root_dir", ""),
            project.get("directories", {}),
        )
        saved = self.batch_project_repo.upsert_project(project)
        self._reload_batch_project_combo()
        self._set_batch_project_combo(saved.get("id", ""))
        self._apply_batch_project(saved, refresh=True)
        self.update_log(f"[产品项目] 已创建并切换：{project_display_name(saved)}")

    def save_current_batch_project(self, silent=False):
        project = self.current_batch_project()
        if not project:
            if not silent:
                QMessageBox.information(
                    self,
                    "尚未选择项目",
                    "请先点击“新建项目”，填写店铺和产品名称。",
                )
            return None
        saved = self.batch_project_repo.upsert_project(
            self._capture_batch_project(project)
        )
        # 切换项目时会静默保存旧项目，此时不能重载下拉框并选回旧项目，
        # 否则界面路径虽已切换，顶部选择仍停留在旧项目，第二次点击将不再触发信号。
        if not silent:
            self._reload_batch_project_combo()
            self._set_batch_project_combo(saved.get("id", ""))
            self._update_batch_project_bar("已保存")
        if not silent:
            QMessageBox.information(
                self,
                "保存成功",
                f"已保存项目：{project_display_name(saved)}\n\n"
                "素材目录、字幕、水印和全部核心参数均已独立保存。",
            )
        return saved

    def rename_current_batch_project(self):
        project = self.current_batch_project()
        if not project:
            return
        dialog = NewBatchProjectDialog(self, project=project)
        dialog.root_input.setEnabled(False)
        if dialog.exec_() != QDialog.Accepted:
            return
        values = dialog.values()
        project["shop_name"] = values["shop_name"]
        project["product_name"] = values["product_name"]
        saved = self.batch_project_repo.upsert_project(project)
        self._reload_batch_project_combo()
        self._set_batch_project_combo(saved.get("id", ""))
        self._update_batch_project_bar("已重命名")

    def delete_current_batch_project(self):
        project = self.current_batch_project()
        if not project:
            return
        reply = QMessageBox.question(
            self,
            "删除产品项目",
            f"确定删除“{project_display_name(project)}”吗？\n\n"
            "只删除配置，不会删除任何素材和已经排队的任务。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.batch_project_repo.delete_project(project.get("id", ""))
        self._reload_batch_project_combo()
        self._update_batch_project_bar("当前为临时配置")

    def _on_batch_project_changed(self, _index):
        if self._project_switch_guard:
            return
        self.cancel_batch_queue_task_edit(clear_name=True, silent=True)
        project_id = str(self.batch_project_combo.currentData() or "")
        old_project = self.current_batch_project()
        if (
            old_project
            and old_project.get("id") != project_id
            and old_project.get("id") == self._applied_batch_project_id
        ):
            self.save_current_batch_project(silent=True)
        self.batch_project_repo.set_current_project(project_id)
        project = self.batch_project_repo.project(project_id)
        if project:
            self._apply_batch_project(project, refresh=True)
            self.update_log(
                f"[产品项目] 已切换到：{project_display_name(project)}；"
                "目录、字幕、水印和核心参数已同步。"
            )
        else:
            self._applied_batch_project_id = ""
        self._update_batch_project_bar()

    def _apply_batch_project(self, project, refresh=True):
        directories = project.get("directories", {})
        for tab, path in directories.items():
            if tab in self.tab_folders and str(path or "").strip():
                self.tab_folders[tab] = resolve_network_path(path)

        selections = copy.deepcopy(project.get("asset_selections", {}))
        self._pending_project_asset_selections = selections
        self._project_pending_scans = set(PROJECT_MEDIA_TABS)
        for tab in PROJECT_MEDIA_TABS:
            selection = selections.get(tab, {})
            checkbox = self.select_all_cbx.get(tab)
            if checkbox is not None:
                checkbox.blockSignals(True)
                checkbox.setChecked(bool(selection.get("select_all", True)))
                checkbox.blockSignals(False)
            table = self.tables.get(tab)
            if refresh and table is not None:
                table.clearContents()
                table.setRowCount(0)
                self.update_asset_count_label(tab)

        rules = project.get("rules", {}) if isinstance(project.get("rules"), dict) else {}
        self._set_mixer_mode(rules.get("mixer_mode", self.mixer_mode_combo.currentData()))
        value_widgets = (
            ("strong_structure", self.strong_structure_chk, "checked"),
            ("hook_duration", self.hook_duration_spin, "value"),
            ("strong_avatar_duration", self.strong_avatar_duration_spin, "value"),
            ("strong_avatar_smart_tail", self.strong_avatar_smart_tail_chk, "checked"),
            ("strong_product_duration", self.strong_product_duration_spin, "value"),
            ("strong_avatar_count", self.strong_avatar_count_spin, "value"),
            ("min_clip", self.min_clip_spin, "value"),
            ("max_clip", self.max_clip_spin, "value"),
            ("clip_jitter", self.clip_jitter_spin, "value"),
            ("overlay_gap_min", self.overlay_gap_min_spin, "value"),
            ("overlay_gap_max", self.overlay_gap_max_spin, "value"),
            ("overlay_offset_strength", self.overlay_offset_spin, "value"),
            ("overlay_zoom_min", self.overlay_zoom_min_spin, "value"),
            ("overlay_zoom_max", self.overlay_zoom_max_spin, "value"),
            ("trans_dur", self.trans_duration, "value"),
            ("main_vol", self.main_vol_spin, "value"),
            ("bgm_vol", self.bgm_vol_spin, "value"),
            ("smart_sfx_enabled", self.smart_sfx_chk, "checked"),
            ("loop_count", self.loop_spin, "value"),
            ("threads", self.thread_spin, "value"),
            ("anti_dedup", self.anti_dedup_chk, "checked"),
            ("auto_perf", self.auto_perf_chk, "checked"),
        )
        for key, widget, kind in value_widgets:
            if key not in rules:
                continue
            if kind == "checked":
                widget.setChecked(bool(rules.get(key)))
            else:
                widget.setValue(rules.get(key))
        for key, combo in (
            ("clip_rhythm", self.clip_rhythm_combo),
            ("overlay_zoom_mode", self.overlay_zoom_mode_combo),
            ("lut_style", self.lut_combo),
            ("smart_sfx_strength", self.smart_sfx_strength_combo),
        ):
            if key in rules:
                self._set_combo_data(combo, rules.get(key))
        for key, combo in (
            ("trans_type", self.trans_combo),
            ("resolution", self.res_combo),
            ("gpu", self.gpu_combo),
        ):
            if key in rules:
                combo.setCurrentText(str(rules.get(key)))
        self.smart_sfx_strength_combo.setEnabled(self.smart_sfx_chk.isChecked())

        subtitle = project.get("subtitle", {})
        if isinstance(subtitle, dict):
            subtitle_values = (
                ("enable", self.sub_enable_chk, "checked"),
                ("word_timestamps", self.sub_word_timestamps_chk, "checked"),
                ("size", self.sub_size, "value"),
                ("border_w", self.sub_border_w, "value"),
                ("margin_v", self.sub_margin_v, "value"),
            )
            for key, widget, kind in subtitle_values:
                if key not in subtitle:
                    continue
                if kind == "checked":
                    widget.setChecked(bool(subtitle.get(key)))
                else:
                    widget.setValue(subtitle.get(key))
            for key, combo in (
                ("font", self.sub_font),
                ("color", self.sub_color),
                ("border_color", self.sub_border_color),
                ("anim", self.sub_anim),
            ):
                if key in subtitle:
                    combo.setCurrentText(str(subtitle.get(key)))

        cover = project.get("cover", {})
        if isinstance(cover, dict):
            self.cover_text_input.setText(str(cover.get("text") or ""))
            self.cover_font.setCurrentText(str(cover.get("font") or self.cover_font.currentText()))
            self.cover_style.setCurrentText(str(cover.get("style") or self.cover_style.currentText()))

        self.watermarks_data = copy.deepcopy(project.get("watermarks", []))
        self._wm_new_draft = copy.deepcopy(project.get("watermark_draft", {}))
        self.editing_wm_index = -1
        if hasattr(self, "wm_table"):
            self.wm_table.blockSignals(True)
            self.wm_table.setRowCount(0)
            for watermark in self.watermarks_data:
                font_name = watermark.get("font_name", "")
                if font_name in self.sys_fonts:
                    watermark["font_path"] = self.sys_fonts.get(font_name, "")
                self.add_wm_to_table(watermark)
            self.wm_table.blockSignals(False)
            self.reset_watermark_form()
        self.apply_mixer_mode_ui()
        self.draw_preview_canvas()
        self.draw_subtitle_preview()
        self._applied_batch_project_id = str(project.get("id") or "")

        if refresh:
            self._update_batch_project_bar("正在读取素材目录")
            for tab in PROJECT_MEDIA_TABS:
                self.refresh_directory_async(tab, silent=True)

    def on_project_directory_populated(self, tab_name):
        selection = self._pending_project_asset_selections.get(tab_name, {})
        table = self.tables.get(tab_name)
        if table is not None and not selection.get("select_all", True):
            selected_paths = {
                os.path.normcase(os.path.abspath(path))
                for path in selection.get("selected_paths", [])
                if path
            }
            table.blockSignals(True)
            try:
                for row in range(table.rowCount()):
                    path_item = table.item(row, 2)
                    current = (
                        os.path.normcase(os.path.abspath(path_item.text()))
                        if path_item and path_item.text() else ""
                    )
                    self._set_asset_row_checked(
                        table,
                        row,
                        current in selected_paths,
                    )
            finally:
                table.blockSignals(False)
            self.update_asset_count_label(tab_name)
        self._project_pending_scans.discard(tab_name)
        self._update_batch_project_bar(
            "配置已就绪" if not self._project_pending_scans else "正在读取素材目录"
        )

    def on_project_directory_scan_failed(self, tab_name):
        self._project_pending_scans.discard(tab_name)
        self._update_batch_project_bar(
            "部分目录读取失败" if not self._project_pending_scans else "正在读取素材目录"
        )

    def _update_batch_project_bar(self, state_text=""):
        if not hasattr(self, "batch_project_status_label"):
            return
        project = self.current_batch_project()
        waiting = self.batch_project_repo.waiting_count()
        self.batch_queue_btn.setText(f"任务队列 ({waiting})" if waiting else "任务队列")
        ready = bool(project) and not self._project_pending_scans
        self.save_batch_project_btn.setEnabled(bool(project))
        self.add_batch_queue_btn.setEnabled(ready)
        editing = bool(self._editing_queue_task_id)
        self.add_batch_queue_btn.setText(
            "保存队列修改" if editing else "加入队列"
        )
        self.cancel_batch_queue_edit_btn.setVisible(editing)
        if project:
            status = state_text or ("正在读取素材目录" if self._project_pending_scans else "配置已就绪")
            self.batch_project_status_label.setText(status)
        else:
            self.batch_project_status_label.setText("当前为临时配置")

    def _default_queue_task_name(self, mode_label):
        prefix = str(mode_label or "混剪任务").strip()
        used = {
            queue_task_display_name(task)
            for task in self.batch_project_repo.tasks(include_payload=False)
        }
        index = 1
        while f"{prefix} {index:02d}" in used:
            index += 1
        return f"{prefix} {index:02d}"

    def _queue_task_name_available(self, task_name, exclude_task_id=""):
        normalized = str(task_name or "").strip().casefold()
        for task in self.batch_project_repo.tasks(include_payload=False):
            if task.get("id") == exclude_task_id:
                continue
            if queue_task_display_name(task).casefold() == normalized:
                return False
        return True

    def _queue_output_dir(self, task_name):
        base_dir = os.path.abspath(self.tab_folders.get("保存视频") or "")
        output_dir = os.path.join(
            base_dir,
            time.strftime("%Y-%m-%d"),
            safe_output_component(task_name, "混剪任务"),
        )
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    @staticmethod
    def _queue_asset_summary(task_data):
        if task_data.get("use_first_track_audio"):
            return (
                f"数字人 {len(task_data.get('first_track_videos', []))} / "
                f"产品 {len(task_data.get('videos', []))}"
            )
        return (
            f"音频 {len(task_data.get('audios', []))} / "
            f"产品 {len(task_data.get('videos', []))}"
        )

    def _build_queue_task_payload(self, project, task_data, expected, task_name):
        stored_task_data = copy.deepcopy(task_data)
        stored_task_data.pop("duration_cache", None)
        stored_task_data.pop("sys_fonts", None)
        output_dir = self._queue_output_dir(task_name)
        stored_task_data["output_dir"] = output_dir
        return {
            "project_id": project.get("id", ""),
            "shop_name": project.get("shop_name", ""),
            "product_name": project.get("product_name", ""),
            "task_name": task_name,
            "mode_label": MODE_LABELS.get(
                task_data.get("mixer_mode"), "混剪任务"
            ),
            "asset_summary": self._queue_asset_summary(task_data),
            "expected": expected,
            "output_dir": output_dir,
            "directories": copy.deepcopy(project.get("directories", {})),
            "project_snapshot": copy.deepcopy(project),
            "task_data": stored_task_data,
            "message": "等待执行",
        }

    def add_current_project_to_queue(self):
        project = self.current_batch_project()
        if not project:
            QMessageBox.information(
                self,
                "请先建立产品项目",
                "任务队列需要知道店铺和产品名称，请先点击“新建项目”。",
            )
            return
        if self._project_pending_scans:
            QMessageBox.information(
                self,
                "素材仍在读取",
                "切换项目后素材目录正在后台读取，读取完成后即可加入队列。",
            )
            return
        project = self.save_current_batch_project(silent=True) or project
        prepared = self.prepare_synthesis_task(
            preview_only=False,
            for_queue=True,
        )
        if not prepared:
            return
        task_data, expected = prepared
        mode_label = MODE_LABELS.get(task_data.get("mixer_mode"), "混剪任务")
        task_name = self.batch_task_name_input.text().strip()
        if not task_name:
            task_name = self._default_queue_task_name(mode_label)
        editing_task_id = str(self._editing_queue_task_id or "")
        if not self._queue_task_name_available(task_name, editing_task_id):
            QMessageBox.warning(
                self,
                "任务名称重复",
                "队列里已经有同名任务，请换一个便于区分的名称。",
            )
            return
        payload = self._build_queue_task_payload(
            project,
            task_data,
            expected,
            task_name,
        )
        if editing_task_id:
            old_task = self.batch_project_repo.task(editing_task_id) or {}
            if old_task.get("status") == RUNNING_STATUS:
                QMessageBox.warning(self, "任务运行中", "运行中的任务不能修改。")
                return
            if int(old_task.get("completed") or 0) > 0 or old_task.get("started_at"):
                reply = QMessageBox.question(
                    self,
                    "重置旧断点",
                    "这个任务已经生成过部分视频。保存新参数后，旧断点会清空，"
                    "已生成的视频文件会保留。确定继续吗？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            queue_task = self.batch_project_repo.replace_task(
                editing_task_id,
                payload,
            )
            if not queue_task:
                QMessageBox.warning(self, "保存失败", "任务正在运行或已经被移除。")
                return
            action_text = "已保存队列修改"
            self.cancel_batch_queue_task_edit(clear_name=True, silent=True)
        else:
            queue_task = self.batch_project_repo.add_task(payload)
            action_text = "已加入队列"
            self.batch_task_name_input.clear()
        self._update_batch_project_bar(action_text)
        self._refresh_task_center()
        self.update_log(
            f"[任务队列] {action_text}：{queue_task_display_name(queue_task)}，"
            f"计划生成 {expected} 条。"
        )
        QMessageBox.information(
            self,
            action_text,
            f"任务：{queue_task_display_name(queue_task)}\n"
            f"计划生成 {expected} 条，当前共有 {self.batch_project_repo.waiting_count()} 个等待任务。\n\n"
            "可以继续调整模式和参数，再加入同一产品的下一个任务。",
        )

    def _queue_task_project_snapshot(self, task):
        snapshot = task.get("project_snapshot")
        if isinstance(snapshot, dict) and snapshot:
            return copy.deepcopy(snapshot)
        snapshot = self.batch_project_repo.project(task.get("project_id", "")) or {}
        snapshot = copy.deepcopy(snapshot)
        snapshot["id"] = str(task.get("project_id") or snapshot.get("id") or "")
        snapshot["shop_name"] = str(
            task.get("shop_name") or snapshot.get("shop_name") or "默认店铺"
        )
        snapshot["product_name"] = str(
            task.get("product_name") or snapshot.get("product_name") or "当前产品"
        )
        if isinstance(task.get("directories"), dict):
            snapshot["directories"] = copy.deepcopy(task.get("directories"))

        task_data = task.get("task_data", {})
        if not isinstance(task_data, dict):
            return snapshot
        rules = snapshot.get("rules", {})
        rules = copy.deepcopy(rules if isinstance(rules, dict) else {})
        for key in (
            "mixer_mode",
            "strong_structure",
            "hook_duration",
            "strong_avatar_duration",
            "strong_avatar_smart_tail",
            "strong_product_duration",
            "strong_avatar_count",
            "clip_rhythm",
            "min_clip",
            "max_clip",
            "clip_jitter",
            "overlay_gap_min",
            "overlay_gap_max",
            "overlay_offset_strength",
            "overlay_zoom_mode",
            "overlay_zoom_min",
            "overlay_zoom_max",
            "trans_type",
            "trans_dur",
            "lut_style",
            "main_vol",
            "bgm_vol",
            "smart_sfx_enabled",
            "smart_sfx_strength",
            "resolution",
            "loop_count",
            "threads",
            "anti_dedup",
        ):
            if key in task_data:
                rules[key] = copy.deepcopy(task_data.get(key))
        if "gpu_mode" in task_data:
            rules["gpu"] = task_data.get("gpu_mode")
        snapshot["rules"] = rules

        asset_fields = {
            "音频素材": "audios",
            "封面图片": "covers",
            "AI开场视频": "ai_intro_videos",
            "钩子素材": "hook_videos",
            "第一轨道": "first_track_videos",
            "第二段指定素材": "second_segment_videos",
            "视频素材": "videos",
            "背景音乐": "bgms",
        }
        selections = copy.deepcopy(snapshot.get("asset_selections", {}))
        for tab, field in asset_fields.items():
            if field not in task_data:
                continue
            paths = [
                str(item.get("path") or "")
                for item in task_data.get(field, [])
                if isinstance(item, dict) and item.get("path")
            ]
            selections[tab] = {
                "select_all": False,
                "selected_paths": paths,
            }
        snapshot["asset_selections"] = selections
        if isinstance(task_data.get("sub_config"), dict):
            snapshot["subtitle"] = copy.deepcopy(task_data.get("sub_config"))
        if isinstance(task_data.get("cover_config"), dict):
            snapshot["cover"] = copy.deepcopy(task_data.get("cover_config"))
        snapshot["watermarks"] = copy.deepcopy(task_data.get("watermarks", []))
        return snapshot

    def edit_batch_queue_task(self, task_id):
        task = self.batch_project_repo.task(task_id)
        if not task:
            QMessageBox.warning(self, "任务不存在", "这个任务可能已经被移除。")
            return False
        if task.get("status") == RUNNING_STATUS:
            QMessageBox.information(
                self,
                "任务运行中",
                "运行中的任务参数已经交给后台线程，结束或停止后才能修改。",
            )
            return False
        if self._queue_auto_run:
            self._queue_auto_run = False
            self.batch_project_repo.set_queue_paused(True)
            self.update_log(
                "[任务队列] 已设置为当前任务结束后暂停，防止待修改任务提前启动。"
            )
        self.save_current_batch_project(silent=True)
        snapshot = self._queue_task_project_snapshot(task)
        if not snapshot.get("id"):
            snapshot = self.batch_project_repo.upsert_project(snapshot)
        elif not self.batch_project_repo.project(snapshot.get("id")):
            snapshot = self.batch_project_repo.upsert_project(snapshot)
        project_id = str(snapshot.get("id") or "")
        self.batch_project_repo.set_current_project(project_id)
        self._reload_batch_project_combo()
        self._set_batch_project_combo(project_id)
        self._editing_queue_task_id = str(task_id)
        self.batch_task_name_input.setText(queue_task_display_name(task))
        self._apply_batch_project(snapshot, refresh=True)
        self._update_batch_project_bar("正在载入任务素材")
        self.update_log(
            f"[任务队列] 已载入“{queue_task_display_name(task)}”，"
            "调整模式、素材或参数后点击“保存队列修改”。"
        )
        return True

    def cancel_batch_queue_task_edit(self, clear_name=True, silent=False):
        editing = bool(self._editing_queue_task_id)
        self._editing_queue_task_id = ""
        if clear_name and hasattr(self, "batch_task_name_input"):
            self.batch_task_name_input.clear()
        if hasattr(self, "add_batch_queue_btn"):
            self._update_batch_project_bar("配置已就绪" if editing else "")
        if editing and not silent:
            self.update_log("[任务队列] 已退出任务修改状态，当前参数不会写回队列。")

    def batch_queue_tasks(self):
        return self.batch_project_repo.tasks(include_payload=False)

    def show_batch_task_center(self):
        dialog = self._task_center_dialog
        if dialog is None:
            dialog = BatchTaskCenterDialog(self)
            self._task_center_dialog = dialog
        dialog.refresh()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _refresh_task_center(self):
        self._update_batch_project_bar()
        dialog = self._task_center_dialog
        if dialog is not None:
            dialog.refresh()

    def start_batch_task_queue(self):
        if self._editing_queue_task_id:
            QMessageBox.information(
                self,
                "任务正在修改",
                "请先保存队列修改或点击“取消修改”，再启动队列。",
            )
            return
        engine = getattr(self, "engine_thread", None)
        if engine is not None and engine.isRunning() and not self._active_queue_task_id:
            QMessageBox.information(
                self,
                "当前任务正在运行",
                "请等待当前直接执行的任务结束，再启动任务队列。",
            )
            return
        if not self.batch_project_repo.next_waiting_task():
            QMessageBox.information(self, "没有等待任务", "请先把混剪方案加入队列。")
            return
        self._queue_auto_run = True
        self._queue_stop_requested = False
        self.batch_project_repo.set_queue_paused(False)
        if not self._active_queue_task_id:
            self._start_next_batch_queue_task()
        self.update_log("[任务队列] 已启动，将按顺序连续执行所有等待任务。")

    def pause_batch_task_queue(self):
        self._queue_auto_run = False
        self.batch_project_repo.set_queue_paused(True)
        if self._active_queue_task_id:
            self.update_log("[任务队列] 已设置为当前任务完成后暂停。")
        else:
            self.update_log("[任务队列] 队列已暂停。")
        self._refresh_task_center()

    def stop_current_queue_task(self):
        if not self._active_queue_task_id:
            self.pause_batch_task_queue()
            return
        self._queue_auto_run = False
        self._queue_stop_requested = True
        self.batch_project_repo.set_queue_paused(True)
        engine = getattr(self, "engine_thread", None)
        if engine is not None and engine.isRunning():
            engine.stop()
        self.update_log("[任务队列] 正在停止当前任务，已经生成的成片会保留。")

    def _validate_queue_task(self, queue_task):
        task_data = queue_task.get("task_data", {})
        if not isinstance(task_data, dict):
            return "任务快照损坏"
        required = ["videos"]
        if task_data.get("use_first_track_audio"):
            required.append("first_track_videos")
        else:
            required.append("audios")
        for key in required:
            items = task_data.get(key, [])
            if not items:
                return f"缺少必要素材：{key}"
            candidates = [
                str(item.get("path") or "")
                for item in items[:12]
                if isinstance(item, dict)
            ]
            if candidates and not any(os.path.exists(path) for path in candidates):
                return f"素材路径当前不可访问：{key}"
        output_dir = str(task_data.get("output_dir") or "")
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            return f"输出目录不可用：{exc}"
        return ""

    def _ensure_queue_fonts(self):
        if not self.sys_fonts:
            self.sys_fonts = {
                "微软雅黑 (默认)": "C:/Windows/Fonts/msyh.ttc",
                "黑体": "C:/Windows/Fonts/simhei.ttf",
                "宋体": "C:/Windows/Fonts/simsun.ttc",
                "楷体": "C:/Windows/Fonts/simkai.ttf",
            }

    def _start_next_batch_queue_task(self):
        if not self._queue_auto_run or self._active_queue_task_id:
            return
        queue_task = self.batch_project_repo.next_waiting_task()
        if not queue_task:
            self._queue_auto_run = False
            self.batch_project_repo.set_queue_paused(True)
            self._restore_mix_start_controls()
            self.update_log("[任务队列] 所有等待任务已经执行完毕。")
            self._refresh_task_center()
            return
        checkpoint_path = str(queue_task.get("checkpoint_path") or "")
        adopted = 0
        if queue_task.get("started_at") and not os.path.isfile(checkpoint_path):
            adopted = adopt_existing_outputs(
                queue_task.get("task_data", {}),
                str(queue_task.get("output_dir") or ""),
                checkpoint_path,
                task_id=str(queue_task.get("id") or ""),
            )
        if adopted:
            self.update_log(
                f"[断点续传] 已识别更新前生成的 {adopted} 条成片，"
                "本次将自动跳过。"
            )
        checkpoint_completed, checkpoint_expected = checkpoint_progress(
            queue_task.get("task_data", {}),
            checkpoint_path,
            task_id=str(queue_task.get("id") or ""),
        )
        expected_total = max(
            int(queue_task.get("expected") or 0),
            int(checkpoint_expected or 0),
        )
        if expected_total > 0 and checkpoint_completed >= expected_total:
            self.batch_project_repo.update_task(
                queue_task.get("id", ""),
                status=COMPLETED_STATUS,
                completed=checkpoint_completed,
                message=f"断点核验完成 {checkpoint_completed}/{expected_total} 条",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            self.update_log(
                f"[断点续传] {queue_task_display_name(queue_task)} 已全部完成，"
                "无需重复渲染。"
            )
            self._refresh_task_center()
            QTimer.singleShot(100, self._start_next_batch_queue_task)
            return
        issue = self._validate_queue_task(queue_task)
        if issue:
            self.batch_project_repo.update_task(
                queue_task.get("id", ""),
                status=FAILED_STATUS,
                message=issue,
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            self.update_log(
                f"[任务队列] {queue_task_display_name(queue_task)} 启动前检查失败：{issue}"
            )
            self._refresh_task_center()
            QTimer.singleShot(100, self._start_next_batch_queue_task)
            return
        task_id = queue_task.get("id", "")
        task_data = copy.deepcopy(queue_task.get("task_data", {}))
        self._ensure_queue_fonts()
        task_data["sys_fonts"] = copy.deepcopy(self.sys_fonts)
        task_data["duration_cache"] = {}
        completed_before_start, expected_from_checkpoint = checkpoint_progress(
            task_data,
            str(queue_task.get("checkpoint_path") or ""),
            task_id=task_id,
        )
        expected = max(
            int(queue_task.get("expected") or 0),
            int(expected_from_checkpoint or 0),
        )
        self._active_queue_task_id = task_id
        self._queue_stop_requested = False
        self.batch_project_repo.update_task(
            task_id,
            status=RUNNING_STATUS,
            completed=completed_before_start,
            message=(
                f"断点续传：已完成 {completed_before_start}/{expected} 条"
                if completed_before_start else
                f"正在执行：{queue_task_display_name(queue_task)}"
            ),
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        self.update_log(
            f"[任务队列] 开始执行：{queue_task_display_name(queue_task)}，"
            f"输出：{queue_task.get('output_dir', '')}"
        )
        self._refresh_task_center()
        try:
            self.launch_synthesis_task(
                task_data,
                expected,
                preview_only=False,
                queue_task_id=task_id,
                initial_completed=completed_before_start,
            )
        except Exception as exc:
            self._active_queue_task_id = ""
            self.batch_project_repo.update_task(
                task_id,
                status=FAILED_STATUS,
                message=f"后台线程启动失败：{exc}",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            self.update_log(
                f"[任务队列] {queue_task_display_name(queue_task)} 启动失败：{exc}"
            )
            self._refresh_task_center()
            QTimer.singleShot(100, self._start_next_batch_queue_task)

    def update_active_queue_progress(self, completed):
        task_id = self._active_queue_task_id
        if not task_id:
            return
        now = time.monotonic()
        should_persist = (
            now - self._last_queue_persist_at >= 3.0
            or int(completed) >= int(self._expected_mix_tasks or 0)
        )
        self.batch_project_repo.update_task(
            task_id,
            persist=should_persist,
            completed=int(completed),
            message=f"已完成 {int(completed)} 条",
        )
        if should_persist:
            self._last_queue_persist_at = now
        self._refresh_task_center()

    def update_active_queue_status(self, text):
        task_id = self._active_queue_task_id
        if task_id and text:
            self.batch_project_repo.update_task(
                task_id,
                persist=False,
                message=str(text),
            )
            self._refresh_task_center()

    def finish_active_queue_task(self, completed, expected):
        task_id = self._active_queue_task_id
        if not task_id:
            return False
        if self._queue_stop_requested:
            status = STOPPED_STATUS
            message = f"已停止，保留已完成 {completed}/{expected} 条"
        elif completed >= expected and expected > 0:
            status = COMPLETED_STATUS
            message = f"已完成 {completed}/{expected} 条"
        else:
            status = FAILED_STATUS
            message = f"任务结束，成功 {completed}/{expected} 条，可点击重试"
        self.batch_project_repo.update_task(
            task_id,
            status=status,
            completed=int(completed),
            message=message,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._active_queue_task_id = ""
        self._queue_stop_requested = False
        self._refresh_task_center()
        if self._queue_auto_run:
            QTimer.singleShot(250, self._start_next_batch_queue_task)
        else:
            self._restore_mix_start_controls()
        return True

    def move_batch_queue_task(self, task_id, offset):
        self.batch_project_repo.move_task(task_id, offset)
        self._refresh_task_center()

    def retry_batch_queue_task(self, task_id):
        self.batch_project_repo.retry_task(task_id)
        self._refresh_task_center()

    def remove_batch_queue_task(self, task_id):
        self.batch_project_repo.remove_task(task_id)
        if task_id == self._editing_queue_task_id:
            self.cancel_batch_queue_task_edit(clear_name=True, silent=True)
        self._refresh_task_center()

    def clear_completed_batch_queue_tasks(self):
        self.batch_project_repo.clear_completed()
        if (
            self._editing_queue_task_id
            and not self.batch_project_repo.task(self._editing_queue_task_id)
        ):
            self.cancel_batch_queue_task_edit(clear_name=True, silent=True)
        self._refresh_task_center()

    def open_batch_queue_output(self, task_id):
        task = self.batch_project_repo.task(task_id)
        output_dir = str((task or {}).get("output_dir") or "")
        if output_dir:
            open_path(output_dir)

    def prepare_batch_queue_close(self):
        self._queue_auto_run = False
        self.batch_project_repo.set_queue_paused(True)
        self.save_current_batch_project(silent=True)
        if self._active_queue_task_id:
            active_task_id = self._active_queue_task_id
            task = self.batch_project_repo.task(active_task_id) or {}
            completed, expected = checkpoint_progress(
                task.get("task_data", {}),
                str(task.get("checkpoint_path") or ""),
                task_id=active_task_id,
            )
            self.batch_project_repo.update_task(
                active_task_id,
                status="waiting",
                completed=completed,
                message=f"软件关闭，已保存断点 {completed}/{expected} 条，等待续传",
                finished_at="",
            )
            # 关闭期间线程结束信号可能随后到达，清空活动标识可避免覆盖续传状态。
            self._active_queue_task_id = ""
