"""Virtual CAN bus shared by several processes on one PC (loopback TCP), for off-line tests of two CanVDS instances.

Every process that opens the device "虚拟总线(多进程)" and starts channel N joins bus N: a frame sent by one process is delivered to
every OTHER process on that bus (like a real CAN bus, the sender does not receive its own frame).  The first process finds nobody
listening on 127.0.0.1:<port> and hosts the hub itself (a daemon thread, lives as long as that process); the others connect to it.
A hub that outlives its first process can be started on its own:  python -m zdiag.netbus [port].
No bit timing is simulated, so it is much faster than a real bus: it checks function, not throughput.

Wire: hello = 1 byte (bus number); then frames  [len u16][bus u8][flags u8][can id u32][data]  (flags: 1 extended, 2 FD, 4 BRS, 8 RTR)."""
from __future__ import annotations

import os
import queue
import socket
import struct
import sys
import threading
import time
from typing import Dict, List, Optional

from .bus import Bus, CanFrame
from .busstats import BusStats

HOST = "127.0.0.1"
DEFAULT_PORT = 47010
QUEUE_MAX = 200_000


def port_from_env() -> int:
    try:
        return int(os.environ.get("CANVDS_NETBUS_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def encode(bus: int, fr: CanFrame) -> bytes:
    flags = (1 if fr.extended else 0) | (2 if fr.fd else 0) | (4 if fr.brs else 0) | (8 if fr.rtr else 0)
    body = struct.pack(">BBI", bus, flags, fr.can_id) + bytes(fr.data)
    return struct.pack(">H", len(body)) + body


def decode(body: bytes) -> CanFrame:
    _, flags, cid = struct.unpack(">BBI", body[:6])
    return CanFrame(cid, bytes(body[6:]), bool(flags & 1), bool(flags & 2), bool(flags & 4), time.perf_counter(), bool(flags & 8))


def _read_exact(s: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            b = s.recv(n - len(buf))
        except OSError:
            return None
        if not b:
            return None
        buf += b
    return buf


class _Peer:
    """One connected process as seen by the hub: frames for it go through a bounded queue and a writer thread (a slow peer
    never blocks the others; when it falls too far behind its oldest frames are dropped)."""

    def __init__(self, sock: socket.socket, bus: int) -> None:
        self.sock, self.bus = sock, bus
        self.q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.alive = True
        threading.Thread(target=self._writer, daemon=True, name="netbus-peer-tx").start()

    def put(self, msg: bytes) -> None:
        if self.q.qsize() >= QUEUE_MAX:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
        self.q.put(msg)

    def _writer(self) -> None:
        while True:
            m = self.q.get()
            if m is None:
                break
            try:
                self.sock.sendall(m)
            except OSError:
                break
        self.alive = False

    def close(self) -> None:
        self.alive = False
        self.q.put(None)
        try:
            self.sock.close()
        except OSError:
            pass


class Hub:
    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0 if os.name == "nt" else 1)
        self.srv.bind((HOST, port))                         # OSError when somebody already hosts it
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        self.peers: List[_Peer] = []
        self.lock = threading.Lock()
        self.stopped = False
        threading.Thread(target=self._accept, daemon=True, name="netbus-accept").start()

    def _accept(self) -> None:
        while not self.stopped:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(s,), daemon=True, name="netbus-peer-rx").start()

    def _serve(self, s: socket.socket) -> None:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        h = _read_exact(s, 1)
        if h is None:
            s.close()
            return
        peer = _Peer(s, h[0])
        with self.lock:
            self.peers.append(peer)
        try:
            while True:
                hd = _read_exact(s, 2)
                if hd is None:
                    break
                body = _read_exact(s, struct.unpack(">H", hd)[0])
                if body is None or len(body) < 6:
                    break
                msg = hd + body
                with self.lock:
                    targets = [p for p in self.peers if p is not peer and p.bus == peer.bus and p.alive]
                for p in targets:
                    p.put(msg)
        finally:
            with self.lock:
                if peer in self.peers:
                    self.peers.remove(peer)
            peer.close()

    def stop(self) -> None:
        self.stopped = True
        try:
            self.srv.close()
        except OSError:
            pass
        with self.lock:
            peers, self.peers = self.peers, []
        for p in peers:
            p.close()


_HUB: Optional[Hub] = None
_HUB_LOCK = threading.Lock()


def connect(port: int, bus: int, timeout: float = 3.0) -> socket.socket:
    """Join bus `bus`; host the hub in this process when nobody does yet."""
    global _HUB
    end = time.monotonic() + timeout
    last: Optional[Exception] = None
    while True:
        try:
            s = socket.create_connection((HOST, port), timeout=1.0)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(None)
            s.sendall(bytes([bus]))
            return s
        except OSError as e:
            last = e
        with _HUB_LOCK:
            if _HUB is None:
                try:
                    _HUB = Hub(port)                       # first one here hosts the bus
                except OSError as e:
                    last = e                               # lost the race: somebody else just started it, connect again
        if time.monotonic() > end:
            raise OSError(f"连不上虚拟总线（127.0.0.1:{port}）：{last}")
        time.sleep(0.05)


class NetBus(Bus):
    def __init__(self, bus: int, cfg, tap=None, port: Optional[int] = None) -> None:
        self.channel, self.tap = bus, tap
        self.stats = BusStats(cfg.bitrate, cfg.data_bitrate)
        self.sock = connect(port or port_from_env(), bus)
        self._q: "queue.Queue[CanFrame]" = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._rx = threading.Thread(target=self._rx_loop, daemon=True, name=f"netbus-rx{bus}")
        self._rx.start()

    def _rx_loop(self) -> None:
        s = self.sock
        while not self._closed:
            hd = _read_exact(s, 2)
            if hd is None:
                break
            body = _read_exact(s, struct.unpack(">H", hd)[0])
            if body is None or len(body) < 6:
                break
            fr = decode(body)
            self.stats.count_rx(fr)
            if self.tap is not None:
                self.tap.feed(self.channel, fr, "RX")
            if self._q.qsize() >= QUEUE_MAX:                        # nobody reads (monitor only): keep the newest
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
            self._q.put(fr)
        self._closed = True

    def send(self, frame: CanFrame) -> None:
        if self._closed:
            raise OSError("虚拟总线连接已断开（承载总线的进程退出了？）")
        frame.timestamp = time.perf_counter()
        with self._lock:
            self.sock.sendall(encode(self.channel, frame))
        self.stats.count_tx(frame)
        if self.tap is not None:
            self.tap.feed(self.channel, frame, "TX")

    def recv(self, timeout: float) -> Optional[CanFrame]:
        try:
            return self._q.get(timeout=max(timeout, 0))
        except queue.Empty:
            return None

    def flush(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class NetDevice:
    """Stands in for ZlgDevice: opening it costs nothing, channels join the shared buses."""

    def __init__(self, name: str, index: int = 0) -> None:
        self.name, self.index, self.buses = name, index, {}

    def start_channel(self, n: int, cfg, tap=None) -> NetBus:
        bus = NetBus(n, cfg, tap)
        self.buses[n] = bus
        return bus

    def stop_channel(self, n: int) -> None:
        b = self.buses.pop(n, None)
        if b is not None:
            b.close()

    def close(self) -> None:
        for n in list(self.buses):
            self.stop_channel(n)


def main(argv: List[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else port_from_env()
    hub = Hub(port)
    print(f"虚拟 CAN 总线已在 127.0.0.1:{hub.port} 监听（Ctrl+C 退出）", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        hub.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
