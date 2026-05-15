import json
import os
import re
import subprocess

from .comfy_manager import find_python_executable, hidden_startupinfo


def _parse_version(value):
    nums = re.findall(r"\d+", str(value or ""))
    return tuple(int(x) for x in nums[:3]) if nums else (0,)


def _query_nvidia():
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=8,
            startupinfo=hidden_startupinfo(),
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
            return {
                "ok": True,
                "name": parts[0] if parts else "",
                "driver": parts[1] if len(parts) > 1 else "",
                "vram_mb": int(float(parts[2])) if len(parts) > 2 and parts[2] else 0,
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": "未检测到 NVIDIA 显卡"}


def _detect_series(gpu_name):
    match = re.search(r"RTX\s*([2453]0)\d{2}", gpu_name or "", re.IGNORECASE)
    if match:
        return f"{match.group(1)}系"
    match = re.search(r"RTX\s*(\d{4})", gpu_name or "", re.IGNORECASE)
    if match:
        return f"{match.group(1)[0]}0系"
    return "未知"


def _query_engine_torch(comfyui_path):
    python_exe = find_python_executable(comfyui_path)
    code = (
        "import json\n"
        "data={'torch_available': False}\n"
        "try:\n"
        " import torch\n"
        " data.update({'torch_available': True, 'torch_version': torch.__version__, "
        "'cuda_version': torch.version.cuda, 'cuda_available': bool(torch.cuda.is_available())})\n"
        " if torch.cuda.is_available():\n"
        "  data['capability']='.'.join(map(str, torch.cuda.get_device_capability(0)))\n"
        "except Exception as e:\n"
        " data['error']=str(e)\n"
        "print(json.dumps(data, ensure_ascii=False))\n"
    )
    try:
        res = subprocess.run(
            [python_exe, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=20,
            startupinfo=hidden_startupinfo(),
        )
        for line in reversed((res.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                data = json.loads(line)
                data["python_exe"] = python_exe
                return data
        return {"torch_available": False, "python_exe": python_exe, "error": res.stdout.strip()[-300:]}
    except Exception as exc:
        return {"torch_available": False, "python_exe": python_exe, "error": str(exc)}


def detect_hardware_profile(comfyui_path):
    gpu = _query_nvidia()
    torch_info = _query_engine_torch(comfyui_path)
    series = _detect_series(gpu.get("name", ""))
    vram_mb = gpu.get("vram_mb", 0)
    driver_version = _parse_version(gpu.get("driver", ""))
    torch_version = _parse_version(torch_info.get("torch_version", ""))
    cuda_version = _parse_version(torch_info.get("cuda_version", ""))
    warnings = []
    failures = []

    if not gpu.get("ok"):
        failures.append("未检测到 NVIDIA 显卡或 nvidia-smi 不可用。")
    if not torch_info.get("torch_available"):
        failures.append("自带 AI 引擎无法导入 PyTorch。")
    if torch_info.get("torch_available") and not torch_info.get("cuda_available"):
        failures.append("自带 AI 引擎的 PyTorch 无法调用 GPU。")

    if series == "50系":
        if driver_version < (570,):
            failures.append("RTX 50 系需要较新的 NVIDIA 驱动，建议安装 R570 或更高版本。")
        if torch_version < (2, 7) or cuda_version < (12, 8):
            failures.append("RTX 50 系需要 PyTorch 2.7+ 与 CUDA 12.8+ 的 AI 引擎。")

    if vram_mb >= 22000:
        level = "高性能档"
        width, height, steps, count, args = 1080, 1920, 6, 1, []
        suggestion = "显存充足，可使用高分辨率商品图参数。"
    elif vram_mb >= 11000:
        level = "标准档"
        width, height, steps, count, args = 900, 1600, 4, 1, []
        suggestion = "适合默认商品图生成参数。"
    elif vram_mb >= 8000:
        level = "低显存档"
        width, height, steps, count, args = 720, 1280, 4, 1, ["--lowvram"]
        suggestion = "显存偏紧，已建议降低分辨率并启用低显存模式。"
    else:
        level = "兼容尝试档"
        width, height, steps, count, args = 640, 1136, 3, 1, ["--lowvram"]
        warnings.append("显存低于 8GB，Z-Image 可能无法稳定运行。")
        suggestion = "建议使用 8GB 以上 NVIDIA 显卡，当前仅适合低分辨率尝试。"

    status = "fail" if failures else ("warn" if warnings else "pass")
    return {
        "status": status,
        "gpu": gpu,
        "torch": torch_info,
        "series": series,
        "level": level,
        "width": width,
        "height": height,
        "steps": steps,
        "image_count": count,
        "comfyui_args": args,
        "suggestion": suggestion,
        "warnings": warnings,
        "failures": failures,
    }


def profile_summary(profile):
    gpu = profile.get("gpu", {})
    torch_info = profile.get("torch", {})
    vram_gb = round(gpu.get("vram_mb", 0) / 1024, 1)
    return (
        f"{gpu.get('name', '未检测到显卡')} / {vram_gb}GB / "
        f"驱动 {gpu.get('driver', '未知')} / "
        f"PyTorch {torch_info.get('torch_version', '未知')}+CUDA {torch_info.get('cuda_version', '未知')} / "
        f"{profile.get('series', '未知')} {profile.get('level', '')}"
    )
