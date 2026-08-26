import json
import os
import subprocess
import time
import urllib.request
import uuid

from paths import core_path, logs_dir, runtime_path, workspace_path


EVENT_PREFIX = "VOICE_EVENT:"
RUNTIME_LOG_FILE = os.path.join(logs_dir(), "voice_clone_runtime_latest.log")
ASR_RUNTIME_LOG_FILE = os.path.join(logs_dir(), "asr_runtime_latest.log")
RUNTIME_PYTHON = runtime_path("engines", "ComfyUI", "python", "python.exe")
RUNTIME_SITE_PACKAGES = runtime_path("engines", "voice_clone", "site-packages")


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def runtime_worker_path():
    return core_path("modules", "voice_clone", "runtime_worker.py")


def build_runtime_env():
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    paths = [RUNTIME_SITE_PACKAGES]
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["MODELSCOPE_CACHE"] = runtime_path("models")
    env.setdefault("PYTHONUTF8", "1")
    return env


def release_comfy_gpu_memory():
    payload = json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8")
    for port in (8199, 8188):
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/free",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                if 200 <= response.status < 300:
                    return True, port
        except Exception:
            continue
    return False, None


def probe_voice_runtime(timeout=60):
    if not os.path.isfile(RUNTIME_PYTHON):
        return False, {"error": f"未找到 AI Python：{RUNTIME_PYTHON}"}
    if not os.path.isdir(RUNTIME_SITE_PACKAGES):
        return False, {"error": f"未找到音频克隆运行层：{RUNTIME_SITE_PACKAGES}"}
    code = (
        "import json,sys\n"
        "data={'python':sys.executable,'python_version':sys.version.split()[0]}\n"
        "try:\n"
        " import torch,torchaudio,transformers,soundfile,voxcpm,funasr,modelscope\n"
        " data.update({'torch':torch.__version__,'torchaudio':torchaudio.__version__,"
        "'transformers':transformers.__version__,'cuda_available':bool(torch.cuda.is_available()),"
        "'cuda_version':torch.version.cuda,'voxcpm_file':voxcpm.__file__,"
        "'funasr':getattr(funasr,'__version__','已安装'),"
        "'modelscope':getattr(modelscope,'__version__','已安装')})\n"
        " if torch.cuda.is_available():\n"
        "  props=torch.cuda.get_device_properties(0)\n"
        "  data.update({'gpu_name':props.name,'vram_mb':int(props.total_memory/1024/1024)})\n"
        "except Exception as exc:\n"
        " data['error']=repr(exc)\n"
        "print(json.dumps(data,ensure_ascii=False))\n"
        "raise SystemExit(0 if 'error' not in data and data.get('cuda_available') else 2)\n"
    )
    try:
        result = subprocess.run(
            [RUNTIME_PYTHON, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=build_runtime_env(),
            timeout=timeout,
            startupinfo=hidden_startupinfo(),
        )
        data = {}
        for line in reversed((result.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    break
                except Exception:
                    continue
        if not data:
            data = {"error": (result.stdout or "").strip()[-2000:] or "运行层探针没有返回结果"}
        return result.returncode == 0, data
    except Exception as exc:
        return False, {"error": str(exc)}


def probe_asr_runtime(timeout=60):
    if not os.path.isfile(RUNTIME_PYTHON):
        return False, {"error": f"未找到 AI Python：{RUNTIME_PYTHON}"}
    code = (
        "import json,sys\n"
        "data={'python':sys.executable,'python_version':sys.version.split()[0]}\n"
        "try:\n"
        " import torch,funasr,modelscope\n"
        " data.update({'torch':torch.__version__,"
        "'cuda_available':bool(torch.cuda.is_available()),"
        "'cuda_runtime':torch.version.cuda,"
        "'funasr':getattr(funasr,'__version__','已安装'),"
        "'modelscope':getattr(modelscope,'__version__','已安装')})\n"
        " if torch.cuda.is_available():\n"
        "  props=torch.cuda.get_device_properties(0)\n"
        "  data.update({'gpu_name':props.name,"
        "'vram_mb':int(props.total_memory/1024/1024),"
        "'compute_capability':f'{props.major}.{props.minor}'})\n"
        "  x=torch.ones((128,128),device='cuda'); y=x@x; torch.cuda.synchronize()\n"
        "  data['cuda_tensor_probe']=float(y[0,0].item())==128.0\n"
        "except Exception as exc:\n"
        " data['error']=repr(exc)\n"
        "print(json.dumps(data,ensure_ascii=False))\n"
        "raise SystemExit(0 if 'error' not in data and data.get('cuda_available') "
        "and data.get('cuda_tensor_probe') else 2)\n"
    )
    try:
        result = subprocess.run(
            [RUNTIME_PYTHON, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=build_runtime_env(),
            timeout=timeout,
            startupinfo=hidden_startupinfo(),
        )
        data = {}
        for line in reversed((result.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    break
                except Exception:
                    continue
        if not data:
            data = {"error": (result.stdout or "").strip()[-2000:] or "ASR 运行层探针没有返回结果"}
        return result.returncode == 0, data
    except Exception as exc:
        return False, {"error": str(exc)}


def _terminate_process_tree(process):
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=hidden_startupinfo(),
                timeout=15,
                check=False,
            )
        else:
            process.terminate()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def run_voice_task(task, event_callback=None, process_holder=None):
    os.makedirs(logs_dir(), exist_ok=True)
    cache_dir = workspace_path("缓存")
    os.makedirs(cache_dir, exist_ok=True)
    token = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    task_path = os.path.join(cache_dir, f"voice_task_{token}.json")
    result_path = os.path.join(cache_dir, f"voice_result_{token}.json")
    worker = runtime_worker_path()
    is_asr_task = task.get("operation") in {"transcribe", "asr_preload"}
    runtime_log_file = ASR_RUNTIME_LOG_FILE if is_asr_task else RUNTIME_LOG_FILE
    task_label = "SenseVoice 转写" if is_asr_task else "音频克隆"

    if not os.path.isfile(RUNTIME_PYTHON):
        raise RuntimeError(f"未找到独立 AI Python：{RUNTIME_PYTHON}")
    if not os.path.isfile(worker):
        raise RuntimeError(f"未找到音频克隆运行脚本：{worker}")
    if not os.path.isdir(RUNTIME_SITE_PACKAGES):
        raise RuntimeError(f"未找到音频克隆运行层：{RUNTIME_SITE_PACKAGES}")

    with open(task_path, "w", encoding="utf-8") as handle:
        json.dump(task, handle, ensure_ascii=False, indent=2)

    command = [RUNTIME_PYTHON, worker, task_path, result_path]
    process = None
    output_lines = []
    try:
        released, comfy_port = release_comfy_gpu_memory()
        if released and event_callback:
            event_callback({
                "message": f"已释放 ComfyUI 后台模型显存（端口 {comfy_port}），准备启动{task_label}。",
                "progress": 8,
            })
        with open(runtime_log_file, "w", encoding="utf-8", errors="replace") as log:
            log.write(f"【{task_label}独立运行日志】\n")
            log.write(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            log.write(f"AI Python：{RUNTIME_PYTHON}\n")
            log.write(f"运行层：{RUNTIME_SITE_PACKAGES}\n")
            log.write(f"任务类型：{task.get('kind') or task.get('operation')}\n")
            log.write(f"模型路径：{task.get('model_path') or task.get('asr_model_path', '')}\n\n【底层输出】\n")
            if released:
                log.write(f"已请求 ComfyUI 端口 {comfy_port} 卸载模型并释放显存。\n")
            log.flush()
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=build_runtime_env(),
                startupinfo=hidden_startupinfo(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if process_holder is not None:
                process_holder["process"] = process
            for raw_line in iter(process.stdout.readline, ""):
                line = raw_line.rstrip("\r\n")
                output_lines.append(line)
                log.write(line + "\n")
                log.flush()
                if line.startswith(EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(EVENT_PREFIX):])
                        if event_callback:
                            event_callback(event)
                    except Exception:
                        pass
            returncode = process.wait()

        result = {}
        if os.path.isfile(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as handle:
                    result = json.load(handle)
            except Exception:
                result = {}
        if returncode != 0 or not result.get("ok"):
            detail = result.get("error") or "\n".join(output_lines[-30:]) or f"退出码 {returncode}"
            raise RuntimeError(f"{detail}\n完整日志：{runtime_log_file}")
        return result
    finally:
        if process_holder is not None:
            process_holder.pop("process", None)
        for path in (task_path, result_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


def cancel_voice_task(process_holder):
    process = process_holder.get("process") if process_holder else None
    _terminate_process_tree(process)
