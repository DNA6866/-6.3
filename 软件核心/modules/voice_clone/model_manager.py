import os

from .config import MODEL_DIR, load_config, save_config


def get_default_model_path():
    return MODEL_DIR


def find_model_files(model_path):
    result = {
        "config_files": [],
        "weight_files": [],
        "other_files": [],
        "total_files": 0
    }
    if not model_path or not os.path.isdir(model_path):
        return result

    for root, _, files in os.walk(model_path):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, model_path)
            result["total_files"] += 1
            lower = name.lower()
            if lower in ["config.json", "tokenizer_config.json"] or "config" in lower:
                result["config_files"].append(rel)
            elif lower.endswith(('.safetensors', '.bin', '.pt', '.pth')):
                result["weight_files"].append(rel)
            else:
                result["other_files"].append(rel)
    return result


def check_model_path(model_path):
    if not model_path:
        return False, "未设置模型路径。", find_model_files(model_path)
    if not os.path.isdir(model_path):
        return False, "模型目录不存在。", find_model_files(model_path)

    files = find_model_files(model_path)
    if files["total_files"] < 3:
        return False, "模型目录文件数量过少，请确认选择的是 VoxCPM2 完整模型目录。", files
    if not files["config_files"]:
        return False, "模型目录中未找到 config/tokenizer/config 类文件。", files
    if not files["weight_files"]:
        return False, "模型目录中未找到 .safetensors/.bin/.pt 等权重文件。", files
    return True, "模型目录看起来有效。", files


__all__ = [
    "get_default_model_path",
    "load_config",
    "save_config",
    "check_model_path",
    "find_model_files",
]
