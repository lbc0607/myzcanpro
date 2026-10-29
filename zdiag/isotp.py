"""ISO 15765-2 (ISO-TP) transport layer, normal addressing, classic CAN and CAN FD.

Timing parameters (ISO 15765-2) and where they appear in this file:
  N_As  sender   : own frame transmit time      -> Bus.send() must return (IsoTpError on failure)
  N_Bs  sender   : wait for FlowControl          -> _wait_fc()         timeout 1.0 s
  N_Cs  sender   : gap between consecutive frames-> STmin wait in send()
  N_Cr  receiver : wait for next consecutive     -> recv()             timeout 1.0 s
  N_Ar  receiver : own FC transmit time          -> Bus.send()
  N_Br  receiver : time until FC is sent         -> we send FC immediately
"""
from __future__ import annotations
import time
from typing import Optional

from .bus import Bus, CanFrame

_FD_LENGTHS = (8, 12, 16, 20, 24, 32, 48, 64)


class IsoTpError(Exception):
    pass


class IsoTpTimeout(IsoTpError):
    def __init__(self, which: str):
        super().__init__(f"ISO-TP timeout {which}")
        self.which = which


def decode_stmin(raw: int) -> float:
    """STmin byte -> seconds.  Reserved values are treated as 127 ms (ISO 15765-2)."""
    if raw <= 0x7F:
        return raw / 1000.0
    if 0xF1 <= raw <= 0xF9:
        return (raw - 0xF0) * 100e-6
    return 0.127


def _wait_until(t: float) -> None:
    """Sleep until perf_counter() == t.  Sleeps coarsely, spins for the last ~1.5 ms
    (Windows timer granularity would otherwise destroy STmin < 2 ms)."""
    while True:
        r = t - time.perf_counter()
        if r <= 0:
            return
        if r > 0.0025:
            time.sleep(r - 0.0015)


