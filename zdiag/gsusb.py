"""gs_usb (candleLight / CANable "Candlelight" firmware, e.g. Elmue 2.5) driver over WinUSB/libusb via pyusb (no Qt, no vendor DLL).

Layering (so everything above can be tested without hardware):
    protocol helpers  - struct (de)serialisation of host frames / control requests, bit-timing calculation (pure functions)
    UsbIO             - 5 methods the driver needs from USB: ctrl_out, ctrl_in, read, write, close; PyUsbIO is the real one
    GsUsbDevice       - one opened USB device (index-th match), owns the single RX thread that demultiplexes by channel
    GsUsbBus          - one started channel; implements zdiag.bus.Bus (send / send_many / recv / flush / close)

Protocol reference: Linux drivers/net/can/usb/gs_usb.c and candleLight_fw.  Everything is little endian.
Frame:  u32 echo_id | u32 can_id | u8 dlc | u8 channel | u8 flags | u8 reserved | data[8 | 64] | (u32 timestamp_us)
        echo_id = 0xFFFFFFFF for received frames, otherwise the id we sent (the device echoes a frame once it is on the bus).
"""
from __future__ import annotations
import collections
import os
import queue
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .bus import Bus, CanFrame
from .busstats import BusStats, ChannelCfg

# ---------------------------------------------------------------- constants
KNOWN_IDS = [(0x1D50, 0x606F),      # candleLight / CANable Candlelight (this is what the 鹿小班 CANable2.0 shows after flashing)
             (0x1209, 0x2323), (0x1CD2, 0x606F), (0x16D0, 0x10B8), (0x16D0, 0x0F30)]

BREQ_HOST_FORMAT, BREQ_BITTIMING, BREQ_MODE, BREQ_BERR, BREQ_BT_CONST, BREQ_DEVICE_CONFIG, BREQ_TIMESTAMP = 0, 1, 2, 3, 4, 5, 6
BREQ_IDENTIFY, BREQ_GET_USER_ID, BREQ_SET_USER_ID, BREQ_DATA_BITTIMING, BREQ_BT_CONST_EXT = 7, 8, 9, 10, 11
BREQ_SET_TERMINATION, BREQ_GET_TERMINATION, BREQ_GET_STATE = 12, 13, 14

MODE_RESET, MODE_START = 0, 1
F_LISTEN_ONLY, F_LOOP_BACK, F_TRIPLE_SAMPLE, F_ONE_SHOT, F_HW_TIMESTAMP = 1, 2, 4, 8, 16
F_IDENTIFY, F_USER_ID, F_PAD_PKTS, F_FD, F_BT_CONST_EXT, F_TERMINATION, F_BERR_REPORTING, F_GET_STATE = 32, 64, 128, 256, 1024, 2048, 4096, 8192

FLAG_OVERFLOW, FLAG_FD, FLAG_BRS, FLAG_ESI = 1, 2, 4, 8
ID_EFF, ID_RTR, ID_ERR = 0x80000000, 0x40000000, 0x20000000
ERR_BUSOFF = 0x40
ECHO_RX = 0xFFFFFFFF
DLC_LEN = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]

TX_WINDOW = int(os.environ.get("CANVDS_GS_TXWIN", "32"))      # frames that may be on their way (sent, not yet echoed) at the same time
TX_PACK = int(os.environ.get("CANVDS_GS_PACK", "1"))          # frames concatenated into ONE bulk write; 1 = the standard one frame per transfer


class GsUsbError(RuntimeError):
    pass


def dlc_code(n: int) -> int:
    for i, v in enumerate(DLC_LEN):
        if v >= n:
            return i
    raise ValueError("frame longer than 64 bytes")


# ---------------------------------------------------------------- frames
def frame_size(fd: bool, ts: bool) -> int:
    return 12 + (64 if fd else 8) + (4 if ts else 0)


