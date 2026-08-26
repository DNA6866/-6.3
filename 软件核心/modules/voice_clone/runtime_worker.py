import inspect
import json
import os
import re
import subprocess
import sys
import traceback


EVENT_PREFIX = "VOICE_EVENT:"


def emit_event(message, progress=None):
    event = {"message": str(message)}
    if progress is not None:
        event["progress"] = int(progress)
    print(EVENT_PREFIX + json.dumps(event, ensure_ascii=False), flush=True)


def normalize_text_basic(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip()
    replacements = {
        "％": "%",
        "～": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def is_ascii_path(path):
    try:
        str(path).encode("ascii")
        return True
    except Exception:
        return False


def subst_ascii_dir(path):
    if os.name != "nt" or not path or is_ascii_path(path):
        return path, ""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return path, ""
    for letter in "ZYXWVUTSRQPONMLKJIHGFEDCBA":
        drive = f"{letter}:"
        if os.path.exists(drive + "\\"):
            continue
        result = subprocess.run(
            ["subst", drive, path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=hidden_startupinfo(),
        )
        if result.returncode == 0:
            return drive + "\\", drive
    return path, ""


def release_subst_drive(drive):
    if os.name == "nt" and drive:
        subprocess.run(
            ["subst", drive, "/D"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            startupinfo=hidden_startupinfo(),
        )


def clean_asr_text(text):
    text = re.sub(r"<\|[^>]+?\|>", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def gpu_diagnostics():
    import torch

    details = [
        f"PyTorch {torch.__version__}",
        f"CUDA运行库 {torch.version.cuda or '无'}",
        f"torch.cuda.is_available={torch.cuda.is_available()}",
    ]
    if torch.cuda.is_available():
        try:
            props = torch.cuda.get_device_properties(0)
            details.extend([
                f"显卡 {props.name}",
                f"显存 {int(props.total_memory / 1024 / 1024)} MB",
                f"算力 {props.major}.{props.minor}",
            ])
        except Exception as exc:
            details.append(f"读取显卡属性失败：{exc}")
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            startupinfo=hidden_startupinfo(),
        )
        if (result.stdout or "").strip():
            details.append("nvidia-smi " + (result.stdout or "").strip().splitlines()[0])
    except Exception as exc:
        details.append(f"nvidia-smi不可用：{exc}")
    return "；".join(details)


def _sensevoice_devices(force_cpu=False, allow_cpu_fallback=False):
    if force_cpu:
        return ["cpu"]
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError(
            "SenseVoice GPU不可用，不再自动切换CPU。"
            f"GPU诊断：{gpu_diagnostics()}"
        )
    devices = ["cuda:0"]
    if allow_cpu_fallback:
        devices.append("cpu")
    return devices


def _load_sensevoice(model_path, action, devices=None):
    from funasr import AutoModel

    last_error = None
    for device in devices or _sensevoice_devices():
        try:
            emit_event(f"正在独立 AI 环境中加载 SenseVoiceSmall，设备：{device}...", 20)
            model = AutoModel(
                model=model_path,
                trust_remote_code=False,
                disable_update=True,
                device=device,
            )
            return model, device
        except Exception as exc:
            last_error = exc
            if device.startswith("cuda") and "cpu" in (devices or []):
                emit_event(f"SenseVoice GPU {action}失败，自动切换 CPU：{exc}", 25)
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                continue
            if device.startswith("cuda"):
                raise RuntimeError(
                    f"SenseVoice GPU {action}失败，不再自动切换CPU：{exc}。"
                    f"GPU诊断：{gpu_diagnostics()}"
                ) from exc
            raise
    raise RuntimeError(
        f"SenseVoice GPU {action}失败，不再自动切换CPU：{last_error}。"
        f"GPU诊断：{gpu_diagnostics()}"
    )


def preload_sensevoice(task):
    model_path = str(task.get("asr_model_path") or "").strip()
    if not os.path.isfile(os.path.join(model_path, "model.pt")):
        raise RuntimeError(f"SenseVoiceSmall 模型不完整：{model_path}")
    safe_model_path, subst_drive = subst_ascii_dir(model_path)
    try:
        devices = _sensevoice_devices(
            force_cpu=bool(task.get("force_cpu")),
            allow_cpu_fallback=bool(task.get("allow_cpu_fallback")),
        )
        if not task.get("force_cpu"):
            emit_event(f"SenseVoice GPU检测通过：{gpu_diagnostics()}", 12)
        _model, device = _load_sensevoice(safe_model_path, "加载", devices=devices)
    finally:
        release_subst_drive(subst_drive)
    emit_event("SenseVoiceSmall 独立环境检测完成。", 100)
    return {"ok": True, "device": device}


def transcribe_reference(task):
    audio_path = str(task.get("audio_path") or "").strip()
    model_path = str(task.get("asr_model_path") or "").strip()
    context = str(task.get("context") or "音频").strip()
    if not os.path.isfile(audio_path):
        raise RuntimeError(f"参考音频不存在：{audio_path}")
    if not os.path.isfile(os.path.join(model_path, "model.pt")):
        raise RuntimeError(f"SenseVoiceSmall 模型不完整：{model_path}")

    safe_model_path, subst_drive = subst_ascii_dir(model_path)
    try:
        devices = _sensevoice_devices(
            force_cpu=bool(task.get("force_cpu")),
            allow_cpu_fallback=bool(task.get("allow_cpu_fallback")),
        )
        if not task.get("force_cpu"):
            emit_event(f"SenseVoice GPU检测通过：{gpu_diagnostics()}", 12)
        model, device = _load_sensevoice(safe_model_path, "转写", devices=devices)
        emit_event(f"正在识别{context}文字稿...", 60)
        try:
            result = model.generate(
                input=audio_path,
                language="zh",
                use_itn=True,
                batch_size_s=60,
            )
        except Exception as exc:
            if not device.startswith("cuda") or not task.get("allow_cpu_fallback"):
                if device.startswith("cuda"):
                    raise RuntimeError(
                        f"SenseVoice GPU转写失败，不再自动切换CPU：{exc}。"
                        f"GPU诊断：{gpu_diagnostics()}"
                    ) from exc
                raise
            emit_event(f"SenseVoice GPU 转写失败，自动切换 CPU：{exc}", 55)
            try:
                del model
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
            model, device = _load_sensevoice(
                safe_model_path,
                "CPU 转写",
                devices=["cpu"],
            )
            result = model.generate(
                input=audio_path,
                language="zh",
                use_itn=True,
                batch_size_s=60,
            )
    finally:
        release_subst_drive(subst_drive)

    if isinstance(result, list) and result:
        text = result[0].get("text", "")
    elif isinstance(result, dict):
        text = result.get("text", "")
    else:
        text = str(result)
    text = clean_asr_text(text)
    if not text:
        raise RuntimeError("SenseVoice 没有识别到有效文字，请确认参考音频中有清晰人声。")
    emit_event(f"{context}文字稿识别完成。", 100)
    return {"ok": True, "text": text, "device": device}


def build_engine(task):
    from voxcpm import VoxCPM

    model_path = task["model_path"]
    enable_denoiser = bool(task.get("enable_denoiser", False))
    zipenhancer = task.get("zipenhancer_model_path") or None
    emit_event("正在独立 AI 环境中加载 VoxCPM2 模型...", 15)
    model = VoxCPM.from_pretrained(
        model_path,
        load_denoiser=enable_denoiser,
        zipenhancer_model_id=zipenhancer,
        local_files_only=True,
        optimize=False,
    )
    emit_event("VoxCPM2 模型加载完成。", 38)
    return model


def accepted_kwargs(model, kwargs):
    try:
        signature = inspect.signature(model.generate)
        if any(param.kind == param.VAR_KEYWORD for param in signature.parameters.values()):
            return kwargs
        accepted = set(signature.parameters)
        return {key: value for key, value in kwargs.items() if key in accepted}
    except Exception:
        return kwargs


def write_wav(model, wav, output_path):
    import soundfile as sf

    sample_rate = getattr(getattr(model, "tts_model", None), "sample_rate", 48000)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    sf.write(output_path, wav, sample_rate)
    if not os.path.isfile(output_path) or os.path.getsize(output_path) < 1024:
        raise RuntimeError("推理已结束，但没有生成有效音频文件。")
    return output_path


def generate_one(model, task, output_path=None):
    kind = task.get("kind")
    text = normalize_text_basic(task.get("text", ""))
    if not text:
        raise RuntimeError("生成文本为空。")
    if task.get("normalize"):
        text = normalize_text_basic(text)

    kwargs = {
        "text": text,
        "cfg_value": float(task.get("cfg_value", 2.0)),
        "inference_timesteps": int(task.get("inference_timesteps", 10)),
        "normalize": False,
        "denoise": bool(task.get("denoise", False)),
    }
    if kind == "tts":
        description = str(task.get("voice_description", "")).strip()
        if description:
            kwargs["text"] = f"({description}){text}"
    elif kind == "clone":
        reference = task.get("reference_audio", "")
        if not os.path.isfile(reference):
            raise RuntimeError(f"参考音频不存在：{reference}")
        prompt = str(task.get("control_prompt", "")).strip()
        if prompt:
            kwargs["text"] = f"({prompt}){text}"
        kwargs["reference_wav_path"] = reference
    elif kind == "ultimate":
        reference = task.get("reference_audio", "")
        prompt_text = str(task.get("prompt_text", "")).strip()
        if not os.path.isfile(reference):
            raise RuntimeError(f"参考音频不存在：{reference}")
        if not prompt_text:
            raise RuntimeError("参考音频文字稿为空。")
        kwargs.update({
            "prompt_wav_path": reference,
            "prompt_text": prompt_text,
            "reference_wav_path": reference,
        })
    else:
        raise RuntimeError(f"未知音频任务：{kind}")

    emit_event("正在生成音频，请勿关闭软件...", 48)
    wav = model.generate(**accepted_kwargs(model, kwargs))
    return write_wav(model, wav, output_path or task["output_path"])


def run_batch(task):
    texts = task.get("texts") or []
    if not texts:
        raise RuntimeError("批量生成文本为空。")
    model = build_engine(task)
    output_dir = task["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    success = 0
    failed = 0
    outputs = []
    output_paths = task.get("output_paths") or []
    total = len(texts)
    for index, text in enumerate(texts, 1):
        item = dict(task)
        item["text"] = text
        if task.get("reference_audio") and task.get("prompt_text"):
            item["kind"] = "ultimate"
        elif task.get("reference_audio"):
            item["kind"] = "clone"
        else:
            item["kind"] = "tts"
            item["voice_description"] = task.get("control_prompt", "")
        output_path = (
            output_paths[index - 1]
            if index - 1 < len(output_paths)
            else os.path.join(output_dir, f"batch_{index:03d}.wav")
        )
        try:
            outputs.append(generate_one(model, item, output_path))
            success += 1
            emit_event(
                f"第 {index}/{total} 条生成完成。",
                38 + int(index / total * 61),
            )
        except Exception as exc:
            failed += 1
            emit_event(f"第 {index}/{total} 条生成失败：{exc}", 38 + int(index / total * 61))
    return {
        "ok": True,
        "success": success,
        "failed": failed,
        "outputs": outputs,
        "output_dir": output_dir,
    }


def main(task_path, result_path):
    with open(task_path, "r", encoding="utf-8") as handle:
        task = json.load(handle)
    if task.get("operation") == "asr_preload":
        result = preload_sensevoice(task)
    elif task.get("operation") == "transcribe":
        result = transcribe_reference(task)
    elif task.get("operation") == "batch":
        result = run_batch(task)
    else:
        model = build_engine(task)
        output = generate_one(model, task)
        emit_event("音频生成完成。", 100)
        result = {"ok": True, "output": output}
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    result_file = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        raise SystemExit(main(sys.argv[1], result_file))
    except SystemExit:
        raise
    except Exception as exc:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        emit_event(f"独立 AI 任务运行失败：{exc}", 0)
        if result_file:
            try:
                with open(result_file, "w", encoding="utf-8") as handle:
                    json.dump({"ok": False, "error": detail}, handle, ensure_ascii=False, indent=2)
            except Exception:
                pass
        print(detail, file=sys.stderr, flush=True)
        raise SystemExit(1)
