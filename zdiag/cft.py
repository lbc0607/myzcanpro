"""CFT - CAN file transfer (our own protocol, not ISO-TP): resumable, any file type, whole folders, 1..n links.

Two peers that both run this code (one Sender, one Receiver).  A *link* is one CAN / CAN FD channel; with two links the file is
split in chunks which the link threads pull from one shared queue (a faster link simply carries more), a link that goes silent is
noticed within `link_timeout` and its chunks are re-sent over the remaining link; when it comes back it is used again.

CAN IDs (base = cfg.base_id): base   control  sender -> receiver   (messages, heartbeat)
                              base+1 control  receiver -> sender   (messages, acks, STATUS)
                              base+2 data     sender -> receiver
Data frame   [epoch][chunk & 0xFF][frame idx][payload ...]        always a full frame (8 / FD length), payload = len - 3
             the chunk's stream is  data + CRC32(data)  (4 bytes): a chunk whose CRC does not match is dropped and simply re-sent
Control      [T_MSG][msg id][frag idx | 0x80 if last][payload len][payload]    reliable small messages (stop-and-wait, acked)
             [T_ACK][msg id]   [T_HB][link]   [T_STATUS][epoch][base u32][bitmap: bit i = chunk base+1+i received]
A file is cut into `chunk_bytes` chunks; the receiver reports `base` (first missing chunk) + a bitmap inside the window, the sender
re-sends on timeout / when later chunks of the same link were acked.  The receiver writes `<name>.cftpart` plus `<name>.cftmeta`
(received-chunk state); a later session with the same file continues where it stopped.  The end-to-end check is CRC32 of the file;
when it does not match, nothing is thrown away: both sides compare per-segment CRCs (M_SEGQ/M_SEGR), only the segments that differ
are sent again (M_REPAIR, a sub-transfer of one chunk range under a new epoch) and the file is verified again."""
from __future__ import annotations

import collections
import json
import os
import queue
import random
import shutil
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .bus import Bus, CanFrame

VERSION = 2                     # 2: every chunk carries its own CRC32
META_VERSIONS = (1, 2)          # a v1 `.cftmeta` is still good for resuming (only the wire format changed)
FD_LENS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
T_MSG, T_ACK, T_HB, T_STATUS = 1, 2, 3, 4
M_HELLO, M_HELLO_OK, M_OFFER, M_REPLY, M_END, M_RESULT, M_BYE = 1, 2, 3, 4, 5, 6, 7
M_SEGQ, M_SEGR, M_REPAIR, M_DISCARD = 8, 9, 10, 11
CRC_LEN = 4
SEG_BYTES = 4 << 20             # repair granularity: segments of this size (rounded to whole chunks) are compared by CRC
SEG_PER_MSG = 100               # CRCs per M_SEGR message (a message is at most 128 control frames)
REPAIR_ROUNDS = 3
A_RECEIVE, A_SKIP, A_REJECT = 0, 1, 2
F_FILE, F_DIR = 0, 1
HEAD_BYTES = 4096
PART, META = ".cftpart", ".cftmeta"
_ONES = b"\x01" * 65536


class CftError(Exception):
    pass


class LinkError(CftError):
    pass


def dlc_len(n: int) -> int:
    return next(x for x in FD_LENS if x >= n)


@dataclass
class CftConfig:
    base_id: int = 0x6A0            # three consecutive CAN IDs: control s->r, control r->s, data s->r
    extended: bool = False          # 29-bit IDs
    chunk_bytes: int = 1024         # unit of acknowledgement / retransmission / resume
    window: int = 128               # max chunks in flight (<= 128; classic CAN can only report 16, FD 128)
    fd_frame_len: int = 64          # frame length on CAN FD links (12/16/20/24/32/48/64)
    link_timeout: float = 0.6       # a link with nothing heard for this long is declared down (seconds)
    hb_ms: int = 100                # heartbeat per link (also keeps a dead link probed so it can come back)
    status_ms: int = 20             # receiver STATUS interval
    rto_min: float = 0.4            # minimum retransmission timeout (adapts upwards to the measured delay)
    idle_timeout: float = 30.0      # give up when all links are down / nothing is acknowledged for this long
    retries: int = 1                # re-send a whole file once when the final CRC does not match
    overwrite: bool = True          # receiver: replace an existing different file (False = keep it, skip)

    def validate(self) -> None:
        if not 0 <= self.base_id <= (0x1FFFFFFD if self.extended else 0x7FD):
            raise CftError("CAN ID 超出范围（需要连续 3 个 ID）")
        if not 64 <= self.chunk_bytes <= 15000:
            raise CftError("块大小应在 64~15000 字节")
        if not 1 <= self.window <= 128:
            raise CftError("窗口应在 1~128 个块")
        if self.fd_frame_len not in (12, 16, 20, 24, 32, 48, 64):
            raise CftError("FD 帧长度应为 12/16/20/24/32/48/64")

    @property
    def id_s2r(self) -> int:
        return self.base_id

    @property
    def id_r2s(self) -> int:
        return self.base_id + 1

    @property
    def id_data(self) -> int:
        return self.base_id + 2


# ====================================================================== links
class Link:
    """One CAN channel of the transfer."""

    def __init__(self, idx: int, bus: Bus, fd: bool, brs: bool, cfg: CftConfig, name: str = "") -> None:
        self.idx, self.bus, self.fd, self.brs, self.cfg = idx, bus, bool(fd), bool(brs), cfg
        self.name = name or f"链路{idx}"
        self.frame_len = cfg.fd_frame_len if fd else 8
        self.lock = threading.Lock()
        self.alive = False
        self.last_rx = 0.0
        self.tx_frames = self.rx_frames = self.errors = self.tx_bytes = 0
        self.downs = 0
        self.backoff = 0.0                                        # seconds slept before every frame while the bus reports error storms
        self._err_mark = 0
        self.on_change: Optional[Callable[["Link", bool], None]] = None

    @property
    def payload(self) -> int:
        return self.frame_len - 3

    @property
    def status_bytes(self) -> int:
        return self.frame_len - 6

    def send(self, can_id: int, data: bytes) -> None:
        if len(data) > self.frame_len:
            raise ValueError("frame too long")
        if self.fd:
            data = data.ljust(dlc_len(len(data)), b"\x00")
        fr = CanFrame(can_id, bytes(data), self.cfg.extended, self.fd, self.brs)
        err: Optional[Exception] = None
        if self.backoff > 0:
            time.sleep(self.backoff)
        for _ in range(100):                                      # a full device TX buffer clears within a few ms
            try:
                with self.lock:
                    self.bus.send(fr)
                self.tx_frames += 1
                self.tx_bytes += len(data)
                return
            except Exception as e:                                # ZlgError etc.
                err = e
                self.errors += 1
                time.sleep(0.002)
        raise LinkError(f"{self.name} 发送失败: {err}")

    def bus_errors(self) -> int:
        st = getattr(self.bus, "stats", None)
        return getattr(st, "err", 0) if st is not None else 0

    def health(self) -> Dict[str, Any]:
        fn = getattr(self.bus, "health", None)
        try:
            return fn() if callable(fn) else {}
        except Exception:
            return {}

    def pace(self) -> bool:
        """Called ~4x/s by the supervisor: a burst of bus error frames / failed transmits makes this link slow down (and ease off again
        when it calms down) instead of hammering a sick bus.  True while backed off."""
        n = self.bus_errors()
        delta, self._err_mark = n - self._err_mark, n
        if delta >= 20:
            self.backoff = min(0.25, max(0.005, self.backoff * 2))
        elif delta <= 2:
            self.backoff = 0.0 if self.backoff < 0.003 else self.backoff / 2
        return self.backoff > 0

    def touch(self) -> None:
        """Something arrived from the peer on this link."""
        self.last_rx = time.monotonic()
        if not self.alive:
            self.alive = True
            if self.on_change:
                self.on_change(self, True)

    def check(self, now: float, timeout: float) -> bool:
        """True when the link just went down."""
        if self.alive and now - self.last_rx > timeout:
            self.alive = False
            self.downs += 1
            if self.on_change:
                self.on_change(self, False)
            return True
        return False


