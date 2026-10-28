"""ECU刷新 window: 编辑器 (editable step plan) + 执行器 (run it and watch the log)."""
import copy
import os
import threading
import time
from typing import Dict, List, Optional

from zdiag.engine import LogEvent, preflight
from zdiag.project import Project, fmt_hex, new_step, parse_id, sample_project, step_summary
from .appctx import ConnError
from .common import fill_channel_combo, Bridge, Worker, event_row, info, pick_file, save_file, warn
from .dialogs import CrcToolDialog, ServiceEditorDialog, SettingsDialog
from .flash_forms import make_form
from .logsaver import LogSaver
from .qt import Qt, QtGui, QtWidgets as W
from .style import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_RUNNING

PATH_KEYS = ("file", "dll", "source", "workdir")
FILTER = "刷新方案 (*.json);;所有文件 (*.*)"


def relativize(project: Project, base: str) -> dict:
    """Project as dict with file paths below `base` made relative (portable plans)."""
    d = copy.deepcopy(project.to_dict())
    for s in d["steps"]:
        for k in PATH_KEYS:
            p = s.get(k, "")
            if p and os.path.isabs(p):
                try:
                    rel = os.path.relpath(p, base)
                except ValueError:                        # other drive
                    continue
                if not rel.startswith(".."):
                    s[k] = rel
    return d


def card_text(i: int, step: dict) -> str:
    try:
        summ = step_summary(step)
    except ValueError:
        summ = "?"
    if step["type"] in ("service", "routine", "delay"):
        summ = f"数据[{summ} ]"
    return f"{i + 1}. {step.get('name', '')}\n{summ}"


