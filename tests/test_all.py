import logging, os, sys, time, unittest, zlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.bus import VirtualBus, CanFrame
from zdiag.isotp import IsoTp, IsoTpTimeout, IsoTpError
from zdiag.uds import UdsClient, NegativeResponse, UdsTimeout
from zdiag.sim_ecu import SimEcu
from zdiag.seedkey import xor_demo_key
from zdiag.flash import Flasher, FlashConfig, Segment, parse_ihex, parse_srec


def rig(**kw):
    a, b = VirtualBus.pair()
    ecu = SimEcu(b, **kw)
    tp = IsoTp(a, tx_id=0x7E0, rx_id=0x7E8)
    return a, ecu, tp, UdsClient(tp)


class IsoTpLoop(unittest.TestCase):
    def echo(self, n, fd_dl=8, big=False):
        a, b = VirtualBus.pair()
        t1 = IsoTp(a, 0x1, 0x2, tx_dl=fd_dl, st_min=0, block_size=4)
        t2 = IsoTp(b, 0x2, 0x1, tx_dl=fd_dl, st_min=0, block_size=4)
        msg = bytes((i * 7) & 0xFF for i in range(n))
        got = {}
        import threading
        th = threading.Thread(target=lambda: got.setdefault("m", t2.recv(3)))
        th.start(); t1.send(msg); th.join()
        self.assertEqual(got["m"], msg)

    def test_lengths(self):
        for n in (1, 6, 7, 8, 9, 13, 100, 4095, 5000):
            with self.subTest(n=n): self.echo(n)

    def test_fd(self):
        for n in (1, 62, 63, 64, 200, 5000):
            with self.subTest(n=n): self.echo(n, fd_dl=64)

    def test_stmin_respected(self):
        a, b = VirtualBus.pair()
        t1 = IsoTp(a, 1, 2)
        t2 = IsoTp(b, 2, 1, st_min=5, block_size=0)
        import threading
        th = threading.Thread(target=lambda: t2.recv(3)); th.start()
        t1.send(bytes(50)); th.join()
        cfs = [f.timestamp for f in a.log][1:]
        gaps = [y - x for x, y in zip(cfs, cfs[1:])]
        self.assertTrue(all(g >= 0.0045 for g in gaps), gaps)

    def test_n_bs_timeout(self):
        a, b = VirtualBus.pair()
        t1 = IsoTp(a, 1, 2, n_bs=0.2)
        with self.assertRaises(IsoTpTimeout) as c: t1.send(bytes(30))
        self.assertEqual(c.exception.which, "N_Bs")

    def test_n_cr_timeout(self):
        a, b = VirtualBus.pair()
        t2 = IsoTp(b, 2, 1, n_cr=0.2)
        a.send(CanFrame(1, bytes([0x10, 30]) + bytes(6)))        # FF, then silence
        with self.assertRaises(IsoTpTimeout) as c: t2.recv(1)
        self.assertEqual(c.exception.which, "N_Cr")

    def test_bad_sn(self):
        a, b = VirtualBus.pair()
        t2 = IsoTp(b, 2, 1)
        a.send(CanFrame(1, bytes([0x10, 30]) + bytes(6)))
        a.send(CanFrame(1, bytes([0x23]) + bytes(7)))
        with self.assertRaises(IsoTpError): t2.recv(1)


