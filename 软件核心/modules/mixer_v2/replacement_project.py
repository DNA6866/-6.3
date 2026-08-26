import hashlib
import json
import os
import re
import shutil
import time


PROJECT_SCHEMA_VERSION = 1
VIDEO_EXTENSIONS = (
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
    ".ts",
    ".mts",
    ".m2ts",
)
AUDIO_EXTENSIONS = (
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".wma",
)


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def safe_filename(value, fallback="未命名项目"):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "")).strip(" ._")
    return value[:80] or fallback


def atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def load_json(path, fallback=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return fallback


def source_signature(path):
    path = os.path.abspath(path)
    stat = os.stat(path)
    raw = f"{os.path.normcase(path)}|{stat.st_size}|{stat.st_mtime_ns}"
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "fingerprint": hashlib.sha1(raw.encode("utf-8")).hexdigest(),
    }


def scan_video_files(directory):
    if not directory or not os.path.isdir(directory):
        return []
    paths = []
    for root, _, names in os.walk(directory):
        for name in names:
            path = os.path.join(root, name)
            if path.lower().endswith(VIDEO_EXTENSIONS) and os.path.isfile(path):
                paths.append(os.path.abspath(path))
    return sorted(set(paths), key=lambda item: os.path.normcase(item))


def scan_audio_files(directory):
    if not directory or not os.path.isdir(directory):
        return []
    paths = []
    for root, _, names in os.walk(directory):
        for name in names:
            path = os.path.join(root, name)
            if path.lower().endswith(AUDIO_EXTENSIONS) and os.path.isfile(path):
                paths.append(os.path.abspath(path))
    return sorted(set(paths), key=lambda item: os.path.normcase(item))