def pack_frame(fr: CanFrame, echo_id: int, channel: int, fd_mode: bool, ts: bool, brs_ok: bool = True) -> bytes:
    cid = fr.can_id | (ID_EFF if fr.extended else 0) | (ID_RTR if fr.rtr else 0)
    flags = 0
    if fr.fd and fd_mode:
        data = bytes(fr.data).ljust(DLC_LEN[dlc_code(len(fr.data))], b"\x00")
        dlc = dlc_code(len(data))
        flags = FLAG_FD | (FLAG_BRS if (fr.brs and brs_ok) else 0)
    else:
        if len(fr.data) > 8:
            raise GsUsbError("classic CAN frame > 8 bytes")
        data, dlc = bytes(fr.data), len(fr.data)
    head = struct.pack("<IIBBBB", echo_id, cid, dlc, channel, flags, 0)
    return head + data.ljust(64 if fd_mode else 8, b"\x00") + (b"\x00\x00\x00\x00" if ts else b"")


def unpack_frames(buf: bytes, fd_mode: bool, ts: bool) -> List[dict]:
    """Split one bulk-IN transfer into frames.  The size is the one we expect for the mode; when the transfer length says otherwise
    (some firmware sends the short form for classic frames) it is inferred from the length instead."""
    out, pos, exp = [], 0, frame_size(fd_mode, ts)
    while len(buf) - pos >= 12 + 8:
        rest = len(buf) - pos
        size = exp if rest >= exp else None
        if size is None or (rest != exp and rest in (20, 24, 76, 80)):
            size = rest if rest in (20, 24, 76, 80) else None
        if size is None:
            break
        echo, cid, dlc, ch, flags, _ = struct.unpack_from("<IIBBBB", buf, pos)
        big = size in (76, 80)
        n = 64 if big else 8
        has_ts = size in (24, 80)
        is_fd = bool(flags & FLAG_FD)
        ln = DLC_LEN[dlc & 15] if is_fd else min(dlc, 8)
        data = bytes(buf[pos + 12:pos + 12 + min(ln, n)])
        t_us = struct.unpack_from("<I", buf, pos + 12 + n)[0] if has_ts else None
        out.append(dict(echo=echo, id=cid & 0x1FFFFFFF, eff=bool(cid & ID_EFF), rtr=bool(cid & ID_RTR), err=bool(cid & ID_ERR), raw_id=cid,
                        dlc=dlc, ch=ch, flags=flags, fd=is_fd, brs=bool(flags & FLAG_BRS), data=data, ts=t_us))
        pos += size
    return out


# ---------------------------------------------------------------- bit timing
def calc_bittiming(fclk: int, rate: int, sample_pct: float, lim: Dict[str, int]) -> Dict[str, int]:
    """Exact-rate bit timing: brp * (1 + tseg1 + tseg2) * rate == fclk.  Among all exact solutions the sample point closest to the
    wanted one wins; ties go to the finer time quantum (more tq per bit).  Raises ValueError when the rate cannot be made exactly."""
    best = None
    brp_inc = max(1, lim.get("brp_inc", 1))
    for brp in range(max(1, lim["brp_min"]), lim["brp_max"] + 1, brp_inc):
        if fclk % (brp * rate):
            continue
        total = fclk // (brp * rate)
        for t2 in range(lim["tseg2_min"], lim["tseg2_max"] + 1):
            t1 = total - 1 - t2
            if not lim["tseg1_min"] <= t1 <= lim["tseg1_max"]:
                continue
            sp = (1 + t1) * 100.0 / total
            key = (abs(sp - sample_pct), brp)
            if best is None or key < best[0]:
                best = (key, brp, t1, t2, sp)
    if best is None:
        raise ValueError(f"无法用 {fclk / 1e6:g} MHz 时钟精确产生 {rate / 1000:g} kbaud/s（请换一个常用波特率）")
    _, brp, t1, t2, sp = best
    prop = t1 // 2
    return dict(prop_seg=prop, phase_seg1=t1 - prop, phase_seg2=t2, sjw=max(1, min(lim["sjw_max"], t2)), brp=brp, sample_pct=sp)


def auto_sample_pct(rate: int) -> float:
    return 75.0 if rate > 800_000 else 80.0 if rate > 500_000 else 87.5


def parse_bt_const(b: bytes) -> Dict[str, int]:
    f = struct.unpack_from("<10I", b, 0)
    return dict(feature=f[0], fclk=f[1], tseg1_min=f[2], tseg1_max=f[3], tseg2_min=f[4], tseg2_max=f[5], sjw_max=f[6],
                brp_min=f[7], brp_max=f[8], brp_inc=f[9])


