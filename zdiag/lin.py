"""LIN diagnostics (UDS over LIN, ISO 17987-2 / LIN 2.x transport layer).

Layers:
  LinMaster      what a LIN master driver offers: publish a frame / read a slave response (SimLinMaster for tests;
                 the ZLG box binding is a separate LinMaster implementation, see zdiag/zlglin.py)
  LinTp          NAD + PCI transport on master request 0x3C / slave response 0x3D; same interface as IsoTp
                 (tx_id = NAD, flush / send / recv), so zdiag.uds.UdsClient runs on it unchanged
  SimLinSlave    a tiny UDS slave (10 / 3E / 22) to dry-run everything without hardware
  scan_nodes / scan_pump   discover NADs that answer, read identification DIDs of one node
"""
from __future__ import annotations

import queue
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from .uds import NegativeResponse, UdsClient, UdsError, UdsTimeout

MASTER_REQ_ID = 0x3C
SLAVE_RESP_ID = 0x3D
NAD_FUNCTIONAL = 0x7E
NAD_WILDCARD = 0x7F
PAD = 0xFF


class LinError(Exception):
    pass


# --------------------------------------------------------------------------- frame helpers
def protected_id(fid: int) -> int:
    """6-bit frame ID -> 8-bit protected ID (parity bits P0 / P1)."""
    fid &= 0x3F
    b = [(fid >> i) & 1 for i in range(6)]
    p0 = b[0] ^ b[1] ^ b[2] ^ b[4]
    p1 = 1 ^ (b[1] ^ b[3] ^ b[4] ^ b[5])
    return fid | (p0 << 6) | (p1 << 7)


def checksum(data: bytes, pid: int = -1) -> int:
    """LIN checksum: classic (pid < 0, used by 0x3C / 0x3D) or enhanced (pid included)."""
    s = pid if pid >= 0 else 0
    for b in data:
        s += b
        if s > 0xFF:
            s -= 0xFF
    return (~s) & 0xFF


# --------------------------------------------------------------------------- master driver interface
class LinMaster(ABC):
    @abstractmethod
    def send_frame(self, frame_id: int, data: bytes) -> None:
        """Master publishes `data` (<= 8 bytes) under `frame_id` (header + response by the master)."""

    @abstractmethod
    def read_frame(self, frame_id: int, timeout: float) -> Optional[bytes]:
        """Master sends the header of `frame_id`; returns the slave's response data, or None when nobody answered."""

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def uds_request(self, nad: int, pdu: bytes, p2: float):
        """Drivers whose library runs the UDS transport itself (ZLG) return {"ok", "data" | "nrc", "error"} here; None = use LinTp."""
        return None


# --------------------------------------------------------------------------- transport (NAD + PCI)
class LinTp:
    """Diagnostic transport on 0x3C / 0x3D.  `tx_id` is the NAD of the addressed node (0x7E = functional)."""

    def __init__(self, master: LinMaster, nad: int, *, st_min: float = 0.0, n_cr: float = 1.0, poll: float = 0.02) -> None:
        if not 0 <= nad <= 0xFF:
            raise ValueError("NAD 应在 0..255")
        self.master, self.tx_id, self.st_min, self.n_cr, self.poll = master, nad, st_min, n_cr, poll

    def flush(self) -> None:
        self.master.flush()

    @staticmethod
    def _pad(b: bytes) -> bytes:
        return b + bytes([PAD]) * (8 - len(b))

    def send(self, data: bytes) -> None:
        n = len(data)
        if n == 0:
            raise ValueError("empty payload")
        nad = self.tx_id
        try:
            if n <= 6:
                self.master.send_frame(MASTER_REQ_ID, self._pad(bytes([nad, n]) + data))
                return
            if n > 0xFFF:
                raise ValueError("payload too long for LIN transport")
            self.master.send_frame(MASTER_REQ_ID, bytes([nad, 0x10 | (n >> 8), n & 0xFF]) + data[:5])
            sn, pos = 1, 5
            while pos < n:
                if self.st_min:
                    time.sleep(self.st_min)
                chunk = data[pos:pos + 6]
                self.master.send_frame(MASTER_REQ_ID, self._pad(bytes([nad, 0x20 | sn]) + chunk))
                sn, pos = (sn + 1) & 0x0F, pos + 6
        except LinError:
            raise
        except ValueError:
            raise
        except Exception as e:
            raise LinError(f"LIN 发送失败: {e}") from e

    def _poll(self, timeout: float) -> Optional[bytes]:
        """Next slave response frame of our node (header 0x3D is repeated until something arrives)."""
        deadline = time.perf_counter() + timeout
        while True:
            left = deadline - time.perf_counter()
            fr = self.master.read_frame(SLAVE_RESP_ID, max(min(left, self.poll), 0))
            if fr is not None and len(fr) >= 2 and (fr[0] == self.tx_id or self.tx_id in (NAD_FUNCTIONAL, NAD_WILDCARD)):
                return bytes(fr)
            if time.perf_counter() >= deadline:
                return None

    def recv(self, timeout: float) -> Optional[bytes]:
        fr = self._poll(timeout)
        if fr is None:
            return None
        pci = fr[1] >> 4
        if pci == 0:
            n = fr[1] & 0x0F
            return bytes(fr[2:2 + n]) if 1 <= n <= 6 else None
        if pci != 1 or len(fr) < 3:
            return None
        total = ((fr[1] & 0x0F) << 8) | fr[2]
        buf = bytearray(fr[3:8])
        nad, sn = fr[0], 1
        while len(buf) < total:
            cf = self._poll(self.n_cr)
            if cf is None or cf[0] != nad or cf[1] >> 4 != 2:
                return None
            if (cf[1] & 0x0F) != sn:
                return None
            buf += cf[2:8]
            sn = (sn + 1) & 0x0F
        return bytes(buf[:total])


