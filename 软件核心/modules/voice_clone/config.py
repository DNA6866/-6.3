import json
import os
import sys
from paths import APP_ROOT as PATH_APP_ROOT, config_dir, logs_dir, models_dir, workspace_path


def app_root():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return PATH_APP_ROOT


APP_ROOT = app_root()
CONFIG_DIR = config_dir()
OUTPUT_DIR = workspace_path('输出', 'voice_clone')
MODEL_DIR = os.path.join(models_dir(), 'VoxCPM2')
ASR_MODEL_DIR = os.path.join(models_dir(), 'SenseVoiceSmall')
LOG_DIR = logs_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, 'voice_clone_config.json')
ENV_REPORT_FILE = os.path.join(LOG_DIR, 'voice_clone_env_report.txt')


DEFAULT_CONFIG = {
    "model_path": MODEL_DIR,
    "asr_model_path": ASR_MODEL_DIR,
    "output_dir": OUTPUT_DIR,
    "last_reference_audio": "",
    "default_cfg_value": 2.0,
    "default_inference_timesteps": 10
}


def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(MODEL_DIR, exist_ok=True)
    os.makedirs(ASR_MODEL_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def load_config():
    ensure_dirs()
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG.copy())
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(data)
    if not os.path.exists(cfg.get("model_path", "")):
        cfg["model_path"] = MODEL_DIR
    if not os.path.exists(cfg.get("asr_model_path", "")):
        cfg["asr_model_path"] = ASR_MODEL_DIR
    for key in ("model_path", "asr_model_path", "output_dir"):
        value = cfg.get(key, "")
        if value and not os.path.isabs(value):
            cfg[key] = os.path.abspath(os.path.join(APP_ROOT, value))
    return cfg


def save_config(config):
    ensure_dirs()
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
