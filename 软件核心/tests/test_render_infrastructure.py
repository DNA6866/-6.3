import os
import tempfile
import time
import unittest
from unittest import mock


CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(CORE_DIR)
for path in (PROJECT_DIR, CORE_DIR):
    if path not in os.sys.path:
        os.sys.path.insert(0, path)

from services.adaptive_concurrency_service import (  # noqa: E402
    choose_render_concurrency,
)
from services.fast_render_pipeline import (  # noqa: E402
    MAX_FAST_RENDER_CLIPS,
    FastClipInput,
    build_fast_render_graph,
)
from services.job_resume_service import (  # noqa: E402
    JobResumeStore,
    source_identity,
    stable_job_key,
)
from services.media_index_service import MediaIndex  # noqa: E402
from services.media_validation_service import (  # noqa: E402
    atomic_media_path,
    replace_command_output,
)
from services.render_metrics_service import (  # noqa: E402
    RenderStageProfiler,
    classify_ffmpeg_stage,
)
from services.render_checkpoint_service import RenderCheckpointStore  # noqa: E402
from services.thread_lifecycle_service import ThreadLifecycleRegistry  # noqa: E402
from services.ui_layout_service import choose_window_layout  # noqa: E402
from main import _apply_remote_4k_scale_guard  # noqa: E402


class _TransitionSelector:
    def choose(self):
        return "fade"


def _clip(index, duration=2.0):
    return FastClipInput(
        source_path=f"clip_{index}.mp4",
        start=0.25,
        input_duration=duration,
        output_duration=duration,
        video_filter="scale=720:1280",
    )


class _Signal:
    def connect(self, _callback):
        return None


class _StoppingWorker:
    def __init__(self):
        self.running = True
        self.finished = _Signal()
        self.interrupted = False

    def isRunning(self):
        return self.running

    def requestInterruption(self):
        self.interrupted = True
        self.running = False

    def wait(self, _timeout):
        return not self.running


