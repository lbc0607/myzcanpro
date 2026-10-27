"""Small Qt helpers shared by the windows."""
import os
import time
from typing import Callable, List, Optional

from zdiag.engine import Hooks, LogEvent
from .qt import QtCore, QtWidgets as W, Signal


class Bridge(QtCore.QObject):
    """Carries engine callbacks (worker thread) to the GUI thread via queued signals."""
    event = Signal(object)
    step = Signal(int, str, str)
    progress = Signal(int, int)

    def hooks(self) -> Hooks:
        h = Hooks()
        h.log = lambda e: self.event.emit(e)
        h.step = lambda i, s, d: self.step.emit(i, s, d)
        h.progress = lambda d, t: self.progress.emit(d, t)
        return h


class Worker(QtCore.QThread):
    result = Signal(object)
    error = Signal(str)

    def __init__(self, fn: Callable, parent=None) -> None:
        super().__init__(parent)
        self.fn = fn

    def run(self) -> None:
        try:
            self.result.emit(self.fn())
        except Exception as e:                                # surfaced in the GUI
            self.error.emit(f"{type(e).__name__}: {e}")


def info(parent, text: str, title: str = "提示") -> None:
    W.QMessageBox.information(parent, title, text)


def warn(parent, text: str, title: str = "错误") -> None:
    W.QMessageBox.warning(parent, title, text)


def hex_edit(text: str = "", width: int = 0):
    e = W.QLineEdit(text)
    if width:
        e.setFixedWidth(width)
    return e


def labeled(label: str, widget, stretch: bool = True):
    row = W.QHBoxLayout()
    row.addWidget(W.QLabel(label))
    row.addWidget(widget)
    return row


def pick_file(parent, ctx, title: str, filt: str = "所有文件 (*.*)") -> str:
    path, _ = W.QFileDialog.getOpenFileName(parent, title, ctx.last_dir, filt)
    if path:
        ctx.last_dir = os.path.dirname(path)
    return path


def save_file(parent, ctx, title: str, filt: str) -> str:
    path, _ = W.QFileDialog.getSaveFileName(parent, title, ctx.last_dir, filt)
    if path:
        ctx.last_dir = os.path.dirname(path)
    return path


def int_of(edit, name: str, lo: int = 0, hi: int = 1 << 31, base: int = 10) -> int:
    t = edit.text().strip()
    try:
        v = int(t, base)
    except ValueError:
        raise ValueError(f"{name} 无效: {t!r}")
    if not lo <= v <= hi:
        raise ValueError(f"{name} 超出范围 ({lo}~{hi}): {v}")
    return v


EVENT_KIND_TEXT = {"REQ": "请求", "RSP": "响应", "NRC": "否定响应", "INFO": "信息",
                   "ERR": "错误", "OK": "完成", "CAN": "CAN帧"}


def event_row(e: LogEvent, fmt: str = "hex") -> List[str]:
    t = fmt_time(e.ts)
    body = ""
    if e.data:
        body = " ".join(f"{b:02X}" for b in e.data) if fmt == "hex" else \
            "".join(chr(b) if 32 <= b < 127 else "." for b in e.data)
    desc = (body + "  " + e.text).strip() if e.text else body
    return [t, EVENT_KIND_TEXT.get(e.kind, e.kind), f"{e.can_id:X}" if e.can_id is not None else "",
            e.direction, str(len(e.data)) if e.data else "", desc]


def fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts * 1000) % 1000:03d}"
