"""Small start window with the three tools (ZCANPro: 高级功能)."""
import os
import time

from zdiag.busstats import ChannelCfg
from zdiag.devmgr import ConnError, channel_count, find_zlgcan, has_terminator, is_fd_device
from zdiag.project import DEVICES
from .appctx import AppContext
from .common import warn
from .qt import QtCore, QtWidgets as W


class ChannelPanel(W.QFrame):
    """One channel row: [启动/停止] toggle, configuration summary and live counters."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.n = n
        self.setObjectName("panel")
        h = W.QHBoxLayout(self)
        self.l_name = W.QLabel(f"通道{n}")
        self.b_toggle = W.QPushButton("启动")
        self.b_toggle.setFixedWidth(70)
        self.l_state = W.QLabel("未启动")
        self.l_rx, self.l_tx, self.l_err, self.l_load = W.QLabel("接收: 0"), W.QLabel("发送: 0"), W.QLabel("错误: 0"), W.QLabel("总线负载: --")
        h.addWidget(self.l_name)
        h.addWidget(self.b_toggle)
        h.addWidget(self.l_state, 1)
        for lb in (self.l_rx, self.l_tx, self.l_err, self.l_load):
            lb.setMinimumWidth(96)
            h.addWidget(lb)


class LauncherWindow(W.QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.windows = {}
        self.setWindowTitle("myzcanpro - 高级功能")
        self.resize(640, 460)
        root = W.QWidget()
        self.setCentralWidget(root)
        v = W.QVBoxLayout(root)
        title = W.QLabel("myzcanpro 诊断工具")
        v.addWidget(title)
        self._build_device_panel(v)
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

    # ------------------------------------------------------------------ 设备管理
    def _build_device_panel(self, v) -> None:
        g = W.QGroupBox("设备管理")
        gl = W.QVBoxLayout(g)
        top = W.QHBoxLayout()
        self.c_device = W.QComboBox()
        self.c_device.addItems(DEVICES)
        self.s_index = W.QSpinBox()
        self.s_index.setRange(0, 31)
        self.b_device = W.QPushButton("打开")
        self.b_device.setFixedWidth(80)
        self.l_dev_state = W.QLabel("设备未打开")
        for wd in (W.QLabel("设备:"), self.c_device, W.QLabel("索引:"), self.s_index, self.b_device, self.l_dev_state):
            top.addWidget(wd)
        top.addStretch(1)
        gl.addLayout(top)
        self.chan_box = W.QWidget()
        self.chan_layout = W.QVBoxLayout(self.chan_box)
        self.chan_layout.setContentsMargins(0, 0, 0, 0)
        gl.addWidget(self.chan_box)
        self.panels = []
        self._load_t = {}                       # channel -> time of the last bus-load sample
        v.addWidget(g)
        sel = self.ctx.devsel
        self.c_device.setCurrentText(sel.get("device", ""))
        self.s_index.setValue(int(sel.get("index", 0)))
        self.b_device.clicked.connect(self._toggle_device)
        self.ctx.dev.add_listener(self._refresh_device)
        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self.refresh_stats)
        self._clock.start(100)
        self._refresh_device()

    def _toggle_device(self) -> None:
        dev = self.ctx.dev
        try:
            if dev.is_open:
                dev.close()
            else:
                self.ctx.devsel = {"device": self.c_device.currentText(), "index": self.s_index.value()}
                dev.open(self.ctx.devsel["device"], self.ctx.devsel["index"])
                self.ctx.save()
        except ConnError as e:
            warn(self, str(e))

    def _toggle_channel(self, n: int) -> None:
        dev = self.ctx.dev
        try:
            if n in dev.chans:
                dev.stop_channel(n)
                return
            from .dialogs import ChannelConfigDialog
            cfg = self.ctx.chan_cfgs.get(n, ChannelCfg())
            if not is_fd_device(dev.name):
                cfg.protocol = "CAN"
            dlg = ChannelConfigDialog(dev.name, n, cfg, self)
            if not dlg.exec_() or dlg.result_cfg is None:
                return
            cfg = dlg.result_cfg
            dev.start_channel(n, cfg, self.ctx.diag_cfg.addr, self.ctx.channel.sim_profile)
            self.ctx.chan_cfgs[n] = cfg
            self.ctx.save()
        except ConnError as e:
            warn(self, str(e))

    def _refresh_device(self) -> None:
        """Re-draw button/labels/channel rows from the device manager state (called on every state change)."""
        dev = self.ctx.dev
        opened = dev.is_open
        self.b_device.setText("关闭" if opened else "打开")
        self.c_device.setEnabled(not opened)
        self.s_index.setEnabled(not opened)
        self.l_dev_state.setText(f"已打开: {dev.name} #{dev.index}" if opened else "设备未打开")
        want = channel_count(dev.name) if opened else 0
        while len(self.panels) < want:
            p = ChannelPanel(len(self.panels))
            p.b_toggle.clicked.connect(lambda _=False, n=p.n: self._toggle_channel(n))
            self.chan_layout.addWidget(p)
            self.panels.append(p)
        for p in self.panels:
            p.setVisible(p.n < want)
            cfg = dev.cfg_of(p.n)
            p.b_toggle.setText("停止" if cfg else "启动")
            p.l_state.setText(cfg.summary() if cfg else "未启动")
            if not cfg:
                self._load_t.pop(p.n, None)
                for lb, t in ((p.l_rx, "接收: 0"), (p.l_tx, "发送: 0"), (p.l_err, "错误: 0"), (p.l_load, "总线负载: --")):
                    lb.setText(t)
        self.refresh_stats()

    def refresh_stats(self) -> None:
        now = time.monotonic()
        for p in self.panels:
            st, cfg = self.ctx.dev.stats_of(p.n), self.ctx.dev.cfg_of(p.n)
            if st is None or cfg is None:
                continue
            rx, tx, err = st.snapshot()
            p.l_rx.setText(f"接收: {rx}")
            p.l_tx.setText(f"发送: {tx}")
            p.l_err.setText(f"错误: {err}")
            if cfg.calc_load:
                last = self._load_t.get(p.n)
                if last is None or (now - last) * 1000 >= cfg.load_interval_ms:
                    self._load_t[p.n] = now
                    load = st.sample_load(now)
                    p.l_load.setText("总线负载: --" if load is None else f"总线负载: {load:.1f}%")
            else:
                p.l_load.setText("总线负载: 未启用")

    def _status(self) -> None:
        dll = find_zlgcan(self.ctx.dir)
        kd = os.path.join(os.path.dirname(dll), "kerneldlls")
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
        self._clock.stop()
        self.ctx.dev.shutdown()
        self.ctx.save()
        event.accept()
