import copy
from datetime import datetime
import os
import subprocess
import time

from paths import tools_dir
from services.batch_project_service import safe_output_component
from services.media_index_service import get_media_index
from services.media_service import hidden_startupinfo


ASSET_FIELDS = {
    "音频素材": "audios", "第一轨道": "first_track_videos",
    "视频素材": "videos", "钩子素材": "hook_videos",
    "第二段指定素材": "second_segment_videos", "AI开场视频": "ai_intro_videos",
    "封面图片": "covers", "背景音乐": "bgms",
}
VIDEO_MODES = {"first_track", "ai_intro_first_track"}
AI_MODES = {"ai_intro_first_track", "ai_intro_audio"}
MODE_TITLES = {"traditional": "传统音频混剪", "ordinary": "普通视频拼接",
               "first_track": "数字人口播第一轨道", "ai_intro_first_track": "AI完整开场",
               "ai_intro_audio": "AI开场+音频拼接"}


def allocate_targets(total, project_ids):
    ids = list(dict.fromkeys(project_ids))
    if not ids:
        raise ValueError("请勾选至少一个参与分配的模板")
    if int(total) < len(ids):
        raise ValueError("店铺目标不能小于该店铺所选模板数量")
    base, extra = divmod(int(total), len(ids))
    return {pid: base + (index < extra) for index, pid in enumerate(ids)}


def allocate_shop_targets(per_shop_target, project_ids, projects):
    """按已保存模板的店铺名称分组，每个店铺独立均分目标。"""
    by_id = {project.get("id"): project for project in projects}
    groups = {}
    if not project_ids:
        raise ValueError("请勾选至少一个参与分配的模板")
    for pid in dict.fromkeys(project_ids):
        project = by_id.get(pid)
        if project is None:
            raise ValueError("计划中的模板已被删除，请重新选择模板")
        shop = str(project.get("shop_name") or "").strip()
        if not shop:
            raise ValueError(f"模板“{project.get('product_name', pid)}”未填写店铺名称，请先保存店铺名称")
        groups.setdefault(shop, []).append(pid)
    return [{"shop_name": shop, "target": int(per_shop_target),
             "targets": allocate_targets(per_shop_target, ids)} for shop, ids in groups.items()]


def nearest_rounds(target, count):
    if count <= 0:
        raise ValueError("目录中没有可用的驱动素材")
    lower = max(1, int(target) // count)
    rounds = min((lower, lower + 1), key=lambda n: (abs(n * count - target), n))
    return rounds, rounds * count


def plan_due(plan, runs, now=None):
    now = now or datetime.now()
    day = now.date().isoformat()
    return bool(plan.get("enabled") and now.strftime("%H:%M") >= plan.get("time", "09:00")
                and f"{plan.get('id')}:{day}" not in runs)


def plans_in_rotation(plans, runs):
    # 优先执行从未生产或最久未生产的计划，避免耗时任务跨天后一直排在最前。
    def last_day(plan):
        prefix = f"{plan.get('id')}:"
        return max((run.get("day", "") for key, run in runs.items() if key.startswith(prefix)), default="")

    return sorted(plans, key=last_day)


def scan_materials(root, extensions, cancelled=lambda: False, stable_seconds=10):
    """后台递归读取，包括深层 NAS 目录；不可访问时抛错而非伪装成空目录。"""
    if not root:
        return []
    root = os.path.abspath(root)
    extensions = tuple(ext.lower() for ext in extensions)
    found = []

    def raise_error(exc):
        raise exc

    if not os.path.isdir(root):
        raise OSError(f"素材目录不可访问：{root}")
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=raise_error):
        if cancelled():
            raise InterruptedError("素材读取已取消")
        dirs.sort(key=str.casefold)
        for name in files:
            if extensions and not name.lower().endswith(extensions):
                continue
            path = os.path.join(directory, name)
            stat = os.stat(path)
            if stat.st_size <= 0 or time.time() - stat.st_mtime < stable_seconds:
                continue
            found.append((os.path.relpath(path, root), path))
    found.sort(key=lambda item: item[0].casefold())
    get_media_index().store_directory(root, extensions, found)
    return found


