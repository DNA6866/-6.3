import copy
from datetime import datetime
import os
from pathlib import Path
import tempfile
import time
import unittest
import wave
from unittest import mock


CORE = str(Path(__file__).resolve().parents[1])
ROOT = str(Path(CORE).parent)
for directory in (ROOT, CORE):
    if directory not in os.sys.path:
        os.sys.path.insert(0, directory)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QFont  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402
from modules.batch_mixer.daily_plan_dialog import DailyPlanDialog  # noqa: E402
from modules.batch_mixer.daily_automation_mixin import DailyAutomationMixin  # noqa: E402
from services.batch_project_service import BatchProjectRepository  # noqa: E402
from services.daily_production_service import (  # noqa: E402
    allocate_shop_targets, allocate_targets, build_daily_payloads, nearest_rounds, plan_due,
    plans_in_rotation, scan_materials,
)
from services.media_index_service import MediaIndex  # noqa: E402
from services.thread_lifecycle_service import ThreadLifecycleRegistry  # noqa: E402


class AutomationOwner(QWidget, DailyAutomationMixin):
    def __init__(self, repo, extensions, cache_dir):
        super().__init__()
        self.batch_project_repo = repo
        self.tab_exts = extensions
        self.cache_dir = cache_dir
        self._thread_registry = ThreadLifecycleRegistry()
        self._editing_queue_task_id = ""
        self._active_queue_task_id = ""
        self.engine_thread = None
        self.logs = []
        self.started = 0
        self._init_daily_automation()

    def update_log(self, text):
        self.logs.append(text)

    def _refresh_task_center(self):
        pass

    def start_batch_task_queue(self):
        self.started += 1


class DailyProductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setFont(QFont("Microsoft YaHei", 10))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.index = MediaIndex(str(self.root / "index.db"))
        self.index_patch = mock.patch("services.daily_production_service.get_media_index", return_value=self.index)
        self.index_patch.start()
        self.checkpoint_patch = mock.patch("services.batch_project_service.checkpoint_path_for_task",
                                          side_effect=lambda key: str(self.root / f"{key}.json"))
        self.checkpoint_patch.start()
        self.repo = BatchProjectRepository(str(self.root / "state.json"))
        self.extensions = {"音频素材": [".wav"], "视频素材": [".mp4"], "第一轨道": [".mp4"],
                           "钩子素材": [".mp4"], "AI开场视频": [".mp4"],
                           "第二段指定素材": [".mp4"], "背景音乐": [".wav"], "封面图片": [".png"]}

    def tearDown(self):
        self.checkpoint_patch.stop()
        self.index_patch.stop()
        self.temp.cleanup()

    def file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test media")
        os.utime(path, (time.time() - 30, time.time() - 30))
        return str(path)

    def project(self, pid="a", count=5, mode="traditional"):
        tab = "第一轨道" if mode in {"first_track", "ai_intro_first_track"} else "音频素材"
        suffix = "mp4" if tab == "第一轨道" else "wav"
        for i in range(count):
            self.file(f"{pid}/drive/sub/{i}.{suffix}")
        self.file(f"{pid}/video/product.mp4")
        return {"id": pid, "shop_name": "测试店铺", "product_name": "锦裕禾",
                "directories": {tab: str(self.root / pid / "drive"),
                                "视频素材": str(self.root / pid / "video"),
                                "保存视频": str(self.root / "output")},
                "rules": {"mixer_mode": mode, "loop_count": 99, "gpu": "Nvidia NVENC 加速",
                          "threads": 3, "strong_structure": True, "min_clip": 2.5},
                "subtitle": {"enable": True, "color": "white", "border_color": "随机颜色", "shadow": 2},
                "watermarks": [{"text": "店铺专属水印"}], "cover": {"text": "商品"}}

    def plan(self, ids=("a", "b")):
        return self.repo.save_daily_plan({"id": "daily", "name": "锦裕禾每日生产", "enabled": True,
                                          "time": "09:00", "total": 800, "project_ids": list(ids)})

    def build(self, plan, projects, runs=None, day="2026-09-13"):
        return build_daily_payloads(plan, projects, runs or {}, day, self.extensions,
                                    str(self.root / "cache"), probe=lambda path: 5.0)

    def test_total_is_shared_only_by_selected_templates(self):
        self.assertEqual(allocate_targets(800, ["a", "b"]), {"a": 400, "b": 400})
        self.assertEqual(allocate_targets(800, ["a"]), {"a": 800})
        self.assertEqual(allocate_targets(800, ["a", "b", "c"]), {"a": 267, "b": 267, "c": 266})
        with self.assertRaises(ValueError):
            allocate_targets(0, ["a"])

    def test_nearest_whole_rounds(self):
        for count, expected in ((5, (160, 800)), (6, (133, 798)), (7, (114, 798)), (9, (89, 801))):
            self.assertEqual(nearest_rounds(800, count), expected)
        with self.assertRaises(ValueError):
            nearest_rounds(800, 0)

    def test_each_shop_gets_800_and_only_its_selected_templates_share_it(self):
        projects = [{"id": pid, "shop_name": shop} for pid, shop in
                    (("a", "A店"), ("b", " A店 "), ("c", "B店"), ("d", "B店"), ("e", "C店"))]
        groups = allocate_shop_targets(800, ["a", "b", "c", "d", "a"], projects)
        self.assertEqual(groups, [
            {"shop_name": "A店", "target": 800, "targets": {"a": 400, "b": 400}},
            {"shop_name": "B店", "target": 800, "targets": {"c": 400, "d": 400}},
        ])
        self.assertEqual(sum(group.get("target") for group in groups), 1600)

    def test_unequal_template_counts_do_not_take_other_shops_quota(self):
        projects = [{"id": pid, "shop_name": "A店" if pid != "d" else "B店"} for pid in "abcd"]
        groups = allocate_shop_targets(800, list("abcd"), projects)
        self.assertEqual(groups[0].get("targets"), {"a": 267, "b": 267, "c": 266})
        self.assertEqual(groups[1].get("targets"), {"d": 800})
        with self.assertRaises(ValueError):
            allocate_shop_targets(800, ["missing"], projects)
        with self.assertRaises(ValueError):
            allocate_shop_targets(800, ["a"], [{"id": "a", "shop_name": " "}])

    def test_legacy_plan_total_is_per_shop_for_new_payloads(self):
        projects = [self.project(pid) for pid in "abcd"]
        for index, project in enumerate(projects):
            project["shop_name"] = "A店" if index < 2 else "B店"
        plan = dict(self.plan(list("abcd")), name="锦玉河")
        tasks, _ = self.build(plan, projects)
        self.assertEqual([task.get("target") for task in tasks], [400] * 4)
        self.assertEqual(sum(task.get("expected") for task in tasks), 1600)
        self.assertEqual({task.get("shop_name") for task in tasks}, {"A店", "B店"})
        self.assertTrue(all(task.get("shop_target") == 800 for task in tasks))
        self.assertTrue(all(task.get("quantity_scope") == "per_shop" for task in tasks))
        self.assertTrue(all("锦玉河" not in task.get("output_dir") for task in tasks))

    def test_both_modes_count_all_files_and_keep_independent_settings(self):
        projects = [self.project(), self.project("b", 4, "first_track"), self.project("not_selected", 9)]
        before = copy.deepcopy(projects)
        tasks, hooks = self.build(self.plan(), projects)
        a, b = tasks
        self.assertEqual(a.get("expected"), 400)
        self.assertEqual(a.get("task_data").get("loop_count"), 80)
        self.assertEqual(b.get("task_data").get("loop_count"), 100)
        self.assertFalse(a.get("task_data").get("strong_structure"))
        self.assertTrue(b.get("task_data").get("strong_structure"))
        self.assertEqual(b.get("task_data").get("audios")[2].get("track_index"), 2)
        self.assertEqual(a.get("task_data").get("sub_config").get("color"), "#FFFFFF")
        self.assertEqual(a.get("task_data").get("watermarks"), before[0].get("watermarks"))
        self.assertEqual(projects, before)
        self.assertNotEqual(a.get("output_dir"), b.get("output_dir"))
        self.assertIn("2026-09-13", a.get("output_dir"))
        self.assertEqual(hooks, {})

    def test_new_files_in_deep_folder_are_in_next_snapshot(self):
        project = self.project()
        plan = self.plan(["a"])
        original, _ = self.build(plan, [project])
        self.file("a/drive/deep/inside/new.wav")
        updated, _ = self.build(plan, [project])
        self.assertEqual(original[0].get("expected"), 800)
        self.assertEqual(len(original[0].get("task_data").get("audios")), 5)
        self.assertEqual(updated[0].get("expected"), 798)
        self.assertEqual(len(updated[0].get("task_data").get("audios")), 6)

    def test_bad_driver_is_reported_and_not_counted(self):
        project = self.project(count=6)

        def probe(path):
            if path.endswith("5.wav"):
                raise ValueError("5.wav：损坏文件")
            return 5.0

        tasks, _ = build_daily_payloads(self.plan(["a"]), [project], {}, "2026-09-13",
                                        self.extensions, str(self.root / "cache"), probe=probe)
        self.assertEqual(tasks[0].get("expected"), 800)
        self.assertEqual(len(tasks[0].get("task_data").get("audios")), 5)
        self.assertEqual(len(tasks[0].get("skipped_drivers")), 1)

    def test_hook_rotation_new_then_oldest(self):
        project = self.project()
        first = self.file("hooks/batch1/one.mp4")
        self.file("hooks/batch2/two.mp4")
        (self.root / "hooks" / "empty").mkdir()
        plan = self.plan(["a"])
        plan["hook_roots"] = {"a": str(self.root / "hooks")}
        tasks, hooks = self.build(plan, [project])
        self.assertEqual(tasks[0].get("task_data").get("hook_videos")[0].get("path"), first)
        runs = {"daily:2026-09-13": {"day": "2026-09-13", "hooks": hooks}}
        _, next_hooks = self.build(plan, [project], runs, "2026-09-14")
        self.assertTrue(next_hooks.get("a").endswith("batch2"))
        runs["daily:2026-09-14"] = {"day": "2026-09-14", "hooks": next_hooks}
        _, oldest = self.build(plan, [project], runs, "2026-09-15")
        self.assertEqual(oldest, hooks)
        self.file("hooks/new_batch/three.mp4")
        _, new = self.build(plan, [project], runs, "2026-09-15")
        self.assertTrue(new.get("a").endswith("new_batch"))

    def hook_fixture(self, folders=4):
        projects = [self.project(pid=pid, count=1) for pid in "abcd"]
        root = str(self.root / "hooks")
        for index in range(folders):
            self.file(f"hooks/batch{index + 1}/clip.mp4")
        plan = self.plan(tuple("abcd"))
        plan["hook_roots"] = {pid: root for pid in "abcd"}
        return plan, projects

    def test_shared_hook_root_assigns_four_distinct_folders(self):
        plan, projects = self.hook_fixture()
        tasks, choices = self.build(plan, projects)
        self.assertEqual(len(set(choices.values())), 4)
        for task in tasks:
            folder = choices[task.get("project_id")]
            self.assertEqual(task.get("directories", {}).get("钩子素材"), folder)
            self.assertTrue(all(Path(item.get("path")).is_relative_to(folder)
                                for item in task.get("task_data", {}).get("hook_videos", [])))

    def test_shared_hook_shortage_balances_reuse(self):
        for folders in (1, 2):
            with self.subTest(folders=folders):
                plan, projects = self.hook_fixture(folders)
                _, choices = self.build(plan, projects)
                counts = {folder: list(choices.values()).count(folder) for folder in choices.values()}
                self.assertEqual(len(counts), folders)
                self.assertEqual(set(counts.values()), {4 // folders})

    def test_empty_hook_folders_do_not_prevent_valid_reuse(self):
        plan, projects = self.hook_fixture(2)
        (self.root / "hooks/000empty").mkdir()
        self.file("hooks/001not_video/readme.txt")
        _, choices = self.build(plan, projects)
        self.assertEqual({Path(path).name for path in choices.values()}, {"batch1", "batch2"})

    def test_hook_assignments_survive_restart_and_coordinate_other_plans(self):
        plan, projects = self.hook_fixture()
        plan["project_ids"] = ["a"]
        plan = self.repo.save_daily_plan(plan)
        tasks, choices = self.build(plan, projects)
        queued = self.repo.add_daily_batch(plan, "2026-09-13", tasks, choices)
        restarted = BatchProjectRepository(self.repo.state_path)
        before = restarted.task(queued[0].get("id"))
        other = copy.deepcopy(plan)
        other.update({"id": "second", "project_ids": list("bcd")})
        _, next_choices = self.build(other, projects, restarted.daily_state().get("runs", {}))
        self.assertEqual(len(set(choices.values()) | set(next_choices.values())), 4)
        self.assertEqual(restarted.task(queued[0].get("id")), before)
        self.assertEqual(restarted.add_daily_batch(plan, "2026-09-13", tasks, choices), [])

    def test_equivalent_root_spellings_still_share_today_counts(self):
        plan, projects = self.hook_fixture()
        plan["hook_roots"]["b"] += os.sep + "."
        plan["hook_roots"]["c"] = plan["hook_roots"]["c"].upper()
        _, choices = self.build(plan, projects)
        self.assertEqual(len({os.path.normcase(os.path.abspath(path)) for path in choices.values()}), 4)

    def test_different_hook_roots_do_not_share_same_named_children(self):
        plan, projects = self.hook_fixture(1)
        self.file("other_hooks/batch1/clip.mp4")
        for pid in "cd":
            plan["hook_roots"][pid] = str(self.root / "other_hooks")
        _, choices = self.build(plan, projects)
        self.assertEqual(choices.get("a"), choices.get("b"))
        self.assertEqual(choices.get("c"), choices.get("d"))
        self.assertNotEqual(choices.get("a"), choices.get("c"))

    def test_global_hook_history_prefers_unused_then_oldest(self):
        plan, projects = self.hook_fixture(2)
        plan["project_ids"] = ["a"]
        first = str(self.root / "hooks/batch1")
        second = str(self.root / "hooks/batch2")
        runs = {"other:yesterday": {"day": "2026-09-12", "hooks": {"other_template": first}}}
        _, choices = self.build(plan, projects, runs)
        self.assertEqual(choices.get("a"), second)
        runs["different:older"] = {"day": "2026-09-11", "hooks": {"unknown": second}}
        _, choices = self.build(plan, projects, runs)
        self.assertEqual(choices.get("a"), second)
        self.file("hooks/new/clip.mp4")
        _, choices = self.build(plan, projects, runs)
        self.assertEqual(choices.get("a"), str(self.root / "hooks/new"))

    def test_today_hook_counts_reset_on_new_day(self):
        plan, projects = self.hook_fixture(2)
        first, second = [str(self.root / f"hooks/batch{i}") for i in (1, 2)]
        runs = {"old:2026-09-12": {"day": "2026-09-12",
                                  "hooks": {"a": first, "b": first, "c": first, "d": second}}}
        _, choices = self.build(plan, projects, runs)
        self.assertEqual(list(choices.values()).count(first), 2)
        self.assertEqual(list(choices.values()).count(second), 2)

    def test_failed_preparation_does_not_consume_hook_choices(self):
        plan, projects = self.hook_fixture()
        runs = {}
        original_projects = copy.deepcopy(projects)
        with self.assertRaises(ValueError):
            build_daily_payloads(plan, projects, runs, "2026-09-13", self.extensions,
                                 str(self.root / "cache"), probe=lambda path: 0)
        self.assertEqual(runs, {})
        self.assertEqual(projects, original_projects)
        self.assertEqual(self.repo.daily_state().get("runs", {}), {})
        _, choices = self.build(plan, projects)
        self.assertEqual(Path(choices.get("a")).name, "batch1")

    def test_ai_mode_ignores_hooks_and_requires_intro(self):
        project = self.project("a", 5, "ai_intro_first_track")
        plan = self.plan(["a"])
        plan["hook_roots"] = {"a": "Z:/offline"}
        with self.assertRaises(ValueError):
            self.build(plan, [project])
        self.file("intro/one.mp4")
        project.get("directories")["AI开场视频"] = str(self.root / "intro")
        tasks, hooks = self.build(plan, [project])
        self.assertTrue(tasks[0].get("task_data").get("ai_intro_mode"))
        self.assertEqual(tasks[0].get("task_data").get("hook_videos"), [])
        self.assertEqual(hooks, {})

    def test_daily_batch_is_atomic_and_idempotent_after_restart_and_clear(self):
        plan = self.plan(["a"])
        payloads, hooks = self.build(plan, [self.project()])
        tasks = self.repo.add_daily_batch(plan, "2026-09-13", payloads, hooks)
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertTrue(task.get("task_data").get("audios")[0].get("resume_source_id"))
        self.repo.update_task(task.get("id"), status="completed", completed=800)
        self.repo.clear_completed()
        restarted = BatchProjectRepository(str(self.root / "state.json"))
        self.assertEqual(restarted.add_daily_batch(plan, "2026-09-13", payloads, hooks), [])
        self.assertEqual(restarted.tasks(), [])

    def test_suspend_and_revision_changes_discard_inflight_preparation(self):
        plan = self.plan(["a"])
        payloads, hooks = self.build(plan, [self.project()])
        self.repo.suspend_daily(True)
        self.assertEqual(self.repo.add_daily_batch(plan, "2026-09-13", payloads, hooks), [])
        self.repo.suspend_daily(False)
        self.repo.save_daily_plan(dict(plan, total=1000))
        self.assertEqual(self.repo.add_daily_batch(plan, "2026-09-13", payloads, hooks), [])
        self.assertEqual(self.repo.daily_state().get("runs", {}), {})

    def test_failed_commit_does_not_leave_partial_queue_in_memory(self):
        plan = self.plan(["a"])
        payloads, hooks = self.build(plan, [self.project()])
        with mock.patch.object(self.repo, "_save_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.repo.add_daily_batch(plan, "2026-09-13", payloads, hooks)
        self.assertEqual(self.repo.tasks(), [])
        self.assertEqual(self.repo.daily_state().get("runs", {}), {})

    def test_due_date_does_not_backfill_or_repeat(self):
        plan = self.plan(["a"])
        self.assertFalse(plan_due(plan, {}, datetime(2026, 9, 13, 8, 59)))
        self.assertTrue(plan_due(plan, {}, datetime(2026, 9, 13, 9, 0)))
        self.assertFalse(plan_due(plan, {"daily:2026-09-13": {}}, datetime(2026, 9, 13, 23, 0)))
        self.assertTrue(plan_due(plan, {"daily:2026-09-13": {}}, datetime(2026, 9, 14, 9, 0)))

    def test_long_running_plan_does_not_starve_other_products(self):
        plans = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        runs = {"a:2026-09-13": {"day": "2026-09-13"}, "b:2026-09-12": {"day": "2026-09-12"}}
        self.assertEqual([p.get("id") for p in plans_in_rotation(plans, runs)], ["c", "b", "a"])

    def test_unfinished_queue_not_trimmed_at_200(self):
        self.repo._state["tasks"] = [{"id": str(i), "status": "waiting"} for i in range(220)]
        self.repo.add_task({"task_name": "last"})
        self.assertEqual(len(self.repo.tasks(False)), 221)

    def test_scan_offline_does_not_return_empty_and_skips_partial_files(self):
        with self.assertRaises(OSError):
            scan_materials(str(self.root / "missing"), [".mp4"])
        self.file("video/ok.mp4")
        new = self.file("video/copying.mp4")
        os.utime(new, None)
        self.assertEqual(len(scan_materials(str(self.root / "video"), [".mp4"])), 1)
        with self.assertRaises(InterruptedError):
            scan_materials(str(self.root / "video"), [".mp4"], lambda: True)

    def test_manual_pause_blocks_timer(self):
        self.plan(["a"])
        owner = mock.Mock()
        owner._daily_closing = False
        owner._daily_worker = None
        owner._daily_forced_plan_ids = set()
        owner.batch_project_repo = self.repo
        self.repo.suspend_daily(True)
        DailyAutomationMixin._daily_tick(owner)
        owner.start_batch_task_queue.assert_not_called()

    def test_background_prepare_real_audio_reaches_queue_without_touching_template(self):
        project = self.project(count=1)
        source = self.root / "a/drive/sub/0.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x00" * 16000)
        os.utime(source, (time.time() - 30, time.time() - 30))
        self.repo.upsert_project(project)
        plan = self.plan(["a"])
        owner = AutomationOwner(self.repo, self.extensions, str(self.root / "cache"))
        try:
            owner._daily_tick(force_plan_id=plan.get("id"))
            deadline = time.monotonic() + 20
            while owner._daily_worker is not None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.app.processEvents()
            self.assertIsNone(owner._daily_worker)
            self.assertEqual(len(self.repo.tasks()), 1, owner.logs)
            self.assertEqual(owner.started, 1)
            self.assertEqual(self.repo.tasks()[0].get("expected"), 800)
            self.assertEqual(self.repo.project("a").get("rules").get("loop_count"), 99)
            self.assertGreater(self.index.metadata(str(source)).get("duration", 0), 0)
        finally:
            owner._close_daily_automation()
            owner._thread_registry.shutdown(force=False)
            self.app.processEvents()
            owner.close()

    def test_dialog_layout_and_shared_total(self):
        for i in range(4):
            self.repo.upsert_project(self.project(str(i)))
        owner = QWidget()
        owner.batch_project_repo = self.repo
        dialog = DailyPlanDialog(owner)
        dialog.table.item(0, 0).setCheckState(2)
        dialog.table.item(1, 0).setCheckState(2)
        dialog.show()
        self.app.processEvents()
        self.assertIn("400 / 400", dialog.allocation.text())
        self.assertGreater(dialog.table.columnWidth(2), 100)
        self.assertGreater(dialog.table.height(), 150)
        screenshot = os.environ.get("DAILY_UI_SCREENSHOT")
        if screenshot:
            dialog.grab().save(screenshot)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Discard):
            dialog.close()
        owner.close()

    def test_dialog_displays_shop_totals_and_template_allocations(self):
        for index, pid in enumerate("abcd"):
            project = self.project(pid)
            project["shop_name"] = "A店" if index < 2 else "B店"
            self.repo.upsert_project(project)
        owner = QWidget()
        owner.batch_project_repo = self.repo
        dialog = DailyPlanDialog(owner)
        dialog.name.setText("锦玉河")
        for row in range(4):
            dialog.table.item(row, 0).setCheckState(2)
        self.assertIn("2 家店，每店 800 条，合计目标 1600 条", dialog.allocation.text())
        self.assertEqual([dialog.table.item(row, 3).text() for row in range(4)], ["400 条"] * 4)
        dialog.show()
        self.app.processEvents()
        screenshot = os.environ.get("DAILY_SHOP_UI_SCREENSHOT")
        if screenshot:
            dialog.grab().save(screenshot)
        dialog.table.item(3, 0).setCheckState(0)
        self.assertEqual(dialog.table.item(2, 3).text(), "800 条")
        self.assertEqual(dialog.table.item(3, 3).text(), "未参与")
        dialog.save_button.click()
        saved = self.repo.daily_state().get("plans", [])[0]
        self.assertEqual(saved.get("total"), 800)
        self.assertEqual(saved.get("quantity_scope"), "per_shop")
        dialog.close()
        owner.close()

    def test_create_button_saves_filled_form_and_selects_visible_plan(self):
        self.repo.upsert_project(self.project())
        owner = QWidget()
        owner.batch_project_repo = self.repo
        dialog = DailyPlanDialog(owner)
        dialog.name.setText("锦裕禾每日800条")
        dialog.total.setValue(800)
        dialog.table.item(0, 0).setCheckState(2)
        dialog.table.item(0, 2).setText("Z:/每日钩子")
        self.assertEqual(dialog.save_button.text(), "创建计划")
        dialog.save_button.click()
        saved = self.repo.daily_state().get("plans", [])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].get("hook_roots", {}).get("a"), "Z:/每日钩子")
        self.assertEqual(dialog.plans.currentItem().text(), "锦裕禾每日800条")
        self.assertEqual(dialog.save_button.text(), "保存修改")
        self.assertIn("已保存到左侧", dialog.status.text())
        dialog.show()
        self.app.processEvents()
        screenshot = os.environ.get("DAILY_UI_CREATED_SCREENSHOT")
        if screenshot:
            dialog.grab().save(screenshot)
        dialog.total.setValue(900)
        dialog.save_button.click()
        self.assertEqual(len(self.repo.daily_state().get("plans", [])), 1)
        self.assertEqual(self.repo.daily_state().get("plans", [])[0].get("total"), 900)
        dialog.close()
        reopened = DailyPlanDialog(owner)
        self.assertEqual(reopened.name.text(), "锦裕禾每日800条")
        self.assertEqual(reopened.total.value(), 900)
        reopened.close()
        owner.close()

    def test_new_and_close_do_not_silently_discard_draft(self):
        self.repo.upsert_project(self.project())
        owner = QWidget()
        owner.batch_project_repo = self.repo
        dialog = DailyPlanDialog(owner)
        dialog.name.setText("未保存计划")
        dialog.table.item(0, 0).setCheckState(2)
        dialog.show()
        self.app.processEvents()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel):
            dialog._new()
            self.assertEqual(dialog.name.text(), "未保存计划")
            dialog.close()
            self.assertTrue(dialog.isVisible())
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Save):
            dialog._new()
        self.assertEqual(self.repo.daily_state().get("plans", [])[0].get("name"), "未保存计划")
        self.assertEqual(dialog.name.text(), "")
        self.assertEqual(dialog.save_button.text(), "创建计划")
        dialog.close()
        owner.close()

    def test_switching_plan_can_save_current_changes_without_losing_target(self):
        self.repo.upsert_project(self.project())
        self.plan(["a"])
        self.repo.save_daily_plan({"id": "second", "name": "第二计划", "project_ids": ["a"], "total": 600})
        owner = QWidget()
        owner.batch_project_repo = self.repo
        dialog = DailyPlanDialog(owner)
        dialog.total.setValue(1000)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel):
            dialog.plans.setCurrentRow(1)
        self.assertEqual(dialog.plan.get("id"), "daily")
        self.assertEqual(dialog.plans.currentRow(), 0)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Save):
            dialog.plans.setCurrentRow(1)
        self.assertEqual(dialog.plan.get("id"), "second")
        first = next(p for p in self.repo.daily_state().get("plans", []) if p.get("id") == "daily")
        self.assertEqual(first.get("total"), 1000)
        dialog.close()
        owner.close()

    def test_main_window_has_daily_entry_and_does_not_activate_unconfigured_plans(self):
        import paths
        from PyQt5.QtCore import QTimer
        from PyQt5.QtWidgets import QPushButton
        from ui import VideoMixerApp

        with mock.patch.object(paths, "WORKSPACE_ROOT", str(self.root / "workspace")), \
                mock.patch.object(QTimer, "singleShot"), \
                mock.patch.object(VideoMixerApp, "apply_best_performance_profile"):
            window = VideoMixerApp()
            try:
                buttons = [button.text() for button in window.findChildren(QPushButton)]
                self.assertIn("每日自动任务", buttons)
                self.assertTrue(window._daily_timer.isActive())
                window._daily_tick()
                self.assertIsNone(window._daily_worker)
                self.assertEqual(window.batch_project_repo.tasks(), [])
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
