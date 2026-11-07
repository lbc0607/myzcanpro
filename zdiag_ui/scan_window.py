"""软件扫描: module list (scan the software of every core) -> per-module function page -> executor editor.

Pages (a QStackedWidget, no pop-ups): 0 module list, 1 module functions (folder tree), 2 executor editor."""
import os
import threading
from typing import Dict, List, Optional

from zdiag.devmgr import ConnError
from zdiag.engine import Hooks
from zdiag.project import parse_id
from zdiag.scanlib import (INFO_FIELDS, FIELD_LABEL, Entry, Executor, Module, ScanError, ScanStore, cfg_for,
                           run_executor, scan_module)
from .common import Bridge, Worker, fill_channel_combo, info, warn
from .exec_editor import ExecEditorPage, RunPanel
from .qt import Qt, QtCore, QtWidgets as W, Signal

LIST, MODULE, EDITOR = 0, 1, 2
COLS = ["", "序号", "软件", "软件编码", "软件版本", "硬件版本", "故障码数量", ""]


class _Sig(QtCore.QObject):
    field = Signal(str, str, str)          # module folder, key, text
    state = Signal(str, str)               # module folder, "扫描中" | ""
    outcome = Signal(object)


class ModuleAddrDialog(W.QDialog):
    """Edit the addresses of a software (opened by clicking its name)."""

    def __init__(self, m: Module, parent=None) -> None:
        super().__init__(parent)
        self.m, self.result_vals = m, None
        self.setWindowTitle(f"{m.name} - 地址")
        g = W.QGridLayout(self)
        self.e_phys, self.e_resp, self.e_func = W.QLineEdit(f"{m.phys:X}"), W.QLineEdit(f"{m.resp_id:X}"), W.QLineEdit(f"{m.func:X}")
        self._auto = m.resp_id == m.phys + 8
        for r, (t, e) in enumerate((("物理地址(0x):", self.e_phys), ("响应地址(0x):", self.e_resp), ("功能地址(0x):", self.e_func))):
            g.addWidget(W.QLabel(t), r, 0)
            g.addWidget(e, r, 1)
        g.addWidget(W.QLabel("修改物理地址时，响应地址默认跟随为 物理地址+8；也可单独修改。"), 3, 0, 1, 2)
        self.btn_ok, self.btn_cancel = W.QPushButton("确定"), W.QPushButton("取消")
        g.addWidget(self.btn_ok, 4, 0)
        g.addWidget(self.btn_cancel, 4, 1)
        self.e_phys.textEdited.connect(self._follow)
        self.e_resp.textEdited.connect(self._manual)
        self.btn_ok.clicked.connect(self._on_ok)
        self.btn_cancel.clicked.connect(self.reject)

    def _follow(self, *_) -> None:
        if self._auto:
            try:
                self.e_resp.setText(f"{parse_id(self.e_phys.text()) + 8:X}")
            except ValueError:
                pass

    def _manual(self, *_) -> None:
        self._auto = False

    def _on_ok(self) -> None:
        try:
            vals = tuple(parse_id(e.text()) for e in (self.e_phys, self.e_resp, self.e_func))
        except ValueError:
            warn(self, "CAN ID 必须是十六进制数")
            return
        self.result_vals = vals
        self.accept()


class ChoiceDialog(W.QDialog):
    """Manage mode: what to do with a tile."""

    def __init__(self, name: str, is_dir: bool, parent=None) -> None:
        super().__init__(parent)
        self.choice = ""
        self.setWindowTitle(name)
        v = W.QVBoxLayout(self)
        v.addWidget(W.QLabel(f"「{name}」"))
        self.buttons = {}
        for key, text in (("edit", "进入编辑" if is_dir else "编辑执行器"), ("rename", "重命名"), ("delete", "删除")):
            b = W.QPushButton(text)
            b.clicked.connect(lambda _=False, k=key: self._pick(k))
            v.addWidget(b)
            self.buttons[key] = b
        c = W.QPushButton("取消")
        c.clicked.connect(self.reject)
        v.addWidget(c)

    def _pick(self, k: str) -> None:
        self.choice = k
        self.accept()


