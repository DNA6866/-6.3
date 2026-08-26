"""启动环境自检：启动时检查主包/工具/模型是否齐全，写入统一日志。

用途：
  1. 客户缺主包（网盘资源包）时，启动即提示，而不是用到功能才报错。
  2. 每次启动把环境全貌写入 日志/environment_check.txt，开发者拿到日志即可定位缺什么。
  3. 只做文件存在性检查，不加载模型，不影响启动速度。

返回：list[dict] —— {name, path, ok, level}，level 为 critical/warning/ok。
"""

import os

from paths import RESOURCE_ROOT, app_root, logs_dir, models_dir, tools_dir


def _status(name, path, ok, level):
    return {"name": name, "path": path, "ok": bool(ok), "level": level}


def run_environment_check():
    """执行启动自检，返回检测项列表。"""
    items = []

    # 1. 主包（网盘资源包）
    resource_ok = os.path.isdir(RESOURCE_ROOT)
    items.append(_status(
        "主包（网盘资源包）", RESOURCE_ROOT, resource_ok,
        "critical" if not resource_ok else "ok",
    ))

    # 2. 基础工具 ffmpeg / ffprobe
    tools = tools_dir()
    ffmpeg_ok = os.path.isfile(os.path.join(tools, "ffmpeg.exe"))
    ffprobe_ok = os.path.isfile(os.path.join(tools, "ffprobe.exe"))
    items.append(_status("ffmpeg.exe", os.path.join(tools, "ffmpeg.exe"), ffmpeg_ok,
                         "critical" if not ffmpeg_ok else "ok"))
    items.append(_status("ffprobe.exe", os.path.join(tools, "ffprobe.exe"), ffprobe_ok,
                         "critical" if not ffprobe_ok else "ok"))

    models = models_dir()
    # 3. SenseVoiceSmall（智能字幕 / 音频克隆 ASR）
    sv_ok = os.path.isdir(os.path.join(models, "SenseVoiceSmall"))
    items.append(_status("SenseVoiceSmall 模型", os.path.join(models, "SenseVoiceSmall"),
                         sv_ok, "warning" if not sv_ok else "ok"))
    # 4. VoxCPM2（音频克隆）
    vc_ok = os.path.isdir(os.path.join(models, "VoxCPM2"))
    items.append(_status("VoxCPM2 模型", os.path.join(models, "VoxCPM2"),
                         vc_ok, "warning" if not vc_ok else "ok"))

    # 5. ComfyUI 内置引擎（封面生成）
    comfy_ok = os.path.isdir(os.path.join(RESOURCE_ROOT, "engines", "ComfyUI", "ComfyUI"))
    items.append(_status("ComfyUI 引擎", os.path.join(RESOURCE_ROOT, "engines", "ComfyUI", "ComfyUI"),
                         comfy_ok, "warning" if not comfy_ok else "ok"))
    # 6. ComfyUI Python（AI 子进程运行层，克隆/图生共用）
    comfy_py_ok = os.path.isfile(os.path.join(RESOURCE_ROOT, "engines", "ComfyUI", "python", "python.exe"))
    items.append(_status("ComfyUI Python 环境",
                         os.path.join(RESOURCE_ROOT, "engines", "ComfyUI", "python", "python.exe"),
                         comfy_py_ok, "warning" if not comfy_py_ok else "ok"))
    # 7. voice_clone 运行层
    vc_runtime_ok = os.path.isdir(os.path.join(RESOURCE_ROOT, "engines", "voice_clone"))
    items.append(_status("语音克隆运行层", os.path.join(RESOURCE_ROOT, "engines", "voice_clone"),
                         vc_runtime_ok, "warning" if not vc_runtime_ok else "ok"))

    return items


def write_check_report(items):
    """将自检结果写入 日志/environment_check.txt（追加）。"""
    try:
        os.makedirs(logs_dir(), exist_ok=True)
        report_path = os.path.join(logs_dir(), "environment_check.txt")
        lines = []
        for item in items:
            mark = "✅" if item["ok"] else ("❌" if item["level"] == "critical" else "⚠️")
            lines.append(f"{mark} {item['name']}: {'正常' if item['ok'] else '缺失/异常'}  {item['path']}")
        critical = [i for i in items if not i["ok"] and i["level"] == "critical"]
        summary = "全部正常" if not critical else f"{len(critical)} 项关键组件缺失"
        block = (
            "\n" + "=" * 60 + "\n"
            f"[环境自检 {__import__('time').strftime('%Y-%m-%d %H:%M:%S')}] {summary}\n"
            + "\n".join(lines)
            + "\n" + "=" * 60 + "\n"
        )
        with open(report_path, "a", encoding="utf-8") as f:
            f.write(block)
        return report_path
    except Exception:
        return ""


def get_critical_missing(items):
    """返回关键缺失项（critical 级别）。"""
    return [i for i in items if not i["ok"] and i["level"] == "critical"]
