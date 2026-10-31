"""Passive frame monitor (no Qt): a thread-safe tap fed by the channel rx threads / send paths.

The tap never reads from the device itself - the channel's single rx thread offers every frame to the UDS queue
*and* to the tap, so diagnostics/flashing and the monitor cannot steal frames from each other.  feed() only does a
dict update under a lock (microseconds); the GUI polls snapshot() from its own thread."""
from __future__ import annotations
import collections
import queue
import threading
import time
from typing import Deque, Dict, List, Optional, Tuple

from .bus import CanFrame

TX, RX = "TX", "RX"


def frame_type(fr: CanFrame) -> str:
    return "CAN FD" if fr.fd else "CAN"


def fmt_id(can_id: int, extended: bool) -> str:
    return f"{can_id:08X}" if extended else f"{can_id:03X}"


def fmt_data(fr: CanFrame) -> str:
    if fr.rtr:
        return "[远程帧]"
    return fr.data.hex(" ").upper()


class Row:
    __slots__ = ("seq", "ch", "dir", "can_id", "ext", "fd", "brs", "rtr", "data", "count", "ts", "dt_id", "dt_prev")

    def __init__(self, seq, ch, direction, fr: CanFrame, ts: float, dt_prev: float) -> None:
        self.seq, self.ch, self.dir = seq, ch, direction
        self.can_id, self.ext, self.fd, self.brs, self.rtr = fr.can_id, fr.extended, fr.fd, fr.brs, fr.rtr
        self.data = bytes(fr.data)
        self.count = 1
        self.ts = ts
        self.dt_id = 0.0                 # seconds since the previous frame with the same key
        self.dt_prev = dt_prev           # seconds since the previous frame on the bus (any id)

    def copy(self) -> "Row":
        r = Row.__new__(Row)
        for k in Row.__slots__:
            setattr(r, k, getattr(self, k))
        return r

    @property
    def id_text(self) -> str:
        return fmt_id(self.can_id, self.ext)

    @property
    def type_text(self) -> str:
        return "CAN FD" if self.fd else "CAN"

    @property
    def data_text(self) -> str:
        return "[远程帧]" if self.rtr else self.data.hex(" ").upper()


class CsvRecorder:
    """Real-time save: every frame (chronological) is appended to a CSV file by a background thread."""
    HEADER = "序号,时间标识(s),通道,帧ID,CAN类型,方向,长度,数据\n"
    MAX_BACKLOG = 500_000

    def __init__(self, path: str) -> None:
        self.path = path
        self.written = 0
        self.dropped = 0
        self._q: "queue.SimpleQueue" = queue.SimpleQueue()
        self._f = open(path, "w", encoding="utf-8-sig", newline="")          # BOM: Excel opens Chinese headers correctly
        self._f.write(self.HEADER)
        self._thr = threading.Thread(target=self._run, name="monitor-save", daemon=True)
        self._thr.start()

    def put(self, line: str) -> None:
        if self._q.qsize() > self.MAX_BACKLOG:
            self.dropped += 1
        else:
            self._q.put(line)

    def _run(self) -> None:
        while True:
            line = self._q.get()
            if line is None:
                break
            batch = [line]
            try:
                while len(batch) < 2000:
                    nxt = self._q.get_nowait()
                    if nxt is None:
                        self._q.put(None)
                        break
                    batch.append(nxt)
            except queue.Empty:
                pass
            try:
                self._f.write("".join(batch))
            except OSError:
                pass
            self.written += len(batch)

    def close(self) -> None:
        self._q.put(None)
        self._thr.join(timeout=5)
        try:
            self._f.close()
        except OSError:
            pass


class FrameTap:
    RING = 5000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()
        self.recorder: Optional[CsvRecorder] = None

    def reset(self) -> None:
        with self._lock:
            self._rows: Dict[Tuple, Row] = {}
            self._ring: Deque[Row] = collections.deque(maxlen=self.RING)
            self._seq = 0
            self._t0: Optional[float] = None
            self._last_ts: Optional[float] = None
            self.errors = 0
            self.total = 0

    # ------------------------------------------------------------ producers (rx threads / send paths)
    def feed(self, ch: int, fr: CanFrame, direction: str, ts: Optional[float] = None) -> None:
        ts = time.perf_counter() if ts is None else ts
        with self._lock:
            if self._t0 is None:
                self._t0 = ts
            dt_prev = 0.0 if self._last_ts is None else max(0.0, ts - self._last_ts)
            self._last_ts = max(ts, self._last_ts or ts)
            self.total += 1
            key = (ch, direction, fr.can_id, fr.extended)
            row = self._rows.get(key)
            if row is None:
                self._seq += 1
                row = Row(self._seq, ch, direction, fr, ts, dt_prev)
                self._rows[key] = row
            else:
                row.dt_id = max(0.0, ts - row.ts)
                row.dt_prev = dt_prev
                row.ts, row.count = ts, row.count + 1
                row.data, row.fd, row.brs, row.rtr = bytes(fr.data), fr.fd, fr.brs, fr.rtr
            snap = row.copy()
            self._ring.append(snap)
            rec, t0, n = self.recorder, self._t0, self.total
        if rec is not None:
            rec.put(f"{n},{ts - t0:.6f},{ch},{fmt_id(fr.can_id, fr.extended)},{frame_type(fr)},"
                    f"{'发送' if direction == TX else '接收'},{len(fr.data)},{fmt_data(fr)}\n")

    def feed_err(self, ch: int) -> None:
        with self._lock:
            self.errors += 1

    # ------------------------------------------------------------ consumers (GUI thread)
    @property
    def t0(self) -> float:
        return self._t0 or 0.0

    def snapshot(self) -> List[Row]:
        with self._lock:
            return [r.copy() for r in sorted(self._rows.values(), key=lambda r: r.seq)]

    def trace(self, n: int = 2000) -> List[Row]:
        with self._lock:
            return list(self._ring)[-n:]

    # ------------------------------------------------------------ real-time save
    def start_record(self, path: str) -> None:
        self.stop_record()
        self.recorder = CsvRecorder(path)

    def stop_record(self) -> Optional[CsvRecorder]:
        rec, self.recorder = self.recorder, None
        if rec is not None:
            rec.close()
        return rec

    def save_snapshot(self, path: str, trace: bool = False) -> int:
        rows = self.trace(self.RING) if trace else self.snapshot()
        t0 = self.t0
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("序号,时间标识(s),通道,帧ID,CAN类型,方向,长度,数据,计数,与同ID上一帧时间差(ms),与上一帧时间差(ms)\n")
            for i, r in enumerate(rows, 1):
                f.write(f"{i},{r.ts - t0:.6f},{r.ch},{r.id_text},{r.type_text},{'发送' if r.dir == TX else '接收'},"
                        f"{len(r.data)},{r.data_text},{r.count},{r.dt_id * 1000:.3f},{r.dt_prev * 1000:.3f}\n")
        return len(rows)
