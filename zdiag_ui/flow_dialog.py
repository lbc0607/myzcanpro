"""流程编排: the add / edit dialog of one node (the form is built from the node type)."""
import copy
import os
from typing import Dict, List, Optional

from zdiag.busstats import ChannelCfg
from zdiag.devmgr import is_fd_device
from zdiag.flow import (LOOP_MODES, MAX_CHANNEL, NEEDS_VALUE, OPS, TYPE_LABEL, Flow, cond_text, walk)
from zdiag.project import DEVICES, parse_hex, parse_id
from zdiag.scanlib import INFO_FIELDS, PARSE_MODES, ScanStore, function_paths, module_names
from .common import warn
from .qt import QtWidgets as W

PRESETS = [("(自定义)", "", ""), ("ECU复位 (硬复位)", "11 01", "ECU复位"), ("进入扩展会话", "10 03", "进入扩展会话"),
           ("读故障码", "19 02 08", "读故障码"), ("清除故障码", "14 FF FF FF", "清除故障码"),
           ("读VIN (ASCII)", "22 F1 90", "读VIN")]
RESULT_TYPES = ("scan", "function", "uds", "delay", "send", "stop_send", "open_device", "start_channel")


class CondEditor(W.QWidget):
    """「某步骤的结果」+ 比较方式 + 值."""

    def __init__(self, flow: Flow, owner_id: str, cond: dict, parent=None) -> None:
        super().__init__(parent)
        self.flow = flow
        g = W.QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        self.c_ref, self.c_field, self.c_op, self.e_val = W.QComboBox(), W.QComboBox(), W.QComboBox(), W.QLineEdit()
        for n in flow.walk():
            if n["type"] in RESULT_TYPES and n["id"] != owner_id:
                self.c_ref.addItem(f"{n['name']}  [{n['id']}]", n["id"])
        for k, lab in OPS:
            self.c_op.addItem(lab, k)
        g.addWidget(W.QLabel("如果步骤:"), 0, 0)
        g.addWidget(self.c_ref, 0, 1, 1, 3)
        g.addWidget(W.QLabel("的"), 1, 0)
        g.addWidget(self.c_field, 1, 1)
        g.addWidget(self.c_op, 1, 2)
        g.addWidget(self.e_val, 1, 3)
        self.c_ref.currentIndexChanged.connect(self._sync)
        self.c_op.currentIndexChanged.connect(self._sync)
        i = self.c_ref.findData(cond.get("ref", ""))
        self.c_ref.setCurrentIndex(i if i >= 0 else 0)
        self._fill_fields()
        j = self.c_field.findData(cond.get("field", ""))
        self.c_field.setCurrentIndex(j if j >= 0 else 0)
        k = self.c_op.findData(cond.get("op", "ok"))
        self.c_op.setCurrentIndex(k if k >= 0 else 0)
        self.e_val.setText(str(cond.get("value", "")))
        self._sync()

    def _fill_fields(self) -> None:
        self.c_field.clear()
        self.c_field.addItem("结果", "")
        ref = self.flow.find(self.c_ref.currentData() or "")
        if ref is not None and ref["type"] == "scan":
            for k, lab in INFO_FIELDS:
                if k in ref.get("fields", []):
                    self.c_field.addItem(lab, k)

    def _sync(self, *_) -> None:
        cur = self.c_field.currentData()
        self._fill_fields()
        j = self.c_field.findData(cur)
        self.c_field.setCurrentIndex(j if j >= 0 else 0)
        self.e_val.setEnabled(self.c_op.currentData() in NEEDS_VALUE)

    def value(self) -> dict:
        if self.c_ref.currentData() is None:
            raise ValueError("条件需要先有一个会产生结果的步骤可以引用（比如扫描 / 功能 / UDS请求）")
        op = self.c_op.currentData()
        val = self.e_val.text().strip()
        if op in NEEDS_VALUE and not val:
            raise ValueError("请填写比较值")
        return {"ref": self.c_ref.currentData(), "field": self.c_field.currentData() or "", "op": op,
                "value": val if op in NEEDS_VALUE else ""}


