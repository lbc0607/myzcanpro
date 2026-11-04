"""Small start window with the three tools (ZCANPro: 高级功能)."""
import os
import time

from zdiag.busstats import ChannelCfg
from zdiag.devmgr import ConnError, channel_count, find_zlgcan, has_terminator, is_fd_device
from zdiag.project import DEVICES
from .appctx import AppContext
from .common import warn
from .qt import Qt, QtCore, QtWidgets as W


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


TOOLS = [("uds", "UDS诊断"), ("flash", "ECU刷新"), ("scan", "软件扫描"), ("send", "普通发送"),
         ("monitor", "报文监视"), ("crc", "校验码计算工具")]
TITLES = dict(TOOLS)


class FloatHost(W.QWidget):
    """A tool undocked from the tab bar: its own top-level window with a 'dock back' button."""

    def __init__(self, shell: "LauncherWindow", key: str, widget, title: str) -> None:
        super().__init__()
        self.shell, self.key, self.widget_, self.released = shell, key, widget, False
        self.setWindowFlags(Qt.Window)
        self.setWindowTitle(f"{title} - myzcanpro")
        self.resize(1180, 720)
        v = W.QVBoxLayout(self)
        v.setContentsMargins(2, 2, 2, 2)
        bar = W.QHBoxLayout()
        self.b_dock = W.QPushButton("⇲ 停靠回主窗口")
        bar.addWidget(self.b_dock)
        bar.addStretch(1)
        v.addLayout(bar)
        v.addWidget(widget, 1)
        widget.show()
        self.b_dock.clicked.connect(lambda: shell.dock_tool(key))

    def release(self):
        self.released = True
        self.layout().removeWidget(self.widget_)
        self.widget_.setParent(None)
        return self.widget_

    def closeEvent(self, event) -> None:
        if self.released or self.shell.close_tool(self.key):
            event.accept()
        else:
            event.ignore()


