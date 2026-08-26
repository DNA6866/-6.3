import os

from services.platform_service import open_path, reveal_file as reveal_in_explorer


def open_output_dir(path):
    os.makedirs(path, exist_ok=True)
    open_path(path)


def reveal_file(path):
    if os.path.exists(path):
        reveal_in_explorer(path)