class ReplacementProjectStore:
    def __init__(self, base_dir):
        self.base_dir = os.path.abspath(base_dir)
        self.projects_dir = os.path.join(self.base_dir, "局部替换项目")
        self.archived_projects_dir = os.path.join(self.base_dir, "已删除项目")
        self.cache_dir = os.path.join(self.base_dir, "缓存")
        os.makedirs(self.projects_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    @staticmethod
    def _runtime_project(data, project_path):
        result = dict(data or {})
        result["_project_path"] = os.path.abspath(project_path)
        result["_project_dir"] = os.path.dirname(os.path.abspath(project_path))
        return result

    @staticmethod
    def _persisted_project(project):
        return {
            key: value
            for key, value in dict(project or {}).items()
            if not str(key).startswith("_")
        }

    def list_projects(self):
        projects = []
        if not os.path.isdir(self.projects_dir):
            return projects
        for name in os.listdir(self.projects_dir):
            project_path = os.path.join(self.projects_dir, name, "project.json")
            data = load_json(project_path, {})
            if int((data or {}).get("schema_version", 0) or 0) != PROJECT_SCHEMA_VERSION:
                continue
            projects.append(self._runtime_project(data, project_path))
        projects.sort(
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )
        return projects

    def create_or_open(self, source_path, metadata=None):
        source_path = os.path.abspath(source_path)
        if not os.path.isfile(source_path):
            raise FileNotFoundError(f"竞品原片不存在：{source_path}")
        signature = source_signature(source_path)
        for project in self.list_projects():
            if (
                os.path.normcase(str(project.get("source_path") or ""))
                == os.path.normcase(source_path)
                and str((project.get("source_signature") or {}).get("fingerprint") or "")
                == signature["fingerprint"]
            ):
                return project

        stem = safe_filename(os.path.splitext(os.path.basename(source_path))[0])
        project_id = f"{stem}_{signature['fingerprint'][:8]}"
        project_dir = os.path.join(self.projects_dir, project_id)
        suffix = 2
        while os.path.exists(project_dir):
            project_id = f"{stem}_{signature['fingerprint'][:8]}_{suffix}"
            project_dir = os.path.join(self.projects_dir, project_id)
            suffix += 1

        shared_dir = os.path.join(project_dir, "产品公共素材池")
        intro_dir = os.path.join(project_dir, "片头素材")
        audio_dir = os.path.join(project_dir, "替换音频")
        slot_root = os.path.join(project_dir, "替换槽位")
        output_dir = os.path.join(project_dir, "输出")
        for path in (shared_dir, intro_dir, audio_dir, slot_root, output_dir):
            os.makedirs(path, exist_ok=True)

        metadata = dict(metadata or {})
        project = {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": project_id,
            "name": stem,
            "source_path": source_path,
            "source_signature": signature,
            "source_metadata": {
                "duration": float(metadata.get("duration") or 0.0),
                "width": int(metadata.get("width") or 0),
                "height": int(metadata.get("height") or 0),
                "fps": float(metadata.get("fps") or 25.0),
                "audio_codec": str(metadata.get("audio_codec") or ""),
                "video_codec": str(metadata.get("video_codec") or ""),
            },
            "shared_material_dir": "产品公共素材池",
            "intro_material_dir": "片头素材",
            "audio_material_dir": "替换音频",
            "slot_root": "替换槽位",
            "output_dir": "输出",
            "slots": [],
            "cut_points": [],
            "suppressed_boundaries": [],
            "segment_mappings": [],
            "settings": {
                "loops": 1,
                "slot_mode": "dedicated_fallback",
                "boundary_frames": 1,
                "transition_mode": "none",
                "transition_duration": 0.25,
                "smart_sfx_enabled": False,
                "smart_sfx_strength": "standard",
                "intro_enabled": False,
                "intro_external_dir": "",
                "intro_full_play": True,
                "intro_duration": 3.0,
                "audio_mode": "source",
                "audio_external_dir": "",
            },
            "created_at": now_text(),
            "updated_at": now_text(),
        }
        project_path = os.path.join(project_dir, "project.json")
        atomic_write_json(project_path, project)
        return self._runtime_project(project, project_path)

    def load(self, project_path):
        project_path = os.path.abspath(project_path)
        data = load_json(project_path, {})
        if int((data or {}).get("schema_version", 0) or 0) != PROJECT_SCHEMA_VERSION:
            raise RuntimeError("局部替换项目版本无效")
        return self._runtime_project(data, project_path)

    def save(self, project):
        project = dict(project or {})
        project_path = str(project.get("_project_path") or "")
        if not project_path:
            raise RuntimeError("项目保存路径缺失")
        project["updated_at"] = now_text()
        atomic_write_json(project_path, self._persisted_project(project))
        return self._runtime_project(self._persisted_project(project), project_path)

    def archive_project(self, project):
        """将项目移出活动列表，保留项目素材与输出，且不触碰竞品原片。"""
        project = dict(project or {})
        project_path = os.path.abspath(str(project.get("_project_path") or ""))
        project_dir = os.path.realpath(os.path.dirname(project_path))
        projects_root = os.path.realpath(self.projects_dir)
        if (
            not project_path
            or os.path.normcase(os.path.dirname(project_dir))
            != os.path.normcase(projects_root)
            or os.path.normcase(project_path)
            != os.path.normcase(os.path.join(project_dir, "project.json"))
        ):
            raise RuntimeError("拒绝移除项目目录之外的文件")
        if not os.path.isfile(project_path):
            raise FileNotFoundError("项目文件不存在，可能已被移动")

        os.makedirs(self.archived_projects_dir, exist_ok=True)
        stem = safe_filename(os.path.basename(project_dir), fallback="已删除项目")
        suffix = time.strftime("%Y%m%d_%H%M%S")
        target_dir = os.path.join(self.archived_projects_dir, f"{stem}_{suffix}")
        index = 2
        while os.path.exists(target_dir):
            target_dir = os.path.join(
                self.archived_projects_dir, f"{stem}_{suffix}_{index}"
            )
            index += 1
        shutil.move(project_dir, target_dir)
        return target_dir

    @staticmethod
    def project_dir(project):
        return os.path.abspath(str((project or {}).get("_project_dir") or ""))

    def shared_dir(self, project):
        return os.path.join(
            self.project_dir(project),
            str((project or {}).get("shared_material_dir") or "产品公共素材池"),
        )

    def output_dir(self, project):
        return os.path.join(
            self.project_dir(project),
            str((project or {}).get("output_dir") or "输出"),
        )

    def audio_dir(self, project):
        settings = dict((project or {}).get("settings") or {})
        external = str(settings.get("audio_external_dir") or "").strip()
        if external:
            return os.path.abspath(external)
        return os.path.join(
            self.project_dir(project),
            str((project or {}).get("audio_material_dir") or "替换音频"),
        )

    def audio_materials(self, project):
        return scan_audio_files(self.audio_dir(project))

    def intro_dir(self, project):
        settings = dict((project or {}).get("settings") or {})
        external = str(settings.get("intro_external_dir") or "").strip()
        if external:
            return os.path.abspath(external)
        return os.path.join(
            self.project_dir(project),
            str((project or {}).get("intro_material_dir") or "片头素材"),
        )

    def intro_materials(self, project):
        return scan_video_files(self.intro_dir(project))

    def slot_dir(self, project, slot):
        return os.path.join(
            self.project_dir(project),
            str((slot or {}).get("folder") or ""),
        )

    def _legacy_slot_folder(self, project, slot):
        external = str((slot or {}).get("external_dir") or "").strip()
        if external:
            return os.path.abspath(external)
        dedicated = self.slot_dir(project, slot)
        shared = self.shared_dir(project)
        mode = str((slot or {}).get("mode") or "dedicated_fallback")
        if mode != "shared" and scan_video_files(dedicated):
            return dedicated
        if mode != "dedicated" and scan_video_files(shared):
            return shared
        return ""

    def timeline_segments(self, project):
        """按所有标记区间的边界拆分完整时间线，并保留已有路径映射。"""
        project = dict(project or {})
        duration = float((project.get("source_metadata") or {}).get("duration") or 0.0)
        if duration <= 0:
            return []
        slots = sorted(
            [
                dict(item)
                for item in project.get("slots", [])
                if isinstance(item, dict) and item.get("enabled", True)
            ],
            key=lambda item: float(item.get("start") or 0.0),
        )
        cut_points = [
            float(value)
            for value in project.get("cut_points", [])
            if isinstance(value, (int, float))
            and 0.001 < float(value) < duration - 0.001
        ]
        if not slots and not cut_points:
            return []
        boundaries = [0.0, duration]
        for slot in slots:
            boundaries.extend(
                [
                    max(0.0, min(duration, float(slot.get("start") or 0.0))),
                    max(0.0, min(duration, float(slot.get("end") or 0.0))),
                ]
            )
        boundaries.extend(cut_points)
        suppressed = [
            float(value)
            for value in project.get("suppressed_boundaries", [])
            if isinstance(value, (int, float))
        ]
        boundaries = [
            value
            for value in boundaries
            if value <= 0.001
            or value >= duration - 0.001
            or not any(abs(value - hidden) <= 0.002 for hidden in suppressed)
        ]
        merged_boundaries = []
        for value in sorted(boundaries):
            if not merged_boundaries or value - merged_boundaries[-1] > 0.001:
                merged_boundaries.append(value)
            else:
                merged_boundaries[-1] = max(merged_boundaries[-1], value)

        had_mappings = "segment_mappings" in project
        existing = [
            dict(item)
            for item in project.get("segment_mappings", [])
            if isinstance(item, dict)
        ]

        def mapped_folder(start, end):
            for mapping in existing:
                if abs(float(mapping.get("start") or 0.0) - start) <= 0.002 and abs(
                    float(mapping.get("end") or 0.0) - end
                ) <= 0.002:
                    return str(mapping.get("folder") or "")
            overlapping = [
                mapping
                for mapping in existing
                if min(end, float(mapping.get("end") or 0.0))
                - max(start, float(mapping.get("start") or 0.0))
                > 0.001
            ]
            if overlapping:
                folders = [str(item.get("folder") or "") for item in overlapping]
                normalized = {os.path.normcase(item) for item in folders}
                if len(normalized) == 1:
                    return folders[0]
            if not had_mappings:
                for slot in slots:
                    if abs(float(slot.get("start") or 0.0) - start) <= 0.002 and abs(
                        float(slot.get("end") or 0.0) - end
                    ) <= 0.002:
                        return self._legacy_slot_folder(project, slot)
            return ""

        segments = []
        for index in range(max(0, len(merged_boundaries) - 1)):
            start = merged_boundaries[index]
            end = merged_boundaries[index + 1]
            if end - start <= 0.001:
                continue
            folder = mapped_folder(start, end)
            segments.append(
                {
                    "id": len(segments) + 1,
                    "start": round(start, 6),
                    "end": round(end, 6),
                    "duration": round(end - start, 6),
                    "folder": os.path.abspath(folder) if folder else "",
                }
            )
        return segments

    def sync_segment_mappings(self, project, save=False):
        project = dict(project or {})
        project["segment_mappings"] = self.timeline_segments(project)
        return self.save(project) if save else project

    def update_segment_folder(self, project, start, end, folder):
        project = self.sync_segment_mappings(project, save=False)
        target = next(
            (
                item
                for item in project.get("segment_mappings", [])
                if abs(float(item.get("start") or 0.0) - float(start)) <= 0.002
                and abs(float(item.get("end") or 0.0) - float(end)) <= 0.002
            ),
            None,
        )
        if target is None:
            raise RuntimeError("片段边界已经变化，请刷新后重新设置路径")
        cleaned = str(folder or "").strip().strip('"')
        target["folder"] = os.path.abspath(cleaned) if cleaned else ""
        return self.save(project), target

    def set_smart_cut_points(self, project, cut_points):
        """替换智能切点；重做智能分割时恢复之前人工合并隐藏的边界。"""
        project = dict(project or {})
        duration = float((project.get("source_metadata") or {}).get("duration") or 0.0)
        cleaned = sorted(
            {
                round(float(value), 6)
                for value in cut_points or []
                if isinstance(value, (int, float))
                and 0.001 < float(value) < duration - 0.001
            }
        )
        project["cut_points"] = cleaned
        project["suppressed_boundaries"] = []
        project["segment_mappings"] = self.timeline_segments(project)
        return self.save(project)

    def merge_segments(self, project, segment_ids):
        """合并连续片段，保留最前面的有效替换路径并返回被涉及的路径。"""
        project = dict(project or {})
        segments = self.timeline_segments(project)
        selected_ids = sorted({int(value) for value in segment_ids or [] if int(value) > 0})
        selected = [
            item for item in segments if int(item.get("id") or 0) in selected_ids
        ]
        if len(selected) < 2:
            raise ValueError("请至少选择两个相邻片段")
        positions = [segments.index(item) for item in selected]
        if positions != list(range(positions[0], positions[-1] + 1)):
            raise ValueError("只能合并编号连续的相邻片段")

        folders = [
            str(item.get("folder") or "").strip()
            for item in selected
            if str(item.get("folder") or "").strip()
        ]
        retained_folder = folders[0] if folders else ""
        hidden = [
            float(value)
            for value in project.get("suppressed_boundaries", [])
            if isinstance(value, (int, float))
        ]
        hidden.extend(float(item.get("end") or 0.0) for item in selected[:-1])
        project["suppressed_boundaries"] = sorted(
            {round(value, 6) for value in hidden if value > 0.001}
        )

        merged_start = float(selected[0].get("start") or 0.0)
        merged_end = float(selected[-1].get("end") or 0.0)
        merged_segments = self.timeline_segments(project)
        target = next(
            (
                item
                for item in merged_segments
                if abs(float(item.get("start") or 0.0) - merged_start) <= 0.002
                and abs(float(item.get("end") or 0.0) - merged_end) <= 0.002
            ),
            None,
        )
        if target is None:
            raise RuntimeError("合并后的片段边界生成失败")
        target["folder"] = os.path.abspath(retained_folder) if retained_folder else ""
        project["segment_mappings"] = merged_segments
        return self.save(project), target, folders

    @staticmethod
    def segment_materials(segment):
        return scan_video_files(str((segment or {}).get("folder") or ""))

    def replacement_segments(self, project):
        return [
            item
            for item in self.timeline_segments(project)
            if str(item.get("folder") or "").strip()
        ]

    def add_slot(self, project, start, end, label=""):
        project = dict(project or {})
        start = max(0.0, float(start))
        end = max(start, float(end))
        if end - start < 0.10:
            raise ValueError("切分区间至少需要0.10秒")
        duration = float((project.get("source_metadata") or {}).get("duration") or 0.0)
        if duration > 0:
            end = min(duration, end)
        slots = [dict(item) for item in project.get("slots", []) if isinstance(item, dict)]
        for slot in slots:
            slot_start = float(slot.get("start") or 0.0)
            slot_end = float(slot.get("end") or 0.0)
            if start < slot_end - 0.001 and end > slot_start + 0.001:
                raise ValueError(
                    f"切分区间与标记{int(slot.get('id') or 0):03d}重叠，请调整入点或出点"
                )
        slot_id = max([int(item.get("id") or 0) for item in slots] or [0]) + 1
        folder = os.path.join("替换槽位", f"槽位{slot_id:03d}_专属素材")
        slot = {
            "id": slot_id,
            "label": str(label or f"切分区间{slot_id:03d}"),
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "mode": "dedicated_fallback",
            "folder": folder,
            "external_dir": "",
            "reference_image": os.path.join(folder, "原片参考.jpg"),
            "enabled": True,
        }
        os.makedirs(self.slot_dir(project, slot), exist_ok=True)
        slots.append(slot)
        slots.sort(key=lambda item: float(item.get("start") or 0.0))
        project["slots"] = slots
        project["suppressed_boundaries"] = [
            value
            for value in project.get("suppressed_boundaries", [])
            if isinstance(value, (int, float))
            and abs(float(value) - start) > 0.002
            and abs(float(value) - end) > 0.002
        ]
        project["segment_mappings"] = self.timeline_segments(project)
        return self.save(project), slot

    def update_slot(self, project, slot_id, start, end, label=None, mode=None, external_dir=None):
        project = dict(project or {})
        slots = [dict(item) for item in project.get("slots", []) if isinstance(item, dict)]
        target = next((item for item in slots if int(item.get("id") or 0) == int(slot_id)), None)
        if target is None:
            raise RuntimeError("没有找到需要更新的替换槽位")
        start = max(0.0, float(start))
        end = max(start, float(end))
        if end - start < 0.10:
            raise ValueError("切分区间至少需要0.10秒")
        duration = float((project.get("source_metadata") or {}).get("duration") or 0.0)
        if duration > 0:
            end = min(duration, end)
        for slot in slots:
            if slot is target:
                continue
            if start < float(slot.get("end") or 0.0) - 0.001 and end > float(
                slot.get("start") or 0.0
            ) + 0.001:
                raise ValueError(
                    f"切分区间与标记{int(slot.get('id') or 0):03d}重叠，请调整入点或出点"
                )
        target["start"] = round(start, 3)
        target["end"] = round(end, 3)
        target["duration"] = round(end - start, 3)
        if label is not None:
            target["label"] = str(label or target.get("label") or "")
        if mode is not None:
            target["mode"] = str(mode)
        if external_dir is not None:
            target["external_dir"] = os.path.abspath(external_dir) if external_dir else ""
        slots.sort(key=lambda item: float(item.get("start") or 0.0))
        project["slots"] = slots
        project["suppressed_boundaries"] = [
            value
            for value in project.get("suppressed_boundaries", [])
            if isinstance(value, (int, float))
            and abs(float(value) - start) > 0.002
            and abs(float(value) - end) > 0.002
        ]
        project["segment_mappings"] = self.timeline_segments(project)
        return self.save(project), target

    def archive_slot(self, project, slot_id):
        project = dict(project or {})
        slots = [dict(item) for item in project.get("slots", []) if isinstance(item, dict)]
        target = next((item for item in slots if int(item.get("id") or 0) == int(slot_id)), None)
        if target is None:
            return self.save(project)
        source_dir = self.slot_dir(project, target)
        if os.path.isdir(source_dir):
            archive_root = os.path.join(self.project_dir(project), "已删除槽位")
            os.makedirs(archive_root, exist_ok=True)
            archive_name = (
                f"槽位{int(target.get('id') or 0):03d}_"
                f"{time.strftime('%Y%m%d_%H%M%S')}"
            )
            archive_path = os.path.join(archive_root, archive_name)
            shutil.move(source_dir, archive_path)
        project["slots"] = [
            item for item in slots if int(item.get("id") or 0) != int(slot_id)
        ]
        project["segment_mappings"] = self.timeline_segments(project)
        return self.save(project)

    def slot_materials(self, project, slot):
        mode = str((slot or {}).get("mode") or "dedicated_fallback")
        dedicated = scan_video_files(self.slot_dir(project, slot))
        external = scan_video_files(str((slot or {}).get("external_dir") or ""))
        shared = scan_video_files(self.shared_dir(project))
        dedicated = sorted(set(dedicated + external), key=lambda item: os.path.normcase(item))
        if mode == "shared":
            return shared
        if mode == "dedicated":
            return dedicated
        return dedicated if dedicated else shared

    def validate(self, project):
        issues = []
        source_path = str((project or {}).get("source_path") or "")
        if not os.path.isfile(source_path):
            issues.append("竞品原片不存在")
        slots = [
            item
            for item in (project or {}).get("slots", [])
            if isinstance(item, dict) and item.get("enabled", True)
        ]
        cut_points = [
            value
            for value in (project or {}).get("cut_points", [])
            if isinstance(value, (int, float))
        ]
        if not slots and not cut_points:
            issues.append("尚未添加切分区间")
        segments = self.timeline_segments(project)
        replacements = [
            item for item in segments if str(item.get("folder") or "").strip()
        ]
        if not replacements:
            issues.append("尚未给任何片段填写替换文件夹路径")
        settings = dict((project or {}).get("settings") or {})
        if bool(settings.get("intro_enabled", False)):
            intro_dir = self.intro_dir(project)
            if not os.path.isdir(intro_dir):
                issues.append("片头素材文件夹不存在")
            elif not self.intro_materials(project):
                issues.append("片头素材文件夹中没有可用视频")
        if str(settings.get("audio_mode") or "source") == "folder":
            audio_dir = self.audio_dir(project)
            if not os.path.isdir(audio_dir):
                issues.append("替换音频文件夹不存在")
            elif not self.audio_materials(project):
                issues.append("替换音频文件夹中没有可用音频")
        status = []
        for segment in segments:
            folder = str(segment.get("folder") or "").strip()
            materials = self.segment_materials(segment) if folder else []
            ready = not folder or bool(materials)
            if folder and not materials:
                issues.append(
                    f"片段{int(segment.get('id') or 0):03d}的路径中没有可用视频"
                )
            status.append(
                {
                    "segment_id": int(segment.get("id") or 0),
                    "start": float(segment.get("start") or 0.0),
                    "end": float(segment.get("end") or 0.0),
                    "folder": folder,
                    "material_count": len(materials),
                    "ready": ready,
                }
            )
        return issues, status
