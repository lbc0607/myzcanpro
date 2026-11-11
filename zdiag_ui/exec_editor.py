"""Executor editor page (embedded, not a dialog): the ECU刷新 step editor plus 'which reply is the result'."""
import copy
from typing import Callable, Optional

from zdiag.project import Project
from zdiag.scanlib import PARSE_MODES, Executor
from .common import info, warn
from .step_editor import StepEditorTab
from .qt import QtWidgets as W


class RunPanel(W.QWidget):
    """Log of a run + its result line + stop button."""

    def __init__(self) -> None:
        super().__init__()
        v = W.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        h = W.QHBoxLayout()
        self.l_result = W.QLabel("")
        self.b_stop = W.QPushButton("停止")
        self.b_stop.setEnabled(False)
        self.b_clear = W.QPushButton("清空日志")
        h.addWidget(W.QLabel("运行结果:"))
        h.addWidget(self.l_result, 1)
        h.addWidget(self.b_clear)
        h.addWidget(self.b_stop)
        v.addLayout(h)
        self.log = W.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        v.addWidget(self.log)
        self.b_clear.clicked.connect(self.log.clear)

    def start(self) -> None:
        self.log.clear()
        self.l_result.setText("运行中…")
        self.b_stop.setEnabled(True)

    def finish(self, text: str) -> None:
        self.l_result.setText(text)
        self.b_stop.setEnabled(False)

    def add(self, line: str) -> None:
        self.log.appendPlainText(line)


class _Host:
    """What StepEditorTab expects from its window."""

    def __init__(self, page: "ExecEditorPage") -> None:
        self.page = page
        self.project = Project()

    def mark_dirty(self) -> None:
        self.page.on_changed()

    def new_project(self) -> None: ...
    def open_project(self) -> None: ...
    def save_project_as(self) -> None: ...

    def save_project(self) -> None:
        self.page.save()

    def apply_to_executor(self) -> None:
        self.page.run()


class ExecEditorPage(W.QWidget):
    """save_fn(executor) -> bool and run_fn(executor) are supplied by the scan window."""

    def __init__(self, ctx, save_fn: Callable, run_fn: Callable, back_fn: Callable, name_editable: bool = True) -> None:
        super().__init__()
        self.ctx, self.save_fn, self.run_fn, self.back_fn = ctx, save_fn, run_fn, back_fn
        self.executor = Executor()
        self.dirty = False
        self.host = _Host(self)
        v = W.QVBoxLayout(self)
        top = W.QHBoxLayout()
        self.b_back = W.QPushButton("‹ 上一级")
        self.l_title = W.QLabel("")
        self.e_name = W.QLineEdit()
        self.e_name.setFixedWidth(220)
        top.addWidget(self.b_back)
        top.addWidget(self.l_title, 1)
        top.addWidget(W.QLabel("名称:"))
        top.addWidget(self.e_name)
        v.addLayout(top)
        self.editor = StepEditorTab(ctx, self.host)
        self.editor.embed("试运行")
        v.addWidget(self.editor, 1)
        box = W.QGroupBox("结果显示")
        h = W.QHBoxLayout(box)
        self.c_step = W.QComboBox()
        self.e_skip = W.QLineEdit()
        self.e_skip.setFixedWidth(60)
        self.e_skip.setPlaceholderText("自动")
        self.c_parse = W.QComboBox()
        self.c_parse.addItems([t for _, t in PARSE_MODES])
        for wd in (W.QLabel("取哪一步的回复:"), self.c_step, W.QLabel("跳过开头字节数:"), self.e_skip,
                   W.QLabel("解析方式:"), self.c_parse):
            h.addWidget(wd)
        h.addWidget(W.QLabel("(跳过留空=自动: 22 服务跳过 62+DID 共3字节, 其它服务跳过1字节)"), 1)
        v.addWidget(box)
        self.panel = RunPanel()
        v.addWidget(self.panel)
        self.b_back.clicked.connect(self.back_fn)
        self.e_name.editingFinished.connect(self.on_changed)
        self.e_name.setEnabled(name_editable)

    # ------------------------------------------------------------------ data
    def load(self, ex: Executor, title: str, name_editable: bool = True) -> None:
        self.executor = copy.deepcopy(ex)
        self.host.project = self.executor.project
        self.l_title.setText(title)
        self.e_name.setText(ex.name)
        self.e_name.setEnabled(name_editable)
        self.editor.refresh()
        self._fill_result()
        self.dirty = False

    def _fill_result(self) -> None:
        steps = self.executor.steps
        self.c_step.blockSignals(True)
        self.c_step.clear()
        self.c_step.addItem("最后一个有回复的服务", -1)
        for i, s in enumerate(steps):
            if s["type"] == "service":
                self.c_step.addItem(f"{i + 1}. {s.get('name', '')}", i)
        i = self.c_step.findData(int(self.executor.result.get("step", -1)))
        self.c_step.setCurrentIndex(i if i >= 0 else 0)
        self.c_step.blockSignals(False)
        sk = int(self.executor.result.get("skip", -1))
        self.e_skip.setText("" if sk < 0 else str(sk))
        modes = [k for k, _ in PARSE_MODES]
        pm = str(self.executor.result.get("parse", "hex"))
        self.c_parse.setCurrentIndex(modes.index(pm) if pm in modes else 0)

    def current(self) -> Optional[Executor]:
        """The executor as edited; None (with a message) when the result settings are invalid."""
        txt = self.e_skip.text().strip()
        try:
            skip = int(txt) if txt else -1
            if skip < -1:
                raise ValueError
        except ValueError:
            warn(self, "「跳过开头字节数」应为非负整数，或留空表示自动")
            return None
        step = self.c_step.currentData()
        self.executor.result = {"step": -1 if step is None else step, "skip": skip, "parse": PARSE_MODES[max(self.c_parse.currentIndex(), 0)][0]}
        self.executor.name = self.e_name.text().strip() or self.executor.name
        self.executor.project.name = self.executor.name
        return self.executor

    # ------------------------------------------------------------------ callbacks
    def on_changed(self) -> None:
        self.dirty = True
        cur = self.c_step.currentData()
        self._fill_result()
        i = self.c_step.findData(cur)
        if i >= 0:
            self.c_step.setCurrentIndex(i)

    def save(self) -> bool:
        ex = self.current()
        if ex is None:
            return False
        if not ex.steps:
            info(self, "还没有任何服务步骤，请先在左侧选择服务并「添加到列表」。")
            return False
        if self.save_fn(ex):
            self.dirty = False
            return True
        return False

    def run(self) -> None:
        ex = self.current()
        if ex is not None:
            self.run_fn(ex)