class ScanWindow(W.QMainWindow):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("软件扫描")
        self.resize(1180, 700)
        self.store = ScanStore(ctx.scan_dir)
        self.store.ensure_defaults()
        self.modules: List[Module] = []
        self.rows: List[dict] = []
        self._cells: list = []
        self._tile_widgets: list = []
        self.tile_buttons: list = []
        self.cur_module: Optional[Module] = None
        self.cur_dir = ""
        self.editing = None                                   # ("info", module, key) | ("func", module, parent_dir, path|None)
        self.worker: Optional[Worker] = None
        self.cancel = threading.Event()
        self.sig, self.bridge = _Sig(), Bridge()
        self.sig.field.connect(self._on_field)
        self.sig.state.connect(self._on_state)
        self.sig.outcome.connect(self._on_outcome)
        self.bridge.event.connect(self._on_event)
        self.panel: Optional[RunPanel] = None
        self.manage = False
        self.stack = W.QStackedWidget()
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self._build_list())
        self.stack.addWidget(self._build_module_page())
        self.editor = ExecEditorPage(ctx, self._save_editor, self._run_editor, self._editor_back)
        self.stack.addWidget(self.editor)
        self.editor.panel.b_stop.clicked.connect(self._stop)
        self.reload_modules()
        ctx.dev.add_listener(self._on_dev_change)

    # ================================================================== page 0: module list
    def _build_list(self) -> W.QWidget:
        pg = W.QWidget()
        v = W.QVBoxLayout(pg)
        top = W.QHBoxLayout()
        self.c_ch = W.QComboBox()
        self.b_dir = W.QPushButton("数据文件夹…")
        self.l_dir = W.QLabel(self.ctx.scan_dir)
        for wd in (W.QLabel("通道:"), self.c_ch, self.b_dir, self.l_dir):
            top.addWidget(wd)
        top.addStretch(1)
        v.addLayout(top)
        self.grid_host = W.QWidget()
        self.grid = W.QGridLayout(self.grid_host)
        v.addWidget(self.grid_host)
        v.addStretch(1)
        bot = W.QHBoxLayout()
        self.k_all = W.QCheckBox("全选")
        self.l_status = W.QLabel("")
        self.b_start, self.b_stop = W.QPushButton("开始扫描"), W.QPushButton("停止扫描")
        self.b_stop.setEnabled(False)
        for wd in (self.k_all, self.l_status):
            bot.addWidget(wd)
        bot.addStretch(1)
        bot.addWidget(self.b_start)
        bot.addWidget(self.b_stop)
        v.addLayout(bot)
        self.b_start.clicked.connect(self._start_scan)
        self.b_stop.clicked.connect(self._stop)
        self.k_all.clicked.connect(self._select_all)
        self.b_dir.clicked.connect(self._choose_dir)
        self.c_ch.currentIndexChanged.connect(self._on_channel)
        self._on_dev_change()
        return pg

    def reload_modules(self) -> None:
        self.modules = self.store.modules()
        self._clear_grid(self.grid, self._cells)
        self.rows = []
        for c, t in enumerate(COLS):
            lb = W.QLabel(t)
            lb.setStyleSheet("font-weight: bold;")
            self._put(self.grid, self._cells, lb, 0, c)
        for i, m in enumerate(self.modules):
            r = {"m": m, "chk": W.QCheckBox(), "name": W.QPushButton(m.name), "fields": {}, "next": W.QPushButton("›"),
                 "state": W.QLabel("")}
            r["chk"].setChecked(True)
            r["name"].setFlat(True)
            r["name"].setToolTip(f"物理 {m.phys:X} / 响应 {m.resp_id:X} / 功能 {m.func:X} — 点击修改地址")
            self._put(self.grid, self._cells, r["chk"], i + 1, 0)
            self._put(self.grid, self._cells, W.QLabel(str(i + 1)), i + 1, 1)
            self._put(self.grid, self._cells, r["name"], i + 1, 2)
            for c, (key, label) in enumerate(INFO_FIELDS):
                b = W.QPushButton("-")
                b.setFlat(True)
                b.setToolTip(f"点击编辑「{label}」的读取方式(执行器)")
                b.clicked.connect(lambda _=False, mm=m, k=key: self._edit_info(mm, k))
                r["fields"][key] = b
                self._put(self.grid, self._cells, b, i + 1, 3 + c)
            self._put(self.grid, self._cells, r["next"], i + 1, 7)
            r["name"].clicked.connect(lambda _=False, mm=m: self._edit_addr(mm))
            r["next"].clicked.connect(lambda _=False, mm=m: self._open_module(mm))
            self.rows.append(r)

    @staticmethod
    def _put(layout, bag: list, w, r: int, c: int) -> None:
        bag.append(w)
        layout.addWidget(w, r, c)

    @staticmethod
    def _clear_grid(layout, bag: list) -> None:
        for w in bag:
            layout.removeWidget(w)
            w.hide()
            w.deleteLater()
        bag.clear()

    def _row(self, folder: str) -> Optional[dict]:
        for r in self.rows:
            if r["m"].folder == folder:
                return r
        return None

    def _select_all(self) -> None:
        for r in self.rows:
            r["chk"].setChecked(self.k_all.isChecked())

    def _choose_dir(self) -> None:
        if self.busy():
            return
        d = W.QFileDialog.getExistingDirectory(self, "选择数据文件夹", self.ctx.scan_dir)
        if d:
            self.ctx.scan_dir = d
            self.ctx.save()
            self.l_dir.setText(d)
            self.store = ScanStore(d)
            self.store.ensure_defaults()
            self.reload_modules()

    def _on_dev_change(self) -> None:
        n = fill_channel_combo(self.c_ch, self.ctx.dev, self.ctx.channel.channel)
        if n >= 0 and n != self.ctx.channel.channel:
            self.ctx.channel.channel = n

    def _on_channel(self, *_) -> None:
        n = self.c_ch.currentData()
        if n is not None and n >= 0 and n != self.ctx.channel.channel:
            self.ctx.channel.channel = n
            self.ctx.save()

    def _edit_addr(self, m: Module) -> None:
        if self.busy():
            return
        d = ModuleAddrDialog(m, self)
        if d.exec_() and d.result_vals:
            m.phys, m.resp, m.func = d.result_vals
            self.store.save_module(m)
            self.reload_modules()

    def _edit_info(self, m: Module, key: str) -> None:
        if self.busy():
            return
        try:
            ex = self.store.load_info(m, key)
        except ScanError as e:
            warn(self, str(e))
            return
        self.editing = ("info", m, key)
        self.editor.load(ex, f"{m.name} › {FIELD_LABEL[key]}（读取方式）", name_editable=False)
        self.editor.panel.finish("")
        self.panel = self.editor.panel
        self.stack.setCurrentIndex(EDITOR)

    # ------------------------------------------------------------------ scanning
    def busy(self) -> bool:
        if self.worker is not None and self.worker.isRunning():
            info(self, "正在运行，请先等待结束或点「停止」。")
            return True
        return False

    def _session_for(self, m: Module, hooks: Hooks):
        cfg = cfg_for(self.ctx.diag_cfg, m)
        return self.ctx.open_session(cfg, self.ctx.channel, self, hooks)

    def _start_scan(self) -> None:
        if self.busy():
            return
        sel = [r["m"] for r in self.rows if r["chk"].isChecked()]
        if not sel:
            info(self, "请先在左侧勾选要扫描的软件。")
            return
        if not self.ctx.dev.started():
            warn(self, "没有已启动的通道：请先在主界面打开设备并启动通道。")
            return
        for m in sel:
            r = self._row(m.folder)
            for b in r["fields"].values():
                b.setText("-")
        self.cancel = threading.Event()
        cancel, store = self.cancel, self.store
        self.b_start.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.l_status.setText("扫描中…")

        def job():
            try:
                for m in sel:
                    if cancel.is_set():
                        break
                    self.sig.state.emit(m.folder, "扫描中")
                    try:
                        scan_module(store, m, lambda mm: self._session_for(mm, Hooks()), cancel,
                                    lambda k, t, f=m.folder: self.sig.field.emit(f, k, t))
                    finally:
                        self.sig.state.emit(m.folder, "")
                return "已取消" if cancel.is_set() else "扫描完成"
            finally:
                self.ctx.dev.release(self)

        self._run_worker(job, self._scan_done)

    def _scan_done(self, msg) -> None:
        self.l_status.setText(str(msg))

    def _on_field(self, folder: str, key: str, text: str) -> None:
        r = self._row(folder)
        if r is not None:
            r["fields"][key].setText(text)

    def _on_state(self, folder: str, state: str) -> None:
        r = self._row(folder)
        if r is not None:
            r["name"].setText(f"{r['m'].name}  {state}".strip())

    def _stop(self) -> None:
        self.cancel.set()

    # ------------------------------------------------------------------ worker plumbing
    def _run_worker(self, fn, on_result) -> None:
        self.worker = Worker(fn)
        self.worker.result.connect(on_result)
        self.worker.error.connect(self._on_error)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _on_error(self, msg: str) -> None:
        if self.panel is not None and self.stack.currentIndex() != LIST and e.core:
            self.panel.finish(f"失败: {msg}")
        self.l_status.setText(f"失败: {msg}")
        warn(self, msg)

    def _on_finished(self) -> None:
        self.b_start.setEnabled(True)
        self.b_stop.setEnabled(False)
        if self.panel is not None:
            self.panel.b_stop.setEnabled(False)

    # ================================================================== page 1: module functions
    def _build_module_page(self) -> W.QWidget:
        pg = W.QWidget()
        v = W.QVBoxLayout(pg)
        top = W.QHBoxLayout()
        self.b_up = W.QPushButton("‹ 上一级")
        self.l_path = W.QLabel("")
        self.b_addfn, self.b_addfolder = W.QPushButton("添加功能"), W.QPushButton("新建文件夹")
        self.b_manage = W.QPushButton("管理")
        self.b_manage.setCheckable(True)
        top.addWidget(self.b_up)
        top.addWidget(self.l_path, 1)
        for b in (self.b_addfn, self.b_addfolder, self.b_manage):
            top.addWidget(b)
        v.addLayout(top)
        self.tiles_host = W.QWidget()
        self.tiles = W.QGridLayout(self.tiles_host)
        v.addWidget(self.tiles_host)
        v.addStretch(1)
        self.mpanel = RunPanel()
        self.mpanel.b_stop.clicked.connect(self._stop)
        v.addWidget(self.mpanel)
        self.b_up.clicked.connect(self._go_up)
        self.b_addfn.clicked.connect(self._add_function)
        self.b_addfolder.clicked.connect(self._add_folder)
        self.b_manage.clicked.connect(self._toggle_manage)
        return pg

    def _open_module(self, m: Module) -> None:
        if self.busy():
            return
        self.cur_module = m
        self._goto(self.store.func_dir(m))
        self.panel = self.mpanel
        self.stack.setCurrentIndex(MODULE)

    def _breadcrumb(self) -> str:
        base = self.store.func_dir(self.cur_module)
        rel = os.path.relpath(self.cur_dir, base)
        parts = [self.cur_module.name, "功能"] + ([] if rel == "." else rel.split(os.sep))
        return " › ".join(parts)

    def _goto(self, directory: str) -> None:
        self.cur_dir = directory
        self.l_path.setText(self._breadcrumb())
        self._clear_grid(self.tiles, self._tile_widgets)
        self.tile_buttons = []
        entries = self.store.list_entries(directory)
        for i, e in enumerate(entries):
            b = W.QPushButton(("▣ " if e.is_dir else "") + e.name)
            b.setMinimumHeight(70)
            b.setMinimumWidth(150)
            b.clicked.connect(lambda _=False, ee=e: self._tile(ee))
            self._put(self.tiles, self._tile_widgets, b, i // 4, i % 4)
            self.tile_buttons.append((b, e))
        if not entries:
            self._put(self.tiles, self._tile_widgets, W.QLabel("这里还没有功能：点「添加功能」或「新建文件夹」。"), 0, 0)

    def _go_up(self) -> None:
        if self.busy():
            return
        base = self.store.func_dir(self.cur_module)
        if os.path.abspath(self.cur_dir) == os.path.abspath(base):
            self.stack.setCurrentIndex(LIST)
        else:
            self._goto(os.path.dirname(self.cur_dir))

    def _toggle_manage(self) -> None:
        self.manage = self.b_manage.isChecked()
        self.mpanel.l_result.setText("管理模式：点击功能/文件夹可编辑、重命名、删除" if self.manage else "")

    def _tile(self, e: Entry) -> None:
        if self.manage:
            d = ChoiceDialog(e.name, e.is_dir, self)
            if not d.exec_():
                return
            if d.choice == "edit":
                self._open_entry_edit(e)
            elif d.choice == "rename":
                self._rename(e)
            elif d.choice == "delete":
                self._delete(e)
            return
        if e.is_dir:
            self._goto(e.path)
        else:
            self._run_entry(e)

    def _open_entry_edit(self, e: Entry) -> None:
        if e.is_dir:
            self._goto(e.path)
            return
        try:
            ex = Executor.load(e.path)
        except ScanError as err:
            warn(self, str(err))
            return
        self.editing = ("func", self.cur_module, self.cur_dir, e.path)
        self.editor.load(ex, f"{self._breadcrumb()} › {e.name}")
        self.editor.panel.finish("")
        self.panel = self.editor.panel
        self.stack.setCurrentIndex(EDITOR)

    def _rename(self, e: Entry) -> None:
        name, ok = W.QInputDialog.getText(self, "重命名", "新名称:", text=e.name)
        if ok and name.strip() and name.strip() != e.name:
            try:
                self.store.rename(e, name)
            except (ScanError, OSError) as err:
                warn(self, str(err))
            self._goto(self.cur_dir)

    def _delete(self, e: Entry) -> None:
        extra = "（连同里面的所有功能）" if e.is_dir else ""
        if W.QMessageBox.question(self, "删除", f"确定删除「{e.name}」{extra}?") != W.QMessageBox.Yes:
            return
        try:
            self.store.delete(e)
        except (ScanError, OSError) as err:
            warn(self, str(err))
        self._goto(self.cur_dir)

    def _add_folder(self) -> None:
        name, ok = W.QInputDialog.getText(self, "新建文件夹", "文件夹名称:")
        if ok and name.strip():
            try:
                self.store.make_folder(self.cur_dir, name)
            except (ScanError, OSError) as err:
                warn(self, str(err))
            self._goto(self.cur_dir)

    def _add_function(self) -> None:
        if self.busy():
            return
        self.editing = ("func", self.cur_module, self.cur_dir, None)
        self.editor.load(Executor("新建功能"), f"{self._breadcrumb()} › 添加功能")
        self.editor.panel.finish("")
        self.panel = self.editor.panel
        self.stack.setCurrentIndex(EDITOR)

    # ------------------------------------------------------------------ running an executor
    def _run_entry(self, e: Entry) -> None:
        try:
            ex = Executor.load(e.path)
        except ScanError as err:
            warn(self, str(err))
            return
        self._run_executor(ex, self.cur_module, self.mpanel)

    def _run_executor(self, ex: Executor, m: Module, panel: RunPanel) -> None:
        if self.busy():
            return
        if not self.ctx.dev.started():
            warn(self, "没有已启动的通道：请先在主界面打开设备并启动通道。")
            return
        self.panel = panel
        panel.start()
        self.cancel = threading.Event()
        cancel, hooks = self.cancel, self.bridge.hooks()

        def job():
            sess = None
            try:
                sess = self._session_for(m, hooks)
                sess.cancel = cancel
                out = run_executor(sess, ex)
            finally:
                if sess is not None:
                    sess.close()
                self.ctx.dev.release(self)
            self.sig.outcome.emit(out)
            return out

        self._run_worker(job, lambda *_: None)

    def _on_outcome(self, out) -> None:
        if self.panel is None:
            return
        if out.ok:
            self.panel.finish(out.text if out.text else "完成（无数据）")
        else:
            self.panel.finish(f"失败: {out.error}")

    def _on_event(self, e) -> None:
        if self.panel is not None and self.stack.currentIndex() != LIST and e.core:
            self.panel.add(e.line("hex"))

    # ------------------------------------------------------------------ editor page
    def _save_editor(self, ex: Executor) -> bool:
        kind = self.editing[0] if self.editing else ""
        try:
            if kind == "info":
                _, m, key = self.editing
                self.store.save_info(m, key, ex)
            elif kind == "func":
                _, m, parent, path = self.editing
                name = ex.name.strip()
                if path is None:
                    self.editing = ("func", m, parent, self.store.save_new(parent, name, ex))
                else:
                    cur = os.path.splitext(os.path.basename(path))[0]
                    if name != cur:
                        path = self.store.rename(Entry(cur, path, False), name)
                        self.editing = ("func", m, parent, path)
                    ex.save(path)
            else:
                return False
        except (ScanError, OSError) as e:
            warn(self, str(e))
            return False
        self.editor.panel.finish("已保存")
        return True

    def _run_editor(self, ex: Executor) -> None:
        if self.editing:
            self._run_executor(ex, self.editing[1], self.editor.panel)

    def _editor_back(self) -> None:
        if self.busy():
            return
        if self.editor.dirty:
            r = W.QMessageBox.question(self, "未保存", "有未保存的修改，返回将丢弃。是否返回?")
            if r != W.QMessageBox.Yes:
                return
        kind = self.editing[0] if self.editing else "info"
        if kind == "func":
            self._goto(self.cur_dir if self.cur_module is self.editing[1] else self.editing[2])
            self.panel = self.mpanel
            self.stack.setCurrentIndex(MODULE)
        else:
            self.panel = None
            self.stack.setCurrentIndex(LIST)
        self.editing = None

    # ------------------------------------------------------------------ lifecycle
    def closeEvent(self, event) -> None:
        if self.worker is not None and self.worker.isRunning():
            info(self, "正在运行，请先停止后再关闭窗口。")
            event.ignore()
            return
        self.ctx.dev.remove_listener(self._on_dev_change)
        self.ctx.dev.release(self)
        self.ctx.save()
        event.accept()