# ====================================================================== reliable small messages
class Messenger:
    """Stop-and-wait messages (fragmented over control frames) sent on every usable link, acked by the peer."""

    def __init__(self, links: List[Link], tx_id: int, stop: threading.Event, usable: Callable[[Link], bool]) -> None:
        self.links, self.tx_id, self.stop, self.usable = links, tx_id, stop, usable
        self.inbox: "queue.Queue[Tuple[int, bytes]]" = queue.Queue()
        self._mid = random.randint(1, 255)
        self._wait_id = -1
        self._acked = threading.Event()
        self._asm: Dict[int, Tuple[int, Dict[int, bytes]]] = {}        # link idx -> (msg id, fragments)
        self._recent: Deque[int] = collections.deque(maxlen=32)      # ids delivered lately: a copy that arrives late over the other link
        self._send_lock = threading.Lock()

    def _next(self) -> int:
        self._mid = self._mid % 255 + 1
        return self._mid

    def send(self, kind: int, payload: bytes = b"", timeout: float = 4.0, tries: int = 8) -> None:
        body = bytes([kind]) + payload
        with self._send_lock:
            mid = self._next()
            self._wait_id = mid
            self._acked.clear()
            per_try = max(timeout / tries, 0.2)
            for _ in range(tries):
                sent = 0
                targets = [l for l in self.links if self.usable(l)] or self.links
                for l in targets:
                    try:
                        self._fragments(l, mid, body)
                        sent += 1
                    except LinkError:
                        pass
                if sent and self._acked.wait(per_try):
                    return
                if self.stop.is_set():
                    raise CftError("已停止")
                if not sent:
                    time.sleep(0.1)
            raise CftError(f"对端没有应答（消息 {kind}）")

    def _fragments(self, link: Link, mid: int, body: bytes) -> None:
        step = link.frame_len - 4
        parts = [body[i:i + step] for i in range(0, len(body), step)] or [b""]
        if len(parts) > 128:
            raise CftError("消息太长")
        for i, p in enumerate(parts):
            last = 0x80 if i == len(parts) - 1 else 0
            link.send(self.tx_id, bytes([T_MSG, mid, i | last, len(p)]) + p)

    def on_msg(self, link: Link, d: bytes) -> None:
        if len(d) < 4:
            return
        mid, fi, plen = d[1], d[2] & 0x7F, d[3]
        last = bool(d[2] & 0x80)
        cur = self._asm.get(link.idx)
        if cur is None or cur[0] != mid or fi == 0:
            cur = self._asm[link.idx] = (mid, {})
        cur[1][fi] = bytes(d[4:4 + plen])
        if not last:
            return
        n = fi + 1
        if any(i not in cur[1] for i in range(n)):
            return
        del self._asm[link.idx]
        body = b"".join(cur[1][i] for i in range(n))
        try:
            link.send(self.tx_id, bytes([T_ACK, mid]))
        except LinkError:
            pass
        if not body:
            return
        if mid in self._recent and body[0] != M_HELLO:                  # the same message arrived over the other link too / re-sent
            return
        self._recent.append(mid)
        self.inbox.put((body[0], body[1:]))

    def on_ack(self, d: bytes) -> None:
        if len(d) >= 2 and d[1] == self._wait_id:
            self._acked.set()

    def expect(self, kind: int, timeout: float) -> bytes:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise CftError(f"等待对端消息超时（{kind}）")
            if self.stop.is_set():
                raise CftError("已停止")
            try:
                k, p = self.inbox.get(timeout=min(left, 0.1))
            except queue.Empty:
                continue
            if k == kind:
                return p


# ====================================================================== helpers
def safe_rel(rel: str) -> List[str]:
    parts = rel.split("/")
    for p in parts:
        if p in ("", ".", "..") or ":" in p or "\\" in p or "\x00" in p or len(p) > 255:
            raise CftError(f"不安全的路径: {rel!r}")
    if parts[-1].endswith((PART, META)):
        raise CftError(f"不接收 {PART}/{META} 文件: {rel!r}")
    return parts


def head_crc(path: str, size: int) -> int:
    with open(path, "rb") as f:
        return zlib.crc32(f.read(min(size, HEAD_BYTES))) & 0xFFFFFFFF


def file_crc(path: str, stop: Optional[threading.Event] = None) -> int:
    crc = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                return crc & 0xFFFFFFFF
            if stop is not None and stop.is_set():
                raise CftError("已停止")
            crc = zlib.crc32(b, crc)


def collect_entries(paths: List[str]) -> List[Dict[str, Any]]:
    """Files and folders -> ordered entries {kind, rel, abs, size, mtime_ms}; a folder keeps its own name as the top level."""
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(kind: int, rel: str, ab: str) -> None:
        if rel in seen:
            return
        seen.add(rel)
        st = os.stat(ab)
        out.append({"kind": kind, "rel": rel, "abs": ab, "size": 0 if kind == F_DIR else st.st_size,
                    "mtime_ms": int(st.st_mtime * 1000)})

    for p in paths:
        p = os.path.abspath(p)
        if not os.path.exists(p):
            raise CftError(f"路径不存在: {p}")
        top = os.path.basename(p.rstrip("\\/")) or "root"
        if os.path.isfile(p):
            add(F_FILE, top, p)
            continue
        add(F_DIR, top, p)
        for root, dirs, files in os.walk(p):
            dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
            rel_root = os.path.relpath(root, p)
            pre = top if rel_root == "." else top + "/" + rel_root.replace(os.sep, "/")
            for d in dirs:
                add(F_DIR, pre + "/" + d, os.path.join(root, d))
            for f in sorted(files):
                fp = os.path.join(root, f)
                if os.path.islink(fp) or not os.path.isfile(fp):
                    continue
                add(F_FILE, pre + "/" + f, fp)
    for e in out:
        safe_rel(e["rel"])
        if len(e["rel"].encode("utf-8")) > 1000:
            raise CftError(f"路径太长: {e['rel'][:60]}…")
    return out


