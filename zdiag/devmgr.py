"""Device manager: open/close a device, start/stop its channels, hand out the shared buses (no Qt).

State machine (one-way toggles, as in the UI):
    device:  closed --open()--> opened --close()--> closed
    channel: stopped --start_channel()--> started --stop_channel()--> stopped
Closing the device stops all its channels; a channel that a window is using cannot be stopped.
"""
from __future__ import annotations
import os
import threading
from typing import Callable, Dict, List, Optional

from .busstats import BusStats, ChannelCfg, CountingBus
from .monitor import FrameTap
from .project import DEVICES, SIM_DEVICE


class ConnError(Exception):
    pass


def find_zlgcan(base: str, override: str = "") -> str:
    """zlgcan.dll lookup: explicit path, next to the program, then <program>\\vendor (source checkout)."""
    for p in (override, os.path.join(base, "zlgcan.dll"), os.path.join(base, "vendor", "zlgcan.dll")):
        if p and os.path.isfile(p):
            return p
    return override or os.path.join(base, "zlgcan.dll")


def channel_count(device: str) -> int:
    if device == "USBCANFD-400U":
        return 4
    if device in ("USBCANFD-100U", "USBCANFD-MINI"):      # 100U: 1 channel per usbcanfd-100u.xml; MINI assumed 1
        return 1
    return 2


def is_fd_device(device: str) -> bool:
    return device.startswith("USBCANFD") or device == SIM_DEVICE


def has_terminator(device: str) -> bool:
    """Software-switchable termination resistor (only the FD devices have it)."""
    return device.startswith("USBCANFD")


def bitrate_choices_kbps(device: str) -> List[float]:
    if device.startswith("USBCANFD") or device == SIM_DEVICE:
        return [1000, 800, 500, 250, 125, 100, 50, 20, 10, 5]
    return [1000, 800, 500, 250, 125, 100, 50, 20, 10]            # USBCAN-II timing table


class _Chan:
    def __init__(self, cfg: ChannelCfg, bus, stats: BusStats, n: int = 0) -> None:
        self.cfg, self.bus, self.stats, self.n = cfg, bus, stats, n
        self.ecu = None                  # simulator only
        self.sim_key = None


