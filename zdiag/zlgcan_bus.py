"""ctypes binding for zlgcan.dll (ZLG ZCAN API) -> Bus implementation.

Facts verified from the DLL you supplied:
  * 32-bit x86 PE  -> you MUST run a 32-bit Python (python-3.x-win32).
  * exports are cdecl (plain `ret`)  -> ctypes.CDLL, not WinDLL.
  * it loads kerneldlls\\CANDevice.dll etc. at runtime -> copy the whole
    `kerneldlls` folder from the ZCANPro install dir next to zlgcan.dll.

Struct layouts / device-type numbers follow the public zlgcan.h / zlgcan_demo.
Please double check them against the header that ships with your SDK.
"""
from __future__ import annotations
import ctypes as C
import os
import queue
import threading
import time
from typing import Optional, Union

from .bus import Bus, CanFrame
from .busstats import BusStats, ChannelCfg

# ---- device types (zlgcan.h) -------------------------------------------------
ZCAN_USBCAN1 = 3
ZCAN_USBCAN2 = 4            # USBCAN-II / II+
ZCAN_USBCANFD_200U = 41
ZCAN_USBCANFD_100U = 42
ZCAN_USBCANFD_MINI = 43
ZCAN_USBCANFD_400U = 76     # verify with your header
DEVICE_TYPES = {
    "USBCAN-II": ZCAN_USBCAN2, "USBCANFD-200U": ZCAN_USBCANFD_200U,
    "USBCANFD-100U": ZCAN_USBCANFD_100U, "USBCANFD-MINI": ZCAN_USBCANFD_MINI,
    "USBCANFD-400U": ZCAN_USBCANFD_400U,
}
FD_TYPES = {ZCAN_USBCANFD_200U, ZCAN_USBCANFD_100U, ZCAN_USBCANFD_MINI, ZCAN_USBCANFD_400U}

# classic SJA1000-style timing (USBCAN-II, 8 MHz-equivalent as used by ZLG)
TIMING_T0T1 = {
    1_000_000: (0x00, 0x14), 800_000: (0x00, 0x16), 500_000: (0x00, 0x1C),
    250_000: (0x01, 0x1C), 125_000: (0x03, 0x1C), 100_000: (0x04, 0x1C),
    50_000: (0x09, 0x1C), 20_000: (0x18, 0x1C), 10_000: (0x31, 0x1C),
}

STATUS_OK = 1
TYPE_CAN, TYPE_CANFD = 0, 1
INVALID_HANDLE = 0


# ---- structures --------------------------------------------------------------
class ZCAN_CAN_FRAME(C.Structure):
    _fields_ = [("can_id", C.c_uint, 29), ("err", C.c_uint, 1),
                ("rtr", C.c_uint, 1), ("eff", C.c_uint, 1),
                ("can_dlc", C.c_ubyte), ("pad", C.c_ubyte),
                ("res0", C.c_ubyte), ("res1", C.c_ubyte),
                ("data", C.c_ubyte * 8)]


class ZCAN_CANFD_FRAME(C.Structure):
    _fields_ = [("can_id", C.c_uint, 29), ("err", C.c_uint, 1),
                ("rtr", C.c_uint, 1), ("eff", C.c_uint, 1),
                ("len", C.c_ubyte), ("flags", C.c_ubyte),   # flags: bit0 BRS, bit1 ESI
                ("res0", C.c_ubyte), ("res1", C.c_ubyte),
                ("data", C.c_ubyte * 64)]


class ZCAN_Transmit_Data(C.Structure):
    _fields_ = [("frame", ZCAN_CAN_FRAME), ("transmit_type", C.c_uint)]


class ZCAN_TransmitFD_Data(C.Structure):
    _fields_ = [("frame", ZCAN_CANFD_FRAME), ("transmit_type", C.c_uint)]


class ZCAN_Receive_Data(C.Structure):
    _fields_ = [("frame", ZCAN_CAN_FRAME), ("timestamp", C.c_ulonglong)]


