import copy
import json
import os
import re
import shutil
from pathlib import Path

from .config import WORKFLOW_DIR


ZIMAGE_NAMES = ("z-image", "z_image", "zimage")

# ───────── Z-Image 模型自动检测与版本管理 ─────────

def _scan_safetensors(directory, pattern):
    """扫描目录中匹配模式的所有 .safetensors 文件，按版本排序。"""
    results = []
    try:
        for entry in os.listdir(str(directory)):
            if not entry.lower().endswith(".safetensors"):
                continue
            if pattern and not re.search(pattern, entry, re.IGNORECASE):
                continue
            full = os.path.join(str(directory), entry)
            stat = os.stat(full)
            results.append((full, entry, stat.st_mtime, stat.st_size))
    except OSError:
        return []
    # 按修改时间降序（最新的在前），同时间按大小降序
    results.sort(key=lambda x: (-x[2], -x[3]))
    return results


def _parse_model_version(filename):
    """从文件名提取版本信息用于比较优先级。"""
    lower = filename.lower()
    # 优先级：turbo_v2 > turbo > bf16 > fp8 > fp16 > base
    if "turbo" in lower and ("v2" in lower or "v2" in lower.replace("turbo","")):
        return (0, "turbo_v2")
    if "turbo" in lower:
        return (1, "turbo")
    if "bf16" in lower:
        return (2, "bf16")
    if "fp8" in lower:
        return (3, "fp8")
    if "fp16" in lower:
        return (4, "fp16")
    return (5, "base")


def detect_zimage_models(comfyui_path):
    """扫描 ComfyUI models 目录，自动发现 Z-Image 相关模型。
    返回: (unet_files, clip_files, vae_files) — 每个为 [(path, filename, mtime, size), ...]
    """
    base = Path(comfyui_path) / "models"
    unet_dir = base / "diffusion_models"
    clip_dir = base / "text_encoders"
    vae_dir = base / "vae"

    unet_files = _scan_safetensors(unet_dir, r"z.?image")
    clip_files = _scan_safetensors(clip_dir, r"qwen|clip")
    vae_files = _scan_safetensors(vae_dir, r"ae|vae|auto.?encoder")

    return unet_files, clip_files, vae_files


def select_best_unet(unet_files, fallback="z_image_turbo_bf16.safetensors"):
    """从候选 UNET 模型中选择最佳版本。"""
    if not unet_files:
        return fallback, False
    # 按版本优先级排序
    ordered = sorted(unet_files, key=lambda x: _parse_model_version(x[1]))
    return ordered[0][1], True


def select_best_clip(clip_files, fallback="qwen_3_4b.safetensors"):
    if not clip_files:
        return fallback
    return clip_files[0][1]


def select_best_vae(vae_files, fallback="ae.safetensors"):
    if not vae_files:
        return fallback
    return vae_files[0][1]


def get_model_status(comfyui_path):
    """获取模型状态报告：当前使用/可用升级。"""
    unet_files, clip_files, vae_files = detect_zimage_models(comfyui_path)
    current_unet, has_better = select_best_unet(unet_files)

    status = {
        "current_unet": current_unet,
        "unet_count": len(unet_files),
        "all_unet": [f[1] for f in unet_files],
        "has_older_versions": len(unet_files) > 1,
        "clip_name": select_best_clip(clip_files),
        "vae_name": select_best_vae(vae_files),
    }
    return status


def cleanup_old_models(comfyui_path, keep_latest=True):
    """删除旧版 Z-Image 模型文件，仅保留最新版本。
    返回: (deleted_count, deleted_names, kept_names)
    """
    unet_files, _, _ = detect_zimage_models(comfyui_path)
    if len(unet_files) <= 1:
        return 0, [], [f[1] for f in unet_files]
    deleted = []
    kept = []
    # 保留第一个（最佳版本），删除其余
    for i, (path, name, _, _) in enumerate(unet_files):
        if i == 0 and keep_latest:
            kept.append(name)
            continue
        try:
            os.remove(path)
            deleted.append(name)
        except OSError:
            kept.append(name)
    return len(deleted), deleted, kept


# ───────── 现有工作流管理 ─────────

def scan_zimage_workflows(comfyui_path, local_dir=WORKFLOW_DIR):
    candidates = []
    search_dirs = [
        Path(local_dir),
        Path(comfyui_path) / "user" / "default" / "workflows",
        Path(comfyui_path) / "workflows",
        Path(comfyui_path) / "blueprints",
    ]
    seen = set()
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*.json"):
            lower = path.name.lower()
            if any(key in lower for key in ZIMAGE_NAMES):
                resolved = str(path.resolve())
                if resolved not in seen:
                    candidates.append(resolved)
                    seen.add(resolved)
    return candidates


def copy_workflow_to_local(path, local_dir=WORKFLOW_DIR):
    os.makedirs(local_dir, exist_ok=True)
    target = os.path.join(local_dir, os.path.basename(path))
    source_abs = os.path.abspath(path)
    target_abs = os.path.abspath(target)
    try:
        same_file = os.path.exists(source_abs) and os.path.exists(target_abs) and os.path.samefile(source_abs, target_abs)
    except OSError:
        same_file = os.path.normcase(source_abs) == os.path.normcase(target_abs)
    if not same_file:
        shutil.copy2(path, target)
    return target


