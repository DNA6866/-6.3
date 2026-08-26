import json
import os
import re
import subprocess

from .comfy_manager import find_python_executable, hidden_startupinfo


def _parse_version(value):
    nums = re.findall(r"\d+", str(value or ""))
    return tuple(int(x) for x in nums[:3]) if nums else (0,)

def _candidate_nvidia_smi_paths():
    candidates = ["nvidia-smi"]
    for base in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramW6432"),
        r"C:\Program Files",
    ):
        if base:
            candidates.append(os.path.join(base, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"))
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidates.append(os.path.join(system_root, "System32", "nvidia-smi.exe"))

    result = []
    seen = set()
    for path in candidates:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if path == "nvidia-smi" or os.path.exists(path):
            result.append(path)
    return result

def _run_probe(cmd, timeout=8):
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            startupinfo=hidden_startupinfo(),
        )
    except Exception as exc:
        return exc

def _query_nvidia_smi():
    for exe in _candidate_nvidia_smi_paths():
        res = _run_probe(
            [exe, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
            timeout=8,
        )
        if isinstance(res, subprocess.CompletedProcess) and res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
            return {
                "ok": True,
                "name": parts[0] if parts else "",
                "driver": parts[1] if len(parts) > 1 else "",
                "vram_mb": int(float(parts[2])) if len(parts) > 2 and parts[2] else 0,
                "source": exe,
            }
    return None

def _query_windows_nvidia_name():
    commands = [
        ["wmic", "path", "win32_VideoController", "get", "name"],
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "(Get-CimInstance Win32_VideoController | ForEach-Object {$_.Name}) -join \"`n\"",
        ],
    ]
    for cmd in commands:
        res = _run_probe(cmd, timeout=6)
        if not isinstance(res, subprocess.CompletedProcess) or not res.stdout:
            continue
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip() and line.strip().lower() != "name"]
        name = next((line for line in lines if any(k in line.lower() for k in ("nvidia", "geforce", "rtx"))), "")
        if name:
            return name
    return ""

def _query_nvidia():
    smi = _query_nvidia_smi()
    if smi:
        return smi
    name = _query_windows_nvidia_name()
    if name:
        return {"ok": True, "name": name, "driver": "", "vram_mb": 0, "source": "Windows 显卡信息"}
    return {"ok": False, "error": "未检测到 NVIDIA 显卡，或 nvidia-smi / Windows 显卡信息均不可用"}


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
        "  props=torch.cuda.get_device_properties(0)\n"
        "  data['device_name']=props.name\n"
        "  data['vram_mb']=int(props.total_memory/1024/1024)\n"
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
    if torch_info.get("cuda_available"):
        gpu["ok"] = True
        gpu["name"] = gpu.get("name") or torch_info.get("device_name", "")
        gpu["vram_mb"] = max(int(gpu.get("vram_mb") or 0), int(torch_info.get("vram_mb") or 0))
        gpu["source"] = gpu.get("source") or "PyTorch CUDA"
    series = _detect_series(gpu.get("name", ""))
    vram_mb = gpu.get("vram_mb", 0)
    driver_version = _parse_version(gpu.get("driver", ""))
    torch_version = _parse_version(torch_info.get("torch_version", ""))
    cuda_version = _parse_version(torch_info.get("cuda_version", ""))
    warnings = []
    failures = []

    if not gpu.get("ok"):
        failures.append("未检测到 NVIDIA 显卡，或当前 AI 引擎无法从 PyTorch/系统信息读取显卡。")
    if not torch_info.get("torch_available"):
        failures.append("自带 AI 引擎无法导入 PyTorch。")
    if torch_info.get("torch_available") and not torch_info.get("cuda_available"):
        failures.append("自带 AI 引擎的 PyTorch 无法调用 GPU。")

    if series == "50系":
        if gpu.get("driver") and driver_version < (570,):
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
