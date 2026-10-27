"""Dialogs: 更多设置, CRC configuration widget + 校验码计算工具, 服务编辑器."""
import os
from typing import Dict, List, Optional

from zdiag.crc import CUSTOM, PRESETS, CrcSpec, crc_calc, preset_names
from zdiag.flash import load_image
from zdiag.project import DEVICES, SIM_DEVICE, Channel, DiagConfig, parse_hex, parse_id
from .common import hex_edit, info, int_of, pick_file, warn
from .qt import Qt, QtWidgets as W


# =============================================================================== 更多设置
class SettingsDialog(W.QDialog):
    """Tabs 常规设置 / 高级设置 (as in ZCANPro) plus 通道设置 / 安全访问 (needed by this tool)."""

    def __init__(self, ctx, cfg: DiagConfig, channel: Channel, parent=None, with_channel: bool = True) -> None:
        super().__init__(parent)
        self.ctx, self.cfg, self.channel = ctx, cfg, channel
        self.setWindowTitle("参数设置")
        self.resize(760, 560)
        v = W.QVBoxLayout(self)
        self.tabs = W.QTabWidget()
        v.addWidget(self.tabs)
        self.tabs.addTab(self._general_tab(), "常规设置")
        self.tabs.addTab(self._advanced_tab(), "高级设置")
        if with_channel:
            self.tabs.addTab(self._channel_tab(), "通道设置")
        self.tabs.addTab(self._security_tab(), "安全访问")
        btns = W.QHBoxLayout()
        btns.addStretch(1)
        self.btn_ok, self.btn_cancel = W.QPushButton("确定"), W.QPushButton("取消")
        self.btn_ok.setFixedWidth(120)
        self.btn_cancel.setFixedWidth(120)
        btns.addWidget(self.btn_ok)
        btns.addWidget(self.btn_cancel)
        v.addLayout(btns)
        self.btn_ok.clicked.connect(self._on_ok)
        self.btn_cancel.clicked.connect(self.reject)
        self.with_channel = with_channel
        self._load()

    # -- tabs
    def _general_tab(self):
        w = W.QWidget()
        v = W.QVBoxLayout(w)
        g = W.QGroupBox("地址")
        gl = W.QGridLayout(g)
        self.e_phys, self.e_resp, self.e_func = W.QLineEdit(), W.QLineEdit(), W.QLineEdit()
        self.c_ext = W.QCheckBox("扩展帧")
        gl.addWidget(W.QLabel("物理地址(0x):"), 0, 0)
        gl.addWidget(self.e_phys, 0, 1)
        gl.addWidget(W.QLabel("响应地址(0x):"), 0, 2)
        gl.addWidget(self.e_resp, 0, 3)
        gl.addWidget(W.QLabel("功能地址(0x):"), 1, 0)
        gl.addWidget(self.e_func, 1, 1)
        gl.addWidget(self.c_ext, 1, 2)
        v.addWidget(g)
        g2 = W.QGroupBox("会话保持")
        g2l = W.QGridLayout(g2)
        self.k_addr = W.QComboBox()
        self.k_addr.addItems(["功能地址", "物理地址"])
        self.k_on = W.QCheckBox("启动")
        self.k_period = W.QLineEdit()
        self.k_sup = W.QCheckBox("抑制响应")
        g2l.addWidget(W.QLabel("地址:"), 0, 0)
        g2l.addWidget(self.k_addr, 0, 1)
        g2l.addWidget(self.k_on, 0, 2)
        g2l.addWidget(W.QLabel("发送周期(ms):"), 1, 0)
        g2l.addWidget(self.k_period, 1, 1)
        g2l.addWidget(self.k_sup, 2, 0)
        v.addWidget(g2)
        v.addStretch(1)
        return w

    def _advanced_tab(self):
        w = W.QWidget()
        v = W.QVBoxLayout(w)
        g = W.QGroupBox("传输层")
        gl = W.QGridLayout(g)
        self.a_proto = W.QComboBox()
        self.a_proto.addItems(["ISO15765-2(2004)", "ISO15765-2(2016)"])
        self.a_pad_on, self.a_pad = W.QCheckBox("填充字节(0x):"), W.QLineEdit()
        self.a_p2, self.a_p2s = W.QLineEdit(), W.QLineEdit()
        self.a_stmin, self.a_bs = W.QLineEdit(), W.QLineEdit()
        self.a_ovr_on, self.a_ovr = W.QCheckBox("替换远端STmin(ms):"), W.QLineEdit()
        self.a_retry, self.a_interval, self.a_fc = W.QLineEdit(), W.QLineEdit(), W.QLineEdit()
        self.a_supwait, self.a_anyneg = W.QCheckBox("抑制响应等待"), W.QCheckBox("判断任意消极响应")
        rows = [
            (W.QLabel("传输协议:"), self.a_proto, self.a_pad_on, self.a_pad),
            (W.QLabel("P2超时(ms):"), self.a_p2, W.QLabel("P2*超时(ms):"), self.a_p2s),
            (W.QLabel("本地STmin(ms):"), self.a_stmin, W.QLabel("BS:"), self.a_bs),
            (self.a_ovr_on, self.a_ovr, W.QLabel("下载文件重试次数:"), self.a_retry),
            (W.QLabel("文件下载$36间隔(ms):"), self.a_interval, W.QLabel("流控超时(ms):"), self.a_fc),
            (self.a_supwait, None, self.a_anyneg, None),
        ]
        for r, row in enumerate(rows):
            for c, wd in enumerate(row):
                if wd is not None:
                    gl.addWidget(wd, r, c)
        self.a_ovr_on.toggled.connect(lambda on: self.a_ovr.setEnabled(on))
        self.a_pad_on.toggled.connect(lambda on: self.a_pad.setEnabled(on))
        v.addWidget(g)
        g2 = W.QGroupBox("输出信息")
        g2l = W.QGridLayout(g2)
        self.o_max, self.o_save = W.QLineEdit(), W.QLineEdit()
        self.o_raw = W.QCheckBox("显示原始帧")
        g2l.addWidget(W.QLabel("输出信息最大行数:"), 0, 0)
        g2l.addWidget(self.o_max, 0, 1)
        g2l.addWidget(W.QLabel("实时保存单个文件的最大行数:"), 1, 0)
        g2l.addWidget(self.o_save, 1, 1)
        g2l.addWidget(self.o_raw, 2, 0)
        v.addWidget(g2)
        v.addStretch(1)
        return w

    def _channel_tab(self):
        w = W.QWidget()
        g = W.QGridLayout(w)
        self.ch_dev = W.QComboBox()
        self.ch_dev.addItems(DEVICES)
        self.ch_idx, self.ch_ch = W.QLineEdit(), W.QLineEdit()
        self.ch_type = W.QComboBox()
        self.ch_type.addItems(["CAN", "CAN FD"])
        self.ch_baud, self.ch_dbaud = W.QLineEdit(), W.QLineEdit()
        self.ch_term = W.QCheckBox("使能终端电阻")
        self.ch_fddl = W.QComboBox()
        self.ch_fddl.addItems(["8", "12", "16", "20", "24", "32", "48", "64"])
        self.ch_dll, self.ch_dll_btn = W.QLineEdit(), W.QPushButton("浏览")
        self.ch_sim = W.QComboBox()
        self.ch_sim.addItem("新产品 (16字节seed, 例程 01 02)", "new")
        self.ch_sim.addItem("旧产品 (2字节seed, 例程 DF FE)", "old")
        items = [("设备:", self.ch_dev), ("设备索引:", self.ch_idx), ("通道:", self.ch_ch),
                 ("CAN类型:", self.ch_type), ("波特率(bit/s):", self.ch_baud),
                 ("数据域波特率(bit/s):", self.ch_dbaud), ("ISO-TP帧长(CAN FD):", self.ch_fddl),
                 ("zlgcan.dll 路径:", self.ch_dll), ("模拟ECU 产品:", self.ch_sim)]
        for r, (lab, wd) in enumerate(items):
            g.addWidget(W.QLabel(lab), r, 0)
            g.addWidget(wd, r, 1)
        g.addWidget(self.ch_dll_btn, 7, 2)
        g.addWidget(self.ch_term, len(items), 1)
        g.addWidget(W.QLabel("留空则使用程序目录下的 zlgcan.dll；kerneldlls 文件夹需与之放在一起。"), len(items) + 1, 0, 1, 3)
        self.ch_dll_btn.clicked.connect(self._browse_dll)
        return w

    def _security_tab(self):
        w = W.QWidget()
        v = W.QVBoxLayout(w)
        v.addWidget(W.QLabel("ECUserialnumber.txt 第2、3行按诊断ID配置，格式  ID:行2,行3 ，用分号分隔："))
        self.s_map = W.QLineEdit()
        v.addWidget(self.s_map)
        v.addWidget(W.QLabel("例: 7e2:0,0; 7e3:1,3; 7e8:2,4  （先按请求ID查找，找不到再按响应ID）"))
        v.addStretch(1)
        return w

    def _browse_dll(self):
        p = pick_file(self, self.ctx, "选择 zlgcan.dll", "DLL (*.dll);;所有文件 (*.*)")
        if p:
            self.ch_dll.setText(p)

    # -- model <-> widgets
    def _load(self) -> None:
        c, a, k, s = self.cfg, self.cfg.addr, self.cfg.keepalive, self.cfg.settings
        self.e_phys.setText(f"{a.phys:x}")
        self.e_resp.setText(f"{a.resp:x}")
        self.e_func.setText(f"{a.func:x}")
        self.c_ext.setChecked(a.extended)
        self.k_addr.setCurrentIndex(0 if k.addr == "func" else 1)
        self.k_on.setChecked(k.enabled)
        self.k_period.setText(str(k.period_ms))
        self.k_sup.setChecked(k.suppress)
        self.a_proto.setCurrentText(s.protocol)
        self.a_pad_on.setChecked(s.padding_enabled)
        self.a_pad.setText(f"{s.padding:02X}")
        self.a_pad.setEnabled(s.padding_enabled)
        self.a_p2.setText(str(s.p2_ms))
        self.a_p2s.setText(str(s.p2star_ms))
        self.a_stmin.setText(str(s.st_min_ms))
        self.a_bs.setText(str(s.bs))
        self.a_ovr_on.setChecked(s.override_stmin)
        self.a_ovr.setText(str(s.override_stmin_ms))
        self.a_ovr.setEnabled(s.override_stmin)
        self.a_retry.setText(str(s.dl_retry))
        self.a_interval.setText(str(s.block_interval_ms))
        self.a_fc.setText(str(s.fc_timeout_ms))
        self.a_supwait.setChecked(s.suppress_wait)
        self.a_anyneg.setChecked(s.any_negative)
        self.o_max.setText(str(s.max_log_lines))
        self.o_save.setText(str(s.save_max_lines))
        self.o_raw.setChecked(s.show_raw)
        self.s_map.setText("; ".join(f"{k_}:{v_[0]},{v_[1]}" for k_, v_ in c.serial_id_map.items()))
        if self.with_channel:
            ch = self.channel
            self.ch_dev.setCurrentText(ch.device)
            self.ch_idx.setText(str(ch.index))
            self.ch_ch.setText(str(ch.channel))
            self.ch_type.setCurrentText(ch.can_type)
            self.ch_baud.setText(str(ch.bitrate))
            self.ch_dbaud.setText(str(ch.data_bitrate))
            self.ch_fddl.setCurrentText(str(ch.fd_dl))
            self.ch_term.setChecked(ch.terminator)
            self.ch_dll.setText(ch.zlgcan_dll)
            self.ch_sim.setCurrentIndex(0 if ch.sim_profile == "new" else 1)

    @staticmethod
    def parse_id_map(text: str) -> Dict[str, List[int]]:
        out = {}
        for part in text.replace("；", ";").split(";"):
            part = part.strip()
            if not part:
                continue
            try:
                ident, vals = part.split(":")
                a, b = vals.replace("，", ",").split(",")
                out[f"{parse_id(ident):x}"] = [int(a), int(b)]
            except ValueError:
                raise ValueError(f"序列号文件映射格式错误: {part!r}  (应为 7e2:0,0)")
        return out

    def _on_ok(self) -> None:
        try:
            self.apply()
        except ValueError as e:
            warn(self, str(e))
            return
        self.accept()

    def apply(self) -> None:
        """Validate everything first, then commit (so a bad field never half-applies)."""
        a = (parse_id(self.e_phys.text()), parse_id(self.e_func.text()), parse_id(self.e_resp.text()))
        ext = self.c_ext.isChecked()
        for ident in a:
            if ident > (0x1FFFFFFF if ext else 0x7FF):
                raise ValueError(f"CAN ID 0x{ident:X} 超出{'扩展' if ext else '标准'}帧范围")
        k_period = int_of(self.k_period, "发送周期", 10, 600000)
        pad = int_of(self.a_pad, "填充字节", 0, 255, 16) if self.a_pad_on.isChecked() else 0
        vals = dict(
            p2_ms=int_of(self.a_p2, "P2超时", 1, 600000), p2star_ms=int_of(self.a_p2s, "P2*超时", 1, 600000),
            st_min_ms=int_of(self.a_stmin, "本地STmin", 0, 127), bs=int_of(self.a_bs, "BS", 0, 255),
            override_stmin_ms=int_of(self.a_ovr, "替换远端STmin", 0, 127),
            dl_retry=int_of(self.a_retry, "下载文件重试次数", 0, 100),
            block_interval_ms=int_of(self.a_interval, "文件下载$36间隔", 0, 60000),
            fc_timeout_ms=int_of(self.a_fc, "流控超时", 1, 60000),
            max_log_lines=int_of(self.o_max, "输出信息最大行数", 100, 100000000),
            save_max_lines=int_of(self.o_save, "实时保存单个文件的最大行数", 100, 100000000))
        id_map = self.parse_id_map(self.s_map.text())
        chv = None
        if self.with_channel:
            chv = dict(device=self.ch_dev.currentText(), index=int_of(self.ch_idx, "设备索引", 0, 63),
                       channel=int_of(self.ch_ch, "通道", 0, 15), can_type=self.ch_type.currentText(),
                       bitrate=int_of(self.ch_baud, "波特率", 1000, 8000000),
                       data_bitrate=int_of(self.ch_dbaud, "数据域波特率", 1000, 12000000),
                       fd_dl=int(self.ch_fddl.currentText()), terminator=self.ch_term.isChecked(),
                       zlgcan_dll=self.ch_dll.text().strip(), sim_profile=self.ch_sim.currentData() or "new")
        # ---- commit
        ad = self.cfg.addr
        ad.phys, ad.func, ad.resp = a
        ad.extended = ext
        ka = self.cfg.keepalive
        ka.addr = "func" if self.k_addr.currentIndex() == 0 else "phys"
        ka.enabled, ka.period_ms, ka.suppress = self.k_on.isChecked(), k_period, self.k_sup.isChecked()
        s = self.cfg.settings
        s.protocol = self.a_proto.currentText()
        s.padding_enabled, s.padding = self.a_pad_on.isChecked(), pad
        s.override_stmin = self.a_ovr_on.isChecked()
        s.suppress_wait, s.any_negative, s.show_raw = (self.a_supwait.isChecked(),
                                                       self.a_anyneg.isChecked(), self.o_raw.isChecked())
        for k_, v_ in vals.items():
            setattr(s, k_, v_)
        self.cfg.serial_id_map = id_map
        if chv:
            for k_, v_ in chv.items():
                setattr(self.channel, k_, v_)


