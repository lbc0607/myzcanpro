"""A small simulated ECU (UDS server over ISO-TP) for testing without hardware."""
from __future__ import annotations
import threading
import time
import zlib
from typing import Dict

from .bus import Bus, CanFrame
from .isotp import IsoTp, IsoTpError
from .seedkey import xor_demo_key


class SimEcu:
    def __init__(self, bus: Bus, req_id=0x7E0, rsp_id=0x7E8, max_block=258,
                 chatter=True, erase_pending=2, key_fn=None,
                 serial=b"SN2024ABCD0001", seed_len=4, crc_rids=(), padding=0xAA) -> None:
        # the ECU receives on req_id, transmits on rsp_id; asks tester for BS=8 / STmin=1ms
        self.tp = IsoTp(bus, tx_id=rsp_id, rx_id=req_id, block_size=8, st_min=1, padding=padding)
        self.bus, self.rsp_id, self.max_block = bus, rsp_id, max_block
        self.session, self.unlocked, self.seed = 1, False, b""
        self.bad_keys = 0
        self.mem: Dict[int, int] = {}
        self.dl_addr = self.dl_len = self.dl_got = 0
        self.expect_seq = 1
        self.rx_crc = 0
        self.erase_pending, self.chatter = erase_pending, chatter
        self.reset_count = 0
        self.key_fn, self.serial = key_fn or xor_demo_key, serial
        self.seed_len, self.crc_rids = seed_len, set(crc_rids)
        self.crc_checked = []          # (rid, ok) of every CRC routine received
        self.tester_present_count = 0
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.thread.join(timeout=1)

    def _noise(self):
        # unrelated bus traffic, and a *stale* response from an earlier request
        if self.chatter:
            self.bus.send(CanFrame(0x123, bytes(8)))
            self.bus.send(CanFrame(0x7E9, b"\x03\x62\xF1\x90\xAA\xAA\xAA\xAA"))           # other ECU
            self.bus.send(CanFrame(self.rsp_id, b"\x03\x7F\x19\x31\xAA\xAA\xAA\xAA"))      # stale NRC of an earlier, different request

    def _run(self):
        while not self._stop.is_set():
            try:
                msg = self.tp.recv(0.05)
            except IsoTpError:
                continue
            if msg:
                try:
                    self._handle(msg)
                except IsoTpError:
                    pass

    def _reply(self, data: bytes):
        self.tp.send(data)

    def _nrc(self, sid, nrc):
        self._reply(bytes([0x7F, sid, nrc]))

    def _handle(self, m: bytes):
        sid, sub = m[0], (m[1] if len(m) > 1 else 0)
        sup = bool(sub & 0x80) and sid in (0x10, 0x11, 0x27, 0x28, 0x3E, 0x85, 0x31)
        self._noise()
        if sid == 0x10:
            self.session = sub & 0x7F
            if not sup:
                self._reply(bytes([0x50, sub & 0x7F, 0x00, 0x32, 0x01, 0xF4]))
        elif sid == 0x3E:
            self.tester_present_count += 1
            if not sup:
                self._reply(b"\x7E" + bytes([sub & 0x7F]))
        elif sid == 0x22:
            did = int.from_bytes(m[1:3], "big")
            if did == 0xF190:
                self._reply(b"\x62\xF1\x90" + b"LSVAB12345C000001")
            elif did == 0xF18C:
                self._reply(b"\x62\xF1\x8C" + self.serial)
            elif did == 0xF1A0:
                self._reply(b"\x62\xF1\xA0" + bytes(range(100)))         # long multi-frame
            else:
                self._nrc(sid, 0x31)
        elif sid == 0x2E:
            self._reply(b"\x6E" + m[1:3])
        elif sid == 0x27:
            if self.session == 1:
                return self._nrc(sid, 0x7F)
            if sub % 2:
                if self.seed_len == 4:
                    fresh = bytes([0x12, 0x34, 0x56, 0x78])
                else:
                    fresh = bytes((0x11 + 0x13 * i) & 0xFF for i in range(self.seed_len))
                self.seed = fresh if not self.unlocked else bytes(self.seed_len)
                self._reply(bytes([0x67, sub]) + self.seed)
            else:
                if self.bad_keys >= 3:
                    return self._nrc(sid, 0x36)
                if m[2:] == self.key_fn(self.seed, sub - 1):
                    self.unlocked = True
                    self._reply(bytes([0x67, sub]))
                else:
                    self.bad_keys += 1
                    self._nrc(sid, 0x35)
        elif sid in (0x28, 0x85):
            if not sup:
                self._reply(bytes([sid + 0x40, sub & 0x7F]))
        elif sid == 0x31:
            rid = int.from_bytes(m[2:4], "big")
            if not self.unlocked:
                return self._nrc(sid, 0x33)
            if rid == 0xFF00:
                for _ in range(self.erase_pending):
                    self._nrc(sid, 0x78)
                    time.sleep(0.1)
                self._reply(b"\x71\x01\xFF\x00\x00")
            elif rid in self.crc_rids:
                ok = m[4:8] == (self.rx_crc & 0xFFFFFFFF).to_bytes(4, "big")
                self.crc_checked.append((rid, ok))
                if ok:
                    self._reply(b"\x71" + bytes([sub & 0x7F]) + m[2:4] + b"\x00")
                else:
                    self._nrc(sid, 0x72)
            elif rid == 0xFF01:
                ok = m[4:8] == (self.rx_crc & 0xFFFFFFFF).to_bytes(4, "big")
                self._reply(b"\x71\x01\xFF\x01" + (b"\x00" if ok else b"\x01"))
                if not ok:
                    pass
            else:
                self._nrc(sid, 0x31)
        elif sid == 0x34:
            if not self.unlocked:
                return self._nrc(sid, 0x33)
            alfi = m[2]
            al, sl = alfi & 0xF, alfi >> 4
            self.dl_addr = int.from_bytes(m[3:3 + al], "big")
            self.dl_len = int.from_bytes(m[3 + al:3 + al + sl], "big")
            self.dl_got, self.expect_seq, self.rx_crc = 0, 1, 0
            self._reply(b"\x74\x20" + self.max_block.to_bytes(2, "big"))
        elif sid == 0x36:
            if m[1] != self.expect_seq:
                return self._nrc(sid, 0x73)
            data = m[2:]
            for i, b in enumerate(data):
                self.mem[self.dl_addr + self.dl_got + i] = b
            self.rx_crc = zlib.crc32(data, self.rx_crc)
            self.dl_got += len(data)
            self.expect_seq = (self.expect_seq + 1) & 0xFF
            self._reply(bytes([0x76, m[1]]))
        elif sid == 0x37:
            self._reply(b"\x77")
        elif sid == 0x11:
            self.reset_count += 1
            self.session, self.unlocked = 1, False
            if not sup:
                self._reply(bytes([0x51, sub & 0x7F]))
        else:
            self._nrc(sid, 0x11)


def make_sim(addr, profile: str = "new", **kw):
    """(tester_bus, SimEcu) for the two product generations:
         old: 2-byte seed/key, CRC routine 31 01 DF FE
         new: 16-byte seed/key, CRC routine 31 01 01 02"""
    from .bus import VirtualBus
    a, b = VirtualBus.pair()
    prof = dict(seed_len=16, crc_rids=(0x0102,)) if profile == "new" else \
        dict(seed_len=2, crc_rids=(0xDFFE,))
    prof.update(kw)
    return a, SimEcu(b, req_id=addr.phys, rsp_id=addr.resp, **prof)
