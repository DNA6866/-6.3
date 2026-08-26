"""
模型文件迁移与路径重定向脚本。
将外部项目（如 F:\\TPFT）中的模型复制到当前软件目录（网盘资源包/models），
并更新配置文件中的路径引用，实现依赖隔离。
"""
import json
import os
import shutil
import sys

# ───── 配置 ─────
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET_MODELS = os.path.join(APP_ROOT, "网盘资源包", "models")

# 源路径：原项目中的模型位置
SOURCE_BASE = r"F:\TPFT"

# 模型迁移清单（源相对路径 → 目标相对路径）
MODEL_MAP = {
    # ─ Z-Image Turbo ─
    r"ComfyUI\models\diffusion_models":      r"ComfyUI\models\diffusion_models",
    r"ComfyUI\models\text_encoders":          r"ComfyUI\models\text_encoders",
    r"ComfyUI\models\vae":                    r"ComfyUI\models\vae",
    # ─ Qwen3-VL 图片反推 ─
    r"ComfyUI\models\LLM":                    r"LLM",
    # ─ 语音克隆 ─
    r"ComfyUI\models\VoxCPM2":                r"VoxCPM2",
    r"ComfyUI\models\SenseVoiceSmall":        r"SenseVoiceSmall",
}

# 配置文件更新映射
CONFIG_FILES = [
    "用户工作区/配置/voice_clone_config.json",
    "用户工作区/配置/config.json",
]


def log(msg):
    print(f"[迁移] {msg}")


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def copy_model(src_dir, dst_dir, pattern=None):
    """复制源目录下所有模型文件到目标目录。pattern 为 glob 过滤（如 *.safetensors）。"""
    if not os.path.isdir(src_dir):
        log(f"⚠️ 源目录不存在：{src_dir}")
        return 0
    count = 0
    for root, dirs, files in os.walk(src_dir):
        rel_path = os.path.relpath(root, src_dir)
        target_dir = os.path.join(dst_dir, rel_path) if rel_path != "." else dst_dir
        ensure_dir(target_dir)
        for f in files:
            if pattern and not any(f.endswith(ext) for ext in pattern):
                continue
            src = os.path.join(root, f)
            dst = os.path.join(target_dir, f)
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                continue  # 已存在且大小相同，跳过
            try:
                shutil.copy2(src, dst)
                size_mb = os.path.getsize(dst) / (1024 * 1024)
                log(f"  ✅ {rel_path}/{f} ({size_mb:.1f} MB)")
                count += 1
            except Exception as exc:
                log(f"  ❌ {f} 复制失败：{exc}")
    return count


def update_config_paths():
    """更新配置文件中硬编码的旧路径为新路径。"""
    old_base = SOURCE_BASE.replace("\\", "/")
    new_base = APP_ROOT.replace("\\", "/")
    changed = 0

    for cfg_rel in CONFIG_FILES:
        cfg_path = os.path.join(APP_ROOT, cfg_rel)
        if not os.path.exists(cfg_path):
            continue
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                text = f.read()
            # 检测是否包含旧路径引用
            if old_base in text:
                new_text = text.replace(old_base, new_base)
                with open(cfg_path, "w", encoding="utf-8") as f:
                    f.write(new_text)
                occurrences = text.count(old_base)
                log(f"  ✅ 更新 {cfg_rel}：替换了 {occurrences} 处路径引用")
                changed += 1
            else:
                log(f"  ⏭️ {cfg_rel}：无需更新（无旧路径引用）")
        except Exception as exc:
            log(f"  ❌ {cfg_rel} 更新失败：{exc}")

    return changed


def verify_migration():
    """验证所有目标目录下是否已有模型文件。"""
    log("=" * 50)
    log("验证迁移完整性...")
    all_ok = True
    for src_rel, dst_rel in MODEL_MAP.items():
        src = os.path.join(SOURCE_BASE, src_rel)
        dst = os.path.join(TARGET_MODELS, dst_rel)
        if not os.path.isdir(src):
            log(f"  ⚪ 跳过（源不存在）：{src_rel}")
            continue
        src_count = sum(1 for _ in _iter_model_files(src))
        dst_count = sum(1 for _ in _iter_model_files(dst))
        if dst_count == 0:
            log(f"  ❌ 目标目录为空：{dst_rel}")
            all_ok = False
        elif dst_count < src_count:
            log(f"  ⚠️ 目标文件少于源（{dst_count}/{src_count}）：{dst_rel}")
        else:
            log(f"  ✅ {dst_rel}：{dst_count} 个文件")

    # 检查配置文件
    for cfg_rel in CONFIG_FILES:
        cfg_path = os.path.join(APP_ROOT, cfg_rel)
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    text = f.read()
                if SOURCE_BASE.replace("\\", "/") in text:
                    log(f"  ❌ 配置文件仍引用旧路径：{cfg_rel}")
                    all_ok = False
                else:
                    log(f"  ✅ 配置文件路径已解绑：{cfg_rel}")
            except Exception:
                log(f"  ❌ 无法读取配置文件：{cfg_rel}")
                all_ok = False

    if all_ok:
        log("✅ 迁移验证通过！所有模型已就绪，配置文件已解绑。")
    else:
        log("⚠️ 存在未完成项，请检查上述 ❌ 标记。")
    return all_ok


def _iter_model_files(directory):
    """遍历目录下的模型文件（.safetensors, .gguf, .pt, .bin, .onnx, .json）。"""
    if not os.path.isdir(directory):
        return
    for root, _, files in os.walk(directory):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in (".safetensors", ".gguf", ".pt", ".pth", ".bin", ".onnx", ".json", ".txt", ".model"):
                yield os.path.join(root, f)


# ───── 主流程 ─────
def main():
    log("=" * 50)
    log("模型文件迁移工具")
    log(f"软件目录：{APP_ROOT}")
    log(f"源项目路径：{SOURCE_BASE}")
    log(f"目标模型目录：{TARGET_MODELS}")
    log("=" * 50)

    ensure_dir(TARGET_MODELS)
    total_copied = 0

    for src_rel, dst_rel in MODEL_MAP.items():
        src = os.path.join(SOURCE_BASE, src_rel)
        dst = os.path.join(TARGET_MODELS, dst_rel)
        log(f"\n处理：{src_rel} → {dst_rel}")
        count = copy_model(src, dst)
        total_copied += count
        if count > 0:
            log(f"  共复制 {count} 个文件。")

    log(f"\n总计复制 {total_copied} 个模型文件。")

    if total_copied > 0:
        log("\n更新配置文件引用...")
        update_config_paths()

    log("")
    verify_migration()

    log("\n完成！可以关闭此窗口。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("用户中断。")
    except Exception as exc:
        log(f"致命错误：{exc}")
        import traceback
        traceback.print_exc()
    input("\n按 Enter 退出...")
