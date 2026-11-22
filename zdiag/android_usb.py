"""Android USB host bridge for the gs_usb driver (zdiag/gsusb.py) - no pyusb / libusb on the phone.

The Android app (Kotlin) asks for the USB permission, opens the device (UsbManager.openDevice), claims interface 0 and finds the two bulk
endpoints; it then calls attach(connection, interface_number, ep_in, ep_out, present) here (through Chaquopy).  DeviceManager._open_gs
picks the attached connection up instead of pyusb.  Everything Java-specific is confined to _jbuf / _jbytes so tests can use fakes."""
from __future__ import annotations
import collections
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
    """Chaquopy turns a Python bytes object into a Java byte[] by itself (one JNI call, not one per byte)."""
    return bytes(data)


def _pybytes(buf, n: int) -> bytes:
    """Java byte[] -> bytes.  bytes(array) copies the whole array in one JNI call; going through list() would cost one call per element
    and made the receive thread so slow that the echoes of our own frames came late and the send window ran dry."""
    if isinstance(buf, (bytes, bytearray)):
        return bytes(buf[:n])
    return bytes(buf)[:n]


class AndroidUsbIO(UsbIO):
    def __init__(self, conn, ifnum: int, ep_in, ep_out, present: Optional[Callable[[], bool]] = None, reader=None, writer=None) -> None:
        self.conn, self.ifnum, self.ep_in, self.ep_out, self.present = conn, ifnum, ep_in, ep_out, present
        self.reader = reader                                   # Kotlin thread that keeps the IN endpoint drained (see UsbReader.kt)
        self.writer = writer                                   # Kotlin thread pool: several bulk-OUT transfers in flight at once (UsbWriter.kt)
        if writer is not None:
            self.write_frames = self._write_frames
        self._chunks: "collections.deque" = collections.deque()
        self._rbuf = None
        self._rsize = 0
        self._chk = 0.0
        self._closed = False
        self.w_timeouts = self.w_ok = self.r_ok = 0
        self.w_ms = 0.0                                        # moving average of one bulk-OUT call, milliseconds

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

    def _read_reader(self, timeout_ms):
        if not self._chunks:
            blob = self.reader.drain(int(timeout_ms))          # many frames in ONE call: [u16 len][bytes]...
            if blob is not None:
                b = bytes(blob)
                i = 0
                while i + 2 <= len(b):
                    n = b[i] | (b[i + 1] << 8)
                    self._chunks.append(b[i + 2:i + 2 + n])
                    i += 2 + n
        if self._chunks:
            self.r_ok += 1
            return self._chunks.popleft()
        if self._gone():
            raise GsUsbError("USB 设备已拔出")
        return None

    def read(self, size, timeout_ms):
        if self.reader is not None:
            return self._read_reader(timeout_ms)
        if self._rbuf is None or self._rsize != size:
            self._rbuf, self._rsize = _jbuf(size), size
        n = self.conn.bulkTransfer(self.ep_in, self._rbuf, size, timeout_ms)
        if n > 0:
            self.r_ok += 1
            return _pybytes(self._rbuf, n)
        if self._gone():
            raise GsUsbError("USB 设备已拔出")
        return None

    def _write_frames(self, frames, timeout_ms):
        """Send frames concurrently; returns how many LEADING frames went through (later ones may have too - the caller treats the rest as
        unsent and the protocol above tolerates the resulting duplicates)."""
        blob = b"".join(len(f).to_bytes(2, "little") + f for f in frames)
        t0 = time.perf_counter()
        k = int(self.writer.writeAll(blob, max(timeout_ms, 300)))
        self.w_ms += ((time.perf_counter() - t0) * 1000.0 / max(1, len(frames)) - self.w_ms) * 0.05
        self.w_ok += k
        if k < len(frames):
            self.w_timeouts += 1
            if self._gone():
                raise GsUsbError("USB 设备已拔出")
        return k

    def write(self, data, timeout_ms):
        d = bytes(data)
        # A timeout cancels the transfer; cancelling between the two USB packets of an 80-byte frame would leave half a frame in the device
        # and shift every following one.  So "timeout" must mean "really stuck", not "busy for a moment".
        t0 = time.perf_counter()
        n = self.conn.bulkTransfer(self.ep_out, _jbytes(d), len(d), max(timeout_ms, 300))
        self.w_ms += ((time.perf_counter() - t0) * 1000.0 - self.w_ms) * 0.02
        if n >= len(d):
            self.w_ok += 1
            return True
        self.w_timeouts += 1
        if self._gone():
            raise GsUsbError("USB 设备已拔出")
        return False

    def close(self):
        """Closing the CAN device in the UI must not destroy the USB connection the app opened: it stays usable for the next open."""
        self._chunks.clear()

    def flush(self):
        self._chunks.clear()
        if self.reader is not None:
            while self.reader.drain(0) is not None:
                pass

    def release(self):
        """The app gave the connection up (unplugged / replaced)."""
        self._closed = True
        for h in (self.reader, self.writer):
            if h is not None:
                try:
                    h.stop()
                except Exception:
                    pass


_current: Optional[AndroidUsbIO] = None
_cur_lock = threading.Lock()


def _install_hooks() -> None:
    """Thread priority / ADPF boost (Perf.kt) for the threads that move CAN frames, and its status text for the diagnostics line."""
    from . import gsusb
    try:
        from java import jclass
        perf = jclass("com.canvds.app.Perf")
        gsusb.THREAD_HOOK = lambda: perf.boostThisThread()
        gsusb.BOOST_INFO = lambda: perf.info()
    except Exception:
        pass                                                           # not on Android (tests)


def attach(conn, ifnum: int, ep_in, ep_out, present=None, reader=None, writer=None) -> None:
    """Called by the app once the permission is granted and the interface is claimed."""
    global _current
    _install_hooks()
    with _cur_lock:
        old, _current = _current, AndroidUsbIO(conn, ifnum, ep_in, ep_out, present, reader, writer)
    if old is not None:
        old.release()


def detach() -> None:
    global _current
    with _cur_lock:
        old, _current = _current, None
    if old is not None:
        old.release()


def current() -> Optional[AndroidUsbIO]:
    with _cur_lock:
        return _current if _current is not None and not _current._closed else None