class RenderInfrastructureTests(unittest.TestCase):
    def test_fast_concat_graph_uses_one_filter_graph(self):
        graph = build_fast_render_graph(
            [_clip(1), _clip(2), _clip(3)],
            "yuv420p",
            False,
            0.0,
            _TransitionSelector(),
        )
        self.assertEqual(graph.clip_count, 3)
        self.assertAlmostEqual(graph.duration, 6.0)
        self.assertEqual(graph.input_args.count("-i"), 3)
        self.assertIn("concat=n=3:v=1:a=0", graph.filter_complex)

    def test_fast_transition_graph_has_frame_accurate_offsets(self):
        graph = build_fast_render_graph(
            [_clip(1, 2.0), _clip(2, 3.0), _clip(3, 4.0)],
            "yuv420p",
            True,
            0.4,
            _TransitionSelector(),
        )
        self.assertAlmostEqual(graph.duration, 8.2)
        self.assertIn("offset=1.600", graph.filter_complex)
        self.assertIn("offset=4.200", graph.filter_complex)

    def test_fast_graph_rejects_oversized_plan_for_stable_fallback(self):
        with self.assertRaises(ValueError):
            build_fast_render_graph(
                [_clip(index) for index in range(MAX_FAST_RENDER_CLIPS + 1)],
                "yuv420p",
                False,
                0.0,
                _TransitionSelector(),
            )

    def test_atomic_output_keeps_extension_and_replaces_command_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "result.mp4")
            temporary = atomic_media_path(output)
            self.assertTrue(temporary.endswith(".mp4"))
            command = ["ffmpeg", "-i", "input.mp4", output]
            updated = replace_command_output(command, output, temporary)
            self.assertEqual(updated[-1], temporary)
            self.assertEqual(command[-1], output)

    def test_media_index_invalidates_metadata_after_source_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = os.path.join(temp_dir, "index.sqlite3")
            source = os.path.join(temp_dir, "素材.mp4")
            with open(source, "wb") as handle:
                handle.write(b"video")
            index = MediaIndex(database)
            index.update_metadata(source, duration=3.25, width=720, height=1280)
            cached = index.metadata(source)
            self.assertEqual(cached.get("duration"), 3.25)
            with open(source, "ab") as handle:
                handle.write(b"changed")
            self.assertEqual(index.metadata(source), {})

    def test_directory_index_reuses_recent_nas_style_listing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = os.path.join(temp_dir, "配置", "index.sqlite3")
            media_root = os.path.join(temp_dir, "素材")
            folder = os.path.join(media_root, "产品A")
            os.makedirs(folder)
            source = os.path.join(folder, "001.mp4")
            with open(source, "wb") as handle:
                handle.write(b"data")
            index = MediaIndex(database)
            listing = [("产品A/001.mp4", source)]
            index.store_directory(media_root, [".mp4"], listing)
            cached, fresh = index.cached_directory(media_root, [".mp4"])
            self.assertTrue(fresh)
            self.assertEqual(cached, listing)

    def test_auto_concurrency_caps_nas_and_slow_disk(self):
        with mock.patch(
            "services.adaptive_concurrency_service._storage_write_mbps",
            return_value=50.0,
        ), mock.patch(
            "services.adaptive_concurrency_service.is_remote_path",
            side_effect=lambda path: str(path).startswith("\\\\nas\\"),
        ), mock.patch("services.adaptive_concurrency_service.os.cpu_count", return_value=24):
            decision = choose_render_concurrency(
                8,
                "Nvidia NVENC 加速",
                "cache",
                ["\\\\nas\\产品\\1.mp4", "\\\\nas\\产品\\2.mp4"],
            )
        self.assertEqual(decision.workers, 2)
        self.assertEqual(decision.remote_sources, 2)

    def test_resume_store_only_reopens_unfinished_matching_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "resume.json")
            fingerprint = stable_job_key(["batch", 1])
            store = JobResumeStore(path, fingerprint)
            store.update("job-1", "completed", os.path.join(temp_dir, "1.mp4"))
            reopened = JobResumeStore(path, fingerprint)
            self.assertTrue(reopened.resumed)
            self.assertEqual(reopened.job("job-1").get("status"), "completed")
            reopened.finish(True)
            fresh = JobResumeStore(path, fingerprint)
            self.assertFalse(fresh.resumed)

    def test_render_checkpoint_revalidates_only_after_file_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "output.mp4")
            checkpoint = os.path.join(temp_dir, "checkpoint.json")
            with open(output, "wb") as handle:
                handle.write(b"0" * 2048)
            validator = mock.Mock(return_value=True)
            store = RenderCheckpointStore(
                checkpoint,
                task_id="task-1",
                output_validator=validator,
            )
            self.assertTrue(store.mark_completed("job-1", output))
            self.assertEqual(validator.call_count, 1)
            self.assertEqual(store.completed_keys({"job-1"}), {"job-1"})
            self.assertEqual(validator.call_count, 1)
            with open(output, "ab") as handle:
                handle.write(b"changed")
            self.assertEqual(store.completed_keys({"job-1"}), {"job-1"})
            self.assertEqual(validator.call_count, 2)

    def test_source_identity_changes_with_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "source.mov")
            with open(path, "wb") as handle:
                handle.write(b"a")
            before = source_identity(path)
            time.sleep(0.002)
            with open(path, "ab") as handle:
                handle.write(b"b")
            after = source_identity(path)
            self.assertNotEqual(before, after)

    def test_profiler_classifies_and_emits_summary(self):
        messages = []
        profiler = RenderStageProfiler("测试任务", messages.append)
        profiler.record_ffmpeg("temp_a_clip_1.ts", 1.5, True)
        profiler.record_ffmpeg("fastmix_a.ts", 2.0, True)
        with mock.patch.object(RenderStageProfiler, "_append_jsonl"):
            summary = profiler.finish(True)
        self.assertIn("片段渲染", summary)
        self.assertIn("快速合成", summary)
        self.assertEqual(messages, [summary])
        self.assertEqual(classify_ffmpeg_stage("watermark_a.ts"), "水印烧录")

    def test_thread_registry_requests_cooperative_stop(self):
        worker = _StoppingWorker()
        registry = ThreadLifecycleRegistry()
        registry.register(worker, "测试线程")
        result = registry.shutdown(timeout_ms=100, force=True)
        self.assertTrue(worker.interrupted)
        self.assertEqual(result.get("remaining"), [])

    def test_window_profiles_fit_common_screens(self):
        compact = choose_window_layout(1366, 728)
        standard = choose_window_layout(1920, 1040)
        four_k = choose_window_layout(3840, 2080)
        self.assertTrue(compact.compact_header)
        self.assertLessEqual(compact.minimum_height, 728)
        self.assertFalse(standard.compact_header)
        self.assertEqual(four_k.window_width, 2040)
        self.assertLess(four_k.window_width, 3840)

    def test_remote_4k_guard_applies_readable_scale(self):
        with mock.patch.dict(os.environ, {"QT_SCALE_FACTOR": ""}):
            with mock.patch("main.os.name", "nt"), mock.patch(
                "services.platform_service.get_primary_screen_metrics",
                return_value=(3840, 2160, 96),
            ):
                _apply_remote_4k_scale_guard()
            self.assertEqual(os.environ.get("QT_SCALE_FACTOR"), "1.5")


if __name__ == "__main__":
    unittest.main()
