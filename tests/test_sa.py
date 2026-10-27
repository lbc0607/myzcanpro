import os, shutil, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.bus import VirtualBus
from zdiag.isotp import IsoTp
from zdiag.uds import UdsClient
from zdiag.sim_ecu import SimEcu
from zdiag.seedkey import ZlgKeyDll, SerialAwareKey, SeedKeyError

HERE = os.path.join(os.path.dirname(__file__), "mock")
SERIAL = b"SN2024ABCD0001"


def expected_key(seed, level, serial, a, b):          # independent re-statement of the mock algorithm
    return bytes(s ^ serial[i % len(serial)] ^ ((a * 16 + b) & 0xFF) ^ level for i, s in enumerate(seed))


@unittest.skipUnless(shutil.which("g++") and sys.platform.startswith("linux"), "needs g++ on Linux (mock .so)")
class SerialFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.so = os.path.join(cls.tmp, "serial_key.so")
        cls.demo = os.path.join(cls.tmp, "sa_demo.so")
        subprocess.check_call(["g++", "-shared", "-fPIC", "-o", cls.so, os.path.join(HERE, "serial_key.cpp")])
        subprocess.check_call(["g++", "-shared", "-fPIC", "-o", cls.demo, os.path.join(HERE, "sa.cpp")])

    @classmethod
    def tearDownClass(cls): shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_real_demo_source_marshalling(self):
        k = ZlgKeyDll(self.demo, workdir=self.tmp, reload=False)(bytes([1, 2, 0xFF, 0x10]), 1)
        self.assertEqual(k, bytes([2, 3, 0x00, 0x11]))            # demo algorithm: seed[i] + 1

    def run_case(self, req, rsp, ab):
        wd = tempfile.mkdtemp(dir=self.tmp)
        a, b = VirtualBus.pair()
        ecu = SimEcu(b, req_id=req, rsp_id=rsp, serial=SERIAL,
                     key_fn=lambda seed, lvl: expected_key(seed, lvl, SERIAL, *ab))
        try:
            u = UdsClient(IsoTp(a, tx_id=req, rx_id=rsp))
            sak = SerialAwareKey(ZlgKeyDll(self.so, workdir=wd, reload=False))
            u.session(0x03)
            self.assertTrue(u.security_access(sak, 0x01))
            u.stop_keepalive()
            self.assertTrue(ecu.unlocked)
            with open(os.path.join(wd, "ECUserialnumber.txt"), "rb") as f:
                self.assertEqual(f.read(), SERIAL + b"\n" + str(ab[0]).encode() + b"\n" + str(ab[1]).encode() + b"\n")
        finally:
            ecu.stop()

    def test_7e2(self): self.run_case(0x7E2, 0x7EA, (0, 0))
    def test_7e3(self): self.run_case(0x7E3, 0x7EB, (1, 3))
    def test_7e8_by_response_id(self): self.run_case(0x7E0, 0x7E8, (2, 4))

    def test_unknown_id_refuses(self):
        a, b = VirtualBus.pair(); ecu = SimEcu(b, req_id=0x700, rsp_id=0x708, serial=SERIAL)
        try:
            u = UdsClient(IsoTp(a, tx_id=0x700, rx_id=0x708)); u.session(0x03)
            sak = SerialAwareKey(ZlgKeyDll(self.so, workdir=self.tmp, reload=False))
            with self.assertRaises(SeedKeyError): u.security_access(sak, 0x01)
            self.assertEqual(ecu.bad_keys, 0)                      # no key was ever sent
            u.stop_keepalive()
        finally: ecu.stop()

    def test_stale_file_is_replaced(self):                          # second ECU must not see first serial
        wd = tempfile.mkdtemp(dir=self.tmp)
        open(os.path.join(wd, "ECUserialnumber.txt"), "w").write("OLDSERIAL\n9\n9\n")
        self.run_case_with(wd)
    def run_case_with(self, wd):
        a, b = VirtualBus.pair()
        ecu = SimEcu(b, serial=SERIAL, key_fn=lambda s, l: expected_key(s, l, SERIAL, 2, 4))
        try:
            u = UdsClient(IsoTp(a, 0x7E0, 0x7E8)); u.session(0x03)
            u.security_access(SerialAwareKey(ZlgKeyDll(self.so, workdir=wd, reload=False)), 1)
            u.stop_keepalive(); self.assertTrue(ecu.unlocked)
        finally: ecu.stop()

    def test_serial_padding_stripped(self):
        sak = SerialAwareKey(None)
        self.assertEqual(sak.lines_for(0x7E3, 0x7EB, b"AB12\xff\xff\x00"), "AB12\n1\n3\n")
        self.assertEqual(SerialAwareKey(None, serial_format="hex").lines_for(0x7E2, 0, b"\x01\xAB"), "01AB\n0\n0\n")

    def test_flash_uses_serial_aware_key(self):
        from zdiag.flash import Flasher, FlashConfig, Segment
        wd = tempfile.mkdtemp(dir=self.tmp)
        a, b = VirtualBus.pair()
        ecu = SimEcu(b, serial=SERIAL, key_fn=lambda s, l: expected_key(s, l, SERIAL, 2, 4))
        try:
            u = UdsClient(IsoTp(a, 0x7E0, 0x7E8))
            sak = SerialAwareKey(ZlgKeyDll(self.so, workdir=wd, reload=False))
            Flasher(u, sak, FlashConfig(boot_delay=0.0)).run([Segment(0x1000, bytes(range(200)))])
            self.assertEqual(ecu.reset_count, 1)
        finally: ecu.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