# =============================================================================== CRC widgets
class CrcConfigWidget(W.QWidget):
    """校验类型 combo + parameter table (CRC类型 / 多项式 / 初始值 / 输出异或值 / 输入反转 / 输出反转)."""

    ROWS = ["CRC类型", "CRC位宽", "CRC多项式", "CRC初始值", "输出异或值", "输入反转", "输出反转"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        v = W.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        row = W.QHBoxLayout()
        row.addWidget(W.QLabel("校验类型:"))
        self.combo = W.QComboBox()
        self.combo.addItems(preset_names())
        row.addWidget(self.combo)
        v.addLayout(row)
        self.table = W.QTableWidget(len(self.ROWS), 2)
        self.table.setHorizontalHeaderLabels(["参数", "值"])
        self.chk_in, self.chk_out = W.QCheckBox(), W.QCheckBox()
        for r, name in enumerate(self.ROWS):
            it = W.QTableWidgetItem(name)
            it.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, 0, it)
            if name not in ("输入反转", "输出反转"):
                self.table.setItem(r, 1, W.QTableWidgetItem(""))
        self.table.setCellWidget(5, 1, self.chk_in)
        self.table.setCellWidget(6, 1, self.chk_out)
        v.addWidget(self.table)
        self.combo.currentIndexChanged.connect(self._on_combo)
        self._on_combo()

    def _set(self, row: int, text: str) -> None:
        self.table.item(row, 1).setText(text)

    def _get(self, row: int) -> str:
        return self.table.item(row, 1).text().strip()

    def _on_combo(self, *_):
        name = self.combo.currentText()
        custom = name not in PRESETS
        if custom:
            self._set(0, CUSTOM)
        else:
            self.set_spec(PRESETS[name], select=False)
        flags = (Qt.ItemIsEnabled | Qt.ItemIsEditable | Qt.ItemIsSelectable) if custom else Qt.ItemIsEnabled
        for r in range(1, 5):                      # only a custom CRC may be edited
            self.table.item(r, 1).setFlags(flags)
        self.chk_in.setEnabled(custom)
        self.chk_out.setEnabled(custom)

    def set_spec(self, spec: CrcSpec, select: bool = True) -> None:
        """Show a spec; if it equals a preset the combo follows, else it becomes 自定义."""
        if select:
            match = [n for n, p in PRESETS.items() if p.to_dict() == dict(spec.to_dict(), name=n)]
            self.combo.blockSignals(True)
            self.combo.setCurrentText(match[0] if match else CUSTOM)
            self.combo.blockSignals(False)
        self._set(0, spec.name)
        self._set(1, str(spec.width))
        hexw = max(2, (spec.width + 3) // 4)
        self._set(2, f"0x{spec.poly:0{hexw}X}")
        self._set(3, f"0x{spec.init:0{hexw}X}")
        self._set(4, f"0x{spec.xorout:0{hexw}X}")
        self.chk_in.setChecked(spec.refin)
        self.chk_out.setChecked(spec.refout)

    def spec(self) -> CrcSpec:
        try:
            sp = CrcSpec(CUSTOM, int(self._get(1), 10), int(self._get(2), 16), int(self._get(3), 16),
                         int(self._get(4), 16), self.chk_in.isChecked(), self.chk_out.isChecked())
        except ValueError:
            raise ValueError("CRC 参数无效 (多项式/初始值/异或值请填十六进制，位宽填十进制)")
        if not 8 <= sp.width <= 32:
            raise ValueError("CRC 位宽必须在 8~32 之间")
        for n, pr in PRESETS.items():              # name it after the preset it equals
            if pr.to_dict() == dict(sp.to_dict(), name=n):
                sp.name = n
                break
        return sp


class CrcToolDialog(W.QDialog):
    """校验码计算工具: per-region CRC of a HEX/S19/BIN file."""

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("校验码计算工具")
        self.resize(1000, 640)
        v = W.QVBoxLayout(self)
        top = W.QHBoxLayout()
        self.e_path = W.QLineEdit()
        self.b_open, self.b_calc = W.QPushButton("打开文件"), W.QPushButton("重新计算")
        top.addWidget(self.e_path)
        top.addWidget(self.b_open)
        top.addWidget(self.b_calc)
        v.addLayout(top)
        body = W.QHBoxLayout()
        self.crc = CrcConfigWidget()
        self.crc.setFixedWidth(300)
        body.addWidget(self.crc)
        self.table = W.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["区域", "起始地址(Hex)", "长度", "校验码(Hex)"])
        body.addWidget(self.table)
        v.addLayout(body)
        self.b_open.clicked.connect(self._open)
        self.b_calc.clicked.connect(self.calculate)

    def _open(self) -> None:
        p = pick_file(self, self.ctx, "打开固件文件", "固件 (*.hex *.s19 *.srec *.bin *.mot);;所有文件 (*.*)")
        if p:
            self.e_path.setText(p)
            self.calculate()

    def calculate(self) -> None:
        path = self.e_path.text().strip()
        if not path:
            return
        try:
            spec = self.crc.spec()
            segs = load_image(path, 0)
        except (ValueError, OSError) as e:
            warn(self, str(e))
            return
        self.table.setRowCount(0)
        rows = [(f"区域{i + 1}", g.addr, len(g.data), crc_calc(spec, g.data)) for i, g in enumerate(segs)]
        if len(segs) > 1:
            allb = b"".join(g.data for g in segs)
            rows.append(("全部(拼接)", segs[0].addr, len(allb), crc_calc(spec, allb)))
        w = spec.nbytes * 2
        for r, (name, addr, n, c) in enumerate(rows):
            self.table.insertRow(r)
            for col, text in enumerate([name, f"{addr:X}", str(n), f"{c:0{w}X}"]):
                self.table.setItem(r, col, W.QTableWidgetItem(text))


