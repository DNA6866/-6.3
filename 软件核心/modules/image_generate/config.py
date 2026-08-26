import json
import os
import sys
from paths import APP_ROOT as PATH_APP_ROOT, config_dir, logs_dir, runtime_path, workspace_path


def app_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return PATH_APP_ROOT


APP_ROOT = app_root()
CONFIG_DIR = config_dir()
OUTPUT_DIR = workspace_path("输出", "image_generate")
WORKFLOW_DIR = runtime_path("workflows", "zimage")
LOG_DIR = logs_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, "image_generate_config.json")
ENV_REPORT_FILE = os.path.join(LOG_DIR, "image_generate_env_report.txt")
STARTUP_LOG_FILE = os.path.join(LOG_DIR, "comfyui_startup.log")
DIAGNOSTIC_FILE = os.path.join(LOG_DIR, "image_generate_diagnostic.txt")
BUNDLED_COMFYUI_PATH = runtime_path("engines", "ComfyUI", "ComfyUI")
DEV_COMFYUI_PATH = "D:/ComfyUI/ComfyUI"


def get_preferred_comfyui_path():
    """优先使用随软件分发的 ComfyUI，引擎缺失时再回退到开发机路径。"""
    if os.path.exists(os.path.join(BUNDLED_COMFYUI_PATH, "main.py")):
        return BUNDLED_COMFYUI_PATH
    return DEV_COMFYUI_PATH

DEFAULT_CONFIG = {
    "comfyui_path": get_preferred_comfyui_path(),
    "workflow_path": "",
    "output_dir": OUTPUT_DIR,
    "host": "127.0.0.1",
    "port": 8199,
    "startup_timeout": 180,
    "auto_start_comfyui": True,
    "auto_hardware_profile": True,
    "comfyui_args": [],
}


def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(WORKFLOW_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def load_config():
    ensure_dirs()
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG.copy())
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(data)
    current_path = cfg.get("comfyui_path", "")
    bundled_ready = os.path.exists(os.path.join(BUNDLED_COMFYUI_PATH, "main.py"))
    current_norm = os.path.normcase(os.path.normpath(current_path)) if current_path else ""
    dev_norm = os.path.normcase(os.path.normpath(DEV_COMFYUI_PATH))
    current_exists = os.path.exists(os.path.join(current_path, "main.py")) if current_path else False
    if bundled_ready and (not current_path or current_norm == dev_norm or not current_exists):
        cfg["comfyui_path"] = BUNDLED_COMFYUI_PATH
    if cfg.get("workflow_path") and not os.path.exists(cfg["workflow_path"]):
        cfg["workflow_path"] = ""
    for key in ("output_dir", "workflow_path", "comfyui_path"):
        value = cfg.get(key, "")
        if value and not os.path.isabs(value):
            cfg[key] = os.path.abspath(os.path.join(APP_ROOT, value))
    return cfg


def save_config(config):
    ensure_dirs()
    portable_config = config.copy()
    for key in ("output_dir", "workflow_path", "comfyui_path"):
        value = portable_config.get(key, "")
        if value and os.path.isabs(value):
            try:
                common = os.path.commonpath([APP_ROOT, os.path.abspath(value)])
                if os.path.normcase(common) == os.path.normcase(APP_ROOT):
                    portable_config[key] = os.path.relpath(value, APP_ROOT).replace("\\", "/")
            except ValueError:
                pass
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(portable_config, f, indent=4, ensure_ascii=False)
