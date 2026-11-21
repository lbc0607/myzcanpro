"""Android USB host bridge for the gs_usb driver (zdiag/gsusb.py) - no pyusb / libusb on the phone.

The Android app (Kotlin) asks for the USB permission, opens the device (UsbManager.openDevice), claims interface 0 and finds the two bulk
endpoints; it then calls attach(connection, interface_number, ep_in, ep_out, present) here (through Chaquopy).  DeviceManager._open_gs
picks the attached connection up instead of pyusb.  Everything Java-specific is confined to _jbuf / _jbytes so tests can use fakes."""
from __future__ import annotations
import os
import threading
import time
from typing import Callable, Optional

from .gsusb import GsUsbError, UsbIO


def on_android() -> bool:
    return bool(os.environ.get("ANDROID_ROOT")) and bool(os.environ.get("ANDROID_DATA"))


def _jbuf(n: int):
    try:
        from java import jarray, jbyte                       # Chaquopy
        return jarray(jbyte)(n)
    except ImportError:
        return bytearray(n)


def _jbytes(data: bytes):
    try:
        from java import jarray, jbyte
        return jarray(jbyte)(list(b - 256 if b > 127 else b for b in data))
    except ImportError:
        return bytes(data)


def _pybytes(buf, n: int) -> bytes:
    if isinstance(buf, (bytes, bytearray)):
        return bytes(buf[:n])
    return bytes((b & 0xFF) for b in list(buf)[:n])


class AndroidUsbIO(UsbIO):
    def __init__(self, conn, ifnum: int, ep_in, ep_out, present: Optional[Callable[[], bool]] = None) -> None:
        self.conn, self.ifnum, self.ep_in, self.ep_out, self.present = conn, ifnum, ep_in, ep_out, present
        self._rbuf = None
        self._rsize = 0
        self._chk = 0.0
        self._lock = threading.Lock()                          # UsbDeviceConnection calls are serialised per direction below
        self._closed = False

    def ctrl_out(self, breq, value, data):
        d = bytes(data)
        n = self.conn.controlTransfer(0x41, breq, value, self.ifnum, _jbytes(d), len(d), 1000)
        if n < 0:
            raise GsUsbError("USB 控制传输失败(request %d)" % breq)

    def ctrl_in(self, breq, value, length):
        buf = _jbuf(length)
        n = self.conn.controlTransfer(0xC1, breq, value, self.ifnum, buf, length, 1000)
        if n < 0:
            raise GsUsbError("USB 控制读取失败(request %d)" % breq)
        return _pybytes(buf, n)

    def _gone(self) -> bool:
        now = time.monotonic()
        if self._closed:
            return True
        if self.present is None or now - self._chk < 0.5:
            return False
        self._chk = now
        try:
            return not self.present()
        except Exception:
            return False

    def read(self, size, timeout_ms):
        if self._rbuf is None or self._rsize != size:
            self._rbuf, self._rsize = _jbuf(size), size
        n = self.conn.bulkTransfer(self.ep_in, self._rbuf, size, timeout_ms)
        if n > 0:
            return _pybytes(self._rbuf, n)
        if self._gone():
            raise GsUsbError("USB 设备已拔出")
        return None

    def write(self, data, timeout_ms):
        d = bytes(data)
        n = self.conn.bulkTransfer(self.ep_out, _jbytes(d), len(d), timeout_ms)
        if n >= len(d):
            return True
        if self._gone():
            raise GsUsbError("USB 设备已拔出")
        return False

    def close(self):
        self._closed = True


_current: Optional[AndroidUsbIO] = None
_cur_lock = threading.Lock()


def attach(conn, ifnum: int, ep_in, ep_out, present=None) -> None:
    """Called by the app once the permission is granted and the interface is claimed."""
    global _current
    with _cur_lock:
        old, _current = _current, AndroidUsbIO(conn, ifnum, ep_in, ep_out, present)
    if old is not None:
        old.close()


def detach() -> None:
    global _current
    with _cur_lock:
        old, _current = _current, None
    if old is not None:
        old.close()


def current() -> Optional[AndroidUsbIO]:
    with _cur_lock:
        return _current if _current is not None and not _current._closed else None
