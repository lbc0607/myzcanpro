"""普通发送 window: raw CAN / CAN FD frame sending (single frames and a send list)."""
import threading
from typing import List, Optional

from zdiag.devmgr import ConnError
from zdiag.project import fmt_hex, parse_hex, parse_id
from zdiag.sendlist import (FD_LENGTHS, PARALLEL, SERIAL, SendItem, SendResult, frame_at, load_list, run_send,
                            save_list)
from .common import Worker, fill_channel_combo, info, pick_file, save_file, warn
from .qt import Qt, QtCore, QtWidgets as W, Signal

HEADERS = ["序号", "状态", "ID(0x)", "协议", "长度", "名称", "数据", "帧类型", "每次发送帧数", "发送次数", "每次间隔(ms)"]
FILTER = "发送列表 (*.json);;所有文件 (*.*)"
EDITABLE = (2, 5, 6, 8, 9, 10)


class _Status(QtCore.QObject):
    changed = Signal(int, str)                     # worker thread -> GUI thread


class SendTab(W.QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.items: List[SendItem] = []
        self.worker: Optional[Worker] = None
        self.cancel = threading.Event()
        self.sig = _Status()
        self.sig.changed.connect(self._on_status)
        self._run_rows: List[int] = []             # table rows taking part in the current list run
        self._build()
        self.on_dev_change()
        self._render()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        v = W.QVBoxLayout(self)
        g1 = W.QGroupBox("帧发送")
        g = W.QGridLayout(g1)
        self.c_ch = W.QComboBox()
        self.c_ch.setMinimumWidth(190)
        self.e_id, self.e_per = W.QLineEdit("7e2"), W.QLineEdit("1")
        self.k_idinc, self.k_datainc = W.QCheckBox("ID递增"), W.QCheckBox("数据递增")
        self.c_type = W.QComboBox()
        self.c_type.addItems(["标准帧", "扩展帧"])
        self.c_len = W.QComboBox()
        self.e_count, self.e_name = W.QLineEdit("1"), W.QLineEdit()
        self.c_fmt = W.QComboBox()
        self.c_fmt.addItems(["数据帧", "远程帧"])
        self.e_data, self.e_int = W.QLineEdit("02 10 01"), W.QLineEdit("0")
        self.b_add, self.b_now = W.QPushButton("添加到列表"), W.QPushButton("立即发送")
        self.l_time = W.QLabel("发送时间(s):")
        cells = [
            (0, 0, "通道:", self.c_ch), (1, 0, "帧ID:0x", self.e_id), (2, 0, "每次发送帧数:", self.e_per),
            (0, 2, "帧类型:", self.c_type), (1, 2, "数据长度:", self.c_len), (2, 2, "发送次数:", self.e_count),
            (3, 2, "名称(可选):", self.e_name), (0, 4, "帧格式:", self.c_fmt), (1, 4, "数据:0x", self.e_data),
            (2, 4, "每次间隔(ms):", self.e_int)]
        for r, c, lab, wd in cells:
            g.addWidget(W.QLabel(lab), r, c)
            g.addWidget(wd, r, c + 1)
        g.addWidget(self.k_idinc, 3, 0)
        g.addWidget(self.k_datainc, 3, 1)
        bl = W.QHBoxLayout()
        bl.addStretch(1)
        for wd in (self.b_add, self.b_now, self.l_time):
            bl.addWidget(wd)
        g.addLayout(bl, 4, 0, 1, 6)
        v.addWidget(g1)

        g2 = W.QGroupBox("列表发送")
        l2 = W.QVBoxLayout(g2)
        self.table = W.QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        l2.addWidget(self.table, 1)
        tb = W.QHBoxLayout()
        self.b_all, self.b_inv = W.QPushButton("全选"), W.QPushButton("反选")
        self.b_up, self.b_down = W.QPushButton("上移"), W.QPushButton("下移")
        self.b_del, self.b_clear = W.QPushButton("删除"), W.QPushButton("清空")
        self.b_imp, self.b_exp = W.QPushButton("导入"), W.QPushButton("导出")
        for wd in (self.b_all, self.b_inv, self.b_up, self.b_down, self.b_del, self.b_clear, self.b_imp, self.b_exp):
            tb.addWidget(wd)
        tb.addStretch(1)
        self.c_mode, self.c_fail, self.c_speed = W.QComboBox(), W.QComboBox(), W.QComboBox()
        self.c_mode.addItems([PARALLEL, SERIAL])
        self.c_fail.addItems(["失败停止", "失败继续"])
        self.c_speed.setEditable(True)
        self.c_speed.addItems(["1", "2", "5", "10", "0.5"])
        self.b_list = W.QPushButton("列表发送")
        for wd in (self.c_mode, self.c_fail, W.QLabel("发送速度:"), self.c_speed, W.QLabel("倍"), self.b_list):
            tb.addWidget(wd)
        l2.addLayout(tb)
        v.addWidget(g2, 1)
        # wiring
        self.c_ch.currentIndexChanged.connect(self._on_channel)
        self.c_fmt.currentIndexChanged.connect(self._on_fmt)
        self.b_add.clicked.connect(self._add)
        self.b_now.clicked.connect(self._send_now)
        self.b_list.clicked.connect(self._send_list)
        self.b_all.clicked.connect(lambda: self._set_all(True))
        self.b_inv.clicked.connect(self._invert)
        self.b_up.clicked.connect(lambda: self._move(-1))
        self.b_down.clicked.connect(lambda: self._move(1))
        self.b_del.clicked.connect(self._delete)
        self.b_clear.clicked.connect(self._clear)
        self.b_imp.clicked.connect(self._import)
        self.b_exp.clicked.connect(self._export)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.currentCellChanged.connect(self._on_row_selected)

    # ------------------------------------------------------------------ channel / protocol
    def _channel(self) -> int:
        n = self.c_ch.currentData()
        return -1 if n is None else n

    def _is_fd(self) -> bool:
        cfg = self.ctx.dev.cfg_of(self._channel())
        return bool(cfg and cfg.is_fd)

    def _protocol_text(self) -> str:
        cfg = self.ctx.dev.cfg_of(self._channel())
        return cfg.protocol if cfg else "CAN"

    def on_dev_change(self) -> None:
        fill_channel_combo(self.c_ch, self.ctx.dev, self._channel())
        self._on_channel()

    def _on_channel(self, *_) -> None:
        fd = self._is_fd()
        keep = self.c_len.currentText()
        self.c_len.clear()
        self.c_len.addItems([str(n) for n in (FD_LENGTHS if fd else range(9))])
        self.c_len.setCurrentText(keep if keep in [self.c_len.itemText(i) for i in range(self.c_len.count())] else "8")
        if fd and self.c_fmt.currentIndex() == 1:
            self.c_fmt.setCurrentIndex(0)                       # CAN FD has no remote frames
        self.c_fmt.setEnabled(not fd)
        self._render()

    def _on_fmt(self, *_) -> None:
        self.e_data.setEnabled(self.c_fmt.currentIndex() == 0)

    # ------------------------------------------------------------------ form <-> item
    def _read_form(self) -> Optional[SendItem]:
        try:
            remote = self.c_fmt.currentIndex() == 1
            it = SendItem(
                can_id=parse_id(self.e_id.text()), extended=self.c_type.currentIndex() == 1, remote=remote,
                length=int(self.c_len.currentText() or 0),
                data=b"" if remote else parse_hex(self.e_data.text()),
                per_send=int(self.e_per.text()), count=int(self.e_count.text()),
                interval_ms=int(self.e_int.text()), id_inc=self.k_idinc.isChecked(),
                data_inc=self.k_datainc.isChecked(), name=self.e_name.text().strip())
            it.validate(self._is_fd())
        except ValueError as e:
            warn(self, f"发送参数无效: {e}")
            return None
        return it

    def _load_form(self, it: SendItem) -> None:
        self.e_id.setText(f"{it.can_id:x}")
        self.c_type.setCurrentIndex(1 if it.extended else 0)
        self.c_fmt.setCurrentIndex(1 if it.remote else 0)
        self.c_len.setCurrentText(str(it.length))
        self.e_data.setText(fmt_hex(it.data))
        self.e_per.setText(str(it.per_send))
        self.e_count.setText(str(it.count))
        self.e_int.setText(str(it.interval_ms))
        self.k_idinc.setChecked(it.id_inc)
        self.k_datainc.setChecked(it.data_inc)
        self.e_name.setText(it.name)

    # ------------------------------------------------------------------ list
    def _render(self, select: int = -1) -> None:
        proto = self._protocol_text()
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.items))
        for r, it in enumerate(self.items):
            shown = bytes(it.length).hex(" ").upper() if it.remote else frame_at(it, 0).data.hex(" ").upper()
            cells = [str(r + 1), "", f"{it.can_id:X}", proto, str(it.length), it.name,
                     "(远程帧)" if it.remote else shown, ("扩展" if it.extended else "标准") + ("远程帧" if it.remote else "数据帧"),
                     str(it.per_send), str(it.count), str(it.interval_ms)]
            for c, text in enumerate(cells):
                cell = W.QTableWidgetItem(text)
                flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
                if c == 0:
                    flags |= Qt.ItemIsUserCheckable
                    cell.setCheckState(Qt.Checked if it.checked else Qt.Unchecked)
                if c in EDITABLE:
                    flags |= Qt.ItemIsEditable
                cell.setFlags(flags)
                self.table.setItem(r, c, cell)
        self.table.blockSignals(False)
        if 0 <= select < len(self.items):
            self.table.selectRow(select)

    def _add(self) -> None:
        it = self._read_form()
        if it is not None:
            self.items.append(it)
            self._render(len(self.items) - 1)

    def _delete(self) -> None:
        r = self.table.currentRow()
        if 0 <= r < len(self.items):
            del self.items[r]
            self._render(min(r, len(self.items) - 1))

    def _clear(self) -> None:
        self.items = []
        self._render()

    def _move(self, d: int) -> None:
        r, n = self.table.currentRow(), self.table.currentRow() + d
        if 0 <= r < len(self.items) and 0 <= n < len(self.items):
            self.items[r], self.items[n] = self.items[n], self.items[r]
            self._render(n)

    def _set_all(self, on: bool) -> None:
        for it in self.items:
            it.checked = on
        self._render(self.table.currentRow())

    def _invert(self) -> None:
        for it in self.items:
            it.checked = not it.checked
        self._render(self.table.currentRow())

    def _on_row_selected(self, r, *_) -> None:
        if 0 <= r < len(self.items):
            self._load_form(self.items[r])

    def _on_item_changed(self, cell) -> None:
        r, c = cell.row(), cell.column()
        if not 0 <= r < len(self.items):
            return
        it = self.items[r]
        text = cell.text().strip()
        if c == 0:
            it.checked = cell.checkState() == Qt.Checked
            return
        if c not in EDITABLE:                                    # e.g. the 状态 column written while sending
            return
        try:
            new = SendItem.from_dict(it.to_dict())
            if c == 2:
                new.can_id = parse_id(text)
            elif c == 5:
                new.name = text
            elif c == 6:
                new.data = parse_hex(text)
                if len(new.data) > new.length:                  # grow the length to the next legal size
                    legal = FD_LENGTHS if self._is_fd() else range(9)
                    new.length = next((n for n in legal if n >= len(new.data)), len(new.data))
            elif c == 8:
                new.per_send = int(text)
            elif c == 9:
                new.count = int(text)
            elif c == 10:
                new.interval_ms = int(text)
            new.validate(self._is_fd())
            self.items[r] = new
        except ValueError as e:
            warn(self, f"无效的值: {e}")
        self._render(r)

    def _import(self) -> None:
        path = pick_file(self, self.ctx, "导入发送列表", FILTER)
        if path:
            try:
                self.items.extend(load_list(path))
            except (OSError, ValueError, KeyError) as e:
                warn(self, f"导入失败: {e}")
            self._render()

    def _export(self) -> None:
        path = save_file(self, self.ctx, "导出发送列表", FILTER)
        if path:
            try:
                save_list(path, self.items)
            except OSError as e:
                warn(self, f"导出失败: {e}")

    # ------------------------------------------------------------------ sending
    def running(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _speed(self) -> Optional[float]:
        try:
            v = float(self.c_speed.currentText())
            if v <= 0:
                raise ValueError
            return v
        except ValueError:
            warn(self, "发送速度应为大于 0 的数字")
            return None

    def _send_now(self) -> None:
        if self.running():
            return
        it = self._read_form()
        if it is not None:
            self._start([it], rows=[], mode=PARALLEL, speed=1.0, stop=True)

    def _send_list(self) -> None:
        if self.running():
            self.cancel.set()                                    # the button reads 停止 while running
            return
        rows = [i for i, it in enumerate(self.items) if it.checked]
        if not rows:
            info(self, "列表里没有勾选的帧，请先添加并勾选。")
            return
        speed = self._speed()
        if speed is None:
            return
        self._start([self.items[i] for i in rows], rows=rows, mode=self.c_mode.currentText(), speed=speed,
                    stop=self.c_fail.currentIndex() == 0)

    def _start(self, items: List[SendItem], rows: List[int], mode: str, speed: float, stop: bool) -> None:
        n = self._channel()
        try:
            for it in items:
                it.validate(self._is_fd())
            bus = self.ctx.dev.acquire(n, self, self.ctx.diag_cfg.addr, self.ctx.channel.sim_profile,
                                       self.ctx.diag_cfg.settings.rx_padding)
        except (ConnError, ValueError) as e:
            warn(self, str(e))
            return
        cfg = self.ctx.dev.cfg_of(n)
        fd = bool(cfg and cfg.is_fd)
        self._run_rows = rows
        for r in rows:
            self._set_status(r, "")
        self.cancel.clear()
        sig = self.sig

        def job() -> SendResult:
            return run_send(bus, items, mode=mode, speed=speed, stop_on_fail=stop, fd=fd, brs=fd, cancel=self.cancel,
                            on_status=lambda i, s: sig.changed.emit(i, s))

        w = Worker(job, self)
        w.result.connect(self._on_done)
        w.error.connect(self._on_error)
        w.finished.connect(self._on_finished)
        self.worker = w
        self.b_now.setEnabled(False)
        self.b_list.setText("停止")
        self.l_time.setText("发送时间(s): ...")
        w.start()

    def _on_status(self, i: int, status: str) -> None:
        if 0 <= i < len(self._run_rows):
            self._set_status(self._run_rows[i], status)

    def _set_status(self, row: int, text: str) -> None:
        item = self.table.item(row, 1)
        if item is not None:
            self.table.blockSignals(True)
            item.setText(text)
            self.table.blockSignals(False)

    def _on_done(self, res) -> None:
        self.l_time.setText(f"发送时间(s): {res.elapsed:.3f}")
        if res.failed:
            warn(self, f"发送出现失败: 成功 {res.sent} 帧，失败 {res.failed} 帧"
                       f"{'，已按设置停止' if res.stopped_on_fail else ''}。\n请检查总线连接、波特率和终端电阻。")

    def _on_error(self, msg: str) -> None:
        self.l_time.setText("发送时间(s): -")
        warn(self, msg)

    def _on_finished(self) -> None:
        self.b_now.setEnabled(True)
        self.b_list.setText("列表发送")
        self.ctx.dev.release(self)

    def shutdown(self) -> None:
        self.cancel.set()
        if self.worker is not None:
            self.worker.wait(2000)
        self.ctx.dev.release(self)


class SendWindow(W.QMainWindow):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("普通发送")
        self.resize(1180, 720)
        self.tabs = W.QTabWidget()
        self.tabs.setTabsClosable(True)
        self.b_new = W.QPushButton("+")
        self.b_new.setFixedWidth(30)
        self.tabs.setCornerWidget(self.b_new, Qt.TopLeftCorner)
        self.setCentralWidget(self.tabs)
        self.b_new.clicked.connect(self.add_tab)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self._count = 0
        self.add_tab()
        ctx.dev.add_listener(self._on_dev_change)

    def add_tab(self) -> SendTab:
        tab = SendTab(self.ctx)
        self._count += 1
        self.tabs.addTab(tab, f"Tab{'' if self._count == 1 else self._count}")
        self.tabs.setCurrentIndex(self.tabs.count() - 1)
        return tab

    def _close_tab(self, i: int) -> None:
        tab = self.tabs.widget(i)
        if tab.running():
            info(self, "该页正在发送，请先停止。")
            return
        if self.tabs.count() > 1:
            tab.shutdown()
            self.tabs.removeTab(i)

    def _on_dev_change(self) -> None:
        for i in range(self.tabs.count()):
            self.tabs.widget(i).on_dev_change()

    def closeEvent(self, event) -> None:
        self.ctx.dev.remove_listener(self._on_dev_change)
        for i in range(self.tabs.count()):
            self.tabs.widget(i).shutdown()
        self.ctx.save()
        event.accept()
