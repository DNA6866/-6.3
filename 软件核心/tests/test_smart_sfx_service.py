import json
import os
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch


CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(CORE_DIR)
for path in (PROJECT_DIR, CORE_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from services import smart_sfx_service as sfx  # noqa: E402


def _empty_external_library():
    result = {family: [] for family in sfx.SFX_FAMILIES}
    result["general"] = []
    result["jianying"] = []
    return result


class SmartSfxServiceTests(unittest.TestCase):
    def setUp(self):
        with sfx._selection_lock:
            sfx._recent_asset_keys.clear()
            sfx._recent_families.clear()
            sfx._family_usage.clear()

    def test_original_asset_is_valid_stereo_pcm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ffmpeg_path = os.path.join(temp_dir, "ffmpeg.exe")
            with open(ffmpeg_path, "wb") as handle:
                handle.write(b"test")
            output_path, duration = sfx.ensure_sfx_asset(
                ffmpeg_path,
                temp_dir,
                "impact",
                3,
            )
            self.assertGreater(duration, 0.3)
            self.assertGreater(os.path.getsize(output_path), 2048)
            with wave.open(output_path, "rb") as handle:
                self.assertEqual(handle.getnchannels(), 2)
                self.assertEqual(handle.getsampwidth(), 2)
                self.assertEqual(handle.getframerate(), 48000)
                self.assertGreater(handle.getnframes(), 10000)

    def test_plan_avoids_adjacent_family_repetition(self):
        candidates = [
            {
                "time": float(index) * 1.5,
                "kind": "intro" if index == 0 else "transition",
                "priority": 3 if index == 0 else 1,
                "style": "wipeleft",
            }
            for index in range(30)
        ]
        with patch.object(sfx, "_scan_external_library", _empty_external_library), patch.object(
            sfx,
            "ensure_sfx_asset",
            side_effect=lambda _ffmpeg, cache, family, variant: (
                os.path.join(cache, f"{family}_{variant}.wav"),
                0.5,
            ),
        ):
            events = sfx.plan_smart_sfx(
                "ffmpeg.exe",
                45.0,
                candidates,
                "strong",
                "density-test",
                cache_dir="cache",
                base_gain=2.0,
            )
        self.assertGreaterEqual(len(events), 3)
        self.assertLessEqual(len(events), 10)
        self.assertTrue(all(event.volume > 1.3 for event in events))
        for previous, current in zip(events, events[1:]):
            self.assertNotEqual(previous.family, current.family)

    def test_cross_video_first_family_rotates(self):
        candidate = [{"time": 0.0, "kind": "intro", "priority": 3}]
        with patch.object(sfx, "_scan_external_library", _empty_external_library), patch.object(
            sfx,
            "ensure_sfx_asset",
            side_effect=lambda _ffmpeg, cache, family, variant: (
                os.path.join(cache, f"{family}_{variant}.wav"),
                0.5,
            ),
        ):
            first = sfx.plan_smart_sfx(
                "ffmpeg.exe", 10.0, candidate, seed="video-1", cache_dir="cache"
            )
            second = sfx.plan_smart_sfx(
                "ffmpeg.exe", 10.0, candidate, seed="video-2", cache_dir="cache"
            )
        self.assertNotEqual(first[0].family, second[0].family)

    def test_event_limit_still_covers_late_timeline(self):
        candidates = [
            {"time": 0.0, "kind": "hook", "priority": 3, "style": "intro"},
            *[
                {
                    "time": float(second),
                    "kind": "transition",
                    "priority": 2,
                    "style": "zoom",
                }
                for second in range(2, 60, 2)
            ],
            {"time": 59.0, "kind": "ending", "priority": 3, "style": "ending"},
        ]
        with patch.object(sfx, "_scan_external_library", _empty_external_library), patch.object(
            sfx,
            "ensure_sfx_asset",
            side_effect=lambda _ffmpeg, cache, family, variant: (
                os.path.join(cache, f"{family}_{variant}.wav"),
                0.5,
            ),
        ):
            events = sfx.plan_smart_sfx(
                "ffmpeg.exe",
                60.0,
                candidates,
                "standard",
                "timeline-distribution",
                cache_dir="cache",
            )
        self.assertEqual(len(events), 9)
        self.assertEqual(events[0].time, 0.0)
        self.assertTrue(any(event.time >= 45.0 for event in events))

    def test_transition_style_changes_family_weights(self):
        directional = dict(sfx._family_weights("transition", "wipeleft"))
        pixel = dict(sfx._family_weights("transition", "pixelize"))
        fade = dict(sfx._family_weights("transition", "dissolve"))
        self.assertGreater(directional["whoosh"], directional["snap"])
        self.assertGreater(pixel["glitch"], pixel["whoosh"])
        self.assertGreater(fade["settle"], fade["whoosh"])

    def test_mix_filter_ducks_base_audio(self):
        event = sfx.SmartSfxEvent(
            time=1.25,
            kind="product",
            family="pop",
            variant=2,
            path="pop.wav",
            duration=0.4,
            volume=1.2,
        )
        filters = sfx.build_sfx_mix_filters("base", [(2, event)], 48000, "out")
        graph = ";".join(filters)
        self.assertIn("adelay=1250:all=1", graph)
        self.assertIn("sidechaincompress=", graph)
        self.assertIn("duration=first", graph)
        self.assertIn("[out]", graph)

    def test_custom_asset_is_classified_by_chinese_folder(self):
        path = os.path.join("智能音效", "转场", "快速划过.wav")
        self.assertEqual(sfx._classify_external_asset(path), "whoosh")

    def test_jianying_download_manifest_is_read_safely(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = os.path.join(temp_dir, "music")
            os.makedirs(cache_dir)
            valid_path = os.path.join(cache_dir, "valid.mp3")
            with open(valid_path, "wb") as handle:
                handle.write(b"audio" * 700)
            outside_path = os.path.join(temp_dir, "outside.mp3")
            with open(outside_path, "wb") as handle:
                handle.write(b"outside" * 500)
            config = {
                "list": [
                    {"date": "2", "path": "valid.mp3"},
                    {"date": "3", "path": "..\\outside.mp3"},
                    {"date": "1", "path": "valid.mp3"},
                    {"date": "4", "path": "missing.mp3"},
                ]
            }
            with open(
                os.path.join(cache_dir, "downLoadcfg"),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(config, handle)
            paths = sfx._load_jianying_downloaded_sfx(cache_dir)
        self.assertEqual(paths, [valid_path])

    def test_jianying_library_is_used_before_general_fallback(self):
        library = _empty_external_library()
        library["jianying"] = ["jianying.mp3"]
        library["impact"] = ["classified_custom.mp3"]
        library["general"] = ["general.wav"]
        with patch.object(sfx, "_scan_external_library", return_value=library), patch.object(
            sfx,
            "_prepare_external_asset",
            side_effect=lambda _ffmpeg, _cache, _family, source: (source, 0.5),
        ):
            events = sfx.plan_smart_sfx(
                "ffmpeg.exe",
                10.0,
                [{"time": 0.0, "kind": "intro", "priority": 3}],
                seed="jianying-priority",
                cache_dir="cache",
            )
        self.assertEqual(events[0].path, "jianying.mp3")
        self.assertEqual(events[0].origin, "jianying")
        self.assertEqual(sfx.smart_sfx_origin_summary(events), "剪映本地 1 个")


if __name__ == "__main__":
    unittest.main()
