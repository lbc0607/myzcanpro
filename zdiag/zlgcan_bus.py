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


class ZlgBus(Bus):
    """One channel of a ZLG USBCAN-II or USBCANFD device.

    bitrate / data_bitrate are in bit/s.  For FD devices the arbitration and
    data rates are set through ZCAN_SetValue("<ch>/canfd_abit_baud_rate", ...)
    BEFORE ZCAN_InitCAN (this is the documented order for the *U series).
    """

    def __init__(self, dll_path: str, device: "Union[str, int]" = "USBCANFD-200U",
                 dev_index: int = 0, channel: int = 0, bitrate: int = 500_000,
                 data_bitrate: int = 2_000_000, fd: Optional[bool] = None,
                 terminator: bool = True, listen_only: bool = False) -> None:
        dev_type = DEVICE_TYPES[device] if isinstance(device, str) else device
        self.is_fd_dev = dev_type in FD_TYPES
        self.use_fd = self.is_fd_dev if fd is None else (fd and self.is_fd_dev)
        self.channel = channel
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

        self.dev = L.ZCAN_OpenDevice(dev_type, dev_index, 0)
        if not self.dev:
            raise ZlgError("ZCAN_OpenDevice failed (driver installed? kerneldlls present? "
                           "32-bit Python? device index?)")
        ch = channel
        cfg = ZCAN_CHANNEL_INIT_CONFIG()
        try:
            if self.is_fd_dev:
                self._set(f"{ch}/canfd_abit_baud_rate", bitrate)
                self._set(f"{ch}/canfd_dbit_baud_rate", data_bitrate)
                self._set(f"{ch}/initenal_resistance", 1 if terminator else 0)  # sic: ZLG spelling
                cfg.can_type = 1
                cfg.cfg.canfd.acc_code = 0
                cfg.cfg.canfd.acc_mask = 0xFFFFFFFF
                cfg.cfg.canfd.mode = 1 if listen_only else 0
            else:
                t0, t1 = TIMING_T0T1[bitrate]
                cfg.can_type = 0
                cfg.cfg.can.acc_code = 0
                cfg.cfg.can.acc_mask = 0xFFFFFFFF
                cfg.cfg.can.filter = 1
                cfg.cfg.can.timing0, cfg.cfg.can.timing1 = t0, t1
                cfg.cfg.can.mode = 1 if listen_only else 0
            self.chn = L.ZCAN_InitCAN(self.dev, ch, C.byref(cfg))
            if not self.chn:
                raise ZlgError("ZCAN_InitCAN failed")
            if L.ZCAN_StartCAN(self.chn) != STATUS_OK:
                raise ZlgError("ZCAN_StartCAN failed")
        except Exception:
            L.ZCAN_CloseDevice(self.dev)
            raise

        self._q: "queue.Queue[CanFrame]" = queue.Queue()
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._rx_loop, name="zlg-rx", daemon=True)
        self._thr.start()

    # -- helpers
    def _set(self, path: str, value) -> None:
        if self.lib.ZCAN_SetValue(self.dev, path.encode(), str(value).encode()) != STATUS_OK:
            raise ZlgError(f"ZCAN_SetValue({path}={value}) failed")

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
                    if f.err or f.rtr:
                        continue
                    self._q.put(CanFrame(f.can_id, bytes(f.data[:f.can_dlc]), bool(f.eff),
                                         False, False, buf[i].timestamp / 1e6))
                got = got or k > 0
            if self.use_fd:
                n = L.ZCAN_GetReceiveNum(self.chn, 1)
                if n:
                    k = L.ZCAN_ReceiveFD(self.chn, fbuf, min(n, 100), 0)
                    for i in range(k):
                        f = fbuf[i].frame
                        if f.err or f.rtr:
                            continue
                        self._q.put(CanFrame(f.can_id, bytes(f.data[:f.len]), bool(f.eff),
                                             True, bool(f.flags & 1), fbuf[i].timestamp / 1e6))
                    got = got or k > 0
            if not got:
                time.sleep(0.0005)

    # -- Bus API
    def send(self, fr: CanFrame) -> None:
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
            t.frame.can_dlc = len(fr.data)
            C.memmove(t.frame.data, fr.data, len(fr.data))
            ok = L.ZCAN_Transmit(self.chn, C.byref(t), 1)
        if ok != 1:
            raise ZlgError("transmit failed (bus-off / tx buffer full / no ACK?)")

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
        self.lib.ZCAN_CloseDevice(self.dev)
