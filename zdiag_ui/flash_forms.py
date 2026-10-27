"""Per-service parameter forms of the ECU刷新 editor (middle column in the ZCANPro screenshot).

Every form implements  to_step() -> dict (raises ValueError with a readable message)  and
load_step(step).  All parameters of a step stay editable, so old/new product differences
(27 01/02 vs other levels, 31 01 DF FE vs 31 01 01 02, CRC type, ...) are just different values.
"""
import os
from typing import Dict, Optional

from zdiag.crc import CrcSpec
from zdiag.project import SUPPRESS_SIDS, fmt_hex, new_step, parse_hex
from .common import int_of, pick_file
from .dialogs import CrcConfigWidget
from .qt import QtWidgets as W

NO_SUB = "(无子服务)"


def _row(grid, r, label, widget, extra=None):
    grid.addWidget(W.QLabel(label), r, 0)
    grid.addWidget(widget, r, 1)
    if extra is not None:
        grid.addWidget(extra, r, 2)


def _yn():
    c = W.QComboBox()
    c.addItems(["否", "是"])
    return c


class StepForm(W.QWidget):
    kind = ""

    def __init__(self, ctx, svc: Dict, parent=None) -> None:
        super().__init__(parent)
        self.ctx, self.svc = ctx, svc
        self.v = W.QVBoxLayout(self)
        self.e_desc = W.QLineEdit(svc["name"])
        top = W.QHBoxLayout()
        top.addWidget(W.QLabel("描述:"))
        top.addWidget(self.e_desc)
        self.v.addLayout(top)

    def desc(self) -> str:
        return self.e_desc.text().strip() or self.svc["name"]

    def to_step(self) -> dict:
        raise NotImplementedError

    def load_step(self, step: dict) -> None:
        raise NotImplementedError

    def _browse(self, edit, title: str, filt: str) -> None:
        p = pick_file(self, self.ctx, title, filt)
        if p:
            edit.setText(p)


# --------------------------------------------------------------------------- generic services
class GenericForm(StepForm):
    kind = "service"

    def __init__(self, ctx, svc, parent=None) -> None:
        super().__init__(ctx, svc, parent)
        ga = W.QGroupBox("地址")
        gl = W.QGridLayout(ga)
        self.c_addr = W.QComboBox()
        self.c_addr.addItems(["物理地址", "功能地址"])
        _row(gl, 0, "请求地址:", self.c_addr)
        self.v.addWidget(ga)
        gr = W.QGroupBox("请求")
        g = W.QGridLayout(gr)
        self.l_sid = W.QLabel(f"{svc['sid']:02X}")
        self.c_sub = W.QComboBox()
        self.c_sub.setEditable(True)
        if svc["subs"]:
            self.c_sub.addItem(NO_SUB)
            for s in svc["subs"]:
                self.c_sub.addItem(f"{s['code']:02X} {s['name']}")
            self.c_sub.setCurrentIndex(1)
        self.c_sup, self.c_chk = _yn(), _yn()
        self.c_chk.setCurrentIndex(1)
        self.e_data, self.e_exp = W.QLineEdit(), W.QLineEdit()
        _row(g, 0, "服务ID(0x):", self.l_sid)
        r = 1
        if svc["subs"]:
            _row(g, r, "子服务:", self.c_sub)
            r += 1
        _row(g, r, "抑制响应:", self.c_sup)
        _row(g, r + 1, "数据(0x):", self.e_data)
        _row(g, r + 2, "校验响应:", self.c_chk)
        _row(g, r + 3, "期望响应(0x):", self.e_exp)
        g.addWidget(W.QLabel("期望响应留空 = 自动(服务ID+0x40)；可用 xx 作通配字节"), r + 4, 0, 1, 3)
        self.v.addWidget(gr)
        self.v.addStretch(1)

    def _sub_byte(self) -> Optional[int]:
        t = self.c_sub.currentText().strip()
        if not self.svc["subs"] or t == NO_SUB or not t:
            return None
        b = parse_hex(t.split()[0])
        if len(b) != 1:
            raise ValueError("子服务必须是 1 字节")
        return b[0]

    def to_step(self) -> dict:
        sub = self._sub_byte()
        pdu = bytes([self.svc["sid"]]) + (bytes([sub]) if sub is not None else b"") + parse_hex(self.e_data.text())
        exp = self.e_exp.text().strip()
        if exp:
            parse_hex(exp.replace("xx", "00").replace("XX", "00"))
        return new_step("service", name=self.desc(), addr="func" if self.c_addr.currentIndex() else "phys",
                        pdu=fmt_hex(pdu), suppress=self.c_sup.currentIndex() == 1,
                        check=self.c_chk.currentIndex() == 1, expect=exp)

    def load_step(self, s: dict) -> None:
        pdu = parse_hex(s["pdu"])
        self.e_desc.setText(s.get("name", ""))
        self.c_addr.setCurrentIndex(1 if s.get("addr") == "func" else 0)
        self.c_sup.setCurrentIndex(1 if s.get("suppress") else 0)
        self.c_chk.setCurrentIndex(1 if s.get("check", True) else 0)
        self.e_exp.setText(s.get("expect", ""))
        rest = pdu[1:]
        if self.svc["subs"] and rest:
            code = rest[0] & 0x7F
            idx = self.c_sub.findText(next((f"{x['code']:02X} {x['name']}" for x in self.svc["subs"]
                                            if x["code"] == code), ""))
            if idx >= 0:
                self.c_sub.setCurrentIndex(idx)
            else:
                self.c_sub.setCurrentText(f"{code:02X}")
            rest = rest[1:]
        elif self.svc["subs"]:
            self.c_sub.setCurrentIndex(0)
        self.e_data.setText(fmt_hex(rest))