# --------------------------------------------------------------------------- simulation
class SimLinSlave:
    """UDS slave: sessions (10), TesterPresent (3E), ReadDataByIdentifier (22).  `dids` maps DID -> bytes."""

    def __init__(self, nad: int, dids: Dict[int, bytes]) -> None:
        self.nad, self.dids, self.session = nad, dict(dids), 1
        self._rx = bytearray()
        self._rx_total = 0
        self._rx_sn = 0
        self.out: "queue.Queue[bytes]" = queue.Queue()

    def feed(self, fr: bytes) -> None:
        if len(fr) < 2 or fr[0] != self.nad:                      # physical requests only (functional ones are not answered)
            return
        pci = fr[1] >> 4
        if pci == 0:
            n = fr[1] & 0x0F
            if 1 <= n <= 6:
                self._handle(bytes(fr[2:2 + n]))
        elif pci == 1 and len(fr) >= 3:
            self._rx_total = ((fr[1] & 0x0F) << 8) | fr[2]
            self._rx = bytearray(fr[3:8])
            self._rx_sn = 1
        elif pci == 2 and self._rx_total:
            if (fr[1] & 0x0F) != self._rx_sn:
                self._rx_total = 0
                return
            self._rx += fr[2:8]
            self._rx_sn = (self._rx_sn + 1) & 0x0F
            if len(self._rx) >= self._rx_total:
                pdu = bytes(self._rx[:self._rx_total])
                self._rx_total = 0
                self._handle(pdu)

    def _handle(self, pdu: bytes) -> None:
        sid = pdu[0]
        if sid == 0x10 and len(pdu) == 2:
            self.session = pdu[1] & 0x7F
            self._reply(bytes([0x50, pdu[1] & 0x7F, 0x00, 0x32, 0x01, 0xF4]))
        elif sid == 0x3E and len(pdu) == 2:
            if not pdu[1] & 0x80:
                self._reply(bytes([0x7E, 0x00]))
        elif sid == 0x22 and len(pdu) >= 3 and len(pdu) % 2 == 1:
            out = bytearray([0x62])
            for i in range(1, len(pdu), 2):
                did = int.from_bytes(pdu[i:i + 2], "big")
                if did not in self.dids:
                    self._reply(bytes([0x7F, 0x22, 0x31]))
                    return
                out += pdu[i:i + 2] + self.dids[did]
            self._reply(bytes(out))
        elif sid in (0x10, 0x3E, 0x22):
            self._reply(bytes([0x7F, sid, 0x13]))
        else:
            self._reply(bytes([0x7F, sid, 0x11]))

    def _reply(self, data: bytes) -> None:
        n = len(data)
        if n <= 6:
            self.out.put(LinTp._pad(bytes([self.nad, n]) + data))
            return
        self.out.put(bytes([self.nad, 0x10 | (n >> 8), n & 0xFF]) + data[:5])
        sn, pos = 1, 5
        while pos < n:
            self.out.put(LinTp._pad(bytes([self.nad, 0x20 | sn]) + data[pos:pos + 6]))
            sn, pos = (sn + 1) & 0x0F, pos + 6


PUMP_DIDS = {
    0xF187: b"OP-1000-A2",          # 零件号
    0xF189: b"SW1.04.7",            # 软件版本
    0xF18A: b"DEMO PUMPS Co.",      # 供应商
    0xF18C: b"SN20260417-0042",     # 序列号
    0xF191: b"HW-B3",               # 硬件版本
    0xF193: b"B3.1",
    0xF195: b"1.04.7",
}


