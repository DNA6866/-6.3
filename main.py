"""
牛爷爷电商视频工具箱 — 主入口。
支持命令行启动和双击运行，崩溃时自动记录日志并保持控制台可见。
"""
import os
import sys
import time
import traceback


# ───── 最先设置：确保崩溃日志目录存在 ─────
def _app_root():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_root()
LOG_FILE = os.path.join(APP_DIR, "用户工作区", "配置", "crash_log.txt") if os.path.isdir(
    os.path.join(APP_DIR, "用户工作区")) else os.path.join(APP_DIR, "crash_log.txt")


def _prepare_qt_high_dpi_environment():
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    _apply_remote_4k_scale_guard()


def _apply_remote_4k_scale_guard():
    if os.environ.get("QT_SCALE_FACTOR"):
        return
    if os.name != "nt":
        return
    try:
        from paths import ensure_core_path

        ensure_core_path()
        from services.platform_service import get_primary_screen_metrics

        metrics = get_primary_screen_metrics()
    except Exception:
        metrics = None
    if not metrics:
        return
    width, height, dpi = metrics
    if width >= 3000 and height >= 1700 and dpi <= 120:
        os.environ["QT_SCALE_FACTOR"] = "1.5"


def _enable_qt_high_dpi(QApplication, Qt):
    try:
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    except Exception:
        pass
    try:
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    try:
        policy_enum = getattr(Qt, "HighDpiScaleFactorRoundingPolicy", None)
        policy = getattr(policy_enum, "PassThrough", None) if policy_enum else None
        if policy is not None and hasattr(QApplication, "setHighDpiScaleFactorRoundingPolicy"):
            QApplication.setHighDpiScaleFactorRoundingPolicy(policy)
    except Exception:
        pass


def _write_crash_log(exc_type, exc_value, exc_tb, preamble=""):
    """将崩溃信息写入文件，同时打印到 stderr。"""
    lines = [
        "─" * 60,
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Python：{sys.version}",
        f"目录：{APP_DIR}",
        preamble.strip(),
        "",
    ]
    if exc_tb:
        lines.extend(traceback.format_exception(exc_type, exc_value, exc_tb))
    else:
        lines.append(str(exc_value) if exc_value else "未知错误")

    text = "\n".join(lines)
    # 写入崩溃日志文件
    try:
        log_dir = os.path.dirname(LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass
    # 打印到控制台
    print(text, file=sys.stderr, flush=True)
    sys.stderr.flush()


def main():
    _prepare_qt_high_dpi_environment()

    # ── 解析路径前置 ──
    try:
        from paths import ensure_core_path
        ensure_core_path()
    except Exception as exc:
        _write_crash_log(type(exc), exc, exc.__traceback__,
                          "路径初始化失败。请确认软件目录结构完整，未缺少 paths.py。")
        input("\n按回车键退出...")
        sys.exit(1)

    # ── 导入 PyQt ──
    try:
        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QApplication, QMessageBox
        _enable_qt_high_dpi(QApplication, Qt)
    except ImportError as exc:
        _write_crash_log(type(exc), exc, exc.__traceback__,
                          "PyQt5 未安装。请执行：pip install PyQt5")
        input("\n按回车键退出...")
        sys.exit(1)

    # ── 导入主界面 ──
    try:
        from ui import VideoMixerApp
    except Exception as exc:
        _write_crash_log(type(exc), exc, exc.__traceback__,
                          "导入 ui 模块失败。最常见原因：\n"
                          "  1. 依赖包缺失 → 查看上方 import 错误，pip install 对应包\n"
                          "  2. Python 中文路径兼容性 → 尝试移动软件到纯英文路径\n"
                          "  3. 模块文件损坏 → 重新解压软件")
        input("\n按回车键退出...")
        sys.exit(1)

    # ── 启动应用 ──
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # ── 启动环境自检（只检查文件存在，写统一日志；关键缺失弹窗提示）──
    try:
        from services.environment_check import (
            get_critical_missing,
            run_environment_check,
            write_check_report,
        )
        _env_items = run_environment_check()
        write_check_report(_env_items)
        _critical = get_critical_missing(_env_items)
        if _critical:
            _missing = "\n".join(f"  • {i['name']}" for i in _critical)
            QMessageBox.warning(
                None, "环境组件缺失",
                f"检测到以下关键组件缺失，可能导致软件无法正常使用：\n\n{_missing}\n\n"
                f"请确认已安装完整主包（网盘资源包），并将软件放置到正确位置。\n"
                f"详细检测结果：{os.path.join(APP_DIR, '用户工作区', '日志', 'environment_check.txt')}"
            )
    except Exception:
        pass  # 自检失败不影响启动

    try:
        ex = VideoMixerApp()
    except Exception as exc:
        _write_crash_log(type(exc), exc, exc.__traceback__,
                          "VideoMixerApp 初始化崩溃。常见原因：\n"
                          "  - QSS 样式表语法错误\n"
                          "  - 缺少模型文件或配置文件损坏\n"
                          "  - PyQt5 版本不兼容")
        # GUI 可能已经部分初始化，尝试弹出消息框
        try:
            QMessageBox.critical(
                None, "启动失败",
                f"软件初始化时发生错误。\n\n"
                f"错误详情已写入：{LOG_FILE}\n\n"
                f"错误信息：{exc}"
            )
        except Exception:
            pass
        input("\n按回车键退出...")
        sys.exit(1)

    # ── 设置全局异常捕获（运行时崩溃兜底）────
    def _runtime_crash_handler(exc_type, exc_value, exc_tb):
        _write_crash_log(exc_type, exc_value, exc_tb, "运行时异常捕获：")
        # 尝试弹框
        try:
            QMessageBox.critical(
                None, "软件发生错误",
                f"软件遇到未预期的错误，即将关闭。\n\n"
                f"错误：{exc_value}\n\n"
                f"详细日志：{LOG_FILE}"
            )
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _runtime_crash_handler

    # ── 显示窗口并进入事件循环 ──
    ex.show()

    def _log_qt_quit():
        _write_crash_log(None, "Qt 应用即将退出（aboutToQuit）。", None, "Qt 退出事件：")

    def _log_last_window_closed():
        _write_crash_log(None, "最后一个窗口已关闭（lastWindowClosed）。", None, "Qt 窗口关闭事件：")

    app.lastWindowClosed.connect(_log_last_window_closed)
    app.aboutToQuit.connect(_log_qt_quit)
    sys.exit(app.exec_())


if __name__ == '__main__':
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:
        pass
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        _write_crash_log(type(exc), exc, exc.__traceback__,
                          "main() 最外层异常捕获（通常为内存错误或系统级故障）：")
        input("\n按回车键退出...")
        sys.exit(1)
