"""CAN bus abstraction + an in-memory virtual bus for tests."""
from __future__ import annotations
import queue
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class CanFrame:
    can_id: int
    data: bytes
    extended: bool = False
    fd: bool = False
    brs: bool = False
    timestamp: float = 0.0
    rtr: bool = False           # remote frame (classic CAN only); `data` then only gives the DLC

    def __repr__(self) -> str:
        return f"CanFrame(0x{self.can_id:X}, {self.data.hex(' ')}{', FD' if self.fd else ''}{', RTR' if self.rtr else ''})"


class Bus(ABC):
    @abstractmethod
    def send(self, frame: CanFrame) -> None: ...

    @abstractmethod
    def recv(self, timeout: float) -> Optional[CanFrame]: ...

    @abstractmethod
    def flush(self) -> None: ...

    def close(self) -> None:
        pass


class VirtualBus(Bus):
    """Two endpoints wired back-to-back (tester <-> simulated ECU)."""

    def __init__(self) -> None:
        self._rx: "queue.Queue[CanFrame]" = queue.Queue()
        self.peer: Optional["VirtualBus"] = None
        self.log: list = []          # every frame sent from this end, for inspection
        self.tap = None              # optional callback(frame, "TX"|"RX") for the passive monitor (this end's point of view)

    @staticmethod
    def pair():
        a, b = VirtualBus(), VirtualBus()
        a.peer, b.peer = b, a
        return a, b

    def send(self, frame: CanFrame) -> None:
        frame.timestamp = time.perf_counter()
        self.log.append(frame)
        self.peer._rx.put(frame)
        if self.tap is not None:
            self.tap(frame, "TX")
        if self.peer.tap is not None:
            self.peer.tap(frame, "RX")

    def recv(self, timeout: float) -> Optional[CanFrame]:
        try:
            return self._rx.get(timeout=max(timeout, 0))
        except queue.Empty:
            return None

    def flush(self) -> None:
        while True:
            try:
                self._rx.get_nowait()
            except queue.Empty:
                return
