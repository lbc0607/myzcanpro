"""Image loading (Intel HEX / S-record / BIN) and a configurable UDS flashing sequence.

The sequence below is the common ISO 14229 / OEM-bootloader pattern; every OEM differs in
routine IDs, checksum type, fingerprint DID and which preconditions it wants.  All of that is
in FlashConfig / hooks, so adapt the config rather than the code.
"""
from __future__ import annotations
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .uds import UdsClient, KeyFn


@dataclass
class Segment:
    addr: int
    data: bytes


# ------------------------------------------------------------------ image loading
def _merge(chunks: List[Segment]) -> List[Segment]:
    chunks.sort(key=lambda s: s.addr)
    out: List[Segment] = []
    for c in chunks:
        if out and out[-1].addr + len(out[-1].data) == c.addr:
            out[-1] = Segment(out[-1].addr, out[-1].data + c.data)
        else:
            out.append(Segment(c.addr, c.data))
    return out


def parse_ihex(text: str) -> List[Segment]:
    base, chunks = 0, []
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"line {ln}: not Intel HEX")
        raw = bytes.fromhex(line[1:])
        if sum(raw) & 0xFF:
            raise ValueError(f"line {ln}: checksum error")
        n, addr, typ = raw[0], (raw[1] << 8) | raw[2], raw[3]
        data = raw[4:4 + n]
        if typ == 0:
            chunks.append(Segment(base + addr, bytes(data)))
        elif typ == 2:
            base = int.from_bytes(data, "big") << 4
        elif typ == 4:
            base = int.from_bytes(data, "big") << 16
        elif typ == 1:
            break
    return _merge(chunks)


def parse_srec(text: str) -> List[Segment]:
    chunks = []
    for ln, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line[0] != "S" or line[1] not in "123":
            continue
        raw = bytes.fromhex(line[2:])
        if (sum(raw) & 0xFF) != 0xFF:
            raise ValueError(f"line {ln}: checksum error")
        alen = {"1": 2, "2": 3, "3": 4}[line[1]]
        addr = int.from_bytes(raw[1:1 + alen], "big")
        chunks.append(Segment(addr, bytes(raw[1 + alen:-1])))
    return _merge(chunks)


def load_image(path: str, bin_addr: int = 0) -> List[Segment]:
    low = path.lower()
    if low.endswith((".hex", ".ihx")):
        return parse_ihex(open(path, encoding="ascii", errors="replace").read())
    if low.endswith((".s19", ".srec", ".s28", ".s37", ".mot")):
        return parse_srec(open(path, encoding="ascii", errors="replace").read())
    return [Segment(bin_addr, open(path, "rb").read())]


# ------------------------------------------------------------------ flashing
def crc32_check(seg: Segment) -> bytes:
    return (zlib.crc32(seg.data) & 0xFFFFFFFF).to_bytes(4, "big")


@dataclass
class FlashConfig:
    prog_session: int = 0x02
    ext_session: int = 0x03
    security_level: int = 0x01            # odd requestSeed sub-function (OEMs often use 0x11 etc.)
    addr_len: int = 4
    size_len: int = 4
    erase_rid: int = 0xFF00
    check_rid: int = 0xFF01
    check_data: Callable[[Segment], bytes] = crc32_check   # bytes appended after the RID
    fingerprint_did: Optional[int] = 0xF15A
    fingerprint: bytes = b"\x00" * 9                       # tester serial / date, per OEM
    disable_comm: bool = True             # 85 02 + 28 03 01 before programming
    boot_delay: float = 0.3               # settle time after 10 02
    reset_type: int = 0x01
    max_block_cap: Optional[int] = None   # optionally cap 0x36 payload (e.g. 4095 for ISO-TP)
    # routineStatusRecord of the check routine -> True if verification passed (OEM specific;
    # default: empty record or first byte 0x00 means OK)
    check_ok: Callable[[bytes], bool] = lambda r: not r or r[0] == 0


class FlashError(Exception):
    pass


class Flasher:
    def __init__(self, uds: UdsClient, key_fn: KeyFn, cfg: FlashConfig = FlashConfig(),
                 functional: Optional[UdsClient] = None) -> None:
        self.uds, self.key_fn, self.cfg, self.func = uds, key_fn, cfg, functional

    def _alfi_blob(self, seg: Segment) -> bytes:
        c = self.cfg
        return seg.addr.to_bytes(c.addr_len, "big") + len(seg.data).to_bytes(c.size_len, "big")

    def run(self, segments: List[Segment],
            progress: Callable[[str, int, int], None] = lambda stage, done, total: None) -> None:
        u, c = self.uds, self.cfg
        total = sum(len(s.data) for s in segments)
        done = 0
        progress("session", 0, total)
        u.session(c.ext_session)
        if c.disable_comm:
            tgt = self.func or u                  # functional addressing if provided
            tgt.control_dtc(0x02, suppress_positive=True)
            tgt.comm_control(0x03, 0x01, suppress_positive=True)
        u.session(c.prog_session)
        time.sleep(c.boot_delay)
        progress("security", 0, total)
        u.security_access(self.key_fn, c.security_level)
        if c.fingerprint_did is not None:
            u.write_did(c.fingerprint_did, c.fingerprint)

        for seg in segments:
            progress("erase", done, total)
            u.routine(0x01, c.erase_rid, self._alfi_blob(seg))
            max_len = u.request_download(seg.addr, len(seg.data), c.addr_len, c.size_len)
            if c.max_block_cap:
                max_len = min(max_len, c.max_block_cap)
            payload = max_len - 2                 # minus SID and block counter
            if payload <= 0:
                raise FlashError(f"ECU returned unusable maxNumberOfBlockLength={max_len}")
            seq = 1
            for off in range(0, len(seg.data), payload):
                u.transfer_data(seq, seg.data[off:off + payload])
                seq = (seq + 1) & 0xFF
                done += min(payload, len(seg.data) - off)
                progress("transfer", done, total)
            u.request_transfer_exit()
            progress("verify", done, total)
            res = u.routine(0x01, c.check_rid, c.check_data(seg))
            if not c.check_ok(res):
                u.stop_keepalive()
                raise FlashError(f"verification failed for segment 0x{seg.addr:X}: status {res.hex()}"
                                 " - NOT resetting, ECU stays in bootloader")

        progress("reset", done, total)
        u.ecu_reset(c.reset_type)
        u.stop_keepalive()
        if c.disable_comm:
            time.sleep(1.0)                       # ECU reboots; comms restored by the reset itself
        progress("done", total, total)