class LauncherWindow(W.QMainWindow):
    """Main window: menu bar on top, tools open as tabs (tab bar at the bottom); a tab can be undocked."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.windows = {}            # tool key -> widget (docked in a tab or inside a FloatHost)
        self.floating = {}           # tool key -> FloatHost
        self.setWindowTitle("myzcanpro")
        self.resize(1280, 780)
        root = W.QWidget()
        self.setCentralWidget(root)
        outer = W.QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # ---- menu bar (top)
        bar = W.QFrame()
        bar.setObjectName("menubar")
        bh = W.QHBoxLayout(bar)
        bh.setContentsMargins(8, 0, 8, 0)
        bh.setSpacing(2)
        brand = W.QLabel("myzcanpro")
        brand.setObjectName("brand")
        bh.addWidget(brand)
        self.b_home = W.QPushButton("设备管理")
        self.b_uds, self.b_flash, self.b_scan = W.QPushButton("UDS诊断"), W.QPushButton("ECU刷新"), W.QPushButton("软件扫描")
        self.b_send, self.b_mon, self.b_crc = W.QPushButton("普通发送"), W.QPushButton("报文监视"), W.QPushButton("校验码计算工具")
        for b in (self.b_home, self.b_uds, self.b_flash, self.b_scan, self.b_send, self.b_mon, self.b_crc):
            bh.addWidget(b)
        bh.addStretch(1)
        outer.addWidget(bar)
        # ---- tabs (bottom): tab 0 is the device manager and cannot be closed
        self.tabs = W.QTabWidget()
        self.tabs.setTabPosition(W.QTabWidget.South)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        outer.addWidget(self.tabs, 1)
        home = W.QWidget()
        v = W.QVBoxLayout(home)
        self._build_device_panel(v)
        self.l_info = W.QLabel("")
        v.addWidget(self.l_info)
        v.addStretch(1)
        self.tabs.addTab(home, "设备管理")
        self.tabs.tabBar().setTabButton(0, W.QTabBar.RightSide, None)
        self.b_float = W.QPushButton("⇱ 浮动")
        self.b_float.setToolTip("把当前标签页变成独立窗口（也可双击标签）")
        self.tabs.setCornerWidget(self.b_float, Qt.BottomRightCorner)
        self.tabs.tabCloseRequested.connect(self._close_tab_at)
        self.tabs.tabBarDoubleClicked.connect(self._float_at)
        self.b_float.clicked.connect(lambda: self._float_at(self.tabs.currentIndex()))
        self.b_home.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        for key, b in (("uds", self.b_uds), ("flash", self.b_flash), ("scan", self.b_scan), ("send", self.b_send),
                       ("monitor", self.b_mon), ("crc", self.b_crc)):
            b.clicked.connect(lambda _=False, k=key: self._open(k))
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
            dev.start_channel(n, cfg, self.ctx.diag_cfg.addr, self.ctx.channel.sim_profile,
                              self.ctx.diag_cfg.settings.rx_padding)
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

    # ------------------------------------------------------------------ tools as tabs
    def _make_tool(self, which: str):
        if which == "uds":
            from .diag_window import DiagWindow
            return DiagWindow(self.ctx)
        if which == "scan":
            from .scan_window import ScanWindow
            return ScanWindow(self.ctx)
        if which == "monitor":
            from .monitor_window import MonitorWindow
            return MonitorWindow(self.ctx)
        if which == "send":
            from .send_window import SendWindow
            return SendWindow(self.ctx)
        if which == "flash":
            from .flash_window import FlashWindow
            return FlashWindow(self.ctx)
        from .dialogs import CrcToolDialog
        d = CrcToolDialog(self.ctx)
        d.setWindowFlags(Qt.Widget)                    # a dialog would otherwise stay a separate window
        return d

    def _open(self, which: str) -> None:
        """Open the tool in a new tab (or focus its tab / floating window if it is already open)."""
        win = self.windows.get(which)
        if win is None:
            win = self._make_tool(which)
            self.windows[which] = win
            self.tabs.addTab(win, TITLES[which])
        host = self.floating.get(which)
        if host is not None:
            host.show()
            host.raise_()
            host.activateWindow()
            return
        self.tabs.setCurrentIndex(self.tabs.indexOf(win))
        win.show()

    def _key_of_tab(self, i: int):
        w = self.tabs.widget(i)
        for k, v in self.windows.items():
            if v is w and k not in self.floating:
                return k
        return None

    def _close_tab_at(self, i: int) -> None:
        k = self._key_of_tab(i)
        if k is not None:
            self.close_tool(k)

    def _float_at(self, i: int) -> None:
        k = self._key_of_tab(i)
        if k is not None:
            self.float_tool(k)

    def close_tool(self, key: str) -> bool:
        """Close a tool (tab or floating window). False when it vetoed (e.g. a flash is running)."""
        w = self.windows.get(key)
        if w is None:
            return True
        if not w.close():
            return False
        host = self.floating.pop(key, None)
        if host is not None:
            host.released = True
            host.close()
            host.deleteLater()
        else:
            i = self.tabs.indexOf(w)
            if i >= 0:
                self.tabs.removeTab(i)
        w.deleteLater()
        del self.windows[key]
        return True

    def float_tool(self, key: str) -> None:
        w = self.windows.get(key)
        if w is None or key in self.floating:
            return
        i = self.tabs.indexOf(w)
        title = TITLES[key]
        if i >= 0:
            self.tabs.removeTab(i)
        host = FloatHost(self, key, w, title)
        self.floating[key] = host
        host.show()

    def dock_tool(self, key: str) -> None:
        host = self.floating.pop(key, None)
        if host is None:
            return
        w = host.release()
        host.close()
        host.deleteLater()
        self.tabs.addTab(w, TITLES[key])
        self.tabs.setCurrentIndex(self.tabs.indexOf(w))
        w.show()

    def closeEvent(self, event) -> None:
        for key in list(self.windows):
            if not self.close_tool(key):               # the tool vetoed closing (e.g. flashing)
                event.ignore()
                return
        self._clock.stop()
        self.ctx.dev.shutdown()
        self.ctx.save()
        event.accept()
