"""流程编排 window: build a flow (per-channel lanes with scans / functions / requests / delays / cyclic sends /
conditions / loops), save it as a JSON file, and run it with one click."""
import copy
import os
import threading
from typing import Dict, List, Optional

from zdiag.flow import (CONTAINER_KEYS, LANE_TYPES, SETUP_TYPES, TYPE_LABEL, Flow, FlowError, children_of, describe, validate)
from zdiag.flowrun import CtxEnv, FlowEvent, FlowReport, FlowRunner
from zdiag.project import SIM_DEVICE
from .common import Worker, info, pick_file, save_file, warn
from .flow_dialog import NodeDialog
from .flow_view import FlowView
from .qt import Qt, QtCore, QtGui, QtWidgets as W, Signal

HEADERS = ["步骤", "类型", "参数", "状态", "结果 / 返回信息"]
FILTER = "流程文件 (*.json);;所有文件 (*.*)"
STATE_TEXT = {"running": "▶ 执行中", "ok": "✔ 成功", "fail": "✘ 失败", "skip": "— 跳过", "info": "▶ 执行中"}
STATE_COLOR = {"running": "#2980B9", "info": "#2980B9", "ok": "#27AE60", "fail": "#C0392B", "skip": "#95A5A6"}
ADDABLE = [k for k in TYPE_LABEL if k != "setup"]


class _Sig(QtCore.QObject):
    event = Signal(object)                           # lane threads -> GUI thread