# ---------------------------------------------------------------- USB layer
class UsbIO:
    """What the driver needs from USB.  Implemented by PyUsbIO (real) and by the fake in tests."""
    def ctrl_out(self, breq: int, value: int, data: bytes) -> None: raise NotImplementedError
    def ctrl_in(self, breq: int, value: int, length: int) -> bytes: raise NotImplementedError
    def read(self, size: int, timeout_ms: int) -> Optional[bytes]: raise NotImplementedError      # None = timeout
    def write(self, data: bytes, timeout_ms: int) -> bool: raise NotImplementedError              # False = timeout (device FIFO full)
    def close(self) -> None: pass


def find_libusb(base: str = "") -> str:
    for p in (os.path.join(base, "libusb-1.0.dll"), os.path.join(base, "vendor", "libusb-1.0.dll")):
        if p and os.path.isfile(p):
            return p
    return ""


def list_usb_devices(base: str = "") -> List[dict]:
    """Enumerate gs_usb devices: [{index, vid, pid, bus, address, serial, product}]."""
    usb_core = _import_usb()
    backend = _backend(base)
    devs = _find(usb_core, backend)
    out = []
    for i, d in enumerate(devs):
        item = dict(index=i, vid=d.idVendor, pid=d.idProduct, bus=getattr(d, "bus", 0), address=getattr(d, "address", 0), serial="", product="")
        try:
            item["serial"] = d.serial_number or ""
            item["product"] = d.product or ""
        except Exception:
            pass
        out.append(item)
    return out


def _import_usb():
    try:
        import usb.core
        import usb.util            # noqa: F401
        return usb.core
    except ImportError:
        raise GsUsbError("缺少 pyusb：请执行 python -m pip install pyusb，并把 32 位 libusb-1.0.dll 放到程序目录")


def _backend(base: str):
    import usb.backend.libusb1 as lb
    path = find_libusb(base)
    be = lb.get_backend(find_library=(lambda _n: path)) if path else lb.get_backend()
    if be is None:
        raise GsUsbError("找不到 libusb-1.0.dll：请把 32 位的 libusb-1.0.dll 放到程序目录（与 exe 同级）")
    return be


def _find(usb_core, backend) -> list:
    devs = [d for d in usb_core.find(find_all=True, backend=backend) if (d.idVendor, d.idProduct) in KNOWN_IDS]
    devs.sort(key=lambda d: (getattr(d, "bus", 0), getattr(d, "address", 0)))
    return devs


class PyUsbIO(UsbIO):
    def __init__(self, base: str, index: int = 0) -> None:
        import usb.util
        usb_core = _import_usb()
        devs = _find(usb_core, _backend(base))
        if index >= len(devs):
            raise GsUsbError("没有找到 gs_usb 设备（VID 1D50 / PID 606F）：请检查是否插好，设备管理器里是否有 \"CAN FD Interface\"")
        self.dev, self._util = devs[index], usb.util
        try:
            self.dev.set_configuration()
        except Exception:
            pass                                                   # WinUSB: already configured
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.ifnum = intf.bInterfaceNumber
        eps = list(intf)
        self.ep_in = next((e.bEndpointAddress for e in eps if e.bEndpointAddress & 0x80 and (e.bmAttributes & 3) == 2), 0x81)
        self.ep_out = next((e.bEndpointAddress for e in eps if not e.bEndpointAddress & 0x80 and (e.bmAttributes & 3) == 2), 0x02)
        try:
            usb.util.claim_interface(self.dev, self.ifnum)
        except Exception as e:
            raise GsUsbError(f"占用 USB 接口失败（是否被别的程序打开？）: {e}")

    def ctrl_out(self, breq, value, data):
        self.dev.ctrl_transfer(0x41, breq, value, self.ifnum, data, 1000)

    def ctrl_in(self, breq, value, length):
        return bytes(self.dev.ctrl_transfer(0xC1, breq, value, self.ifnum, length, 1000))

    def read(self, size, timeout_ms):
        import usb.core
        try:
            return bytes(self.dev.read(self.ep_in, size, timeout_ms))
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as e:
            if getattr(e, "errno", None) in (110, 10060) or "timeout" in str(e).lower():
                return None
            raise GsUsbError(f"USB 读失败（设备被拔出？）: {e}")

    def write(self, data, timeout_ms):
        import usb.core
        try:
            self.dev.write(self.ep_out, data, timeout_ms)
            return True
        except usb.core.USBTimeoutError:
            return False
        except usb.core.USBError as e:
            if getattr(e, "errno", None) in (110, 10060) or "timeout" in str(e).lower():
                return False
            raise GsUsbError(f"USB 写失败（设备被拔出？）: {e}")

    def close(self):
        try:
            self._util.release_interface(self.dev, self.ifnum)
        except Exception:
            pass
        try:
            self._util.dispose_resources(self.dev)
        except Exception:
            pass