class NodeDialog(W.QDialog):
    """Edits a copy of `node`; `result` is the new dict after 确定 (None on cancel)."""

    def __init__(self, flow: Flow, node: dict, store: ScanStore, dev_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.flow, self.node, self.store = flow, copy.deepcopy(node), store
        self.result: Optional[dict] = None
        t = node["type"]
        self.setWindowTitle(f"{TYPE_LABEL[t]} - 编辑")
        self.g = W.QGridLayout(self)
        self.row = 0
        self.w: Dict[str, object] = {}
        self.e_name = self._line("名称:", node.get("name", ""))
        getattr(self, "_build_" + t)(self.node)
        if t not in ("setup", "lane", "note"):
            self.c_fail = W.QComboBox()
            self.c_fail.addItem("停止本泳道（后面的步骤不再执行）", "stop")
            self.c_fail.addItem("继续往下执行（可用条件分支处理失败）", "continue")
            self.c_fail.setCurrentIndex(0 if node.get("on_fail", "stop") == "stop" else 1)
            self._add("失败后:", self.c_fail)
        self.k_enabled = W.QCheckBox("启用（取消勾选则执行时跳过）")
        self.k_enabled.setChecked(bool(node.get("enabled", True)))
        self.g.addWidget(self.k_enabled, self.row, 0, 1, 3)
        self.row += 1
        self.b_ok, self.b_cancel = W.QPushButton("确定"), W.QPushButton("取消")
        self.g.addWidget(self.b_ok, self.row, 1)
        self.g.addWidget(self.b_cancel, self.row, 2)
        self.b_ok.clicked.connect(self._on_ok)
        self.b_cancel.clicked.connect(self.reject)

    # -- helpers
    def _add(self, label: str, widget, span: int = 2) -> None:
        self.g.addWidget(W.QLabel(label), self.row, 0)
        self.g.addWidget(widget, self.row, 1, 1, span)
        self.row += 1

    def _line(self, label: str, text) -> "W.QLineEdit":
        e = W.QLineEdit()
        e.setText(str(text))
        self._add(label, e)
        return e

    def _combo(self, label: str, items, current) -> "W.QComboBox":
        c = W.QComboBox()
        for text, data in items:
            c.addItem(text, data)
        i = c.findData(current)
        c.setCurrentIndex(i if i >= 0 else 0)
        self._add(label, c)
        return c

    def _module_combo(self, n: dict) -> "W.QComboBox":
        names = module_names(self.store)
        cur = n.get("module", "")
        if cur and cur not in names:
            names.append(cur)                                   # keep an unknown one visible so it can be fixed
        return self._combo("软件模块:", [(x, x) for x in names], cur)

    # -- per type forms
    def _build_setup(self, n):
        pass

    def _build_lane(self, n):
        self.c_channel = self._combo("通道:", [(f"通道{i}", i) for i in range(MAX_CHANNEL + 1)], n.get("channel", 0))

    def _build_note(self, n):
        self.e_text = self._line("内容:", n.get("text", ""))

    def _build_open_device(self, n):
        self.c_device = self._combo("设备类型:", [(d, d) for d in DEVICES], n.get("device"))
        self.e_index = self._line("设备索引:", n.get("index", 0))

    def _build_start_channel(self, n):
        c = n.get("cfg", {})
        self.c_channel = self._combo("通道:", [(f"通道{i}", i) for i in range(MAX_CHANNEL + 1)], n.get("channel", 0))
        self.c_proto = self._combo("协议:", [("CAN", "CAN"), ("CAN FD", "CAN FD")], c.get("protocol", "CAN"))
        self.c_accel = self._combo("CANFD加速:", [("是", True), ("否", False)], c.get("fd_accel", True))
        self.e_baud = self._line("波特率(kbaud/s):", f"{c.get('bitrate_kbps', 500):g}")
        self.e_dbaud = self._line("数据域波特率(kbaud/s):", f"{c.get('data_kbps', 2000):g}")
        self.c_mode = self._combo("工作模式:", [("正常模式", False), ("只听模式", True)], c.get("listen_only", False))
        self.k_term = W.QCheckBox("打开终端电阻")
        self.k_term.setChecked(bool(c.get("terminator", True)))
        self.g.addWidget(self.k_term, self.row, 0, 1, 3)
        self.row += 1
        self.c_proto.currentIndexChanged.connect(self._sync_channel)
        self.c_accel.currentIndexChanged.connect(self._sync_channel)
        self._sync_channel()

    def _sync_channel(self, *_) -> None:
        fd = self.c_proto.currentData() == "CAN FD"
        self.c_accel.setEnabled(fd)
        self.e_dbaud.setEnabled(fd and bool(self.c_accel.currentData()))

    def _build_scan(self, n):
        self.module = self._module_combo(n)
        self.k_fields = {}
        for k, lab in INFO_FIELDS:
            cb = W.QCheckBox(lab)
            cb.setChecked(k in n.get("fields", []))
            self.k_fields[k] = cb
            self.g.addWidget(cb, self.row, 1)
            self.row += 1

    def _build_function(self, n):
        self.module = self._module_combo(n)
        self.c_path = W.QComboBox()
        self._add("功能:", self.c_path)
        self.module.currentIndexChanged.connect(lambda *_: self._fill_paths(None))
        self._fill_paths(n.get("path", ""))

    def _fill_paths(self, cur) -> None:
        self.c_path.clear()
        paths = function_paths(self.store, self.module.currentData() or "")
        if cur and cur not in paths:
            paths.append(cur)
        for p in paths:
            self.c_path.addItem(p, p)
        if cur:
            self.c_path.setCurrentIndex(max(self.c_path.findData(cur), 0))

    def _build_uds(self, n):
        self.module = self._module_combo(n)
        self.c_preset = self._combo("常用请求:", [(p[0], i) for i, p in enumerate(PRESETS)], 0)
        self.e_pdu = self._line("请求报文(十六进制):", n.get("pdu", ""))
        self.k_pre = W.QCheckBox("先进入扩展会话 (10 03)")
        self.k_pre.setChecked(bool(n.get("pre_session")))
        self.g.addWidget(self.k_pre, self.row, 1, 1, 2)
        self.row += 1
        self.e_skip = self._line("结果跳过开头字节(-1=自动):", n.get("skip", -1))
        self.c_parse = self._combo("结果显示为:", [(t, k) for k, t in PARSE_MODES], n.get("parse", "hex"))
        self.c_preset.currentIndexChanged.connect(self._preset)

    def _preset(self, *_) -> None:
        _, pdu, name = PRESETS[self.c_preset.currentData() or 0]
        if pdu:
            self.e_pdu.setText(pdu)
            if self.e_name.text().strip() in ("", "UDS请求") or self.e_name.text() in [p[2] for p in PRESETS]:
                self.e_name.setText(name)
            if pdu == "22 F1 90":
                self.c_parse.setCurrentIndex(max(self.c_parse.findData("ascii"), 0))
                self.e_skip.setText("3")
                self.k_pre.setChecked(True)

    def _build_delay(self, n):
        self.e_seconds = self._line("延时(秒):", f"{n.get('seconds', 3):g}")

    def _build_send(self, n):
        self.e_key = self._line("发送名称(用于停止):", n.get("key", "cyc1"))
        self.e_id = self._line("帧ID(0x):", n.get("can_id", "123"))
        self.k_ext = W.QCheckBox("扩展帧")
        self.k_ext.setChecked(bool(n.get("extended")))
        self.k_fd = W.QCheckBox("CAN FD 帧（通道需为 CAN FD）")
        self.k_fd.setChecked(bool(n.get("fd")))
        for cb in (self.k_ext, self.k_fd):
            self.g.addWidget(cb, self.row, 1, 1, 2)
            self.row += 1
        self.e_data = self._line("数据(十六进制):", n.get("data", ""))
        self.e_period = self._line("发送周期(ms):", n.get("period_ms", 100))

    def _build_stop_send(self, n):
        keys = [m["key"] for m in self.flow.walk() if m["type"] == "send"]
        self.c_key = self._combo("停止哪一个:", [("全部", "")] + [(k, k) for k in keys], n.get("key", ""))

    def _build_if(self, n):
        self.cond = CondEditor(self.flow, n["id"], n.get("cond", {}))
        self.g.addWidget(self.cond, self.row, 0, 1, 3)
        self.row += 1

    def _build_loop(self, n):
        self.c_mode_l = self._combo("循环方式:", [(t, k) for k, t in LOOP_MODES], n.get("mode", "count"))
        self.e_count = self._line("循环次数(固定次数时):", n.get("count", 2))
        self.e_max = self._line("最多循环次数(条件循环时):", n.get("max_iter", 100))
        self.cond = CondEditor(self.flow, n["id"], n.get("cond", {}))
        self.g.addWidget(self.cond, self.row, 0, 1, 3)
        self.row += 1
        self.c_mode_l.currentIndexChanged.connect(self._sync_loop)
        self._sync_loop()

    def _sync_loop(self, *_) -> None:
        count = self.c_mode_l.currentData() == "count"
        self.e_count.setEnabled(count)
        self.e_max.setEnabled(not count)
        self.cond.setEnabled(not count)

    # -- collect
    def _collect(self) -> dict:
        n = copy.deepcopy(self.node)
        t = n["type"]
        n["name"] = self.e_name.text().strip() or TYPE_LABEL[t]
        n["enabled"] = self.k_enabled.isChecked()
        if hasattr(self, "c_fail"):
            n["on_fail"] = self.c_fail.currentData() or "stop"
        if t == "lane":
            n["channel"] = int(self.c_channel.currentData())
        elif t == "note":
            n["text"] = self.e_text.text()
        elif t == "open_device":
            n["device"] = self.c_device.currentData()
            n["index"] = _int(self.e_index, "设备索引", 0, 15)
        elif t == "start_channel":
            fd = self.c_proto.currentData() == "CAN FD"
            cfg = ChannelCfg(protocol=self.c_proto.currentData(), bitrate_kbps=_float(self.e_baud, "波特率"),
                             data_kbps=_float(self.e_dbaud, "数据域波特率") if fd else 2000.0,
                             fd_accel=bool(self.c_accel.currentData()) if fd else True,
                             listen_only=bool(self.c_mode.currentData()), terminator=self.k_term.isChecked())
            cfg.validate()
            n["channel"] = int(self.c_channel.currentData())
            n["cfg"] = {"protocol": cfg.protocol, "bitrate_kbps": cfg.bitrate_kbps, "data_kbps": cfg.data_kbps,
                        "fd_accel": cfg.fd_accel, "listen_only": cfg.listen_only, "terminator": cfg.terminator}
        elif t in ("scan", "function", "uds"):
            mod = self.module.currentData()
            if not mod:
                raise ValueError("请选择软件模块")
            n["module"] = mod
            if t == "scan":
                n["fields"] = [k for k, _ in INFO_FIELDS if self.k_fields[k].isChecked()]
                if not n["fields"]:
                    raise ValueError("至少选择一个要读取的字段")
            elif t == "function":
                if not self.c_path.currentData():
                    raise ValueError("这个模块里还没有功能：请先在「软件扫描」里给它添加功能")
                n["path"] = self.c_path.currentData()
            else:
                if not parse_hex(self.e_pdu.text()):
                    raise ValueError("请求报文不能为空")
                n["pdu"] = " ".join(f"{b:02X}" for b in parse_hex(self.e_pdu.text()))
                n["pre_session"] = self.k_pre.isChecked()
                n["skip"] = _int(self.e_skip, "跳过字节数", -1, 4095)
                n["parse"] = self.c_parse.currentData() or "hex"
        elif t == "delay":
            n["seconds"] = _float(self.e_seconds, "延时", 0, 86400)
        elif t == "send":
            data = parse_hex(self.e_data.text())
            n.update(key=self.e_key.text().strip(), can_id=f"{parse_id(self.e_id.text()):X}", extended=self.k_ext.isChecked(),
                     fd=self.k_fd.isChecked(), data=" ".join(f"{b:02X}" for b in data), period_ms=_int(self.e_period, "发送周期", 1, 3600000))
            if not n["key"]:
                raise ValueError("请给这个周期发送起一个名称（停止时用）")
        elif t == "stop_send":
            n["key"] = self.c_key.currentData() or ""
        elif t == "if":
            n["cond"] = self.cond.value()
        elif t == "loop":
            n["mode"] = self.c_mode_l.currentData()
            n["count"] = _int(self.e_count, "循环次数", 1, 10000) if n["mode"] == "count" else n.get("count", 1)
            n["max_iter"] = _int(self.e_max, "最多循环次数", 1, 10000) if n["mode"] != "count" else n.get("max_iter", 100)
            if n["mode"] != "count":
                n["cond"] = self.cond.value()
        return n

    def _on_ok(self) -> None:
        try:
            self.result = self._collect()
        except ValueError as e:
            warn(self, str(e))
            return
        self.accept()


def _int(edit, name: str, lo: int, hi: int) -> int:
    try:
        v = int(edit.text().strip(), 10)
    except ValueError:
        raise ValueError(f"「{name}」应为整数")
    if not lo <= v <= hi:
        raise ValueError(f"「{name}」应在 {lo}~{hi} 之间")
    return v


def _float(edit, name: str, lo: float = 0.001, hi: float = 1e9) -> float:
    try:
        v = float(edit.text().strip())
    except ValueError:
        raise ValueError(f"「{name}」应为数字")
    if not lo <= v <= hi:
        raise ValueError(f"「{name}」应在 {lo:g}~{hi:g} 之间")
    return v