class FlowWindow(W.QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.env = CtxEnv(ctx)
        self.flow = Flow()
        self.path = ""
        self.dirty = False
        self.rows: List[dict] = []                   # flattened tree: {"kind": node|group, "node", "key", "depth"}
        self.state: Dict[str, tuple] = {}            # node id -> (state, text)
        self.worker: Optional[Worker] = None
        self.cancel: Optional[threading.Event] = None
        self.sig = _Sig()
        self.sig.event.connect(self._on_event)
        self._build()
        self._starter()
        self._render()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        v = W.QVBoxLayout(self)
        top = W.QHBoxLayout()
        self.b_new, self.b_open, self.b_save, self.b_saveas = (W.QPushButton(t) for t in ("新建", "打开", "保存", "另存为"))
        self.b_run, self.b_stop, self.b_clear = W.QPushButton("▶ 执行"), W.QPushButton("■ 停止"), W.QPushButton("清除结果")
        self.l_name = W.QLabel("")
        for b in (self.b_new, self.b_open, self.b_save, self.b_saveas):
            top.addWidget(b)
        top.addSpacing(20)
        for b in (self.b_run, self.b_stop, self.b_clear):
            top.addWidget(b)
        top.addWidget(self.l_name)
        top.addStretch(1)
        v.addLayout(top)
        bar = W.QHBoxLayout()
        self.c_type = W.QComboBox()
        for k in ADDABLE:
            self.c_type.addItem(TYPE_LABEL[k], k)
        self.b_add, self.b_edit, self.b_del = W.QPushButton("添加"), W.QPushButton("编辑"), W.QPushButton("删除")
        self.b_up, self.b_down, self.b_dup, self.b_toggle = (W.QPushButton(t) for t in ("上移", "下移", "复制", "启用/禁用"))
        self.b_view = W.QPushButton("切换到列表")
        bar.addWidget(W.QLabel("添加步骤:"))
        bar.addWidget(self.c_type)
        for b in (self.b_add, self.b_edit, self.b_del, self.b_up, self.b_down, self.b_dup, self.b_toggle):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.b_view)
        v.addLayout(bar)
        self.table = W.QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(W.QAbstractItemView.SingleSelection)
        for c, w in enumerate((280, 90, 340, 90, 320)):
            self.table.setColumnWidth(c, w)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.view = FlowView()
        self.stack = W.QStackedWidget()
        self.stack.addWidget(self.view)                  # 0 = 流程图 (default), 1 = 列表
        self.stack.addWidget(self.table)
        v.addWidget(self.stack, 1)
        self.l_status = W.QLabel("提示：先选中「通道泳道」或其中的步骤再点「添加」；每个通道一条泳道，各泳道同时执行。")
        v.addWidget(self.l_status)
        self.b_new.clicked.connect(self._new)
        self.b_open.clicked.connect(self._open)
        self.b_save.clicked.connect(lambda: self._save(False))
        self.b_saveas.clicked.connect(lambda: self._save(True))
        self.b_run.clicked.connect(self._run)
        self.b_stop.clicked.connect(self._stop)
        self.b_clear.clicked.connect(self._clear_state)
        self.b_add.clicked.connect(self._add)
        self.b_edit.clicked.connect(self._edit)
        self.b_del.clicked.connect(self._delete)
        self.b_up.clicked.connect(lambda: self._move(-1))
        self.b_down.clicked.connect(lambda: self._move(1))
        self.b_dup.clicked.connect(self._duplicate)
        self.b_toggle.clicked.connect(self._toggle)
        self.table.cellDoubleClicked.connect(lambda r, c: self._edit())
        self.table.itemSelectionChanged.connect(self._sync_view)
        self.view.picked.connect(self._pick)
        self.view.edited.connect(lambda k: self._edit())
        self.b_view.clicked.connect(self._switch_view)
        self.b_stop.setEnabled(False)

    def _starter(self) -> None:
        """A new flow starts with the usual preparation and one empty lane."""
        f = Flow("新流程")
        s = f.ensure_setup()
        s["children"].append(f.make("open_device", device=self.ctx.dev.name or SIM_DEVICE))
        s["children"].append(f.make("start_channel", channel=0))
        f.nodes.append(f.make("lane", channel=0))
        self.flow, self.path = f, ""
        self.state.clear()
        self.dirty = False

    # ------------------------------------------------------------------ rendering
    def _flatten(self, nodes: List[dict], depth: int, out: List[dict]) -> None:
        for n in nodes:
            out.append({"kind": "node", "node": n, "depth": depth, "key": ""})
            for key, label, lst in children_of(n) if n["type"] in CONTAINER_KEYS else []:
                d = depth + 1
                if label:
                    out.append({"kind": "group", "node": n, "depth": d, "key": key, "label": label})
                    d += 1
                self._flatten(lst, d, out)

    def _render(self, keep: Optional[str] = None) -> None:
        sel = keep if keep is not None else self._selected_key()
        self.rows = []
        self._flatten(self.flow.nodes, 0, self.rows)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            n, ind = row["node"], "      " * row["depth"]
            if row["kind"] == "group":
                cells = [f"{ind}↳ {row['label']}", "", "", "", ""]
            else:
                tag = "" if n.get("enabled", True) else "（已禁用）"
                cells = [f"{ind}{n['name']}{tag}", TYPE_LABEL[n["type"]], describe(n), "", ""]
                st = self.state.get(n["id"])
                if st:
                    cells[3], cells[4] = STATE_TEXT.get(st[0], st[0]), st[1]
            for c, text in enumerate(cells):
                it = W.QTableWidgetItem(text)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(r, c, it)
            if row["kind"] == "node":
                self._paint(r, self.state.get(n["id"]), n)
        self.table.blockSignals(False)
        for r, row in enumerate(self.rows):
            if sel and self._key_of(row) == sel:
                self.table.selectRow(r)
                break
        self.view.show_flow(self.flow, self.state, self._selected_key())
        self.l_name.setText(f"  {self.flow.name}{' *' if self.dirty else ''}  {os.path.basename(self.path) if self.path else '(未保存)'}")

    def _paint(self, r: int, st, node: dict) -> None:
        it = self.table.item(r, 3)
        if it is None:
            return
        if st and st[0] in STATE_COLOR:
            it.setForeground(QtGui.QBrush(QtGui.QColor(STATE_COLOR[st[0]])))
        if not node.get("enabled", True):
            for c in range(len(HEADERS)):
                self.table.item(r, c).setForeground(QtGui.QBrush(QtGui.QColor("#95A5A6")))

    @staticmethod
    def _key_of(row: dict) -> str:
        return f"{row['node']['id']}:{row['key']}" if row["kind"] == "group" else row["node"]["id"]

    def _selected_key(self) -> str:
        r = self.table.currentRow()
        return self._key_of(self.rows[r]) if 0 <= r < len(self.rows) else ""

    def _sync_view(self) -> None:
        self.view.select(self._selected_key())

    def _pick(self, key: str) -> None:
        """A shape was clicked in the flowchart: select the matching row (a list placeholder selects its owner)."""
        for cand in (key, key.split(":")[0]):
            for r, row in enumerate(self.rows):
                if self._key_of(row) == cand:
                    self.table.selectRow(r)
                    return

    def _switch_view(self) -> None:
        graph = self.stack.currentIndex() == 0
        self.stack.setCurrentIndex(1 if graph else 0)
        self.b_view.setText("切换到流程图" if graph else "切换到列表")

    def _cur(self) -> Optional[dict]:
        r = self.table.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    # ------------------------------------------------------------------ editing
    def _busy(self) -> bool:
        if self.worker is not None and self.worker.isRunning():
            info(self, "流程正在执行，请先停止。")
            return True
        return False

    def _touch(self, keep: Optional[str] = None) -> None:
        self.dirty = True
        self._render(keep)

    def _target(self, kind: str):
        """(list, index, container kind) where a new node of `kind` goes, or None (message shown)."""
        row = self._cur()
        if kind == "lane":
            return self.flow.nodes, len(self.flow.nodes), "top"
        if row is None:
            lanes = self.flow.lanes()
            if not lanes:
                warn(self, "请先添加一条「通道泳道」。")
                return None
            return lanes[-1]["children"], len(lanes[-1]["children"]), "lane"
        n = row["node"]
        if row["kind"] == "group":
            lst = n[row["key"]]
            return lst, len(lst), self._ckind(n, row["key"])
        if n["type"] in ("lane", "setup"):
            return n["children"], len(n["children"]), n["type"]
        loc = self.flow.locate(n["id"])
        lst, i = loc
        return lst, i + 1, self._ckind_of_list(lst)

    @staticmethod
    def _ckind(node: dict, key: str) -> str:
        return node["type"] if node["type"] in ("lane", "setup") else key

    def _ckind_of_list(self, lst: list) -> str:
        if lst is self.flow.nodes:
            return "top"
        for n in self.flow.walk():
            for key, _, l in children_of(n) if n["type"] in CONTAINER_KEYS else []:
                if l is lst:
                    return self._ckind(n, key)
        return "lane"

    def _add(self) -> None:
        if self._busy():
            return
        kind = self.c_type.currentData()
        tgt = self._target(kind)
        if tgt is None:
            return
        lst, idx, ck = tgt
        allowed = SETUP_TYPES if ck == "setup" else LANE_TYPES if ck in ("lane", "body", "then", "else") else ("lane",)
        if kind not in allowed:
            warn(self, f"「{TYPE_LABEL[kind]}」不能添加到这里。" +
                 ("「准备」里只能放 打开设备 / 配置并启动通道 / 延时 / 备注。" if ck == "setup" else
                  "请先选中一条「通道泳道」。" if ck == "top" else "「打开设备」「配置并启动通道」只能放在「准备」里。"))
            return
        node = self.flow.make(kind)
        if kind == "lane":
            used = {l["channel"] for l in self.flow.lanes()}
            node["channel"] = next((c for c in range(8) if c not in used), 0)
            node["name"] = f"通道{node['channel']}"
        mods = self.env.store.modules()
        if kind in ("scan", "function", "uds") and mods:
            node["module"] = mods[0].name
        dlg = NodeDialog(self.flow, node, self.env.store, parent=self)
        if not dlg.exec_() or dlg.result is None:
            return
        lst.insert(idx, dlg.result)
        self._touch(dlg.result["id"])

    def _edit(self) -> None:
        row = self._cur()
        if self._busy() or row is None or row["kind"] != "node":
            return
        n = row["node"]
        if n["type"] == "setup":
            info(self, "「准备」里直接编辑它的子步骤（打开设备 / 配置并启动通道）。")
            return
        dlg = NodeDialog(self.flow, n, self.env.store, parent=self)
        if not dlg.exec_() or dlg.result is None:
            return
        n.clear()
        n.update(dlg.result)
        self._touch(n["id"])

    def _delete(self) -> None:
        row = self._cur()
        if self._busy() or row is None or row["kind"] != "node":
            return
        n = row["node"]
        extra = "及其里面的所有步骤" if n["type"] in CONTAINER_KEYS else ""
        if W.QMessageBox.question(self, "删除", f"确定删除「{n['name']}」{extra}?") != W.QMessageBox.Yes:
            return
        lst, i = self.flow.locate(n["id"])
        del lst[i]
        for m in walk_ids(n):
            self.state.pop(m, None)
        self._touch("")

    def _move(self, d: int) -> None:
        row = self._cur()
        if self._busy() or row is None or row["kind"] != "node":
            return
        lst, i = self.flow.locate(row["node"]["id"])
        j = i + d
        if 0 <= j < len(lst):
            lst[i], lst[j] = lst[j], lst[i]
            self._touch(row["node"]["id"])

    def _duplicate(self) -> None:
        row = self._cur()
        if self._busy() or row is None or row["kind"] != "node" or row["node"]["type"] in ("setup", "lane"):
            if row is not None and row["node"]["type"] in ("setup", "lane"):
                info(self, "泳道不能复制（每个通道只能有一条泳道）。")
            return
        lst, i = self.flow.locate(row["node"]["id"])
        c = self.flow.adopt(copy.deepcopy(row["node"]))
        c["name"] += " 副本"
        lst.insert(i + 1, c)
        self._touch(c["id"])

    def _toggle(self) -> None:
        row = self._cur()
        if self._busy() or row is None or row["kind"] != "node":
            return
        n = row["node"]
        n["enabled"] = not n.get("enabled", True)
        self._touch(n["id"])

    # ------------------------------------------------------------------ files
    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        return W.QMessageBox.question(self, "未保存的修改", "当前流程有未保存的修改，继续将丢弃它们。是否继续?") == W.QMessageBox.Yes

    def _new(self) -> None:
        if self._busy() or not self._confirm_discard():
            return
        self._starter()
        self._render("")

    def _open(self) -> None:
        if self._busy() or not self._confirm_discard():
            return
        p = pick_file(self, self.ctx, "打开流程", FILTER)
        if not p:
            return
        try:
            f = Flow.load(p)
        except FlowError as e:
            warn(self, str(e))
            return
        self.flow, self.path, self.dirty = f, p, False
        self.state.clear()
        self._render("")
        probs = validate(f, self.env.store)
        if probs:
            warn(self, "流程已打开，但有问题需要处理（否则无法执行）:\n" + "\n".join("· " + x for x in probs[:12]))

    def _save(self, as_new: bool) -> bool:
        p = self.path
        if as_new or not p:
            p = save_file(self, self.ctx, "保存流程", FILTER)
            if not p:
                return False
            if not p.lower().endswith(".json"):
                p += ".json"
        try:
            self.flow.save(p)
        except OSError as e:
            warn(self, f"保存失败: {e}")
            return False
        self.path, self.dirty = p, False
        self.flow.name = os.path.splitext(os.path.basename(p))[0]
        self.flow.save(p)
        self._render()
        self.l_status.setText(f"已保存: {p}")
        return True

    # ------------------------------------------------------------------ running
    def _clear_state(self) -> None:
        if self._busy():
            return
        self.state.clear()
        self._render()

    def _run(self) -> None:
        if self._busy():
            return
        probs = validate(self.flow, self.env.store)
        if probs:
            warn(self, "流程有问题，不能执行:\n" + "\n".join("· " + x for x in probs[:15]))
            return
        self.state.clear()
        self._render()
        self.cancel = threading.Event()
        runner = FlowRunner(self.flow, self.env, self.cancel, on_event=lambda e: self.sig.event.emit(e))
        self.worker = Worker(runner.run, self)
        self.worker.result.connect(self._finished)
        self.worker.error.connect(lambda m: self._finished(FlowReport(False, {}, m)))
        for b in (self.b_run, self.b_new, self.b_open):
            b.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.l_status.setText("执行中…")
        self.worker.start()

    def _stop(self) -> None:
        if self.cancel is not None:
            self.cancel.set()
            self.l_status.setText("正在停止…")

    def _on_event(self, e: FlowEvent) -> None:
        if e.state == "reset":
            self.state.pop(e.node, None)
        else:
            self.state[e.node] = (e.state, e.text)
        self.view.set_state(e.node, self.state.get(e.node))
        for r, row in enumerate(self.rows):
            if row["kind"] == "node" and row["node"]["id"] == e.node:
                st = self.state.get(e.node)
                self.table.item(r, 3).setText(STATE_TEXT.get(st[0], st[0]) if st else "")
                self.table.item(r, 4).setText(st[1] if st else "")
                self._paint(r, st, row["node"])
                if st and st[0] in ("running", "info"):
                    self.table.selectRow(r)
                break

    def _finished(self, rep: FlowReport) -> None:
        for b in (self.b_run, self.b_new, self.b_open):
            b.setEnabled(True)
        self.b_stop.setEnabled(False)
        self.l_status.setText(("✔ " if rep.ok else "✘ ") + rep.message)
        self.worker = None
        if not rep.ok and rep.message.startswith("流程有问题"):
            warn(self, rep.message)

    def closeEvent(self, event) -> None:
        if self.worker is not None and self.worker.isRunning():
            if W.QMessageBox.question(self, "流程正在执行", "流程还在执行，关闭将停止它。是否关闭?") != W.QMessageBox.Yes:
                event.ignore()
                return
            self.cancel.set()
            self.worker.wait(8000)
        elif not self._confirm_discard():
            event.ignore()
            return
        event.accept()


def walk_ids(n: dict):
    from zdiag.flow import walk
    return [m["id"] for m in walk([n])]
