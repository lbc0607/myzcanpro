"""步骤编辑器: edit a 功能's step sequence (services, 解锁, 下载, 例程, 延时 ...).  Used by the 软件扫描 function editor."""
from typing import List, Optional

from zdiag.project import Project, fmt_hex, new_step, parse_id, step_summary
from .common import info, warn
from .dialogs import CrcToolDialog, ServiceEditorDialog, SettingsDialog
from .flash_forms import make_form
from .qt import QtWidgets as W


# =============================================================================== 编辑器
class StepEditorTab(W.QWidget):
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
        self.l_addr = W.QLabel("地址:")
        for wd in (self.l_addr, self.e_phys, self.e_func, self.e_resp,
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

    def embed(self, apply_text: str = "试运行") -> None:
        """Used by the 软件扫描 executor editor: addresses come from the module, files/settings are not needed."""
        for w in (self.b_new, self.b_open, self.b_export, self.b_more, self.b_crc, self.l_addr,
                  self.e_phys, self.e_func, self.e_resp):
            w.hide()
        self.b_apply.setText(apply_text)

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

    def _insert(self, step: dict) -> None:
        """New steps go directly below the selected one (appended when nothing is selected)."""
        st = self.project.steps
        r = self.table.currentRow()
        pos = r + 1 if 0 <= r < len(st) else len(st)
        st.insert(pos, step)
        self.win.mark_dirty()
        self.refresh(pos)

    def _add(self) -> None:
        s = self._build_step()
        if s is not None:
            self._insert(s)

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
        self._insert(new_step("delay", ms=ms))

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
