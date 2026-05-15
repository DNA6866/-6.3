import sys
import os
from paths import ensure_core_path
from PyQt5.QtWidgets import QApplication

ensure_core_path()
from ui import VideoMixerApp


def app_root():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


if __name__ == '__main__':
    os.chdir(app_root())
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    ex = VideoMixerApp()
    ex.show()
    sys.exit(app.exec_())
