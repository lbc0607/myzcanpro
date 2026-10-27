"""UDS (ISO 14229-1) client.  One outstanding request at a time (per ECU)."""
from __future__ import annotations
import logging
import threading
import time
from typing import Callable, Optional

from .isotp import IsoTp

log = logging.getLogger("zdiag.uds")

NRC = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat", 0x14: "responseTooLong",
    0x21: "busyRepeatRequest", 0x22: "conditionsNotCorrect", 0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent", 0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange", 0x33: "securityAccessDenied", 0x35: "invalidKey",
    0x36: "exceededNumberOfAttempts", 0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted", 0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure", 0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending", 0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

# SIDs whose positive response echoes N bytes of the request (sub-function / DID / RID)
_ECHO = {0x10: 1, 0x11: 1, 0x27: 1, 0x28: 1, 0x3E: 1, 0x85: 1, 0x19: 1,
         0x22: 2, 0x2E: 2, 0x31: 3, 0x36: 1}


class UdsError(Exception):
    pass


class UdsTimeout(UdsError):
    pass


class NegativeResponse(UdsError):
    def __init__(self, sid: int, nrc: int):
        self.sid, self.nrc = sid, nrc
        super().__init__(f"NRC 0x{nrc:02X} ({NRC.get(nrc, '?')}) for service 0x{sid:02X}")


KeyFn = Callable[[bytes, int], bytes]          # (seed, level) -> key


