"""The example frames in docs/CFT_文件传输协议规范.md (chapter 13) must be exactly what the code produces."""
import os
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from zdiag import cft
from zdiag.bus import VirtualBus
from zdiag.cft import CftConfig, CftError, Link, Messenger, Sender
import threading


class SpecVectors(unittest.TestCase):
    def test_crc32_check_value(self):
        self.assertEqual(zlib.crc32(b"123456789") & 0xFFFFFFFF, 0xCBF43926)

    def test_data_frames(self):
        data = bytes(range(1, 11))
        self.assertEqual(zlib.crc32(data) & 0xFFFFFFFF, 0x2520577B)
        stream = data + struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)
        p, head = 5, bytes((1, 0))
        got = [(head + bytes((i,)) + stream[i * p:(i + 1) * p].ljust(p, b"\0")).hex() for i in range(3)]
        self.assertEqual(got, ["0100000102030405", "010001060708090a", "0100022520577b00"])

    def test_hello_fragments_and_ack(self):
        a, b = VirtualBus.pair()
        c = CftConfig()
        l = Link(0, a, False, False, c, "x")
        m = Messenger([l], c.id_s2r, threading.Event(), lambda x: True)
        m._mid = 0                                                         # first message number = 1 (the start value is arbitrary)
        body = bytes([cft.M_HELLO]) + struct.pack(">BIQHI", 2, 3, 1000, 128, 0x11223344)
        self.assertEqual(body.hex(), "01020000000300000000000003e8008011223344")
        def go():
            try:
                m.send(cft.M_HELLO, body[1:], timeout=0.4, tries=1)
            except CftError:
                pass                                                        # nobody acks in this test
        t = threading.Thread(target=go, daemon=True); t.start()
        frames = [b.recv(1.0) for _ in range(5)]
        t.join(2)
        self.assertEqual([f.data.hex() for f in frames], ["0101000401020000", "0101010400030000", "0101020400000000",
                                                           "0101030403e80080", "0101840411223344"])
        self.assertTrue(all(f.can_id == 0x6A0 for f in frames))

    def test_offer_and_status(self):
        data = bytes(range(1, 11))
        rel = b"a.bin"
        o = bytes([cft.M_OFFER]) + struct.pack(">BBIQQIHH", 1, 0, 0, 10, 1700000000000, zlib.crc32(data) & 0xFFFFFFFF, 1024, len(rel)) + rel
        self.assertEqual(len(o), 36)
        self.assertEqual(" ".join(f"{x:02X}" for x in o), "03 01 00 00 00 00 00 00 00 00 00 00 00 00 0A 00 00 01 8B CF E5 68 00 "
                                                          "25 20 57 7B 04 00 00 05 61 2E 62 69 6E")
        st = bytes([cft.T_STATUS, 1]) + struct.pack(">I", 5) + bytes([0b101, 0])
        self.assertEqual(st.hex(), "0401000000050500")

    def test_chunk_size_limit_counts_the_crc(self):
        a, _ = VirtualBus.pair()
        c = CftConfig(chunk_bytes=1280)                                   # 1280 + 4 CRC bytes = 257 frames of 5: one too many
        with self.assertRaises(CftError):
            Sender([Link(0, a, False, False, c, "x")], c)
        c2 = CftConfig(chunk_bytes=1276)
        Sender([Link(0, a, False, False, c2, "x")], c2)


if __name__ == "__main__":
    unittest.main()