# --------------------------------------------------------------------------- 27 安全访问
class UnlockForm(StepForm):
    kind = "unlock"

    def __init__(self, ctx, svc, parent=None) -> None:
        super().__init__(ctx, svc, parent)
        self.e_desc.setText("ECU解锁")
        g = W.QGridLayout()
        self.e_level, self.e_dll, self.e_wd = W.QLineEdit("1"), W.QLineEdit(), W.QLineEdit()
        self.b_dll, self.b_wd = W.QPushButton("浏览"), W.QPushButton("浏览")
        self.c_serial = W.QCheckBox("解锁前读取ECU序列号(22 F18C)，写入 ECUserialnumber.txt")
        _row(g, 0, "级别(奇数):", self.e_level)
        _row(g, 1, "DLL:", self.e_dll, self.b_dll)
        _row(g, 2, "DLL工作目录:", self.e_wd, self.b_wd)
        g.addWidget(self.c_serial, 3, 0, 1, 3)
        g.addWidget(W.QLabel("级别 1 即 27 01 请求种子 / 27 02 发送密钥；3 即 27 03/04，依此类推。"), 4, 0, 1, 3)
        g.addWidget(W.QLabel("种子/密钥长度由 ECU 和 DLL 决定，单帧/多帧传输由协议栈自动处理。"), 5, 0, 1, 3)
        g.addWidget(W.QLabel("DLL工作目录留空 = 程序当前目录（DLL 读取序列号文件的位置）。"), 6, 0, 1, 3)
        self.v.addLayout(g)
        self.v.addStretch(1)
        self.b_dll.clicked.connect(lambda: self._browse(self.e_dll, "选择密钥 DLL", "DLL (*.dll);;所有文件 (*.*)"))
        self.b_wd.clicked.connect(self._pick_dir)

    def _pick_dir(self) -> None:
        d = W.QFileDialog.getExistingDirectory(self, "选择工作目录", self.ctx.last_dir)
        if d:
            self.e_wd.setText(d)

    def to_step(self) -> dict:
        level = int_of(self.e_level, "级别", 1, 0x7D)
        if level % 2 == 0:
            raise ValueError("安全级别必须是奇数 (27 请求种子的子功能)")
        if not self.e_dll.text().strip():
            raise ValueError("请选择密钥 DLL")
        return new_step("unlock", name=self.desc(), level=level, dll=self.e_dll.text().strip(),
                        workdir=self.e_wd.text().strip(), use_serial=self.c_serial.isChecked())

    def load_step(self, s: dict) -> None:
        self.e_desc.setText(s.get("name", ""))
        self.e_level.setText(str(s.get("level", 1)))
        self.e_dll.setText(s.get("dll", ""))
        self.e_wd.setText(s.get("workdir", ""))
        self.c_serial.setChecked(bool(s.get("use_serial")))