class ZCAN_ReceiveFD_Data(C.Structure):
    _fields_ = [("frame", ZCAN_CANFD_FRAME), ("timestamp", C.c_ulonglong)]


class _CanCfg(C.Structure):
    _fields_ = [("acc_code", C.c_uint), ("acc_mask", C.c_uint), ("reserved", C.c_uint),
                ("filter", C.c_ubyte), ("timing0", C.c_ubyte),
                ("timing1", C.c_ubyte), ("mode", C.c_ubyte)]


class _CanFdCfg(C.Structure):
    _fields_ = [("acc_code", C.c_uint), ("acc_mask", C.c_uint),
                ("abit_timing", C.c_uint), ("dbit_timing", C.c_uint),
                ("brp", C.c_uint), ("filter", C.c_ubyte), ("mode", C.c_ubyte),
                ("pad", C.c_ushort), ("reserved", C.c_uint)]


class _CfgUnion(C.Union):
    _fields_ = [("can", _CanCfg), ("canfd", _CanFdCfg)]


class ZCAN_CHANNEL_INIT_CONFIG(C.Structure):
    _fields_ = [("can_type", C.c_uint), ("cfg", _CfgUnion)]   # 0 = CAN, 1 = CAN FD


class ZlgError(RuntimeError):
    pass


def _dlc_len(n: int) -> int:
    for v in (8, 12, 16, 20, 24, 32, 48, 64):
        if n <= v:
            return v
    raise ValueError("FD payload > 64")


def supported_bitrates_kbps(device: str):
    """USBCAN-II only has the fixed timing table; FD devices accept any rate (common ones offered)."""
    if DEVICE_TYPES[device] not in FD_TYPES:
        return sorted((b / 1000 for b in TIMING_T0T1), reverse=True)
    return [1000, 800, 500, 250, 125, 100, 50, 20, 10, 5]


class ZlgDevice:
    """One opened physical device (ZCAN_OpenDevice); channels are started on it individually."""

    def __init__(self, dll_path: str, device: str, dev_index: int = 0) -> None:
        self.name = device
        self.dev_type = DEVICE_TYPES[device]
        self.is_fd_dev = self.dev_type in FD_TYPES
        dll_path = os.path.abspath(dll_path)
        os.add_dll_directory(os.path.dirname(dll_path)) if hasattr(os, "add_dll_directory") else None
        self.lib = C.CDLL(dll_path)             # cdecl (verified)
        L = self.lib
        L.ZCAN_OpenDevice.restype = C.c_void_p
        L.ZCAN_OpenDevice.argtypes = [C.c_uint, C.c_uint, C.c_uint]
        L.ZCAN_CloseDevice.argtypes = [C.c_void_p]
        L.ZCAN_SetValue.argtypes = [C.c_void_p, C.c_char_p, C.c_char_p]
        L.ZCAN_SetValue.restype = C.c_uint
        L.ZCAN_InitCAN.restype = C.c_void_p
        L.ZCAN_InitCAN.argtypes = [C.c_void_p, C.c_uint, C.POINTER(ZCAN_CHANNEL_INIT_CONFIG)]
        for n in ("ZCAN_StartCAN", "ZCAN_ResetCAN", "ZCAN_ClearBuffer"):
            getattr(L, n).argtypes = [C.c_void_p]
            getattr(L, n).restype = C.c_uint
        L.ZCAN_GetReceiveNum.argtypes = [C.c_void_p, C.c_ubyte]
        L.ZCAN_GetReceiveNum.restype = C.c_uint
        L.ZCAN_Transmit.argtypes = [C.c_void_p, C.POINTER(ZCAN_Transmit_Data), C.c_uint]
        L.ZCAN_Transmit.restype = C.c_uint
        L.ZCAN_TransmitFD.argtypes = [C.c_void_p, C.POINTER(ZCAN_TransmitFD_Data), C.c_uint]
        L.ZCAN_TransmitFD.restype = C.c_uint
        L.ZCAN_Receive.argtypes = [C.c_void_p, C.POINTER(ZCAN_Receive_Data), C.c_uint, C.c_int]
        L.ZCAN_Receive.restype = C.c_uint
        L.ZCAN_ReceiveFD.argtypes = [C.c_void_p, C.POINTER(ZCAN_ReceiveFD_Data), C.c_uint, C.c_int]
        L.ZCAN_ReceiveFD.restype = C.c_uint
        self.handle = L.ZCAN_OpenDevice(self.dev_type, dev_index, 0)
        if not self.handle:
            raise ZlgError("ZCAN_OpenDevice 失败：请检查设备是否插好、驱动是否安装、kerneldlls 是否在 zlgcan.dll 旁边、"
                           "是否为 32 位 Python、设备索引是否正确。")
        self.buses: dict = {}

    def set_value(self, path: str, value) -> None:
        if self.lib.ZCAN_SetValue(self.handle, path.encode(), str(value).encode()) != STATUS_OK:
            raise ZlgError(f"ZCAN_SetValue({path}={value}) 失败")

    def start_channel(self, channel: int, cfg: ChannelCfg) -> "ZlgBus":
        if channel in self.buses:
            raise ZlgError(f"通道{channel} 已经启动")
        if cfg.is_fd and not self.is_fd_dev:
            raise ZlgError("该设备不支持 CAN FD")
        bus = ZlgBus(self, channel, cfg)
        self.buses[channel] = bus
        return bus

    def stop_channel(self, channel: int) -> None:
        bus = self.buses.pop(channel, None)
        if bus is not None:
            bus.close()

    def close(self) -> None:
        for ch in list(self.buses):
            self.stop_channel(ch)
        if self.handle:
            self.lib.ZCAN_CloseDevice(self.handle)
            self.handle = None


