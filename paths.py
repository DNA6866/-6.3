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


def runtime_path(*parts):
    """运行资源路径：优先使用网盘资源包，兼容旧版根目录资源。"""
    bundled = os.path.join(RESOURCE_ROOT, *parts)
    legacy = os.path.join(APP_ROOT, *parts)
    if os.path.exists(bundled) or not os.path.exists(legacy):
        return bundled
    return legacy


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


def config_dir():
    return workspace_path("配置")


def logs_dir():
    return workspace_path("日志")


def tools_dir():
    return runtime_path("tools")


def models_dir():
    return runtime_path("models")


def engines_dir():
    return runtime_path("engines")


def workflows_dir():
    return runtime_path("workflows")