class DeviceManager:
    def __init__(self, dll_dir: str, dll_path_fn: Optional[Callable[[], str]] = None) -> None:
        self.dll_dir = dll_dir
        self.dll_path_fn = dll_path_fn or (lambda: "")
        self.name: Optional[str] = None
        self.index = 0
        self.device = None                       # ZlgDevice when a real device is open
        self.chans: Dict[int, _Chan] = {}
        self.owners: Dict[int, object] = {}
        self.listeners: List[Callable[[], None]] = []
        self._lock = threading.RLock()
        self.tap = FrameTap()                    # passive monitor shared by all channels (see zdiag/monitor.py)

    # ------------------------------------------------------------ observers
    def add_listener(self, fn) -> None:
        if fn not in self.listeners:
            self.listeners.append(fn)

    def remove_listener(self, fn) -> None:
        if fn in self.listeners:
            self.listeners.remove(fn)

    def _notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass

    # ------------------------------------------------------------ state
    @property
    def is_open(self) -> bool:
        return self.name is not None

    def started(self) -> List[int]:
        return sorted(self.chans)

    def cfg_of(self, ch: int) -> Optional[ChannelCfg]:
        c = self.chans.get(ch)
        return c.cfg if c else None

    def stats_of(self, ch: int) -> Optional[BusStats]:
        c = self.chans.get(ch)
        return c.stats if c else None

    def sim_ecu(self, ch: int = 0):
        c = self.chans.get(ch)
        return c.ecu if c else None

    # ------------------------------------------------------------ device
    def open(self, name: str, index: int = 0) -> None:
        with self._lock:
            if self.is_open:
                raise ConnError("设备已经打开")
            if name not in DEVICES:
                raise ConnError(f"不支持的设备: {name}")
            if name != SIM_DEVICE:
                self.device = self._open_zlg(name, index)
            self.name, self.index = name, index
        self._notify()

    def _open_zlg(self, name: str, index: int):
        dll = find_zlgcan(self.dll_dir, self.dll_path_fn())
        if not os.path.isfile(dll):
            raise ConnError(f"找不到 {dll}\n请把 zlgcan.dll 和 kerneldlls 文件夹复制到程序目录，"
                            f"或在「更多设置 → 通道设置」里指定路径。")
        try:
            from .zlgcan_bus import ZlgDevice
            return ZlgDevice(dll, name, index)
        except Exception as e:
            raise ConnError(f"打开设备失败: {e}")

    def close(self) -> None:
        """Close the device (stops every channel). Raises ConnError while a window is using one."""
        with self._lock:
            busy = [c for c in self.chans if c in self.owners]
            if busy:
                raise ConnError("通道正在被诊断/刷新窗口使用，请先停止后再关闭设备")
            self.shutdown()
        self._notify()

    def shutdown(self) -> None:
        """Unconditional teardown (application exit)."""
        with self._lock:
            for n in list(self.chans):
                self._stop_locked(n)
            if self.device is not None:
                try:
                    self.device.close()
                except Exception:
                    pass
            self.device = None
            self.name = None
            self.owners.clear()
            self.tap.stop_record()

    # ------------------------------------------------------------ channels
    def start_channel(self, n: int, cfg: ChannelCfg, sim_addr=None, sim_profile: str = "new",
                      sim_padding: int = 0xAA) -> None:
        with self._lock:
            if not self.is_open:
                raise ConnError("请先打开设备")
            if not 0 <= n < channel_count(self.name):
                raise ConnError(f"该设备没有通道{n}")
            if n in self.chans:
                raise ConnError(f"通道{n} 已经启动")
            if cfg.is_fd and not is_fd_device(self.name):
                raise ConnError("该设备不支持 CAN FD")
            try:
                cfg.validate()
            except ValueError as e:
                raise ConnError(str(e))
            stats = BusStats(cfg.bitrate, cfg.data_bitrate)
            if self.name == SIM_DEVICE:
                ch = _Chan(cfg, None, stats, n)
                self._build_sim(ch, sim_addr, sim_profile, sim_padding)
            else:
                try:
                    bus = self.device.start_channel(n, cfg, self.tap)
                except Exception as e:
                    raise ConnError(f"启动通道{n} 失败: {e}")
                ch = _Chan(cfg, bus, bus.stats, n)
            self.chans[n] = ch
        self._notify()

    def _build_sim(self, ch: _Chan, addr, profile: str, padding: int = 0xAA) -> None:
        from .project import Addressing
        from .sim_ecu import make_sim
        addr = addr or Addressing()
        if ch.ecu is not None:
            ch.ecu.stop()
        bus, ecu = make_sim(addr, profile, padding=padding)
        n, tap = ch.n, self.tap
        bus.tap = lambda fr, d: tap.feed(n, fr, d)                     # tester end of the virtual bus: TX / RX
        ch.bus, ch.ecu = CountingBus(bus, ch.stats), ecu
        ch.sim_key = (profile, addr.phys, addr.resp, padding)

    def stop_channel(self, n: int) -> None:
        with self._lock:
            if n in self.owners:
                raise ConnError(f"通道{n} 正在被诊断/刷新窗口使用，请先停止")
            self._stop_locked(n)
        self._notify()

    def _stop_locked(self, n: int) -> None:
        ch = self.chans.pop(n, None)
        if ch is None:
            return
        if ch.ecu is not None:
            ch.ecu.stop()
        elif self.device is not None:
            try:
                self.device.stop_channel(n)
            except Exception:
                pass
        self.owners.pop(n, None)

    # ------------------------------------------------------------ sharing the bus with the windows
    def acquire(self, n: int, owner, addr=None, sim_profile: str = "new", sim_padding: int = 0xAA):
        with self._lock:
            ch = self.chans.get(n)
            if ch is None:
                raise ConnError("通道未启动：请先在主界面打开设备并启动该通道")
            if self.owners.get(n) not in (None, owner):
                raise ConnError(f"通道{n} 正被另一个窗口使用，请先停止它")
            if ch.ecu is not None and addr is not None and ch.sim_key != (sim_profile, addr.phys, addr.resp, sim_padding):
                self._build_sim(ch, addr, sim_profile, sim_padding)        # simulator follows the address/product settings
            if ch.cfg.listen_only:
                raise ConnError(f"通道{n} 处于只听模式，不能发送诊断请求；请停止后以正常模式重新启动")
            self.owners[n] = owner
            return ch.bus

    def release(self, owner) -> None:
        with self._lock:
            for n in [k for k, o in self.owners.items() if o is owner]:
                del self.owners[n]
