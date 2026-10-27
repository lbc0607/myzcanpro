"""myzcanpro entry point (double-click run.bat, or run the packaged exe)."""
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def _crash(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(os.path.join(HERE, "error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n{text}")
    except OSError:
        pass
    try:
        from zdiag_ui.qt import QtWidgets
        QtWidgets.QMessageBox.critical(None, "程序错误", f"发生未处理的错误，已写入 error.log:\n\n{text[-1500:]}")
    except Exception:
        pass


def main() -> int:
    sys.excepthook = _crash
    from zdiag_ui.qt import Qt, QtWidgets
    from zdiag_ui.style import QSS
    try:
        QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    except Exception:
        pass
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(QSS)
    from zdiag_ui.appctx import AppContext
    from zdiag_ui.launcher import LauncherWindow
    win = LauncherWindow(AppContext())
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
