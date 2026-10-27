"""Small start window with the three tools (ZCANPro: 高级功能)."""
import os

from .appctx import AppContext
from .qt import QtWidgets as W


class LauncherWindow(W.QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.windows = {}
        self.setWindowTitle("myzcanpro - 高级功能")
        self.resize(420, 300)
        root = W.QWidget()
        self.setCentralWidget(root)
        v = W.QVBoxLayout(root)
        title = W.QLabel("myzcanpro 诊断工具")
        v.addWidget(title)
        self.b_uds, self.b_flash, self.b_crc = W.QPushButton("UDS诊断"), W.QPushButton("ECU刷新"), W.QPushButton("校验码计算工具")
        for b in (self.b_uds, self.b_flash, self.b_crc):
            b.setMinimumHeight(40)
            v.addWidget(b)
        self.l_info = W.QLabel("")
        v.addWidget(self.l_info)
        v.addStretch(1)
        self.b_uds.clicked.connect(lambda: self._open("uds"))
        self.b_flash.clicked.connect(lambda: self._open("flash"))
        self.b_crc.clicked.connect(lambda: self._open("crc"))
        self._status()

    def _status(self) -> None:
        dll = os.path.join(self.ctx.dir, "zlgcan.dll")
        kd = os.path.join(self.ctx.dir, "kerneldlls")
        parts = [f"程序目录: {self.ctx.dir}",
                 f"zlgcan.dll: {'已找到' if os.path.isfile(dll) else '未找到 (真机需要; 可先用「模拟ECU」)'}",
                 f"kerneldlls: {'已找到' if os.path.isdir(kd) else '未找到 (真机需要)'}"]
        self.l_info.setText("\n".join(parts))

    def _open(self, which: str) -> None:
        win = self.windows.get(which)
        if win is None or not win.isVisible():
            if which == "uds":
                from .diag_window import DiagWindow
                win = DiagWindow(self.ctx)
            elif which == "flash":
                from .flash_window import FlashWindow
                win = FlashWindow(self.ctx)
            else:
                from .dialogs import CrcToolDialog
                win = CrcToolDialog(self.ctx)
            self.windows[which] = win
        win.show()
        win.raise_()
        win.activateWindow()

    def closeEvent(self, event) -> None:
        for w in list(self.windows.values()):
            if w.isVisible():
                w.close()
                if w.isVisible():                      # the window vetoed closing (e.g. flashing)
                    event.ignore()
                    return
        self.ctx.conn.close()
        self.ctx.save()
        event.accept()