# ---------------------------------------------------------------- device
class GsUsbDevice:
    """One opened gs_usb device.  `io_factory(index)` is replaced by a fake in tests."""

    def __init__(self, name: str, index: int = 0, base: str = "", io: Optional[UsbIO] = None) -> None:
        self.name, self.index = name, index
        self.io = io or PyUsbIO(base, index)
        self.buses: Dict[int, "GsUsbBus"] = {}
        self._lock = threading.RLock()
        try:
            self.io.ctrl_out(BREQ_HOST_FORMAT, 1, struct.pack("<I", 0x0000BEEF))
            cfgb = self.io.ctrl_in(BREQ_DEVICE_CONFIG, 1, 12)
        except Exception as e:
            self.io.close()
            raise GsUsbError(f"设备不响应 gs_usb 请求（固件不是 Candlelight？）: {e}")
        icount, self.sw_version, self.hw_version = cfgb[3], *struct.unpack_from("<II", cfgb, 4)
        self.channels = icount + 1
        self.consts: Dict[int, Tuple[Dict[int, int], Optional[Dict[str, int]]]] = {}
        for ch in range(self.channels):
            nom = parse_bt_const(self.io.ctrl_in(BREQ_BT_CONST, ch, 40))
            dat = None
            if nom["feature"] & F_BT_CONST_EXT:
                try:
                    ext = self.io.ctrl_in(BREQ_BT_CONST_EXT, ch, 80)
                    nom, dat = parse_bt_const(ext[:40]), parse_bt_const(ext[40:80])
                except Exception:
                    pass
            self.consts[ch] = (nom, dat)
        self._clk: Optional[float] = None
        self._ts_hi, self._ts_last = 0, 0
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._rx_loop, name="gsusb-rx", daemon=True)
        self._thr.start()
        try:
            from .zlgcan_bus import hires_timer
            hires_timer(True)
            self._timer = True
        except Exception:
            self._timer = False

    # -- info
    @property
    def is_fd_dev(self) -> bool:
        return any(c[0]["feature"] & F_FD for c in self.consts.values())

    def info(self) -> dict:
        nom, dat = self.consts[0]
        return dict(channels=self.channels, sw=self.sw_version, hw=self.hw_version, fclk=nom["fclk"], feature=nom["feature"],
                    fd=bool(nom["feature"] & F_FD), nominal=nom, data=dat)

    # -- channels
    def start_channel(self, channel: int, cfg: ChannelCfg, tap=None, extra_flags: int = 0) -> "GsUsbBus":
        with self._lock:
            if channel in self.buses:
                raise GsUsbError(f"通道{channel} 已经启动")
            if not 0 <= channel < self.channels:
                raise GsUsbError(f"该设备没有通道{channel}")
            if cfg.is_fd and not self.consts[channel][0]["feature"] & F_FD:
                raise GsUsbError("该固件不支持 CAN FD")
            bus = GsUsbBus(self, channel, cfg, tap, extra_flags)
            self.buses[channel] = bus
            return bus

    def stop_channel(self, channel: int) -> None:
        with self._lock:
            bus = self.buses.pop(channel, None)
        if bus is not None:
            bus.close()

    def close(self) -> None:
        for ch in list(self.buses):
            self.stop_channel(ch)
        self._stop.set()
        self._thr.join(timeout=1)
        self.io.close()
        if self._timer:
            try:
                from .zlgcan_bus import hires_timer
                hires_timer(False)
            except Exception:
                pass
            self._timer = False

    # -- time
    def host_ts(self, dev_us: Optional[int]) -> float:
        now = time.perf_counter()
        if dev_us is None:
            return now
        if dev_us < self._ts_last and self._ts_last - dev_us > 0x80000000:       # 32-bit µs counter wrapped (every 71 min)
            self._ts_hi += 1
        self._ts_last = dev_us
        dev = (self._ts_hi * 4294967296 + dev_us) / 1e6
        off = now - dev
        if self._clk is None or abs(off - self._clk) > 0.5:
            self._clk = off
        return dev + self._clk

    # -- rx thread (one IN endpoint for all channels)
    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                buf = self.io.read(1024, 20)
            except GsUsbError as e:
                for b in list(self.buses.values()):
                    b._fail(str(e))
                return
            if not buf:
                continue
            fds = dict(self.buses)
            any_bus = next(iter(fds.values()), None)
            if any_bus is None:
                continue
            for f in unpack_frames(buf, any_bus.fd_mode, any_bus.use_ts):
                b = fds.get(f["ch"])
                if b is not None:
                    b._on_frame(f)


