import os
import random
import sys
import tempfile
import unittest


CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(CORE_DIR)
for path in (PROJECT_DIR, CORE_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from services.asset_usage_service import (  # noqa: E402
    SELECTION_SAMPLE_SIZE,
    AssetUsageScheduler,
)
from services.batch_project_service import BatchProjectRepository  # noqa: E402
from services.render_checkpoint_service import (  # noqa: E402
    adopt_existing_outputs,
    checkpoint_progress,
)
from services.render_planner import (  # noqa: E402
    MAX_MEDIA_SELECTION_ATTEMPTS,
    MediaSegmentPlanner,
    RenderSampler,
)


def _sampler():
    return RenderSampler(
        target_w=720,
        target_h=1280,
        zoom_min=1.01,
        zoom_max=1.10,
        zoom_mode="slow_push_in",
        offset_strength=0.5,
        legacy_offset=None,
        clip_rhythm="balanced",
        min_clip=2.0,
        max_clip=2.5,
        clip_jitter=0.5,
        overlay_gap_min=1.5,
        overlay_gap_max=3.5,
    )


class RenderPerformanceGuardTests(unittest.TestCase):
    def test_sub_frame_tail_does_not_select_material(self):
        calls = []
        planner = MediaSegmentPlanner(
            duration_getter=lambda _path: 5.0,
            cached_source_getter=lambda path: path,
            render_sampler=_sampler(),
            asset_selector=lambda candidates, _kind: calls.append(candidates) or candidates[0],
        )
        result = planner.choose_random_product_segment(
            ["unused.mp4"],
            start_time=10.0,
            remaining=0.49,
        )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_invalid_material_attempts_are_bounded(self):
        calls = []
        paths = [f"missing_{index}.mp4" for index in range(2000)]
        planner = MediaSegmentPlanner(
            duration_getter=lambda _path: 0.0,
            cached_source_getter=lambda path: path,
            render_sampler=_sampler(),
            asset_selector=lambda candidates, _kind: calls.append(1) or candidates[0],
        )
        result = planner.choose_random_product_segment(paths, 0.0, 3.0)
        self.assertIsNone(result)
        self.assertLessEqual(len(calls), MAX_MEDIA_SELECTION_ATTEMPTS)

    def test_usage_scheduler_only_scores_fixed_sample(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            scheduler = AssetUsageScheduler(
                state_path=os.path.join(temp_dir, "usage.json"),
                rng=random.Random(7),
            )
            inspected = []

            def metadata(path):
                inspected.append(path)
                return 1.0, 1

            scheduler._file_metadata = metadata
            selected = scheduler.choose(
                [f"video_{index}.mp4" for index in range(5000)],
                "product",
                "job",
            )
            self.assertTrue(selected)
            self.assertLessEqual(len(inspected), SELECTION_SAMPLE_SIZE)

    def test_task_summary_does_not_copy_large_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = BatchProjectRepository(
                state_path=os.path.join(temp_dir, "tasks.json")
            )
            repository._state["tasks"] = [{
                "id": "task-1",
                "status": "waiting",
                "task_data": {"videos": ["x"] * 5000},
                "directories": {"视频素材": "x"},
            }]
            summary = repository.tasks(include_payload=False)
            self.assertEqual(summary[0]["id"], "task-1")
            self.assertNotIn("task_data", summary[0])
            self.assertNotIn("directories", summary[0])
            self.assertNotIn("project_snapshot", summary[0])

    def test_editing_queue_task_replaces_snapshot_and_clears_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = BatchProjectRepository(
                state_path=os.path.join(temp_dir, "tasks.json")
            )
            original = repository.add_task({
                "task_name": "数字人方案",
                "mode_label": "数字人口播第一轨道",
                "expected": 3,
                "task_data": {
                    "audios": [{"path": "old.wav"}],
                    "loop_count": 3,
                },
            })
            checkpoint_path = original["checkpoint_path"]
            os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
            with open(checkpoint_path, "w", encoding="utf-8") as handle:
                handle.write("{}")

            updated = repository.replace_task(original["id"], {
                "task_name": "普通拼接方案",
                "mode_label": "普通视频拼接",
                "expected": 2,
                "task_data": {
                    "audios": [{"path": "new.wav"}],
                    "loop_count": 2,
                },
            })

            self.assertEqual(updated["id"], original["id"])
            self.assertEqual(updated["task_name"], "普通拼接方案")
            self.assertEqual(updated["status"], "waiting")
            self.assertEqual(updated["completed"], 0)
            self.assertEqual(updated["task_data"]["resume_task_id"], original["id"])
            self.assertFalse(os.path.exists(checkpoint_path))

    def test_existing_outputs_are_adopted_as_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = os.path.join(temp_dir, "outputs")
            os.makedirs(output_dir)
            audio_path = os.path.join(temp_dir, "口播001.wav")
            output_path = os.path.join(
                output_dir,
                "20260812_171434_口播001_12345.mp4",
            )
            with open(output_path, "wb") as handle:
                handle.write(b"0" * 2048)
            checkpoint_path = os.path.join(temp_dir, "checkpoint.json")
            task_data = {
                "audios": [{"path": audio_path}],
                "loop_count": 1,
            }
            adopted = adopt_existing_outputs(
                task_data,
                output_dir,
                checkpoint_path,
                task_id="task-1",
            )
            completed, expected = checkpoint_progress(
                task_data,
                checkpoint_path,
                task_id="task-1",
            )
            self.assertEqual(adopted, 1)
            self.assertEqual((completed, expected), (1, 1))

    def test_one_existing_output_is_not_claimed_by_duplicate_audio_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = os.path.join(temp_dir, "outputs")
            os.makedirs(output_dir)
            output_path = os.path.join(
                output_dir,
                "20260812_171434_口播001_12345.mp4",
            )
            with open(output_path, "wb") as handle:
                handle.write(b"0" * 2048)
            checkpoint_path = os.path.join(temp_dir, "checkpoint.json")
            task_data = {
                "audios": [
                    {"path": os.path.join(temp_dir, "a", "口播001.wav")},
                    {"path": os.path.join(temp_dir, "b", "口播001.wav")},
                ],
                "loop_count": 1,
            }
            adopted = adopt_existing_outputs(
                task_data,
                output_dir,
                checkpoint_path,
                task_id="task-1",
            )
            completed, expected = checkpoint_progress(
                task_data,
                checkpoint_path,
                task_id="task-1",
            )
            self.assertEqual(adopted, 1)
            self.assertEqual((completed, expected), (1, 2))

    def test_similar_audio_name_does_not_claim_another_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = os.path.join(temp_dir, "outputs")
            os.makedirs(output_dir)
            output_path = os.path.join(
                output_dir,
                "20260812_171434_口播001加长_12345.mp4",
            )
            with open(output_path, "wb") as handle:
                handle.write(b"0" * 2048)
            checkpoint_path = os.path.join(temp_dir, "checkpoint.json")
            task_data = {
                "audios": [{"path": os.path.join(temp_dir, "口播001.wav")}],
                "loop_count": 1,
            }
            adopted = adopt_existing_outputs(
                task_data,
                output_dir,
                checkpoint_path,
                task_id="task-1",
            )
            completed, expected = checkpoint_progress(
                task_data,
                checkpoint_path,
                task_id="task-1",
            )
            self.assertEqual(adopted, 0)
            self.assertEqual((completed, expected), (0, 1))


if __name__ == "__main__":
    unittest.main()
