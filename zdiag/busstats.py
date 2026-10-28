"""Per-channel configuration, frame counters and bus-load estimation (no Qt, no DLL)."""
from __future__ import annotations
import threading
from dataclasses import dataclass
from typing import Optional

from .bus import CanFrame


@dataclass
class ChannelCfg:
    protocol: str = "CAN"            # "CAN" | "CAN FD"
    bitrate_kbps: float = 500.0      # arbitration-phase rate, kbaud/s
    data_kbps: float = 2000.0        # CAN FD data-phase rate, kbaud/s
    listen_only: bool = False
    terminator: bool = True
    calc_load: bool = True
    load_interval_ms: int = 200

    @property
    def bitrate(self) -> int:
        return int(round(self.bitrate_kbps * 1000))

    @property
    def data_bitrate(self) -> int:
        return int(round(self.data_kbps * 1000))

    @property
    def is_fd(self) -> bool:
        return self.protocol == "CAN FD"

    def summary(self) -> str:
        s = f"{self.protocol} {self.bitrate_kbps:g}k"
        if self.is_fd:
            s += f"/{self.data_kbps:g}k"
        return s + (" 只听" if self.listen_only else "")

    def validate(self) -> None:
        if not 5 <= self.bitrate_kbps <= 1000:
            raise ValueError("波特率应在 5~1000 kbaud/s")
        if self.is_fd and not self.bitrate_kbps <= self.data_kbps <= 8000:
            raise ValueError("数据域波特率应不低于仲裁域且不超过 8000 kbaud/s")
        if self.calc_load and not 20 <= self.load_interval_ms <= 10000:
            raise ValueError("负载率刷新时间应在 20~10000 ms")


# ---------------------------------------------------------------- bus-load model
def _crc15(bits) -> int:
    crc = 0
    for b in bits:
        top = (crc >> 14) & 1
        crc = (crc << 1) & 0x7FFF
        if top ^ b:
            crc ^= 0x4599
    return crc


def _stuffed_len(bits) -> int:
    n, run, last = 0, 0, None
    for b in bits:
        n += 1
        if b == last:
            run += 1
        else:
            last, run = b, 1
        if run == 5:                       # a stuff bit (complement) follows and starts a new run
            n += 1
            last, run = 1 - b, 1
    return n


def _bits_of(value: int, width: int):
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def frame_seconds(fr: CanFrame, abit: int, dbit: int = 0) -> float:
    """Time the frame occupies the bus.  Classic CAN: exact (real stuff bits).
    CAN FD: estimate (stuffing in the arbitration/data phases approximated)."""
    n = len(fr.data)
    if not fr.fd:
        bits = [0]                                                   # SOF
        if fr.extended:
            bits += _bits_of(fr.can_id >> 18 & 0x7FF, 11) + [1, 1] + _bits_of(fr.can_id & 0x3FFFF, 18) + [0]
        else:
            bits += _bits_of(fr.can_id & 0x7FF, 11) + [0, 0]         # RTR, IDE
        bits += [0, 0] if fr.extended else [0]                       # ext: r1, r0 | std: r0
        bits += _bits_of(min(n, 8), 4)
        for byte in fr.data[:8]:
            bits += _bits_of(byte, 8)
        bits += _bits_of(_crc15(bits), 15)
        total = _stuffed_len(bits) + 1 + 1 + 1 + 7 + 3               # CRC del, ACK, ACK del, EOF, IFS
        return total / float(abit)
    # ---- CAN FD (estimate)
    fixed_arb = (1 + 11 + 1 + 1 + 1 + 1) if not fr.extended else (1 + 11 + 1 + 1 + 18 + 1 + 1 + 1)
    arb_bits = fixed_arb * 1.2 + 1 + 1 + 7 + 3                       # stuffing ~20 %; ACK, del, EOF, IFS
    crc_len = 17 if n <= 16 else 21
    data_bits = (1 + 1 + 4 + 8 * n) * 1.2 + crc_len + (crc_len + 3) // 4      # ESI, DLC, payload, CRC(+fixed stuff)
    if fr.brs and dbit:
        return arb_bits / float(abit) + data_bits / float(dbit)
    return (arb_bits + data_bits) / float(abit)


class BusStats:
    """Thread-safe counters + busy-time integrator used to derive the bus load."""

    def __init__(self, abit: int = 500_000, dbit: int = 2_000_000) -> None:
        self.abit, self.dbit = abit, dbit
        self.rx = self.tx = self.err = 0
        self._busy = 0.0
        self._lock = threading.Lock()
        self._last_t: Optional[float] = None
        self._last_busy = 0.0
        self.load: Optional[float] = None

    def count_rx(self, fr: CanFrame) -> None:
        with self._lock:
            self.rx += 1
            self._busy += frame_seconds(fr, self.abit, self.dbit)

    def count_tx(self, fr: CanFrame) -> None:
        with self._lock:
            self.tx += 1
            self._busy += frame_seconds(fr, self.abit, self.dbit)

    def count_err(self, n: int = 1) -> None:
        with self._lock:
            self.err += n

    def snapshot(self):
        with self._lock:
            return self.rx, self.tx, self.err

    def reset(self) -> None:
        with self._lock:
            self.rx = self.tx = self.err = 0

    def sample_load(self, now: float) -> Optional[float]:
        """Percent of bus time used since the previous call (None on the first call)."""
        with self._lock:
            if self._last_t is None:
                self._last_t, self._last_busy = now, self._busy
                return None
            dt = now - self._last_t
            if dt <= 0:
                return self.load
            self.load = min(100.0, (self._busy - self._last_busy) / dt * 100.0)
            self._last_t, self._last_busy = now, self._busy
            return self.load


class CountingBus:
    """Wraps any Bus (used for the simulator) so it feeds a BusStats like the real channels do."""

    def __init__(self, bus, stats: BusStats) -> None:
        self._bus, self.stats = bus, stats

    def send(self, fr: CanFrame) -> None:
        self._bus.send(fr)
        self.stats.count_tx(fr)

    def recv(self, timeout: float):
        fr = self._bus.recv(timeout)
        if fr is not None:
            self.stats.count_rx(fr)
        return fr

    def flush(self) -> None:
        self._bus.flush()

    def close(self) -> None:
        self._bus.close()
