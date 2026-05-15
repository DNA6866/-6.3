import importlib
import os
import platform
import subprocess
import sys
import time

from .comfy_manager import check_comfyui_path, is_api_available
from .config import ENV_REPORT_FILE, ensure_dirs
from .hardware_profile import detect_hardware_profile, profile_summary
from .workflow_manager import check_required_models, scan_zimage_workflows


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _result(name, status, value, suggestion=""):
    return {"name": name, "status": status, "value": str(value), "suggestion": suggestion}


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
            startupinfo=_hidden_startupinfo(),
        )
        if res.returncode == 0 and res.stdout.strip():
            return True, res.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return False, "未检测到"


def _module_available(name):
    try:
        module = importlib.import_module(name)
        return True, getattr(module, "__version__", "已安装")
    except Exception as exc:
        return False, str(exc)


def _output_writable(output_dir):
    try:
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "image_generate_write_test.tmp")
        with open(path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(path)
        return True, "可写入"
    except Exception as exc:
        return False, str(exc)


def check_environment(config):
    ensure_dirs()
    comfyui_path = config.get("comfyui_path", "")
    output_dir = config.get("output_dir", "")
    host = config.get("host", "127.0.0.1")
    port = int(config.get("port", 8188))
    results = []

    ok, msg = check_comfyui_path(comfyui_path)
    results.append(_result("ComfyUI 路径是否存在", "pass" if ok else "fail", comfyui_path, "" if ok else msg))
    results.append(_result("ComfyUI 主程序 main.py 是否存在", "pass" if os.path.exists(os.path.join(comfyui_path, "main.py")) else "fail", msg))

    workflows = scan_zimage_workflows(comfyui_path)
    results.append(_result(
        "是否找到 Z-Image workflow",
        "pass" if workflows else "fail",
        workflows[0] if workflows else "未找到",
        "" if workflows else "未找到 Z-Image 工作流文件，请确认工作流已正确放置。",
    ))

    model_ok, found, missing = check_required_models(comfyui_path, config.get("workflow_path", ""))
    results.append(_result(
        "是否找到相关模型文件",
        "pass" if model_ok else "fail",
        f"已找到 {len(found)} 个，缺少 {len(missing)} 个",
        "" if model_ok else "未找到相关模型文件，请确认模型已放入 ComfyUI 对应模型目录。",
    ))

    profile = detect_hardware_profile(comfyui_path)
    nvidia_ok, nvidia_info = _query_nvidia()
    results.append(_result("是否有 nvidia-smi", "pass" if nvidia_ok else "fail", "可调用" if nvidia_ok else "不可调用"))
    results.append(_result("是否检测到 NVIDIA 显卡", "pass" if nvidia_ok else "fail", nvidia_info))
    results.append(_result(
        "AI 商品图硬件适配档位",
        profile["status"],
        profile_summary(profile),
        "；".join(profile.get("failures") or profile.get("warnings") or [profile.get("suggestion", "")]),
    ))

    torch_info = profile.get("torch", {})
    torch_ok = bool(torch_info.get("torch_available"))
    results.append(_result("自带 AI 引擎是否有 torch", "pass" if torch_ok else "fail", torch_info.get("torch_version", torch_info.get("error", "未安装"))))
    cuda_available = bool(torch_info.get("cuda_available"))
    results.append(_result(
        "自带 AI 引擎 torch.cuda.is_available() 是否为 True",
        "pass" if cuda_available else "fail",
        str(cuda_available),
        "" if cuda_available else "当前 PyTorch 无法调用 GPU，请检查显卡驱动或 CUDA 环境。",
    ))

    writable, writable_msg = _output_writable(output_dir)
    results.append(_result("输出目录是否可写", "pass" if writable else "fail", writable_msg))

    api_ok = is_api_available(host, port)
    results.append(_result(
        "ComfyUI API 是否可访问",
        "pass" if api_ok else "warn",
        f"http://{host}:{port}" if api_ok else "未启动或不可访问",
        "" if api_ok else "ComfyUI 未启动时此项会显示警告，可点击“启动 ComfyUI”后重新检测。",
    ))

    final_pass = not any(item["status"] == "fail" for item in results)
    results.append(_result("最终环境是否通过", "pass" if final_pass else "fail", "通过" if final_pass else "未通过"))
    report = build_report(results, config, found, missing, profile)
    write_report(report)
    return results, ENV_REPORT_FILE, final_pass


def build_report(results, config, found, missing, profile=None):
    profile = profile or {}
    lines = [
        "软件名称：牛爷爷剪辑软件",
        "模块名称：AI商品图生成",
        f"检测时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"系统信息：{platform.platform()}",
        f"Python 版本：{sys.version.split()[0]}",
        f"ComfyUI 路径：{config.get('comfyui_path', '')}",
        f"工作流路径：{config.get('workflow_path', '')}",
        f"输出目录：{config.get('output_dir', '')}",
        f"硬件适配档位：{profile_summary(profile) if profile else '未检测'}",
        f"推荐生成尺寸：{profile.get('width', '')} x {profile.get('height', '')}",
        f"推荐启动参数：{' '.join(profile.get('comfyui_args', [])) or '默认'}",
        "",
        "【检测明细】",
    ]
    for item in results:
        status = {"pass": "通过", "warn": "警告", "fail": "失败"}.get(item["status"], item["status"])
        lines.append(f"- {item['name']}：{status}；{item['value']}")
        if item.get("suggestion"):
            lines.append(f"  建议：{item['suggestion']}")
    lines.extend(["", "【模型文件】"])
    lines.extend([f"- 已找到：{x}" for x in found] or ["- 已找到：无"])
    lines.extend([f"- 缺少：{x}" for x in missing] or ["- 缺少：无"])
    return "\n".join(lines)


def write_report(text):
    ensure_dirs()
    with open(ENV_REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(text)
