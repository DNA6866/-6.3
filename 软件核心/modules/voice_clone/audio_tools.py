import os
import subprocess
import time


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
    subprocess.run(cmd, startupinfo=hidden_startupinfo(), check=True, capture_output=True)
    return output_path


def trim_audio(ffmpeg_path, input_path, output_path, start=0, duration=30):
    cmd = [
        ffmpeg_path, '-y', '-hide_banner', '-loglevel', 'error',
        '-ss', str(start), '-i', input_path, '-t', str(duration),
        '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', output_path
    ]
    subprocess.run(cmd, startupinfo=hidden_startupinfo(), check=True, capture_output=True)
    return output_path


def play_audio(path):
    if os.name == 'nt' and os.path.exists(path):
        os.startfile(os.path.abspath(path))


def open_output_dir(path):
    os.makedirs(path, exist_ok=True)
    if os.name == 'nt':
        os.startfile(os.path.abspath(path))
