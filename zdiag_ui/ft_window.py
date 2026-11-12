"""文件传输 window: send / receive files and whole folders over 1~2 CAN / CAN FD channels (zdiag/cft.py via the ops layer)."""
from typing import Dict, List

from zdiag.ops import Ops, OpsError
from .common import warn
from .qt import Qt, QtCore, QtWidgets as W


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


STATE = {"idle": "空闲", "waiting": "等待发送方", "connecting": "连接中", "sending": "发送中", "receiving": "接收中", "done": "完成",
         "done_with_errors": "完成（有失败）", "error": "出错", "stopped": "已停止"}


class FileTransferWindow(W.QWidget):
    def __init__(self, ctx) -> None:
        super().__init__()
        self.ctx = ctx
        self.ops = Ops(ctx)
        self.role = "send"
        self._checked: Dict[int, bool] = {}
        self._boxes: Dict[int, W.QCheckBox] = {}
        self._last_line = ""
        self._build()
        self._on_dev_change()
        self._role_changed()
        ctx.dev.add_listener(self._on_dev_change)
        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(200)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        v = W.QVBoxLayout(self)
        g = W.QGroupBox("设置")
        gl = W.QGridLayout(g)
        self.c_role = W.QComboBox()
        self.c_role.addItem("发送文件 / 文件夹", "send")
        self.c_role.addItem("接收", "receive")
        self.e_id, self.e_chunk = W.QLineEdit("6A0"), W.QLineEdit("1024")
        gl.addWidget(W.QLabel("角色:"), 0, 0)
        gl.addWidget(self.c_role, 0, 1)
        gl.addWidget(W.QLabel("CAN ID 起始(0x，占用连续 3 个):"), 0, 2)
        gl.addWidget(self.e_id, 0, 3)
        gl.addWidget(W.QLabel("块大小(字节):"), 0, 4)
        gl.addWidget(self.e_chunk, 0, 5)
        self.box_row = W.QHBoxLayout()
        self.l_nochan = W.QLabel("（没有已启动的通道：先在设备管理里启动；选两个通道即双通道传输，一条断线自动改走另一条）")
        self.box_row.addWidget(W.QLabel("通道(最多 2 个):"))
        gl.addLayout(self.box_row, 1, 0, 1, 6)
        gl.addWidget(self.l_nochan, 2, 0, 1, 6)
        v.addWidget(g)

        self.g_send = W.QGroupBox("要发送的文件 / 文件夹（文件夹会连同子目录整个发送）")
        sl = W.QVBoxLayout(self.g_send)
        self.lst = W.QListWidget()
        sl.addWidget(self.lst, 1)
        bl = W.QHBoxLayout()
        self.b_addf, self.b_addd = W.QPushButton("添加文件"), W.QPushButton("添加文件夹")
        self.b_rm, self.b_clr = W.QPushButton("移除选中"), W.QPushButton("清空")
        for b in (self.b_addf, self.b_addd, self.b_rm, self.b_clr):
            bl.addWidget(b)
        bl.addStretch(1)
        sl.addLayout(bl)
        v.addWidget(self.g_send, 1)

        self.g_recv = W.QGroupBox("接收")
        rl = W.QGridLayout(self.g_recv)
        self.e_dest, self.b_dest = W.QLineEdit(), W.QPushButton("浏览")
        self.k_over, self.k_loop = W.QCheckBox("覆盖已有的不同文件"), W.QCheckBox("一直监听（不勾选=接收完一次就结束）")
        self.k_over.setChecked(True)
        rl.addWidget(W.QLabel("保存到:"), 0, 0)
        rl.addWidget(self.e_dest, 0, 1)
        rl.addWidget(self.b_dest, 0, 2)
        rl.addWidget(self.k_over, 1, 1)
        rl.addWidget(self.k_loop, 2, 1)
        v.addWidget(self.g_recv)

        row = W.QHBoxLayout()
        self.b_start, self.b_stop = W.QPushButton("开始"), W.QPushButton("停止")
        self.b_stop.setEnabled(False)
        row.addWidget(self.b_start)
        row.addWidget(self.b_stop)
        row.addWidget(W.QLabel("中断后再次发送同一文件/文件夹会自动续传"))
        row.addStretch(1)
        v.addLayout(row)
        self.bar = W.QProgressBar()
        self.bar.setMaximum(1000)
        self.l_state, self.l_links = W.QLabel("空闲"), W.QLabel("")
        v.addWidget(self.bar)
        v.addWidget(self.l_state)
        v.addWidget(self.l_links)
        self.log = W.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        v.addWidget(self.log, 1)

        self.c_role.currentIndexChanged.connect(self._role_changed)
        self.b_addf.clicked.connect(self._add_file)
        self.b_addd.clicked.connect(self._add_dir)
        self.b_rm.clicked.connect(self._remove)
        self.b_clr.clicked.connect(self.lst.clear)
        self.b_dest.clicked.connect(self._pick_dest)
        self.b_start.clicked.connect(self._start)
        self.b_stop.clicked.connect(self._stop)

    def _role_changed(self) -> None:
        self.role = self.c_role.currentData() or "send"
        self.g_send.setVisible(self.role == "send")
        self.g_recv.setVisible(self.role == "receive")
        self.b_start.setText("开始发送" if self.role == "send" else "开始接收")

    def _on_dev_change(self) -> None:
        for n, b in self._boxes.items():
            self._checked[n] = b.isChecked()
        for b in self._boxes.values():
            self.box_row.removeWidget(b)
            b.setParent(None)
        self._boxes = {}
        started = self.ctx.dev.started()
        for n in started:
            b = W.QCheckBox(f"通道{n} ({self.ctx.dev.cfg_of(n).summary()})")
            b.setChecked(self._checked.get(n, len(self._boxes) < 1))
            self.box_row.addWidget(b)
            self._boxes[n] = b
        self.l_nochan.setVisible(not started)

    # ------------------------------------------------------------------ actions
    def _add_file(self) -> None:
        f, _ = W.QFileDialog.getOpenFileName(self, "选择要发送的文件", self.ctx.last_dir, "所有文件 (*.*)")
        if f:
            self.lst.addItem(f)

    def _add_dir(self) -> None:
        d = W.QFileDialog.getExistingDirectory(self, "选择要发送的文件夹", self.ctx.last_dir)
        if d:
            self.lst.addItem(d)

    def _remove(self) -> None:
        r = self.lst.currentRow()
        if r >= 0:
            self.lst.takeItem(r)

    def _pick_dest(self) -> None:
        d = W.QFileDialog.getExistingDirectory(self, "选择保存文件夹", self.e_dest.text().strip() or self.ctx.last_dir)
        if d:
            self.e_dest.setText(d)

    def selected_channels(self) -> List[int]:
        return [n for n, b in self._boxes.items() if b.isChecked()]

    def running(self) -> bool:
        return any(j.running() for j in self.ops._ft.values())

    def _start(self) -> None:
        chans = self.selected_channels()
        if not chans or len(chans) > 2:
            warn(self, "请选择 1~2 个已启动的通道。")
            return
        try:
            chunk = int(self.e_chunk.text().strip())
        except ValueError:
            warn(self, "块大小应为整数。")
            return
        try:
            if self.role == "send":
                paths = [self.lst.item(i).text() for i in range(self.lst.count())]
                if not paths:
                    warn(self, "请先添加要发送的文件或文件夹。")
                    return
                self.ops.ft_send(chans, paths, base_id=self.e_id.text().strip(), chunk_bytes=chunk)
            else:
                dest = self.e_dest.text().strip()
                if not dest:
                    warn(self, "请选择保存文件夹。")
                    return
                self.ops.ft_receive(chans, dest, base_id=self.e_id.text().strip(), overwrite=self.k_over.isChecked(),
                                    once=not self.k_loop.isChecked(), chunk_bytes=chunk)
        except OpsError as e:
            warn(self, str(e))
            return
        self._last_line = ""
        self.log.clear()
        self._tick()

    def _stop(self) -> None:
        self.ops.ft_stop(self.role, wait=0)                 # flag only: the window never waits for the worker threads
        self.b_stop.setEnabled(False)
        self._tick()

    # ------------------------------------------------------------------ progress
    def _tick(self) -> None:
        job = self.ops._ft.get(self.role)
        run = self.running()
        self.b_start.setEnabled(not run and not (job and job.running()))
        self.b_stop.setEnabled(bool(job and job.running()))
        for w in (self.c_role, self.e_id, self.e_chunk):
            w.setEnabled(not run)
        if job is None:
            return
        st = job.status()
        total = max(st["bytes_total"], 1)
        self.bar.setValue(int(1000 * min(st["bytes_done"], total) / total))
        txt = f"{STATE.get(st['state'], st['state'])}  {human(st['bytes_done'])} / {human(st['bytes_total'])}  速度 {human(st['speed_bps'])}/s  " \
              f"文件 {st['files_done'] + st['files_skipped']}/{st['files_total']}"
        if st["file"]:
            txt += f"  当前: {st['file']}"
        if st["error"]:
            txt += f"\n{st['error']}"
        if st["failed"]:
            txt += f"\n失败: {', '.join(st['failed'][:5])}"
        self.l_state.setText(txt)
        self.l_links.setText("    ".join(f"{l['name']}: {'在线' if l['alive'] else '离线'}  {l.get('tx_frames', 0)}帧  错误{l.get('errors', 0)}  总线错误帧{l.get('bus_errors', 0)}"
                                         + (f"  降速{l['backoff_ms']}ms" if l.get('backoff_ms') else "")
                                         for l in st["links"]))
        lines = st["log"]
        if lines and lines[-1] != self._last_line:
            at = max((i for i, ln in enumerate(lines) if ln == self._last_line), default=-1)
            for ln in lines[at + 1:]:
                self.log.appendPlainText(ln)
            self._last_line = lines[-1]

    def closeEvent(self, event) -> None:
        self._clock.stop()                                   # closing always wins; partial files stay for resuming
        self.ctx.dev.remove_listener(self._on_dev_change)
        self.ops.ft_stop(wait=0)
        event.accept()