# --------------------------------------------------------------------------- 34 文件下载 / Flash driver
class DownloadForm(StepForm):
    kind = "download"

    def __init__(self, ctx, svc, parent=None) -> None:
        super().__init__(ctx, svc, parent)
        self.e_desc.setText("文件下载")
        g = W.QGridLayout()
        self.c_kind = W.QComboBox()
        self.c_kind.addItem("文件下载", "download")
        self.c_kind.addItem("Flash driver", "driver")
        self.e_file, self.b_file = W.QLineEdit(), W.QPushButton("浏览")
        self.e_base = W.QLineEdit("0")
        self.e_al, self.e_sl, self.e_dfi = W.QLineEdit("4"), W.QLineEdit("4"), W.QLineEdit("00")
        _row(g, 0, "类型:", self.c_kind)
        _row(g, 1, "文件名:", self.e_file, self.b_file)
        _row(g, 2, "BIN起始地址(0x):", self.e_base)
        _row(g, 3, "地址长度(字节):", self.e_al)
        _row(g, 4, "数据长度(字节):", self.e_sl)
        _row(g, 5, "数据格式标识(0x):", self.e_dfi)
        g.addWidget(W.QLabel("流程: 34 请求下载 → 36 传输数据(按ECU返回的块长) → 37 退出传输；HEX/S19 的每个区域依次下载。"), 6, 0, 1, 3)
        self.v.addLayout(g)
        self.v.addStretch(1)
        self.b_file.clicked.connect(lambda: self._browse(
            self.e_file, "选择固件/Driver 文件", "固件 (*.hex *.s19 *.srec *.bin *.mot);;所有文件 (*.*)"))
        self.c_kind.currentIndexChanged.connect(self._kind_changed)

    def _kind_changed(self, *_) -> None:
        if self.e_desc.text() in ("文件下载", "Flash driver", ""):
            self.e_desc.setText(self.c_kind.currentText())

    def to_step(self) -> dict:
        if not self.e_file.text().strip():
            raise ValueError("请选择文件")
        parse_hex(self.e_base.text() or "0")
        dfi = parse_hex(self.e_dfi.text() or "00")
        if len(dfi) != 1:
            raise ValueError("数据格式标识必须是 1 字节")
        return new_step(self.c_kind.currentData() or "download", name=self.desc(),
                        file=self.e_file.text().strip(), base_addr=(self.e_base.text().strip() or "0"),
                        addr_len=int_of(self.e_al, "地址长度", 1, 8), size_len=int_of(self.e_sl, "数据长度", 1, 8),
                        dfi=fmt_hex(dfi))

    def load_step(self, s: dict) -> None:
        self.c_kind.setCurrentIndex(1 if s["type"] == "driver" else 0)
        self.e_desc.setText(s.get("name", ""))
        self.e_file.setText(s.get("file", ""))
        self.e_base.setText(str(s.get("base_addr", "0")))
        self.e_al.setText(str(s.get("addr_len", 4)))
        self.e_sl.setText(str(s.get("size_len", 4)))
        self.e_dfi.setText(s.get("dfi", "00"))