def load_workflow(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_model_name(ui_workflow, node_type, fallback):
    text = json.dumps(ui_workflow, ensure_ascii=False)
    if fallback in text:
        return fallback
    try:
        subgraphs = ui_workflow.get("definitions", {}).get("subgraphs", [])
        for sub in subgraphs:
            for node in sub.get("nodes", []):
                if node.get("type") == node_type and node.get("widgets_values"):
                    return node["widgets_values"][0]
    except Exception:
        pass
    return fallback


def build_default_zimage_api_workflow(ui_workflow, params, comfyui_path=None):
    # 自动检测最佳模型
    if comfyui_path:
        unet_files, clip_files, vae_files = detect_zimage_models(comfyui_path)
        unet_name, _ = select_best_unet(unet_files, "z_image_turbo_bf16.safetensors")
        clip_name = select_best_clip(clip_files, "qwen_3_4b.safetensors")
        vae_name = select_best_vae(vae_files, "ae.safetensors")
    else:
        unet_name = _extract_model_name(ui_workflow, "UNETLoader", "z_image_turbo_bf16.safetensors")
        clip_name = _extract_model_name(ui_workflow, "CLIPLoader", "qwen_3_4b.safetensors")
        vae_name = _extract_model_name(ui_workflow, "VAELoader", "ae.safetensors")

    prompt = params.get("prompt", "")
    width = int(params.get("width", 900))
    height = int(params.get("height", 1600))
    steps = int(params.get("steps", 4))
    cfg = float(params.get("cfg", 1.0))
    seed = int(params.get("seed", 0))
    # 低显存显卡上 Z-Image 批量 latent 容易出现糊图/异常图。
    # 多张生成由 ImageGenerateWorker 单张循环提交，这里固定 batch_size=1。
    image_count = 1

    return {
        "28": {"class_type": "UNETLoader", "inputs": {"unet_name": unet_name, "weight_dtype": "default"}},
        "30": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip_name, "type": "lumina2", "device": "default"}},
        "29": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "27": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["30", 0], "text": prompt}},
        "31": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["27", 0]}},
        "13": {"class_type": "EmptySD3LatentImage", "inputs": {"width": width, "height": height, "batch_size": image_count}},
        "11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["28", 0], "shift": 3}},
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["11", 0],
                "positive": ["27", 0],
                "negative": ["31", 0],
                "latent_image": ["13", 0],
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "res_multistep",
                "scheduler": "simple",
                "denoise": 1,
            },
        },
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["29", 0]}},
        "99": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "niuyeye_zimage"}},
    }


def is_api_workflow(workflow):
    return isinstance(workflow, dict) and workflow and all(
        isinstance(v, dict) and "class_type" in v and "inputs" in v
        for v in workflow.values()
    )


def patch_api_workflow(workflow, params):
    wf = copy.deepcopy(workflow)
    text_nodes = [node for node in wf.values() if node.get("class_type", "").lower().endswith("textencode") or "TextEncode" in node.get("class_type", "")]
    if text_nodes:
        text_nodes[0].setdefault("inputs", {})["text"] = params.get("prompt", "")
    for node in text_nodes[1:]:
        node.setdefault("inputs", {})["text"] = ""

    for node in wf.values():
        class_type = node.get("class_type", "")
        inputs = node.setdefault("inputs", {})
        if "KSampler" in class_type:
            inputs["seed"] = int(params.get("seed", inputs.get("seed", 0)))
            inputs["steps"] = int(params.get("steps", inputs.get("steps", 4)))
            if "cfg" in inputs:
                inputs["cfg"] = float(params.get("cfg", inputs.get("cfg", 1.0)))
        if class_type in ("EmptyLatentImage", "EmptySD3LatentImage") or "LatentImage" in class_type:
            if "width" in inputs:
                inputs["width"] = int(params.get("width", inputs.get("width", 900)))
            if "height" in inputs:
                inputs["height"] = int(params.get("height", inputs.get("height", 1600)))
            if "batch_size" in inputs:
                inputs["batch_size"] = 1
        if class_type == "SaveImage":
            inputs["filename_prefix"] = "niuyeye_zimage"
    return wf


def prepare_workflow(workflow_path, params, comfyui_path=None):
    workflow = load_workflow(workflow_path)
    if is_api_workflow(workflow):
        return patch_api_workflow(workflow, params)
    return build_default_zimage_api_workflow(workflow, params, comfyui_path)


def check_required_models(comfyui_path, workflow_path=""):
    """检查所需模型，自动发现可用模型而非硬编码。"""
    unet_files, clip_files, vae_files = detect_zimage_models(comfyui_path)

    missing = []
    found = []

    if unet_files:
        found.append(unet_files[0][1] + " (UNET)")
    else:
        missing.append("Z-Image UNET 模型 (diffusion_models/z_image_*.safetensors)")

    if clip_files:
        found.append(clip_files[0][1] + " (CLIP)")
    else:
        missing.append("Qwen CLIP 模型 (text_encoders/qwen_*.safetensors)")

    if vae_files:
        found.append(vae_files[0][1] + " (VAE)")
    else:
        missing.append("AE/VAE 模型 (vae/ae*.safetensors)")

    return not missing, found, missing