class ZlgBus(Bus):
    """One started channel of a ZlgDevice.  Owns the rx thread and the BusStats."""

    def __init__(self, device: ZlgDevice, channel: int, cfg: ChannelCfg) -> None:
        self.device, self.channel, self.cfg = device, channel, cfg
        self.use_fd = cfg.is_fd
        self.stats = BusStats(cfg.bitrate, cfg.data_bitrate)
        L, ch = device.lib, channel
        init = ZCAN_CHANNEL_INIT_CONFIG()
        mode = 1 if cfg.listen_only else 0                        # work_mode: 0 normal, 1 read-only
        # Property paths / ordering below follow kerneldlls\devices_property\*.xml of the official SDK.
        if device.is_fd_dev:
            device.set_value(f"{ch}/protocol", 1 if cfg.is_fd else 0)              # pre-InitCAN
            device.set_value(f"{ch}/canfd_abit_baud_rate", cfg.bitrate)            # also the CAN rate
            if cfg.is_fd:
                device.set_value(f"{ch}/canfd_dbit_baud_rate", cfg.data_bitrate)
            init.can_type = TYPE_CANFD                            # "FD series must use 1" (zlgcan.h)
            init.cfg.canfd.acc_code, init.cfg.canfd.acc_mask, init.cfg.canfd.mode = 0, 0xFFFFFFFF, mode
        else:
            key = round(cfg.bitrate_kbps * 1000)
            if key not in TIMING_T0T1:
                raise ZlgError("USBCAN-II 不支持该波特率，可选: " +
                               ", ".join(f"{b:g}" for b in supported_bitrates_kbps(device.name)) + " kbaud/s")
            device.set_value(f"{ch}/baud_rate", key)                               # pre-InitCAN
            init.can_type = TYPE_CAN
            init.cfg.can.acc_code, init.cfg.can.acc_mask, init.cfg.can.filter = 0, 0xFFFFFFFF, 1
            init.cfg.can.timing0, init.cfg.can.timing1 = TIMING_T0T1[key]          # consistent fallback
            init.cfg.can.mode = mode
        self.chn = L.ZCAN_InitCAN(device.handle, ch, C.byref(init))
        if not self.chn:
            raise ZlgError(f"通道{ch} 初始化失败(ZCAN_InitCAN)")
        if device.is_fd_dev:                                                       # post-InitCAN
            device.set_value(f"{ch}/initenal_resistance", 1 if cfg.terminator else 0)  # sic: ZLG spelling
        if L.ZCAN_StartCAN(self.chn) != STATUS_OK:
            raise ZlgError(f"通道{ch} 启动失败(ZCAN_StartCAN)")
        self.lib = L
        self._q: "queue.Queue[CanFrame]" = queue.Queue()
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._rx_loop, name=f"zlg-rx{ch}", daemon=True)
        self._thr.start()

    def _rx_loop(self) -> None:
        L = self.lib
        buf = (ZCAN_Receive_Data * 100)()
        fbuf = (ZCAN_ReceiveFD_Data * 100)()
        while not self._stop.is_set():
            got = False
            n = L.ZCAN_GetReceiveNum(self.chn, 0)
            if n:
                k = L.ZCAN_Receive(self.chn, buf, min(n, 100), 0)
                for i in range(k):
                    f = buf[i].frame
                    if f.err:
                        self.stats.count_err()
                        continue
                    fr = CanFrame(f.can_id, bytes(f.data[:f.can_dlc]), bool(f.eff),
                                  False, False, buf[i].timestamp / 1e6)
                    self.stats.count_rx(fr)
                    if not f.rtr:
                        self._q.put(fr)
                got = got or k > 0
            if self.use_fd:
                n = L.ZCAN_GetReceiveNum(self.chn, 1)
                if n:
                    k = L.ZCAN_ReceiveFD(self.chn, fbuf, min(n, 100), 0)
                    for i in range(k):
                        f = fbuf[i].frame
                        if f.err:
                            self.stats.count_err()
                            continue
                        fr = CanFrame(f.can_id, bytes(f.data[:f.len]), bool(f.eff),
                                      True, bool(f.flags & 1), fbuf[i].timestamp / 1e6)
                        self.stats.count_rx(fr)
                        if not f.rtr:
                            self._q.put(fr)
                    got = got or k > 0
            if not got:
                time.sleep(0.0005)

    # -- Bus API
    def send(self, fr: CanFrame) -> None:
        if self.cfg.listen_only:
            raise ZlgError("通道处于只听模式，不能发送")
        L = self.lib
        if fr.fd and self.use_fd:
            t = ZCAN_TransmitFD_Data()
            t.transmit_type = 0
            t.frame.can_id = fr.can_id
            t.frame.eff = 1 if fr.extended else 0
            data = fr.data.ljust(_dlc_len(len(fr.data)), b"\x00")
            t.frame.len = len(data)
            t.frame.flags = 1 if fr.brs else 0
            C.memmove(t.frame.data, data, len(data))
            ok = L.ZCAN_TransmitFD(self.chn, C.byref(t), 1)
        else:
            if len(fr.data) > 8:
                raise ZlgError("classic CAN frame > 8 bytes")
            t = ZCAN_Transmit_Data()
            t.transmit_type = 0
            t.frame.can_id = fr.can_id
            t.frame.eff = 1 if fr.extended else 0
            t.frame.rtr = 1 if fr.rtr else 0
            t.frame.can_dlc = len(fr.data)
            C.memmove(t.frame.data, fr.data, len(fr.data))
            ok = L.ZCAN_Transmit(self.chn, C.byref(t), 1)
        if ok != 1:
            self.stats.count_err()
            raise ZlgError("发送失败(总线关闭/发送缓冲满/无应答?)")
        self.stats.count_tx(fr)

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
        self._stop.set()
        self._thr.join(timeout=1)
        self.lib.ZCAN_ResetCAN(self.chn)