# --------------------------------------------------------------------------- 31 例程控制 (+CRC)
class RoutineForm(StepForm):
    kind = "routine"

    def __init__(self, ctx, svc, parent=None) -> None:
        super().__init__(ctx, svc, parent)
        self.e_desc.setText("CRC校验")
        g = W.QGridLayout()
        self.c_addr = W.QComboBox()
        self.c_addr.addItems(["物理地址", "功能地址"])
        self.c_sub = W.QComboBox()
        self.c_sub.setEditable(True)
        for s in svc["subs"]:
            self.c_sub.addItem(f"{s['code']:02X} {s['name']}")
        self.e_rid, self.e_data = W.QLineEdit(), W.QLineEdit()
        self.e_exp, self.e_status = W.QLineEdit(), W.QLineEdit()
        _row(g, 0, "请求地址:", self.c_addr)
        _row(g, 1, "子服务:", self.c_sub)
        _row(g, 2, "例程标识符(0x):", self.e_rid)
        _row(g, 3, "附加数据(0x):", self.e_data)
        _row(g, 4, "期望响应(0x):", self.e_exp)
        _row(g, 5, "例程状态应为(0x):", self.e_status)
        g.addWidget(W.QLabel("附加数据可含占位符 {addr} {len} {crc}，如  {addr} {len}"), 6, 0, 1, 3)
        self.v.addLayout(g)
        gc = W.QGroupBox("CRC")
        cl = W.QVBoxLayout(gc)
        self.k_crc = W.QCheckBox("自动计算 CRC 并追加到 PDU 末尾")
        cl.addWidget(self.k_crc)
        self.crc = CrcConfigWidget()
        cl.addWidget(self.crc)
        g2 = W.QGridLayout()
        self.c_order = W.QComboBox()
        self.c_order.addItems(["大端(高字节在前)", "小端(低字节在前)"])
        self.e_src, self.b_src = W.QLineEdit(), W.QPushButton("浏览")
        self.k_reg = W.QCheckBox("按区域分别发送(每个区域一条请求)")
        self.k_reg.setChecked(True)
        self.e_al, self.e_sl = W.QLineEdit("4"), W.QLineEdit("4")
        _row(g2, 0, "CRC字节序:", self.c_order)
        _row(g2, 1, "数据来源文件:", self.e_src, self.b_src)
        g2.addWidget(self.k_reg, 2, 0, 1, 3)
        _row(g2, 3, "地址长度(字节):", self.e_al)
        _row(g2, 4, "数据长度(字节):", self.e_sl)
        g2.addWidget(W.QLabel("数据来源留空 = 最近一次「文件下载」步骤的文件。"), 5, 0, 1, 3)
        cl.addLayout(g2)
        self.v.addWidget(gc)
        self.v.addStretch(1)
        self.c_sub.setCurrentIndex(0)
        self.b_src.clicked.connect(lambda: self._browse(
            self.e_src, "选择数据来源文件", "固件 (*.hex *.s19 *.srec *.bin *.mot);;所有文件 (*.*)"))

    def to_step(self) -> dict:
        t = self.c_sub.currentText().strip()
        sub = parse_hex(t.split()[0]) if t else b""
        if len(sub) != 1:
            raise ValueError("子服务必须是 1 字节")
        rid = parse_hex(self.e_rid.text())
        if len(rid) != 2:
            raise ValueError("例程标识符必须是 2 字节，如 DF FE")
        data = self.e_data.text().strip()
        from zdiag.engine import render_template, StepError
        try:
            render_template(data, {"addr": b"", "len": b"", "crc": b""})
        except StepError as e:
            raise ValueError(str(e))
        for txt in (self.e_exp.text(), self.e_status.text()):
            if txt.strip():
                parse_hex(txt.replace("xx", "00").replace("XX", "00"))
        spec = self.crc.spec() if self.k_crc.isChecked() else CrcSpec()
        return new_step("routine", name=self.desc(), addr="func" if self.c_addr.currentIndex() else "phys",
                        sub=fmt_hex(sub), rid=fmt_hex(rid), data=data, use_crc=self.k_crc.isChecked(),
                        crc=spec.to_dict(), crc_order="little" if self.c_order.currentIndex() else "big",
                        source=self.e_src.text().strip(), per_region=self.k_reg.isChecked(),
                        addr_len=int_of(self.e_al, "地址长度", 1, 8), size_len=int_of(self.e_sl, "数据长度", 1, 8),
                        expect=self.e_exp.text().strip(), status_ok=self.e_status.text().strip())

    def load_step(self, s: dict) -> None:
        self.e_desc.setText(s.get("name", ""))
        self.c_addr.setCurrentIndex(1 if s.get("addr") == "func" else 0)
        code = parse_hex(s.get("sub", "01"))[0]
        idx = self.c_sub.findText(next((f"{x['code']:02X} {x['name']}" for x in self.svc["subs"]
                                        if x["code"] == code), ""))
        if idx >= 0:
            self.c_sub.setCurrentIndex(idx)
        else:
            self.c_sub.setCurrentText(f"{code:02X}")
        self.e_rid.setText(s.get("rid", ""))
        self.e_data.setText(s.get("data", ""))
        self.e_exp.setText(s.get("expect", ""))
        self.e_status.setText(s.get("status_ok", ""))
        self.k_crc.setChecked(bool(s.get("use_crc")))
        self.crc.set_spec(CrcSpec.from_dict(s.get("crc", {})))
        self.c_order.setCurrentIndex(1 if s.get("crc_order") == "little" else 0)
        self.e_src.setText(s.get("source", ""))
        self.k_reg.setChecked(bool(s.get("per_region", True)))
        self.e_al.setText(str(s.get("addr_len", 4)))
        self.e_sl.setText(str(s.get("size_len", 4)))


def make_form(ctx, svc: Dict) -> StepForm:
    if svc["sid"] == 0x27:
        return UnlockForm(ctx, svc)
    if svc["sid"] == 0x34:
        return DownloadForm(ctx, svc)
    if svc["sid"] == 0x31:
        return RoutineForm(ctx, svc)
    return GenericForm(ctx, svc)
