import os
import shutil
import subprocess
from array import array


def hidden_startupinfo():
    if os.name != 'nt':
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def import_files_to_folder(files, folder_path, allowed_exts=None):
    os.makedirs(folder_path, exist_ok=True)
    imported = 0
    skipped = 0
    allowed_exts = allowed_exts or []

    for src in files:
        if not os.path.isfile(src):
            skipped += 1
            continue
        if allowed_exts and not any(src.lower().endswith(ext) for ext in allowed_exts):
            skipped += 1
            continue

        try:
            root = os.path.abspath(folder_path)
            filename = os.path.basename(src)
            dst = os.path.join(root, filename)
            if os.path.exists(dst) and os.path.samefile(src, dst):
                skipped += 1
                continue
            stem, ext = os.path.splitext(filename)
            number = 1
            while True:
                try:
                    # 独占创建，不能覆盖不同子目录导入的同名素材。
                    with open(dst, 'xb') as target:
                        try:
                            with open(src, 'rb') as source:
                                shutil.copyfileobj(source, target)
                        except Exception:
                            target.close()
                            os.remove(dst)
                            raise
                    break
                except FileExistsError:
                    number += 1
                    dst = os.path.join(root, f"{stem}_{number}{ext}")
            try:
                shutil.copystat(src, dst)
            except OSError:
                # 文件内容已完整导入，部分网盘不支持复制时间戳，不影响导入结果。
                pass
            imported += 1
        except Exception:
            skipped += 1

    return imported, skipped


def extract_reference_frame(ffmpeg_path, source, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    startupinfo = hidden_startupinfo()
    cmd = [
        ffmpeg_path, '-y', '-hide_banner', '-loglevel', 'error',
        '-ss', '1', '-i', source, '-frames:v', '1', '-q:v', '2', output_path
    ]
    res = subprocess.run(cmd, startupinfo=startupinfo, capture_output=True)
    if res.returncode != 0 or not os.path.exists(output_path):
        fallback_cmd = [
            ffmpeg_path, '-y', '-hide_banner', '-loglevel', 'error',
            '-i', source, '-frames:v', '1', '-q:v', '2', output_path
        ]
        subprocess.run(fallback_cmd, startupinfo=startupinfo, capture_output=True, check=True)
    return output_path if os.path.exists(output_path) else None


def build_waveform_peaks(ffmpeg_path, source, points=900):
    cmd = [
        ffmpeg_path, '-hide_banner', '-loglevel', 'error',
        '-i', source, '-vn', '-ac', '1', '-ar', '8000',
        '-f', 's16le', 'pipe:1'
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=hidden_startupinfo())
    if res.returncode != 0 or not res.stdout:
        return []

    samples = array('h')
    samples.frombytes(res.stdout)
    if not samples:
        return []

    chunk = max(1, len(samples) // max(1, points))
    peaks = []
    max_amp = 1
    for i in range(0, len(samples), chunk):
        block = samples[i:i + chunk]
        peak = max(abs(v) for v in block) if block else 0
        peaks.append(peak)
        if peak > max_amp:
            max_amp = peak
    return [p / max_amp for p in peaks]