class SimLinMaster(LinMaster):
    """In-memory LIN bus with simulated slaves (default: an oil pump on NAD 0x21)."""

    def __init__(self, slaves: Optional[List[SimLinSlave]] = None) -> None:
        self.slaves = slaves if slaves is not None else [SimLinSlave(0x21, PUMP_DIDS)]
        self.log: List[tuple] = []                                 # (frame_id, data) published by the master
        self.closed = False

    def send_frame(self, frame_id: int, data: bytes) -> None:
        if self.closed:
            raise LinError("LIN 通道已关闭")
        self.log.append((frame_id, bytes(data)))
        if frame_id == MASTER_REQ_ID:
            for s in self.slaves:
                s.feed(bytes(data))

    def read_frame(self, frame_id: int, timeout: float) -> Optional[bytes]:
        if self.closed:
            raise LinError("LIN 通道已关闭")
        if frame_id != SLAVE_RESP_ID:
            return None
        for s in self.slaves:
            try:
                return s.out.get_nowait()
            except queue.Empty:
                pass
        if timeout > 0:
            time.sleep(min(timeout, 0.002))
        return None

    def flush(self) -> None:
        for s in self.slaves:
            while True:
                try:
                    s.out.get_nowait()
                except queue.Empty:
                    break

    def close(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- high level
def client(master: LinMaster, nad: int, p2: float = 0.3) -> UdsClient:
    c = UdsClient(LinTp(master, nad), p2=p2)
    c.auto_keepalive = False
    return c


def request(master: LinMaster, nad: int, pdu: bytes, p2: float = 0.3) -> Dict[str, Any]:
    """One UDS request to a node -> {"ok", "data"} | {"ok": False, "nrc", "error"}."""
    direct = master.uds_request(nad, pdu, p2)
    if direct is not None:
        return direct
    try:
        rsp = client(master, nad, p2).request(pdu)
        return {"ok": True, "data": rsp}
    except NegativeResponse as e:
        return {"ok": False, "nrc": e.nrc, "error": str(e), "data": bytes([0x7F, e.sid, e.nrc])}
    except UdsTimeout:
        return {"ok": False, "error": "无响应"}
    except UdsError as e:
        return {"ok": False, "error": str(e)}


def scan_nodes(master: LinMaster, first: int = 0x01, last: int = 0x7D, p2: float = 0.05,
               cancel: Optional[threading.Event] = None, progress: Optional[Callable[[int, int], None]] = None) -> List[int]:
    """NADs that answer a TesterPresent (3E 00); a negative response also counts as 'there is a node'."""
    found: List[int] = []
    total = last - first + 1
    for i, nad in enumerate(range(first, last + 1)):
        if cancel is not None and cancel.is_set():
            break
        r = request(master, nad, bytes([0x3E, 0x00]), p2)
        if r["ok"] or "nrc" in r:
            found.append(nad)
        if progress:
            progress(i + 1, total)
    return found


STANDARD_DIDS = [(0xF187, "零件号"), (0xF189, "软件版本"), (0xF18A, "供应商"), (0xF18C, "序列号"),
                 (0xF191, "硬件版本"), (0xF193, "硬件版本号"), (0xF195, "软件版本号")]


def render(raw: bytes, parse: str = "auto") -> str:
    if parse == "auto":
        parse = "ascii" if raw and all(0x20 <= b < 0x7F for b in raw) else "hex"
    if parse == "ascii":
        return raw.decode("ascii", "replace").rstrip("\x00 ")
    if parse == "dec":
        return str(int.from_bytes(raw, "big")) if raw else ""
    return raw.hex(" ").upper()


def scan_pump(master: LinMaster, nad: int, dids: Optional[List[tuple]] = None, session: bool = True,
              p2: float = 0.3) -> Dict[str, Any]:
    """Read the identification DIDs of one node. `dids` = [(did:int, label:str, parse:str)]; default STANDARD_DIDS."""
    items = [(d[0], d[1] if len(d) > 1 else f"{d[0]:04X}", d[2] if len(d) > 2 else "auto") for d in (dids or STANDARD_DIDS)]
    out: Dict[str, Any] = {"nad": f"{nad:02X}", "items": [], "ok": True}
    if session:
        r = request(master, nad, bytes([0x10, 0x03]), p2)          # extended session; a node without it still answers 22
        out["session"] = "ok" if r["ok"] else r.get("error", "")
    alive = False
    for did, label, parse in items:
        r = request(master, nad, bytes([0x22]) + did.to_bytes(2, "big"), p2)
        row: Dict[str, Any] = {"did": f"{did:04X}", "label": label}
        if r["ok"]:
            alive = True
            raw = bytes(r["data"][3:])
            row.update(ok=True, raw=raw.hex(" ").upper(), value=render(raw, parse))
        else:
            if "nrc" in r:
                alive = True
            row.update(ok=False, error=r.get("error", ""))
            if r.get("nrc") is not None:
                row["nrc"] = f"{r['nrc']:02X}"
        out["items"].append(row)
    if not alive:
        out.update(ok=False, error=f"NAD 0x{nad:02X} 没有响应：检查 LIN 供电/波特率/NAD")
    return out
