import importlib
import os
import platform
import subprocess
import sys
import time

from .comfy_manager import check_comfyui_path, find_python_executable, is_api_available
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


def _timeout_output(exc):
    output = getattr(exc, "stdout", None) or getattr(exc, "output", None) or ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


def _last_probe_module(output):
    current = ""
    completed = set()
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if line.startswith("PROBE_BEGIN "):
            current = line.split(" ", 1)[1].strip()
        elif line.startswith(("PROBE_OK ", "PROBE_FAIL ")):
            parts = line.split(" ", 2)
            if len(parts) >= 2:
                completed.add(parts[1].strip())
    return "" if current in completed else current


def _probe_comfyui_runtime(comfyui_path):
    python_exe = find_python_executable(comfyui_path)
    if not python_exe or not os.path.exists(python_exe):
        return "fail", f"未找到 ComfyUI 内置 Python：{python_exe}"
    code = (
        "import importlib\n"
        "import importlib.util\n"
        "import time\n"
        "mods=['aiohttp','yaml','PIL','safetensors','sqlalchemy','alembic',"
        "'comfyui_frontend_package','comfy_aimdo.control','comfy_kitchen']\n"
        "missing=[name for name in mods if importlib.util.find_spec(name) is None]\n"
        "if missing:\n"
        " print('PROBE_MISSING ' + ','.join(missing), flush=True)\n"
        " raise SystemExit(3)\n"
        "failed=[]\n"
        "for name in mods:\n"
        " print('PROBE_BEGIN ' + name, flush=True)\n"
        " started=time.perf_counter()\n"
        " try:\n"
        "  importlib.import_module(name)\n"
        "  print(f'PROBE_OK {name} {time.perf_counter()-started:.2f}s', flush=True)\n"
        " except Exception as exc:\n"
        "  failed.append(f'{name}: {exc}')\n"
        "  print(f'PROBE_FAIL {name} {exc}', flush=True)\n"
        "print('PROBE_COMPLETE' if not failed else '\\n'.join(failed), flush=True)\n"
        "raise SystemExit(0 if not failed else 2)\n"
    )
    try:
        res = subprocess.run(
            [python_exe, "-c", code],
            cwd=comfyui_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            startupinfo=_hidden_startupinfo(),
        )
        output = (res.stdout or "").strip()
        if res.returncode == 0:
            timing_lines = [line for line in output.splitlines() if line.startswith("PROBE_OK ")]
            timing = "；".join(line.replace("PROBE_OK ", "", 1) for line in timing_lines)
            return "pass", f"{python_exe}；核心依赖可导入；{timing}"
        return "fail", f"{python_exe}；退出码 {res.returncode}；{output[-1200:] or '无输出'}"
    except subprocess.TimeoutExpired as exc:
        output = _timeout_output(exc)
        current = _last_probe_module(output)
        detail = f"，当前模块：{current}" if current else ""
        return (
            "warn",
            f"{python_exe}；依赖文件均已找到，但首次导入超过 90 秒{detail}。"
            "这通常是磁盘读取或安全软件实时扫描较慢，不代表环境损坏",
        )
    except Exception as exc:
        return "fail", f"{python_exe}；探针执行失败：{exc}"


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
    runtime_status, runtime_msg = _probe_comfyui_runtime(comfyui_path)
    results.append(_result(
        "ComfyUI 内置 Python 与核心依赖",
        runtime_status,
        runtime_msg,
        (
            ""
            if runtime_status == "pass"
            else "首次加载较慢，可直接点击[启动 ComfyUI]验证；若启动仍失败，请复制包含启动日志的诊断信息。"
            if runtime_status == "warn"
            else "内置 AI 环境不完整或被安全软件拦截，请更新网盘资源包中的 ComfyUI 引擎。"
        ),
    ))

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
    gpu_info = profile.get("gpu", {})
    torch_info = profile.get("torch", {})
    source = gpu_info.get("source", "")
    smi_ok = bool(source and "nvidia-smi" in source.lower())
    nvidia_ok = bool(gpu_info.get("ok"))
    nvidia_info = (
        f"{gpu_info.get('name', '未检测到')} / "
        f"显存 {round(int(gpu_info.get('vram_mb') or 0) / 1024, 1)}GB / "
        f"驱动 {gpu_info.get('driver') or '未知'} / "
        f"来源 {source or '无'}"
    )
    results.append(_result(
        "是否有 nvidia-smi",
        "pass" if smi_ok else "warn",
        "可调用" if smi_ok else "不可调用或不在 PATH，已尝试系统/PyTorch 兜底检测",
    ))
    results.append(_result("是否检测到 NVIDIA 显卡", "pass" if nvidia_ok else "fail", nvidia_info))
    results.append(_result(
        "AI 商品图硬件适配档位",
        profile["status"],
        profile_summary(profile),
        "；".join(profile.get("failures") or profile.get("warnings") or [profile.get("suggestion", "")]),
    ))

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
        "" if api_ok else "ComfyUI 未启动时此项会显示警告，可点击[启动 ComfyUI]后重新检测。",
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
