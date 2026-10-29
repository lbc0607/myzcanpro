"""普通发送: raw CAN / CAN FD frame list, scheduling and execution (no Qt).

Semantics (modelled on ZCANPro's 普通发送):
  * one *round* sends `per_send` frames back to back; a row runs `count` rounds, `interval_ms` apart
  * ID递增 / 数据递增 count frames across the whole row (frame index = round * per_send + k)
  * 并行发送模式: every row starts at t=0 on its own timeline (rows interleave by time)
    串行发送模式: rows run one after another
  * 发送速度 N 倍: all intervals are divided by N
"""
from __future__ import annotations
import json
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Callable, Dict, List, Optional

from .bus import CanFrame

FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
FORMAT, VERSION = "myzcanpro-send", 1
PARALLEL, SERIAL = "并行发送模式", "串行发送模式"


@dataclass
class SendItem:
    can_id: int = 0x7E2
    extended: bool = False
    remote: bool = False
    length: int = 8
    data: bytes = b""
    per_send: int = 1
    count: int = 1
    interval_ms: int = 0
    id_inc: bool = False
    data_inc: bool = False
    name: str = ""
    checked: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["can_id"] = f"{self.can_id:X}"
        d["data"] = self.data.hex(" ").upper()
        return d

    @staticmethod
    def from_dict(d: dict) -> "SendItem":
        it = SendItem()
        names = {f.name for f in fields(it)}
        for k, v in d.items():
            if k in names:
                setattr(it, k, v)
        it.can_id = int(str(d.get("can_id", it.can_id)), 16)
        it.data = bytes.fromhex(str(d.get("data", "")).replace(" ", ""))
        return it

    def validate(self, fd: bool = False) -> None:
        limit = 0x1FFFFFFF if self.extended else 0x7FF
        if not 0 <= self.can_id <= limit:
            raise ValueError(f"帧ID 0x{self.can_id:X} 超出{'扩展' if self.extended else '标准'}帧范围")
        if self.remote and fd:
            raise ValueError("CAN FD 不支持远程帧")
        if self.remote:
            if not 0 <= self.length <= 8:
                raise ValueError("远程帧的数据长度(DLC)应为 0~8")
        else:
            if fd and self.length not in FD_LENGTHS:
                raise ValueError(f"CAN FD 数据长度应为 {', '.join(map(str, FD_LENGTHS))} 之一")
            if not fd and not 0 <= self.length <= 8:
                raise ValueError("CAN 数据长度应为 0~8")
            if len(self.data) > self.length:
                raise ValueError(f"数据有 {len(self.data)} 字节，超出数据长度 {self.length}")
        if self.per_send < 1 or self.count < 1:
            raise ValueError("每次发送帧数、发送次数必须 ≥ 1")
        if self.interval_ms < 0:
            raise ValueError("每次间隔不能为负")

    @property
    def total_frames(self) -> int:
        return self.per_send * self.count


def frame_at(it: SendItem, index: int, fd: bool = False, brs: bool = False) -> CanFrame:
    """The `index`-th frame of a row (0-based), with ID / data increments applied."""
    limit = 0x1FFFFFFF if it.extended else 0x7FF
    ident = (it.can_id + index) & limit if it.id_inc else it.can_id
    if it.remote:
        return CanFrame(ident, bytes(it.length), it.extended, rtr=True)
    data = it.data.ljust(it.length, b"\x00")[:it.length]
    if it.data_inc and it.length:
        v = (int.from_bytes(data, "big") + index) % (1 << (8 * it.length))
        data = v.to_bytes(it.length, "big")                # big-endian counter over the whole data field
    return CanFrame(ident, data, it.extended, fd=fd, brs=brs and fd)


@dataclass
class Event:
    t: float                  # seconds from start
    row: int                  # index into the (selected) item list
    rnd: int
    frames: List[CanFrame] = field(default_factory=list)


def build_schedule(items: List[SendItem], mode: str = PARALLEL, speed: float = 1.0,
                   fd: bool = False, brs: bool = False) -> List[Event]:
    if speed <= 0:
        raise ValueError("发送速度必须大于 0")
    events: List[Event] = []
    offset = 0.0
    for row, it in enumerate(items):
        step = it.interval_ms / 1000.0 / speed
        base = offset if mode == SERIAL else 0.0
        for r in range(it.count):
            frames = [frame_at(it, r * it.per_send + k, fd, brs) for k in range(it.per_send)]
            events.append(Event(base + r * step, row, r, frames))
        if mode == SERIAL:
            offset = base + (it.count - 1) * step
    events.sort(key=lambda e: (e.t, e.row, e.rnd))
    return events


@dataclass
class SendResult:
    sent: int = 0
    failed: int = 0
    elapsed: float = 0.0
    cancelled: bool = False
    stopped_on_fail: bool = False
    per_row: Dict[int, List[int]] = field(default_factory=dict)      # row -> [sent, failed]

    @property
    def ok(self) -> bool:
        return not self.failed and not self.cancelled


def run_send(bus, items: List[SendItem], mode: str = PARALLEL, speed: float = 1.0, stop_on_fail: bool = True,
             fd: bool = False, brs: bool = False, cancel=None,
             on_status: Optional[Callable[[int, str], None]] = None,
             clock: Callable[[], float] = time.perf_counter,
             sleep: Callable[[float], None] = time.sleep) -> SendResult:
    for it in items:
        it.validate(fd)
    events = build_schedule(items, mode, speed, fd, brs)
    res = SendResult(per_row={i: [0, 0] for i in range(len(items))})
    remaining = {i: it.count for i, it in enumerate(items)}
    status = on_status or (lambda i, s: None)
    started = set()
    t0 = clock()
    for ev in events:
        while True:
            if cancel is not None and cancel.is_set():
                res.cancelled = True
                break
            wait = ev.t - (clock() - t0)
            if wait <= 0:
                break
            sleep(min(wait, 0.05))
        if res.cancelled:
            break
        if ev.row not in started:
            started.add(ev.row)
            status(ev.row, "发送中")
        for fr in ev.frames:
            try:
                bus.send(fr)
                res.sent += 1
                res.per_row[ev.row][0] += 1
            except Exception:
                res.failed += 1
                res.per_row[ev.row][1] += 1
                if stop_on_fail:
                    res.stopped_on_fail = True
                    status(ev.row, "失败")
                    break
        if res.stopped_on_fail:
            break
        remaining[ev.row] -= 1
        if remaining[ev.row] == 0:
            status(ev.row, "失败" if res.per_row[ev.row][1] else "完成")
    res.elapsed = clock() - t0
    for row in started:                                       # rows interrupted mid-way
        if remaining[row] > 0 and not (res.stopped_on_fail and res.per_row[row][1]):
            status(row, "已停止")
    return res


# ------------------------------------------------------------------ import / export
def save_list(path: str, items: List[SendItem]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"format": FORMAT, "version": VERSION, "items": [i.to_dict() for i in items]},
                  f, ensure_ascii=False, indent=2)


def load_list(path: str) -> List[SendItem]:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    if d.get("format") != FORMAT:
        raise ValueError("不是 myzcanpro 的发送列表文件")
    return [SendItem.from_dict(x) for x in d.get("items", [])]
