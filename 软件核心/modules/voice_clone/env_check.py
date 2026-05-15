import importlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time

from packaging.version import Version, InvalidVersion

from paths import tools_dir
from .config import ENV_REPORT_FILE, OUTPUT_DIR, ensure_dirs
from .model_manager import check_model_path


def _version_ok(current, minimum=None, maximum=None):
    try:
        current_v = Version(str(current).split("+")[0])
        if minimum and current_v < Version(minimum):
            return False
        if maximum and current_v >= Version(maximum):
            return False
        return True
    except InvalidVersion:
        return False


def _result(name, status, value, suggestion=""):
    return {
        "name": name,
        "status": status,
        "value": str(value),
        "suggestion": suggestion,
    }


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _run_cmd(cmd, timeout=8):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=timeout,
        startupinfo=_hidden_startupinfo(),
    )


def _find_ffmpeg():
    bundled = os.path.join(tools_dir(), "ffmpeg.exe")
    if os.path.exists(bundled):
        return bundled
    return shutil.which("ffmpeg") or ""


def _query_gpu():
    info = {
        "nvidia_smi": False,
        "gpu_name": "",
        "memory_mb": 0,
        "driver": "",
        "error": "",
    }
    try:
        res = _run_cmd([
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ])
        if res.returncode != 0:
            info["error"] = (res.stdout or "").strip()
            return info
        line = [x for x in (res.stdout or "").splitlines() if x.strip()][0]
        parts = [x.strip() for x in line.split(",")]
        info["nvidia_smi"] = True
        info["gpu_name"] = parts[0] if parts else ""
        info["memory_mb"] = int(float(parts[1])) if len(parts) > 1 else 0
        info["driver"] = parts[2] if len(parts) > 2 else ""
    except Exception as exc:
        info["error"] = str(exc)
    return info


def _module_version(module_name):
    try:
        module = importlib.import_module(module_name)
        return True, getattr(module, "__version__", "已安装")
    except Exception as exc:
        return False, str(exc)


def _output_writable(output_dir):
    try:
        os.makedirs(output_dir, exist_ok=True)
        fd, test_path = tempfile.mkstemp(prefix="voice_clone_test_", suffix=".tmp", dir=output_dir)
        os.close(fd)
        os.remove(test_path)
        return True, "可写入"
    except Exception as exc:
        return False, str(exc)


