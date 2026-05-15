import os


def open_output_dir(path):
    os.makedirs(path, exist_ok=True)
    if os.name == "nt":
        os.startfile(os.path.abspath(path))


def reveal_file(path):
    if os.name == "nt" and os.path.exists(path):
        os.startfile(os.path.dirname(os.path.abspath(path)))

