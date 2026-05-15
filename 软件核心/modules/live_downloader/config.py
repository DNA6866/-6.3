import json
import os
import sys
from paths import APP_ROOT as PATH_APP_ROOT, config_dir, workspace_path


def app_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return PATH_APP_ROOT


APP_ROOT = app_root()
OUTPUT_DIR = workspace_path("输出", "live_downloader")
CONFIG_DIR = config_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, "live_downloader_config.json")

DEFAULT_CONFIG = {
    "output_dir": OUTPUT_DIR,
    "last_url": "",
    "prefer_quality": "best",
    "monitor_interval": 30,
    "rooms": [],
}


def ensure_dirs():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _abs_project_path(value):
    if value and not os.path.isabs(value):
        return os.path.abspath(os.path.join(APP_ROOT, value))
    return value


def load_config():
    ensure_dirs()
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG.copy())
        return {**DEFAULT_CONFIG, "output_dir": OUTPUT_DIR}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(data)
    cfg["output_dir"] = _abs_project_path(cfg.get("output_dir") or OUTPUT_DIR)
    return cfg


def save_config(config):
    ensure_dirs()
    data = config.copy()
    value = data.get("output_dir", "")
    if value and os.path.isabs(value):
        try:
            if os.path.normcase(os.path.commonpath([APP_ROOT, value])) == os.path.normcase(APP_ROOT):
                data["output_dir"] = os.path.relpath(value, APP_ROOT).replace("\\", "/")
        except ValueError:
            pass
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