def probe_driver(path):
    import json

    index = get_media_index()
    cached = index.metadata(path)
    if float(cached.get("duration") or 0) > 0:
        return float(cached.get("duration"))
    try:
        result = subprocess.run(
            [os.path.join(tools_dir(), "ffprobe.exe"), "-v", "error",
             "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            startupinfo=hidden_startupinfo(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=12,
        )
        if result.returncode:
            raise ValueError((result.stderr or "无法解码")[-300:])
        duration = float(json.loads(result.stdout).get("format", {}).get("duration", 0))
        if duration <= 0:
            raise ValueError("无有效时长")
        index.update_metadata(path, duration=duration)
        return duration
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        raise ValueError(f"驱动素材无法读取：{path}；{exc}") from exc


def hook_usage(runs, day):
    """共用实际目录的模板共享使用记录，兼容旧版按计划和模板保存的记录。"""
    history, today_counts = {}, {}
    for run in runs.values():
        used_day = run.get("day", "")
        if not used_day or used_day > day:
            continue
        for folder in run.get("hooks", {}).values():
            if not folder:
                continue
            normalized = os.path.normcase(os.path.abspath(folder))
            history[normalized] = max(history.get(normalized, ""), used_day)
            if used_day == day:
                today_counts[normalized] = today_counts.get(normalized, 0) + 1
    return history, today_counts


def choose_hook(root, history, scanner, today_counts=None):
    if not os.path.isdir(root):
        raise OSError(f"钩子总目录不可访问：{root}")
    with os.scandir(root) as entries:
        folders = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    today_counts = today_counts or {}
    # 先错开当天已分配的目录；不足时均衡复用，再优先未用或最久未用的批次。
    def priority(folder):
        normalized = os.path.normcase(os.path.abspath(folder))
        return today_counts.get(normalized, 0), history.get(normalized, ""), folder.casefold()

    folders.sort(key=priority)
    for folder in folders:
        files = scanner(folder)
        if files:
            return folder, files
    raise ValueError(f"钩子总目录中没有包含有效视频的子文件夹：{root}")


def build_daily_payloads(plan, projects, runs, day, extensions, cache_dir,
                         cancelled=lambda: False, probe=probe_driver):
    from PyQt5.QtGui import QColor

    # 沿用旧计划的数值字段，统一解释为每店目标；已入队快照不在此处修改。
    shop_groups = allocate_shop_targets(plan.get("total", 800), plan.get("project_ids", []), projects)
    targets = {pid: target for group in shop_groups for pid, target in group.get("targets", {}).items()}
    by_id = {p.get("id"): p for p in projects}
    scanned = {}

    def scan(root, tab):
        key = (os.path.normcase(os.path.abspath(root)), tuple(extensions.get(tab, [])))
        if key not in scanned:
            scanned[key] = scan_materials(root, extensions.get(tab, []), cancelled)
        return scanned[key]

    payloads, hook_choices = [], {}
    hook_history, today_hook_counts = hook_usage(runs, day)
    for pid, target in targets.items():
        if cancelled():
            raise InterruptedError("每日任务准备已取消")
        if pid not in by_id:
            raise ValueError("计划中的模板已被删除，请重新选择模板")
        project = copy.deepcopy(by_id.get(pid))
        directories = project.get("directories", {})
        rules = copy.deepcopy(project.get("rules", {}))
        mode = rules.get("mixer_mode", "traditional")
        if mode not in VIDEO_MODES | AI_MODES | {"traditional", "ordinary"}:
            raise ValueError(f"不支持的混剪模式：{mode}")
        is_video = mode in VIDEO_MODES
        is_ai = mode in AI_MODES
        driver_tab = "第一轨道" if is_video else "音频素材"
        required = {driver_tab, "视频素材"} | ({"AI开场视频"} if is_ai else set())
        data = rules
        data.update({field: [] for field in ASSET_FIELDS.values()})
        hook_root = str(plan.get("hook_roots", {}).get(pid) or "").strip()
        for tab, field in ASSET_FIELDS.items():
            if ((tab == "第一轨道" and not is_video) or (tab == "音频素材" and is_video)
                    or (tab == "AI开场视频" and not is_ai)
                    or (tab == "钩子素材" and (is_ai or hook_root))
                    or (tab == "第二段指定素材" and is_video)):
                continue
            root = str(directories.get(tab) or "").strip()
            files = scan(root, tab) if root else []
            if tab in required and not files:
                raise ValueError(f"{project.get('product_name', pid)}：{tab}没有可用素材")
            data[field] = [{"path": path, "row": -1} for _, path in files]
        if hook_root and not is_ai:
            folder, files = choose_hook(
                hook_root, hook_history, lambda p: scan(p, "钩子素材"), today_hook_counts,
            )
            normalized = os.path.normcase(os.path.abspath(folder))
            today_hook_counts[normalized] = today_hook_counts.get(normalized, 0) + 1
            hook_history[normalized] = day
            hook_choices[pid] = folder
            directories["钩子素材"] = folder
            data["hook_videos"] = [{"path": p, "row": -1} for _, p in files]
        drivers, skipped = [], []
        for item in data.get(ASSET_FIELDS.get(driver_tab), []):
            if cancelled():
                raise InterruptedError("每日任务准备已取消")
            try:
                duration = probe(item.get("path", ""))
                if duration <= 0:
                    raise ValueError("无有效时长")
                drivers.append(item)
            except ValueError as exc:
                skipped.append(str(exc))
        if not drivers:
            raise ValueError(f"{driver_tab}无有效素材；" + "；".join(skipped[:3]))
        data[ASSET_FIELDS.get(driver_tab)] = drivers
        rounds, expected = nearest_rounds(target, len(drivers))
        if is_video:
            data["audios"] = [dict(item, first_track_path=item.get("path"), track_index=i)
                              for i, item in enumerate(drivers)]
        output_base = str(directories.get("保存视频") or "").strip()
        if not output_base:
            raise ValueError("模板未配置保存视频目录")
        shop = safe_output_component(project.get("shop_name"), "默认店铺")
        name = safe_output_component(project.get("product_name"), "模板")
        # 模板 ID 与计划 ID 避免同店同名模板或多个计划写入同一路径。
        output = os.path.join(output_base, shop, day, f"{name}_{pid[:6]}_{plan.get('id', '')[:6]}")
        subtitle = copy.deepcopy(project.get("subtitle", {}))
        for field in ("color", "border_color"):
            color = str(subtitle.get(field) or ("white" if field == "color" else "black"))
            if "随机" not in color:
                parsed = QColor(color)
                if not parsed.isValid():
                    raise ValueError(f"模板字幕颜色无效：{color}")
                color = parsed.name().upper()
            subtitle[field] = color
        data.update({
            "loop_count": rounds, "use_first_track_audio": is_video, "ai_intro_mode": is_ai,
            "strong_structure": bool(data.get("strong_structure") and is_video),
            "gpu_mode": data.pop("gpu", "CPU 软解"),
            "auto_performance": data.pop("auto_perf", True),
            "watermarks": copy.deepcopy(project.get("watermarks", [])),
            "sub_config": subtitle, "cover_config": copy.deepcopy(project.get("cover", {})),
            "cache_dir": cache_dir, "output_dir": output, "duration_cache": {},
            "asset_scope": directories.get("视频素材", ""), "enable_standard_video_cache": True,
            "preview_only": False, "fair_quota": 0,
        })
        project.setdefault("rules", {})["loop_count"] = rounds
        project["asset_selections"] = {
            tab: {"select_all": True, "selected_paths": [item.get("path", "") for item in data.get(field, [])]}
            for tab, field in ASSET_FIELDS.items()
        }
        task_name = f"{shop} {day} {name} {pid[:6]} {plan.get('id', '')[:6]}"
        payloads.append({
            "project_id": pid, "project_snapshot": project, "directories": directories,
            "shop_name": project.get("shop_name", ""), "product_name": project.get("product_name", ""),
            "task_name": task_name, "mode_label": MODE_TITLES.get(mode, mode), "expected": expected,
            "output_dir": output, "task_data": data, "daily_plan_id": plan.get("id"),
            "production_day": day, "target": target,
            "shop_target": int(plan.get("total", 800)), "quantity_scope": "per_shop",
            "skipped_drivers": skipped,
            "asset_summary": f"驱动素材 {len(drivers)} / {rounds} 轮 / 本店目标 {plan.get('total', 800)} / 本模板目标 {target}",
            "message": f"每日任务：目标 {target}，预计 {expected} 条",
        })
    return payloads, hook_choices
