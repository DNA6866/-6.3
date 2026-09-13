import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

CORE = str(Path(__file__).resolve().parents[1])
for directory in (str(Path(CORE).parent), CORE):
    if directory not in os.sys.path:
        os.sys.path.insert(0, directory)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QCheckBox, QLabel, QTableWidget, QWidget  # noqa: E402
from modules.batch_mixer.asset_mixin import AssetMixin  # noqa: E402
from modules.batch_mixer.project_queue_mixin import ProjectQueueMixin  # noqa: E402
from modules.av_processing.page_mixin import AVProcessingMixin  # noqa: E402
from modules.av_processing.workers import AVProcessWorker, AddSubtitleWorker  # noqa: E402
from modules.quick_video_trim.quick_video_trim_widget import QuickVideoTrimWidget  # noqa: E402
from services.batch_project_service import BatchProjectRepository, FAILED_STATUS, WAITING_STATUS  # noqa: E402
from services.media_service import import_files_to_folder  # noqa: E402
from ui_components import DirectoryScanWorker  # noqa: E402


class AssetOwner(QWidget, AssetMixin, ProjectQueueMixin):
    def __init__(self, repo, root):
        super().__init__()
        self.batch_project_repo = repo
        self.tab_folders = {"音频素材": root}
        self.tables = {"音频素材": QTableWidget(0, 4)}
        self.select_all_cbx = {"音频素材": QCheckBox()}
        self.select_all_cbx["音频素材"].setChecked(True)
        self.asset_count_labels = {"音频素材": QLabel()}
        self.tab_exts = {"音频素材": [".wav"]}
        self._directory_scan_seq = 0
        self._directory_scan_latest = {}
        self._directory_scan_workers = {}
        self._project_pending_scans = set()
        self._pending_project_asset_selections = {}
        self._editing_queue_task_id = ""
        self.logs = []

    def update_log(self, text):
        self.logs.append(text)

    def _update_batch_project_bar(self, *args):
        pass

    def is_first_track_mixer_mode(self):
        return False

    def is_ai_intro_mixer_mode(self):
        return False


class WorkflowSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.index = mock.Mock()
        self.index.cached_directory.return_value = ([("old.wav", "deleted.wav")], True)
        self.index_patch = mock.patch("ui_components.get_media_index", return_value=self.index)
        self.index_patch.start()
        self.repo = BatchProjectRepository(str(self.root / "state.json"))

    def tearDown(self):
        self.index_patch.stop()
        self.temp.cleanup()

    def file(self, relative, data=b"media"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def scan(self, path):
        worker = DirectoryScanWorker("音频素材", path, [".wav"], 1, force_refresh=True)
        results, errors = [], []
        worker.scan_finished.connect(lambda *args: results.append(args[-1]))
        worker.scan_failed.connect(lambda *args: errors.append(args[-1]))
        worker.run()
        return results, errors

    def owner(self):
        root = str(self.root / "audio")
        os.makedirs(root, exist_ok=True)
        self.repo.upsert_project({"id": "p", "directories": {"音频素材": root},
                                  "rules": {"loop_count": 160}})
        return AssetOwner(self.repo, root)

    def drain(self, owner):
        deadline = time.monotonic() + 5
        while (owner._directory_scan_workers or getattr(owner, "_fresh_asset_request", None)) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        for worker in list(owner._directory_scan_workers.values()):
            worker.requestInterruption()
            worker.wait(5000)
        self.app.processEvents()
        self.assertFalse(owner._directory_scan_workers)
        self.assertFalse(getattr(owner, "_fresh_asset_request", None))

    def test_force_scan_ignores_cached_files_and_finds_deep_changes(self):
        old = self.file("audio/sub/deep/old.wav")
        os.remove(old)
        new = self.file("audio/sub/deep/new.wav")
        self.file("audio/ignored.mp4")
        results, errors = self.scan(str(self.root / "audio"))
        self.assertEqual([[p for _, p in rows] for rows in results], [[new]])
        self.assertFalse(errors)
        self.index.cached_directory.assert_not_called()

    def test_missing_directory_is_not_recreated(self):
        root = str(self.root / "removed")
        results, errors = self.scan(root)
        self.assertFalse(results)
        self.assertTrue(errors)
        self.assertFalse(os.path.exists(root))

    def test_partial_directory_failure_never_publishes_partial_success(self):
        self.file("audio/valid.wav")
        def failing_walk(root, onerror):
            yield root, [], ["valid.wav"]
            onerror(PermissionError("子目录不可访问"))
        with mock.patch("ui_components.os.walk", side_effect=failing_walk):
            results, errors = self.scan(str(self.root / "audio"))
        self.assertFalse(results)
        self.assertTrue(errors)
        self.index.store_directory.assert_not_called()

    def test_partial_selection_survives_refresh_without_selecting_new_files(self):
        owner = self.owner()
        a, b, c = [self.file(f"audio/{name}.wav") for name in "abc"]
        owner._populate_directory_table("音频素材", [("a", a), ("b", b)])
        owner.select_all_cbx["音频素材"].setChecked(False)
        owner._set_asset_row_checked(owner.tables["音频素材"], 1, False)
        owner._populate_directory_table("音频素材", [("a", a), ("c", c)])
        self.assertEqual(owner._capture_asset_selections()["音频素材"]["selected_paths"], [a])

    def test_all_selection_is_directory_based_not_a_frozen_file_list(self):
        owner = self.owner()
        owner._populate_directory_table("音频素材", [("a", self.file("audio/a.wav"))])
        self.assertEqual(owner._capture_asset_selections()["音频素材"],
                         {"select_all": True, "selected_paths": []})

    def test_unchecking_one_row_exits_all_mode_and_survives_preflight(self):
        owner = self.owner()
        a, b = [self.file(f"audio/{name}.wav") for name in "ab"]
        table = owner.tables["音频素材"]
        owner._populate_directory_table("音频素材", [("a", a), ("b", b)])
        owner.select_all_cbx["音频素材"].stateChanged.connect(lambda state: owner.toggle_select_all("音频素材", state))
        owner._on_asset_table_item_clicked("音频素材", table.item(1, 0))
        self.assertFalse(owner.select_all_cbx["音频素材"].isChecked())
        self.assertTrue(owner._asset_row_checked(table, 0))
        self.file("audio/c.wav")
        owner.request_task_asset_refresh(mock.Mock())
        self.drain(owner)
        self.assertEqual(owner._capture_asset_selections()["音频素材"]["selected_paths"], [a])

    def test_running_audio_status_follows_file_after_rows_change(self):
        owner = self.owner()
        a, b = [self.file(f"audio/{name}.wav") for name in "ab"]
        owner._active_audio_status_paths = {0: a, 1: b}
        owner._populate_directory_table("音频素材", [("b", b)])
        owner.update_table_status(0, "不应写到另一个文件", "green")
        self.assertEqual(owner.tables["音频素材"].item(0, 3).text(), "待处理")
        owner.update_table_status(1, "完成", "green")
        self.assertEqual(owner.tables["音频素材"].item(0, 3).text(), "完成")

    def test_old_template_selection_is_only_applied_once(self):
        owner = self.owner()
        a, b = [self.file(f"audio/{name}.wav") for name in "ab"]
        owner.select_all_cbx["音频素材"].setChecked(False)
        owner._pending_project_asset_selections = {"音频素材": {"select_all": False, "selected_paths": [a]}}
        owner._populate_directory_table("音频素材", [("a", a), ("b", b)])
        owner.on_project_directory_populated("音频素材")
        owner._set_asset_row_checked(owner.tables["音频素材"], 1, True)
        owner.on_project_directory_populated("音频素材")
        self.assertEqual(owner._capture_asset_selections()["音频素材"]["selected_paths"], [a, b])

    def test_preflight_reads_new_directory_and_calls_back_once(self):
        owner = self.owner()
        old = self.file("audio/deleted.wav")
        owner._populate_directory_table("音频素材", [("deleted", old)])
        os.remove(old)
        new = self.file("audio/sub/new.wav")
        callback = mock.Mock()
        owner.request_task_asset_refresh(callback)
        owner.request_task_asset_refresh(callback)
        self.drain(owner)
        callback.assert_called_once()
        self.assertEqual(owner.tables["音频素材"].item(0, 2).text(), new)

    def test_cancel_preflight_prevents_task_start(self):
        owner = self.owner()
        self.file("audio/a.wav")
        callback = mock.Mock()
        owner.request_task_asset_refresh(callback)
        owner.cancel_pending_asset_refresh()
        self.drain(owner)
        callback.assert_not_called()

    def test_switch_project_during_preflight_prevents_task_start(self):
        owner = self.owner()
        callback = mock.Mock()
        owner.request_task_asset_refresh(callback)
        self.repo.set_current_project("another")
        self.drain(owner)
        callback.assert_not_called()

    def test_failed_scan_prevents_task_start(self):
        owner = self.owner()
        os.rmdir(owner.tab_folders["音频素材"])
        callback = mock.Mock()
        owner.request_task_asset_refresh(callback)
        self.drain(owner)
        callback.assert_not_called()

    def test_template_sync_preserves_rules_and_queued_snapshot(self):
        owner = self.owner()
        with mock.patch("services.batch_project_service.checkpoint_path_for_task", return_value=str(self.root / "checkpoint.json")):
            task = self.repo.add_task({"task_data": {"audios": [{"path": "old.wav"}]}})
        before = self.repo.task(task["id"])
        owner.sync_template_asset_selection("音频素材")
        self.assertEqual(self.repo.project("p")["rules"], {"loop_count": 160})
        self.assertEqual(self.repo.task(task["id"]), before)
        self.assertEqual(self.repo.project("p")["asset_selections"]["音频素材"]["selected_paths"], [])

    def test_template_sync_does_not_save_a_changed_directory(self):
        owner = self.owner()
        owner.tab_folders["音频素材"] = str(self.root / "different")
        owner.sync_template_asset_selection("音频素材")
        self.assertNotIn("asset_selections", self.repo.project("p"))

    def test_failed_template_write_is_reported_and_can_be_retried(self):
        owner = self.owner()
        with mock.patch.object(self.repo, "_save_state", side_effect=OSError("磁盘已满")):
            owner.sync_template_asset_selection("音频素材")
        self.assertEqual(self.repo.project("p").get("asset_selections"), {})
        self.assertTrue(any("保存失败" in line for line in owner.logs))
        owner.sync_template_asset_selection("音频素材")
        self.assertTrue(self.repo.project("p")["asset_selections"]["音频素材"]["select_all"])

    def test_preflight_submission_error_does_not_escape_qt_callback(self):
        owner = self.owner()
        callback = mock.Mock(side_effect=OSError("只读配置"))
        owner.request_task_asset_refresh(callback)
        self.drain(owner)
        callback.assert_called_once()
        self.assertTrue(any("任务提交失败" in line for line in owner.logs))

    def test_import_same_names_preserves_both_sources_and_existing_file(self):
        existing = self.file("target/same.wav", b"original")
        a = self.file("one/same.wav", b"one")
        b = self.file("two/same.wav", b"two")
        self.assertEqual(import_files_to_folder([a, b], str(self.root / "target")), (2, 0))
        self.assertEqual(Path(existing).read_bytes(), b"original")
        self.assertEqual((self.root / "target/same_2.wav").read_bytes(), b"one")
        self.assertEqual((self.root / "target/same_3.wav").read_bytes(), b"two")

    def test_import_source_in_destination_is_skipped(self):
        src = self.file("target/same.wav")
        self.assertEqual(import_files_to_folder([src], str(self.root / "target")), (0, 1))
        self.assertEqual(len(list((self.root / "target").iterdir())), 1)

    def test_failed_copy_removes_only_its_incomplete_file(self):
        existing = self.file("target/same.wav", b"original")
        source = self.file("source/same.wav")
        with mock.patch("services.media_service.shutil.copyfileobj", side_effect=OSError("磁盘已满")):
            self.assertEqual(import_files_to_folder([source], str(self.root / "target")), (0, 1))
        self.assertEqual(Path(existing).read_bytes(), b"original")
        self.assertFalse((self.root / "target/same_2.wav").exists())

    def test_av_output_names_unique_for_same_basename_and_second(self):
        owner = SimpleNamespace(av_output_dir=str(self.root / "output"))
        with mock.patch("modules.av_processing.page_mixin.time.strftime", return_value="stamp"):
            a = AVProcessingMixin._av_output_path(owner, "one/same.wav", "转换", "mp4")
            b = AVProcessingMixin._av_output_path(owner, "two/same.wav", "转换", "mp4")
            Path(a).write_bytes(b"existing")
            owner._av_reserved_outputs = set()
            c = AVProcessingMixin._av_output_path(owner, "three/same.wav", "转换", "mp4")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(Path(a).read_bytes(), b"existing")

    def test_av_failed_file_does_not_abort_later_files(self):
        for cls in (AVProcessWorker, AddSubtitleWorker):
            worker = cls({"jobs": [{"input": "bad.wav", "output": "bad.mp4"},
                                   {"input": "good.wav", "output": "good.mp4"}]})
            results = []
            worker.finished_signal.connect(lambda ok, message: results.append((ok, message)))
            with mock.patch.object(worker, "_process_one", side_effect=[RuntimeError("bad"), "good.mp4"]) as process:
                worker.run()
            self.assertEqual(process.call_count, 2)
            self.assertEqual(worker.output_paths, ["good.mp4"])
            self.assertFalse(results[-1][0])

    def test_av_duration_probe_has_timeout_and_returns_safely(self):
        owner = SimpleNamespace(get_si=lambda: None)
        with mock.patch("modules.av_processing.page_mixin.os.path.exists", return_value=True), \
                mock.patch("modules.av_processing.page_mixin.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 8)) as run:
            self.assertEqual(AVProcessingMixin.get_media_duration(owner, "bad.mp4"), 0)
        self.assertEqual(run.call_args.kwargs["timeout"], 8)

    def test_quick_trim_cannot_overwrite_other_video_segment(self):
        segment = {"start": 1, "end": 3}
        owner = SimpleNamespace(current_path="current.mp4", _selected_segment_ref=lambda: "ref",
                                _segment_by_ref=lambda ref: ("other.mp4", 0, segment),
                                _candidate_range=lambda: (7, 9))
        with mock.patch("modules.quick_video_trim.quick_video_trim_widget.QMessageBox.information"):
            QuickVideoTrimWidget.update_selected_segment(owner)
        self.assertEqual(segment, {"start": 1, "end": 3})

    def test_quick_trim_cancel_is_not_reported_as_complete(self):
        owner = SimpleNamespace(_export_stop_requested=True, _export_total=10, _log=mock.Mock(),
                                export_current_button=mock.Mock(), export_all_button=mock.Mock(),
                                stop_export_button=mock.Mock(), export_progress=mock.Mock(), export_status=mock.Mock())
        with mock.patch("modules.quick_video_trim.quick_video_trim_widget.QMessageBox.information"):
            QuickVideoTrimWidget._on_export_finished(owner, 2, 0, str(self.root))
        owner.export_progress.setValue.assert_not_called()
        self.assertIn("未完成 8", owner.export_status.setText.call_args.args[0])
        self.assertIn("已停止", owner.export_status.setText.call_args.args[0])

    def test_fair_queue_without_progress_does_not_retry_forever(self):
        for completed, expected_status in ((3, FAILED_STATUS), (5, WAITING_STATUS)):
            owner = SimpleNamespace(_active_queue_task_id="task", _active_queue_fair=True,
                                    _active_queue_product="shop", _active_queue_completed_before=3,
                                    _queue_stop_requested=False, _queue_auto_run=False,
                                    batch_project_repo=mock.Mock(), update_log=mock.Mock(),
                                    _refresh_task_center=mock.Mock(), _restore_mix_start_controls=mock.Mock())
            ProjectQueueMixin.finish_active_queue_task(owner, completed, 10)
            self.assertEqual(owner.batch_project_repo.update_task.call_args.kwargs["status"], expected_status)

    def test_all_module_and_av_pages_can_be_constructed(self):
        import paths
        from PyQt5.QtCore import QThread, QTimer
        from ui import VideoMixerApp

        # 这里只验证所有页面的构建和导航，不启动客户模型或下载任务。
        with mock.patch.object(paths, "WORKSPACE_ROOT", str(self.root / "workspace")), \
                mock.patch.object(QTimer, "singleShot"), mock.patch.object(QThread, "start"), \
                mock.patch.object(VideoMixerApp, "apply_best_performance_profile"):
            window = VideoMixerApp()
            try:
                for index in range(len(window.nav_names)):
                    window.switch_module(index)
                    self.assertIn(index, window._module_built, window.nav_names[index])
                for index in range(window.av_stack.count()):
                    window._on_av_page_switched(index)
                    self.assertEqual(window.av_stack.currentIndex(), index)
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