class GsUsbBus(Bus):
    """One started channel.  Owns the BusStats and the receive queue; TX is flow-controlled by the device's echo frames."""

    RX_QUEUE_MAX = 100_000

    def __init__(self, device: GsUsbDevice, channel: int, cfg: ChannelCfg, tap=None, extra_flags: int = 0) -> None:
        self.device, self.channel, self.cfg, self.tap = device, channel, cfg, tap
        self.io = device.io
        self.use_fd = cfg.is_fd
        self.fd_mode = cfg.is_fd
        nom, dat = device.consts[channel]
        self.use_ts = bool(nom["feature"] & F_HW_TIMESTAMP)
        self.stats = BusStats(cfg.bitrate, cfg.data_bitrate)
        self.events: "collections.deque" = collections.deque(maxlen=50)
        self.recoveries = self.q_dropped = 0
        self.nominal = calc_bittiming(nom["fclk"], cfg.bitrate, cfg.sample_pct or auto_sample_pct(cfg.bitrate), nom)
        self.databt = None
        if cfg.is_fd:
            lim = dat or nom
            self.databt = calc_bittiming(lim["fclk"], cfg.data_bitrate, cfg.data_sample_pct or auto_sample_pct(cfg.data_bitrate), lim)
        self._q: "queue.Queue[CanFrame]" = queue.Queue()
        self._lock = threading.Lock()
        self._free = collections.deque(range(TX_WINDOW))
        self._pending: Dict[int, CanFrame] = {}
        self._failed: Optional[str] = None
        self._recover_t = 0.0
        self._flags = (F_LISTEN_ONLY if cfg.listen_only else 0) | (F_FD if cfg.is_fd else 0) | (F_HW_TIMESTAMP if self.use_ts else 0)
        self._flags |= extra_flags
        if nom["feature"] & F_BERR_REPORTING:
            self._flags |= F_BERR_REPORTING
        self._configure(nom)
        self._start()

    # -- bring-up
    def _configure(self, nom) -> None:
        io, ch = self.io, self.channel
        b = self.nominal
        io.ctrl_out(BREQ_BITTIMING, ch, struct.pack("<5I", b["prop_seg"], b["phase_seg1"], b["phase_seg2"], b["sjw"], b["brp"]))
        if self.databt:
            d = self.databt
            io.ctrl_out(BREQ_DATA_BITTIMING, ch, struct.pack("<5I", d["prop_seg"], d["phase_seg1"], d["phase_seg2"], d["sjw"], d["brp"]))
        if nom["feature"] & F_TERMINATION:
            try:
                io.ctrl_out(BREQ_SET_TERMINATION, ch, struct.pack("<I", 1 if self.cfg.terminator else 0))
            except Exception as e:
                self._note(f"设置终端电阻失败: {e}")
        elif self.cfg.terminator:
            self._note("固件没有软件终端电阻功能（请用硬件跳线/焊接终端电阻）")

    def _start(self) -> None:
        self.io.ctrl_out(BREQ_MODE, self.channel, struct.pack("<II", MODE_START, self._flags))
        self._note(f"通道已启动: 仲裁域 brp={self.nominal['brp']} 采样点 {self.nominal['sample_pct']:.1f}%"
                   + (f"，数据域 brp={self.databt['brp']} 采样点 {self.databt['sample_pct']:.1f}%" if self.databt else ""))

    def _note(self, text: str) -> None:
        self.events.append((time.time(), text))

    def _fail(self, why: str) -> None:
        self._failed = why
        self._note(why)

    # -- rx path (called from the device's rx thread)
    def _on_frame(self, f: dict) -> None:
        ts = self.device.host_ts(f["ts"])
        if f["echo"] != ECHO_RX:                                          # our own frame is on the bus now
            with self._lock:
                fr = self._pending.pop(f["echo"], None)
                if fr is not None:
                    self._free.append(f["echo"])
            if fr is not None:
                self.stats.count_tx(fr)
                if self.tap is not None:
                    self.tap.feed(self.channel, fr, "TX", ts)
            return
        if f["err"]:
            self.stats.count_err()
            if self.tap is not None:
                self.tap.feed_err(self.channel, ts)
            if f["id"] & ERR_BUSOFF:
                self._recover("总线关闭(bus-off)")
            return
        fr = CanFrame(f["id"] & (0x1FFFFFFF if f["eff"] else 0x7FF), f["data"], f["eff"], f["fd"], f["brs"], ts, f["rtr"])
        self.stats.count_rx(fr)
        if self.tap is not None:
            self.tap.feed(self.channel, fr, "RX", ts)
        if not f["rtr"]:
            if self._q.qsize() >= self.RX_QUEUE_MAX:
                try:
                    self._q.get_nowait()
                    self.q_dropped += 1
                except queue.Empty:
                    pass
            self._q.put(fr)

    def _recover(self, why: str) -> None:
        if time.monotonic() - self._recover_t < 1.0:
            return
        self._recover_t = time.monotonic()
        try:
            self.io.ctrl_out(BREQ_MODE, self.channel, struct.pack("<II", MODE_RESET, 0))
            with self._lock:
                self._pending.clear()
                self._free = collections.deque(range(TX_WINDOW))
            self._start()
            self.recoveries += 1
            self._note(f"{why}，已重启通道（第 {self.recoveries} 次）")
        except Exception as e:
            self._note(f"{why}，重启通道失败: {e}")

    def health(self) -> dict:
        d = {}
        try:
            if self.device.consts[self.channel][0]["feature"] & F_GET_STATE:
                st, rxe, txe = struct.unpack_from("<III", self.io.ctrl_in(BREQ_GET_STATE, self.channel, 12))
                d.update(state=st, rec=rxe, tec=txe, bus_off=(st == 3))
        except Exception:
            pass
        d.update(err_frames=self.stats.err, recoveries=self.recoveries, rx_dropped=self.q_dropped,
                 events=[t for _, t in list(self.events)[-5:]])
        return d

    # -- Bus API
    def _claim(self, fr: CanFrame) -> Optional[int]:
        with self._lock:
            if not self._free:
                return None
            eid = self._free.popleft()
            self._pending[eid] = fr
            return eid

    def _release(self, eid: int) -> None:
        with self._lock:
            if self._pending.pop(eid, None) is not None:
                self._free.append(eid)

    def _check(self) -> None:
        if self.cfg.listen_only:
            raise GsUsbError("通道处于只听模式，不能发送")
        if self._failed:
            raise GsUsbError(self._failed)

    def send(self, fr: CanFrame) -> None:
        self._check()
        t_end = time.monotonic() + 0.5
        while True:
            if self.send_many([fr]):
                return
            if time.monotonic() > t_end:
                self.stats.count_err()
                raise GsUsbError("发送失败(总线关闭/发送缓冲满/无应答?)")
            time.sleep(0.0005)

    def send_many(self, frames: list) -> int:
        self._check()
        sent, i, n = 0, 0, len(frames)
        pack = max(1, TX_PACK)
        while i < n:
            batch, ids = [], []
            while i + len(batch) < n and len(batch) < pack:
                fr = frames[i + len(batch)]
                eid = self._claim(fr)
                if eid is None:
                    break
                batch.append(fr)
                ids.append(eid)
            if not batch:
                break
            data = b"".join(pack_frame(fr, eid, self.channel, self.fd_mode, self.use_ts, self.cfg.fd_accel) for fr, eid in zip(batch, ids))
            try:
                ok = self.io.write(data, 20)
            except GsUsbError:
                for eid in ids:
                    self._release(eid)
                raise
            if not ok:                                                    # device FIFO full (USB NAK) -> nothing of this batch went out
                for eid in ids:
                    self._release(eid)
                break
            sent += len(batch)
            i += len(batch)
        return sent

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
                break

    def close(self) -> None:
        try:
            self.io.ctrl_out(BREQ_MODE, self.channel, struct.pack("<II", MODE_RESET, 0))
        except Exception:
            pass