# =============================================================================== 服务编辑器
class ServiceEditorDialog(W.QDialog):
    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        import copy
        self.ctx = ctx
        self.services = copy.deepcopy(ctx.services)
        self._cur = -1
        self.setWindowTitle("服务编辑器")
        self.resize(900, 600)
        v = W.QVBoxLayout(self)
        body = W.QHBoxLayout()
        left = W.QVBoxLayout()
        self.lst = W.QListWidget()
        self.lst.setObjectName("svcList")
        left.addWidget(self.lst)
        lb = W.QHBoxLayout()
        self.b_add, self.b_del, self.b_def = W.QPushButton("新增服务"), W.QPushButton("删除服务"), W.QPushButton("恢复默认")
        for b in (self.b_add, self.b_del, self.b_def):
            lb.addWidget(b)
        left.addLayout(lb)
        body.addLayout(left, 1)
        right = W.QVBoxLayout()
        form = W.QGridLayout()
        self.e_sid, self.e_name = W.QLineEdit(), W.QLineEdit()
        form.addWidget(W.QLabel("服务ID(0x):"), 0, 0)
        form.addWidget(self.e_sid, 0, 1)
        form.addWidget(W.QLabel("名称:"), 1, 0)
        form.addWidget(self.e_name, 1, 1)
        right.addLayout(form)
        right.addWidget(W.QLabel("子服务:"))
        self.sub = W.QTableWidget(0, 2)
        self.sub.setHorizontalHeaderLabels(["子服务(0x)", "名称"])
        right.addWidget(self.sub)
        sb = W.QHBoxLayout()
        self.b_sadd, self.b_sdel = W.QPushButton("添加子服务"), W.QPushButton("删除子服务")
        sb.addWidget(self.b_sadd)
        sb.addWidget(self.b_sdel)
        sb.addStretch(1)
        right.addLayout(sb)
        body.addLayout(right, 2)
        v.addLayout(body)
        bb = W.QHBoxLayout()
        bb.addStretch(1)
        self.b_ok, self.b_cancel = W.QPushButton("确定"), W.QPushButton("取消")
        bb.addWidget(self.b_ok)
        bb.addWidget(self.b_cancel)
        v.addLayout(bb)
        self.lst.currentRowChanged.connect(self._on_select)
        self.b_add.clicked.connect(self._add)
        self.b_del.clicked.connect(self._delete)
        self.b_def.clicked.connect(self._defaults)
        self.b_sadd.clicked.connect(lambda: self._sub_row("", ""))
        self.b_sdel.clicked.connect(self._sub_del)
        self.b_ok.clicked.connect(self._ok)
        self.b_cancel.clicked.connect(self.reject)
        self._fill_list()

    def _fill_list(self, keep: int = 0) -> None:
        self._cur = -1
        self.lst.clear()
        for s in self.services:
            self.lst.addItem(f"({s['sid']:02X}) {s['name']}")
        if self.services:
            self.lst.setCurrentRow(min(keep, len(self.services) - 1))

    def _sub_row(self, code: str, name: str) -> None:
        r = self.sub.rowCount()
        self.sub.insertRow(r)
        self.sub.setItem(r, 0, W.QTableWidgetItem(code))
        self.sub.setItem(r, 1, W.QTableWidgetItem(name))

    def _commit_current(self) -> bool:
        if not 0 <= self._cur < len(self.services):
            return True
        try:
            sid = parse_hex(self.e_sid.text())
            if len(sid) != 1:
                raise ValueError("服务ID 必须是 1 字节")
            subs = []
            for r in range(self.sub.rowCount()):
                code, name = self.sub.item(r, 0).text().strip(), self.sub.item(r, 1).text().strip()
                if not code and not name:
                    continue
                cb = parse_hex(code)
                if len(cb) != 1:
                    raise ValueError(f"子服务 {code!r} 必须是 1 字节")
                subs.append({"code": cb[0], "name": name})
        except ValueError as e:
            warn(self, str(e))
            return False
        s = self.services[self._cur]
        s["sid"], s["name"], s["subs"] = sid[0], self.e_name.text().strip() or f"服务{sid[0]:02X}", subs
        self.lst.item(self._cur).setText(f"({s['sid']:02X}) {s['name']}")
        return True

    def _on_select(self, row: int) -> None:
        if row == self._cur:
            return
        if not self._commit_current():
            self.lst.setCurrentRow(self._cur)
            return
        self._cur = row
        if not 0 <= row < len(self.services):
            return
        s = self.services[row]
        self.e_sid.setText(f"{s['sid']:02X}")
        self.e_name.setText(s["name"])
        self.sub.setRowCount(0)
        for x in s["subs"]:
            self._sub_row(f"{x['code']:02X}", x["name"])

    def _add(self) -> None:
        if not self._commit_current():
            return
        self.services.append({"sid": 0x00, "name": "新服务", "subs": []})
        self._fill_list(len(self.services) - 1)

    def _delete(self) -> None:
        if 0 <= self._cur < len(self.services):
            del self.services[self._cur]
            self._fill_list(max(self._cur - 1, 0))

    def _sub_del(self) -> None:
        r = self.sub.currentRow()
        if r >= 0:
            self.sub.removeRow(r)

    def _defaults(self) -> None:
        from .services_def import default_services
        self.services = default_services()
        self._fill_list(0)

    def _ok(self) -> None:
        if not self._commit_current():
            return
        sids = [s["sid"] for s in self.services]
        if len(sids) != len(set(sids)):
            warn(self, "服务ID 有重复")
            return
        self.services.sort(key=lambda s: s["sid"])
        self.ctx.services = self.services
        self.ctx.save_services()
        self.accept()