def check_environment(model_path, output_dir=None):
    """检测 VoxCPM2 运行环境，并返回给 UI 使用的结构化结果。"""
    ensure_dirs()
    output_dir = output_dir or OUTPUT_DIR
    results = []

    is_win64 = platform.system().lower() == "windows" and platform.machine().endswith("64")
    results.append(_result(
        "操作系统是否为 Windows 64位",
        "pass" if is_win64 else "fail",
        f"{platform.system()} {platform.machine()}",
        "" if is_win64 else "当前版本主要面向 Windows 64 位客户，请使用 Windows 10/11 64 位系统。",
    ))

    gpu = _query_gpu()
    results.append(_result(
        "是否能调用 nvidia-smi",
        "pass" if gpu["nvidia_smi"] else "fail",
        "可调用" if gpu["nvidia_smi"] else "不可调用",
        "" if gpu["nvidia_smi"] else "未检测到 NVIDIA 显卡驱动，请安装或更新 NVIDIA 官方显卡驱动后重启电脑。",
    ))
    results.append(_result(
        "是否检测到 NVIDIA 显卡",
        "pass" if gpu["gpu_name"] else "fail",
        gpu["gpu_name"] or "未检测到",
        "" if gpu["gpu_name"] else "AI音频克隆建议使用 NVIDIA 独立显卡运行。",
    ))
    results.append(_result("显卡名称", "pass" if gpu["gpu_name"] else "fail", gpu["gpu_name"] or "无"))
    mem_status = "pass" if gpu["memory_mb"] >= 8192 else ("warn" if gpu["memory_mb"] > 0 else "fail")
    results.append(_result(
        "显存大小",
        mem_status,
        f"{gpu['memory_mb']} MB" if gpu["memory_mb"] else "未知",
        "" if gpu["memory_mb"] >= 8192 else "显存低于 8GB，可能无法稳定运行音频克隆功能。",
    ))
    results.append(_result(
        "显存是否 >= 8GB",
        "pass" if gpu["memory_mb"] >= 8192 else "warn",
        "是" if gpu["memory_mb"] >= 8192 else "否",
        "" if gpu["memory_mb"] >= 8192 else "建议使用 8GB 以上显存的 NVIDIA 显卡。",
    ))
    results.append(_result(
        "NVIDIA 驱动是否存在",
        "pass" if gpu["driver"] else "fail",
        gpu["driver"] or "未检测到",
        "" if gpu["driver"] else "请安装 NVIDIA 官方显卡驱动。",
    ))

    py_version = ".".join(str(x) for x in sys.version_info[:3])
    py_ok = sys.version_info >= (3, 10) and sys.version_info < (3, 13)
    results.append(_result(
        "Python 版本是否满足 >=3.10 且 <3.13",
        "pass" if py_ok else "fail",
        py_version,
        "" if py_ok else "VoxCPM2 官方要求 Python >=3.10 且 <3.13。",
    ))

    torch_ok, torch_version = _module_version("torch")
    torch_version_ok = torch_ok and _version_ok(torch_version, "2.5.0")
    results.append(_result(
        "是否安装 torch",
        "pass" if torch_ok else "fail",
        torch_version if torch_ok else "未安装",
        "" if torch_ok else "当前环境未安装 PyTorch，请安装适配显卡的 PyTorch GPU 版本。",
    ))
    results.append(_result(
        "torch 版本是否 >=2.5.0",
        "pass" if torch_version_ok else "fail",
        torch_version if torch_ok else "未安装",
        "" if torch_version_ok else "VoxCPM2 官方要求 PyTorch >= 2.5.0。",
    ))

    cuda_available = False
    cuda_version = "未知"
    if torch_ok:
        try:
            import torch
            cuda_available = bool(torch.cuda.is_available())
            cuda_version = getattr(torch.version, "cuda", "") or "CPU版本"
        except Exception:
            cuda_available = False
    results.append(_result(
        "torch.cuda.is_available() 是否为 True",
        "pass" if cuda_available else "fail",
        str(cuda_available),
        "" if cuda_available else "当前 PyTorch 无法调用 GPU，可能安装成了 CPU 版本。",
    ))
    results.append(_result(
        "CUDA 是否可用",
        "pass" if cuda_available else "fail",
        cuda_version,
        "" if cuda_available else "请确认 NVIDIA 驱动正常，并安装匹配 CUDA 的 PyTorch GPU 版本。",
    ))

    for module_name, label in [
        ("torchaudio", "torchaudio 是否可用"),
        ("voxcpm", "voxcpm 是否可用"),
        ("soundfile", "soundfile 是否可用"),
    ]:
        ok, version_or_error = _module_version(module_name)
        results.append(_result(
            label,
            "pass" if ok else "fail",
            version_or_error if ok else "不可用",
            "" if ok else f"当前环境缺少 {module_name}，请安装依赖后重试。",
        ))

    ffmpeg = _find_ffmpeg()
    results.append(_result(
        "ffmpeg 是否可用",
        "pass" if ffmpeg else "fail",
        ffmpeg or "未找到",
        "" if ffmpeg else "未找到 ffmpeg，音频转换和裁剪功能可能无法使用。",
    ))

    model_ok, model_msg, model_files = check_model_path(model_path)
    results.append(_result(
        "VoxCPM2 模型目录是否存在",
        "pass" if os.path.isdir(model_path) else "fail",
        model_path or "未设置",
        "" if os.path.isdir(model_path) else "未找到 VoxCPM2 模型文件，请在模型路径设置中选择正确的模型文件夹。",
    ))
    results.append(_result(
        "模型目录下是否有关键模型文件",
        "pass" if model_ok else "fail",
        model_msg,
        "" if model_ok else "请确认选择的是完整的 VoxCPM2 模型目录，不要只选择上一级文件夹。",
    ))

    writable, writable_msg = _output_writable(output_dir)
    results.append(_result(
        "用户工作区/输出/voice_clone 是否可写入",
        "pass" if writable else "fail",
        writable_msg,
        "" if writable else "输出目录无法写入，请检查软件目录权限或更换输出目录。",
    ))

    blocking_failures = [
        item for item in results
        if item["status"] == "fail" and item["name"] not in {"显存是否 >= 8GB"}
    ]
    final_pass = not blocking_failures
    results.append(_result(
        "最终环境是否通过",
        "pass" if final_pass else "fail",
        "通过" if final_pass else "未通过",
        "" if final_pass else "请先处理红色失败项，再重新检测运行环境。",
    ))

    report_text = build_report(results, model_path, output_dir, gpu, model_files)
    write_report(report_text)
    return results, ENV_REPORT_FILE, final_pass


def build_report(results, model_path, output_dir, gpu, model_files):
    failed = [x for x in results if x["status"] == "fail" and x["name"] != "最终环境是否通过"]
    warnings = [x for x in results if x["status"] == "warn"]
    lines = [
        "软件名称：牛爷爷剪辑软件",
        "模块名称：AI音频克隆",
        f"检测时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "【系统信息】",
        f"系统信息：{platform.platform()}",
        f"Python 版本：{'.'.join(str(x) for x in sys.version_info[:3])}",
        "",
        "【显卡信息】",
        f"显卡信息：{gpu.get('gpu_name') or '未检测到'}",
        f"显存信息：{gpu.get('memory_mb', 0)} MB",
        f"驱动状态：{gpu.get('driver') or '未检测到'}",
        f"nvidia-smi：{'可调用' if gpu.get('nvidia_smi') else '不可调用'}",
        "",
        "【模型与输出】",
        f"VoxCPM2 模型路径：{model_path}",
        f"输出目录：{output_dir}",
        f"模型文件总数：{model_files.get('total_files', 0)}",
        f"配置文件数量：{len(model_files.get('config_files', []))}",
        f"权重文件数量：{len(model_files.get('weight_files', []))}",
        "",
        "【检测明细】",
    ]
    for item in results:
        status_text = {"pass": "通过", "warn": "警告", "fail": "失败"}.get(item["status"], item["status"])
        lines.append(f"- {item['name']}：{status_text}；{item['value']}")
        if item.get("suggestion"):
            lines.append(f"  建议：{item['suggestion']}")

    lines.extend([
        "",
        "【最终结论】",
        "最终结论：通过" if not failed else "最终结论：未通过",
        "",
        "【失败原因】",
    ])
    if failed:
        lines.extend([f"- {item['name']}：{item['value']}" for item in failed])
    else:
        lines.append("- 无")

    lines.append("")
    lines.append("【建议解决方法】")
    suggestions = [x["suggestion"] for x in failed + warnings if x.get("suggestion")]
    if suggestions:
        lines.extend([f"- {x}" for x in dict.fromkeys(suggestions)])
    else:
        lines.append("- 当前环境检测通过，可以尝试生成音频。")
    return "\n".join(lines)


def write_report(text):
    ensure_dirs()
    with open(ENV_REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(text)
