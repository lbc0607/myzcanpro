"""报文监视 window: live bus view fed by the passive FrameTap (works while UDS / flashing / sending run)."""
import time

from zdiag.monitor import TX
from .common import info, save_file, warn
from .qt import Qt, QtCore, QtGui, QtWidgets as W

HEADERS = ["序号", "时间标识(s)", "源通道", "帧ID", "CAN类型", "方向", "长度", "数据", "计数", "时间差(ms)"]
FILTER = "CSV 文件 (*.csv);;所有文件 (*.*)"
DELTA_MODES = ["与同ID上一帧的时间差", "与上一帧(任意ID)的时间差"]


class MonitorWindow(W.QMainWindow):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.tap = ctx.dev.tap
        self.setWindowTitle("报文监视")
        self._red = QtGui.QBrush(QtGui.QColor("#C0392B"))
        self._black = QtGui.QBrush(QtGui.QColor("#000000"))
        self.resize(1100, 640)
        self._started_key = None
        self._build()
        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self.refresh)
        self._clock.start(100)
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = W.QWidget()
        self.setCentralWidget(root)
        v = W.QVBoxLayout(root)
        top = W.QHBoxLayout()
        self.c_ch = W.QComboBox()
        self.c_ch.setMinimumWidth(130)
        self.b_rec, self.b_save = W.QPushButton("实时保存"), W.QPushButton("保存")
        self.b_clear, self.b_pause = W.QPushButton("清空"), W.QPushButton("暂停")
        self.k_by_id = W.QCheckBox("按ID覆盖显示")
        self.k_by_id.setChecked(True)
        self.k_err = W.QCheckBox("显示错误帧")
        self.k_err.setChecked(True)
        self.c_delta = W.QComboBox()
        self.c_delta.addItems(DELTA_MODES)
        for wd in (W.QLabel("通道:"), self.c_ch, self.b_rec, self.b_save, self.b_clear, self.b_pause, self.k_by_id, self.k_err,
                   W.QLabel("时间差:"), self.c_delta):
            top.addWidget(wd)
        top.addStretch(1)
        v.addLayout(top)

        flt = W.QHBoxLayout()
        self.e_id, self.e_data = W.QLineEdit(), W.QLineEdit()
        self.e_id.setPlaceholderText("帧ID(16进制,可部分)")
        self.e_data.setPlaceholderText("数据包含(16进制)")
        self.c_type, self.c_dir = W.QComboBox(), W.QComboBox()
        self.c_type.addItems(["全部", "CAN", "CAN FD"])
        self.c_dir.addItems(["全部", "发送", "接收"])
        for wd in (W.QLabel("筛选  帧ID:"), self.e_id, W.QLabel("CAN类型:"), self.c_type, W.QLabel("方向:"), self.c_dir,
                   W.QLabel("数据:"), self.e_data):
            flt.addWidget(wd)
        flt.addStretch(1)
        v.addLayout(flt)

        self.table = W.QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        for i, w in enumerate((60, 100, 70, 100, 80, 60, 50, 330, 70, 100)):
            self.table.setColumnWidth(i, w)
        v.addWidget(self.table, 1)

        bot = W.QHBoxLayout()
        self.l_state = W.QLabel("")
        self.l_rx, self.l_tx, self.l_err = W.QLabel("接收帧计数: 0"), W.QLabel("发送帧计数: 0"), W.QLabel("错误帧计数: 0")
        bot.addWidget(self.l_state, 1)
        for wd in (self.l_rx, self.l_tx, self.l_err):
            bot.addWidget(wd)
        v.addLayout(bot)

        self.b_rec.clicked.connect(self._toggle_record)
        self.b_save.clicked.connect(self._save)
        self.b_clear.clicked.connect(self._clear)
        self.b_pause.clicked.connect(self._toggle_pause)
        self.paused = False

    # ------------------------------------------------------------------ actions
    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.b_pause.setText("继续" if self.paused else "暂停")
        if not self.paused:
            self.refresh()

    def _clear(self) -> None:
        self.tap.reset()
        self.refresh(force=True)

    def _toggle_record(self) -> None:
        if self.tap.recorder is not None:
            rec = self.tap.stop_record()
            info(self, f"已停止实时保存，共写入 {rec.written} 帧" + (f"（积压丢弃 {rec.dropped} 帧）" if rec.dropped else "")
                 + f"\n{rec.path}")
        else:
            path = save_file(self, self.ctx, "实时保存报文", FILTER)
            if not path:
                return
            try:
                self.tap.start_record(path)
            except OSError as e:
                warn(self, f"无法创建文件: {e}")
                return
        self.refresh(force=True)

    def _save(self) -> None:
        path = save_file(self, self.ctx, "保存当前显示", FILTER)
        if not path:
            return
        try:
            n = self.tap.save_snapshot(path, trace=not self.k_by_id.isChecked())
        except OSError as e:
            warn(self, f"保存失败: {e}")
            return
        info(self, f"已保存 {n} 行\n{path}")

    # ------------------------------------------------------------------ refresh
    def _sync_channels(self) -> None:
        started = self.ctx.dev.started()
        if started == self._started_key:
            return
        self._started_key = started
        keep = self.c_ch.currentData()
        self.c_ch.blockSignals(True)
        self.c_ch.clear()
        self.c_ch.addItem("全部通道", -1)
        for n in started:
            self.c_ch.addItem(f"通道{n}", n)
        i = self.c_ch.findData(keep if keep is not None else -1)
        self.c_ch.setCurrentIndex(i if i >= 0 else 0)
        self.c_ch.blockSignals(False)

    def _row_ok(self, r) -> bool:
        if r.err:
            return self.k_err.isChecked() and (self.c_ch.currentData() in (None, -1) or r.ch == self.c_ch.currentData())
        ch = self.c_ch.currentData()
        if ch is not None and ch >= 0 and r.ch != ch:
            return False
        t = self.c_type.currentText()
        if t != "全部" and r.type_text != t:
            return False
        d = self.c_dir.currentText()
        if d != "全部" and (r.dir == TX) != (d == "发送"):
            return False
        f = self.e_id.text().strip().replace("0x", "").replace("0X", "").upper()
        if f and f not in r.id_text:
            return False
        g = self.e_data.text().strip().upper().replace(" ", "")
        if g and g not in r.data.hex().upper():
            return False
        return True

    def refresh(self, force: bool = False) -> None:
        self._sync_channels()
        rec = self.tap.recorder
        self.b_rec.setText("停止保存" if rec is not None else "实时保存")
        self.l_state.setText(f"正在保存: {rec.path} ({rec.written} 帧)" if rec is not None else "")
        rx = tx = err = 0
        for n in self.ctx.dev.started():
            st = self.ctx.dev.stats_of(n)
            if st is not None:
                a, b, c = st.snapshot()
                rx, tx, err = rx + a, tx + b, err + c
        self.l_rx.setText(f"接收帧计数: {rx}")
        self.l_tx.setText(f"发送帧计数: {tx}")
        self.l_err.setText(f"错误帧计数: {err}")
        if self.paused and not force:
            return
        by_id = self.k_by_id.isChecked()
        rows = [r for r in (self.tap.snapshot() if by_id else self.tap.trace(1000)) if self._row_ok(r)]
        use_prev = self.c_delta.currentIndex() == 1
        t0 = self.tap.t0
        tb = self.table
        tb.setUpdatesEnabled(False)
        tb.setRowCount(len(rows))
        for i, r in enumerate(rows):
            dt = (r.dt_prev if use_prev else r.dt_id) * 1000.0
            cells = [str(r.seq if by_id else i + 1), f"{r.ts - t0:.6f}", str(r.ch), r.id_text, r.type_text,
                     "发送" if r.dir == TX else "接收", "" if r.err else str(len(r.data)), r.data_text,
                     str(r.count) if by_id else "", f"{dt:.3f}"]
            for c, text in enumerate(cells):
                it = tb.item(i, c)
                if it is None:
                    it = W.QTableWidgetItem(text)
                    tb.setItem(i, c, it)
                elif it.text() != text:
                    it.setText(text)
                it.setForeground(self._red if r.err else self._black)
        tb.setUpdatesEnabled(True)

    def closeEvent(self, event) -> None:
        self._clock.stop()
        event.accept()
