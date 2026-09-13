import os

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QFileDialog, QTableWidgetItem

from services.media_service import import_files_to_folder
from services.media_index_service import get_media_index
from services.platform_service import open_path
from paths import workspace_path
from ui_components import DirectoryScanWorker, resolve_network_path
from modules.batch_mixer.asset_refresh_mixin import AssetRefreshMixin


class AssetMixin(AssetRefreshMixin):
    def init_directories(self):
        for folder in self.tab_folders.values():
            os.makedirs(folder, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.av_output_dir, exist_ok=True)
        os.makedirs(self.bin_dir, exist_ok=True)
        os.makedirs(self.font_dir, exist_ok=True)
        # 不在软件启动时自动清理共享缓存。批量混剪运行中会持续读写 temp_*.ts，
        # 若第二个窗口或测试实例启动时清缓存，会导致正在运行的 ffmpeg 节点找不到临时片段。

    # ───────── 文件对话框默认目录管理 ─────────
    def _file_dialog_default_dir(self):
        """获取文件对话框的默认打开目录：优先上次使用目录，回退工作区根目录"""
        if self._last_file_dialog_dir and os.path.isdir(self._last_file_dialog_dir):
            return self._last_file_dialog_dir
        return workspace_path()

    def _record_file_dialog_dir(self, file_path):
        """记录用户在文件对话框中选择的目录，供下次使用"""
        if file_path:
            dir_path = os.path.dirname(os.path.abspath(str(file_path)))
            if os.path.isdir(dir_path):
                self._last_file_dialog_dir = dir_path

    def _record_file_dialog_dirs(self, file_paths):
        """记录多选文件对话框中上次使用的目录"""
        if file_paths:
            self._record_file_dialog_dir(file_paths[0])

    # ───────── 更换产品目录（一键切换素材文件夹）─────────
    def change_product_directory(self, tab_name):
        """弹出文件夹选择器，将当前素材区的目录切换为用户指定的文件夹"""
        current = self.tab_folders.get(tab_name, workspace_path())
        dir_path = QFileDialog.getExistingDirectory(self, f'选择【{tab_name}】的产品目录', current)
        if not dir_path:
            return
        selected_path = os.path.abspath(dir_path)
        dir_path = resolve_network_path(selected_path)
        self._last_file_dialog_dir = dir_path
        self.tab_folders[tab_name] = dir_path
        self.refresh_directory_async(tab_name)
        self.update_log(f'[产品目录] 【{tab_name}】已切换到：{dir_path}')
        if dir_path != selected_path:
            self.update_log(f'[NAS目录] 已将映射盘路径转换为稳定网络路径：{dir_path}')
        self.save_configuration(silent=True)

    def open_directory_explorer(self, tab_name):
        try:
            folder_path = self.tab_folders.get(tab_name, "")
            if os.path.exists(folder_path):
                open_path(folder_path)
        except Exception:
            print(f"[警告] 打开目录失败: {tab_name}", flush=True)

    def handle_files_dropped(self, tab_name, files):
        folder_path = self.tab_folders.get(tab_name, "")
        if not folder_path:
            return
        os.makedirs(folder_path, exist_ok=True)
        allowed_exts = self.tab_exts.get(tab_name, [])
        imported = 0
        skipped = 0
        imported, skipped = import_files_to_folder(files, folder_path, allowed_exts)
        self.refresh_directory_async(tab_name, silent=True)
        if imported or skipped:
            self.update_log(f"[素材导入] {tab_name}: 导入 {imported} 个，跳过 {skipped} 个。")

    def _filter_from_exts(self, allowed_exts):
        if not allowed_exts:
            return "All Files (*.*)"
        patterns = " ".join([f"*{ext}" for ext in allowed_exts])
        return f"Supported Files ({patterns});;All Files (*.*)"

    def choose_import_files_to_tab(self, tab_name):
        allowed_exts = self.tab_exts.get(tab_name, [])
        files, _ = QFileDialog.getOpenFileNames(self, f"导入【{tab_name}】文件", self._file_dialog_default_dir(), self._filter_from_exts(allowed_exts))
        if files:
            self._record_file_dialog_dirs(files)
            self.handle_files_dropped(tab_name, files)

    def choose_import_folder_to_tab(self, tab_name):
        folder = QFileDialog.getExistingDirectory(self, f"导入【{tab_name}】文件夹", self._file_dialog_default_dir())
        if not folder:
            return
        self._last_file_dialog_dir = os.path.abspath(folder)
        allowed_exts = self.tab_exts.get(tab_name, [])
        files = []
        for root, _, names in os.walk(folder):
            for name in names:
                path = os.path.join(root, name)
                if not allowed_exts or any(path.lower().endswith(ext) for ext in allowed_exts):
                    files.append(path)
        self.handle_files_dropped(tab_name, files)

    def refresh_directory_async(self, tab_name, silent=False):
        folder_path = self.tab_folders.get(tab_name, "")
        if not folder_path or tab_name not in self.tables:
            return
        request = getattr(self, "_fresh_asset_request", None)
        if request and tab_name in request.get("ids", {}):
            self.cancel_pending_asset_refresh()
        folder_path = os.path.abspath(folder_path)
        self._directory_scan_seq += 1
        request_id = self._directory_scan_seq
        self._directory_scan_latest[tab_name] = request_id
        for old_worker in list(self._directory_scan_workers.values()):
            if old_worker.tab_name == tab_name and old_worker.isRunning():
                old_worker.requestInterruption()
        label = self.asset_count_labels.get(tab_name)
        if label:
            label.setText("正在后台读取目录...")

        worker = DirectoryScanWorker(
            tab_name,
            folder_path,
            self.tab_exts.get(tab_name, []),
            request_id,
            force_refresh=True,
        )
        self._directory_scan_workers[request_id] = worker
        registry = getattr(self, "_thread_registry", None)
        if registry is not None:
            registry.register(worker, f"目录扫描:{tab_name}")
        worker.scan_finished.connect(self._on_directory_scan_finished, Qt.QueuedConnection)
        worker.scan_failed.connect(self._on_directory_scan_failed, Qt.QueuedConnection)
        worker.finished.connect(
            lambda rid=request_id: self._on_directory_scan_thread_finished(rid),
            Qt.QueuedConnection,
        )
        worker.start()
        return request_id

    def _on_directory_scan_finished(self, tab_name, folder_path, request_id, files):
        if self._directory_scan_latest.get(tab_name) != request_id:
            return
        current_folder = resolve_network_path(self.tab_folders.get(tab_name, ""))
        scanned_folder = resolve_network_path(folder_path)
        if os.path.normcase(current_folder) != os.path.normcase(scanned_folder):
            return
        self._populate_directory_table(tab_name, files)
        if hasattr(self, 'on_project_directory_populated'):
            self.on_project_directory_populated(tab_name)
        self.sync_template_asset_selection(tab_name)
        self._finish_fresh_asset_scan(tab_name, request_id)
        if tab_name == '第一轨道' or scanned_folder.startswith("\\\\"):
            child_count = sum(
                1
                for display_name, _ in files
                if os.sep in display_name or "/" in display_name
            )
            self.update_log(
                f"[目录读取] 【{tab_name}】扫描完成：共 {len(files)} 个素材，"
                f"其中子目录 {child_count} 个；路径：{scanned_folder}"
            )

    def _on_directory_scan_failed(self, tab_name, folder_path, request_id, message):
        if self._directory_scan_latest.get(tab_name) != request_id:
            return
        label = self.asset_count_labels.get(tab_name)
        if label:
            label.setText("目录读取失败")
        self.update_log(f"[目录读取] 【{tab_name}】后台扫描失败：{message}")
        if hasattr(self, 'on_project_directory_scan_failed'):
            self.on_project_directory_scan_failed(tab_name)
        self._finish_fresh_asset_scan(tab_name, request_id, message)

    def _on_directory_scan_thread_finished(self, request_id):
        worker = self._directory_scan_workers.pop(request_id, None)
        if worker is not None:
            worker.deleteLater()

    def _populate_directory_table(self, tab_name, files):
        table = self.tables.get(tab_name)
        if table is None:
            return
        selected = {table.item(row, 2).text() for row in range(table.rowCount())
                    if table.item(row, 2) and self._asset_row_checked(table, row)}
        statuses = {table.item(row, 2).text(): table.item(row, 3).text()
                    for row in range(table.rowCount())
                    if tab_name == '音频素材' and table.item(row, 2) and table.item(row, 3)}
        table.blockSignals(True)
        table.setUpdatesEnabled(False)
        try:
            table.clearContents()
            table.setRowCount(len(files))
            current_select_all = self.select_all_cbx[tab_name].isChecked()
            for row, (display_name, full_path) in enumerate(files):
                chk_item = QTableWidgetItem()
                chk_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                chk_item.setTextAlignment(Qt.AlignCenter)
                self._set_asset_check_item_visual(chk_item, current_select_all or full_path in selected)
                table.setItem(row, 0, chk_item)
                table.setItem(row, 1, QTableWidgetItem(display_name))
                table.setItem(row, 2, QTableWidgetItem(full_path))
                if tab_name == '音频素材':
                    status_item = QTableWidgetItem(statuses.get(full_path, "待处理"))
                    status_item.setTextAlignment(Qt.AlignCenter)
                    table.setItem(row, 3, status_item)
        finally:
            table.setUpdatesEnabled(True)
            table.blockSignals(False)
        if tab_name == '音频素材':
            self._audio_table_rows = {os.path.normcase(path): row for row, (_, path) in enumerate(files)}
        table.viewport().update()
        self.update_asset_count_label(tab_name)
        if tab_name == '视频素材' and hasattr(self, 'preview_canvas') and not self.preview_bg_manual:
            QTimer.singleShot(0, lambda: self.auto_load_watermark_reference_frame(silent=True))
        if tab_name == '视频素材' and hasattr(self, 'sub_preview_canvas') and not self.sub_preview_bg_manual:
            QTimer.singleShot(0, lambda: self.auto_load_subtitle_reference_frame(silent=True))

    def refresh_directory(self, tab_name, silent=False):
        folder_path = self.tab_folders.get(tab_name, "")
        allowed_exts = self.tab_exts.get(tab_name, [])
        try:
            if not os.path.isdir(folder_path):
                raise OSError(f"素材目录不可访问：{folder_path}")
            files = []
            def on_error(exc):
                raise exc
            for root, _, names in os.walk(folder_path, onerror=on_error):
                for filename in names:
                    full_path = os.path.abspath(os.path.join(root, filename))
                    if not os.path.isfile(full_path):
                        continue
                    if allowed_exts and not any(filename.lower().endswith(ext) for ext in allowed_exts):
                        continue
                    rel_name = os.path.relpath(full_path, folder_path)
                    files.append((rel_name, full_path))
            files.sort(key=lambda item: item[0].lower())
            get_media_index().store_directory(folder_path, allowed_exts, files)
            self._populate_directory_table(tab_name, files)
            # 同步刷新同样要恢复项目素材勾选（否则启动恢复/切换时勾选不联动）
            if hasattr(self, "on_project_directory_populated"):
                self.on_project_directory_populated(tab_name)
            self.sync_template_asset_selection(tab_name)
        except OSError as exc:
            self.update_log(f"[目录读取] {tab_name}：{exc}")

    def update_asset_count_label(self, tab_name):
        label = self.asset_count_labels.get(tab_name)
        table = self.tables.get(tab_name)
        if not label or not table:
            return
        if tab_name in getattr(self, '_asset_count_update_blocked', set()):
            return
        total = table.rowCount()
        selected = 0
        for row in range(total):
            if self._asset_row_checked(table, row):
                selected += 1
        label.setText(f"已选 {selected} / 共 {total} 个素材（含子目录）")

    def _asset_row_checked(self, table, row):
        item = table.item(row, 0)
        if item is not None and item.data(Qt.UserRole) is not None:
            return bool(item.data(Qt.UserRole))
        if item is not None and (item.flags() & Qt.ItemIsUserCheckable):
            return item.checkState() == Qt.Checked
        chk_widget = table.cellWidget(row, 0)
        if chk_widget and chk_widget.layout() and chk_widget.layout().count():
            chk = chk_widget.layout().itemAt(0).widget()
            return bool(chk and chk.isChecked())
        return False

    def _set_asset_row_checked(self, table, row, checked):
        item = table.item(row, 0)
        if item is not None and item.data(Qt.UserRole) is not None:
            self._set_asset_check_item_visual(item, checked)
            return
        if item is not None and (item.flags() & Qt.ItemIsUserCheckable):
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            return
        chk_widget = table.cellWidget(row, 0)
        if chk_widget and chk_widget.layout() and chk_widget.layout().count():
            chk = chk_widget.layout().itemAt(0).widget()
            if chk:
                chk.setChecked(checked)

    def _set_asset_check_item_visual(self, item, checked):
        item.setData(Qt.UserRole, bool(checked))
        item.setText("已选" if checked else "选择")
        item.setForeground(QColor("#86EFAC" if checked else "#CBD5E1"))
        item.setBackground(QColor("#12351F" if checked else "#020617"))
        item.setToolTip("点击这里选择/取消选择素材")
        font = item.font()
        font.setBold(True)
        if font.pointSize() < 11:
            font.setPointSize(11)
        item.setFont(font)

    def _on_asset_table_item_changed(self, tab_name, item):
        if item is not None and item.column() == 0:
            self.update_asset_count_label(tab_name)

    def _on_asset_table_item_clicked(self, tab_name, item):
        if item is None or item.column() != 0:
            return
        table = self.tables.get(tab_name)
        if not table:
            return
        checked = not self._asset_row_checked(table, item.row())
        self._set_asset_row_checked(table, item.row(), checked)
        checkbox = self.select_all_cbx.get(tab_name)
        if not checked and checkbox and checkbox.isChecked():
            # 单独取消一项即进入手动选择；不能触发“取消全选”把其他行也清掉。
            blocked = checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.blockSignals(blocked)
        self.update_asset_count_label(tab_name)

    def update_table_status(self, row_index, text, color_name):
        table = self.tables.get('音频素材')
        if not table:
            return
        paths = getattr(self, '_active_audio_status_paths', None)
        if paths is not None:
            # 运行期间手动刷新或准备新队列可能改变行号，进度必须跟随原文件。
            source = paths.get(row_index, '')
            row_index = getattr(self, '_audio_table_rows', {}).get(os.path.normcase(source), -1)
        if row_index < 0 or row_index >= table.rowCount():
            return
        if row_index < table.rowCount():
            item = QTableWidgetItem(text)
            if color_name == "blue":
                item.setForeground(Qt.blue)
            elif color_name == "green":
                item.setForeground(Qt.darkGreen)
            elif color_name == "red":
                item.setForeground(Qt.red)
            elif color_name:
                item.setForeground(QColor(color_name))
            item.setTextAlignment(Qt.AlignCenter)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            table.setItem(row_index, 3, item)

    def auto_refresh_all(self):
        for t in self.tab_folders.keys():
            self.refresh_directory_async(t, silent=True)
        
    def toggle_select_all(self, tab_name, state):
        table = self.tables[tab_name]
        is_checked = state == Qt.Checked
        self._asset_count_update_blocked.add(tab_name)
        table.blockSignals(True)
        table.setUpdatesEnabled(False)
        try:
            for row in range(table.rowCount()):
                self._set_asset_row_checked(table, row, is_checked)
        finally:
            table.setUpdatesEnabled(True)
            table.blockSignals(False)
            self._asset_count_update_blocked.discard(tab_name)
        table.viewport().update()
        self.update_asset_count_label(tab_name)