class IsoTp:
    def __init__(self, bus: Bus, tx_id: int, rx_id: int, *, extended: bool = False,
                 tx_dl: int = 8, block_size: int = 0, st_min: int = 0,
                 padding: Optional[int] = 0x55, n_bs: float = 1.0, n_cr: float = 1.0,
                 max_wft: int = 10, rx_max: int = 1 << 20, brs: bool = True,
                 stmin_override: Optional[float] = None, auto_fc: bool = True) -> None:
        if tx_dl != 8 and tx_dl not in _FD_LENGTHS:
            raise ValueError("tx_dl must be 8 or a CAN FD length")
        self.bus, self.tx_id, self.rx_id, self.extended = bus, tx_id, rx_id, extended
        self.tx_dl, self.block_size, self.st_min = tx_dl, block_size, st_min
        self.padding, self.n_bs, self.n_cr, self.max_wft = padding, n_bs, n_cr, max_wft
        self.rx_max, self.brs = rx_max, brs
        self.stmin_override = stmin_override      # seconds; replaces the STmin the ECU asks for
        self.auto_fc = auto_fc                    # False: never answer FF/CF with a FlowControl

    # ------------------------------------------------------------ low level
    def flush(self) -> None:
        self.bus.flush()

    def _tx(self, data: bytes) -> None:
        fd = self.tx_dl > 8
        if fd:
            target = next(v for v in _FD_LENGTHS if v >= len(data))
        else:
            target = 8 if self.padding is not None else len(data)
        if len(data) < target:
            data = data + bytes([self.padding if self.padding is not None else 0]) * (target - len(data))
        try:
            self.bus.send(CanFrame(self.tx_id, data, self.extended, fd, fd and self.brs))
        except Exception as e:                      # N_As / N_Ar failure
            raise IsoTpError(f"CAN transmit failed: {e}") from e

    def _rx(self, timeout: float) -> Optional[bytes]:
        """Next frame with our rx_id (everything else on the bus is dropped here)."""
        deadline = time.perf_counter() + timeout
        while True:
            fr = self.bus.recv(max(deadline - time.perf_counter(), 0))
            if fr is None:
                return None
            if fr.can_id == self.rx_id and fr.extended == self.extended and fr.data:
                return fr.data
            if time.perf_counter() >= deadline:
                return None

    # ------------------------------------------------------------ send
    def send(self, data: bytes) -> None:
        n, dl = len(data), self.tx_dl
        sf_max = dl - 1 if dl <= 8 else dl - 2
        if n == 0:
            raise ValueError("empty payload")
        if n <= sf_max:
            hdr = bytes([n]) if dl <= 8 else bytes([0x00, n])
            self._tx(hdr + data)
            return
        hdr = bytes([0x10 | (n >> 8), n & 0xFF]) if n <= 4095 else \
            bytes([0x10, 0x00]) + n.to_bytes(4, "big")
        cap = dl - len(hdr)
        self._tx(hdr + data[:cap])
        pos, sn = cap, 1
        while pos < n:
            bs, stmin = self._wait_fc()
            if self.stmin_override is not None:
                stmin = self.stmin_override
            in_block, next_t = 0, time.perf_counter()
            while pos < n:
                _wait_until(next_t)                                  # N_Cs (STmin)
                chunk = data[pos:pos + dl - 1]
                self._tx(bytes([0x20 | sn]) + chunk)
                next_t = time.perf_counter() + stmin
                pos += len(chunk)
                sn = (sn + 1) & 0xF
                in_block += 1
                if bs and in_block >= bs:
                    break

    def _wait_fc(self):
        deadline = time.perf_counter() + self.n_bs
        wft = 0
        while True:
            d = self._rx(deadline - time.perf_counter())
            if d is None:
                raise IsoTpTimeout("N_Bs")
            if d[0] >> 4 != 3:
                continue                                         # not a FlowControl
            fs = d[0] & 0xF
            if fs == 0 and len(d) >= 3:
                return d[1], decode_stmin(d[2])
            if fs == 1:                                          # WAIT
                wft += 1
                if wft > self.max_wft:
                    raise IsoTpError("too many FC(WAIT)")
                deadline = time.perf_counter() + self.n_bs
            elif fs == 2:
                raise IsoTpError("receiver reported buffer overflow")
            else:
                raise IsoTpError(f"invalid FlowStatus {fs}")

    # ------------------------------------------------------------ receive
    def recv(self, timeout: float) -> Optional[bytes]:
        """One complete ISO-TP message, or None if nothing started within `timeout`."""
        deadline = time.perf_counter() + timeout
        while True:
            d = self._rx(max(deadline - time.perf_counter(), 0))
            if d is None:
                return None
            t = d[0] >> 4
            if t == 0:                                           # SingleFrame
                n = d[0] & 0xF
                body = d[1:1 + n] if n else d[2:2 + d[1]] if len(d) > 1 else b""
                if body:
                    return bytes(body)
            elif t == 1:                                         # FirstFrame
                return self._recv_multi(d)
            # stray CF / FC: ignore
            if time.perf_counter() >= deadline:
                return None

    def _recv_multi(self, ff: bytes) -> bytes:
        total = ((ff[0] & 0xF) << 8) | ff[1]
        body = ff[2:]
        if total == 0:
            total = int.from_bytes(ff[2:6], "big")
            body = ff[6:]
        if total > self.rx_max:
            self._tx(bytes([0x32, 0, 0]))
            raise IsoTpError(f"incoming message too large ({total})")
        buf = bytearray(body[:total])
        if self.auto_fc:
            self._tx(bytes([0x30, self.block_size, self.st_min]))
        sn, in_block = 1, 0
        while len(buf) < total:
            d = self._rx(self.n_cr)
            if d is None:
                raise IsoTpTimeout("N_Cr")
            if d[0] >> 4 != 2:
                continue                                         # ignore anything but CF
            if d[0] & 0xF != sn:
                raise IsoTpError(f"bad sequence number {d[0] & 0xF}, expected {sn}")
            buf += d[1:1 + total - len(buf)]
            sn = (sn + 1) & 0xF
            in_block += 1
            if self.block_size and in_block >= self.block_size and len(buf) < total:
                if self.auto_fc:
                    self._tx(bytes([0x30, self.block_size, self.st_min]))
                in_block = 0
        return bytes(buf)
