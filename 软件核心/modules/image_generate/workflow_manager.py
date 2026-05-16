import copy
import json
import os
import shutil
from pathlib import Path

from .config import WORKFLOW_DIR


ZIMAGE_NAMES = ("z-image", "z_image", "zimage")


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


def build_default_zimage_api_workflow(ui_workflow, params):
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
                # 不使用 ComfyUI latent batch 批量生成，多张改由后台 worker 单张循环提交。
                inputs["batch_size"] = 1
        if class_type == "SaveImage":
            inputs["filename_prefix"] = "niuyeye_zimage"
    return wf


def prepare_workflow(workflow_path, params):
    workflow = load_workflow(workflow_path)
    if is_api_workflow(workflow):
        return patch_api_workflow(workflow, params)
    return build_default_zimage_api_workflow(workflow, params)


def check_required_models(comfyui_path, workflow_path=""):
    base = Path(comfyui_path) / "models"
    required = [
        base / "diffusion_models" / "z_image_turbo_bf16.safetensors",
        base / "text_encoders" / "qwen_3_4b.safetensors",
        base / "vae" / "ae.safetensors",
    ]
    found = [str(path) for path in required if path.exists()]
    missing = [str(path) for path in required if not path.exists()]
    return not missing, found, missing