# =============================================================================== 编辑器
class FlashEditorTab(W.QWidget):
    def __init__(self, ctx, win) -> None:
        super().__init__()
        self.ctx, self.win = ctx, win
        self.forms: List = []
        self._loading = False
        self._build()
        self.rebuild_forms()

    @property
    def project(self) -> Project:
        return self.win.project

    def _build(self) -> None:
        v = W.QVBoxLayout(self)
        bar = W.QFrame()
        bar.setObjectName("panel")
        h = W.QHBoxLayout(bar)
        self.b_new, self.b_open, self.b_save = W.QPushButton("新建"), W.QPushButton("打开"), W.QPushButton("保存")
        self.b_export, self.b_apply = W.QPushButton("导出"), W.QPushButton("应用到执行器")
        for b in (self.b_new, self.b_open, self.b_save, self.b_export, self.b_apply):
            h.addWidget(b)
        h.addStretch(1)
        self.e_phys, self.e_func, self.e_resp = W.QLineEdit(), W.QLineEdit(), W.QLineEdit()
        for e in (self.e_phys, self.e_func, self.e_resp):
            e.setFixedWidth(84)
        self.b_more, self.b_svc, self.b_crc = W.QPushButton("更多设置"), W.QPushButton("服务编辑器"), W.QPushButton("校验码工具")
        for wd in (W.QLabel("地址:"), self.e_phys, self.e_func, self.e_resp,
                   self.b_more, self.b_svc, self.b_crc):
            h.addWidget(wd)
        v.addWidget(bar)
        body = W.QHBoxLayout()
        v.addLayout(body, 1)
        self.lst = W.QListWidget()
        self.lst.setObjectName("svcList")
        body.addWidget(self.lst, 3)
        mid = W.QVBoxLayout()
        self.stack = W.QStackedWidget()
        mid.addWidget(self.stack, 1)
        mb = W.QHBoxLayout()
        self.b_add, self.b_upd = W.QPushButton("添加到列表"), W.QPushButton("更新选中步骤")
        mb.addStretch(1)
        mb.addWidget(self.b_upd)
        mb.addWidget(self.b_add)
        mid.addLayout(mb)
        body.addLayout(mid, 4)
        right = W.QVBoxLayout()
        rb = W.QHBoxLayout()
        self.b_up, self.b_down, self.b_del, self.b_clr = (W.QPushButton(t) for t in ("上移", "下移", "删除", "清空"))
        self.b_delay, self.e_delay = W.QPushButton("添加延时"), W.QLineEdit("1000")
        self.e_delay.setFixedWidth(70)
        for wd in (self.b_up, self.b_down, self.b_del, self.b_clr, self.b_delay, self.e_delay):
            rb.addWidget(wd)
        rb.addStretch(1)
        right.addLayout(rb)
        self.table = W.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["描述", "数据"])
        right.addWidget(self.table, 1)
        body.addLayout(right, 5)
        # wiring
        self.b_new.clicked.connect(self.win.new_project)
        self.b_open.clicked.connect(self.win.open_project)
        self.b_save.clicked.connect(self.win.save_project)
        self.b_export.clicked.connect(self.win.save_project_as)
        self.b_apply.clicked.connect(self.win.apply_to_executor)
        for e in (self.e_phys, self.e_func, self.e_resp):
            e.editingFinished.connect(self._commit_addr)
        self.b_more.clicked.connect(self._more)
        self.b_svc.clicked.connect(self._service_editor)
        self.b_crc.clicked.connect(lambda: CrcToolDialog(self.ctx, self).exec_())
        self.lst.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.b_add.clicked.connect(self._add)
        self.b_upd.clicked.connect(self._update)
        self.b_up.clicked.connect(lambda: self._move(-1))
        self.b_down.clicked.connect(lambda: self._move(1))
        self.b_del.clicked.connect(self._delete)
        self.b_clr.clicked.connect(self._clear)
        self.b_delay.clicked.connect(self._add_delay)
        self.table.currentCellChanged.connect(self._on_select)
        self.table.itemChanged.connect(self._on_item_changed)

    # -- forms
    def rebuild_forms(self) -> None:
        self.lst.clear()
        self.forms = []
        while self.stack.count():                           # drop the old pages
            w = self.stack.widget(0)
            self.stack.removeWidget(w)
            w.deleteLater()
        for s in self.ctx.services:
            self.lst.addItem(f"({s['sid']:02X}){s['name']}")
            f = make_form(self.ctx, s)
            self.forms.append((s, f))
            self.stack.addWidget(f)
        if self.forms:
            self.lst.setCurrentRow(0)

    def _form_index_for(self, step: dict) -> int:
        t = step["type"]
        want = {"unlock": 0x27, "driver": 0x34, "download": 0x34, "routine": 0x31}.get(t)
        if t == "service":
            try:
                want = int(step["pdu"].split()[0], 16)
            except (ValueError, IndexError):
                return -1
        for i, (s, _) in enumerate(self.forms):
            if s["sid"] == want:
                return i
        return -1

    # -- project -> widgets
    def refresh(self, select: int = -1) -> None:
        p = self.project
        self._loading = True
        a = p.cfg.addr
        self.e_phys.setText(f"{a.phys:x}")
        self.e_func.setText(f"{a.func:x}")
        self.e_resp.setText(f"{a.resp:x}")
        self.table.blockSignals(True)
        self.table.setRowCount(len(p.steps))
        for r, s in enumerate(p.steps):
            try:
                data = step_summary(s)
            except ValueError:
                data = s.get("pdu", "?")
            self.table.setItem(r, 0, W.QTableWidgetItem(s.get("name", "")))
            self.table.setItem(r, 1, W.QTableWidgetItem(data))
        self.table.blockSignals(False)
        self._loading = False
        if 0 <= select < len(p.steps):
            self.table.selectRow(select)

    def _commit_addr(self) -> None:
        if self._loading:
            return
        try:
            p, f, r = (parse_id(e.text()) for e in (self.e_phys, self.e_func, self.e_resp))
        except ValueError:
            warn(self, "CAN ID 必须是十六进制数")
            self.refresh()
            return
        a = self.project.cfg.addr
        if (p, f, r) != (a.phys, a.func, a.resp):
            a.phys, a.func, a.resp = p, f, r
            self.win.mark_dirty()

    def _more(self) -> None:
        if SettingsDialog(self.ctx, self.project.cfg, self.ctx.channel, self, with_channel=False).exec_():
            self.refresh()
            self.win.mark_dirty()

    def _service_editor(self) -> None:
        if ServiceEditorDialog(self.ctx, self).exec_():
            self.rebuild_forms()

    # -- step list operations
    def _current_form(self):
        i = self.stack.currentIndex()
        return self.forms[i][1] if 0 <= i < len(self.forms) else None

    def _build_step(self) -> Optional[dict]:
        f = self._current_form()
        if f is None:
            return None
        try:
            return f.to_step()
        except ValueError as e:
            warn(self, str(e))
            return None

    def _add(self) -> None:
        s = self._build_step()
        if s is not None:
            self.project.steps.append(s)
            self.win.mark_dirty()
            self.refresh(len(self.project.steps) - 1)

    def _update(self) -> None:
        r = self.table.currentRow()
        if not 0 <= r < len(self.project.steps):
            info(self, "请先在右侧列表中选择要更新的步骤。")
            return
        s = self._build_step()
        if s is not None:
            self.project.steps[r] = s
            self.win.mark_dirty()
            self.refresh(r)

    def _add_delay(self) -> None:
        try:
            ms = int(self.e_delay.text())
            if ms < 0:
                raise ValueError
        except ValueError:
            warn(self, "延时必须是非负整数(ms)")
            return
        self.project.steps.append(new_step("delay", ms=ms))
        self.win.mark_dirty()
        self.refresh(len(self.project.steps) - 1)

    def _delete(self) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self.project.steps):
            del self.project.steps[r]
            self.win.mark_dirty()
            self.refresh(min(r, len(self.project.steps) - 1))

    def _clear(self) -> None:
        self.project.steps = []
        self.win.mark_dirty()
        self.refresh()

    def _move(self, d: int) -> None:
        r, n = self.table.currentRow(), self.table.currentRow() + d
        st = self.project.steps
        if 0 <= r < len(st) and 0 <= n < len(st):
            st[r], st[n] = st[n], st[r]
            self.win.mark_dirty()
            self.refresh(n)

    def _on_select(self, r, *_) -> None:
        if self._loading or not 0 <= r < len(self.project.steps):
            return
        step = self.project.steps[r]
        i = self._form_index_for(step)
        if i < 0:
            return
        self.lst.setCurrentRow(i)
        try:
            self.forms[i][1].load_step(step)
        except (ValueError, KeyError) as e:
            warn(self, f"载入步骤参数失败: {e}")

    def _on_item_changed(self, item) -> None:
        if self._loading:
            return
        r, c = item.row(), item.column()
        if not 0 <= r < len(self.project.steps):
            return
        s = self.project.steps[r]
        if c == 0:
            s["name"] = item.text()
        elif s["type"] == "service":
            from zdiag.project import parse_hex
            try:
                s["pdu"] = fmt_hex(parse_hex(item.text()))
            except ValueError as e:
                warn(self, str(e))
        elif s["type"] == "delay":
            try:
                s["ms"] = int(item.text().replace("ms", "").strip())
            except ValueError:
                warn(self, "延时必须是整数(ms)")
        else:
            info(self, "该步骤的参数请在左侧表单中修改，然后点「更新选中步骤」。")
        self.win.mark_dirty()
        self.refresh(r)