class UdsClient:
    def __init__(self, tp: IsoTp, p2: float = 0.2, p2_star: float = 5.0,
                 max_pending: int = 600) -> None:
        self.tp, self.p2, self.p2_star, self.max_pending = tp, p2, p2_star, max_pending
        self.suppress_wait = False     # True: after a suppress-positive request return at once
        self.auto_keepalive = True     # session() starts/stops the 3E keep-alive by itself
        self.hook = None               # optional callable(kind, payload): "TX" | "RX" | "NRC"
        self._lock = threading.RLock()
        self._ka_stop: Optional[threading.Event] = None
        self._ka_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ core
    @staticmethod
    def _echo_ok(req: bytes, rsp: bytes) -> bool:
        n = _ECHO.get(req[0], 0)
        if n == 0:
            return True
        if len(rsp) < 1 + n or len(req) < 1 + n:
            return False
        want = bytes([req[1] & 0x7F]) + req[2:1 + n] if req[0] in (0x10, 0x11, 0x27, 0x28, 0x3E, 0x85, 0x19, 0x31) \
            else req[1:1 + n]
        return rsp[1:1 + n] == want

    def request(self, payload: bytes, *, suppress_positive: bool = False,
                p2: Optional[float] = None, p2_star: Optional[float] = None,
                tx_id: Optional[int] = None) -> bytes:
        """Send one request and return the (positive) response bytes.

        Matching rules: only frames on the ECU's response CAN ID reach this code
        (filtered in IsoTp). Then a response belongs to this request iff
          * positive:  SID+0x40 and echoed sub-function/DID/RID match, or
          * negative:  7F <this SID> <NRC>.
        Anything else (late answers of an earlier request) is discarded.
        NRC 0x78 re-arms the timer with P2*.
        """
        p2 = self.p2 if p2 is None else p2
        p2_star = self.p2_star if p2_star is None else p2_star
        with self._lock:                                     # serialise everything
            old_tx = self.tp.tx_id
            if tx_id is not None:
                self.tp.tx_id = tx_id                        # e.g. functional address
            try:
                return self._request_locked(payload, suppress_positive, p2, p2_star)
            finally:
                self.tp.tx_id = old_tx

    def _request_locked(self, payload, suppress_positive, p2, p2_star) -> bytes:
        sid = payload[0]
        if suppress_positive and len(payload) >= 2:
            payload = bytes([sid, payload[1] | 0x80]) + payload[2:]
        else:
            suppress_positive = False
        self.tp.flush()                                  # drop stale bytes
        log.debug("TX %s", payload[:16].hex(" "))
        if self.hook:
            self.hook("TX", payload)
        self.tp.send(payload)                            # P2 starts when the last frame is out
        if suppress_positive and self.suppress_wait:
            return b""
        deadline = time.perf_counter() + p2
        pending = 0
        while True:
            rsp = self.tp.recv(max(deadline - time.perf_counter(), 0))
            if rsp is None:
                if suppress_positive:
                    return b""
                raise UdsTimeout(f"no response to 0x{sid:02X} within "
                                 f"{'P2*' if pending else 'P2'}")
            if rsp[0] == 0x7F and len(rsp) >= 3 and rsp[1] == sid:
                if rsp[2] == 0x78:
                    pending += 1
                    if pending > self.max_pending:
                        raise UdsTimeout("too many ResponsePending")
                    deadline = time.perf_counter() + p2_star
                    continue
                log.debug("RX NRC %s", rsp.hex(" "))
                if self.hook:
                    self.hook("NRC", rsp)
                raise NegativeResponse(sid, rsp[2])
            if rsp[0] == sid + 0x40 and self._echo_ok(payload, rsp):
                log.debug("RX %s", rsp[:16].hex(" "))
                if self.hook:
                    self.hook("RX", rsp)
                return rsp
            log.debug("discard unrelated %s", rsp[:8].hex(" "))

    def send_only(self, payload: bytes, tx_id: Optional[int] = None) -> None:
        """Fire-and-forget (functional TesterPresent etc.). Still takes the lock."""
        with self._lock:
            old_tx = self.tp.tx_id
            if tx_id is not None:
                self.tp.tx_id = tx_id
            try:
                if self.hook:
                    self.hook("TX", payload)
                self.tp.send(payload)
            finally:
                self.tp.tx_id = old_tx

    # ------------------------------------------------------------ keep-alive
    def start_keepalive(self, period: float = 2.0, tx_id: Optional[int] = None,
                        payload: bytes = b"\x3E\x80") -> None:
        """Cyclic `3E 80`. It shares the request lock, so it can never be injected in the
        middle of another transaction. Any request already resets the server's S3 timer,
        so this only matters while the host is idle."""
        self.stop_keepalive()
        self._ka_stop = threading.Event()

        def run(stop: threading.Event):
            while not stop.wait(period):
                try:
                    self.send_only(payload, tx_id)
                except Exception as e:                       # keep going; surfaced in log
                    log.warning("keepalive failed: %s", e)
        self._ka_thread = threading.Thread(target=run, args=(self._ka_stop,), daemon=True)
        self._ka_thread.start()

    def stop_keepalive(self) -> None:
        if self._ka_stop:
            self._ka_stop.set()
            self._ka_thread.join(timeout=1)
        self._ka_stop = self._ka_thread = None

    # ------------------------------------------------------------ services
    def session(self, sub: int, adopt_timing: bool = True) -> bytes:
        r = self.request(bytes([0x10, sub]))
        if adopt_timing and len(r) >= 6:
            p2 = int.from_bytes(r[2:4], "big") / 1000.0
            p2s = int.from_bytes(r[4:6], "big") / 100.0
            self.p2 = max(self.p2, p2 + 0.1)                 # margin for USB/CAN latency
            self.p2_star = max(self.p2_star, p2s + 0.5)
        if self.auto_keepalive:
            if sub & 0x7F != 0x01:
                self.start_keepalive()
            else:
                self.stop_keepalive()
        return r

    def ecu_reset(self, sub: int = 0x01) -> bytes:
        return self.request(bytes([0x11, sub]))

    def tester_present(self) -> bytes:
        return self.request(b"\x3E\x00")

    def read_did(self, did: int) -> bytes:
        return self.request(b"\x22" + did.to_bytes(2, "big"))[3:]

    def write_did(self, did: int, data: bytes) -> bytes:
        return self.request(b"\x2E" + did.to_bytes(2, "big") + data)

    def routine(self, ctrl: int, rid: int, data: bytes = b"", **kw) -> bytes:
        return self.request(bytes([0x31, ctrl]) + rid.to_bytes(2, "big") + data, **kw)[4:]

    def comm_control(self, ctrl: int, comm_type: int = 0x01, **kw) -> bytes:
        return self.request(bytes([0x28, ctrl, comm_type]), **kw)

    def control_dtc(self, setting: int, **kw) -> bytes:
        return self.request(bytes([0x85, setting]), **kw)

    def security_access(self, key_fn: KeyFn, level: int = 0x01) -> bool:
        """level = odd requestSeed sub-function. Returns False if already unlocked.
        No automatic retry: a wrong key counts against the ECU's attempt counter
        (NRC 0x36 / 0x37 are raised to the caller on purpose)."""
        if level % 2 == 0:
            raise ValueError("level must be the odd requestSeed sub-function")
        with self._lock:                                    # seed + key in one atomic exchange
            prep = getattr(key_fn, "prepare", None)         # e.g. write ECUserialnumber.txt
            if prep:
                prep(self)
            seed = self.request(bytes([0x27, level]))[2:]
            if not any(seed):
                return False                                # all-zero seed: already unlocked
            key = key_fn(seed, level)
            self.request(bytes([0x27, level + 1]) + key)
        return True

    def request_download(self, addr: int, size: int, addr_len: int = 4, size_len: int = 4,
                         dfi: int = 0x00) -> int:
        """Returns maxNumberOfBlockLength (the 0x36 payload INCLUDING SID+counter)."""
        alfi = (size_len << 4) | addr_len
        r = self.request(bytes([0x34, dfi, alfi]) + addr.to_bytes(addr_len, "big")
                         + size.to_bytes(size_len, "big"))
        n = r[1] >> 4
        return int.from_bytes(r[2:2 + n], "big")

    def transfer_data(self, seq: int, data: bytes) -> bytes:
        return self.request(bytes([0x36, seq & 0xFF]) + data)

    def request_transfer_exit(self, data: bytes = b"") -> bytes:
        return self.request(b"\x37" + data)