class _Log:
    def __init__(self) -> None:
        self.lines: Deque[str] = collections.deque(maxlen=300)
        self.t0 = time.time()
        self.sink: Optional[Callable[[str], None]] = None

    def __call__(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.lines.append(line)
        if self.sink:
            try:
                self.sink(line)
            except Exception:
                pass


# ====================================================================== sender
class _SendFile:
    def __init__(self, e: Dict[str, Any], epoch: int, chunk: int, base: int) -> None:
        self.e, self.epoch, self.chunk, self.size = e, epoch, chunk, e["size"]
        self.n = (self.size + chunk - 1) // chunk
        self.base = base
        self.acked: set = set()
        self.inflight: Dict[int, list] = {}                         # chunk -> [link idx, t_sent | None, seq]
        self.retry: Deque[int] = collections.deque()
        self.next_new = base
        self.seq = 0
        self.fh = open(e["abs"], "rb") if self.size else None
        self.flock = threading.Lock()
        self.last_progress = time.monotonic()

    def clen(self, idx: int) -> int:
        return min(self.chunk, self.size - idx * self.chunk)

    def read(self, idx: int) -> bytes:
        with self.flock:
            self.fh.seek(idx * self.chunk)
            return self.fh.read(self.clen(idx))

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None

    def ack(self, idx: int) -> bool:
        if idx < self.base or idx in self.acked or idx >= self.n:
            return False
        self.acked.add(idx)
        self.inflight.pop(idx, None)
        return True

    def advance(self) -> None:
        while self.base in self.acked:
            self.acked.discard(self.base)
            self.base += 1

    def requeue(self, idx: int) -> None:
        if self.inflight.pop(idx, None) is not None and idx >= self.base and idx not in self.acked:
            self.retry.append(idx)

    def done_chunks(self) -> int:
        return self.base + len(self.acked)


class Sender:
    def __init__(self, links: List[Link], cfg: Optional[CftConfig] = None) -> None:
        if not links:
            raise CftError("至少需要一个链路（通道）")
        self.cfg = cfg or CftConfig()
        self.cfg.validate()
        self.links = links
        for l in links:
            if self.cfg.chunk_bytes > 256 * l.payload:
                raise CftError(f"块大小 {self.cfg.chunk_bytes} 超过 {l.name} 的上限 {256 * l.payload}（每块最多 256 帧）")
        self.stop_ev = threading.Event()
        self.cond = threading.Condition()
        self.log = _Log()
        self.msg = Messenger(links, self.cfg.id_s2r, self.stop_ev, lambda l: l.alive)
        self.cur: Optional[_SendFile] = None
        self.state = "idle"
        self.error = ""
        self.entries: List[Dict[str, Any]] = []
        self.files_done = self.files_skipped = 0
        self.failed: List[str] = []
        self.bytes_total = 0
        self.bytes_done = 0                                          # finished files
        self.cur_name = ""
        self.srtt: Optional[float] = None
        self.started = 0.0
        self._speed: Deque[Tuple[float, int]] = collections.deque(maxlen=200)
        self._threads: List[threading.Thread] = []
        for l in links:
            l.on_change = self._link_changed

    # ------------------------------------------------------------ public
    def stop(self) -> None:
        self.stop_ev.set()
        with self.cond:
            self.cond.notify_all()

    def window_eff(self) -> int:
        alive = [l for l in self.links if l.alive] or self.links
        return max(1, min(self.cfg.window, 8 * max(l.status_bytes for l in alive)))

    def status(self) -> Dict[str, Any]:
        fs = self.cur
        cur_bytes = 0
        if fs is not None:
            cur_bytes = min(fs.size, fs.done_chunks() * fs.chunk)
        done = self.bytes_done + cur_bytes
        now = time.monotonic()
        speed = 0.0
        if len(self._speed) >= 2 and now - self._speed[0][0] > 0.05:
            t0, b0 = self._speed[0]
            speed = max(0.0, (done - b0) / (now - t0))
        return {"role": "send", "state": self.state, "error": self.error, "file": self.cur_name,
                "files_total": len([e for e in self.entries if e["kind"] == F_FILE]), "files_done": self.files_done,
                "files_skipped": self.files_skipped, "failed": list(self.failed), "bytes_total": self.bytes_total,
                "bytes_done": done, "speed_bps": round(speed), "elapsed_s": round(time.time() - self.started, 1) if self.started else 0,
                "links": [self._link_info(l) for l in self.links]}

    @staticmethod
    def _link_info(l: Link) -> Dict[str, Any]:
        return {"name": l.name, "alive": l.alive, "frame_len": l.frame_len, "fd": l.fd, "tx_frames": l.tx_frames,
                "tx_bytes": l.tx_bytes, "errors": l.errors, "downs": l.downs, "bus_errors": l.bus_errors(),
                "backoff_ms": round(l.backoff * 1000), "health": l.health()}

    @staticmethod
    def _diag_text(l: Link) -> str:
        h = l.health()
        bits = [f"总线错误帧 {l.bus_errors()}"]
        if "tec" in h:
            bits.append(f"TEC={h['tec']} REC={h['rec']}" + ("，总线关闭" if h.get("bus_off") else ""))
        if h.get("recoveries"):
            bits.append(f"已自动重启通道 {h['recoveries']} 次")
        if h.get("rx_dropped"):
            bits.append(f"接收缓冲丢弃 {h['rx_dropped']} 帧")
        return "，".join(bits)

    def _diag_all(self) -> str:
        return "；".join(f"{l.name}: {self._diag_text(l)}" for l in self.links) + "。请检查接线、终端电阻(120Ω)、两端波特率/FD 设置是否一致"

    def _link_changed(self, l: Link, up: bool) -> None:
        self.log(f"{l.name} {'恢复' if up else '掉线'}" + ("" if up else "，改由其余链路传输"))
        with self.cond:
            self.cond.notify_all()

    # ------------------------------------------------------------ run
    def run(self, paths: List[str]) -> Dict[str, Any]:
        self.started = time.time()
        try:
            self.entries = collect_entries(paths)
            self.bytes_total = sum(e["size"] for e in self.entries)
            nfiles = len([e for e in self.entries if e["kind"] == F_FILE])
            self.log(f"准备发送 {nfiles} 个文件，共 {self.bytes_total} 字节，{len(self.links)} 条链路")
            self._start()
            self._handshake(nfiles)
            for e in self.entries:
                if self.stop_ev.is_set():
                    raise CftError("已停止")
                self._send_entry(e)
            try:
                self.msg.send(M_BYE, b"", timeout=2.0, tries=4)
            except CftError:
                pass
            self.state = "done" if not self.failed else "done_with_errors"
            self.log("传输完成" if not self.failed else f"传输结束，失败 {len(self.failed)} 个")
        except CftError as e:
            self.state, self.error = "error", str(e)
            self.log(f"中止: {e}（已收到的部分保留在对端，重新发送同一文件/文件夹即可续传）")
        except Exception as e:                                       # never leave the caller hanging
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            self.log(f"异常: {self.error}")
        finally:
            self.stop_ev.set()
            with self.cond:
                self.cond.notify_all()
            for t in self._threads:
                t.join(timeout=2)
            if self.cur is not None:
                self.cur.close()
        return self.status()

    def _start(self) -> None:
        for l in self.links:
            for target in (self._rx_loop, self._worker):
                t = threading.Thread(target=target, args=(l,), daemon=True, name=f"cft-{target.__name__}-{l.idx}")
                t.start()
                self._threads.append(t)
        t = threading.Thread(target=self._hb_loop, daemon=True, name="cft-hb")
        t.start()
        self._threads.append(t)

    def _handshake(self, nfiles: int) -> None:
        self.state = "connecting"
        self._nonce = random.getrandbits(32)
        self.msg.send(M_HELLO, struct.pack(">BIQHI", VERSION, nfiles, self.bytes_total, self.cfg.window, self._nonce), timeout=10.0, tries=20)
        p = self.msg.expect(M_HELLO_OK, 10.0)
        ver, status, win = struct.unpack(">BBH", p[:4])
        if status != 0:
            raise CftError("对端忙或拒绝了连接")
        if ver != VERSION:
            raise CftError(f"协议版本不一致（本机 {VERSION}，对端 {ver}）")
        self.cfg.window = min(self.cfg.window, win)
        self.log(f"已连接对端，在线链路: {', '.join(l.name for l in self.links if l.alive) or '（等待状态帧）'}")

    # ------------------------------------------------------------ rx / heartbeat
    def _rx_loop(self, l: Link) -> None:
        while not self.stop_ev.is_set():
            fr = l.bus.recv(0.05)
            if fr is None or fr.rtr or fr.can_id != self.cfg.id_r2s or not fr.data:
                continue
            l.rx_frames += 1
            l.touch()
            t = fr.data[0]
            if t == T_STATUS:
                self._on_status(l, fr.data)
            elif t == T_MSG:
                self.msg.on_msg(l, fr.data)
            elif t == T_ACK:
                self.msg.on_ack(fr.data)

    def _hb_loop(self) -> None:
        while not self.stop_ev.wait(self.cfg.hb_ms / 1000.0):
            for l in self.links:
                try:
                    l.send(self.cfg.id_s2r, bytes([T_HB, l.idx]))
                except LinkError:
                    pass

    def _on_status(self, l: Link, d: bytes) -> None:
        if len(d) < 6:
            return
        epoch, base = d[1], struct.unpack(">I", d[2:6])[0]
        now = time.monotonic()
        with self.cond:
            fs = self.cur
            if fs is None or epoch == 0 or epoch != fs.epoch:
                return
            newly: List[list] = []
            for idx in list(fs.inflight):
                if idx < base:
                    rec = fs.inflight.get(idx)
                    if rec is not None:
                        newly.append(rec)
            for idx in range(fs.base, min(base, fs.n)):
                fs.ack(idx)
            hi: Dict[int, int] = {}
            for i, byte in enumerate(d[6:]):
                if not byte:
                    continue
                for b in range(8):
                    if byte >> b & 1:
                        idx = base + 1 + i * 8 + b
                        rec = fs.inflight.get(idx)
                        if fs.ack(idx) and rec is not None:
                            newly.append(rec)
            if base > fs.base:
                fs.acked = {x for x in fs.acked if x >= base}
                fs.base = min(base, fs.n)
            fs.advance()
            for rec in newly:
                hi[rec[0]] = max(hi.get(rec[0], -1), rec[2])
                if rec[1] is not None:
                    s = now - rec[1]
                    self.srtt = s if self.srtt is None else 0.875 * self.srtt + 0.125 * s
            if newly:
                fs.last_progress = now
                self._speed.append((now, self.bytes_done + min(fs.size, fs.done_chunks() * fs.chunk)))
            for idx, rec in list(fs.inflight.items()):                # later chunks of the same link are acked: this one is lost
                if rec[1] is not None and rec[0] in hi and rec[2] < hi[rec[0]] - 3:
                    fs.requeue(idx)
            self.cond.notify_all()

    # ------------------------------------------------------------ workers
    def _take(self, l: Link) -> Optional[Tuple[_SendFile, int, list]]:
        fs = self.cur
        if fs is None or not l.alive:
            return None
        idx = None
        while fs.retry:
            c = fs.retry.popleft()
            if c >= fs.base and c not in fs.acked and c not in fs.inflight:
                idx = c
                break
        if idx is None:
            while fs.next_new < fs.n and (fs.next_new < fs.base or fs.next_new in fs.acked or fs.next_new in fs.inflight):
                fs.next_new += 1
            if fs.next_new < fs.n and fs.next_new < fs.base + self.window_eff():
                idx = fs.next_new
                fs.next_new += 1
        if idx is None:
            return None
        fs.seq += 1
        rec = [l.idx, None, fs.seq]
        fs.inflight[idx] = rec
        return fs, idx, rec

    def _worker(self, l: Link) -> None:
        while not self.stop_ev.is_set():
            with self.cond:
                job = self._take(l)
                if job is None:
                    self.cond.wait(0.005)
                    continue
            fs, idx, rec = job
            try:
                data = fs.read(idx)
                data += struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)
                p = l.payload
                head = bytes((fs.epoch, idx & 0xFF))
                for fi in range(max(1, (len(data) + p - 1) // p)):
                    part = data[fi * p:(fi + 1) * p]
                    l.send(self.cfg.id_data, head + bytes((fi,)) + part.ljust(p, b"\x00"))
                with self.cond:
                    if fs.inflight.get(idx) is rec:
                        rec[1] = time.monotonic()
            except LinkError as e:
                self.log(str(e))
                with self.cond:
                    l.alive = False
                    l.downs += 1
                    for c, r in list(fs.inflight.items()):
                        if r[0] == l.idx:
                            fs.requeue(c)
                    self.cond.notify_all()
            except (OSError, ValueError) as e:
                self.log(f"读文件失败: {e}")
                self.stop_ev.set()
                self.error = f"读文件失败: {e}"

    # ------------------------------------------------------------ one file
    def _send_entry(self, e: Dict[str, Any]) -> None:
        self.cur_name = e["rel"]
        if e["kind"] == F_DIR:
            self._offer(e, 0, 0)
            return
        for attempt in range(self.cfg.retries + 1):
            ok, text = self._send_file(e)
            if ok:
                return
            self.log(f"{e['rel']}: {text}" + ("，重新发送" if attempt < self.cfg.retries else ""))
        self.failed.append(e["rel"])

    def _offer(self, e: Dict[str, Any], epoch: int, hcrc: int) -> Tuple[int, int, str]:
        rel = e["rel"].encode("utf-8")
        body = struct.pack(">BBIQQIHH", epoch, e["kind"], self.entries.index(e), e["size"], e["mtime_ms"], hcrc,
                           self.cfg.chunk_bytes, len(rel)) + rel
        self.msg.send(M_OFFER, body, timeout=10.0, tries=10)
        p = self.msg.expect(M_REPLY, 20.0)
        ep, action, base = struct.unpack(">BBI", p[:6])
        return action, base, p[6:].decode("utf-8", "replace")

    def _send_file(self, e: Dict[str, Any]) -> Tuple[bool, str]:
        self.state = "sending"
        epoch = getattr(self, "_epoch", 0) % 255 + 1
        self._epoch = epoch
        hcrc = head_crc(e["abs"], e["size"]) if e["size"] else 0
        action, base, text = self._offer(e, epoch, hcrc)
        if action == A_REJECT:
            self.log(f"对端拒绝 {e['rel']}: {text}")
            self.failed.append(e["rel"])
            return True, ""
        if action == A_SKIP:
            self.files_skipped += 1
            self.bytes_done += e["size"]
            self.log(f"跳过 {e['rel']}（{text or '对端已有相同文件'}）")
            return True, ""
        fs = _SendFile(e, epoch, self.cfg.chunk_bytes, base)
        if base:
            self.log(f"续传 {e['rel']}：从第 {base} 块开始（共 {fs.n} 块）")
        else:
            self.log(f"发送 {e['rel']}（{e['size']} 字节，{fs.n} 块）")
        with self.cond:
            self.cur = fs
            self._speed.clear()
            self.cond.notify_all()
        try:
            self._pump(fs)
            if self.stop_ev.is_set():
                raise CftError(self.error or "已停止")
            crc = file_crc(e["abs"], self.stop_ev)
        finally:
            with self.cond:
                self.cur = None
            fs.close()
        ok, text = self._finish(e, epoch, crc)
        if ok:
            self.files_done += 1
            self.bytes_done += e["size"]
            return True, ""
        return False, text or "对端校验失败"

    def _next_epoch(self) -> int:
        self._epoch = getattr(self, "_epoch", 0) % 255 + 1
        return self._epoch

    def _finish(self, e: Dict[str, Any], epoch: int, crc: int) -> Tuple[bool, str]:
        """END -> RESULT.  A whole-file CRC mismatch is repaired segment by segment instead of starting over."""
        wait = 120.0 + e["size"] / 2e7
        for rnd in range(REPAIR_ROUNDS + 1):
            self.msg.send(M_END, struct.pack(">BQI", epoch, e["size"], crc), timeout=10.0, tries=10)
            p = self.msg.expect(M_RESULT, wait)
            if p[1] != 2:
                return bool(p[1]), p[2:].decode("utf-8", "replace")
            if rnd == REPAIR_ROUNDS:
                break
            self.log(f"{e['rel']}: 整文件 CRC 不一致，按段比对定位坏块（第 {rnd + 1} 轮）")
            nxt = self._repair(e, epoch, wait)
            if nxt is None:
                break
            epoch = nxt
        self.msg.send(M_DISCARD, bytes([epoch]), timeout=10.0, tries=10)
        return False, "CRC 校验不一致且按段修复没有成功，已丢弃对端数据，将重新发送"

    def _seg_crc(self, e: Dict[str, Any], lo: int, hi: int) -> int:
        chunk, crc = self.cfg.chunk_bytes, 0
        with open(e["abs"], "rb") as f:
            f.seek(lo * chunk)
            left = min(e["size"], hi * chunk) - lo * chunk
            while left > 0:
                if self.stop_ev.is_set():
                    raise CftError("已停止")
                b = f.read(min(left, 1 << 20))
                if not b:
                    break
                crc = zlib.crc32(b, crc)
                left -= len(b)
        return crc & 0xFFFFFFFF

    def _repair(self, e: Dict[str, Any], epoch: int, wait: float) -> Optional[int]:
        """Compare segment CRCs with the receiver, re-send only the segments that differ. Returns the new epoch (None: nothing found)."""
        chunk = self.cfg.chunk_bytes
        n = (e["size"] + chunk - 1) // chunk
        spc = max(1, SEG_BYTES // chunk)
        nseg = (n + spc - 1) // spc
        bad: List[Tuple[int, int]] = []
        for s0 in range(0, nseg, SEG_PER_MSG):
            cnt = min(SEG_PER_MSG, nseg - s0)
            self.msg.send(M_SEGQ, struct.pack(">BIH", epoch, s0, cnt), timeout=10.0, tries=10)
            p = self.msg.expect(M_SEGR, wait)                       # the first query makes the receiver read its whole file
            _, first, got = struct.unpack(">BIH", p[:7])
            theirs = struct.unpack(f">{got}I", p[7:7 + 4 * got])
            for i, rc in enumerate(theirs):
                lo, hi = (first + i) * spc, min(n, (first + i + 1) * spc)
                if self._seg_crc(e, lo, hi) != rc:
                    bad.append((lo, hi))
        if not bad:
            return None
        self.log(f"{e['rel']}: {len(bad)}/{nseg} 段不一致，只重发这些段（约 {sum(h - l for l, h in bad) * chunk} 字节）")
        for lo, hi in bad:
            epoch = self._next_epoch()
            self.msg.send(M_REPAIR, struct.pack(">BII", epoch, lo, hi), timeout=10.0, tries=10)
            self.msg.expect(M_REPLY, 20.0)
            fs = _SendFile(e, epoch, chunk, lo)
            fs.n = hi
            with self.cond:
                self.cur = fs
                self.cond.notify_all()
            try:
                self._pump(fs)
                if self.stop_ev.is_set():
                    raise CftError(self.error or "已停止")
            finally:
                with self.cond:
                    self.cur = None
                fs.close()
        return epoch

    def _pump(self, fs: _SendFile) -> None:
        """Supervise one file until every chunk is acknowledged."""
        idle_since: Optional[float] = None
        pace_t = 0.0
        while not self.stop_ev.is_set():
            with self.cond:
                now = time.monotonic()
                if now - pace_t >= 0.25:
                    pace_t = now
                    for l in self.links:
                        was = l.backoff > 0
                        if l.pace() and not was:
                            self.log(f"{l.name} 总线错误帧激增，自动降速（{self._diag_text(l)}）")
                        elif was and l.backoff == 0:
                            self.log(f"{l.name} 总线恢复正常，已恢复全速")
                for l in self.links:
                    if l.check(now, self.cfg.link_timeout):
                        for c, r in list(fs.inflight.items()):
                            if r[0] == l.idx:
                                fs.requeue(c)
                rto = max(self.cfg.rto_min, 3.0 * (self.srtt or 0.0) + 0.1)
                for c, r in list(fs.inflight.items()):
                    if r[1] is not None and now - r[1] > rto:
                        fs.requeue(c)
                if fs.base >= fs.n:
                    return
                if any(l.alive for l in self.links):
                    idle_since = None
                    if now - fs.last_progress > self.cfg.idle_timeout:
                        raise CftError(f"{self.cfg.idle_timeout:g} 秒内没有任何块被确认；" + self._diag_all())
                else:
                    idle_since = idle_since or now
                    if now - idle_since > self.cfg.idle_timeout:
                        raise CftError("所有链路都断开了；" + self._diag_all())
                self.cond.wait(0.01)


# ====================================================================== receiver
class _Active:
    def __init__(self, e: Dict[str, Any], epoch: int, chunk: int, final: str, base: int, extra: set) -> None:
        self.e, self.epoch, self.chunk, self.size, self.final = e, epoch, chunk, e["size"], final
        self.part, self.meta = final + PART, final + META
        self.n = self.n_full = (self.size + chunk - 1) // chunk
        self.base, self.done = base, set(extra)
        self.mismatch = self.repairing = False                    # whole-file CRC failed / a segment repair is running (n = range end)
        self.segtab: Optional[List[int]] = None
        self.bad_chunks = 0
        self.bufs: Dict[int, Tuple[bytearray, bytearray]] = {}
        self.lock = threading.Lock()
        self.fh = None
        self.dirty = False
        self.window = 128
        self.received = 0

    def write(self, idx: int, data: bytes) -> None:
        self.fh.seek(idx * self.chunk)
        self.fh.write(data)

    def save_meta(self) -> None:
        with self.lock:
            if not self.dirty or self.fh is None:
                return
            self.fh.flush()
            try:
                os.fsync(self.fh.fileno())
            except OSError:
                pass
            m = {"v": VERSION, "size": self.size, "mtime_ms": self.e["mtime_ms"], "head": self.e["head"], "chunk": self.chunk,
                 "base": self.base, "done": sorted(self.done)}
            self.dirty = False
        tmp = self.meta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f)
            f.flush()
            try:
                os.fsync(f.fileno())                                # else a power cut can leave an empty meta = restart from zero
            except OSError:
                pass
        os.replace(tmp, self.meta)


class Receiver:
    def __init__(self, links: List[Link], dest: str, cfg: Optional[CftConfig] = None, once: bool = True) -> None:
        if not links:
            raise CftError("至少需要一个链路（通道）")
        self.cfg = cfg or CftConfig()
        self.cfg.validate()
        self.links, self.dest, self.once = links, os.path.abspath(dest), once
        self.stop_ev = threading.Event()
        self.log = _Log()
        self.msg = Messenger(links, self.cfg.id_r2s, self.stop_ev, lambda l: time.monotonic() - l.last_rx < 2.0 and l.last_rx > 0)
        self.act: Optional[_Active] = None
        self.state = "waiting"
        self.error = ""
        self.window = 128
        self._nonce = -1
        self.bad_total = 0                                         # chunks dropped because their own CRC did not match
        self.files_done = self.files_skipped = 0
        self.failed: List[str] = []
        self.bytes_total = self.files_total = 0
        self.bytes_done = 0
        self.cur_name = ""
        self.started = 0.0
        self._speed: Deque[Tuple[float, int]] = collections.deque(maxlen=200)
        self._threads: List[threading.Thread] = []
        for l in links:
            l.on_change = lambda l_, up: self.log(f"{l_.name} {'有数据' if up else '无数据'}")

    def stop(self) -> None:
        self.stop_ev.set()

    def status(self) -> Dict[str, Any]:
        a = self.act
        cur = 0
        if a is not None:
            with a.lock:
                cur = min(a.size, (a.base + len(a.done)) * a.chunk)
        done = self.bytes_done + cur
        now = time.monotonic()
        speed = 0.0
        if len(self._speed) >= 2 and now - self._speed[0][0] > 0.05:
            t0, b0 = self._speed[0]
            speed = max(0.0, (done - b0) / (now - t0))
        return {"role": "receive", "state": self.state, "error": self.error, "file": self.cur_name, "dest": self.dest,
                "files_total": self.files_total, "files_done": self.files_done, "files_skipped": self.files_skipped,
                "failed": list(self.failed), "bytes_total": self.bytes_total, "bytes_done": done, "speed_bps": round(speed),
                "elapsed_s": round(time.time() - self.started, 1) if self.started else 0,
                "links": [{"name": l.name, "alive": time.monotonic() - l.last_rx < 2.0 and l.last_rx > 0, "frame_len": l.frame_len,
                           "fd": l.fd, "tx_frames": l.tx_frames, "rx_frames": l.rx_frames, "errors": l.errors,
                           "bus_errors": l.bus_errors(), "health": l.health()} for l in self.links],
                "bad_chunks": self.bad_total}

    # ------------------------------------------------------------ run
    def run(self) -> Dict[str, Any]:
        self.started = time.time()
        os.makedirs(self.dest, exist_ok=True)
        self.log(f"开始接收，保存到 {self.dest}，{len(self.links)} 条链路，CAN ID {self.cfg.base_id:X}/{self.cfg.base_id + 1:X}/{self.cfg.base_id + 2:X}")
        for l in self.links:
            t = threading.Thread(target=self._rx_loop, args=(l,), daemon=True, name=f"cft-rrx-{l.idx}")
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._status_loop, daemon=True, name="cft-status")
        t.start()
        self._threads.append(t)
        try:
            while not self.stop_ev.is_set():
                try:
                    kind, p = self.msg.inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == M_HELLO:
                    self._hello(p)
                elif kind == M_OFFER:
                    self._offer(p)
                elif kind == M_END:
                    self._end(p)
                elif kind == M_SEGQ:
                    self._segq(p)
                elif kind == M_REPAIR:
                    self._repair(p)
                elif kind == M_DISCARD:
                    self._discard(p)
                elif kind == M_BYE:
                    self._close_active()
                    self.state = "done" if not self.failed else "done_with_errors"
                    self.log("发送方已结束传输")
                    if self.once:
                        break
        except CftError as e:
            self.state, self.error = "error", str(e)
            self.log(f"中止: {e}")
        except Exception as e:
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            self.log(f"异常: {self.error}")
        finally:
            self.stop_ev.set()
            self._close_active()
            for t in self._threads:
                t.join(timeout=2)
            if self.state in ("waiting", "receiving"):
                self.state = "stopped"
        return self.status()

    # ------------------------------------------------------------ frames
    def _rx_loop(self, l: Link) -> None:
        while not self.stop_ev.is_set():
            fr = l.bus.recv(0.05)
            if fr is None or fr.rtr or not fr.data:
                continue
            if fr.can_id == self.cfg.id_data:
                l.rx_frames += 1
                l.touch()
                self._on_data(l, fr.data)
            elif fr.can_id == self.cfg.id_s2r:
                l.rx_frames += 1
                l.touch()
                t = fr.data[0]
                if t == T_MSG:
                    self.msg.on_msg(l, fr.data)
                elif t == T_ACK:
                    self.msg.on_ack(fr.data)

    def _on_data(self, l: Link, d: bytes) -> None:
        a = self.act
        if a is None or len(d) < 4 or d[0] != a.epoch:
            return
        lo, fi, p = d[1], d[2], len(d) - 3
        with a.lock:
            cand = a.base + ((lo - a.base) & 0xFF)
            if cand - a.base >= a.window or cand >= a.n or cand in a.done:
                return
            clen = min(a.chunk, a.size - cand * a.chunk)
            slen = clen + CRC_LEN                                  # the chunk's stream: data + CRC32
            buf = a.bufs.get(cand)
            if buf is None:
                buf = a.bufs[cand] = (bytearray(slen), bytearray(slen))
            off = fi * p
            n = min(p, slen - off)
            if n <= 0 or a.fh is None:
                return
            buf[0][off:off + n] = d[3:3 + n]
            buf[1][off:off + n] = _ONES[:n]
            if 0 in buf[1]:
                return
            del a.bufs[cand]
            body = bytes(buf[0])
            if zlib.crc32(body[:clen]) & 0xFFFFFFFF != struct.unpack(">I", body[clen:])[0]:
                self.bad_total += 1
                a.bad_chunks += 1                                  # damaged (or frames of two sends mixed): drop, the sender re-sends it
                if a.bad_chunks <= 3 or a.bad_chunks % 100 == 0:
                    self.log(f"块 {cand} 校验失败，丢弃等待重发（累计 {a.bad_chunks}）")
                return
            a.write(cand, body[:clen])
            a.done.add(cand)
            a.dirty = True
            while a.base in a.done:
                a.done.discard(a.base)
                a.base += 1
            self._speed.append((time.monotonic(), self.bytes_done + min(a.size, (a.base + len(a.done)) * a.chunk)))

    def _status_loop(self) -> None:
        last_meta = time.monotonic()
        while not self.stop_ev.wait(self.cfg.status_ms / 1000.0):
            now = time.monotonic()
            a = self.act
            for l in self.links:
                if l.last_rx <= 0 or now - l.last_rx > 2.0:
                    continue                                          # nothing from the sender on this link: do not shout
                if a is not None:
                    with a.lock:
                        bits = bytearray(l.status_bytes)
                        for idx in a.done:
                            i = idx - a.base - 1
                            if 0 <= i < 8 * len(bits):
                                bits[i >> 3] |= 1 << (i & 7)
                        d = bytes([T_STATUS, a.epoch]) + struct.pack(">I", a.base) + bytes(bits)
                else:
                    d = bytes([T_STATUS, 0]) + struct.pack(">I", 0)
                try:
                    l.send(self.cfg.id_r2s, d)
                except LinkError:
                    pass
            if a is not None and now - last_meta > 1.0:
                last_meta = now
                try:
                    a.save_meta()
                except OSError:
                    pass

    # ------------------------------------------------------------ messages
    def _reply(self, epoch: int, action: int, base: int = 0, text: str = "") -> None:
        self.msg.send(M_REPLY, struct.pack(">BBI", epoch, action, base) + text.encode("utf-8")[:200], timeout=10.0, tries=10)

    def _hello(self, p: bytes) -> None:
        ver, nfiles, nbytes, win, nonce = struct.unpack(">BIQHI", p[:19])
        if nonce == self._nonce:                                    # a copy of the HELLO we already answered (other link / re-send)
            return
        self._nonce = nonce
        self._close_active()
        self.window = max(1, min(win, 128))
        self.files_total, self.bytes_total = nfiles, nbytes
        self.files_done = self.files_skipped = self.bytes_done = 0
        self.failed = []
        self.state = "receiving"
        self.msg.send(M_HELLO_OK, struct.pack(">BBH", VERSION, 0 if ver == VERSION else 1, self.window), timeout=6.0, tries=10)
        self.log(f"发送方连入：{nfiles} 个文件，{nbytes} 字节")

    def _close_active(self) -> None:
        a, self.act = self.act, None
        if a is None:
            return
        try:
            a.save_meta()
        except OSError:
            pass
        with a.lock:
            if a.fh is not None:
                a.fh.close()
                a.fh = None

    def _offer(self, p: bytes) -> None:
        self._close_active()
        epoch, kind, index, size, mtime_ms, hcrc, chunk, plen = struct.unpack(">BBIQQIHH", p[:30])
        rel = p[30:30 + plen].decode("utf-8", "replace")
        self.cur_name = rel
        try:
            parts = safe_rel(rel)
            final = os.path.join(self.dest, *parts)
            real_dest = os.path.realpath(self.dest)
            if not os.path.realpath(final).startswith(real_dest + os.sep):
                raise CftError("路径越出保存目录")
        except CftError as e:
            self.failed.append(rel)
            self.log(f"拒绝 {rel!r}: {e}")
            self._reply(epoch, A_REJECT, 0, str(e))
            return
        if kind == F_DIR:
            os.makedirs(final, exist_ok=True)
            self._reply(epoch, A_SKIP, 0, "目录")
            return
        os.makedirs(os.path.dirname(final), exist_ok=True)
        if os.path.isfile(final):
            st = os.stat(final)
            if st.st_size == size and int(st.st_mtime_ns // 1_000_000) == mtime_ms:
                self.files_skipped += 1
                self.bytes_done += size
                self._reply(epoch, A_SKIP, 0, "已有相同文件")
                return
            if not self.cfg.overwrite:
                self.files_skipped += 1
                self.bytes_done += size
                self._reply(epoch, A_SKIP, 0, "已有同名文件（不覆盖）")
                return
        e = {"size": size, "mtime_ms": mtime_ms, "head": hcrc, "rel": rel}
        a = _Active(e, epoch, chunk, final, 0, set())
        a.window = self.window
        base, extra = self._load_meta(a)
        try:
            free = shutil.disk_usage(os.path.dirname(final)).free
        except OSError:
            free = 1 << 62
        need = max(0, size - (base * chunk))
        if free < need + (1 << 20):
            self.failed.append(rel)
            self.log(f"磁盘空间不足: {rel}")
            self._reply(epoch, A_REJECT, 0, f"磁盘空间不足（需要 {need} 字节）")
            return
        a.base, a.done = base, extra
        mode = "r+b" if base or extra else "wb"
        try:
            a.fh = open(a.part, mode)
        except OSError as ex:
            self.failed.append(rel)
            self._reply(epoch, A_REJECT, 0, f"无法创建文件: {ex}")
            return
        a.dirty = True
        a.save_meta()
        self.act = a
        self.cur_name = rel
        self._speed.clear()
        self._reply(epoch, A_RECEIVE, base)
        self.log(f"{'续传' if base else '接收'} {rel}（{size} 字节" + (f"，已有 {base} 块" if base else "") + "）")
        if size == 0:
            pass                                                     # END follows at once

    def _load_meta(self, a: _Active) -> Tuple[int, set]:
        try:
            with open(a.meta, "r", encoding="utf-8") as f:
                m = json.load(f)
            ok = (m.get("v") in META_VERSIONS and m["size"] == a.size and m["mtime_ms"] == a.e["mtime_ms"] and m["head"] == a.e["head"]
                  and m["chunk"] == a.chunk and os.path.isfile(a.part))
            base, extra = int(m["base"]), set(int(x) for x in m["done"])
            if ok and os.path.getsize(a.part) >= min(a.size, base * a.chunk) and base <= a.n:
                return base, {x for x in extra if base < x < a.n}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        for f in (a.part, a.meta):
            try:
                os.remove(f)
            except OSError:
                pass
        return 0, set()

    def _end(self, p: bytes) -> None:
        epoch, size, crc = struct.unpack(">BQI", p[:13])
        a = self.act
        text = ""
        ok = False
        if a is None or a.epoch != epoch:
            text = "没有对应的文件"
        else:
            with a.lock:
                complete = a.base >= a.n
                if complete and a.repairing:                       # the repaired range is whole: back to the full file
                    a.n, a.base, a.done, a.repairing = a.n_full, a.n_full, set(), False
            if not complete:
                text = f"还缺 {a.n - a.base} 块"
            else:
                try:
                    with a.lock:
                        if a.fh is not None:
                            a.fh.flush()
                            a.fh.truncate(a.size)
                            a.fh.close()
                            a.fh = None
                    got = file_crc(a.part, self.stop_ev)
                    if os.path.getsize(a.part) == size and got == crc:
                        os.replace(a.part, a.final)
                        t = a.e["mtime_ms"] / 1000.0
                        os.utime(a.final, ns=(a.e["mtime_ms"] * 1_000_000,) * 2)
                        try:
                            os.remove(a.meta)
                        except OSError:
                            pass
                        ok = True
                        self.files_done += 1
                        self.bytes_done += size
                        self.log(f"完成 {a.e['rel']}（CRC32 {got:08X}）")
                    else:
                        text = f"CRC 校验不一致（收到 {got:08X}，应为 {crc:08X}）"
                        a.mismatch, a.segtab = True, None           # keep every received byte: the sender locates the damaged segments
                        self.log(f"{a.e['rel']}: {text}，保留已收数据，等待按段比对修复")
                        self.msg.send(M_RESULT, struct.pack(">BB", epoch, 2) + text.encode("utf-8")[:200], timeout=10.0, tries=10)
                        return
                except OSError as e:
                    text = f"写文件失败: {e}"
        self.act = None
        if not ok and a is not None and text and "CRC" not in text:
            self.failed.append(a.e["rel"])
        self.msg.send(M_RESULT, struct.pack(">BB", epoch, 1 if ok else 0) + text.encode("utf-8")[:200], timeout=10.0, tries=10)


    # ------------------------------------------------------------ repair of a file whose whole-file CRC did not match
    def _segq(self, p: bytes) -> None:
        epoch, first, cnt = struct.unpack(">BIH", p[:7])
        a = self.act
        if a is None or a.epoch != epoch or not (a.mismatch or a.repairing):
            return
        if a.segtab is None:                                       # one pass over the file, then every query is answered from memory
            spc = max(1, SEG_BYTES // a.chunk)
            seg = spc * a.chunk
            tab: List[int] = []
            with open(a.part, "rb") as f:
                for s in range(0, (a.n_full + spc - 1) // spc):
                    crc, left = 0, min(seg, a.size - s * seg)
                    while left > 0:
                        if self.stop_ev.is_set():
                            raise CftError("已停止")
                        b = f.read(min(left, 1 << 20))
                        if not b:
                            break
                        crc = zlib.crc32(b, crc)
                        left -= len(b)
                    tab.append(crc & 0xFFFFFFFF)
            a.segtab = tab
        part = a.segtab[first:first + cnt]
        self.msg.send(M_SEGR, struct.pack(">BIH", epoch, first, len(part)) + struct.pack(f">{len(part)}I", *part), timeout=10.0, tries=10)

    def _repair(self, p: bytes) -> None:
        epoch, lo, hi = struct.unpack(">BII", p[:9])
        a = self.act
        if a is None or not (a.mismatch or a.repairing) or not 0 <= lo < hi <= a.n_full:
            self._reply(epoch, A_REJECT, 0, "没有可修复的文件")
            return
        with a.lock:
            if a.fh is None:
                a.fh = open(a.part, "r+b")
            a.epoch, a.base, a.n, a.done, a.repairing, a.segtab = epoch, lo, hi, set(), True, None
            a.bufs.clear()
            a.dirty = True
        self._reply(epoch, A_RECEIVE, lo)
        self.log(f"修复 {a.e['rel']}：重收第 {lo}~{hi - 1} 块")

    def _discard(self, p: bytes) -> None:
        a, self.act = self.act, None
        if a is None:
            return
        with a.lock:
            if a.fh is not None:
                a.fh.close()
                a.fh = None
        for f in (a.part, a.meta):
            try:
                os.remove(f)
            except OSError:
                pass
        self.log(f"{a.e['rel']}: 修复没有成功，已丢弃已收数据，等待重新发送")


# ====================================================================== background job (ops / UI)
class CftJob:
    """Runs a Sender / Receiver in a thread; `status()` is safe to poll."""

    def __init__(self, engine, runner: Callable[[], Any], on_done: Optional[Callable[[], None]] = None) -> None:
        self.engine, self._runner, self._on_done = engine, runner, on_done
        self.thread = threading.Thread(target=self._go, daemon=True, name="cft-job")

    def start(self) -> "CftJob":
        self.thread.start()
        return self

    def _go(self) -> None:
        try:
            self._runner()
        finally:
            if self._on_done:
                self._on_done()

    def running(self) -> bool:
        return self.thread.is_alive()

    def stop(self, wait: float = 10.0) -> None:
        self.engine.stop()
        self.thread.join(timeout=wait)

    def status(self) -> Dict[str, Any]:
        s = self.engine.status()
        s["running"] = self.running()
        s["log"] = list(self.engine.log.lines)[-40:]
        return s