# =============================================================================== 执行器
class FlashExecutorTab(W.QWidget):
    def __init__(self, ctx, win) -> None:
        super().__init__()
        self.ctx, self.win = ctx, win
        self.project = Project()
        self.path: Optional[str] = None
        self.bridge = Bridge()
        self.cancel = threading.Event()
        self.session = None
        self.worker: Optional[Worker] = None
        self.saver = LogSaver()
        self.events: List[LogEvent] = []
        self._build()
        self.bridge.event.connect(self._on_event)
        self.bridge.step.connect(self._on_step)
        self.bridge.progress.connect(self._on_progress)
        self.ctx.dev.add_listener(self.on_dev_change)

    def _build(self) -> None:
        v = W.QVBoxLayout(self)
        bar = W.QFrame()
        bar.setObjectName("panel")
        h = W.QHBoxLayout(bar)
        self.c_ch = W.QComboBox()                       # the channels started in the main window's 设备管理
        self.c_ch.setMinimumWidth(170)
        self.e_path = W.QLineEdit()
        self.e_path.setReadOnly(True)
        self.b_open, self.b_more = W.QPushButton("打开文件"), W.QPushButton("更多设置")
        self.e_phys, self.e_func, self.e_resp = W.QLineEdit(), W.QLineEdit(), W.QLineEdit()
        for e in (self.e_phys, self.e_func, self.e_resp):
            e.setFixedWidth(84)
        h.addWidget(W.QLabel("通道:"))
        h.addWidget(self.c_ch)
        h.addWidget(self.e_path, 1)
        h.addWidget(self.b_open)
        for wd in (W.QLabel("地址:"), self.e_phys, self.e_func, self.e_resp, self.b_more):
            h.addWidget(wd)
        v.addWidget(bar)
        body = W.QHBoxLayout()
        v.addLayout(body, 1)
        self.cards = W.QListWidget()
        self.cards.setObjectName("stepList")
        body.addWidget(self.cards, 3)
        mid = W.QVBoxLayout()
        self.l_cur = W.QLabel("就绪")
        self.bar = W.QProgressBar()
        self.bar.setMaximum(100)
        self.detail = W.QPlainTextEdit()
        self.detail.setReadOnly(True)
        mid.addWidget(self.l_cur)
        mid.addWidget(self.bar)
        mid.addWidget(self.detail, 1)
        body.addLayout(mid, 3)
        right = W.QVBoxLayout()
        rt = W.QFrame()
        rt.setObjectName("panel")
        rh = W.QHBoxLayout(rt)
        self.s_n, self.s_gap = W.QSpinBox(), W.QSpinBox()
        self.s_n.setRange(1, 100000)
        self.s_n.setValue(1)
        self.s_gap.setRange(0, 86400)
        self.s_gap.setSuffix(" s")
        self.b_start = W.QPushButton("开始刷写")
        for wd in (W.QLabel("刷写次数:"), self.s_n, W.QLabel("间隔时间:"), self.s_gap, self.b_start):
            rh.addWidget(wd)
        rh.addStretch(1)
        right.addWidget(rt)
        self.table = W.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["时间", "类型", "帧ID", "方向", "长度", "数据/描述"])
        right.addWidget(self.table, 1)
        rb = W.QFrame()
        rb.setObjectName("panel")
        bh = W.QHBoxLayout(rb)
        self.b_clear, self.b_save, self.b_export = W.QPushButton("清空"), W.QPushButton("实时保存"), W.QPushButton("导出")
        self.b_save.setCheckable(True)
        self.k_scroll = W.QCheckBox("滚动显示")
        self.k_scroll.setChecked(True)
        for wd in (self.b_clear, self.b_save, self.b_export, self.k_scroll):
            bh.addWidget(wd)
        bh.addStretch(1)
        right.addWidget(rb)
        body.addLayout(right, 6)
        self.b_open.clicked.connect(self._open)
        self.b_more.clicked.connect(self._more)
        self.b_start.clicked.connect(self._start_stop)
        self.b_clear.clicked.connect(self._clear)
        self.b_save.toggled.connect(self._toggle_save)
        self.b_export.clicked.connect(self._export)
        self.c_ch.currentIndexChanged.connect(self._on_channel)
        for e in (self.e_phys, self.e_func, self.e_resp):
            e.editingFinished.connect(self._commit_addr)

    # -- project in executor
    @property
    def base_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.path)) if self.path else self.ctx.dir

    def load(self, project: Project, path: Optional[str]) -> None:
        self.project, self.path = copy.deepcopy(project), path
        self.e_path.setText(path or "(未保存的方案)")
        self._sync()
        self.cards.clear()
        for i, s in enumerate(self.project.steps):
            it = W.QListWidgetItem(card_text(i, s))
            self._paint(it, COLOR_IDLE)
            self.cards.addItem(it)
        self.l_cur.setText("就绪")
        self.bar.setValue(0)
        self.detail.clear()

    def _sync(self) -> None:
        a = self.project.cfg.addr
        self.e_phys.setText(f"{a.phys:x}")
        self.e_func.setText(f"{a.func:x}")
        self.e_resp.setText(f"{a.resp:x}")
        fill_channel_combo(self.c_ch, self.ctx.dev, self.ctx.channel.channel)

    @staticmethod
    def _paint(item, color: str) -> None:
        item.setBackground(QtGui.QBrush(QtGui.QColor(color)))
        item.setForeground(QtGui.QBrush(QtGui.QColor("white")))

    def _open(self) -> None:
        p = pick_file(self, self.ctx, "打开刷新方案", FILTER)
        if p:
            try:
                self.load(Project.load(p), p)
            except (OSError, ValueError, KeyError) as e:
                warn(self, f"打开失败: {e}")

    def _on_channel(self, *_) -> None:
        n = self.c_ch.currentData()
        if n is not None and n >= 0 and n != self.ctx.channel.channel:
            self.ctx.channel.channel = n
            self._drop_session()
            self.ctx.save()

    def on_dev_change(self) -> None:
        n = fill_channel_combo(self.c_ch, self.ctx.dev, self.ctx.channel.channel)
        if n >= 0 and n != self.ctx.channel.channel:
            self.ctx.channel.channel = n

    def _commit_addr(self) -> None:
        try:
            p, f, r = (parse_id(e.text()) for e in (self.e_phys, self.e_func, self.e_resp))
        except ValueError:
            warn(self, "CAN ID 必须是十六进制数")
            self._sync()
            return
        a = self.project.cfg.addr
        a.phys, a.func, a.resp = p, f, r
        self._drop_session()

    def _more(self) -> None:
        if SettingsDialog(self.ctx, self.project.cfg, self.ctx.channel, self).exec_():
            self._drop_session()
            self._sync()
            self.ctx.save()

    # -- run control
    def _drop_session(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
        self.ctx.dev.release(self)

    def running(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _start_stop(self) -> None:
        if self.running():
            self.cancel.set()
            return
        steps = self.project.steps
        if not steps:
            info(self, "刷新方案里没有步骤，请先在「编辑器」里添加并点「应用到执行器」。")
            return
        problems = preflight(steps, self.base_dir)
        if problems:
            warn(self, "刷写前检查发现问题:\n\n" + "\n".join(problems), "无法开始")
            return
        self._drop_session()
        try:
            self.session = self.ctx.open_session(self.project.cfg, self.ctx.channel, self,
                                                 self.bridge.hooks(), self.base_dir)
        except ConnError as e:
            warn(self, str(e))
            return
        self.cancel.clear()
        self.session.cancel = self.cancel
        n, gap, sess = self.s_n.value(), self.s_gap.value(), self.session
        for i in range(self.cards.count()):
            self._paint(self.cards.item(i), COLOR_IDLE)
        self.detail.clear()
        self.bar.setValue(0)
        w = Worker(lambda: sess.run_plan(steps, n, float(gap)), self)
        w.result.connect(self._on_done)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_finished)
        self.worker = w
        self.b_start.setText("停止")
        w.start()

    def _on_done(self, ok) -> None:
        if ok:
            self.bar.setValue(100)
        self.l_cur.setText("刷写完成" if ok else "刷写未完成/失败，详见日志")
        (info if ok else warn)(self, "刷写完成。" if ok else "刷写失败或已停止，请查看右侧日志。",
                               "完成" if ok else "失败")

    def _on_error(self, msg: str) -> None:
        warn(self, msg)

    def _on_finished(self) -> None:
        self.b_start.setText("开始刷写")
        self.ctx.dev.release(self)

    # -- feedback
    def _on_step(self, i: int, status: str, detail: str) -> None:
        if not 0 <= i < self.cards.count():
            return
        color = {"running": COLOR_RUNNING, "ok": COLOR_OK, "fail": COLOR_FAIL}.get(status, COLOR_IDLE)
        self._paint(self.cards.item(i), color)
        step = self.project.steps[i]
        self.l_cur.setText(f"{i + 1}. {step.get('name', '')}  {'执行中…' if status == 'running' else detail}")
        if detail:
            self.detail.appendPlainText(f"{i + 1}. {step.get('name', '')}: {detail}")

    def _on_progress(self, done: int, total: int) -> None:
        self.bar.setValue(int(done * 100 / total) if total else 0)

    def _on_event(self, e: LogEvent) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, t in enumerate(event_row(e)):
            it = W.QTableWidgetItem(t)
            if e.kind in ("ERR", "NRC"):
                it.setForeground(QtGui.QBrush(QtGui.QColor("#C0392B")))
            self.table.setItem(r, c, it)
        limit = self.project.cfg.settings.max_log_lines
        if self.table.rowCount() > limit:
            for _ in range(min(1000, self.table.rowCount() - limit + 1000)):
                self.table.removeRow(0)
        if self.k_scroll.isChecked():
            self.table.scrollToBottom()
        self.events.append(e)
        if len(self.events) > limit + 1000:
            del self.events[:len(self.events) - limit]
        self.saver.write(e.line())

    def _clear(self) -> None:
        self.table.setRowCount(0)
        self.events = []

    def _toggle_save(self, on: bool) -> None:
        if on:
            p = self.saver.start(LogSaver.default_name(self.ctx.dir, "flash"),
                                 self.project.cfg.settings.save_max_lines)
            info(self, f"日志实时保存到:\n{p}")
        else:
            self.saver.stop()

    def _export(self) -> None:
        path = save_file(self, self.ctx, "导出日志", "文本 (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(e.line() for e in self.events))

    def shutdown(self) -> None:
        self.ctx.dev.remove_listener(self.on_dev_change)
        self.cancel.set()
        self._drop_session()
        self.saver.stop()


# =============================================================================== 主窗口
class FlashWindow(W.QMainWindow):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.project = sample_project("new")
        self.path: Optional[str] = None
        self.dirty = False
        self.setWindowTitle("ECU刷新")
        self.resize(1260, 720)
        self.tabs = W.QTabWidget()
        self.setCentralWidget(self.tabs)
        self.editor = FlashEditorTab(ctx, self)
        self.executor = FlashExecutorTab(ctx, self)
        self.tabs.addTab(self.editor, "")
        self.tabs.addTab(self.executor, "执行器")
        self.editor.refresh()
        self._retitle()

    # -- bookkeeping
    def _retitle(self) -> None:
        name = os.path.basename(self.path) if self.path else self.project.name
        self.tabs.setTabText(0, f"编辑器 - {name}{' *' if self.dirty else ''}")

    def mark_dirty(self) -> None:
        self.dirty = True
        self._retitle()

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        r = W.QMessageBox.question(self, "未保存的修改", "当前方案有未保存的修改，继续将丢弃它们。是否继续?")
        return r == W.QMessageBox.Yes

    # -- file ops
    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.project, self.path, self.dirty = Project(name="新建方案"), None, False
        self.project.cfg.addr = copy.deepcopy(self.ctx.diag_cfg.addr)
        self.editor.refresh()
        self._retitle()

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        p = pick_file(self, self.ctx, "打开刷新方案", FILTER)
        if not p:
            return
        try:
            self.project = Project.load(p)
        except (OSError, ValueError, KeyError) as e:
            warn(self, f"打开失败: {e}")
            return
        self.path, self.dirty = p, False
        self.editor.refresh()
        self._retitle()

    def _write(self, path: str) -> bool:
        import json
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(relativize(self.project, os.path.dirname(os.path.abspath(path))), f,
                          ensure_ascii=False, indent=2)
        except OSError as e:
            warn(self, f"保存失败: {e}")
            return False
        return True

    def save_project(self) -> None:
        if not self.path:
            self.save_project_as()
            return
        if self._write(self.path):
            self.dirty = False
            self._retitle()

    def save_project_as(self) -> None:
        p = save_file(self, self.ctx, "保存刷新方案", FILTER)
        if not p:
            return
        if not os.path.splitext(p)[1]:
            p += ".json"
        if self._write(p):
            self.path, self.dirty = p, False
            self.project.name = os.path.splitext(os.path.basename(p))[0]
            self._retitle()

    def apply_to_executor(self) -> None:
        self.executor.load(self.project, self.path)
        self.tabs.setCurrentIndex(1)

    def closeEvent(self, event) -> None:
        if self.executor.running():
            info(self, "正在刷写，请先停止后再关闭窗口。")
            event.ignore()
            return
        if not self._confirm_discard():
            event.ignore()
            return
        self.executor.shutdown()
        self.ctx.save()
        event.accept()
