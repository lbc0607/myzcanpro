"""Data model of the diagnostic tool: addressing, settings, channel and the editable flash plan.

Everything is plain JSON (no .zflash).  Missing keys fall back to defaults so files written by
older versions keep loading.
"""
from __future__ import annotations
import copy
import json
import re
from dataclasses import dataclass, field, asdict, fields
from typing import Any, Dict, List

from .crc import CrcSpec, PRESETS

FORMAT_VERSION = 1
SIM_DEVICE = "模拟ECU(测试)"
DEVICES = ["USBCANFD-200U", "USBCANFD-100U", "USBCANFD-MINI", "USBCANFD-400U", "USBCAN-II", SIM_DEVICE]
SUPPRESS_SIDS = {0x10, 0x11, 0x19, 0x27, 0x28, 0x31, 0x3E, 0x83, 0x85, 0x86, 0x87}


# --------------------------------------------------------------------------- hex helpers
def parse_hex(text: str) -> bytes:
    """'10 03', '0x10,0x03', '1003' -> bytes.  Raises ValueError with a readable message."""
    t = re.sub(r"0x", "", text or "", flags=re.I)
    t = re.sub(r"[\s,;:_-]+", "", t)
    if len(t) % 2:
        raise ValueError(f"十六进制长度必须为偶数: {text!r}")
    try:
        return bytes.fromhex(t)
    except ValueError:
        raise ValueError(f"含有非十六进制字符: {text!r}") from None


def fmt_hex(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


def parse_id(text: str) -> int:
    t = (text or "").strip().lower().replace("0x", "")
    if not t:
        raise ValueError("CAN ID 为空")
    return int(t, 16)


# --------------------------------------------------------------------------- generic (de)serialisation
def _fill(obj, d: dict):
    names = {f.name for f in fields(obj)}
    for k, v in (d or {}).items():
        if k in names:
            setattr(obj, k, v)
    return obj


@dataclass
class Addressing:
    phys: int = 0x7E2
    func: int = 0x7DF
    resp: int = 0x7EA
    extended: bool = False

    def to_dict(self) -> dict:
        return {"phys": f"{self.phys:X}", "func": f"{self.func:X}",
                "resp": f"{self.resp:X}", "extended": self.extended}

    @staticmethod
    def from_dict(d: dict) -> "Addressing":
        a = Addressing()
        d = d or {}
        for k in ("phys", "func", "resp"):
            if k in d:
                setattr(a, k, parse_id(str(d[k])))
        a.extended = bool(d.get("extended", False))
        return a


@dataclass
class KeepAlive:
    enabled: bool = False
    addr: str = "func"            # "func" | "phys"
    period_ms: int = 2000
    suppress: bool = True         # 3E 80 instead of 3E 00


@dataclass
class Settings:
    protocol: str = "ISO15765-2(2004)"
    padding_enabled: bool = True
    padding: int = 0x00
    p2_ms: int = 2000
    p2star_ms: int = 5000
    st_min_ms: int = 0            # STmin we announce in our own FlowControl
    bs: int = 0
    override_stmin: bool = False  # replace the STmin the ECU announces
    override_stmin_ms: int = 0
    dl_retry: int = 2             # retries of one 0x36 block on timeout
    block_interval_ms: int = 0    # pause between 0x36 requests
    fc_timeout_ms: int = 1000     # N_Bs
    suppress_wait: bool = False   # do not wait for P2 after a suppress-positive request
    any_negative: bool = False    # expected "7F" matches any negative response
    max_log_lines: int = 100000
    save_max_lines: int = 100000
    show_raw: bool = False
    auto_fc: bool = True


@dataclass
class Channel:
    channel: int = 0              # which STARTED channel (device manager) the diag/flash windows use
    fd_dl: int = 64               # ISO-TP frame length when the channel runs CAN FD
    zlgcan_dll: str = ""          # empty -> zlgcan.dll next to the program
    sim_profile: str = "new"      # simulator only: "old" (2-byte seed, DF FE) | "new" (16-byte, 01 02)


@dataclass
class DiagConfig:
    addr: Addressing = field(default_factory=Addressing)
    keepalive: KeepAlive = field(default_factory=KeepAlive)
    settings: Settings = field(default_factory=Settings)
    serial_id_map: Dict[str, List[int]] = field(
        default_factory=lambda: {"7e2": [0, 0], "7e3": [1, 3], "7e8": [2, 4]})

    def to_dict(self) -> dict:
        return {"addr": self.addr.to_dict(), "keepalive": asdict(self.keepalive),
                "settings": asdict(self.settings), "serial_id_map": self.serial_id_map}

    @staticmethod
    def from_dict(d: dict) -> "DiagConfig":
        c = DiagConfig()
        d = d or {}
        c.addr = Addressing.from_dict(d.get("addr", {}))
        _fill(c.keepalive, d.get("keepalive", {}))
        _fill(c.settings, d.get("settings", {}))
        if "serial_id_map" in d:
            c.serial_id_map = {str(k).lower(): list(v) for k, v in d["serial_id_map"].items()}
        return c

    def id_map_int(self) -> Dict[int, tuple]:
        return {int(k, 16): (int(v[0]), int(v[1])) for k, v in self.serial_id_map.items()}


# --------------------------------------------------------------------------- flash steps
STEP_TYPES = ("service", "delay", "unlock", "driver", "download", "routine")

_STEP_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "service": {"name": "", "addr": "phys", "pdu": "", "suppress": False, "check": True, "expect": ""},
    "delay": {"name": "延时", "ms": 1000},
    "unlock": {"name": "ECU解锁", "level": 1, "dll": "", "workdir": "", "use_serial": False},
    "driver": {"name": "Flash driver", "file": "", "base_addr": "0", "addr_len": 4, "size_len": 4, "dfi": "00"},
    "download": {"name": "文件下载", "file": "", "base_addr": "0", "addr_len": 4, "size_len": 4, "dfi": "00"},
    "routine": {"name": "例程控制", "addr": "phys", "sub": "01", "rid": "", "data": "",
                "use_crc": False, "crc": CrcSpec().to_dict(), "crc_order": "big",
                "source": "", "per_region": True, "addr_len": 4, "size_len": 4,
                "expect": "", "status_ok": ""},
}


