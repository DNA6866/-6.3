import os
import sys


RESOURCE_BUNDLE_DIRNAME = "网盘资源包"
WORKSPACE_DIRNAME = "用户工作区"
CORE_DIRNAME = "软件核心"


def app_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_ROOT = app_root()
INTERNAL_ROOT = getattr(sys, "_MEIPASS", APP_ROOT)
RESOURCE_ROOT = os.path.join(APP_ROOT, RESOURCE_BUNDLE_DIRNAME)
WORKSPACE_ROOT = os.path.join(APP_ROOT, WORKSPACE_DIRNAME)
CORE_ROOT = os.path.join(APP_ROOT, CORE_DIRNAME)
INTERNAL_CORE_ROOT = os.path.join(INTERNAL_ROOT, CORE_DIRNAME)


def ensure_core_path():
    """让内部代码目录参与 import，兼容把 modules/services/styles 移到软件核心。"""
    for path in (INTERNAL_CORE_ROOT, CORE_ROOT):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)


ensure_core_path()


def _unique_paths(paths):
    result = []
    seen = set()
    for path in paths:
        if not path:
            continue
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(os.path.abspath(path))
    return result


def resource_root_candidates():
    """列出可能的网盘资源包位置，兼容客户误套一层目录的旧安装结构。"""
    parent = os.path.dirname(APP_ROOT)
    candidates = [
        os.environ.get("NYY_RESOURCE_ROOT", ""),
        RESOURCE_ROOT,
        os.path.join(parent, RESOURCE_BUNDLE_DIRNAME),
    ]
    for base in _unique_paths((APP_ROOT,)):
        try:
            with os.scandir(base) as entries:
                nested = [
                    os.path.join(entry.path, RESOURCE_BUNDLE_DIRNAME)
                    for entry in entries
                    if entry.is_dir(follow_symlinks=False)
                    and os.path.isdir(os.path.join(entry.path, RESOURCE_BUNDLE_DIRNAME))
                ]
            candidates.extend(sorted(nested, key=os.path.normcase))
        except OSError:
            continue
    return _unique_paths(candidates)


def _runtime_candidates(*parts):
    candidates = [os.path.join(RESOURCE_ROOT, *parts), os.path.join(APP_ROOT, *parts)]
    candidates.extend(os.path.join(root, *parts) for root in resource_root_candidates())
    return _unique_paths(candidates)


def runtime_path(*parts):
    """运行资源路径：优先使用网盘资源包，兼容旧版根目录资源。"""
    candidates = _runtime_candidates(*parts)
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def workspace_path(*parts):
    return os.path.join(WORKSPACE_ROOT, *parts)


def core_path(*parts):
    internal = os.path.join(INTERNAL_CORE_ROOT, *parts)
    if os.path.exists(internal):
        return internal
    bundled = os.path.join(CORE_ROOT, *parts)
    legacy = os.path.join(APP_ROOT, *parts)
    if os.path.exists(bundled) or not os.path.exists(legacy):
        return bundled
    return legacy


def logs_dir():
    return workspace_path("日志")


def config_dir():
    return workspace_path("配置")


def tools_dir():
    candidates = _runtime_candidates("tools")
    for candidate in candidates:
        if all(os.path.isfile(os.path.join(candidate, name)) for name in ("ffmpeg.exe", "ffprobe.exe")):
            return candidate
    return runtime_path("tools")


def models_dir():
    return runtime_path("models")
