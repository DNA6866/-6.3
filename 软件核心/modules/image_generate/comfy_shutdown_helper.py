import os
import sys
import time

from comfy_manager import ComfyUIManager


def _read_timestamp(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def main():
    if len(sys.argv) < 6:
        return 1
    stamp_path = sys.argv[1]
    comfyui_path = sys.argv[2]
    host = sys.argv[3]
    port = int(sys.argv[4])
    delay = int(float(sys.argv[5]))

    time.sleep(max(1, delay))
    if not os.path.exists(stamp_path):
        return 0
    last_shutdown = _read_timestamp(stamp_path)
    if last_shutdown <= 0 or time.time() - last_shutdown < max(1, delay - 5):
        return 0

    manager = ComfyUIManager(comfyui_path, host, port)
    if manager.is_running():
        manager.stop()
    try:
        os.remove(stamp_path)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