def new_step(kind: str, **kw) -> Dict[str, Any]:
    if kind not in STEP_TYPES:
        raise ValueError(f"unknown step type {kind}")
    s = copy.deepcopy(_STEP_DEFAULTS[kind])
    s["type"] = kind
    s.update(kw)
    return s


def normalize_step(d: dict) -> Dict[str, Any]:
    kind = d.get("type")
    base = new_step(kind)
    base.update(copy.deepcopy(d))
    return base


def step_summary(s: dict) -> str:
    t = s["type"]
    if t == "service":
        return fmt_hex(parse_hex(s["pdu"])) if s.get("pdu", "").strip() else ""
    if t == "delay":
        return f"{s['ms']} ms"
    if t == "unlock":
        import os
        return f"级别:{s['level']} DLL:{os.path.basename(s['dll']) if s['dll'] else ''}"
    if t in ("driver", "download"):
        import os
        return f"文件名:{os.path.basename(s['file']) if s['file'] else ''}"
    if t == "routine":
        txt = f"31 {s['sub'].strip().upper()} {fmt_hex(parse_hex(s['rid'])) if s['rid'].strip() else ''}".strip()
        if s.get("data", "").strip():
            txt += " " + s["data"].strip()
        if s.get("use_crc"):
            txt += f" [{s['crc'].get('name', 'CRC')}]"
        return txt
    return ""


@dataclass
class Project:
    name: str = "新建方案"
    can_type: str = "CAN"
    cfg: DiagConfig = field(default_factory=DiagConfig)
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"format": "myzcanpro-flash", "version": FORMAT_VERSION, "name": self.name,
                "can_type": self.can_type, "config": self.cfg.to_dict(), "steps": self.steps}

    @staticmethod
    def from_dict(d: dict) -> "Project":
        if d.get("format") != "myzcanpro-flash":
            raise ValueError("不是 myzcanpro 刷新方案文件")
        p = Project(name=d.get("name", "未命名"), can_type=d.get("can_type", "CAN"))
        p.cfg = DiagConfig.from_dict(d.get("config", {}))
        p.steps = [normalize_step(s) for s in d.get("steps", [])]
        return p

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @staticmethod
    def load(path: str) -> "Project":
        with open(path, "r", encoding="utf-8") as f:
            return Project.from_dict(json.load(f))


def sample_project(product: str = "new") -> Project:
    """Mirrors the sample plan in the ZCANPro screenshot.  product: 'new' | 'old'."""
    rid = "01 02" if product == "new" else "DF FE"
    p = Project(name=f"sample-{product}")
    p.steps = [
        new_step("service", name="进入扩展模式", pdu="10 03"),
        new_step("service", name="关闭DTC设置", pdu="85 02"),
        new_step("service", name="关闭非诊断通信", pdu="28 01 01"),
        new_step("service", name="进入编程模式", pdu="10 02"),
        new_step("unlock", level=1, dll="sa_demo.dll"),
        new_step("driver"),
        new_step("download"),
        new_step("routine", name="CRC校验", sub="01", rid=rid, use_crc=True,
                 crc=PRESETS["CRC32"].to_dict()),
        new_step("service", name="ECU复位", pdu="11 01"),
    ]
    return p
