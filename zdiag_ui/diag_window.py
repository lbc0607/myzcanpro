"""UDS诊断 window (ZCANPro "高级功能 → UDS诊断")."""
import json
import threading
import time
from typing import Dict, List, Optional

from zdiag.engine import DiagSession, LogEvent, match_expect
from zdiag.project import fmt_hex, parse_hex, parse_id
from .appctx import ConnError
from .common import fill_channel_combo, Bridge, Worker, event_row, info, pick_file, save_file, warn
from .dialogs import ServiceEditorDialog, SettingsDialog
from .logsaver import LogSaver
from .qt import Qt, QtWidgets as W, Signal

ROLE = Qt.UserRole


class DiagWindow(W.QMainWindow):
    stat = Signal(str)                                   # "pass" | "fail" | "unchecked"

    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.cfg, self.ch = ctx.diag_cfg, ctx.channel
        self.bridge = Bridge()
        self.session: Optional[DiagSession] = None
        self.worker: Optional[Worker] = None
        self.cancel = threading.Event()
        self.rows: List[Dict] = []
        self.events: List[LogEvent] = []
        self.saver = LogSaver()
        self.counts = {"tested": 0, "pass": 0, "fail": 0, "unchecked": 0}
        self._sel_name = "原始报文"
        self.setWindowTitle("UDS诊断")
        self.resize(1180, 760)
        self._build()
        self._load_tree()
        self._sync_top()
        self.bridge.event.connect(self._on_event)
        self.stat.connect(self._on_stat)
        self.ctx.dev.add_listener(self._on_dev_change)

    # ------------------------------------------------------------------ UI construction
    def _build(self) -> None:
        root = W.QWidget()
        self.setCentralWidget(root)
        v = W.QVBoxLayout(root)
        # ---- top bar
        top = W.QFrame()
        top.setObjectName("panel")
        h = W.QHBoxLayout(top)
        self.c_ch = W.QComboBox()                       # the channels started in the main window's 设备管理
        self.c_ch.setMinimumWidth(170)
        self.e_phys, self.e_func, self.e_resp = W.QLineEdit(), W.QLineEdit(), W.QLineEdit()
        for e in (self.e_phys, self.e_func, self.e_resp):
            e.setFixedWidth(84)
        self.c_addr = W.QComboBox()
        self.c_addr.addItems(["物理地址", "功能地址"])
        self.b_more = W.QPushButton("更多设置")
        self.k_fc = W.QCheckBox("自动流控")
        self.b_svc, self.b_dtc = W.QPushButton("服务编辑器"), W.QPushButton("故障码编辑器")
        for wd in (W.QLabel("通道:"), self.c_ch, W.QLabel("  地址:"), self.e_phys, self.e_func,
                   self.e_resp, self.c_addr, self.b_more, self.k_fc):
            h.addWidget(wd)
        h.addStretch(1)
        h.addWidget(self.b_svc)
        h.addWidget(self.b_dtc)
        v.addWidget(top)
        # ---- tabs
        self.tabs = W.QTabWidget()
        v.addWidget(self.tabs, 1)
        page = W.QWidget()
        self.tabs.addTab(page, "服务")
        dtc = W.QWidget()
        dl = W.QVBoxLayout(dtc)
        dl.addWidget(W.QLabel("故障码功能将在后续版本提供。"))
        dl.addStretch(1)
        self.tabs.addTab(dtc, "故障码")
        body = W.QHBoxLayout(page)
        self.tree = W.QTreeWidget()
        self.tree.setHeaderHidden(True)
        body.addWidget(self.tree, 3)
        right = W.QVBoxLayout()
        body.addLayout(right, 7)
        # request panel
        f1 = W.QFrame()
        f1.setObjectName("panel")
        g = W.QGridLayout(f1)
        self.e_pdu, self.e_exp = W.QLineEdit(), W.QLineEdit()
        self.c_sup, self.c_chk = W.QComboBox(), W.QComboBox()
        for c in (self.c_sup, self.c_chk):
            c.addItems(["否", "是"])
        self.b_add, self.b_now = W.QPushButton("添加到列表"), W.QPushButton("立即发送")
        g.addWidget(W.QLabel("请求PDU:"), 0, 0)
        g.addWidget(self.e_pdu, 0, 1)
        g.addWidget(W.QLabel("抑制响应:"), 0, 2)
        g.addWidget(self.c_sup, 0, 3)
        g.addWidget(W.QLabel("响应PDU:"), 1, 0)
        g.addWidget(self.e_exp, 1, 1)
        g.addWidget(W.QLabel("校验响应:"), 1, 2)
        g.addWidget(self.c_chk, 1, 3)
        bl = W.QHBoxLayout()
        bl.addWidget(self.b_add)
        bl.addWidget(self.b_now)
        bl.addStretch(1)
        g.addLayout(bl, 2, 0, 1, 4)
        right.addWidget(f1)
        # list panel
        f2 = W.QFrame()
        f2.setObjectName("panel")
        l2 = W.QVBoxLayout(f2)
        tb = W.QHBoxLayout()
        self.b_delay, self.b_del, self.b_clear = W.QPushButton("添加延时"), W.QPushButton("删除"), W.QPushButton("清空")
        self.b_down, self.b_up = W.QPushButton("下移"), W.QPushButton("上移")
        self.b_imp, self.b_exp = W.QPushButton("导入"), W.QPushButton("导出")
        self.s_loop, self.s_gap = W.QSpinBox(), W.QSpinBox()
        self.s_loop.setRange(1, 1000000)
        self.s_loop.setValue(1)
        self.s_gap.setRange(0, 600000)
        self.s_gap.setValue(1)
        self.b_list = W.QPushButton("列表发送")
        for wd in (self.b_delay, self.b_del, self.b_clear, self.b_down, self.b_up, self.b_imp, self.b_exp,
                   W.QLabel("循环次数:"), self.s_loop, W.QLabel("请求间隔(ms):"), self.s_gap, self.b_list):
            tb.addWidget(wd)
        tb.addStretch(1)
        l2.addLayout(tb)
        self.table = W.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", "名称", "请求PDU", "请求地址", "抑制响应", "校验响应"])
        l2.addWidget(self.table)
        right.addWidget(f2, 3)
        # log panel
        f3 = W.QFrame()
        f3.setObjectName("panel")
        l3 = W.QVBoxLayout(f3)
        lt = W.QHBoxLayout()
        self.c_fmt = W.QComboBox()
        self.c_fmt.addItems(["十六进制", "ASCII"])
        self.b_lexp, self.b_save, self.b_lclr = W.QPushButton("导出"), W.QPushButton("实时保存"), W.QPushButton("清空")
        self.b_save.setCheckable(True)
        for wd in (self.c_fmt, self.b_lexp, self.b_save, self.b_lclr):
            lt.addWidget(wd)
        lt.addStretch(1)
        l3.addLayout(lt)
        self.log = W.QPlainTextEdit()
        self.log.setReadOnly(True)
        l3.addWidget(self.log)
        st = W.QHBoxLayout()
        self.l_tested, self.l_pass, self.l_fail, self.l_unch = (W.QLabel("0") for _ in range(4))
        for lab, wd in (("测试次数:", self.l_tested), ("通过:", self.l_pass), ("未通过:", self.l_fail),
                        ("未检查响应:", self.l_unch)):
            st.addWidget(W.QLabel(lab))
            st.addWidget(wd)
            st.addSpacing(30)
        st.addStretch(1)
        self.b_reset = W.QPushButton("重置")
        st.addWidget(self.b_reset)
        l3.addLayout(st)
        right.addWidget(f3, 3)
        # ---- wiring
        self.c_ch.currentIndexChanged.connect(self._on_channel)
        for e in (self.e_phys, self.e_func, self.e_resp):
            e.editingFinished.connect(self._commit_addr)
        self.k_fc.toggled.connect(self._on_fc)
        self.b_more.clicked.connect(self._more)
        self.b_svc.clicked.connect(self._service_editor)
        self.b_dtc.clicked.connect(lambda: info(self, "故障码功能将在后续版本提供。"))
        self.tree.currentItemChanged.connect(self._on_tree)
        self.b_add.clicked.connect(self._add_to_list)
        self.b_now.clicked.connect(self._send_now)
        self.b_delay.clicked.connect(self._add_delay)
        self.b_del.clicked.connect(self._delete_row)
        self.b_clear.clicked.connect(self._clear_rows)
        self.b_up.clicked.connect(lambda: self._move(-1))
        self.b_down.clicked.connect(lambda: self._move(1))
        self.b_imp.clicked.connect(self._import)
        self.b_exp.clicked.connect(self._export)
        self.b_list.clicked.connect(self._list_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.currentCellChanged.connect(self._on_row_selected)
        self.c_fmt.currentIndexChanged.connect(self._rerender)
        self.b_lexp.clicked.connect(self._export_log)
        self.b_save.toggled.connect(self._toggle_save)
        self.b_lclr.clicked.connect(self._clear_log)
        self.b_reset.clicked.connect(self._reset_stats)

    # ------------------------------------------------------------------ top bar <-> config
    def _sync_top(self) -> None:
        a = self.cfg.addr
        fill_channel_combo(self.c_ch, self.ctx.dev, self.ch.channel)
        self.e_phys.setText(f"{a.phys:x}")
        self.e_func.setText(f"{a.func:x}")
        self.e_resp.setText(f"{a.resp:x}")
        self.k_fc.blockSignals(True)
        self.k_fc.setChecked(self.cfg.settings.auto_fc)
        self.k_fc.blockSignals(False)
        self.log.setMaximumBlockCount(self.cfg.settings.max_log_lines)

    def _on_channel(self, *_) -> None:
        n = self.c_ch.currentData()
        if n is not None and n >= 0 and n != self.ch.channel:
            self.ch.channel = n
            self._drop_session()
            self.ctx.save()

    def _on_dev_change(self) -> None:
        """Device manager state changed (channel started/stopped, device closed)."""
        n = fill_channel_combo(self.c_ch, self.ctx.dev, self.ch.channel)
        if n >= 0 and n != self.ch.channel:
            self.ch.channel = n

    def _commit_addr(self) -> None:
        try:
            p, f, r = (parse_id(e.text()) for e in (self.e_phys, self.e_func, self.e_resp))
        except ValueError:
            warn(self, "CAN ID 必须是十六进制数")
            self._sync_top()
            return
        a = self.cfg.addr
        if (p, f, r) != (a.phys, a.func, a.resp):
            a.phys, a.func, a.resp = p, f, r
            self._drop_session()
            self.ctx.save()

    def _on_fc(self, on: bool) -> None:
        self.cfg.settings.auto_fc = bool(on)
        self._drop_session()
        self.ctx.save()

    def _more(self) -> None:
        dlg = SettingsDialog(self.ctx, self.cfg, self.ch, self)
        if dlg.exec_():
            self._drop_session()
            self._sync_top()
            self.ctx.save()

    def _service_editor(self) -> None:
        if ServiceEditorDialog(self.ctx, self).exec_():
            self._load_tree()

    # ------------------------------------------------------------------ service tree
    def _load_tree(self) -> None:
        self.tree.clear()
        raw = W.QTreeWidgetItem(["【原始报文】"])
        raw.setData(0, ROLE, ("raw",))
        self.tree.addTopLevelItem(raw)
        for s in self.ctx.services:
            it = W.QTreeWidgetItem([f"({s['sid']:02X}) {s['name']}"])
            it.setData(0, ROLE, ("svc", s["sid"]))
            for sub in s["subs"]:
                ch = W.QTreeWidgetItem([f"({sub['code']:02X}) {sub['name']}"])
                ch.setData(0, ROLE, ("sub", s["sid"], sub["code"]))
                it.addChild(ch)
            self.tree.addTopLevelItem(it)

    def _on_tree(self, cur, prev=None) -> None:
        if cur is None:
            return
        d = cur.data(0, ROLE)
        if not d:
            return
        if d[0] == "raw":
            pdu, exp, self._sel_name = "", "", "原始报文"
        elif d[0] == "svc":
            pdu, exp, self._sel_name = f"{d[1]:02X}", f"{(d[1] + 0x40) & 0xFF:02X}", cur.text(0)
        else:
            pdu = f"{d[1]:02X} {d[2]:02X}"
            exp = f"{(d[1] + 0x40) & 0xFF:02X}"
            self._sel_name = f"{cur.parent().text(0)} {cur.text(0)}"
        self.e_pdu.setText(pdu)
        self.e_exp.setText(exp)

    # ------------------------------------------------------------------ sessions / threads
    def _drop_session(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.cancel.set()
        if self.session is not None:
            self.session.close()
            self.session = None
        self.ctx.dev.release(self)

    def _ensure_session(self) -> bool:
        if self.session is not None:
            return True
        try:
            self.session = self.ctx.open_session(self.cfg, self.ch, self, self.bridge.hooks())
        except ConnError as e:
            warn(self, str(e))
            return False
        self.session.cancel = self.cancel
        if self.cfg.keepalive.enabled:
            self.session.apply_keepalive()
        return True

    def _busy(self) -> bool:
        if self.worker is not None and self.worker.isRunning():
            info(self, "正在发送中，请等待或先停止。")
            return True
        return False

    def _start(self, fn, on_done=None) -> None:
        self.cancel.clear()
        w = Worker(fn, self)
        w.result.connect(self._job_result)            # bound methods -> always run in the GUI thread
        w.error.connect(self._job_error)
        w.finished.connect(self._job_finished)
        self.worker = w
        self.b_now.setEnabled(False)
        w.start()

    def _job_result(self, _res) -> None:
        pass

    def _job_error(self, msg: str) -> None:
        warn(self, msg)

    def _job_finished(self) -> None:
        self.b_now.setEnabled(True)
        self.b_list.setText("列表发送")
        if not self.cfg.keepalive.enabled:
            self.ctx.dev.release(self)

    # ------------------------------------------------------------------ requests
    def _read_request(self) -> Optional[Dict]:
        try:
            pdu = parse_hex(self.e_pdu.text())
            if not pdu:
                raise ValueError("请求PDU 为空")
            exp = self.e_exp.text().strip()
            parse_hex(exp.replace("xx", "00").replace("XX", "00").replace("**", "00").replace("??", "00"))
        except ValueError as e:
            warn(self, str(e))
            return None
        return {"type": "req", "name": self._sel_name, "pdu": fmt_hex(pdu), "expect": exp,
                "addr": "func" if self.c_addr.currentIndex() == 1 else "phys",
                "suppress": self.c_sup.currentIndex() == 1, "check": self.c_chk.currentIndex() == 1,
                "enabled": True}

    def _evaluate(self, row: Dict, reply) -> str:
        """Compare reply with expectation, log verdict, return 'pass'|'fail'|'unchecked'."""
        if not row["check"]:
            verdict, text = "unchecked", "未检查响应"
        else:
            ok, why = match_expect(row["expect"], reply, self.cfg.settings.any_negative,
                                   suppressed=row["suppress"])
            verdict, text = ("pass", "通过") if ok else ("fail", f"未通过: {why}")
        kind = "ERR" if verdict == "fail" else "OK"
        self.bridge.event.emit(LogEvent(time.time(), kind, None, "", b"", text))
        self.stat.emit(verdict)
        return verdict

    def _send_now(self) -> None:
        if self._busy():
            return
        row = self._read_request()
        if row is None or not self._ensure_session():
            return
        sess = self.session
        pdu = parse_hex(row["pdu"])

        def job():
            reply = sess.exchange(pdu, row["addr"], row["suppress"])
            return self._evaluate(row, reply)
        self._start(job)

    # ------------------------------------------------------------------ request list
    def _refresh_table(self, select: int = -1) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            first = W.QTableWidgetItem("")
            first.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
            first.setCheckState(Qt.Checked if row.get("enabled", True) else Qt.Unchecked)
            self.table.setItem(r, 0, first)
            name = W.QTableWidgetItem(row["name"])
            if row["type"] == "delay":
                cells = [f"延时 {row['ms']} ms", "", "", ""]
            else:
                cells = [row["pdu"], "功能地址" if row["addr"] == "func" else "物理地址",
                         "是" if row["suppress"] else "否", "是" if row["check"] else "否"]
            self.table.setItem(r, 1, name)
            for c, t in enumerate(cells, start=2):
                self.table.setItem(r, c, W.QTableWidgetItem(t))
        self.table.blockSignals(False)
        if 0 <= select < len(self.rows):
            self.table.selectRow(select)

    def _add_to_list(self) -> None:
        row = self._read_request()
        if row is not None:
            self.rows.append(row)
            self._refresh_table(len(self.rows) - 1)

    def _add_delay(self) -> None:
        ms, ok = W.QInputDialog.getInt(self, "添加延时", "延时(ms):", 100, 1, 3600000)
        if ok:
            self.rows.append({"type": "delay", "name": "延时", "ms": int(ms), "enabled": True})
            self._refresh_table(len(self.rows) - 1)

    def _delete_row(self) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self.rows):
            del self.rows[r]
            self._refresh_table(min(r, len(self.rows) - 1))

    def _clear_rows(self) -> None:
        self.rows = []
        self._refresh_table()

    def _move(self, d: int) -> None:
        r = self.table.currentRow()
        n = r + d
        if 0 <= r < len(self.rows) and 0 <= n < len(self.rows):
            self.rows[r], self.rows[n] = self.rows[n], self.rows[r]
            self._refresh_table(n)

    def _on_item_changed(self, item) -> None:
        r, c = item.row(), item.column()
        if not 0 <= r < len(self.rows):
            return
        row = self.rows[r]
        if c == 0:
            row["enabled"] = item.checkState() == Qt.Checked
        elif c == 1:
            row["name"] = item.text()
        elif c == 2 and row["type"] == "req":
            try:
                row["pdu"] = fmt_hex(parse_hex(item.text()))
            except ValueError as e:
                warn(self, str(e))
            self._refresh_table(r)

    def _on_row_selected(self, r, *_) -> None:
        if 0 <= r < len(self.rows) and self.rows[r]["type"] == "req":
            row = self.rows[r]
            self.e_pdu.setText(row["pdu"])
            self.e_exp.setText(row["expect"])
            self.c_sup.setCurrentIndex(1 if row["suppress"] else 0)
            self.c_chk.setCurrentIndex(1 if row["check"] else 0)
            self.c_addr.setCurrentIndex(1 if row["addr"] == "func" else 0)
            self._sel_name = row["name"]

    def _list_clicked(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.cancel.set()                         # button doubles as "停止"
            return
        rows = [dict(r) for r in self.rows if r.get("enabled", True)]
        if not rows:
            info(self, "列表里没有勾选的请求。")
            return
        if not self._ensure_session():
            return
        sess, loops, gap = self.session, self.s_loop.value(), self.s_gap.value() / 1000.0

        def job():
            for n in range(loops):
                for row in rows:
                    sess.check_cancel()
                    if row["type"] == "delay":
                        sess.sleep(row["ms"] / 1000.0)
                        continue
                    reply = sess.exchange(parse_hex(row["pdu"]), row["addr"], row["suppress"])
                    self._evaluate(row, reply)
                    if gap:
                        sess.sleep(gap)
            return None

        self.b_list.setText("停止")
        self._start(lambda: self._guard(job))

    def _guard(self, job):
        from zdiag.engine import Cancelled
        try:
            return job()
        except Cancelled:
            self.bridge.event.emit(LogEvent(time.time(), "INFO", None, "", b"", "已停止"))

    def _export(self) -> None:
        path = save_file(self, self.ctx, "导出请求列表", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"format": "myzcanpro-uds-list", "rows": self.rows}, f, ensure_ascii=False, indent=2)

    def _import(self) -> None:
        path = pick_file(self, self.ctx, "导入请求列表", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("format") != "myzcanpro-uds-list":
                raise ValueError("不是请求列表文件")
            rows = d["rows"]
            for r in rows:
                if r["type"] == "req":
                    parse_hex(r["pdu"])
        except (OSError, ValueError, KeyError) as e:
            warn(self, f"导入失败: {e}")
            return
        self.rows = rows
        self._refresh_table()

    # ------------------------------------------------------------------ log / stats
    def _fmt(self) -> str:
        return "hex" if self.c_fmt.currentIndex() == 0 else "ascii"

    def _on_event(self, e: LogEvent) -> None:
        self.events.append(e)
        limit = self.cfg.settings.max_log_lines
        if len(self.events) > limit + 1000:
            del self.events[:len(self.events) - limit]
        line = e.line(self._fmt())
        self.log.appendPlainText(line)
        self.saver.write(line)

    def _rerender(self, *_) -> None:
        self.log.clear()
        fmt = self._fmt()
        for e in self.events:
            self.log.appendPlainText(e.line(fmt))

    def _clear_log(self) -> None:
        self.events = []
        self.log.clear()

    def _export_log(self) -> None:
        path = save_file(self, self.ctx, "导出日志", "文本 (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.toPlainText())

    def _toggle_save(self, on: bool) -> None:
        if on:
            path = self.saver.start(LogSaver.default_name(self.ctx.dir, "uds"), self.cfg.settings.save_max_lines)
            info(self, f"日志实时保存到:\n{path}")
        else:
            self.saver.stop()

    def _on_stat(self, kind: str) -> None:
        self.counts["tested"] += 1
        self.counts[kind] += 1
        self._show_stats()

    def _show_stats(self) -> None:
        c = self.counts
        self.l_tested.setText(str(c["tested"]))
        self.l_pass.setText(str(c["pass"]))
        self.l_fail.setText(str(c["fail"]))
        self.l_unch.setText(str(c["unchecked"]))

    def _reset_stats(self) -> None:
        for k in self.counts:
            self.counts[k] = 0
        self._show_stats()

    # ------------------------------------------------------------------ lifecycle
    def closeEvent(self, event) -> None:
        self.ctx.dev.remove_listener(self._on_dev_change)
        self.cancel.set()
        self._drop_session()
        self.saver.stop()
        self.ctx.save()
        event.accept()
