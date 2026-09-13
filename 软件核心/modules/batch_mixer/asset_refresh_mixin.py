import os

from PyQt5.QtCore import QTimer


class AssetRefreshMixin:
    def _task_media_tabs(self):
        tabs = ["视频素材", "封面图片", "背景音乐"]
        if self.is_first_track_mixer_mode():
            tabs.append("第一轨道")
        else:
            tabs.extend(["音频素材", "第二段指定素材"])
        tabs.append("AI开场视频" if self.is_ai_intro_mixer_mode() else "钩子素材")
        return tabs

    def request_task_asset_refresh(self, callback):
        if getattr(self, "_fresh_asset_request", None):
            self.update_log("[素材核对] 正在核对最新目录，请勿重复提交。")
            return
        roots = {tab: self.tab_folders.get(tab, "") for tab in self._task_media_tabs()
                 if self.tab_folders.get(tab) and tab in self.tables}
        request = {"roots": roots, "tabs": tuple(self._task_media_tabs()),
                   "ids": {}, "pending": set(roots), "errors": [],
                   "project_id": self.batch_project_repo.current_project_id(), "callback": callback}
        self._fresh_asset_request = request
        self.update_log("[素材核对] 正在后台读取最新素材，完成后自动继续，无需手动刷新或保存模板。")
        for tab in roots:
            request["ids"][tab] = self.refresh_directory_async(tab, silent=True)
        if not roots:
            QTimer.singleShot(0, lambda: self._dispatch_fresh_asset_request(request))

    def _finish_fresh_asset_scan(self, tab, request_id, error=""):
        request = getattr(self, "_fresh_asset_request", None)
        if not request or request.get("ids", {}).get(tab) != request_id:
            return
        request.get("pending", set()).discard(tab)
        if error:
            request.get("errors", []).append(f"{tab}：{error}")
        if not request.get("pending"):
            QTimer.singleShot(0, lambda: self._dispatch_fresh_asset_request(request))

    def _dispatch_fresh_asset_request(self, request):
        if getattr(self, "_fresh_asset_request", None) is not request:
            return
        self._fresh_asset_request = None
        changed = self.batch_project_repo.current_project_id() != request.get("project_id")
        changed = changed or tuple(self._task_media_tabs()) != request.get("tabs")
        changed = changed or any(self.tab_folders.get(tab) != root for tab, root in request.get("roots", {}).items())
        changed = changed or any(self._directory_scan_latest.get(tab) != rid for tab, rid in request.get("ids", {}).items())
        errors = request.get("errors", [])
        if changed or errors or getattr(self, "_daily_closing", False):
            reason = "核对期间切换了模板或目录，请重新提交" if changed else "；".join(errors)
            self.update_log(f"[素材核对] 未启动任务：{reason or '软件正在关闭'}")
            return
        try:
            request.get("callback")()
        except Exception as exc:
            self.update_log(f"[素材核对] 任务提交失败：{exc}，请检查配置目录权限和剩余空间。")

    def cancel_pending_asset_refresh(self):
        request = getattr(self, "_fresh_asset_request", None)
        self._fresh_asset_request = None
        if request:
            for rid in request.get("ids", {}).values():
                worker = self._directory_scan_workers.get(rid)
                if worker:
                    worker.requestInterruption()
                    self._project_pending_scans.discard(worker.tab_name)
            self._update_batch_project_bar()
            self.update_log("[素材核对] 已取消，任务不会自动启动。")

    def sync_template_asset_selection(self, tab):
        if getattr(self, "_editing_queue_task_id", ""):
            return
        project = self.current_batch_project()
        if not project or tab not in self.tables:
            return
        root = self.tab_folders.get(tab, "")
        saved_root = project.get("directories", {}).get(tab, "")
        if not root or os.path.normcase(os.path.abspath(root)) != os.path.normcase(os.path.abspath(saved_root)):
            return
        selection = self._capture_asset_selections().get(tab, {})
        try:
            self.batch_project_repo.sync_project_selection(project.get("id"), tab, saved_root, selection)
        except Exception as exc:
            self.update_log(f"[素材自动刷新] 列表已更新，但模板选择保存失败：{exc}；请检查配置目录权限和剩余空间。")