class UdsTests(unittest.TestCase):
    def setUp(self): self.bus, self.ecu, self.tp, self.u = rig()
    def tearDown(self): self.ecu.stop()

    def test_multiframe_and_noise_ignored(self):
        self.assertEqual(self.u.read_did(0xF190), b"LSVAB12345C000001")
        self.assertEqual(self.u.read_did(0xF1A0), bytes(range(100)))

    def test_nrc(self):
        with self.assertRaises(NegativeResponse) as c: self.u.read_did(0x1234)
        self.assertEqual(c.exception.nrc, 0x31)
        with self.assertRaises(NegativeResponse) as c: self.u.request(b"\x99")
        self.assertEqual(c.exception.nrc, 0x11)

    def test_timeout(self):
        self.ecu.stop(); self.u.p2 = 0.1
        with self.assertRaises(UdsTimeout): self.u.read_did(0xF190)

    def test_security_flow(self):
        with self.assertRaises(NegativeResponse) as c: self.u.security_access(xor_demo_key)
        self.assertEqual(c.exception.nrc, 0x7F)                 # default session
        self.u.session(0x03)
        self.assertTrue(self.u.security_access(xor_demo_key))
        self.assertTrue(self.ecu.unlocked)
        self.assertFalse(self.u.security_access(xor_demo_key))   # zero seed -> already unlocked
        self.u.stop_keepalive()

    def test_wrong_key(self):
        self.u.session(0x03)
        with self.assertRaises(NegativeResponse) as c:
            self.u.security_access(lambda s, l: b"\0\0\0\0")
        self.assertEqual(c.exception.nrc, 0x35)
        self.u.stop_keepalive()

    def test_pending_uses_p2_star(self):
        self.u.session(0x03); self.u.security_access(xor_demo_key)
        self.u.p2 = 0.15                                          # 0x78 comes every 100 ms
        t = time.time()
        self.u.routine(0x01, 0xFF00, bytes(8))
        self.assertGreater(time.time() - t, 0.19)
        self.u.stop_keepalive()

    def test_suppress_positive(self):
        self.u.p2 = 0.1
        self.assertEqual(self.u.request(b"\x3E\x00", suppress_positive=True), b"")
        self.assertEqual(self.ecu.tester_present_count, 1)

    def test_keepalive_never_interleaves(self):
        self.u.session(0x03)
        self.u.start_keepalive(0.05)
        t0 = time.time()
        while time.time() - t0 < 0.5: self.u.read_did(0xF1A0)   # multi-frame under load
        self.u.stop_keepalive()
        self.assertGreater(self.ecu.tester_present_count, 2)

    def test_back_to_back_requests(self):
        for i in range(30):
            self.assertEqual(self.u.read_did(0xF190)[:3], b"LSV")


class FlashTests(unittest.TestCase):
    def test_hex_srec_parsers(self):
        hx = ":020000040800F2\n:0400000001020304F2\n:0400040005060708DE\n:00000001FF\n"
        segs = parse_ihex(hx)
        self.assertEqual((segs[0].addr, segs[0].data), (0x08000000, bytes(range(1, 9))))
        sr = "S1070000010203F2\nS9030000FC\n"
        # S1: addr 0x0000, data 01 02 03? length 07 -> 2 addr + 3 data + chk
        s = parse_srec(sr); self.assertEqual(s[0].data, b"\x01\x02\x03")

    def test_full_flash(self):
        bus, ecu, tp, u = rig()
        try:
            image = [Segment(0x08000000, os.urandom(3000)), Segment(0x08010000, os.urandom(777))]
            stages = []
            cfg = FlashConfig(boot_delay=0.0)
            Flasher(u, xor_demo_key, cfg).run(image, lambda s, d, t: stages.append(s))
            for seg in image:
                got = bytes(ecu.mem[seg.addr + i] for i in range(len(seg.data)))
                self.assertEqual(got, seg.data)
            self.assertEqual(ecu.reset_count, 1)
            self.assertIn("transfer", stages); self.assertEqual(stages[-1], "done")
        finally:
            ecu.stop()

    def test_flash_corruption_detected(self):
        bus, ecu, tp, u = rig()
        try:
            from zdiag.flash import FlashError
            bad = FlashConfig(boot_delay=0.0, check_data=lambda s: b"\xDE\xAD\xBE\xEF")
            with self.assertRaises(FlashError):
                Flasher(u, xor_demo_key, bad).run([Segment(0, bytes(100))])
            self.assertEqual(ecu.reset_count, 0)         # must not reset after a failed verify
        finally:
            ecu.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
