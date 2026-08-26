import os
import subprocess
import time

from services.platform_service import open_path


def hidden_startupinfo():
    if os.name != 'nt':
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


def audio_exists(path):
    return bool(path) and os.path.isfile(path)


def make_output_path(output_dir, prefix="voice", ext="wav"):
    os.makedirs(output_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(output_dir, f"{prefix}_{stamp}.{ext}")


def convert_to_wav(ffmpeg_path, input_path, output_path, sample_rate=16000, mono=True):
    channels = ['-ac', '1'] if mono else []
    cmd = [
        ffmpeg_path, '-y', '-hide_banner', '-loglevel', 'error',
        '-i', input_path, *channels, '-ar', str(sample_rate),
        '-c:a', 'pcm_s16le', output_path
    ]
    subprocess.run(
        cmd,
        startupinfo=hidden_startupinfo(),
        check=True,
        capture_output=True,
        timeout=300,
    )
    return output_path


def get_media_duration(ffprobe_path, input_path):
    try:
        res = subprocess.run(
            [
                ffprobe_path, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                input_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=hidden_startupinfo(),
            timeout=15,
        )
        return max(0.0, float((res.stdout or "0").strip()))
    except Exception:
        return 0.0


def trim_audio(ffmpeg_path, input_path, output_path, start=0, duration=30):
    cmd = [
        ffmpeg_path, '-y', '-hide_banner', '-loglevel', 'error',
        '-ss', str(start), '-i', input_path, '-t', str(duration),
        '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', output_path
    ]
    subprocess.run(
        cmd,
        startupinfo=hidden_startupinfo(),
        check=True,
        capture_output=True,
        timeout=300,
    )
    return output_path


def extract_audio_from_media(ffmpeg_path, input_path, output_path, start=0, duration=0, sample_rate=16000):
    cmd = [ffmpeg_path, '-y', '-hide_banner', '-loglevel', 'error']
    if float(start or 0) > 0:
        cmd.extend(['-ss', str(start)])
    cmd.extend(['-i', input_path])
    if float(duration or 0) > 0:
        cmd.extend(['-t', str(duration)])
    cmd.extend(['-vn', '-ac', '1', '-ar', str(sample_rate), '-c:a', 'pcm_s16le', output_path])
    subprocess.run(
        cmd,
        startupinfo=hidden_startupinfo(),
        check=True,
        capture_output=True,
        timeout=300,
    )
    return output_path


def normalize_audio_loudness(ffmpeg_path, input_path, output_path, sample_rate=16000):
    cmd = [
        ffmpeg_path, '-y', '-hide_banner', '-loglevel', 'error',
        '-i', input_path,
        '-af', 'loudnorm=I=-18:TP=-1.5:LRA=11',
        '-ac', '1', '-ar', str(sample_rate), '-c:a', 'pcm_s16le', output_path
    ]
    subprocess.run(
        cmd,
        startupinfo=hidden_startupinfo(),
        check=True,
        capture_output=True,
        timeout=300,
    )
    return output_path


def play_audio(path):
    if os.path.exists(path):
        open_path(path)


def open_output_dir(path):
    os.makedirs(path, exist_ok=True)
    open_path(path)
